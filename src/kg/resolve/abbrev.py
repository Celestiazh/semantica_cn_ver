# -*- coding: utf-8 -*-
"""简称→全称提名（--resolve 的常规步骤，非事后补丁）。

定位：事项/角色/人员边里真实出现、但不在名册的 ORG 写法（多为简称，如
「工务署」「方大公司」「省安公司」），自动提名对应的名单全称，人工确认后
沉淀为名册别名 —— 下一轮归一自动生效，不再事后手工补。

通道（宁缺勿滥，只提名不判定）：
  ① 规则：简称 ⊂ 某全称（含后缀式，如「工务署」⊂「…建筑工务署」）；
  ② 模型：provider 非 none 时，让模型在名单内选全称并分类
     （公司简称 / 部门内设 / 站点地标 / 无匹配）——不确定必须留空。

产出：var/resolve/review_alias.csv（人工填「决定」= 合并 / 驳回 / 部门保留）。
沉淀（--resolve --apply）：合并 → roster_extra.yaml 别名；其余 → abbrev_decided_extra.yaml
（留痕，且提名单跳过已决定项）。

⛔ 分类口径：项目部/分部/监理部/工区/站点等「内设机构」不是独立法人，
   默认归为「部门保留」类候选，不建公司别名。
"""
import csv
import json
import re
import time
from pathlib import Path

# 内设机构/站点特征（不作为公司别名的对象，单独归类给人工确认）
INTERNAL_RE = re.compile(r"(?:项目部|分部|工区|标段|监理部|总监办|工程部|工作站|站)$")
ROLE_SET = {"施工单位", "监理单位", "建设单位", "代建单位", "设计单位", "总承包单位",
            "总包单位", "分包单位", "使用单位", "物业公司", "业主单位", "检测单位",
            "机电单位", "幕墙单位", "钢结构单位", "样板装修单位", "第三方监测单位"}


def collect_abbrev_orgs(graph, alias_map, ev_index, min_edges=1):
    """名册外、有业务边、非角色词的 ORG → [{name, edges, ev}]（按业务边数降序）。"""
    alias_targets = set(alias_map.values()) | set(alias_map.keys())
    biz = {}
    for r in graph.get("relationships") or []:
        ty = r.get("type")
        if ty not in ("单位角色", "事项责任单位", "所属单位"):
            continue
        for end in (r.get("source"), r.get("target")):
            if end and end not in biz:
                biz[end] = 0
            if end in biz:
                biz[end] += 1
    out = []
    for e in graph.get("entities") or []:
        if e.get("type") != "ORG":
            continue
        name = e.get("name", "")
        if (not name or len(name) < 2 or name in alias_targets
                or name in ROLE_SET or biz.get(e["id"], 0) < min_edges):
            continue
        out.append({"name": name, "edges": biz[e["id"]],
                    "ev": (ev_index.get(name) or [])[:2]})
    out.sort(key=lambda x: -x["edges"])
    return out


def rule_match(short, fulls):
    """包含规则：简称 ⊂ 全称（简称 ≥2 字），取最短全称。返回 (全称, 依据) 或 None。"""
    if len(short) < 2:
        return None
    best = None
    for f in sorted(fulls, key=len):
        if short in f and (best is None or len(f) < len(best[0])):
            best = (f, "包含（简称⊂全称）")
    return best


def _llm_one(provider, short, fulls, ev_hint):
    """问模型：简称对应名单里哪个全称。返回 (全称或"", 类型或"")。"""
    names = "\n".join("- " + f for f in fulls)
    prompt = (
        "你是建筑行业单位名称归一助手。\n"
        "【简称】%s\n【该简称出现的文件线索】%s\n【单位全称名单】\n%s\n\n"
        "判断简称最可能对应名单里的哪个全称。\n"
        "输出严格 JSON（不要多余文字）：\n"
        '{"全称":"名单中的某一项，或空字符串","类型":"公司简称|部门内设|站点地标|其他"}\n'
        "规则：不确定就输出空字符串；禁止输出名单里没有的全称；"
        "如果简称更像项目部/监理部/站点等内设机构而非公司，类型填「部门内设」或「站点地标」且全称留空。"
        % (short, ev_hint or "（无）", names))
    payload = {"model": provider.model, "temperature": 0, "max_tokens": 300,
               "messages": [{"role": "user", "content": prompt}]}
    if getattr(provider, "reasoning_effort", ""):
        payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
    data = provider._post(provider.base_url + "/chat/completions", payload,
                          {"Authorization": "Bearer " + provider.api_key
                           if provider.api_key else ""})
    txt = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return "", ""
    try:
        d = json.loads(m.group(0))
        return (d.get("全称") or "").strip(), (d.get("类型") or "").strip()
    except Exception:                                    # noqa: BLE001
        return "", ""


def propose_abbrev(graph, alias_map, provider, root, ev_index=None, limit=60,
                   decided=None):
    """主入口：返回简称提名行列表。decided：已决定过的简称集合（跳过）。"""
    root = Path(root) if root else Path(".")
    ev_index = ev_index or {}
    decided = decided or {}
    fulls = sorted(set(alias_map.values()))              # 名册全称（含别名展开前的正名）
    orgs = collect_abbrev_orgs(graph, alias_map, ev_index)
    rows = []
    for o in orgs[: max(limit * 3, limit)]:
        name = o["name"]
        if name in decided:
            continue
        if INTERNAL_RE.search(name):
            rows.append({"简称": name, "事项数": o["edges"], "提名全称": "",
                         "依据": "内设机构特征（项目部/监理部/站点等）", "类型": "部门内设",
                         "ev": o["ev"]})
            continue
        hit = rule_match(name, fulls)
        if hit:
            rows.append({"简称": name, "事项数": o["edges"], "提名全称": hit[0],
                         "依据": hit[1], "类型": "公司简称", "ev": o["ev"]})
            continue
        if provider is not None:
            ev_hint = "；".join(Path(str(x.get("file", ""))).name
                                for x in o["ev"] if x.get("file"))[:120]
            full, typ = _llm_one(provider, name, fulls, ev_hint)
            if full:
                rows.append({"简称": name, "事项数": o["edges"], "提名全称": full,
                             "依据": "模型提名", "类型": typ or "公司简称", "ev": o["ev"]})
            elif typ and typ != "公司简称":
                rows.append({"简称": name, "事项数": o["edges"], "提名全称": "",
                             "依据": "模型判为" + typ, "类型": typ, "ev": o["ev"]})
            if len(rows) >= limit:
                break
    return rows[:limit]


# ---- 人工确认清单：列名的机器↔人话映射 -------------------------------------
# 内部键保持稳定（settle_alias 依赖），文件里用人话表头
ALIAS_HEADERS = [
    ("简称", "图谱里的写法"),
    ("提名全称", "建议归一到的全称"),
    ("依据", "系统判断依据"),
    ("事项数", "关联事项数"),
    ("决定", "你的决定(合并/驳回/部门保留)"),
    ("备注", "备注(可不填)"),
    ("证据文件", "出现过的文件"),
]
_H2K = {h: k for k, h in ALIAS_HEADERS}          # 文件表头 → 内部键
DEC_PRESET = "部门保留"                          # 内设机构行的预填决定
PRESET_NOTE = "系统预填：内设机构默认保留；不同意可改"
DEC_FULL = "保留"                                # 已是完整单位名的预填决定
FULL_ENDINGS = ("有限公司", "股份公司", "有限责任公司", "集团公司")


def _is_internal(r):
    return r.get("类型") == "部门内设" or "内设机构" in (r.get("依据") or "")


def _is_full_name(r):
    """写法本身已是完整单位名（以 公司/集团 等结尾）——不是简称，无需归一。"""
    n = (r.get("简称") or "").strip()
    return n.endswith(FULL_ENDINGS)


def _row_dec(r):
    """行的预填决定：内设机构→部门保留；已是全称→保留；其余留空待人判。"""
    if _is_internal(r):
        return DEC_PRESET
    if _is_full_name(r):
        return DEC_FULL
    return ""


def _row_note(r):
    if _is_internal(r):
        return PRESET_NOTE
    if _is_full_name(r):
        return "系统预填：已是完整单位名，无需归一；不同意可改"
    return ""


def _howto_rows():
    """「怎么填」说明页内容。"""
    return [
        ["这张表是干什么的"],
        ["图谱里出现了一批单位的简写（如“工务署”“省安公司”），需要你确认它们对应哪家全称单位。"
         "你确认后，系统会自动把该写法名下的所有事项/角色归到全称名下，今后也不再重复询问。"],
        [""],
        ["你只需要判断「建议归一到的全称」不是空的行！其余两类已由系统预填（见备注列）"],
        ["第二列（全称）非空", "系统给出了提名 → 你来判断：同一家填“合并”；不是填“驳回”。"
                             "若提名的全称本身错了，直接把那格改成正确全称，再填“合并”"],
        ["第二列为空·备注=内设机构预填", "项目部/监理部/站点等，已预填“部门保留”——默认无需处理，不同意再改"],
        ["第二列为空·备注=完整单位名预填", "它本身就是全称（名册里暂没有而已），已预填“保留”——无需处理"],
        ["第二列为空·备注也是空", "系统猜不出是哪家的简称（如“方大公司”）→ 三选一："
                                 "① 你知道全称就填进第二列并填“合并”；② 不是独立公司/不需要归一填“保留”；"
                                 "③ 不确定就留空，下轮再问"],
        [""],
        ["「你的决定」四种填法（下拉可选）"],
        ["合并", "左边的写法 = 右边的全称，是同一家，系统自动归一"],
        ["驳回", "不是同一家。系统记下“这俩不是一家”，以后不再提名"],
        ["部门保留", "内设机构（项目部/监理部等），保留原样，以后不再提名"],
        ["保留", "该写法无需归一（已是全称等），保留原样，以后不再提名"],
        [""],
        ["其他列说明"],
        ["系统判断依据", "规则命中 / 模型提名 / 内设机构特征 —— 供参考"],
        ["关联事项数", "这个写法名下挂了多少条事项（事项越多，归一收益越大）"],
        ["出现过的文件", "该写法在哪几份纪要的第几页出现过（文件名·第N页，可直接翻原文核对）"],
        [""],
        ["填完后"],
        ["保存本文件，告诉助手执行 --resolve --apply 即可生效。只填“你的决定”列（和改错的全称），其他列不用动"],
    ]


def read_review_alias_rows(alias_csv_p):
    """读取人工填写的简称决定：xlsx 比 csv 新（或 csv 不存在）→ 读 xlsx；否则回退 csv。

    返回决定行列表（可能为空）；两侧都读不了返回 None（调用方自行兜底）。
    ★ 供"先读后写"收割使用——apply 重生成清单前必须先调用本函数保住人工输入。"""
    from pathlib import Path as _P
    from openpyxl import load_workbook
    alias_csv_p = _P(alias_csv_p)
    alias_xlsx_p = alias_csv_p.with_suffix(".xlsx")
    if alias_xlsx_p.is_file() and (not alias_csv_p.is_file()
                                   or alias_xlsx_p.stat().st_mtime > alias_csv_p.stat().st_mtime):
        try:
            wb = load_workbook(alias_xlsx_p, read_only=True, data_only=True)
            ws = wb["候选清单"] if "候选清单" in wb.sheetnames else wb.active
            hdr = [str(c.value or "").strip() if c.value else ""
                   for c in next(ws.iter_rows(min_row=1, max_row=1))]
            _h2k = {h: k for k, h in ALIAS_HEADERS}
            arows = []
            for r in ws.iter_rows(min_row=2, values_only=True):
                d = {}
                for h, v in zip(hdr, r):
                    k = _h2k.get(h, h)
                    if k in ("简称", "提名全称", "决定", "备注"):
                        d[k] = str(v).strip() if v is not None else ""
                if d.get("简称"):
                    arows.append(d)
            wb.close()
            return arows
        except Exception:                                 # noqa: BLE001 xlsx 读不了退回 csv
            return None
    if alias_csv_p.is_file():
        return read_review_alias_csv(alias_csv_p)
    return None


def _ev_str(r):
    """「出现过的文件」列：文件名·第N页（页码是定位原文用的，缺页码时只给文件名）。"""
    parts = []
    for x in (r.get("ev") or []):
        name = Path(str(x.get("file", ""))).name
        if not name:
            continue
        pg = x.get("page")
        parts.append("%s·第%s页" % (name, pg) if pg not in (None, "") else name)
    return "；".join(parts)


def write_review_alias_csv(path, rows):
    """生成人工确认清单：CSV（机器交换）+ 同名 xlsx（人填，说明页+下拉+内设机构预填）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [h for _, h in ALIAS_HEADERS]

    def cells(r):
        return [r["简称"], r.get("提名全称", ""), r.get("依据", ""),
                r.get("事项数", 0), _row_dec(r), _row_note(r), _ev_str(r)]

    # ① CSV（机器交换；被 Excel 锁住时跳过——apply 直接读 xlsx，不依赖它）
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for r in rows:
                w.writerow(cells(r))
    except PermissionError:
        pass

    # ② xlsx：说明页 + 数据页（决定列下拉；内设机构预填部门保留）
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws0 = wb.active
        ws0.title = "怎么填"
        for i, row in enumerate(_howto_rows(), 1):
            ws0.cell(row=i, column=1, value=row[0])
            if len(row) > 1:
                ws0.cell(row=i, column=2, value=row[1])
            if i == 1:
                ws0.cell(row=i, column=1).font = Font(bold=True, size=14)
            elif row[0] in ("你只需要判断「建议归一到的全称」不是空的行！",
                            "「你的决定」只有三种填法（下拉可选）", "其他列说明", "填完后"):
                ws0.cell(row=i, column=1).font = Font(bold=True, size=12)
            elif row[0]:
                ws0.cell(row=i, column=1).font = Font(bold=True)
        ws0.column_dimensions["A"].width = 32
        ws0.column_dimensions["B"].width = 95

        ws = wb.create_sheet("候选清单")
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="FFF2CC")
        for r in rows:
            ws.append(cells(r))
        widths = [22, 34, 22, 11, 26, 34, 42]
        for i, wd in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = wd
        ws.freeze_panes = "A2"
        dv = DataValidation(type="list", formula1='"合并,驳回,部门保留,保留"', allow_blank=True,
                            promptTitle="你的决定", prompt="合并=同一家；驳回=不是一家；部门保留/保留=原样不动。留空=下轮再问",
                            errorTitle="无效输入", error="只能填：合并 / 驳回 / 部门保留 / 保留（或留空）")
        ws.add_data_validation(dv)
        n = max(len(rows), 1)
        dv.add("E2:E%d" % (n + 1))
        xlsx = path.with_suffix(".xlsx")
        try:
            wb.save(xlsx)
        except PermissionError:
            # 预览/Excel 正开着旧文件 → 落到新版本名，不打断
            xlsx = path.with_name(path.stem + "_new.xlsx")
            wb.save(xlsx)
    except Exception:                                    # noqa: BLE001 —— openpyxl 缺席时 CSV 仍可用
        xlsx = None
    return path if xlsx is None else xlsx


def read_review_alias_csv(path):
    """读人工确认清单；新旧表头都认，统一映射回内部键。"""
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        out = []
        for raw in csv.DictReader(f):
            row = {}
            for k, v in raw.items():
                if k is None:
                    continue
                row[_H2K.get(k.strip(), k.strip())] = v
            out.append(row)
        return out


def settle_alias(rows, cfg_dir, decided_by="人工确认(alias_csv)"):
    """决定=合并 → roster_extra 别名；驳回/部门保留 → abbrev_decided_extra.yaml 留痕。

    返回 {"合并": n, "其他": n, "未填": n}。"""
    from .apply import _dump, _load
    cfg_dir = Path(cfg_dir)
    extra_roster = _load(cfg_dir / "roster_extra.yaml") or {"units": []}
    units_by_name = {u["name"]: u for u in extra_roster.get("units") or []}
    dec_path = cfg_dir / "abbrev_decided_extra.yaml"
    decided = _load(dec_path) or {"abbrevs": []}
    decided_keys = {(d.get("简称"), d.get("决定")) for d in decided.get("abbrevs") or []}

    today = time.strftime("%Y-%m-%d")
    stat = {"合并": 0, "其他": 0, "未填": 0}
    for r in rows:
        d = (r.get("决定") or "").strip()
        short = (r.get("简称") or "").strip()
        if not short:
            continue
        if not d:
            stat["未填"] += 1
            continue
        key = (short, d)
        if key in decided_keys:
            continue
        decided.setdefault("abbrevs", []).append(
            {"简称": short, "提名全称": r.get("提名全称", ""), "决定": d,
             "备注": r.get("备注", ""), "decided_by": decided_by, "decided_at": today})
        decided_keys.add(key)
        decided["updated"] = today
        if d == "合并":
            canon = (r.get("提名全称") or "").strip()
            if not canon:
                continue
            if canon in units_by_name:
                if short not in units_by_name[canon]["aliases"]:
                    units_by_name[canon]["aliases"].append(short)
            else:
                units_by_name[canon] = {"name": canon, "aliases": [short], "type": ""}
                extra_roster["units"].append(units_by_name[canon])
            extra_roster.setdefault("updated", today)
            stat["合并"] += 1
        else:
            stat["其他"] += 1

    _dump(cfg_dir / "roster_extra.yaml", extra_roster)
    _dump(dec_path, decided)
    return stat
