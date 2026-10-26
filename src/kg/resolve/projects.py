# -*- coding: utf-8 -*-
"""PROJECT 节点归一：剥离日期前缀 + 确定性合并 + 人工决定 + 相似候选提名。

背景（2026-10-13）：语料文件名常带日期前缀（20260825广东建工科创大厦项目），
抽取后同一真实项目按场次裂成多个 PROJECT 节点 → 单位-项目统计被分散、台账出现大量 0。

规则（宁缺勿滥，沿用消解口径）：
  ① 剥离节点名/id 开头的日期前缀 → 归一名；
  ② 归一名完全相同的变体 → 自动合并到边数最多的节点；
  ③ 归一名互为包含的（A ⊂ B）→ 只提名不合并，写 review_project.csv 人工确认；
  ④ 人工决定（review_project.csv「决定=合并」）→ 按保留哪条落地；
  ⑤ **正名的 id 同步归一**（id 带日期会让看板显示带日期——看板展示的是 id/content）。

合并语义：变体与旧 id 的所有边改挂到最终 id；同 (source,type,target) 边去重合并
evidence；全程幂等：已归一的第二遍跑是无操作。
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

DATE_PREFIX = re.compile(
    r"^\s*(?:20\d{2}\s*[.\-/年]?\s*\d{1,2}\s*[.\-/月]?\s*\d{1,2}\s*日?\s*[-_—]?\s*)+")


def norm_name(name):
    """剥日期前缀 + 去尾部空白。'20260825广东建工科创大厦项目' → '广东建工科创大厦项目'"""
    return DATE_PREFIX.sub("", name or "").strip()


def _ev_list(r):
    ev = (r.get("properties") or {}).get("evidence") or []
    return ev if isinstance(ev, list) else [ev]


def _merge_alias_path(root):
    return (Path(root) / "config" / "projects_merge_alias.yaml"
            if root else Path("config") / "projects_merge_alias.yaml")


def load_merge_alias(root):
    """持久合并别名表：历史变体名 → 归一名（自动沉淀 + 播种，跨重建生效）。"""
    p = _merge_alias_path(root)
    if p.is_file():
        try:
            import yaml
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:                                # noqa: BLE001
            return {}
    return {}


def save_merge_alias(root, alias):
    p = _merge_alias_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    p.write_text(yaml.safe_dump(alias, allow_unicode=True, sort_keys=True),
                 encoding="utf-8")


def seed_alias_from_log(root):
    """从 project_merge_log.csv 播种别名表（变体名 → 归一名）。"""
    log = Path(root) / "var" / "resolve" / "project_merge_log.csv"
    if not log.is_file():
        return 0
    alias = load_merge_alias(root)
    n = 0
    with open(log, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            v = (row.get("变体节点") or "").strip()
            k = (row.get("归一名") or "").strip()
            if v and k and alias.get(v) != k:
                alias[v] = k
                n += 1
    if n:
        save_merge_alias(root, alias)
    return n


def consolidate_projects(graph, root=None):
    """PROJECT 归一主流程（2026-10-14 重写）：

    ① 分组：持久别名(projects_merge_alias)先解析 → 归一名组；
    ② 自动合并（组内边最多者为正名）+ 人工决定（review_project.csv）；
    ③ 正名 id/name 归一（旧 id → 归一名，登记 id_rename）；
    ④ 端点重写：endpoint → merge_to 链 → id_rename → 最终 id；同键边 evidence 合并；
    ⑤ 实体过滤：变体对象删除；同最终 id 的多余实体（如 bare 与正名同 id）保留正名；
    ⑥ 端点修复：仍悬空的（历史名/别名表可对号的）补挂；沉淀别名表。
    全程幂等。
    """
    ents = list(graph.get("entities", []))
    rels = list(graph.get("relationships", []))
    obj_by_id = {e.get("id"): e for e in ents if e.get("id")}

    # ⭐ 名字快照（改名前）：name/归一名 → 旧 id —— fixup 用
    by_name = {}
    for e in ents:
        nm = e.get("name", "")
        by_name.setdefault(nm, e.get("id"))
        nn0 = norm_name(nm)
        if nn0:
            by_name.setdefault(nn0, e.get("id"))

    projs = [e for e in ents if e.get("type") == "PROJECT"]
    alias0 = load_merge_alias(root)

    # ---- ① 分组（持久别名先解析） ------------------------------------------
    by_norm = defaultdict(list)
    for e in projs:
        nm = norm_name(e.get("name", ""))
        if not nm:
            continue
        tn = alias0.get(nm) or alias0.get(e.get("name", "")) or nm
        by_norm[tn].append(e)

    def conn(e):
        return sum(1 for r in rels if e.get("id") in (r.get("source"), r.get("target")))

    merge_to = {}                    # 变体 id → 正名旧 id
    final_name = {}                  # 正名旧 id → 归一名
    groups = 0
    for tn, members in by_norm.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=conn, reverse=True)
        canon = members[0]
        for e in members[1:]:
            merge_to[e["id"]] = canon["id"]
        final_name[canon["id"]] = tn
        groups += 1

    # ---- ①b 人工决定（review_project.csv「决定=合并」） ---------------------
    if root:
        rc = Path(root) / "var" / "resolve" / "review_project.csv"
        if rc.is_file():
            name2id = {}
            for e in projs:
                name2id.setdefault(e.get("name", ""), e["id"])
                nn = norm_name(e.get("name", ""))
                if nn:
                    name2id.setdefault(nn, e["id"])
            with open(rc, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("决定") or "").strip() != "合并":
                        continue
                    ia = name2id.get((row.get("A") or "").strip()) or \
                        name2id.get(norm_name(row.get("A") or ""))
                    ib = name2id.get((row.get("B") or "").strip()) or \
                        name2id.get(norm_name(row.get("B") or ""))
                    keep = (row.get("保留哪条") or "").strip()
                    ik = name2id.get(keep) or name2id.get(norm_name(keep))
                    if not ia or not ib:
                        continue
                    target = ik if ik in (ia, ib) else ia
                    other = ib if target == ia else ia
                    if other != target:
                        merge_to[other] = target
                        final_name.setdefault(target, norm_name(keep) or keep)

    # ---- ② 正名 id/name 归一（旧 id → 归一名） ------------------------------
    id_rename = {}                   # 正名旧 id → 新 id
    renamed = 0
    for cid, tn in final_name.items():
        if cid == tn:
            continue
        id_rename[cid] = tn
        e = obj_by_id.get(cid)
        if e is not None:
            e["id"] = tn
            e["name"] = tn
            renamed += 1

    def final_of(x):
        """旧端点 → 最终 id：merge_to 链（变体→正名旧id）→ id_rename。"""
        seen = set()
        while x in merge_to and x not in seen:
            seen.add(x)
            x = merge_to[x]
        return id_rename.get(x, x)

    # ---- ③ 端点重写 + 同键边 evidence 合并 ----------------------------------
    final_ids = {final_of(e.get("id")) for e in ents}
    seen, new_rels = {}, []
    for r in rels:
        r2 = dict(r)
        r2["source"] = final_of(r2.get("source"))
        r2["target"] = final_of(r2.get("target"))
        key = (r2["source"], r2["type"], r2["target"])
        ev = _ev_list(r2)
        if key in seen:
            base = seen[key]
            old = (base.get("properties") or {}).get("evidence") or []
            old = old if isinstance(old, list) else [old]
            have = {(x.get("file"), x.get("page"), x.get("quote")) for x in old}
            for x in ev:
                k3 = (x.get("file"), x.get("page"), x.get("quote"))
                if k3 not in have:
                    old.append(x)
                    have.add(k3)
            base["properties"]["evidence"] = old
        else:
            if ev:
                r2.setdefault("properties", {})["evidence"] = ev
            seen[key] = r2
            new_rels.append(r2)

    # ---- ④ 实体过滤：变体删除；同最终 id 保留正名对象 ------------------------
    variant_objs = set()
    for k in merge_to:
        o = obj_by_id.get(k)
        if o is not None:
            variant_objs.add(id(o))
    by_final = defaultdict(list)
    for e in ents:
        by_final[final_of(e.get("id"))].append(e)
    claimed = {}
    for cid, tn in id_rename.items():
        o = obj_by_id.get(cid)
        if o is not None:
            claimed[tn] = o
    new_ents = []
    for fid, lst in by_final.items():
        keep = claimed.get(fid) or max(lst, key=conn)
        new_ents.append(keep)

    # ---- ⑤ 端点修复：仍悬空的按 名字快照/持久别名 补挂 ----------------------
    fixed = 0
    merge_alias = load_merge_alias(root)
    for r in new_rels:
        for side in ("source", "target"):
            v = r.get(side)
            if v and v not in final_ids:
                hit = by_name.get(v) or by_name.get(norm_name(v))
                if not hit:
                    a = merge_alias.get(v) or merge_alias.get(norm_name(v))
                    if a:
                        hit = by_name.get(a) or by_name.get(norm_name(a))
                if hit:
                    hit = final_of(hit)
                    r[side] = hit
                    fixed += 1

    # ---- ⑥ 沉淀本轮 变体名/旧id → 归一名（跨重建修复历史引用） ---------------
    if merge_to and root:
        alias = load_merge_alias(root)
        for vid, fid in merge_to.items():
            fid = final_of(fid)
            vn = id2name(ents, vid)
            kn = norm_name(id2name(ents, fid))
            if vn and kn:
                alias[vn] = kn
        save_merge_alias(root, alias)

    graph["entities"] = new_ents
    graph["relationships"] = new_rels

    # 留痕 CSV
    if groups and root:
        out = Path(root) / "var" / "resolve" / "project_merge_log.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["变体节点", "并入正名", "归一名"])
            for vid, fid in merge_to.items():
                w.writerow([id2name(ents, vid), id2name(ents, fid),
                            final_name.get(fid, "")])

    return graph, {"merged_groups": groups, "merged_nodes": len(merge_to),
                   "renamed": renamed, "candidates": 0, "endpoints_fixed": fixed,
                   "projects": sum(1 for e in new_ents if e.get("type") == "PROJECT")}


def id2name(ents, eid):
    for e in ents:
        if e.get("id") == eid:
            return e.get("name", "")
    return eid


def write_candidates(graph, root):
    """包含关系候选 → review_project.csv（人工填「决定」列：合并/驳回）。"""
    cands = []
    projs = [e for e in graph.get("entities", []) if e.get("type") == "PROJECT"]
    nms = sorted({norm_name(e.get("name", "")) for e in projs})
    nms = [n for n in nms if n]
    for i, a in enumerate(nms):
        for b in nms[i + 1:]:
            if a in b or b in a:
                cands.append((a, b))
    if not cands:
        return None
    out = Path(root) / "var" / "resolve" / "review_project.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["A", "B", "依据", "决定", "保留哪条"])
        for a, b in cands:
            w.writerow([a, b, "包含（剥日期后）", "", ""])
    return out
