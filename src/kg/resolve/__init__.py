# -*- coding: utf-8 -*-
"""③ 消解（零模型依赖，从头到尾都是程序）—— **已实现（2026-09-28）**

   ① 规则归一   命中 roster.aliases 的直接归并，**根本不进人工清单**
   ② 提候选     相似度算法（difflib + 包含关系，标准库零新依赖）；⛔ 没有大模型
                —— 它的自动去重实测误报 40%，只提名不判定
   ③ 人工确认   `var/resolve/review.csv` → 人填「决定 / 保留哪条」两列
   ④ 回灌       `--resolve --apply` 读回 CSV 执行合并 → 归并后的图
   ⑤ 沉淀       合并 → `config/roster_extra.yaml`；驳回 → `config/denied_extra.yaml`
                → 下一轮第①步自动归并、第②步直接过滤，**人的活不再重复**

   健康指标：**每轮人工清单长度应递减**。不降 = 沉淀没接上。

⛔ 与原设计的两处说明：
   1. 沉淀写 **extra 文件**（roster_extra / denied_extra），不回写人工维护的
      roster.yaml / denied_pairs.yaml —— pyyaml 回写会把人工注释全毁。
   2. 用户已拍板（2026-09-28）：消解**不加**大模型提名 —— 字形完全不同的简称
      （中建八局/江华公司）由人工给一次全称写进名册。

用法：
    python run.py --resolve            # ①②③：归一 + 提候选 → 出人工确认 CSV
    python run.py --resolve --apply    # ④⑤：读回 CSV → 沉淀 → 产出归并后的图
"""
from pathlib import Path
import time

STATUS = "implemented"

GRAPH = Path("var") / "extract" / "graph_raw.json"
RESOLVED = Path("var") / "extract" / "graph_resolved.json"
CSV_PATH = Path("var") / "resolve" / "review.csv"
REPORT = Path("var") / "resolve" / "gap_report.md"


def _build_evidence_index(graph):
    """名字 → 证据列表（{file, page, quote}）。

    证据挂在**关系边**上（实体本身没有）→ 一条边的两端各记一份出处。
    这就是 CSV「A的出处 / B的出处」两列的数据源：让不熟悉语料的人
    也能从 CSV 直接跳回"这个名字在哪份文件、原文长什么样"再做决定。
    """
    idx = {}
    for rel in graph.get("relationships") or []:
        ev = (rel.get("properties") or {}).get("evidence") or []
        if not ev:
            continue
        for side in (rel.get("source"), rel.get("target")):
            if side:
                idx.setdefault(side, []).extend(ev)
    return idx


def _locate_files(cfg):
    """basename → 绝对路径。

    图里的证据只存**文件名**；人要打开原文就必须还原完整路径（2026-09-29 用户实测：
    只给文件名，在 会议纪要154 里搜不到 —— 那批文件在旧目录 监理会议纪要原文）。
    扫描 `input_dir` + `resolve.corpus_search_dirs`（向下 2 层），同名字先见优先。
    """
    import os
    roots = [Path(cfg.input_dir)]
    for d in (cfg.get("resolve.corpus_search_dirs") or []):
        p = Path(d)
        roots.append(p if p.is_absolute() else cfg.root / p)
    seen = {}
    for r in roots:
        if not r.is_dir():
            continue
        for dirpath, dirs, files in os.walk(r):
            depth = len(Path(dirpath).relative_to(r).parts)
            if depth >= 2:
                dirs[:] = []                  # 只向下 2 层，够覆盖分批目录
            for f in files:
                seen.setdefault(f, str(Path(dirpath) / f))
    return seen


def _harvest_user_decisions(root, cfg):
    """apply 前收割人工决定（先读后写）：确认清单若有人工填写，先沉淀进配置。

    幂等：settle/settle_alias 内部去重。返回人读摘要。"""
    from . import apply as apply_mod
    from . import review as review_mod
    from .apply import _load
    from .abbrev import settle_alias
    notes = []
    # ① review.csv（疑似同一实体候选对）
    csv_path = root / CSV_PATH
    if csv_path.is_file():
        try:
            rows, _ = review_mod.read_review_csv(csv_path)
            filled = [r for r in rows if (r.get("决定") or "").strip()]
            if filled:
                stat = apply_mod.settle(rows, cfg.config_dir)
                notes.append("review.csv 收割 %d 行(合并%s/驳回%s)" % (
                    len(filled), stat.get("合并", 0), stat.get("驳回", 0)))
        except Exception as e:                            # noqa: BLE001
            notes.append("review.csv 收割失败: %s" % e)
    # ② review_alias.xlsx/csv（简称→全称）
    alias_csv = root / "var" / "resolve" / "review_alias.csv"
    try:
        from .abbrev import read_review_alias_rows
        arows = read_review_alias_rows(alias_csv)
        if arows:
            filled = [r for r in arows if (r.get("决定") or "").strip()]
            if filled:
                stat = settle_alias(filled, cfg.config_dir)
                notes.append("review_alias 收割 %d 行(合并%s/其他%s)" % (
                    len(filled), stat.get("合并", 0), stat.get("其他", 0)))
    except Exception as e:                                # noqa: BLE001
        notes.append("review_alias 收割失败: %s" % e)
    return "; ".join(notes)


def run_resolve(cfg, apply=False, threshold=0.72):
    from .normalize import (build_alias_map, load_denied, load_roster,
                            normalize_graph)
    from . import candidates as cand_mod
    from . import review as review_mod
    from . import apply as apply_mod

    root = Path(cfg.root)
    graph_path = root / GRAPH
    if not graph_path.is_file():
        raise FileNotFoundError("没有抽取产物：先跑 `run.py --extract`")
    import json
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    # ⓪ 先读后写（防覆盖，2026-10-19 事故修复）：apply 时先把人工已填的确认清单
    #    收割进配置，再走提名/重生成——否则重生成的空清单会覆盖人工输入。
    #    收割幂等（settle*/abbrev_decided 去重），且收割后提名自动排除已决行。
    harvest_note = ""
    if apply:
        try:
            harvest_note = _harvest_user_decisions(root, cfg)
            if harvest_note:
                print("INFO 先读后写收割：" + harvest_note, flush=True)
        except Exception as e:                            # noqa: BLE001 收割失败不阻塞，但必须大声报
            import sys as _s
            print("⛔ 人工决定收割失败（继续前请人工核对清单未被覆盖）: %s: %s"
                  % (type(e).__name__, e), file=_s.stderr)

    roster = load_roster(cfg.config_dir)
    alias_map = build_alias_map(roster)
    denied = load_denied(cfg.config_dir)
    names = [e["name"] for e in graph.get("entities") or [] if e.get("type") == "ORG"]

    # ① 归一（先做一遍，看名册当前覆盖多少）
    resolved, rename, nstats = normalize_graph(graph, alias_map)

    # ② 提候选（对**归一后仍存在**的名字，避免把已归并的对再弹给人工）
    names2 = [e["name"] for e in resolved.get("entities") or [] if e.get("type") == "ORG"]
    ev_index = _build_evidence_index(graph)
    cands = cand_mod.propose_candidates(names2, alias_map, denied, threshold=threshold,
                                        ev_index=ev_index)

    # ③ 出 CSV + 报告（出处给完整路径 —— 人要能打开原文再决定）
    file_paths = _locate_files(cfg)
    csv_path = root / CSV_PATH
    review_mod.write_review_csv(csv_path, cands, alias_map, file_paths=file_paths)
    ev_files = {e.get("file") for v in ev_index.values() for e in v if e.get("file")}
    located = sum(1 for f in ev_files if f in file_paths)
    report = _write_report(root, roster, alias_map, names, rename, nstats, cands,
                           threshold, input_dir=str(getattr(cfg, "input_dir", "")),
                           located=(located, len(ev_files)))

    summary = {"org_total": len(names), "roster_covered": len(names) - len(set(names) - set(alias_map)),
               "normalized": nstats, "candidates": len(cands),
               "csv": str(csv_path), "report": str(root / REPORT)}

    # ③′ 简称→全称提名（--resolve 常规步骤）：规则通道零模型；provider 非 none 时加模型通道
    try:
        from .abbrev import propose_abbrev, write_review_alias_csv
        prov = None
        try:
            from ..extract import llm_provider as L
            _p = L.build_provider(cfg)
            prov = _p if _p.name != "none" else None
        except Exception:                                # noqa: BLE001
            prov = None
        # 已决定过的简称（abbrev_decided_extra.yaml）不再重复提名
        decided = {}
        from .apply import _load
        _dec = _load(cfg.config_dir / "abbrev_decided_extra.yaml") or {}
        for d in _dec.get("abbrevs") or []:
            if d.get("简称"):
                decided[d["简称"]] = d.get("决定", "")
        # ⛔ 提名用【最终图 graph_resolved】（含 --matter 建的简称 ORG），
        #    而不是消解输入的 graph_raw——原始图里根本没有这些节点（2026-10-14 实测）
        abbrev_graph = graph
        _rp = root / RESOLVED
        if _rp.is_file():
            try:
                abbrev_graph = json.loads(_rp.read_text(encoding="utf-8"))
            except Exception:                            # noqa: BLE001
                pass
        ev_index_ab = _build_evidence_index(abbrev_graph)
        arows = propose_abbrev(abbrev_graph, alias_map, prov, root,
                               ev_index=ev_index_ab, decided=decided)
        alias_csv = root / "var" / "resolve" / "review_alias.csv"
        write_review_alias_csv(alias_csv, arows)
        summary["abbrev_candidates"] = len(arows)
        summary["alias_csv"] = str(alias_csv)
    except Exception as e:                               # noqa: BLE001
        import traceback
        import sys as _s
        print("简称提名失败: %s: %s" % (type(e).__name__, e), file=_s.stderr)
        print(traceback.format_exc()[-600:], file=_s.stderr)
        summary["abbrev_candidates"] = "失败: %s" % e

    # ④⑤ apply
    if apply:
        rows, stat = review_mod.read_review_csv(csv_path)
        settled = apply_mod.settle(rows, cfg.config_dir)
        # 简称别名沉淀（先读后写：收割已在流程顶部完成，此处幂等兜底）
        alias_stat = {}
        alias_csv_p = root / "var" / "resolve" / "review_alias.csv"
        if alias_csv_p.with_suffix(".xlsx").is_file() or alias_csv_p.is_file():
            from .abbrev import read_review_alias_rows, settle_alias
            arows = read_review_alias_rows(alias_csv_p) or []
            alias_stat = settle_alias(arows, cfg.config_dir)
        # 沉淀后重新归一并产出归并后的图
        roster2 = load_roster(cfg.config_dir)
        alias2 = build_alias_map(roster2)
        resolved2, rename2, nstats2 = normalize_graph(graph, alias2)
        # ⛔ PROJECT 归一不在这里做！必须在 --matter 之后跑 run.py --consolidate
        #    （否则 matter 按旧变体名找项目节点会落空，383 个事项失去项目归属——2026-10-13 实测）
        (root / RESOLVED).parent.mkdir(parents=True, exist_ok=True)
        (root / RESOLVED).write_text(json.dumps(resolved2, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        summary.update({"alias_settled": alias_stat, "settled": settled, "after": {
            "org": len([e for e in resolved2["entities"] if e["type"] == "ORG"]),
            "entities": len(resolved2["entities"]),
            "relationships": len(resolved2["relationships"]),
            "renamed": nstats2.get("renamed", 0),
            "graph": str(root / RESOLVED)}})
    return summary


def _write_report(root, roster, alias_map, names, rename, nstats, cands, threshold,
                  input_dir="", located=(0, 0)):
    from .candidates import uncovered_names
    uncovered = uncovered_names(names, alias_map)
    lines = ["# 消解报告（%s）" % time.strftime("%Y-%m-%d %H:%M"), "",
             "- **语料目录**：`%s`" % input_dir,
             "  （CSV 出处列里是**完整绝对路径**，可直接复制到资源管理器地址栏打开）",
             "- 出处文件定位：%d / %d 已找到完整路径" % located,
             "  （未定位的会在 CSV 里只显示文件名 —— 把它的目录加进 settings.yaml"
             " 的 `resolve.corpus_search_dirs` 再跑一次）",
             "- ORG 实体：%d（去重前）" % len(names),
             "- 名册命中（第①步自动归并）：%d" % len(rename),
             "- 不在名册：%d ← **消解真正要处理的**" % len(uncovered),
             "- 疑似同一家候选（相似度 ≥ %.2f，**只提名不判定**）：%d 对" % (threshold, len(cands)),
             "- 人工确认清单：`var/resolve/review.csv`（只填「决定 / 保留哪条」两列）",
             "", "## 不在名册的名字", ""]
    lines += ["- %s" % n for n in uncovered]
    lines += ["", "## 疑似对（前 40）", "", "| A | B | 相似度 | 依据 |", "| :--- | :--- | ---: | :--- |"]
    for c in cands[:40]:
        lines.append("| %s | %s | %.3f | %s |" % (c["a"], c["b"], c["score"], c["basis"]))
    p = root / REPORT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)
