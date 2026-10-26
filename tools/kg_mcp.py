# -*- coding: utf-8 -*-
"""kg-mcp：监理纪要知识图谱的 MCP 服务（stdio，纯标准库，零第三方依赖）。

定位：给对话模型/Agent 提供"取数"工具——返回【结构化事实 + 出处】，组织答案是对话模型的事。

工具（3 个）：
    search_entity(名称, 类型?)        找实体（支持公司后缀省略、人名全名）
    get_entity_facts(实体名, ...)     实体的全部关系事实 + 出处（可带时间窗）
    list_matters(项目?, 单位?, ...)   事项清单（可按项目/单位/时间窗过滤）

MCP 协议：stdio 上的 JSON-RPC 2.0（initialize / tools/list / tools/call）。
接入（openclaw.json）：
    "mcpServers": { "kg": {
        "command": "<python>",
        "args": ["<kg>/tools/kg_mcp.py"] } }

⛔ 本进程 stdout 只输出协议报文，任何日志一律走 stderr。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.serve.query import (get_graph, match_entities_ex, facts_struct,    # noqa: E402
                            build_file_dates, parse_window, list_matters)

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
SERVER_INFO = {"name": "kg-monitoring-graph", "version": "1.0.0"}
PROTOCOL = "2024-11-05"


class GraphStore:
    """图缓存：按文件 mtime 热更新（新图落盘后无需重启服务）。"""

    def __init__(self, path):
        self.path = Path(path)
        self._sig = None
        self._graph = {"entities": [], "relationships": []}

    def get(self):
        # ★ 统一供给口：后端由 settings.graph_backend.backend 决定（neo4j=图库执政 / memory=json）
        g = get_graph()
        n, m = len(g.get("entities", [])), len(g.get("relationships", []))
        if (n, m) != self._sig:
            self._sig = (n, m)
            print("kg-mcp: graph loaded from %s (%d entities / %d relationships)"
                  % (("neo4j" if GRAPH_BACKEND == "neo4j" else "json"), n, m), file=sys.stderr)
        return g


from kg.serve.backends import _settings as _bk_settings                      # noqa: E402
GRAPH_BACKEND = (_bk_settings().get("graph_backend") or {}).get("backend", "memory")

STORE = GraphStore(GRAPH)


# ---------------------------------------------------------------- 工具实现
def _win(args):
    return parse_window(args.get("days"), args.get("since"), args.get("until"))


def t_search_entity(a):
    g = STORE.get()
    mr = match_entities_ex(a.get("名称", ""), g.get("entities", []), max_n=10)
    ents = mr["entities"]
    typ = a.get("类型")
    out = [{"name": e.get("name"), "type": e.get("type"), "id": e.get("id")}
           for e in ents if not typ or e.get("type") == typ]
    from kg.serve.query import disambiguation_info
    dis = disambiguation_info([o["name"] for o in out]) if out else {
        "same_legal_entity_groups": [], "distinct_legal_entity_pairs": []}
    return {"count": len(out), "entities": out, "disambiguation": dis,
            "total_matched": mr["total_matched"], "truncated": mr["truncated"],
            "note": "类型代码：ORG=单位 PERSON=人员 PROJECT=项目 MEETING=会议 MATTER=事项。"
                    "disambiguation.same_legal_entity_groups=同一法人实体的别名组（作答可合并陈述）；"
                    "distinct_legal_entity_pairs=人工确认的不同法人（禁止合并陈述：须分别作答或请用户澄清，禁止择一）"}


def t_get_entity_facts(a):
    g = STORE.get()
    name = a.get("实体名", "")
    ents = []
    by_name = {e.get("name"): e for e in g.get("entities", [])}
    if name in by_name:                               # ★ 精确实体名直查（search_entity 返回的名字必中）
        ents = [by_name[name]]
    else:
        ents = match_entities(name, g.get("entities", []), max_n=1)
    if not ents:
        return {"count": 0, "facts": [],
                "note": "图谱中未找到该实体；先用 search_entity 模糊查找"}
    ent = ents[0]
    exact = ent.get("name", "")
    since, until = _win(a)
    fdates = build_file_dates(ROOT) if (since or until) else {}
    from kg.serve.query import evidence_in_window
    facts = facts_struct(ent, g.get("relationships", []), {e["id"]: e for e in g.get("entities", [])})
    total = len(facts)
    from collections import Counter
    fsum = dict(Counter(f["type"] for f in facts))          # ★ 类型分布：开放盘点不漏类、不数错
    if since or until:
        facts = [f for f in facts if evidence_in_window(f["evidence"], fdates, since, until)]
    return {"entity": exact, "count": len(facts), "total_without_window": total,
            "facts_summary": fsum,
            "window": {"since": str(since) if since else None,
                       "until": str(until) if until else None},
            "facts": facts,
            "note": "每条 facts[].evidence = 出处（file/page/quote）。"
                    "回答时逐条标注出处；事实里没有的信息禁止出现。"}


def t_list_matters(a):
    g = STORE.get()
    since, until = _win(a)
    fdates = build_file_dates(ROOT) if (since or until) else {}
    rows = list_matters(g, fdates,
                        project=a.get("项目"), unit=a.get("单位"),
                        since=since, until=until)
    return {"count": len(rows), "matters": rows[:80], "truncated": len(rows) > 80,
            "note": "责任单位可能是角色词（总包单位等），未必是具体公司名；"
                    "期限为原文写法；closed_loop 信息见原始候选文件。"
                    "truncated=true 时仅返回前 80 条：请概述总数并注明「未逐条展开」，禁止当作全部。"}


TOOLS = [
    {"name": "search_entity",
     "description": "按名称查找图谱实体（单位/人员/项目/会议/事项）。支持公司名省略'有限公司'后缀、人名全名匹配。拿到实体名后再用 get_entity_facts 查详情。",
     "inputSchema": {"type": "object", "properties": {
         "名称": {"type": "string", "description": "实体名称或其前缀，如：中建科工 / 吴杰 / 拉普拉斯"},
         "类型": {"type": "string", "enum": ["ORG", "PERSON", "PROJECT", "MEETING", "MATTER"],
                   "description": "可选，过滤实体类型"}},
         "required": ["名称"]}},
    {"name": "get_entity_facts",
     "description": "获取某实体的全部关系事实（结构化，每条带出处 file/page/quote）。回答用户关于某单位/人员/项目的问题时调用本工具取数，再依据事实组织答案；事实没有的信息不得编造。",
     "inputSchema": {"type": "object", "properties": {
         "实体名": {"type": "string", "description": "实体名称（可先 search_entity 确认）"},
         "days": {"type": "integer", "description": "可选：只看最近 N 天的纪要产生的事实"},
         "since": {"type": "string", "description": "可选：起始日期 YYYY-MM-DD"},
         "until": {"type": "string", "description": "可选：截止日期 YYYY-MM-DD"}},
         "required": ["实体名"]}},
    {"name": "list_matters",
     "description": "事项清单：各项目监理例会提出的待办事项（含内容/期限/责任单位/出处），可按项目、责任单位、时间窗过滤。用户问'有哪些待办/整改事项/谁负责'时调用。",
     "inputSchema": {"type": "object", "properties": {
         "项目": {"type": "string", "description": "可选：项目名（含即可）"},
         "单位": {"type": "string", "description": "可选：责任单位名（含即可）"},
         "days": {"type": "integer", "description": "可选：只看最近 N 天"},
         "since": {"type": "string"}, "until": {"type": "string"}}}},
    {"name": "search_context",
     "description": "语义检索历史问答结论与决策记录（Agent 记忆）。适合：'上次查过什么'、'之前关于XX的分析'、'类似案例'。返回按相似度排序的历史结论（含当时的出处）。",
     "inputSchema": {"type": "object", "properties": {
         "查询": {"type": "string", "description": "语义查询，如：拉普拉斯 进度风险"},
         "limit": {"type": "integer", "description": "可选，默认 5"}},
         "required": ["查询"]}},
]

def t_search_context(args):
    """语义检索 Agent 记忆（历史问答结论 + 决策记录）。"""
    q = (args.get("查询") or "").strip()
    if not q:
        return {"error": "缺少 查询 参数"}
    limit = int(args.get("limit") or 5)
    out = {"matches": [], "decisions": []}
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from kg.serve.intelligence import search_context, search_decisions
        out["matches"] = search_context(q, limit=limit)
        out["decisions"] = [{"scenario": d.get("scenario"), "outcome": d.get("outcome"),
                             "reasoning": (d.get("reasoning") or "")[:150],
                             "timestamp": d.get("timestamp")}
                            for d in (search_decisions(q, limit=limit) or [])][:limit]
    except Exception as e:                   # noqa: BLE001 智能层不可用不阻塞基础查询
        out["error"] = "智能层未启用: %s" % e
    return out

_HANDLERS = {"search_entity": t_search_entity,
             "get_entity_facts": t_get_entity_facts,
             "list_matters": t_list_matters,
             "search_context": t_search_context}


# ---------------------------------------------------------------- 协议层
def reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_err(msg_id, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                 "error": {"code": code, "message": message}},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method", "")
    msg_id = req.get("id")
    if method == "initialize":
        reply(msg_id, {"protocolVersion": PROTOCOL,
                       "capabilities": {"tools": {}},
                       "serverInfo": SERVER_INFO})
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        h = _HANDLERS.get(name)
        if not h:
            reply_err(msg_id, -32602, "unknown tool: %s" % name)
            return
        try:
            result = h(args)
            reply(msg_id, {"content": [{"type": "text",
                                        "text": json.dumps(result, ensure_ascii=False)}]})
        except Exception as e:                           # noqa: BLE001
            reply(msg_id, {"content": [{"type": "text",
                                        "text": "工具执行失败: %s: %s" % (type(e).__name__, e)}],
                           "isError": True})
    elif method.startswith("notifications/"):
        pass                                              # 通知无需应答
    elif msg_id is not None:
        reply_err(msg_id, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:                                # noqa: BLE001
            continue
        handle(req)


if __name__ == "__main__":
    main()
