# -*- coding: utf-8 -*-
"""Qdrant 向量库冒烟：真实调 embedding 服务 → 建集合 → 写入 → 语义检索。

用法：.venv/Scripts/python.exe tools/test_qdrant.py
依赖：config/embed_key.local（qwen3-embedding-8B @8101）、Qdrant @6333（API_KEY 同 Neo4j 测试约定）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

COLL = "kg_smoke_test"
DIM = 4096

from kg.config import Config
from kg.extract import llm_provider as L

cfg = Config(ROOT)
prov = L.build_provider(cfg)                      # 仅用于读 embedding 地址配置？不——直接用 embeddings 段
import yaml
emb = (yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}).get("embeddings") or {}
api_key = (ROOT / "config" / "embed_key.local").read_text(encoding="utf-8").strip()

import urllib.request
import json as J

def embed(texts):
    req = urllib.request.Request(
        emb.get("base_url", "http://123.184.7.210:8101") + "/embeddings",
        data=J.dumps({"model": emb.get("model", "qwen3-embedding-8B"), "input": texts}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = J.loads(r.read().decode())
    return [x["embedding"] for x in d["data"]]

from kg.serve.backends import get_qdrant_client
from qdrant_client.models import Distance, VectorParams, PointStruct

client = get_qdrant_client()

docs = [
    "中建科工集团有限公司参与拉普拉斯产业园项目，担任施工单位。",
    "坪山交警大队营房建设项目由中建科工集团总承包施工。",
    "监理例会要求本周内完成脚手架验收整改并回复期限。",
]
vecs = embed(docs)
print("embedding 服务 ✓ | 维度:", len(vecs[0]), flush=True)

if client.collection_exists(COLL):
    client.delete_collection(COLL)
client.create_collection(COLL, vectors_config=VectorParams(size=len(vecs[0]), distance=Distance.COSINE))
client.upsert(COLL, [PointStruct(id=i, vector=v, payload={"text": t})
                     for i, (t, v) in enumerate(zip(docs, vecs))])
print("写入 3 条 ✓", flush=True)

hits = client.query_points(COLL, query=embed(["脚手架整改的责任落实"])[0], limit=2).points
print("检索 '脚手架整改的责任落实' Top-2：", flush=True)
for h in hits:
    print("  score=%.4f | %s" % (h.score, h.payload["text"][:40]))

client.delete_collection(COLL)
print("清理测试集合 ✓ | Qdrant 冒烟 PASS ✓")
