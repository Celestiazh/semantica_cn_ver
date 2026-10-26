# -*- coding: utf-8 -*-
"""监理纪要知识图谱 · 命令行入口

用法：
    python run.py                   # 扫描目录（列 PDF / 页数 / 哪几页需要 OCR）
    python run.py --parse           # 分级取文本（跑 OCR + 清洗，最慢的一步，落 var/parse/）
    python run.py --extract         # 两级抽取（秒级）：会议要素 + 正文待办候选，落 var/extract/
    python run.py --pending-out f.json   # 导出待办候选（给 Agent / 模型抽字段）
    python run.py --pending-in f.json    # 回灌 Agent / 模型填好的字段
    python run.py --parse --force   # 忽略缓存重跑
    python run.py --parse --limit 2 # 只跑前 2 份（调试用）
    python run.py <目录>            # 临时覆盖输入目录
    python run.py --check           # 只做配置自检（不读 PDF，秒回）

当前进度（2026-10-12）：① 解析 ✅  ② 抽取 ✅（L1 规则 + L2 模型填充 98.2%）
         ③ 消解 ✅（名册 + 人工闭环 + 沉淀）  ④ 建图 ✅（MATTER 事项层已并入）
         看板 ✅（Semantica 服务端 + Explorer 汉化）；⑤ 汇聚 / ⑥ 输出仍为 stub。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from kg import __version__                                     # noqa: E402
from kg.config import load, ConfigError                        # noqa: E402
from kg.logutil import setup_console, get_logger, write_text    # noqa: E402

log = get_logger()


def show_config(cfg):
    log.info("项目      : %s v%s" % (cfg.get("project.name", "?"), __version__))
    log.info("项目根    : %s" % cfg.root)
    log.info("输入目录  : %s" % cfg.input_dir)
    log.info("工作目录  : %s" % cfg.work_dir)
    log.info("输出目录  : %s" % cfg.output_dir)
    log.info("节点类型  : %s" % (", ".join(cfg.node_types) or "（无）"))
    l1 = [r.get("type") for r in cfg.relation_defs(layer=1)]
    l2 = [r.get("type") for r in cfg.relation_defs(layer=2)]
    log.info("关系 L1   : %d 种 —— %s" % (len(l1), ", ".join(l1) or "（无）"))
    log.info("关系 L2   : %d 种 —— %s（待实现）" % (len(l2), ", ".join(l2) or "（无）"))
    log.info("名册单位  : %d 家（别名 %d 条）" % (len(cfg.units), len(cfg.alias_map()) - len(cfg.units)))
    log.info("负向对    : %d 对（驳回） / %d 对（存疑）"
             % (len(cfg.denied_pairs()), len(cfg.pending_pairs())))
    log.info("模型       : provider=%s" % cfg.get("llm.provider", "none"))
    log.info("图后端     : %s" % cfg.get("graph.backend", "local"))
    log.info("OCR 触发   : 文字层 < %d 字" % cfg.get("parse.text_layer_min_chars", 30))


def do_check(cfg):
    problems, amap = cfg.check()
    if problems:
        log.info("")
        log.info("配置自检 —— 发现 %d 项：" % len(problems))
        for p in problems:
            log.info("  ! %s" % p)
    else:
        log.info("")
        log.info("配置自检 —— 无问题")
    return 0 if not problems else 1


def do_scan(cfg, input_dir):
    from kg.parse import scan_documents, summarize, parse_dir    # noqa: E402
    from kg.logutil import write_report

    log.info("")
    log.info("扫描 %s" % input_dir)
    infos, errors, ignored = scan_documents(
        input_dir,
        text_layer_min_chars=cfg.get("parse.text_layer_min_chars", 30),
    )

    s = summarize(infos, ignored)
    log.info("  发现 %d 个文件（PDF %d / DOCX %d），共 %d 页"
             % (s["n_files"], s["n_pdf"], s["n_docx"], s["n_pages"]))
    log.info("  需 OCR %d 页（%.0f%%） / 读文字层 %d 页 / DOCX 直读 %d 页"
             % (s["n_ocr_pages"], s["ocr_ratio"] * 100, s["n_text_pages"], s["n_docx_pages"]))
    if s["ignored_ext"]:
        log.info("  ! 被忽略的扩展名（不在支持范围）：%s"
                 % ", ".join("%s x%d" % (k, v) for k, v in sorted(s["ignored_ext"].items())))

    for f in s["files"]:
        head = "    %-56s %-4s %2d 页" % (f["file"][:56], f.get("kind", ""), f["n_pages"])
        if f["ocr_pages"]:
            head += "  需 OCR: %s" % f["ocr_pages"]
        log.info(head)

    if errors:
        log.info("")
        log.info("  ! %d 个文件读不开：" % len(errors))
        for name, msg in errors:
            log.info("      %s -> %s" % (name, msg))

    out_json = cfg.work_dir / "parse" / "scan.json"
    write_text(out_json, json.dumps(s, ensure_ascii=False, indent=2))
    log.info("")
    log.info("  已写出：%s" % out_json)

    lines = [
        "# 扫描报告（不抽任何内容，只做体检）",
        "",
        "- 文件数：%d（PDF %d / DOCX %d）" % (s["n_files"], s["n_pdf"], s["n_docx"]),
        "- 总页数：%d" % s["n_pages"],
        "- 需 OCR：%d 页（%.0f%%）" % (s["n_ocr_pages"], s["ocr_ratio"] * 100),
        "- 读文字层：%d 页 · DOCX 直读：%d 页" % (s["n_text_pages"], s["n_docx_pages"]),
        "",
        "> 这一步只读文字层 / docx XML，**没有跑 OCR、没有抽任何关系**。",
        "> 它的作用是决定后面要跑多少次 OCR。",
        "",
        "| 文件 | 类型 | 页数 | 需 OCR 的页 |",
        "| :--- | :--- | ---: | :--- |",
    ]
    for f in s["files"]:
        lines.append("| %s | %s | %d | %s |"
                     % (f["file"], f.get("kind", ""), f["n_pages"], f["ocr_pages"] or "—"))
    if s["ignored_ext"]:
        lines += ["", "## ⚠️ 被忽略的扩展名（不在支持范围）", ""]
        lines += ["- `%s` × %d" % (k, v) for k, v in sorted(s["ignored_ext"].items())]
    if errors:
        lines += ["", "## 读不开的文件", ""]
        lines += ["- %s -> %s" % (n, m) for n, m in errors]
    rep = write_report(cfg.work_dir / "parse" / "scan_report.md", lines)
    log.info("  已写出：%s" % rep)
    return 0


def do_parse(cfg, input_dir, force=False, limit=None):
    """分级取文本：跑 OCR + 清洗，落 var/parse/。这一步最慢，所以逐份打进度。"""
    from kg.parse import parse_dir
    from kg.logutil import write_report

    log.info("")
    log.info("解析 %s" % input_dir)
    log.info("  （无文字层的页要走 OCR，实测约 5.3s/页；已解析过的会走缓存跳过）")
    log.info("")

    def on_file(idx, total, row):
        if row.get("error"):
            log.info("  [%2d/%2d] %-34s !! %s" % (idx, total, row["file"][:34], row["error"]))
            return
        log.info("  [%2d/%2d] %-34s %2d页 文字层%2d/OCR%2d %6d字 %5.1fs%s"
                 % (idx, total, row["file"][:34], row["pages"], row["textlayer"], row["ocr"],
                    row["chars"], row["seconds"], "  (缓存)" if row.get("reused") else ""))

    rows, total = parse_dir(input_dir, cfg, force=force, limit=limit, on_file=on_file)

    log.info("")
    log.info("  解析完成：%d 份（复用缓存 %d 份）| %d 页（文字层 %d / OCR %d）| %s 字"
             % (total["n_parsed"], total["n_reused"], total["pages"],
                total["textlayer"], total["ocr"], format(total["chars"], ",")))
    log.info("  清洗：噪声行 %d | 页眉页脚行 %d" % (total["noise_dropped"], total["header_dropped"]))
    log.info("  耗时 %.1fs（其中 OCR %.1fs）→ %s"
             % (total["seconds"], total["ocr_seconds"], total["work_dir"]))
    if total["failures"]:
        log.info("  ! 失败 %d 份：" % len(total["failures"]))
        for name, msg in total["failures"]:
            log.info("      %s -> %s" % (name, msg))

    lines = [
        "# 解析报告（分级取文本）",
        "",
        "- 文件数：%d（复用缓存 %d）" % (total["n_parsed"], total["n_reused"]),
        "- 总页数：%d（文字层 %d / OCR %d）" % (total["pages"], total["textlayer"], total["ocr"]),
        "- 总字符：%s" % format(total["chars"], ","),
        "- 清洗掉：噪声行 %d · 页眉页脚行 %d" % (total["noise_dropped"], total["header_dropped"]),
        "- 耗时：%.1fs（其中 OCR %.1fs）" % (total["seconds"], total["ocr_seconds"]),
        "",
        "> 产物：`var/parse/<文件名>.text.json`（带页码与来源，供抽取阶段定位原文）",
        "> 与 `var/parse/<文件名>.txt`（可读全文，格式与 POC 产物一致）",
        "",
        "| 文件 | 页 | 文字层 | OCR | 字符 | 噪声行 | 页眉页脚 | 耗时 s |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lines.append("| %s | %d | %d | %d | %s | %d | %d | %.1f |"
                     % (r["file"], r.get("pages", 0), r.get("textlayer", 0), r.get("ocr", 0),
                        format(r.get("chars", 0), ","), r.get("noise_dropped", 0),
                        r.get("header_dropped", 0), r.get("seconds", 0.0)))
    if total["failures"]:
        lines += ["", "## 失败的", ""]
        lines += ["- %s -> %s" % (n, m) for n, m in total["failures"]]
    rep = write_report(cfg.work_dir / "parse" / "parse_report.md", lines)
    log.info("  已写出：%s" % rep)
    return 0 if not total["failures"] else 1


def do_extract(cfg, limit=None):
    """两级抽取：第 1 层会议要素（规则）+ 第 2 层正文事项候选（规则筛，字段待填）。"""
    from kg.extract import extract_dir
    from kg.logutil import write_report

    log.info("")
    log.info("抽取（读 var/parse 的产物）")

    def cb(idx, total, row):
        log.info("  [%2d/%2d] %-30s %2d页 | 单位%2d 人%2d 关系%2d | 候选%3d 条"
                 % (idx, total, (row["file"] or "")[:30], row["pages"],
                    row["units"], row["persons"], row["relations"], row["items"]))

    rows, total = extract_dir(cfg, limit=limit, on_file=cb)

    log.info("")
    log.info("  实体 %d（%s）| 关系 原始 %d -> 合并后 %d（同一条边多出处）"
             % (total["entities"],
                " / ".join("%s %d" % (k, v) for k, v in total["entities_by_type"].items()),
                total["relations_raw"], total["relations_merged"]))
    log.info("  带证据的边 %d 条 | 证据条目 %d 个"
             % (total["edges_with_evidence"], total["evidence_entries"]))
    log.info("  待办候选 %d 条（规则给出责任单位 %d / 期限 %d / 闭环标记 %d）"
             % (total["items"], total["items_with_org"], total["items_with_deadline"],
                total["closed_loop"]))
    log.info("  模型：provider=%s -> 字段来源 = %s" % (total["provider"], total["filled_by"]))
    log.info("  耗时 %.1fs -> %s" % (total["seconds"], total["work_dir"]))
    if total.get("warnings_count"):
        log.info("  ⚠️ 数据质量告警 %d 条（前 10）—— **没找到 ≠ 没有**，要显式告知：" % total["warnings_count"])
        for w in total["warnings"][:10]:
            log.info("      %s" % w)
        if total["warnings_count"] > 10:
            log.info("      …… 其余 %d 条见报告" % (total["warnings_count"] - 10))
    for name, msg in total["failures"]:
        log.info("  ! %s -> %s" % (name, msg))

    lines = [
        "# 抽取报告（两级）",
        "",
        "- 文件数：%d" % total["n_files"],
        "- 实体：%d（%s）" % (total["entities"],
                             " · ".join("%s %d" % (k, v) for k, v in total["entities_by_type"].items())),
        "- 关系：原始 %d 条 -> **合并后 %d 条**（按 source/target/type[/role] 合并，evidence 聚为多出处）"
        % (total["relations_raw"], total["relations_merged"]),
        "- 带证据的边：%d 条；证据条目合计 %d 个" % (total["edges_with_evidence"], total["evidence_entries"]),
        "- 待办候选：%d 条（规则给出责任单位 %d · 期限 %d · 闭环标记 %d）"
        % (total["items"], total["items_with_org"], total["items_with_deadline"], total["closed_loop"]),
        "- 模型：provider = %s -> 字段来源 = %s" % (total["provider"], total["filled_by"]),
        "- 数据质量告警：%d 条" % total.get("warnings_count", 0),
        "- 耗时：%.1fs" % total["seconds"],
        "",
        "> ⚠️ **告警不是错误**：它标的是「这类信息原文里本来就没有」或「文件本身是空壳」，",
        "> 目的是**不让「没找到」看起来像「没有」**。",
        "",
        "> 产物：`var/extract/graph_raw.json`（图，边带 evidence）·",
        "> `var/extract/pending_candidates.json`（待办候选，供模型/Agent 填字段）·",
        "> `var/extract/per_file/<文件名>.json`（逐份明细）",
        "",
        "> ⚠️ 口径：候选条目是**规则筛出来的**，不是结论；责任单位/期限字段是**规则参考值**，",
        "> 实测责任单位只到 51%。说\"抽到了\"必须写清是规则给的还是模型给的。",
        "",
        "| 文件 | 页 | 单位 | 人 | 关系 | 会议 | 项目 | 主持人 | 候选 |",
        "| :--- | ---: | ---: | ---: | ---: | :--- | :--- | :--- | ---: |",
    ]
    for r in rows:
        lines.append("| %s | %d | %d | %d | %d | %s | %s | %s | %d |"
                     % ((r["file"] or "")[:44], r["pages"], r["units"], r["persons"],
                        r["relations"], r["meeting"] or "—", r["project"] or "—",
                        r["host"] or "—", r["items"]))
    rep = write_report(cfg.work_dir / "extract" / "extract_report.md", lines)
    log.info("  已写出：%s" % rep)
    return 0 if not total["failures"] else 1


def do_pending_out(cfg, out_path, limit=None):
    """导出待办候选（给 Agent / 模型抽字段用）。"""
    from kg.serve import tools
    from kg.logutil import write_text
    payload = tools.get_pending_candidates(cfg, limit=limit)
    write_text(out_path, json.dumps(payload, ensure_ascii=False, indent=1))
    log.info("已导出 %d 条待办候选 -> %s" % (payload["count"], out_path))
    log.info("  待抽字段：%s" % " / ".join(payload["fields_expected"]))
    return 0


def do_pending_in(cfg, in_path):
    """回灌 Agent / 模型填好的字段。"""
    from kg.serve import tools
    res = tools.submit_extracted_fields(cfg, in_path, source="agent")
    log.info("回灌完成：接收 %d 条 / 拒收 %d 条 / 剩余未填 %d 条"
             % (res["accepted"], res["rejected"], res["remaining_unfilled"]))
    for r in res["reasons"]:
        log.info("  ! %s" % r)
    log.info("  留痕：%s" % res["trail"])
    return 0


def do_resolve(cfg, apply=False):
    """③ 消解：归一 + 提候选（出人工确认 CSV）；--apply 读回执行并沉淀。

    --apply 时顺带处理实体质检（qc）的决定：review_qc.csv 里人工填了
    「移除/保留」的行 → 沉淀 qc_removed.yaml / qc_kept.yaml。
    """
    import json
    from kg.resolve import run_resolve, RESOLVED
    from kg import qc as qc_mod
    s = run_resolve(cfg, apply=apply)
    log.info("ORG 实体（去重前）：%d ｜ 名册覆盖：%d ｜ 不在名册（消解要处理的）：%d"
             % (s["org_total"], s["roster_covered"],
                s["org_total"] - s["roster_covered"]))
    log.info("第①步自动归并（别名→正名）：%d 个名字" % s["normalized"]["renamed"])
    log.info("疑似同一家候选：%d 对（**只提名不判定**，决定权在人工）" % s["candidates"])
    log.info("人工确认清单：%s  ← 只填「决定 / 保留哪条」两列" % s["csv"])
    log.info("报告：%s" % s["report"])
    if apply:
        st = s.get("settled") or {}
        aft = s.get("after") or {}
        log.info("读回执行：合并 %d ｜ 驳回 %d ｜ 存疑 %d ｜ 未填 %d"
                 % (st.get("合并", 0), st.get("驳回", 0), st.get("存疑", 0), st.get("未填", 0)))
        log.info("沉淀：config/roster_extra.yaml · config/denied_extra.yaml（下一轮自动生效）")
        log.info("归并后的图：%s（ORG %d / 实体 %d / 边 %d）"
                 % (aft.get("graph"), aft.get("org"), aft.get("entities"),
                    aft.get("relationships")))
        # 实体质检（qc）：有决定就沉淀；重抽取组装图时按 qc_removed.yaml 自动移除
        qc_path = cfg.root / qc_mod.QC_CSV
        if qc_path.is_file():
            rows, _stat = _read_qc_rows(qc_path)
            if rows:
                settled_qc = qc_mod.settle_qc(rows, cfg.config_dir)
                if settled_qc["移除"] or settled_qc["保留"] or settled_qc["前缀"]:
                    log.info("实体质检沉淀：移除 %d ｜ 保留 %d ｜ 人名前缀 %d"
                             "（qc_removed.yaml · qc_kept.yaml · qc_prefixes.yaml）"
                             % (settled_qc["移除"], settled_qc["保留"], settled_qc["前缀"]))
                    log.info("→ 下次 `run.py --extract` 组装图时自动移除/剥离；再跑 --resolve --apply 出干净图")
    else:
        log.info("下一步：人工填完 CSV 后执行 `run.py --resolve --apply`")
    return 0


def _read_qc_rows(path):
    """读 review_qc.csv 里已填「决定」的行（格式/编码容错见 kg/csvio.py：GBK/xlsx 都能读）。"""
    from kg.csvio import read_rows
    rows = [r for r in read_rows(path) if (r.get("决定") or "").strip()]
    return rows, len(rows)


def do_qc(cfg):
    """②′ 实体质检：LLM 裁判 ORG 实体名（具体名/泛称/不确定）→ 人工确认清单。

    ⛔ 只提名不删 —— 泛称实体要等人工在 review_qc.csv 填「移除」并跑
    `--resolve --apply` 沉淀后，下次抽取才会真正从图里消失。
    """
    from kg.qc import run_qc as _run
    s = _run(cfg)
    log.info("实体质检（LLM 裁判）：ORG %d 个 ｜ %d 次请求 / %.1f 秒"
             % (s["org"], s["calls"], s["seconds"]))
    log.info("判定分布：%s" % s["dist"])
    log.info("人工确认清单：%s  ← 只给「泛称」填「移除」，具体名不用管" % s["csv"])
    log.info("待人工决定：%d 行（已填过的行会保留，不会重复问）" % s["pending"])
    log.info("流程：填完 → `run.py --resolve --apply` 沉淀 → `run.py --extract` 自动移除")
    return 0


def do_consolidate(cfg):
    from kg.resolve.projects import consolidate_projects, write_candidates
    gpath = ROOT / "var" / "extract" / "graph_resolved.json"
    g = json.loads(gpath.read_text(encoding="utf-8"))
    g, stat = consolidate_projects(g, ROOT)
    cpath = write_candidates(g, ROOT)
    gpath.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("PROJECT 归一：合并组 %d ｜ 变体节点 %d ｜ 剩余项目 %d 个"
             % (stat["merged_groups"], stat["merged_nodes"], stat["projects"]))
    if cpath:
        log.info("相似项目候选（人工确认）：" + str(cpath))


def do_matter(cfg):
    """L2 事项层并入图：已填充字段的候选 → MATTER 节点 + 责任单位/项目边。"""
    from kg.matter import build_matter
    s = build_matter(cfg)
    log.info("MATTER 并图：新增事项节点 %d ｜ 事项责任单位边 %d ｜ 属于项目边 %d"
             % (s["matter"], s["edge_org"], s["edge_proj"]))
    log.info("跳过纯角色词责任单位 %d 条（等角色→单位映射，宁缺不错）" % s["skipped_role"])
    log.info("过滤非机构名碎片 %d 条（原文保留在 MATTER 节点属性里）" % s.get("skipped_junk", 0))
    if s["new_orgs"]:
        log.info("新建 ORG 节点 %d 个：%s" % (len(s["new_orgs"]), "、".join(s["new_orgs"][:8])))
    log.info("图已更新：%s（重导 Semantica 或双击 启动看板.bat 生效）" % s["graph"])
    log.info("幂等：重跑 --matter 只做增量，不会重复建节点")
    return 0


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser(description="监理纪要知识图谱")
    ap.add_argument("input", nargs="?", help="输入目录（覆盖 config 里的 input_dir）")
    ap.add_argument("--check", action="store_true", help="只做配置自检")
    ap.add_argument("--no-scan", action="store_true", help="只打印配置，不扫描目录")
    ap.add_argument("--parse", action="store_true", help="分级取文本（跑 OCR + 清洗，最慢的一步）")
    ap.add_argument("--extract", action="store_true", help="两级抽取：会议要素 + 正文待办候选")
    ap.add_argument("--force", action="store_true", help="忽略缓存，强制重新解析")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 份（调试用）")
    ap.add_argument("--pending-out", metavar="FILE", help="导出待办候选（给 Agent / 模型抽字段）")
    ap.add_argument("--pending-in", metavar="FILE", help="回灌 Agent / 模型填好的字段")
    ap.add_argument("--resolve", action="store_true",
                    help="消解：归一 + 提候选 → 出人工确认 CSV（--apply 读回执行）")
    ap.add_argument("--apply", action="store_true",
                    help="（配合 --resolve）读回人工确认 CSV，沉淀配置并产出归并后的图")
    ap.add_argument("--qc", action="store_true",
                    help="实体质检：LLM 裁判 ORG 名（具体名/泛称）→ 人工确认清单 review_qc.csv")
    ap.add_argument("--consolidate", action="store_true",
                    help="PROJECT 归一（剥离日期前缀合并变体）——必须在 --matter 之后跑")
    ap.add_argument("--matter", action="store_true",
                    help="L2 事项层并入图：已填充字段的候选 → MATTER 节点 + 责任单位/项目边")
    args = ap.parse_args(argv)

    try:
        cfg = load(ROOT)
    except ConfigError as e:
        log.error("配置装载失败：%s" % e)
        return 2

    log.info("=" * 68)
    show_config(cfg)
    log.info("=" * 68)

    if args.check:
        return do_check(cfg)

    do_check(cfg)

    input_dir = Path(args.input) if args.input else cfg.input_dir

    if args.pending_out:
        return do_pending_out(cfg, args.pending_out, limit=args.limit)

    if args.pending_in:
        return do_pending_in(cfg, args.pending_in)

    if args.resolve:
        return do_resolve(cfg, apply=args.apply)

    if args.qc:
        return do_qc(cfg)

    if args.matter:
        return do_matter(cfg)

    if args.consolidate:
        return do_consolidate(cfg)

    if args.parse:
        if not input_dir.is_dir():
            log.error("输入目录不存在：%s" % input_dir)
            return 2
        return do_parse(cfg, input_dir, force=args.force, limit=args.limit)

    if args.extract:
        return do_extract(cfg, limit=args.limit)

    if args.no_scan:
        return 0

    if not input_dir.is_dir():
        log.error("输入目录不存在：%s" % input_dir)
        return 2
    return do_scan(cfg, input_dir)


if __name__ == "__main__":
    sys.exit(main())
