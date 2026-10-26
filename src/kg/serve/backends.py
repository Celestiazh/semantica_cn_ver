# -*- coding: utf-8 -*-
"""数据库后端适配层（★ 版本兼容入口）。

所有外部数据库（Neo4j 图库 / Qdrant 向量库）的连接与客户端构造集中在此模块：
  - 地址/凭据一律读 settings.yaml + config/*.local 密码文件，代码零硬编码；
  - 客户端构造支持 driver_options / client_options 透传——库版本兼容参数
    （加密、超时、check_compatibility 等）在配置里加键即可，不改代码；
  - 将来换其他图库/向量库 = 在此新增一个适配函数或 Store 类，调用方零改动。

QdrantMemoryStore：AgentMemory/决策追踪的 Qdrant 形态，
接口与 faiss 版 VectorStore 的使用面（embed/store/search/store_decision/
search_decisions）对齐——intelligence.py 及上层调用方对后端无感知。
"""
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_cfg_cache = None


def _settings():
    global _cfg_cache
    if _cfg_cache is None:
        import yaml
        _cfg_cache = (yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {})
    return _cfg_cache


def _secret(cfg, *file_keys):
    """从配置取密钥：支持明文键或 *_file 指向的本地密码文件（交付包排除）。"""
    for k in file_keys:
        v = cfg.get(k)
        if v:
            return str(v)
    f = cfg.get("password_file") or cfg.get("api_key_file")
    if f:
        p = ROOT / f
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    raise RuntimeError("数据库凭据缺失：配置段缺少明文键或 *_file 文件（%s）" % cfg)


# ---- Neo4j（图库镜像/混合检索） ------------------------------------------------

_neo4j_driver = None
_qd_client = None


def neo4j_cfg():
    gb = _settings().get("graph_backend") or {}
    return gb.get("neo4j") or {}


def get_neo4j_driver():
    """Neo4j 驱动（带缓存）。版本兼容：settings.graph_backend.neo4j.driver_options 透传。"""
    global _neo4j_driver
    if _neo4j_driver is not None:
        return _neo4j_driver
    from neo4j import GraphDatabase
    nc = neo4j_cfg()
    if not nc.get("uri"):
        raise RuntimeError("settings.graph_backend.neo4j 未配置 uri")
    opts = dict(nc.get("driver_options") or {})
    _neo4j_driver = GraphDatabase.driver(
        nc["uri"], auth=(nc.get("user", "neo4j"), _secret(nc)), **opts)
    return _neo4j_driver



# ---- Qdrant（向量库：AgentMemory + 决策追踪） ----------------------------------

def qdrant_cfg():
    return (_settings().get("vector_store") or {}).get("qdrant") or {}


def qdrant_dimension():
    return int((_settings().get("vector_store") or {}).get("dimension", 4096))


_emb_dim_cache = None


def embedding_dimension():
    """探测 embedding 服务实际输出维度（进程内缓存一次）。

    ★ 换 embedding 模型的兼容保证：客户交付后用自己的模型（自部署 vLLM 或云 API），
    维度自动跟随——settings.vector_store.dimension 从"必填项"降级为"校验项"：
      - 未填：以探测值为准；
      - 填了且与探测不符：启动即报错并给出处置指引（fail-fast，不静默写坏库）。"""
    global _emb_dim_cache
    if _emb_dim_cache is not None:
        return _emb_dim_cache
    import json as _json
    import urllib.request
    emb = (_settings().get("embeddings") or {})
    base = (emb.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("settings.embeddings.base_url 未配置，无法探测向量维度")
    key = _secret(emb)
    req = urllib.request.Request(
        base + "/embeddings",
        data=_json.dumps({"model": emb.get("model", ""), "input": ["维度探测"]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = _json.loads(r.read().decode())
    _emb_dim_cache = len(d["data"][0]["embedding"])
    return _emb_dim_cache


def qdrant_collection_names():
    names = (qdrant_cfg().get("collections") or {})
    return {"memory": names.get("memory", "kg_memory"),
            "decisions": names.get("decisions", "kg_decisions")}


def get_qdrant_client():
    """Qdrant 客户端（带缓存）。版本兼容：settings.vector_store.qdrant.client_options 透传。"""
    global _qd_client
    if _qd_client is not None:
        return _qd_client
    from qdrant_client import QdrantClient
    qc = qdrant_cfg()
    if not qc.get("url"):
        raise RuntimeError("settings.vector_store.qdrant 未配置 url")
    opts = dict(qc.get("client_options") or {})
    opts.setdefault("check_compatibility", False)   # 客户端/服务端版本不一致不阻断（兼容入口）
    _qd_client = QdrantClient(url=qc["url"], api_key=_secret(qc), timeout=30, **opts)
    return _qd_client



class QdrantMemoryStore:
    """AgentMemory/决策的 Qdrant 形态（duck-type 对齐 faiss 版使用面）。"""

    def __init__(self, embedder, client, coll_memory, coll_decisions, dimension):
        from qdrant_client.models import Distance, VectorParams
        self.embedder = embedder
        self.client = client
        self.coll_memory = coll_memory
        self.coll_decisions = coll_decisions
        self.dimension = int(dimension)
        for coll in (coll_memory, coll_decisions):
            if not client.collection_exists(coll):
                client.create_collection(
                    coll, vectors_config=VectorParams(size=self.dimension, distance=Distance.COSINE))
            else:
                info = client.get_collection(coll)
                size = info.config.params.vectors.size
                if size != self.dimension:
                    raise RuntimeError(
                        "集合 %s 维度=%s 与当前 embedding 模型维度=%s 不符。"
                        "处置：① settings.vector_store.dimension 改为 %s（或删掉让系统自动探测）；"
                        "② 跑 tools/reembed_memory.py 重嵌入历史记忆（jsonl 镜像可全量回放）"
                        % (coll, size, self.dimension, self.dimension))

    def embed(self, text):
        import numpy as np
        return np.asarray(self.embedder.generate_embedding(text), dtype="float32").reshape(-1)

    def store(self, vectors, texts, metas):
        import uuid
        from qdrant_client.models import PointStruct
        pts, ids = [], []
        for v, t, m in zip(vectors, texts, metas):
            pid = uuid.uuid4().hex
            ids.append(pid)
            pts.append(PointStruct(id=pid, vector=[float(x) for x in v],
                                   payload={"content": t, **(m or {})}))
        self.client.upsert(self.coll_memory, pts)
        return ids

    def search(self, query, limit=5):
        res = self.client.query_points(
            self.coll_memory, query=self.embed(query).tolist(), limit=limit).points
        out = []
        for p in res:
            payload = dict(p.payload or {})
            content = str(payload.pop("content", ""))
            out.append({"content": content[:400], "metadata": payload, "score": p.score})
        return out

    def store_decision(self, scenario="", reasoning="", outcome="", category="",
                       decision_maker="", entities=None, source_documents=None,
                       confidence=1.0, **_kw):
        import uuid
        from qdrant_client.models import PointStruct
        did = uuid.uuid4().hex
        payload = {"scenario": str(scenario), "reasoning": str(reasoning),
                   "outcome": str(outcome), "category": str(category),
                   "decision_maker": str(decision_maker), "entities": list(entities or []),
                   "source_documents": list(source_documents or []),
                   "confidence": float(confidence), "decision_id": did,
                   "timestamp": datetime.now().isoformat(timespec="seconds")}
        vec = self.embed("%s %s" % (scenario, reasoning))
        self.client.upsert(self.coll_decisions,
                           [PointStruct(id=did, vector=vec.tolist(), payload=payload)])
        return did

    def search_decisions(self, query, limit=5):
        res = self.client.query_points(
            self.coll_decisions, query=self.embed(query).tolist(), limit=limit).points
        return [{"score": p.score, **dict(p.payload or {})} for p in res]

    # faiss 版接口兼容位：qdrant 即时持久化，落盘动作无需执行
    def save(self, path):
        return True

    def load(self, path):
        return True


class Neo4jGraphStore:
    """★ Neo4j 执政形态：运行时图数据由图库供给（json 降级为流水线产物+备份）。

    graph() 返回与 graph_resolved.json 同构的 {"entities": [...], "relationships": [...]}
    ——下游全部查询逻辑（match_entities/facts_struct/list_matters/时间窗）零改动。
    缓存指纹：import_neo4j.py 灌库时写入 KGMeta 节点（json 内容哈希），
    指纹不变直接复用进程内缓存，变了自动重取——周增量后 Agent 侧自动跟进。"""

    def __init__(self):
        self.driver = get_neo4j_driver()
        self._cache = None
        self._fp = None

    def fingerprint(self):
        with self.driver.session() as ses:
            rec = ses.run("MATCH (m:KGMeta) RETURN m.fingerprint AS fp LIMIT 1").single()
        return (rec or {}).get("fp") if rec else None

    def graph(self):
        import json as _json
        fp = self.fingerprint()
        if self._cache is not None and fp is not None and fp == self._fp:
            return self._cache
        with self.driver.session() as ses:
            ents = [{"id": r["id"], "name": r["name"], "type": r["type"],
                     "properties": _json.loads(r["props"]) if r["props"] else None}
                    for r in ses.run("MATCH (n:KGEntity) "
                                     "RETURN n.id AS id, n.name AS name, n.type AS type, n.props AS props")]
            rels = []
            for r in ses.run("MATCH (a:KGEntity)-[e:KG_REL]->(b:KGEntity) "
                             "RETURN a.id AS source, b.id AS target, e.type AS type, e.evidence AS evidence"):
                props = _json.loads(r["evidence"]) if r["evidence"] else {}
                rels.append({"source": r["source"], "target": r["target"],
                             "type": r["type"], "properties": props})
        g = {"entities": ents, "relationships": rels}
        self._cache, self._fp = g, fp
        return g
