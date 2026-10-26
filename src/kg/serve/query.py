# -*- coding: utf-8 -*-
"""图谱查询共享逻辑：ask_graph.py（问答验收）与 kg_mcp.py（MCP 工具）共用。

设计原则：本模块只依赖标准库 + 图 JSON 文件，**不 import semantica**；
返回结构化事实（而非文本答案），组织答案是对话模型的事。
"""
import json
import re
from datetime import date
from pathlib import Path

SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司")


def load_graph(path):
    """读归并后的图。path 传 Path 或 str。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


_json_cache = {"graph": None, "mtime": None}


_denied_cache = {"pairs": None, "mtime": None}


def denied_pairs():
    """人工驳回过的实体对（config/denied_pairs.yaml，mtime 缓存热更新）。"""
    import yaml
    p = Path(ROOT) / "config" / "denied_pairs.yaml"
    mt = p.stat().st_mtime if p.is_file() else None
    if mt != _denied_cache["mtime"]:
        pairs = []
        if mt:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            pairs = d.get("denied") or []
        _denied_cache["pairs"] = pairs
        _denied_cache["mtime"] = mt
    return _denied_cache["pairs"]


def disambiguation_info(names):
    """候选实体名的归一元信息——归属关系由系统告知，模型不再靠猜。

    same_legal_entity_groups:  候选中互为别名/归一关系的组（同一法人，可合并陈述）
    distinct_legal_entity_pairs: 候选中人工驳回过合并的对（不同法人，禁止合并陈述）"""
    am = alias_map()

    def canon(n):
        return am.get(n, n)

    groups = {}
    for n in names:
        groups.setdefault(canon(n), []).append(n)
    same = [sorted(set(v)) for v in groups.values() if len(set(v)) > 1]
    ns = set(names)
    distinct = [{"pair": [p["a"], p["b"]],
                 "reason": p.get("reason", ""),
                 "pending_business_confirm": bool(p.get("pending_business_confirm"))}
                for p in denied_pairs() if p.get("a") in ns and p.get("b") in ns]
    return {"same_legal_entity_groups": same, "distinct_legal_entity_pairs": distinct}


def get_graph():
    """★ 运行时图统一供给口（执政后端由 settings.graph_backend.backend 决定）。

    neo4j  → 数据由图库供给（Neo4jGraphStore，指纹缓存）；json 降级为流水线产物+备份
    memory → 读 graph_resolved.json（mtime 缓存）
    上层（kg_mcp / ask_graph / 智能层）统一走这里，对后端无感知。"""
    from kg.serve.backends import _settings as _bk_settings
    backend = ((_bk_settings().get("graph_backend") or {}).get("backend") or "memory")
    if backend == "neo4j":
        from kg.serve.backends import Neo4jGraphStore
        return Neo4jGraphStore().graph()
    jp = Path(ROOT) / "var" / "extract" / "graph_resolved.json"
    mt = jp.stat().st_mtime if jp.is_file() else None
    if mt != _json_cache["mtime"]:
        _json_cache["graph"] = load_graph(jp)
        _json_cache["mtime"] = mt
    return _json_cache["graph"]


ROOT = Path(__file__).resolve().parents[3]          # src/kg/serve/query.py → kg/
_ALIAS_CACHE = {"data": None, "mtime": 0.0}


def alias_map():
    """别名 → 全称（config/roster.yaml + roster_extra.yaml 的 units[].aliases）。

    消解拍板的合并决定沉淀在这里（如 工务署→深圳市坪山区建筑工务署）——
    匹配层接上它，简称提问才能命合并后的全称实体。带 mtime 缓存，热更新。"""
    files = [ROOT / "config" / "roster.yaml", ROOT / "config" / "roster_extra.yaml"]
    latest = 0.0
    for f in files:
        if f.is_file():
            latest = max(latest, f.stat().st_mtime)
    if _ALIAS_CACHE["data"] is not None and latest == _ALIAS_CACHE["mtime"]:
        return _ALIAS_CACHE["data"]
    m = {}
    try:
        import yaml
        for f in files:
            if not f.is_file():
                continue
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for u in d.get("units") or []:
                canon = (u.get("name") or "").strip()
                if not canon:
                    continue
                for a in u.get("aliases") or []:
                    a = str(a).strip()
                    if a and a != canon:
                        m[a] = canon
    except Exception:                                # noqa: BLE001 名册读不了就退回纯规则匹配
        m = _ALIAS_CACHE["data"] or {}
    _ALIAS_CACHE["data"] = m
    _ALIAS_CACHE["mtime"] = latest
    return m


def strip_company_suffix(name):
    """去掉公司后缀，便于前缀匹配（'中建科工' 能命中 '中建科工集团有限公司'）。"""
    for s in SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)]
    return name


def match_entities(query, entities, max_n=10):
    """查询串 → 匹配实体列表。match_entities_ex 的薄包装（只取列表）。"""
    return match_entities_ex(query, entities, max_n=max_n)["entities"]


def match_entities_ex(query, entities, max_n=10):
    """查询串 → 匹配结果（列表 + 全量命中数 + 截断标志）。

    打分：长名前缀命中（≥3 字，防"中建八局"仅凭共享"中建"两字蹭进候选）；
    短名（<4 字，如人名）要求全名出现；公司名先去后缀再试；全等最高分。"""
    hits = []
    q = query.strip()
    q_core = strip_company_suffix(q)
    exact = False                                     # 查询串与某实体名全等 → 别名包含兜底不生效
    for e in entities:
        name = e.get("name", "")
        keys = [name]
        core = strip_company_suffix(name)
        if core and core != name:
            keys.append(core)
        score = 0
        for k in keys:
            if len(k) < 2:
                continue
            if k == q or k == q_core:
                score = max(score, len(k) + 10)               # 全等最高分
                exact = True
            elif len(k) < 4:
                if k in q or k in q_core:
                    score = max(score, len(k))
            else:
                for n in range(min(len(k), 12), 2, -1):   # 最低命中 3 字：2 字前缀噪声太多
                    if k[:n] in q or k[:n] in q_core:
                        score = max(score, n)
                        break
        if score:
            hits.append((e, score))
    # ② 名册别名指认：查询词正好是消解拍板沉淀的简称（工务署/省安公司…）
    #    → 直达全称实体置顶（合并后简称不再是独立节点，纯字符串规则打不中）
    am = alias_map()
    canon = am.get(q) or am.get(q_core)
    if not canon and not exact:                      # 问题里包含某别名（取最长命中）
        # ⛔ 全等命中时不做别名包含：精确实体名（如"中建科工发展有限公司"）可能
        #    恰好包含另一实体的别名（"中建科工集团"），兜底会以 999 分压过全等命中
        cand = [(al, cn) for al, cn in am.items() if len(al) >= 2 and (al in q or al in q_core)]
        if cand:
            canon = max(cand, key=lambda x: len(x[0]))[1]
    if canon:
        by_name = {e.get("name"): e for e in entities}
        tgt = by_name.get(canon)
        if tgt is not None:
            hits = [(e, 999) if e is tgt else (e, s) for e, s in hits]
            if all(e is not tgt for e, _ in hits):
                hits.append((tgt, 999))
    hits.sort(key=lambda x: (-x[1], x[0].get("name", "")))
    return {"entities": [e for e, _ in hits[:max_n]],
            "total_matched": len(hits), "truncated": len(hits) > max_n}


def facts_struct(ent, rels, id2ent=None):
    """实体 → 结构化事实列表（不做文本渲染）。
    匹配：实体的 id 或 name 任一出现在边端点即可（归一并改名后端点仍是旧 id）。
    id2ent: 节点 id → 实体 dict；MATTER 端点解析成事项文字并附期限/责任单位。
    每条：{source, type, target, props, evidence:[{file,page,quote}]}（证据最多 3 条）。"""
    id2ent = id2ent or {}
    keys = {ent.get("id"), ent.get("name")} - {None, ""}

    def disp(x):
        e = id2ent.get(x)
        return e.get("name", x) if e else x

    out = []
    for r in rels:
        if not (keys & {r.get("source"), r.get("target")}):
            continue
        props = {k: v for k, v in (r.get("properties") or {}).items() if k != "evidence"}
        ev = (r.get("properties") or {}).get("evidence") or []
        ev = ev if isinstance(ev, list) else [ev]
        f = {
            "source": disp(r.get("source")),
            "type": r.get("type"),
            "target": disp(r.get("target")),
            "props": props,
            "evidence": [{"file": x.get("file", ""), "page": x.get("page"),
                          "quote": (x.get("quote") or "")[:120]}
                         for x in ev[:3]],
        }
        for end in (r.get("source"), r.get("target")):
            e = id2ent.get(end) or {}
            if e.get("type") == "MATTER":
                mp = e.get("properties") or {}
                f["matter"] = {"内容": e.get("name", ""),
                               "期限": mp.get("期限", ""),
                               "责任单位(原文)": mp.get("责任单位(原文)", "")}
        out.append(f)
    return out


def format_fact(f):
    """结构化事实 → ask_graph 用的文本行（保持既有出处格式，兼容出处硬校验）。"""
    lines = ["- 事实：%s ——%s--> %s" % (f["source"], f["type"], f["target"])]
    if f.get("matter"):
        m = f["matter"]
        parts = ["%s=%s" % (k, v) for k, v in m.items() if v]
        if parts:
            lines.append("  事项属性：" + "；".join(parts))
    if f["props"]:
        lines.append("  附加属性：%s" % json.dumps(f["props"], ensure_ascii=False))
    for v in f["evidence"]:
        lines.append("  出处：《%s》第%s页「%s」" % (v["file"], v["page"], v["quote"][:60]))
    return "\n".join(lines)


def format_fact(f):
    """结构化事实 → ask_graph 用的文本行（保持既有出处格式，兼容出处硬校验）。"""
    lines = ["- 事实：%s ——%s--> %s" % (f["source"], f["type"], f["target"])]
    if f["props"]:
        lines.append("  附加属性：%s" % json.dumps(f["props"], ensure_ascii=False))
    for v in f["evidence"]:
        lines.append("  出处：《%s》第%s页「%s」" % (v["file"], v["page"], v["quote"][:60]))
    return "\n".join(lines)


def build_file_dates(root):
    """文件名(含扩展名) → 会议日期(date)。来源：<root>/var/extract/per_file/*.json 的 info.会议时间；
    解析不出再用文件名里的日期。失败的文件不进表（视为无日期）。"""
    out = {}
    pf = Path(root) / "var" / "extract" / "per_file"
    if not pf.is_dir():
        return out
    for j in pf.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        base = Path(str(d.get("file", j.stem))).name
        t = str((d.get("info") or {}).get("会议时间") or "")
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
        if not m:
            m = re.search(r"(20\d{2})[.\-/年]?(\d{1,2})[.\-/月]?(\d{1,2})", base)
        if m:
            try:
                out[base] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return out


def evidence_in_window(evidence, fdates, since, until):
    """证据是否落在时间窗内（按证据文件查会议日期）；无日期保守保留。"""
    if not since and not until:
        return True
    for x in evidence:
        base = Path(str(x.get("file", ""))).name
        d = fdates.get(base)
        if d is None:
            return True
        if since and d < since:
            return False
        if until and d > until:
            return False
        return True
    return True


def parse_window(days=None, since=None, until=None):
    """days/since/until（均可为 None 或 str/int）→ (date|None, date|None)。"""
    since_d = date.fromisoformat(since) if since else None
    until_d = date.fromisoformat(until) if until else None
    if since_d is None and days:
        since_d = date.today() - __import__("datetime").timedelta(days=int(days))
    return since_d, until_d


def list_matters(g, fdates, project=None, unit=None, since=None, until=None):
    """事项清单（MATTER 节点），可按 项目 / 责任单位 / 时间窗 过滤。
    返回 [{事项, 内容, 期限, 责任单位(原文), 填充方, 项目, 责任单位, 出处:[...]}]。"""
    rels = g.get("relationships", [])
    matters = {e["id"]: e for e in g.get("entities", []) if e.get("type") == "MATTER"}
    ent_name = {e["id"]: e.get("name", "") for e in g.get("entities", [])}
    proj_of, unit_of = {}, {}
    for r in rels:
        s, t, ty = r.get("source"), r.get("target"), r.get("type")
        if ty == "属于项目" and s in matters:
            proj_of[s] = ent_name.get(t, t)
        if ty == "事项责任单位" and s in matters:
            unit_of.setdefault(s, []).append(ent_name.get(t, t))
    out = []
    for mid, e in matters.items():
        if project and project not in proj_of.get(mid, ""):
            continue
        if unit and not any(unit in u for u in unit_of.get(mid, [])):
            continue
        props = e.get("properties") or {}
        ev = props.get("evidence") or []
        if not ev and props.get("file"):
            ev = [{"file": props.get("file"), "page": props.get("page"), "quote": props.get("quote")}]
        ev = ev if isinstance(ev, list) else [ev]
        if not evidence_in_window(ev, fdates, since, until):
            continue
        out.append({
            "事项": e.get("name", ""),            # 事项文字在节点 name（content 已废弃）
            "内容": e.get("name", "") or props.get("content", ""),
            "期限": props.get("期限", ""),
            "责任单位(原文)": props.get("责任单位(原文)", ""),
            "责任单位": unit_of.get(mid, []),
            "项目": proj_of.get(mid, ""),
            "填充方": props.get("filled_by", ""),
            "出处": [{"file": Path(str(x.get("file", ""))).name, "page": x.get("page"),
                      "quote": (x.get("quote") or "")[:100]} for x in ev[:3]],
        })
    return out
