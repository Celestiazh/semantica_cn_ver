# -*- coding: utf-8 -*-
"""②′ 实体质检：LLM 裁判当"提名"，人工点头才从图里移除。

背景（2026-09-30 实验实测）：表格单元格会把「配合设计院」「协调物业公司」这类
**泛称/整句碎片**读成 ORG 实体 —— 静态规则无法判断"是不是一个具体机构"，
而 LLM 二分类在这个环节明显更强（59 个样本抓出 8 个泛称、具体名零误杀）。

分工（用户拍板）：
    规则抽（不动）→ LLM 裁判当"提名"（只分类，不删）→ 泛称候选进人工确认清单
    （var/resolve/review_qc.csv）→ 人填「决定」→ 沉淀进 config/qc_removed.yaml
    → 下次抽取组装图时**配置驱动移除**，人的活不再重复。

⛔ 跨轮可复现性实测：temperature=0 下边界样本仍会翻面（泛称 7↔8）——
   所以 LLM **只能提名，不能自动删**；"不确定"也不删（宁缺不错，留人工裁决）。

用法：
    run.py --qc                 # LLM 裁判 → 产出/更新 review_qc.csv（已填决定不覆盖）
    run.py --resolve --apply    # 与消解同一命令：顺带读回 qc 决定 → 沉淀 qc_removed.yaml
"""
import csv
import json
import time
from pathlib import Path

import csv
import json
import re
import time
from pathlib import Path

GRAPH = Path("var") / "extract" / "graph_raw.json"
QC_CSV = Path("var") / "resolve" / "review_qc.csv"
REMOVED_YAML = "qc_removed.yaml"
KEPT_YAML = "qc_kept.yaml"
PREFIXES_YAML = "qc_prefixes.yaml"           # 人工确认的「人名开头」剥离前缀

HEADER = ["实体名", "模型判定", "理由", "原文片段", "剥离前缀", "决定", "决定人"]


def _unit_core(cfg):
    """单位名 fullmatch 模式（与 meeting.py 同源，从配置词表拼出来）。"""
    mc = cfg.get("extract.meeting") or {}
    alt = "|".join(mc.get("unit_suffixes") or
                   ["公司", "集团", "院", "所", "中心", "部", "局", "署", "大学"])
    return re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:" + alt + ")")


def load_prefixes(cfg):
    """person_prefixes + qc_prefixes.yaml 合并（extract 组装 known_prefixes 用）。"""
    import yaml
    manual = list(cfg.get("extract.meeting.person_prefixes") or [])
    p = Path(cfg.config_dir) / PREFIXES_YAML
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            manual += list(data.get("prefixes") or [])
        except Exception:
            pass
    return sorted(set(manual))

PROMPT = (
    "你是机构名称判别器。下面每条给出一个从工程监理纪要中抽出的「单位名」，"
    "以及它出现处的原文片段。判断这个名字：\n"
    "  - 具体名：能唯一指向一家真实机构（法人/单位），如「中建科工集团有限公司」「深圳市坪山区建筑工务署」\n"
    "  - 泛称：是角色/类别描述，不指向特定机构，如「配合设计院」「协调物业公司」「施工单位」\n"
    "  - 人名开头：名字以一个**人名**开头、后面才是单位名"
    "（如「骆永辉湖北妙润启力建设工程有限公司」= 人名 骆永辉 + 单位），"
    "常见于参会名单里人名列表与下一家公司名之间没有分隔符的排版\n"
    "  - 不确定：证据不足无法判断\n"
    "对每条输出 JSON 对象：\n"
    '  "id": 原样带回编号, "判定": "具体名"/"泛称"/"人名开头"/"不确定", "理由": 不超过 16 字,\n'
    '  "前缀": 仅当判定为"人名开头"时填开头的人名（2~4 字），其余情况填空串\n'
    '只输出 JSON：{"items":[...]}，不要其它文字。'
)


def _load_prev(p):
    """读旧 CSV 里已填的（决定, 决定人），重生成时保留人的活。

    ⚠️ 格式/编码容错见 kg/csvio.py（Excel 会存成 GBK 甚至 xlsx，实测）。
    """
    if not p.is_file():
        return {}
    prev = {}
    try:
        from ..csvio import read_rows
        for r in read_rows(p):
            d = (r.get("决定") or "").strip()
            if d:
                prev[(r.get("实体名") or "").strip()] = d
    except Exception:
        return {}
    return prev


def run_qc(cfg, batch=10):
    """LLM 裁判全部 ORG 实体 → 写 review_qc.csv。返回 summary。"""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from ..config import Config
    from ..extract import llm_provider as L
    from ..resolve import _locate_files

    root = Path(cfg.root)
    graph = json.loads((root / GRAPH).read_text(encoding="utf-8"))
    paths = _locate_files(cfg)
    provider = L.build_provider(cfg)
    if provider.name == "none":
        raise RuntimeError("qc 需要 llm.provider（当前 none）")

    prev = _load_prev(root / QC_CSV)
    ev_of = {}
    for r in graph.get("relationships") or []:
        ev = (r.get("properties") or {}).get("evidence") or []
        for side in (r.get("source"), r.get("target")):
            if side:
                ev_of.setdefault(side, []).extend(ev)
    orgs = sorted(e["name"] for e in graph.get("entities") or [] if e.get("type") == "ORG")

    rows, t0, calls = [], time.time(), 0
    for bi in range(0, len(orgs), batch):
        chunk = orgs[bi:bi + batch]
        user = "\n\n".join("[%d] 名称：%s\n原文片段：%s" % (
            i + 1, name, _ctx(name, ev_of, paths)) for i, name in enumerate(chunk))
        payload = {"model": provider.model, "temperature": 0, "top_p": 1,
                   "max_tokens": 1200,
                   "messages": [{"role": "system", "content": PROMPT},
                                {"role": "user", "content": user}]}
        if getattr(provider, "reasoning_effort", ""):
            payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
        got = {}
        try:
            calls += 1
            data = provider._post(provider.base_url + "/chat/completions", payload,
                                  {"Authorization": "Bearer " + provider.api_key
                                   if provider.api_key else ""})
            obj = L.parse_json_payload(L.message_text((data.get("choices") or [{}])[0].get("message") or {}))
            for it in (obj.get("items") if isinstance(obj, dict) else obj) or []:
                got[str(it.get("id"))] = it
        except Exception as e:                               # noqa: BLE001
            print("批 %d 失败：%s: %s" % (bi + 1, type(e).__name__, e))
        for i, name in enumerate(chunk):
            it = got.get(str(i + 1)) or {}
            d = (it.get("判定") or "").strip()
            prefix = (it.get("前缀") or "").strip()
            if d not in ("具体名", "泛称", "人名开头", "不确定"):
                d, why, prefix = "未判定", "输出非法/批次失败", ""
            else:
                why = (it.get("理由") or "").strip()[:24]
                if d == "人名开头":
                    # 前缀必须是 2~4 字纯中文，且剩余部分是合法单位名 —— 否则降级不确定
                    core = _unit_core(cfg)
                    if not (2 <= len(prefix) <= 4 and prefix == name[:len(prefix)]
                            and re.fullmatch(core, name[len(prefix):])):
                        d, why, prefix = "不确定", "前缀校验未过，降级", ""
            old = prev.get(name, "")
            rows.append({"实体名": name, "模型判定": d, "理由": why,
                         "原文片段": _ctx(name, ev_of, paths)[:160],
                         "剥离前缀": prefix,
                         "决定": old, "决定人": "llm:" + provider.model if old == "" else "人工"})

    p = root / QC_CSV
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    dist = {}
    for r in rows:
        dist[r["模型判定"]] = dist.get(r["模型判定"], 0) + 1
    pending = sum(1 for r in rows if not r["决定"])
    return {"org": len(orgs), "dist": dist, "pending": pending,
            "csv": str(p), "seconds": round(time.time() - t0, 1), "calls": calls}


def _ctx(name, ev_of, paths):
    quotes = []
    for v in ev_of.get(name, [])[:2]:
        f = v.get("file", "")
        # ⭐ 完整绝对路径（用户要求：方便追查；路径截尾巴曾不达标）
        quotes.append("《%s》p%s「%s」" % (paths.get(f, f), v.get("page"),
                                          (v.get("quote") or "")[:50]))
    return " ｜ ".join(quotes) or "（无证据）"


# ---- 沉淀与移除 ----

def _read_yaml(path):
    import yaml
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def settle_qc(rows, config_dir):
    """读回 review_qc.csv 的决定：
    泛称+移除 → qc_removed.yaml；保留 → qc_kept.yaml；
    人名开头+确认 → 前缀进 qc_prefixes.yaml（下次抽取自动剥离人名）。"""
    removed, kept, prefixes = [], [], []
    for r in rows or []:
        d = (r.get("决定") or "").strip()
        name = (r.get("实体名") or r.get("a") or "").strip()
        if not name:
            continue
        if d == "移除":
            removed.append(name)
        elif d == "保留":
            kept.append(name)
        elif d == "确认" and (r.get("模型判定") == "人名开头"
                              or (r.get("剥离前缀") or "").strip()):
            px = (r.get("剥离前缀") or "").strip()
            if px and name.startswith(px):
                prefixes.append(px)
    cdir = Path(config_dir)
    if removed:
        data = _read_yaml(cdir / REMOVED_YAML)
        merged = sorted(set(data.get("removed") or []) | set(removed))
        (cdir / REMOVED_YAML).write_text(
            "# 实体质检：人工确认移除的泛称/碎片实体（由 --resolve --apply 沉淀，下次抽取自动生效）\n"
            "removed:\n" + "".join("- %s\n" % n for n in merged), encoding="utf-8")
    if kept:
        data = _read_yaml(cdir / KEPT_YAML)
        merged = sorted(set(data.get("kept") or []) | set(kept))
        (cdir / KEPT_YAML).write_text(
            "# 实体质检：人工确认保留的具体名（LLM 以后不再提名，如果提名也被这里覆盖）\n"
            "kept:\n" + "".join("- %s\n" % n for n in merged), encoding="utf-8")
    if prefixes:
        data = _read_yaml(cdir / PREFIXES_YAML)
        merged = sorted(set(data.get("prefixes") or []) | set(prefixes))
        (cdir / PREFIXES_YAML).write_text(
            "# 实体质检：人工确认的「人名开头」剥离前缀（下次抽取按 person_prefixes 同机制切开）\n"
            "prefixes:\n" + "".join("- %s\n" % n for n in merged), encoding="utf-8")
    return {"移除": len(removed), "保留": len(kept), "前缀": len(prefixes)}


def load_removed(cfg):
    """抽取组装图时用：config/qc_removed.yaml 里的名字集合（配置驱动移除）。"""
    return set(_read_yaml(Path(cfg.config_dir) / REMOVED_YAML).get("removed") or [])


def filter_graph(graph, cfg):
    """按 qc_removed.yaml 过滤实体与涉及它的边。返回 (graph, n_removed, n_edges_removed)。"""
    removed = load_removed(cfg)
    if not removed:
        return graph, 0, 0
    ents = [e for e in graph.get("entities") or [] if e.get("name") not in removed]
    rels = [r for r in graph.get("relationships") or []
            if r.get("source") not in removed and r.get("target") not in removed]
    n_e = len(graph.get("entities") or []) - len(ents)
    n_r = len(graph.get("relationships") or []) - len(rels)
    return {"entities": ents, "relationships": rels}, n_e, n_r
