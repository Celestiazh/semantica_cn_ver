# -*- coding: utf-8 -*-
"""MATTER 事项层并入图（L2）。

把待办候选里**已填充字段**的条目转成图节点：
  MATTER 节点（id = "M::<候选id>"，name = 事项描述或原文截断）
  ├─ MATTER --事项责任单位--> ORG   （责任单位经名册别名归一到正名；
  │                                  定位不到的建新 ORG 节点 —— 与 L1 行为一致）
  └─ MATTER --属于项目--> PROJECT   （从逐份明细的 info["项目"] 关联，能对上才连）
  节点属性带：期限 / deadline_iso / 原文 quote / file / page / filled_by —— 溯源不断。

幂等：按节点 id 判断，重跑只增量。产出直接写回 graph_resolved.json。
"""
import json
import re
from pathlib import Path

GRAPH = Path("var") / "extract" / "graph_resolved.json"
CAND = Path("var") / "extract" / "pending_candidates.json"
PER_FILE = Path("var") / "extract" / "per_file"


def _unit_core(cfg):
    """单位名 fullmatch 模式（与 meeting/qc 同源）。"""
    mc = cfg.get("extract.meeting") or {}
    alt = "|".join(mc.get("unit_suffixes") or
                   ["公司", "集团", "院", "所", "中心", "部", "局", "署", "大学"])
    return re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:" + alt + ")")


def build_matter(cfg):
    from ..resolve.normalize import build_alias_map, load_roster

    root = Path(cfg.root)
    graph = json.loads((root / GRAPH).read_text(encoding="utf-8"))
    cand = json.loads((root / CAND).read_text(encoding="utf-8"))
    roster = load_roster(cfg.config_dir)
    alias = build_alias_map(roster)

    have_nodes = {e.get("name") for e in graph.get("entities") or []}
    have_ids = {e.get("id") for e in graph.get("entities") or []}
    org_names = {e.get("name") for e in graph.get("entities") or []
                 if e.get("type") == "ORG"}
    proj_names = {e.get("name") for e in graph.get("entities") or []
                  if e.get("type") == "PROJECT"}

    # 文件 → 项目（per_file info）。键名对齐（实测三套后缀混用）：
    # per_file = "<词根>.text.json"，候选 file = "<词根>.pdf" —— 统一剥成词根再对。
    file_proj = {}
    for fp in PER_FILE.glob("*.json"):
        key = fp.name
        if key.endswith(".text.json"):
            key = key[:-len(".text.json")]
        elif key.endswith(".json"):
            key = key[:-len(".json")]
        for suf in (".pdf", ".docx", ".text"):
            if key.endswith(suf):
                key = key[:-len(suf)]
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        proj = ((d.get("info") or {}).get("项目") or "").strip()
        if proj:
            file_proj[key] = proj

    def norm_file(fname):
        f = fname or ""
        for suf in (".pdf", ".docx", ".text"):
            if f.endswith(suf):
                f = f[:-len(suf)]
        return f

    def resolve_org(name):
        """责任单位 → 图上 ORG 名。名册别名 > 精确同名 > 新建。"""
        if not name:
            return None, False
        n = alias.get(name, name)
        if n in org_names:
            return n, False
        return n, True                       # 需新建

    def resolve_project(fname):
        proj = file_proj.get(fname)
        if not proj:
            return None
        if proj in proj_names:
            return proj
        # 剥日期前缀后比较（2026-10-13）：不同场次的日期前缀互不为子串，必须归一后比
        from kg.resolve.projects import norm_name
        pn_norm = norm_name(proj)
        for pn in proj_names:
            if norm_name(pn) == pn_norm:     # 归一后相等 = 同一项目
                return pn
        for pn in proj_names:                # 退一步：归一后互为子串也算同一项目
            a, b = norm_name(pn), pn_norm
            if a and b and (a in b or b in a):
                return pn
        for pn in proj_names:                # 再退：原始写法互为子串
            if proj in pn or pn in proj:
                return pn
        return None

    ents, rels = list(graph.get("entities") or []), list(graph.get("relationships") or [])
    have_nodes = {e.get("name") for e in ents}
    have_ids = {e.get("id") for e in ents}
    new_orgs, new_projs = set(), set()
    n_matter = n_edge_org = n_edge_proj = 0
    skipped_role = 0
    skipped_junk = 0
    backfill_proj = 0

    # 角色词表（纯角色词不建 ORG 节点 —— 等"角色→单位"映射，宁缺不错）
    role_words = set((cfg.get("extract.meeting.role_words") or []))
    role_words |= {"施工单位", "建设单位", "监理单位", "设计单位", "总包单位",
                   "分包单位", "使用单位", "建设单位采购部门"}

    for it in cand.get("items") or []:
        f = it.get("fields") or {}
        org_raw = (f.get("responsible_org") or "").strip()
        if not org_raw:
            continue
        cid = it.get("id")
        mid = "M::" + cid
        if mid in have_ids:
            continue                          # 幂等：已并过
        quote = (it.get("quote") or "").strip()
        matter_txt = (f.get("matter") or "").strip() or quote[:40]
        ents.append({
            "id": mid, "name": matter_txt, "type": "MATTER",
            "properties": {
                "file": it.get("file") or "", "page": it.get("page"),
                "quote": quote, "期限": (f.get("deadline") or "").strip(),
                "deadline_iso": (f.get("deadline_iso") or "").strip(),
                "责任单位(原文)": org_raw, "filled_by": it.get("filled_by") or "",
            }})
        have_ids.add(mid)
        n_matter += 1

        # MATTER --事项责任单位--> ORG
        if org_raw in role_words:
            skipped_role += 1                 # 纯角色词：等映射，不建 ORG
        else:
            org, need_new = resolve_org(org_raw)
            if need_new:
                # ⭐ 新建 ORG 前必须"长得像机构名"（unit_core fullmatch）——
                #    LLM 偶尔回碎片/句段，过滤后宁缺不错；原文保留在 MATTER 属性里
                if not re.fullmatch(_unit_core(cfg), org):
                    skipped_junk += 1
                    org, need_new = None, False
            if org and need_new and org not in have_nodes:
                ents.append({"id": org, "name": org, "type": "ORG"})
                org_names.add(org)
                have_nodes.add(org)
                new_orgs.add(org)
            if org:
                rels.append({"source": mid, "type": "事项责任单位", "target": org,
                             "properties": {"evidence": [{"file": it.get("file") or "",
                                                          "page": it.get("page"),
                                                          "quote": quote[:80]}]}})
                n_edge_org += 1
            elif org is None and need_new is False:
                pass

        # MATTER --属于项目--> PROJECT
        proj = resolve_project(norm_file(it.get("file") or ""))
        if proj:
            rels.append({"source": mid, "type": "属于项目", "target": proj,
                         "properties": {"evidence": [{"file": it.get("file") or "",
                                                      "page": it.get("page"),
                                                      "quote": quote[:50]}]}})
            n_edge_proj += 1

    # ③′ 补边回填：幂等跳过的老 MATTER 节点若缺「属于项目」边（早期键名 bug 所致）→ 补上
    have_proj_edges = {(r.get("source"), r.get("target")) for r in rels
                       if r.get("type") == "属于项目"}
    for it in cand.get("items") or []:
        cid = it.get("id")
        mid = "M::" + cid
        if mid not in have_ids:
            continue
        proj = resolve_project(norm_file(it.get("file") or ""))
        if proj and (mid, proj) not in have_proj_edges:
            quote = (it.get("quote") or "").strip()
            rels.append({"source": mid, "type": "属于项目", "target": proj,
                         "properties": {"evidence": [{"file": it.get("file") or "",
                                                      "page": it.get("page"),
                                                      "quote": quote[:50]}]}})
            have_proj_edges.add((mid, proj))
            backfill_proj += 1

    graph["entities"] = ents
    graph["relationships"] = rels
    (root / GRAPH).write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"matter": n_matter, "edge_org": n_edge_org, "edge_proj": n_edge_proj,
            "backfill_proj": backfill_proj,
            "new_orgs": sorted(new_orgs), "skipped_role": skipped_role,
            "skipped_junk": skipped_junk,
            "graph": str(root / GRAPH)}
