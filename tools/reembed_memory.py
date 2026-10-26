# -*- coding: utf-8 -*-
"""换 embedding 模型后的记忆重嵌入回放。

场景：交付方换用自己的 embedding 模型（维度可能不同）→ Qdrant 集合维度不符。
数据安全网：memories.jsonl / decisions.jsonl 是全量镜像（每次写入都落一份），
本脚本读镜像 → 用当前模型重嵌入 → 重建集合并全量回放。

用法：
    .venv/Scripts/python.exe tools/reembed_memory.py            # 先预览，加 --run 真正执行
"""
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sem2_libs"))
sys.stdout.reconfigure(errors="replace")

RUN = "--run" in sys.argv
INT_DIR = ROOT / "var" / "intelligence"


def main():
    from kg.serve.backends import (QdrantMemoryStore, embedding_dimension,
                                   get_qdrant_client, qdrant_collection_names)
    from kg.serve.intelligence import build_embedder

    dim = embedding_dimension()
    print("当前 embedding 模型维度：%d" % dim, flush=True)

    mem_lines = [json.loads(l) for l in
                 (INT_DIR / "memories.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (INT_DIR / "memories.jsonl").is_file() else []
    dec_lines = [json.loads(l) for l in
                 (INT_DIR / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (INT_DIR / "decisions.jsonl").is_file() else []
    print("镜像待回放：记忆 %d 条 / 决策 %d 条" % (len(mem_lines), len(dec_lines)), flush=True)

    if not RUN:
        print("\n（预览模式；确认无误后加 --run 真正执行）")
        sys.exit(0)

    emb = build_embedder()
    client = get_qdrant_client()
    names = qdrant_collection_names()
    st = QdrantMemoryStore(embedder=emb, client=client,
                           coll_memory=names["memory"], coll_decisions=names["decisions"],
                           dimension=dim)

    # 记忆回放（content 直接在镜像里）
    if mem_lines:
        client.delete_collection(names["memory"])
        client.create_collection(names["memory"],
                                 vectors_config={"size": dim, "distance": "Cosine"})
        from qdrant_client.models import PointStruct
        pts = []
        for r in mem_lines:
            content = r.get("content", "")
            pts.append(PointStruct(id=uuid.uuid4().hex, vector=emb.embed(content).tolist(),
                                   payload={"content": content, **(r.get("meta") or {})}))
        for i in range(0, len(pts), 100):
            client.upsert(names["memory"], pts[i:i + 100])
    print("记忆回放：%d 条 ✓" % len(mem_lines), flush=True)

    # 决策回放（payload 字段齐全，reasoning+scenario 重嵌入）
    if dec_lines:
        client.delete_collection(names["decisions"])
        client.create_collection(names["decisions"],
                                 vectors_config={"size": dim, "distance": "Cosine"})
        from qdrant_client.models import PointStruct
        pts = []
        for r in dec_lines:
            payload = {k: v for k, v in r.items() if k != "metadata"}
            text = "%s %s" % (payload.get("scenario", ""), payload.get("reasoning", ""))
            pts.append(PointStruct(id=uuid.uuid4().hex, vector=emb.embed(text).tolist(),
                                   payload=payload))
        for i in range(0, len(pts), 100):
            client.upsert(names["decisions"], pts[i:i + 100])
    print("决策回放：%d 条 ✓" % len(dec_lines), flush=True)
    print("\n重嵌入完成。验证：tools/test_backends.py")


if __name__ == "__main__":
    main()
