# -*- coding: utf-8 -*-
"""数据库对接端到端验证（本机代码 + 服务器地址，无需登录服务器终端）。

覆盖：
  ① Neo4j 图库：连通 + 节点/边计数 vs graph_resolved.json 对账
  ② Qdrant 向量库：AgentMemory 写入→语义检索闭环 + 决策追踪写入→检索闭环
用法：.venv/Scripts/python.exe tools/test_backends.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sem2_libs"))
sys.stdout.reconfigure(errors="replace")

ok_all = True


def step(name, ok, detail=""):
    global ok_all
    ok_all = ok_all and ok
    print("[%s] %s %s" % ("✓" if ok else "✗", name, detail), flush=True)


# ---- ① Neo4j ----------------------------------------------------------------
print("== ① Neo4j 图库 ==", flush=True)
try:
    from kg.serve.backends import get_neo4j_driver, neo4j_cfg
    g = json.loads((ROOT / "var" / "extract" / "graph_resolved.json").read_text(encoding="utf-8"))
    with get_neo4j_driver().session() as ses:
        ses.run("RETURN 1").consume()
        n_node = ses.run("MATCH (n:KGEntity) RETURN count(n) AS c").single()["c"]
        n_rel = ses.run("MATCH ()-[r:KG_REL]->() RETURN count(r) AS c").single()["c"]
    step("连通（%s）" % neo4j_cfg()["uri"], True)
    match = (n_node == len(g["entities"]) and n_rel == len(g["relationships"]))
    step("对账 %d/%d vs json %d/%d" % (n_node, n_rel, len(g["entities"]), len(g["relationships"])), match,
         "（不一致先跑 tools/import_neo4j.py）")
except Exception as e:
    step("Neo4j", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ---- ② Qdrant（经 intelligence 正式路径，含 embedding 服务） ------------------
print("== ② Qdrant 向量库（AgentMemory + 决策追踪）==", flush=True)
try:
    from kg.serve.intelligence import (record_decision, remember, search_context,
                                       search_decisions)
    step("连接+建集合（按 settings.vector_store.qdrant）", True)

    remember("测试问题：监理整改的闭环流程是什么？",
             "测试回答：整改由责任单位限期完成，监理复核后闭环，出处《xx纪要》第N页。",
             sources=["backends_test.pdf"])
    hits = search_context("监理整改闭环")
    step("记忆写入→语义检索", bool(hits), "| top score: %s" % (round(hits[0]["score"], 4) if hits else "-"))

    record_decision("消解", "backends 测试拍板", "测试依据", "合并", "测试人",
                    entities=["甲公司"], source_documents=["backends_test.pdf"])
    ds = search_decisions("测试拍板")
    step("决策写入→语义检索", bool(ds), "| top score: %s" % (round(ds[0]["score"], 4) if ds else "-"))
except Exception as e:
    import traceback
    step("Qdrant", False, traceback.format_exc().splitlines()[-1][:140])

print()
print("==== 总结果：", "全部 PASS ✓" if ok_all else "存在 FAIL ✗", "====")
sys.exit(0 if ok_all else 1)
