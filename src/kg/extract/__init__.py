# -*- coding: utf-8 -*-
"""② 抽取（两级）+ 与模型/Agent 的接口

## 分层

    第 1 层 · 会议要素（`meeting.py`）—— **纯规则，不碰模型**
        时间 / 地点 / 主持人 / 记录人 / 期号 / 会议名 / 项目 / 参会单位 / 参会人 / 角色→单位
        实测：会议要素段命中 17/18 = 94%；角色→单位映射 7/7

    第 2 层 · 正文事项（`pending.py`）—— **规则筛候选，模型（或 Agent）只填字段**
        规则先出候选（19 份 297 条 / 21,613 字，占全文 44%），模型只在这批候选上抽字段
        ⭐ 价值主要在"带页码的候选 + 逐条可校验"，token 只省 56%（实测，别夸大）

    `llm_provider.py` —— 可插拔 provider（none / ollama / openai-compatible）

## 两条边带证据的约定（本项目红线）

1. 每条边 `properties.evidence = {file, page, quote}`
2. ⭐ **同一条边在多份文件出现时，evidence 聚成数组**（多出处）——
   因为图导入时会按 (source,target,type) 去重（实测 253→93），
   若不先聚合，去重后"这条关系出自哪几份文件"就丢了。
   本模块的 `merge_relationships()` 就是干这个的。

⛔ 铁律：**不让 LLM 改写正文** —— 改写后页码与坐标对不上，溯源链断裂。
"""
import json
import time
from pathlib import Path

from .doctext import DocText
from . import llm_provider
from . import meeting as meeting_mod
from . import pending as pending_mod

__all__ = ["DocText", "extract_dir", "merge_relationships", "meeting_mod", "pending_mod",
           "llm_provider"]


def merge_relationships(rels):
    """按 (source, target, type[, role]) 合并重复边，**evidence 聚成多出处数组**。"""
    merged = {}
    order = []
    for r in rels:
        props = dict(r.get("properties") or {})
        role = props.get("role")
        key = (r.get("source"), r.get("target"), r.get("type"), role)
        if key not in merged:
            out_props = {}
            ev = props.get("evidence")
            if ev:
                out_props["evidence"] = [ev]
            if role:
                out_props["role"] = role
            merged[key] = {"source": r.get("source"), "target": r.get("target"),
                           "type": r.get("type"), "properties": out_props}
            order.append(key)
            continue
        out_props = merged[key]["properties"]
        ev = props.get("evidence")
        if ev:
            lst = out_props.get("evidence")
            if not isinstance(lst, list):
                lst = [lst] if lst else []
            if ev not in lst:
                lst.append(ev)
            out_props["evidence"] = lst
    return [merged[k] for k in order]


def _dedup_entities(ents):
    """按 id 去重（同 id 保留先出现的类型，但 MEETING/PROJECT 优先于 ORG —— 类型别丢）。"""
    rank = {"MEETING": 3, "PROJECT": 3, "ORG": 2, "PERSON": 2}
    out = {}
    for e in ents:
        k = e.get("id")
        if not k:
            continue
        if k not in out or rank.get(e.get("type"), 0) > rank.get(out[k].get("type"), 0):
            out[k] = e
    return list(out.values())


def extract_dir(cfg, limit=None, force=False, on_file=None):
    """对 `var/parse/*.text.json` 跑两级抽取，落 `var/extract/`。返回 (rows, total)。"""
    work = cfg.work_dir / "extract"
    per_file_dir = work / "per_file"
    per_file_dir.mkdir(parents=True, exist_ok=True)

    parse_dir = cfg.work_dir / "parse"
    files = sorted(parse_dir.glob("*.text.json"))
    if limit:
        files = files[:limit]
    if not files:
        raise FileNotFoundError("没有解析产物：先跑 `run.py --parse`（%s）" % parse_dir)

    ecfg = cfg.settings.get("extract") or {}
    # 实体质检沉淀的「人名开头」前缀（qc_prefixes.yaml）并进 meeting 词表 —— 同 person_prefixes 机制
    try:
        from ..qc import load_prefixes
        meeting_cfg = ecfg.setdefault("meeting", {})
        meeting_cfg["person_prefixes"] = load_prefixes(cfg)
    except Exception:
        pass
    do_l1 = (ecfg.get("meeting") or {}).get("enabled", True)
    do_l2 = (ecfg.get("pending") or {}).get("enabled", True)

    rows, all_entities, all_rels, all_cands = [], [], [], []
    file_units = {}                          # 文件名 → 该文件抽到的 ORG 名集合（粘连互证用）
    failures, all_warnings = [], []
    t0 = time.time()

    for idx, f in enumerate(files, 1):
        try:
            dt = DocText.from_file(f)
        except Exception as e:                                   # noqa: BLE001
            failures.append((f.name, "读取失败：%s" % e))
            continue

        ents, rels, info, s1 = ([], [], {}, {})
        if do_l1:
            ents, rels, info, s1 = meeting_mod.extract_meeting(dt, ecfg)
        cands, s2 = ([], {})
        if do_l2:
            cands, s2 = pending_mod.extract_pending(dt, ecfg)

        all_entities += ents
        all_rels += rels
        all_cands += cands
        fu = file_units.setdefault(dt.file or f.name, set())
        fu.update(e.get("name") for e in ents if e.get("type") == "ORG")
        # ⭐ 数据质量告警要**显式往上带**（"没找到 ≠ 没有"）：
        #    例如"本文无参会名单（原文写：详见签到表）"、"内容过少（45 字）可能是空壳"
        warns = list(s1.get("warnings") or [])
        for w in warns:
            all_warnings.append("%s → %s" % ((dt.file or "")[:38], w))
        row = {
            "file": dt.file, "pages": len(dt.doc.get("pages") or []),
            "units": s1.get("units", 0), "persons": s1.get("persons", 0),
            "relations": len(rels), "roles": s1.get("roles", 0),
            "meeting": s1.get("meeting"), "project": s1.get("project"),
            "host": s1.get("host"), "period": s1.get("period"),
            "items": s2.get("items", 0),
            "items_with_org": s2.get("with_org_rule", 0),
            "items_with_deadline": s2.get("with_deadline_rule", 0),
            "warnings": warns,
        }
        rows.append(row)
        per_file_dir.joinpath(f.name).write_text(json.dumps({
            "file": dt.file, "info": info, "stats": {"meeting": s1, "pending": s2},
            "entities": ents, "relationships": rels, "pending": cands,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        if on_file:
            on_file(idx, len(files), row)

    entities = _dedup_entities(all_entities)
    rels_merged = merge_relationships(all_rels)

    # ②′ 粘连剥离（Layer 1，跨文件互证）：「人名+单位」无分隔符黏连的实体，
    #    若剥掉人名后的单位名在其它文件独立出现过 → 自动拆分（确定性规则，
    #    不靠黑名单打地鼠；黑名单 person_prefixes 仍作为无互证时的兜底）。
    graph = {"entities": entities, "relationships": rels_merged}
    if (ecfg.get("glue_split", True)):
        from .glue_split import split_glued
        graph, glue_splits = split_glued(graph, file_units, cfg)
        for p, s, n in glue_splits:
            print("INFO 粘连剥离：「%s」→ 人名[%s] + 单位[%s]（互证文件 %d 个）"
                  % (p + s, p, s, n))
        if glue_splits:
            entities = graph["entities"]
            rels_merged = graph["relationships"]

    # ②′ 实体质检沉淀：config/qc_removed.yaml 里人工确认移除的泛称实体，
    #    在组装图时配置驱动剔除（与消解沉淀同一思路：人的活不再重复）。
    from ..qc import filter_graph
    graph, n_qc_ent, n_qc_rel = filter_graph(
        {"entities": entities, "relationships": rels_merged}, cfg)
    if n_qc_ent:
        print("INFO 实体质检沉淀：移除 %d 个实体 / %d 条边（qc_removed.yaml）" % (n_qc_ent, n_qc_rel))

    (work / "graph_raw.json").write_text(json.dumps(graph, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    provider = llm_provider.build_provider(cfg)
    fields_expected = list((ecfg.get("pending") or {}).get("fields_expected")
                           or pending_mod.EXPECTED_FIELDS)
    cache_path = work / "llm_cache.json"
    candidate_only = bool((ecfg.get("pending") or {}).get("candidate_only", True))

    # ⛔ `filled_by` 必须反映"字段实际由谁填的"，不能只看配置里配了哪个 provider。
    #    踩过（2026-09-28）：settings 里 provider=openai-compatible 且 candidate_only=true
    #    （**一次模型都没调**），报告却写成 `llm:openai-compatible:hy3` ——
    #    等于对外声称"这些字段是模型填的"。**报表口径错了，比没有报表更糟。**
    llm_called = provider.name != "none" and not candidate_only
    filled_by = ("llm:%s:%s" % (provider.name, provider.model)) if llm_called else "rules"

    if llm_called:
        cache = llm_provider.load_cache(cache_path)
        got = provider.fill(all_cands, fields_expected, cache=cache)
        for c in all_cands:
            v = got.get(c["id"]) or {}
            err = v.get("_error")
            c["fields"] = {k: v.get(k) for k in fields_expected if k in v}
            c["filled_by"] = filled_by
            c["filled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if err:
                c["fill_error"] = err
        llm_provider.save_cache(cache_path, cache)

    pending_payload = {
        "schema": "kg.pending.v1",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": provider.name,
        "candidate_only": bool((ecfg.get("pending") or {}).get("candidate_only", True)),
        "fields_expected": fields_expected,
        "note": ("候选条目由规则筛出（不是结论）；字段请由调用方（模型或 Agent）填写后回灌，"
                 "禁止改动 quote 原文。"),
        "items": all_cands,
    }
    (work / "pending_candidates.json").write_text(
        json.dumps(pending_payload, ensure_ascii=False, indent=1), encoding="utf-8")

    total = {
        "n_files": len(rows),
        # ⚠ 用 qc 过滤后的 graph 计数（口径一致：报告数字 = 图里真实数字）
        "entities": len(graph["entities"]),
        "entities_by_type": {t: sum(1 for e in graph["entities"] if e.get("type") == t)
                             for t in ("PERSON", "ORG", "PROJECT", "MEETING")},
        "relations_raw": len(all_rels),
        "relations_merged": len(graph["relationships"]),
        "edges_with_evidence": sum(1 for r in graph["relationships"]
                                   if (r.get("properties") or {}).get("evidence")),
        "evidence_entries": sum(len((r.get("properties") or {}).get("evidence") or [])
                                if isinstance((r.get("properties") or {}).get("evidence"), list)
                                else (1 if (r.get("properties") or {}).get("evidence") else 0)
                                for r in graph["relationships"]),
        "items": len(all_cands),
        "items_with_org": sum(1 for c in all_cands if c["rules"]["responsible_org"]),
        "items_with_deadline": sum(1 for c in all_cands if c["rules"]["deadline_raw"]),
        "closed_loop": sum(1 for c in all_cands if c["rules"]["closed_loop"]),
        "provider": provider.name,
        "llm_called": llm_called,
        "filled_by": filled_by,
        "warnings_count": len(all_warnings),
        "warnings": all_warnings,
        "seconds": round(time.time() - t0, 1),
        "failures": failures,
        "work_dir": str(work),
    }
    return rows, total
