# -*- coding: utf-8 -*-
"""kg_mcp 自测：工具函数直测 + MCP stdio 协议实测。不需要 pytest。

用法：.venv\\Scripts\\python.exe tools\\test_kg_mcp.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import kg_mcp as M                                              # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, detail))


print("== 1) 工具函数直测 ==")
g = M.STORE.get()
check("图加载", len(g.get("entities", [])) >= 1400,
      str(len(g.get("entities", []))))

r = M.t_search_entity({"名称": "中建科工"})
names = [x["name"] for x in r["entities"]]
check("search_entity 中建科工", any("中建科工集团有限公司" == n for n in names), str(names[:3]))
r = M.t_search_entity({"名称": "吴杰", "类型": "PERSON"})
check("search_entity 人名+类型过滤", any(x["name"] == "吴杰" for x in r["entities"]))
r = M.t_search_entity({"名称": "不存在的实体xyz"})
check("search_entity 未命中返回 0", r["count"] == 0)

r = M.t_get_entity_facts({"实体名": "中建科工集团有限公司"})
check("get_entity_facts 事实>0", r["count"] > 0, str(r["count"]))
check("事实带出处", all(x["evidence"] for x in r["facts"][:3]))
r = M.t_get_entity_facts({"实体名": "中建科工集团有限公司", "days": 60})
check("get_entity_facts 时间窗过滤生效", r["count"] <= r["total_without_window"])

r = M.t_list_matters({"项目": "拉普拉斯"})
check("list_matters 按项目", r["count"] > 0, str(r["count"]))
sample = r["matters"][0] if r["matters"] else {}
check("事项字段齐全", all(k in sample for k in ("内容", "期限", "责任单位", "出处")))
r_all = M.t_list_matters({})
r = M.t_list_matters({"days": 30})
check("list_matters 时间窗", r["count"] <= r_all["count"],
      "%d vs %d" % (r["count"], r_all["count"]))

print("== 2) MCP stdio 协议实测 ==")
proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "kg_mcp.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL)


def send(obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline()
    return json.loads(line.decode("utf-8")) if line else {}


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05",
                 "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
resp = recv()
check("initialize 握手", resp.get("result", {}).get("serverInfo", {}).get("name")
      == "kg-monitoring-graph")

send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
resp = recv()
tool_names = [t["name"] for t in resp.get("result", {}).get("tools", [])]
check("tools/list 4 工具", tool_names == ["search_entity", "get_entity_facts", "list_matters", "search_context"],
      str(tool_names))

send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "get_entity_facts",
                 "arguments": {"实体名": "中建科工集团有限公司", "days": 90}}})
resp = recv()
text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
data = json.loads(text)
check("tools/call 取事实", data.get("count", 0) > 0, text[:80])

send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
      "params": {"name": "not_a_tool", "arguments": {}}})
resp = recv()
check("未知工具报错", "error" in resp)

proc.terminate()
print("=" * 40)
if FAIL:
    print("未通过 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过（%d 项）" % PASS)
