# -*- coding: utf-8 -*-
"""把 graph_resolved.json 灌入 Neo4j（满血形态存储层验证）。

用法：
    .venv/Scripts/python.exe tools/import_neo4j.py             # 清空重灌 + 对账
    .venv/Scripts/python.exe tools/import_neo4j.py --no-wipe   # 增量（MERGE 幂等）

连接：读 settings.graph_backend.neo4j（地址）+ config/neo4j_pwd.local（密码，交付包排除）。
对账：灌完比对 Neo4j 节点数/边数 vs json 里的实体数/关系数，一致即 PASS。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
CHUNK = 500


def main():
    wipe = "--no-wipe" not in sys.argv
    from kg.serve.backends import get_neo4j_driver

    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    ents = g["entities"]
    rels = g["relationships"]
    print("源数据：%d 实体 / %d 关系" % (len(ents), len(rels)), flush=True)

    driver = get_neo4j_driver()
    with driver.session() as ses:
        ses.run("RETURN 1").consume()                      # 连通性
        print("连接 OK", flush=True)

        if wipe:
            ses.run("MATCH (n:KGEntity) DETACH DELETE n").consume()
            print("已清空旧 KGEntity", flush=True)

        ses.run("CREATE CONSTRAINT kg_entity_id IF NOT EXISTS "
                "FOR (n:KGEntity) REQUIRE n.id IS UNIQUE").consume()

        t0 = time.time()
        rows = [{"id": e["id"], "name": e.get("name", ""), "type": e.get("type", ""),
                 "props": json.dumps(e["properties"], ensure_ascii=False) if e.get("properties") else ""}
                for e in ents]
        for i in range(0, len(rows), CHUNK):
            ses.run("UNWIND $rows AS r MERGE (n:KGEntity {id: r.id}) "
                    "SET n.name = r.name, n.type = r.type, n.props = r.props",
                    rows=rows[i:i + CHUNK]).consume()
        print("实体灌入完成 %.0fs" % (time.time() - t0), flush=True)

        t0 = time.time()
        rrows = [{"s": r["source"], "t": r["target"], "type": r.get("type", ""),
                  "evidence": json.dumps(r.get("properties") or {}, ensure_ascii=False)}
                 for r in rels if r.get("source") and r.get("target")]
        for i in range(0, len(rrows), CHUNK):
            ses.run("UNWIND $rows AS r "
                    "MATCH (a:KGEntity {id: r.s}), (b:KGEntity {id: r.t}) "
                    "MERGE (a)-[e:KG_REL {type: r.type}]->(b) "
                    "SET e.evidence = r.evidence", rows=rrows[i:i + CHUNK]).consume()
        print("关系灌入完成 %.0fs" % (time.time() - t0), flush=True)

        import hashlib
        fp = hashlib.sha256(GRAPH.read_bytes()).hexdigest()
        ses.run("MERGE (m:KGMeta) SET m.fingerprint = $fp, m.updated = timestamp()",
                fp=fp).consume()
        n_node = ses.run("MATCH (n:KGEntity) RETURN count(n) AS c").single()["c"]
        n_rel = ses.run("MATCH ()-[r:KG_REL]->() RETURN count(r) AS c").single()["c"]

    driver.close()
    print()
    print("=== 对账 ===")
    print("Neo4j: %d 节点 / %d 边" % (n_node, n_rel))
    print("json : %d 实体 / %d 关系" % (len(ents), len(rels)))
    ok = (n_node == len(ents) and n_rel == len(rels))
    print("对账结果：", "PASS ✓" if ok else "MISMATCH ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
