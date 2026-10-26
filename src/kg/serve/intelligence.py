# -*- coding: utf-8 -*-
"""智能层（满血 Semantica 第一批）：Agent 记忆 / 相似检索 / 决策追踪。

全部基于 Semantica 原生 VectorStore（faiss 后端 + 我们注入的 qwen3-embedding）：
    remember / search_context   → vs.store / vs.search（问答结论沉淀与相似检索）
    record_decision             → vs.store_decision（原生决策管道，reasoning 向量化）
    search_decisions            → 决策混合检索（语义+结构，原生）
持久化：vs.save/load → var/intelligence/faiss.index；决策镜像 decisions.jsonl；记忆镜像 memories.jsonl。
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # kg/src/kg/serve/x.py → kg/
_sem_libs = os.environ.get("KG_SEM_LIBS") or str(ROOT / "sem2_libs")
if os.path.isdir(_sem_libs) and _sem_libs not in sys.path:
    sys.path.insert(0, _sem_libs)             # semantica 引擎（函数内 import 前必须就位）
INT_DIR = ROOT / "var" / "intelligence"
DECISIONS_JSONL = INT_DIR / "decisions.jsonl"
MEMORIES_JSONL = INT_DIR / "memories.jsonl"

_vs = None
_cfg = None


def _settings():
    global _cfg
    if _cfg is None:
        import yaml
        _cfg = (yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {})
    return _cfg


class _OpenAIEmbedderAdapter:
    """把 OpenAIStore（已打 base_url 补丁）适配成 VectorStore 需要的 embedder 接口。"""

    def __init__(self, store):
        self.store = store

    def generate_embeddings(self, texts):
        import numpy as np
        single = isinstance(texts, str)     # VectorStore.embed 传字符串 → 约定返回 1 维
        batch = [texts] if single else list(texts)
        arr = np.asarray([list(map(float, v)) for v in self.store.embed_batch(batch)],
                         dtype="float32")
        return arr.reshape(-1) if single else arr

    def generate_embedding(self, text):
        import numpy as np
        return np.asarray(self.store.embed(text), dtype="float32").reshape(-1)

    def embed(self, text):                       # 别名：与 QdrantMemoryStore.embed 调用约定一致
        return self.generate_embedding(text)


def build_embedder():
    """按 settings.embeddings 构建 embedder（OpenAIStore + 指定地址，实测 4096 维）。"""
    from semantica.embeddings import OpenAIStore

    emb = (_settings().get("embeddings") or {})
    provider = emb.get("provider", "openai-compatible")
    if provider != "openai-compatible":
        raise RuntimeError("暂只支持 embeddings.provider=openai-compatible（当前: %s）" % provider)
    api_key = emb.get("api_key") or ""
    kf = emb.get("api_key_file")
    if not api_key and kf:
        p = ROOT / kf
        if p.is_file():
            api_key = p.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError("embeddings 缺少 api_key / api_key_file")
    store = OpenAIStore(api_key=api_key, model=emb.get("model", ""), base_url=emb.get("base_url", ""))
    return _OpenAIEmbedderAdapter(store)


def build_vector_store():
    """按 settings.vector_store.backend 构建记忆存储：qdrant（服务器库）或 faiss（本机文件）。

    两个后端对上层暴露同一套最小接口（embed/store/search/store_decision/
    search_decisions）——remember/search_context/record_decision 无感知差异，
    换库只改配置不改代码。适配层：src/kg/serve/backends.py（版本兼容入口）。"""
    vs_cfg = (_settings().get("vector_store") or {})
    backend = vs_cfg.get("backend", "faiss")

    if backend == "qdrant":
        from kg.serve.backends import (QdrantMemoryStore, embedding_dimension,
                                       get_qdrant_client, qdrant_collection_names)
        dim_actual = embedding_dimension()              # ★ 向 embedding 服务探测实际维度
        dim_cfg = vs_cfg.get("dimension")
        if dim_cfg and int(dim_cfg) != dim_actual:
            raise RuntimeError(
                "settings.vector_store.dimension=%s 与 embedding 服务实际维度=%s 不符。"
                "处置：① 把 dimension 改为 %s（或删掉该键让系统自动探测）；"
                "② 跑 tools/reembed_memory.py 重嵌入历史记忆（jsonl 镜像全量回放）"
                % (dim_cfg, dim_actual, dim_actual))
        st = QdrantMemoryStore(
            embedder=build_embedder(),
            client=get_qdrant_client(),
            coll_memory=qdrant_collection_names()["memory"],
            coll_decisions=qdrant_collection_names()["decisions"],
            dimension=dim_actual)
        st._kg_index_dir = INT_DIR          # jsonl 镜像仍落本机目录（防丢可回放）
        return st

    from semantica.vector_store import VectorStore

    if backend not in ("faiss", "sqlite", "inmemory"):
        raise RuntimeError("vector_store.backend=%s 需服务器侧服务支持，本机请用 faiss/sqlite" % backend)
    index_dir = Path(vs_cfg.get("index_dir", "var/intelligence"))
    if not index_dir.is_absolute():
        index_dir = ROOT / index_dir
    index_dir.mkdir(parents=True, exist_ok=True)

    vs = VectorStore(backend=backend, config={"dimension": int(vs_cfg.get("dimension", 4096)),
                                              "index_dir": str(index_dir)})
    vs.embedder = build_embedder()           # ★ 注入 qwen3-embedding（替换默认 FastEmbed）

    save_path = index_dir / "faiss.index"
    if save_path.exists():                  # ⛔ 注意是目录（save 写目录），不能用 is_file()
        try:
            vs.load(str(save_path))
            # ⛔ 引擎缺口：load 只恢复内存视图，faiss 后端索引与 search_engine 不自动恢复
            #    → FAISSIndex.load 从 index.bin + .meta.json 重建（2026-10-19 实测）
            import faiss as _faiss
            from semantica.vector_store.faiss_store import FAISSIndex, FAISSSearch
            meta = json.load(open(os.path.join(str(save_path), "index.bin.meta.json"), encoding="utf-8"))
            wrapper = FAISSIndex.load(os.path.join(str(save_path), "index.bin"),
                                      int(meta.get("dimension", 4096)),
                                      meta.get("index_type", "flat"))
            bs = vs._backend_store
            bs.index = wrapper
            bs.search_engine = FAISSSearch(wrapper)
        except Exception:                     # noqa: BLE001 索引损坏则重建（jsonl 可回放）
            import traceback
            print("[intelligence] faiss 恢复失败，将重建空索引:", file=sys.stderr)
            print(traceback.format_exc()[-400:], file=sys.stderr)
    vs._kg_index_dir = index_dir
    # ⛔ 决策管道（结构嵌入 128 维 + 语义 4096 维拼接）与本索引维度冲突，
    #    本机固定走语义回退（store_decision/search_decisions 的 fallback 分支，纯 4096 语义）。
    #    生产接 Neo4j 后可用 vs.initialize_decision_pipeline(graph_store=...) 启用结构混合检索。
    vs.decision_pipeline = None
    return vs


def get_vector_store():
    global _vs
    if _vs is None:
        _vs = build_vector_store()
    return _vs


# ---- 记忆（问答结论沉淀 + 相似检索） ------------------------------------------

def remember(question: str, answer: str, sources=None, conversation_id=None):
    """问答结论沉淀（原生 vs.store），并镜像 jsonl 防丢。"""
    import numpy as np
    vs = get_vector_store()
    content = "Q: %s\nA: %s" % (question, answer)
    meta = {"type": "qa_conclusion", "sources": sources or [],
            "ts": datetime.now().isoformat(timespec="seconds")}
    vec = np.asarray(vs.embed(content), dtype="float32").reshape(-1)   # embed 可能返回 (1,d)，展平
    vs.store([vec], [content], [meta])
    INT_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORIES_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"content": content, "meta": meta}, ensure_ascii=False) + "\n")
    _persist(vs)
    return True


def search_context(query: str, limit: int = 5):
    """相似历史结论检索（语义，跨会话）。"""
    vs = get_vector_store()
    hits = vs.search(query, limit=limit) or []
    # ⛔ 引擎 faiss save 不落盘 documents → 正文从 memories.jsonl 镜像回填（meta.ts 作主键）
    mirror = {}
    if MEMORIES_JSONL.is_file():
        for ln in MEMORIES_JSONL.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                mirror[(r.get("meta") or {}).get("ts")] = r.get("content", "")
            except Exception:            # noqa: BLE001
                continue
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        meta = h.get("metadata") or {}
        content = h.get("content") or meta.get("content") or mirror.get(meta.get("ts"), "")
        if content:
            out.append({"content": content[:400], "metadata": meta, "score": h.get("score")})
    return out[:limit]


def _persist(vs):
    try:
        d = getattr(vs, "_kg_index_dir", INT_DIR)
        d.mkdir(parents=True, exist_ok=True)
        vs.save(str(d / "faiss.index"))
    except Exception as e:                   # noqa: BLE001
        print("[intelligence] faiss 持久化失败（jsonl 镜像仍在）: %s" % e, file=sys.stderr)


# ---- 决策追踪（原生 store_decision + jsonl 留痕） ------------------------------

def record_decision(category, scenario, reasoning, outcome, decision_maker,
                    entities=None, source_documents=None, confidence=1.0, metadata=None):
    """记录正式决策（原生 vs.store_decision：reasoning 会向量化，可语义检索）。"""
    vs = get_vector_store()
    did = vs.store_decision(
        scenario=str(scenario), reasoning=str(reasoning), outcome=str(outcome),
        confidence=float(confidence), entities=list(entities or []),
        category=str(category), decision_maker=str(decision_maker),
        source_documents=list(source_documents or []),
    )
    row = {"decision_id": did, "category": category, "scenario": scenario,
           "reasoning": reasoning, "outcome": outcome, "decision_maker": decision_maker,
           "entities": entities or [], "source_documents": source_documents or [],
           "timestamp": datetime.now().isoformat(timespec="seconds"), "confidence": confidence,
           "metadata": metadata or {}}
    INT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DECISIONS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def list_decisions(limit=50):
    if not DECISIONS_JSONL.is_file():
        return []
    rows = [json.loads(ln) for ln in DECISIONS_JSONL.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows[-limit:][::-1]


def search_decisions(query: str, limit: int = 5):
    """决策语义+结构混合检索（原生 search_decisions）。"""
    vs = get_vector_store()
    return vs.search_decisions(query, limit=limit) or []
