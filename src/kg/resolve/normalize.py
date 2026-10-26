# -*- coding: utf-8 -*-
"""③-① 规则归一：名册加载 + 别名映射

⛔ 程序**不回写** `roster.yaml`（pyyaml 会把人工注释全部毁掉）。
   人工确认沉淀写到 **`roster_extra.yaml`**（程序生成、无注释），加载时按 name 合并。
"""
import re
from pathlib import Path

import yaml


def _load(path):
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_roster(cfg_dir):
    """合并 roster.yaml（人工）+ roster_extra.yaml（程序沉淀），按 name 合并 aliases。"""
    cfg_dir = Path(cfg_dir)
    units = {}
    order = []
    for fname in ("roster.yaml", "roster_extra.yaml"):
        d = _load(cfg_dir / fname)
        for u in (d.get("units") or []):
            n = (u.get("name") or "").strip()
            if not n:
                continue
            if n in units:
                for a in (u.get("aliases") or []):
                    if a not in units[n]["aliases"]:
                        units[n]["aliases"].append(a)
            else:
                units[n] = {"name": n,
                            "aliases": list(u.get("aliases") or []),
                            "type": u.get("type") or ""}
                order.append(n)
    return {"units": [units[n] for n in order]}


def load_denied(cfg_dir):
    """合并 denied_pairs.yaml + denied_extra.yaml → {(a, b), (b, a)} 集合。"""
    cfg_dir = Path(cfg_dir)
    pairs = set()
    for fname in ("denied_pairs.yaml", "denied_extra.yaml"):
        d = _load(cfg_dir / fname)
        for it in (d.get("denied") or []):
            a, b = (it.get("a") or "").strip(), (it.get("b") or "").strip()
            if a and b:
                pairs.add((a, b))
                pairs.add((b, a))
    return pairs


def build_alias_map(roster):
    """别名 → 正名（正名映射到自身）。"""
    m = {}
    for u in roster["units"]:
        m[u["name"]] = u["name"]
        for a in u.get("aliases") or []:
            if a:
                m[a] = u["name"]
    return m


def normalize_graph(graph, alias_map):
    """把命中名册别名的实体归并到正名。

    返回 (new_graph, rename, stats)。归并后同名边会重新聚合（evidence 数组去重拼接）。
    """
    rename = {}
    for e in graph.get("entities") or []:
        n = e.get("name")
        if n and n in alias_map and alias_map[n] != n:
            rename[n] = alias_map[n]

    if not rename:
        return graph, rename, {"renamed": 0, "merged_edges": 0}

    def fix(x):
        return rename.get(x, x)

    ents, seen = [], set()
    for e in graph["entities"]:
        e = dict(e)
        old_id, old_name = e.get("id"), e.get("name")
        new_name = fix(old_name)
        e["name"] = new_name
        # ⛔ id 保守规则（2026-10-14）：L1 实体 id==name，随改名；MATTER 等
        #    id≠name 的实体**保留原 id**——否则同名事项互相去重（1187→20）
        #    且边的 M:: 引用全部悬空（--resolve --apply 曾因此抹掉整个事项层）
        e["id"] = new_name if old_id == old_name else old_id
        if e["id"] not in seen:
            seen.add(e["id"])
            ents.append(e)

    grouped, order = {}, []
    for r in graph.get("relationships") or []:
        props = dict(r.get("properties") or {})
        key = (fix(r.get("source")), fix(r.get("target")), r.get("type"), props.get("role"))
        ev = props.get("evidence")
        if isinstance(ev, dict):
            ev = [ev]
        ev = [x for x in (ev or []) if x]
        if key not in grouped:
            keep = {k: v for k, v in props.items() if k != "evidence"}
            keep["evidence"] = list(ev)
            grouped[key] = {"source": key[0], "target": key[1],
                            "type": key[2], "properties": keep}
            order.append(key)
        else:
            lst = grouped[key]["properties"]["evidence"]
            for x in ev:
                if x not in lst:
                    lst.append(x)

    rels = [grouped[k] for k in order]
    stats = {"renamed": len(rename),
             "merged_edges": len(graph.get("relationships") or []) - len(rels)}
    return {"entities": ents, "relationships": rels}, rename, stats


def unit_tail_ok(name, unit_core_re):
    """名字是否以合法的「名称+单位后缀」结构收尾。"""
    return bool(unit_core_re and re.fullmatch(unit_core_re, name or ""))
