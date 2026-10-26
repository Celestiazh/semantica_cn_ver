# -*- coding: utf-8 -*-
"""冒烟测试 —— 直接跑，不需要 pytest：

    .venv\\Scripts\\python.exe tests\\smoke.py

覆盖：配置装载与自检 / 别名表 / 负向表 / 质检门（自己造一份两页 PDF 验证"有文字层 vs 无文字层"）
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.logutil import setup_console                       # noqa: E402
from kg.config import load, ConfigError                    # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print("  %-5s %s%s" % ("[OK]" if cond else "[NG]", name, ("  <- " + str(extra)) if extra else ""))
    if not cond:
        FAILED.append(name)


def test_config():
    print("== 1) 配置 ==")
    cfg = load(ROOT)
    problems, amap = cfg.check()
    check("配置自检无问题", not problems, problems[:3])
    check("节点类型 >= 3", len(cfg.node_types) >= 3, cfg.node_types)
    l1 = cfg.relation_defs(layer=1)
    check("第 1 层关系 >= 5", len(l1) >= 5, len(l1))
    check("名册有单位", len(cfg.units) >= 10, len(cfg.units))

    print("== 2) 别名表（消解第 1 步靠它）==")
    check("别名 -> 全称", amap.get("中建科工集团") == "中建科工集团有限公司", amap.get("中建科工集团"))
    check("全称 -> 自身", amap.get("中建科工集团有限公司") == "中建科工集团有限公司")
    check("自举别名可解析", amap.get("拉普拉斯") == "拉普拉斯新能源科技股份有限公司")

    print("== 3) 负向表 ==")
    d = cfg.denied_pairs()
    pair = frozenset(("中建科工集团有限公司", "中建科工发展有限公司"))
    check("驳回对已登记", pair in d, sorted(map(sorted, [list(x) for x in d]))[:2])
    check("存疑队列已登记", len(cfg.pending_pairs()) >= 1)

    print("== 4) 被误合并的两个法人仍是两条 ==")
    names = [u.get("name") for u in cfg.units]
    check("中建科工集团有限公司 在册", "中建科工集团有限公司" in names)
    check("中建科工发展有限公司 在册", "中建科工发展有限公司" in names)


def test_scanner():
    print("== 5) 质检门（自造两页 PDF：p1 有文字层 / p2 空白）==")
    import pymupdf
    from kg.parse import scan_pdfs, summarize

    tmp = Path(tempfile.mkdtemp(prefix="kg_smoke_"))
    pdf = tmp / "smoke_两页.pdf"
    sample = "监理例会纪要 第46期 会议时间 2026年1月27日 主持人：曾凡新 记录人：李慧 参加人员：黄鹏、张成"
    doc = pymupdf.open()
    p1 = doc.new_page()
    try:
        p1.insert_text((72, 100), sample, fontsize=12, fontname="china-s")
    except Exception:
        # 内置 CJK 字体不可用时退回 ASCII —— 这一步只验"字数是否过阈值"，不验字形
        p1.insert_text((72, 100), "Monitor weekly meeting minutes No.46, host: Zeng Fanxin, "
                                  "recorder: Li Hui, attendees: Huang Peng, Zhang Cheng",
                       fontsize=12)
    doc.new_page()                              # 第二页不放任何文字
    doc.save(str(pdf))
    doc.close()

    infos, errors = scan_pdfs(tmp, text_layer_min_chars=30)
    check("无读不开的文件", not errors, errors)
    check("发现 1 个 PDF", len(infos) == 1, len(infos))
    if infos:
        i = infos[0]
        check("共 2 页", i.n_pages == 2, i.n_pages)
        check("p1 文字层字数过阈值", i.pages[0].chars >= 30, i.pages[0].chars)
        check("p1 判为有文字层", i.pages[0].source == "textlayer",
              (i.pages[0].source, i.pages[0].chars))
        check("p2 判为需 OCR", i.pages[1].source == "ocr",
              (i.pages[1].source, i.pages[1].chars))
        s = summarize(infos)
        check("汇总页数正确", s["n_pages"] == 2 and s["n_ocr_pages"] == 1, s)

    for f in tmp.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass


def test_clean():
    """清洗链的纯函数单测 —— 不依赖 PDF / OCR，所以能精确喂人造数据。"""
    print("== 6) 清洗：噪声行 / 跨页重复行 / 行重建 / 段落 ==")
    from kg.parse import clean

    drop = clean.compile_drop([r"(?i)aspose|evaluation only", r"今日水印", r"^[A-Z0-9]{8,}$"])

    rows = [
        {"text": "第 1 行正文", "bbox": [10, 100, 200, 120]},
        {"text": "Evaluation only. Created with Aspose", "bbox": [10, 130, 400, 150]},
        {"text": "ABCDEFGH1234", "bbox": [10, 160, 200, 180]},
        {"text": "第 2 行正文", "bbox": [10, 190, 200, 210]},
    ]
    kept, dropped = clean.drop_noise(rows, drop)
    check("噪声行被剔除 2 行", len(dropped) == 2, dropped)
    check("正文行保留 2 行", len(kept) == 2, [r["text"] for r in kept])

    # 跨页重复行：同一行在 3/4 页出现 → 判为页眉
    pages = [
        [{"text": "监理例会纪要", "bbox": [0, 0, 100, 20]}, {"text": "各页不同的正文 AAAA", "bbox": [0, 100, 100, 120]}],
        [{"text": "监理例会纪要", "bbox": [0, 0, 100, 20]}, {"text": "各页不同的正文 BBBB", "bbox": [0, 100, 100, 120]}],
        [{"text": "监理例会纪要", "bbox": [0, 0, 100, 20]}, {"text": "各页不同的正文 CCCC", "bbox": [0, 100, 100, 120]}],
        [{"text": "别的抬头 DDDD", "bbox": [0, 0, 100, 20]}, {"text": "各页不同的正文 EEEE", "bbox": [0, 100, 100, 120]}],
    ]
    keys = clean.repeated_keys(pages, ratio=0.6)
    check("重复行被识别为页眉", clean.norm_key("监理例会纪要") in keys, keys)
    kept_pages, hdr = clean.drop_repeated(pages, keys)
    check("页眉在 3 页上被剔除", len(hdr) == 3, len(hdr))
    # 第 4 页的抬头"别的抬头 DDDD"不是重复行，理应保留 → 只有它那页是 2 行
    check("正文没被误删（第4页保留自身抬头）",
          [len(p) for p in kept_pages] == [1, 1, 1, 2], [len(p) for p in kept_pages])

    # 行重建：同一视觉行被切成两框 → 要按 x 拼成一整行
    frag = [
        {"text": "会议时间：", "bbox": [10, 100, 90, 120]},
        {"text": "2026年1月27日", "bbox": [92, 101, 200, 121]},
        {"text": "第二行", "bbox": [10, 140, 100, 160]},
    ]
    grouped = clean.group_rows(frag)
    check("同视觉行被拼合", len(grouped) == 2 and grouped[0]["text"] == "会议时间：2026年1月27日",
          [g["text"] for g in grouped])

    # 段落：正常行距里夹一个明显更大的间距 → 应在那里换段
    # ⚠️ 必须给足行数：阈值 = max(gap_min, 行距中位数*1.9)，
    #    行太少时中位数被大间距抬高 → 判不出换段（这是该公式的已知局限，真实页有几十行不受影响）
    para_rows = [{"text": "第一段第%d行" % i, "y": 10 + 22 * (i - 1), "h": 20} for i in range(1, 5)]
    para_rows += [{"text": "第二段第%d行" % i, "y": 10 + 22 * (i - 1) + 200, "h": 20} for i in range(1, 5)]
    paras = clean.group_paragraphs(para_rows)
    check("按间距分成 2 段", len(paras) == 2, [p[:20] for p in paras])
    check("段内已拼接", paras[0] == "第一段第1行第一段第2行第一段第3行第一段第4行", paras[:1])

    # 端到端：build_pages
    raw = [{"page": 1, "source": "ocr", "rows": [
        {"text": "抬头", "bbox": [0, 0, 50, 20]},
        {"text": "今日水印 相机", "bbox": [0, 30, 200, 50]},
        {"text": "正文一", "bbox": [0, 100, 80, 120]},
    ]}]
    pgs, st = clean.build_pages(raw, ["今日水印"])
    check("build_pages 剔噪声", st["noise_dropped"] == 1, st)
    check("build_pages 保留页码与来源",
          pgs[0]["page"] == 1 and pgs[0]["source"] == "ocr", pgs[0].get("page"))
    check("chars 与段落一致",
          pgs[0]["chars"] == sum(len(x) for x in pgs[0]["paras"]), pgs[0]["chars"])


SYNTH_DOC = {
    "file": "合成样本_监理例会.pdf",
    "pages": [
        {"page": 1, "source": "ocr", "chars": 0, "paras": [
            "监理例会会议纪要【2026】第3期",
            "会议时间：2026年3月4日",
            "会议地点：项目部会议室",
            "会议主持人：张驰",
            "参会人员：甲建设集团有限公司：张三，李四；乙监理有限责任公司：王五",
        ]},
        {"page": 2, "source": "ocr", "chars": 0, "paras": [
            "一、施工单位汇报",
            "1、要求施工单位于3月10日前完成整改。",
            "2、已完成 100% 的形象进度。",
        ]},
    ],
}


def test_extract():
    """抽取：证据页码 / 会议要素 / 条目切分 / 等长替换 —— 全用人造文本，不依赖真实文件。"""
    print("== 7) 抽取：证据页码 / 会议要素 ==")
    from kg.extract import DocText, merge_relationships
    from kg.extract import meeting as meeting_mod
    from kg.extract import pending as pending_mod
    from kg.config import load

    cfg = load(ROOT)
    ecfg = cfg.settings.get("extract") or {}

    dt = DocText(SYNTH_DOC)
    check("DocText 全文非空", len(dt.text) > 60, len(dt.text))
    pos_p2 = dt.text.find("要求施工单位")
    check("第 2 页内容映射到页 2", dt.page_of(pos_p2) == 2, dt.page_of(pos_p2))
    pos_p1 = dt.text.find("张三")
    check("第 1 页内容映射到页 1", dt.page_of(pos_p1) == 1, dt.page_of(pos_p1))
    ev = dt.evidence(pos_p1, pos_p1 + 2)
    check("evidence 三要素齐全",
          bool(ev.get("file")) and ev.get("page") == 1 and bool(ev.get("quote")), ev)

    entities, rels, info, st = meeting_mod.extract_meeting(dt, ecfg)
    check("主持人抽对", info["主持人"] == "张驰", info["主持人"])
    check("会议时间抽对", info["会议时间"] == "2026年3月4日", info["会议时间"])
    check("期号抽对", info["期号"] == "2026年第3期", info["期号"])
    check("会议名归一", info["会议"] == "监理周例会", info["会议"])
    check("抽到 2 家单位", st["units"] == 2, st["units"])
    check("抽到 4 个人（含主持人）", st["persons"] == 4, st["persons"])
    check("参会关系 3 条（不含主持人）",
          len([r for r in rels if r["type"] == "参会"]) == 3,
          [r["source"] for r in rels if r["type"] == "参会"])
    types = {e["id"]: e["type"] for e in entities}
    check("会议有独立节点类型 MEETING", types.get("监理周例会") == "MEETING", types.get("监理周例会"))
    own = [r for r in rels if r["type"] == "所属单位" and r["source"] == "张三"]
    check("所属单位关系存在", len(own) == 1, len(own))
    check("关系带页码证据",
          bool(own) and (own[0]["properties"]["evidence"] or {}).get("page") == 1,
          own[0]["properties"] if own else None)

    print("== 8) 抽取：第 2 层候选（切分 / 触发词 / 页码 / 等长替换）==")
    text = "a" * 5 + "今日水印相机" + "b" * 5
    blanked = pending_mod.blank_noise(text, pending_mod._compiled(["今日水印"]))
    check("清洗为等长替换（偏移不漂）", len(blanked) == len(text), (len(blanked), len(text)))

    cands, st2 = pending_mod.extract_pending(dt, ecfg)
    check("选出 1 条候选（进度句被排除）", st2["items"] == 1,
          [c["quote"][:30] for c in cands])
    if cands:
        c = cands[0]
        check("候选带页码（来自第 2 页）", c["page"] == 2, c["page"])
        check("候选带 evidence", (c.get("evidence") or {}).get("page") == 2, c.get("evidence"))
        check("期限规则抽出", c["rules"]["deadline_raw"] == "3月10日前", c["rules"]["deadline_raw"])
        check("责任单位（角色未映射也给出）", c["rules"]["responsible_org"] == "施工单位",
              c["rules"]["responsible_org"])
        check("候选未被填（等模型/Agent）", c["filled_by"] is None, c["filled_by"])

    print("== 9) 边的合并：同一条边多出处聚合 ==")
    raw = [{"source": "A", "target": "B", "type": "所属单位",
            "properties": {"evidence": {"file": "1.pdf", "page": 1, "quote": "x"}}},
           {"source": "A", "target": "B", "type": "所属单位",
            "properties": {"evidence": {"file": "2.pdf", "page": 3, "quote": "y"}}}]
    merged = merge_relationships(raw)
    check("两条合成一条边", len(merged) == 1, len(merged))
    evs = (merged[0]["properties"] or {}).get("evidence") or []
    check("出处聚成数组（2 个）", isinstance(evs, list) and len(evs) == 2, evs)


def test_rules_uncolon():
    """⭐ 无冒号写法（2026-09-28 新增）：角色词+单位 / 表头字段 / 两列表格 / 行保留 x 坐标。

    造这些测试的直接原因：全量跑完发现 **53 份文件"有内容但 0 单位"**，
    分两类 —— A 类正文首句 `角色词+单位全名`（无冒号）、B 类两列表格（单位|人员）。
    这两个都不是"语义难"，而是**结构化信息没被读出来**，所以必须由自测钉住。
    """
    print("== 10) 无冒号写法：角色词+单位 / 表头字段 / 两列表格 ==")
    from kg.parse.clean import group_rows
    from kg.extract import meeting as meeting_mod
    from kg.extract import table as table_mod
    from kg.extract import DocText
    from kg.config import load

    cfg = load(ROOT)
    ecfg = cfg.settings.get("extract") or {}
    rules = meeting_mod.build_rules(ecfg)

    # --- 10.1 行里必须保留 cells（x 区间）---
    rows = [{"text": "南京城建", "bbox": [209, 10, 251, 22], "score": 1.0},
            {"text": "居松", "bbox": [401, 10, 433, 22], "score": 1.0}]
    g = group_rows(rows)
    check("同一 y 的单元并成一行", len(g) == 1, len(g))
    check("text 仍是拼接结果（不影响已验证的清洗链）", g and g[0]["text"] == "南京城建居松",
          g[0]["text"] if g else None)
    check("cells 保留 x 区间（表格能分列的前提）",
          bool(g) and len(g[0]["cells"]) == 2 and g[0]["cells"][0]["x0"] == 209,
          g[0].get("cells") if g else None)

    # --- 10.2 正文「角色词+单位全名」（无冒号）---
    a_text = ("监理单位召集建设单位广州建鑫嵘赋有限公司、施工总承包单位广东省建筑工程集团有限公司、"
              "设计单位广东省建科建筑设计院有限公司、钢结构单位中建钢构股份有限公司、"
              "电梯安装单位迅达（中国）电梯有限公司在项目现场会议室召开监理例会。")
    got = meeting_mod.scan_role_units(a_text, rules)
    check("A 类抽出 5 家单位", len(got) == 5, [(x["role"], x["unit"]) for x in got])
    names = [x["unit"] for x in got]
    check("不抽到「召集」那一段（切段生效）", "召集" not in names, names)
    check("名称不被截断（续接成全称）",
          "广东省建筑工程集团有限公司" in names, names)
    check("含括号的单位名也对", "迅达（中国）电梯有限公司" in names, names)

    neg = "会议要求施工单位尽快完成整改，并于3月10日前将整改结果报监理单位。由建设单位组织验收。"
    check("反面用例：正文角色词不被当单位", meeting_mod.scan_role_units(neg, rules) == [],
          meeting_mod.scan_role_units(neg, rules))

    # --- 10.3 表头字段（无冒号）---
    b_text = ("监理例会会议纪要编号：100项目名称坪山交警大队营房建设项目会议名称监理工程例会"
              "时间2026年08月21日项目部会议室参加单位名称参加人员"
              "会议纪要内容：由监理单位主持。")
    hf = meeting_mod.scan_header_fields(b_text, rules)
    check("表头抽到项目名", hf.get("项目") == "坪山交警大队营房建设项目", hf)
    check("表头抽到会议名", hf.get("会议") == "监理工程例会", hf)
    check("时间只取日期形状（不吞后面粘着的字）", hf.get("会议时间") == "2026年08月21日", hf)

    # --- 10.4 两列表格结构化直出 ---
    def _c(t, x0, x1):
        return {"t": t, "x0": x0, "x1": x1}

    def _r(text, y, cells):
        return {"text": text, "y": y, "h": 12, "cells": cells}

    page_rows = [
        _r("与会单位与会人员", 100, [_c("与会单位", 209, 251), _c("与会人员", 396, 438)]),
        _r("南京城建居松", 115, [_c("南京城建", 209, 251), _c("居松", 401, 433)]),
        _r("东南监理丁国华", 130, [_c("东南监理", 209, 251), _c("丁国华", 401, 433)]),
        _r("各与会单位及人员中建八局方舟", 145,
           [_c("各与会单位及人员", 95, 131), _c("中建八局", 209, 251), _c("方舟", 401, 433)]),
        _r("签到栏", 160, [_c("签到栏", 95, 131)]),
        _r("注：1、本会议纪要分为…", 175, [_c("注：1、本会议纪要分为…", 86, 508)]),
    ]
    tdoc = DocText({"file": "t.pdf",
                    "pages": [{"page": 1, "source": "textlayer", "chars": 200,
                               "paras": ["与会单位与会人员南京城建居松东南监理丁国华"], "rows": page_rows}]})
    tb = table_mod.extract_paired_table(tdoc, ecfg)
    check("表格抽出 3 组配对", len(tb["pairs"]) == 3, tb["pairs"])
    check("配对内容正确（单位 ↔ 人）",
          ("南京城建", "居松") in tb["pairs"] and ("东南监理", "丁国华") in tb["pairs"],
          tb["pairs"])
    check("左栏竖排标签 / 签到栏不被当成单位",
          all("签到栏" not in o and "与会单位" not in o for o in tb["units"]), tb["units"])
    check("注：之后停止（不越出表格）", len(tb["pairs"]) == 3, len(tb["pairs"]))
    check("角色只能从名称明确看得出时才推（东南监理→监理单位）",
          any(r["unit"] == "东南监理" and r["role"] == "监理单位" for r in tb["roles"]), tb["roles"])

    # 旧产物没有 cells → 必须显式提示"需要重跑解析"，不能静默返回空
    nodoc = DocText({"file": "old.pdf",
                     "pages": [{"page": 1, "source": "textlayer", "chars": 50,
                                "paras": ["与会单位与会人员南京城建居松"],
                                "rows": [{"text": "与会单位与会人员", "y": 100, "h": 12}]}]})
    tb2 = table_mod.extract_paired_table(nodoc, ecfg)
    check("无 cells 时显式告警（不静默空）",
          tb2["pairs"] == [] and any("cells" in w for w in tb2["warnings"]), tb2["warnings"])

    # --- 10.5 docx 也要有 cells（表格单元是 tab 分隔的，用字符位置当伪 x）---
    from kg.parse.docx_reader import _cells_of, PARAMS_FINGERPRINT
    c1 = _cells_of("与会单位\t与会人员")
    check("docx 有 tab 的行拆成 2 个 cell 且左右不重叠",
          len(c1) == 2 and c1[0]["t"] == "与会单位" and c1[0]["x1"] < c1[1]["x0"], c1)
    c2 = _cells_of("一、施工单位汇报本周进展")
    check("docx 无 tab 的段落只有 1 个 cell（正文不会被误当表格）", len(c2) == 1, c2)
    check("docx 有自己的参数指纹（缓存判定不能拿 PDF 指纹去比）",
          "docx" in PARAMS_FINGERPRINT, PARAMS_FINGERPRINT)

    # --- 10.6 参会名单跨行粘连（治本：行级解析，2026-09-28）---
    # 真实案例：段落把"每行一个单位块"无缝拼接 → 上一行末尾人名粘上下行单位名
    #   `…肖辉、景清涛中建科工集团有限公司：…` → ORG「景清涛中建科工集团有限公司」，
    #   还顺带把真正的「中海监理有限公司」弄丢了。
    rows6 = [{"text": "【人员】：深圳市万科发展有限公司：陈潇聪、邓佩华", "y": 100, "h": 12, "cells": []},
             {"text": "中海监理有限公司：向永龙、肖辉、景清涛", "y": 114, "h": 12, "cells": []},
             {"text": "中建科工集团有限公司：蔡博文、张超", "y": 128, "h": 12, "cells": []}]
    glued = "".join(r["text"] for r in rows6)          # 段落 = 行的无缝拼接
    gdoc = DocText({"file": "g.pdf",
                    "pages": [{"page": 1, "source": "textlayer", "chars": len(glued),
                               "paras": [glued], "rows": rows6}]})
    ents6, rels6, info6, st6 = meeting_mod.extract_meeting(gdoc, ecfg)
    names6 = [e["name"] for e in ents6 if e["type"] == "ORG"]
    check("粘连名不再成为实体", "景清涛中建科工集团有限公司" not in names6, names6)
    check("行级解析把三家单位都抽出来",
          {"深圳市万科发展有限公司", "中海监理有限公司", "中建科工集团有限公司"} <= set(names6),
          names6)
    persons6 = [e["name"] for e in ents6 if e["type"] == "PERSON"]
    check("行尾人名归位成人名（景清涛）", "景清涛" in persons6, persons6)
    check("盖章残留名被公共清洗拦截（clean_unit）",
          meeting_mod.clean_unit("项目监理部 (章") == ""
          and meeting_mod.clean_unit("EPC）工程项目监理部 (章") == "",
          [meeting_mod.clean_unit("项目监理部 (章"), meeting_mod.clean_unit("EPC）工程项目监理部 (章")])



    # --- 10.7 单位名合法性防线（junk 长句 / 人名粘连）---
    junk = ["反恐）基地一期项目工程例会在警训基地项目部会议室召开，纪要如下：一、中建科工：（一",
            "1）施工月报每月25日施工单位向总监办及项目公司报送施工月报（电子版和纸质版",
            "项目监理部 (章"]
    ok_units, ok_persons, ok_rels, ok_roles, san = meeting_mod.sanitize_units(
        ["中建科工集团有限公司"] + junk,
        ["景清涛", "蔡博文"],
        [{"source": "蔡博文", "target": "景清涛中建科工集团有限公司", "type": "所属单位",
          "properties": {"evidence": {"file": "x", "page": 1, "quote": "q"}}},
         {"source": "张三", "target": junk[0], "type": "所属单位", "properties": {}}],
        [], rules)
    check("junk 长句 / 盖章残留被丢弃",
          all(j not in ok_units for j in junk), ok_units)
    check("引用 junk 的边一并丢弃", len(ok_rels) == 1, ok_rels)
    check("人名粘连被切开并归位",
          "景清涛中建科工集团有限公司" not in ok_units
          and "中建科工集团有限公司" in ok_units
          and "景清涛" in ok_persons, (ok_units, ok_persons))



def test_resolve():
    """③ 消解：归一 / 提候选 / 沉淀回环（写进临时目录，不碰项目配置）。"""
    print("== 11) 消解：归一 / 提候选 / 沉淀 ==")
    import os
    import tempfile
    import yaml
    from kg.resolve import normalize as N
    from kg.resolve import candidates as C
    from kg.resolve import apply as A

    roster = {"units": [{"name": "中建科工集团有限公司",
                         "aliases": ["中建科工集团"], "type": "施工总包"}]}
    amap = N.build_alias_map(roster)
    check("别名映射", amap.get("中建科工集团") == "中建科工集团有限公司", amap)

    g = {"entities": [{"id": "中建科工集团", "name": "中建科工集团", "type": "ORG"},
                      {"id": "中建科工集团有限公司", "name": "中建科工集团有限公司", "type": "ORG"}],
         "relationships": [{"source": "张三", "target": "中建科工集团", "type": "所属单位",
                            "properties": {"evidence": {"file": "1.pdf", "page": 1, "quote": "q"}}}]}
    g2, rename, st = N.normalize_graph(g, amap)
    check("归一：别名并到正名",
          rename.get("中建科工集团") == "中建科工集团有限公司" and len(g2["entities"]) == 1,
          (rename, st))
    check("归一：边跟着改且证据保留",
          g2["relationships"][0]["target"] == "中建科工集团有限公司"
          and g2["relationships"][0]["properties"]["evidence"]
          == [{"file": "1.pdf", "page": 1, "quote": "q"}],
          g2["relationships"])

    cands = C.propose_candidates(
        ["中建科工集团", "中建科工集团有限公司", "赛迪", "中海监理有限公司"], amap, set())
    check("提候选：包含关系被提名",
          any({"cand_a", "cand_b"} == set()
              or {c["a"], c["b"]} == {"中建科工集团", "中建科工集团有限公司"} for c in cands),
          cands)

    tmp = tempfile.mkdtemp()
    A.settle([{"a": "中海理有限公司", "b": "中海监理有限公司",
               "决定": "合并", "保留": "中海监理有限公司"}], tmp)
    extra = yaml.safe_load(open(os.path.join(tmp, "roster_extra.yaml"), encoding="utf-8"))
    check("沉淀：合并写进 roster_extra（不回写人工 roster）",
          extra["units"][0]["name"] == "中海监理有限公司"
          and "中海理有限公司" in extra["units"][0]["aliases"], extra)
    amap2 = N.build_alias_map(N.load_roster(tmp))
    check("沉淀：下一轮自动归并", amap2.get("中海理有限公司") == "中海监理有限公司", amap2)

    A.settle([{"a": "深圳市博大建设集团", "b": "深圳市大沙河建设投资集团",
               "决定": "驳回", "保留": ""}], tmp)
    denied = N.load_denied(tmp)
    check("沉淀：驳回进负向表",
          ("深圳市博大建设集团", "深圳市大沙河建设投资集团") in denied, denied)
    cands2 = C.propose_candidates(
        ["深圳市博大建设集团", "深圳市大沙河建设投资集团"], amap2, denied)
    check("负向对不再进入候选（人的活不重复）", not cands2, cands2)

    # --- 11.2 CSV 出处列 + 重新生成保留已填决定（2026-09-29 用户指出的缺陷）---
    import csv as _csv
    from kg.resolve import review as R
    csvp = os.path.join(tmp, "review.csv")
    ev_idx = {"中海理有限公司": [{"file": "DEADBEEF-20260821测试纪要.pdf", "page": 2,
                                  "quote": "参会单位：中海理有限公司派人参加"}]}
    cands3 = C.propose_candidates(
        ["中海理有限公司", "中海监理有限公司"], {}, set(), ev_index=ev_idx)
    check("提候选附带双方证据",
          all(c.get("ev_a") is not None and c.get("ev_b") is not None for c in cands3),
          cands3)
    R.write_review_csv(csvp, cands3, {})
    row = list(_csv.DictReader(open(csvp, encoding="utf-8-sig")))[0]
    check("CSV 带 A/B 出处列且文件名完整可搜索",
          "A的出处" in row
          and "《DEADBEEF-20260821测试纪要.pdf》第2页" in row["A的出处"],
          row.get("A的出处"))
    # 文件名 → 完整路径定位表（图里只存文件名，人要打开原文必须给路径）
    R.write_review_csv(csvp, cands3, {},
                       file_paths={"DEADBEEF-20260821测试纪要.pdf": r"X:\语料\DEADBEEF-20260821测试纪要.pdf"})
    row = list(_csv.DictReader(open(csvp, encoding="utf-8-sig")))[0]
    check("出处显示完整绝对路径（能直接打开）",
          "X:" + chr(92) + "语料" + chr(92) + "DEADBEEF-20260821测试纪要.pdf》第2页" in row["A的出处"],
          row.get("A的出处"))
    # 模拟人工已填决定 → 重新生成必须保留
    with open(csvp, "a", encoding="utf-8-sig", newline="") as f:
        _csv.writer(f).writerow(["中海理有限公司", "中海监理有限公司",
                                 0.93, "相似", "", "", "", "合并", "中海监理有限公司"])
    R.write_review_csv(csvp, cands3, {})
    row2 = list(_csv.DictReader(open(csvp, encoding="utf-8-sig")))[0]
    check("重新生成时保留已填决定（人不重填）",
          row2["决定"] == "合并" and row2["保留哪条"] == "中海监理有限公司",
          (row2["决定"], row2["保留哪条"]))


def main():
    setup_console()
    try:
        test_config()
        test_scanner()
        test_clean()
        test_extract()
        test_rules_uncolon()
        test_resolve()
    except ConfigError as e:
        print("配置错误：%s" % e)
        return 2
    print("")
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
