# -*- coding: utf-8 -*-
"""Agent / MCP 工具的实现（每个函数 = 一个工具）。

全部只做两件事：读 `var/extract/` 的产物、把结果写回去并留痕。
**不接模型、不联外网**（模型那一层在 `extract.llm_provider`，由配置切）。
"""
import json
import time
from pathlib import Path

SCHEMA_CANDIDATES = "kg.pending.v1"
SCHEMA_FILLED = "kg.pending.filled.v1"

FILLABLE_FIELDS = ("responsible_org", "deadline", "deadline_iso", "matter", "confidence")


def _work(cfg):
    return cfg.work_dir / "extract"


def _load_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _candidates_path(cfg):
    return _work(cfg) / "pending_candidates.json"


# --------------------------------------------------------------------------
# 工具 1：取候选
# --------------------------------------------------------------------------
def get_pending_candidates(cfg, limit=None, file=None, only_unfilled=True):
    """取待办候选条目（含原文片段与待抽字段的 schema）。

    返回 {"schema", "fields_expected", "items":[{"id","file","page","quote","section",
          "rules","fields_expected","fields", "evidence"}, ...]}
    """
    payload = _load_json(_candidates_path(cfg))
    if not payload:
        raise FileNotFoundError(
            "没有候选产物：先跑 `run.py --extract`（%s）" % _candidates_path(cfg))
    items = payload.get("items") or []
    if file:
        items = [x for x in items if file in (x.get("file") or "")]
    if only_unfilled:
        items = [x for x in items if not (x.get("fields") or {})]
    if limit:
        items = items[:limit]
    return {
        "schema": payload.get("schema") or SCHEMA_CANDIDATES,
        "generated_at": payload.get("generated_at"),
        "provider": payload.get("provider"),
        "fields_expected": payload.get("fields_expected") or [],
        "count": len(items),
        "note": payload.get("note"),
        "items": items,
    }


# --------------------------------------------------------------------------
# 工具 2：回灌 Agent 填好的字段
# --------------------------------------------------------------------------
def submit_extracted_fields(cfg, payload, source="agent"):
    """读回填好的字段并合并。返回 {"accepted", "rejected", "reasons", "remaining_unfilled"}。

    校验（不通过就**拒收该条**，并给出原因）：
      - schema 必须匹配
      - id 必须在候选里存在
      - ⛔ 若带上 `quote`，必须与候选里的原文**逐字相同**（禁止改写正文）
    """
    if isinstance(payload, (str, Path)):
        payload = _load_json(payload)
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict 或指向 JSON 文件的路径")
    schema = payload.get("schema")
    if schema not in (SCHEMA_FILLED, SCHEMA_CANDIDATES, None):
        raise ValueError("schema 不认识：%s" % schema)

    cand_path = _candidates_path(cfg)
    cand_payload = _load_json(cand_path)
    if not cand_payload:
        raise FileNotFoundError("没有候选产物：先跑 `run.py --extract`")

    by_id = {x["id"]: x for x in (cand_payload.get("items") or [])}
    accepted, rejected, reasons = 0, 0, []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    for it in (payload.get("items") or []):
        iid = it.get("id")
        if not iid or iid not in by_id:
            rejected += 1
            reasons.append("id 不存在：%s" % iid)
            continue
        target = by_id[iid]
        quoted = it.get("quote")
        if quoted is not None and quoted.strip() != (target.get("quote") or "").strip():
            rejected += 1
            reasons.append("⛔ quote 被改动，拒收：%s" % iid)
            continue
        fields = {k: it[k] for k in FILLABLE_FIELDS if k in it and it[k] not in (None, "")}
        if not fields:
            rejected += 1
            reasons.append("没有任何可填字段：%s" % iid)
            continue
        target["fields"] = fields
        target["filled_by"] = it.get("filled_by") or source
        target["filled_at"] = stamp
        accepted += 1

    cand_path.write_text(json.dumps(cand_payload, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    # 另存一份"回灌留痕"：谁在什么时候填了什么（对应设计里的流程血统）
    trail = _work(cfg) / "pending_filled.json"
    hist = _load_json(trail, []) or []
    hist.append({"schema": SCHEMA_FILLED, "submitted_at": stamp, "source": source,
                 "accepted": accepted, "rejected": rejected,
                 "items": [x for x in (payload.get("items") or [])]})
    trail.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")

    remaining = sum(1 for x in (cand_payload.get("items") or []) if not (x.get("fields") or {}))
    return {"accepted": accepted, "rejected": rejected, "reasons": reasons[:20],
            "remaining_unfilled": remaining, "trail": str(trail)}


# --------------------------------------------------------------------------
# 工具 3：进度（Agent 循环用）
# --------------------------------------------------------------------------
def pending_status(cfg):
    payload = _load_json(_candidates_path(cfg)) or {}
    items = payload.get("items") or []
    filled = [x for x in items if (x.get("fields") or {})]
    by_who = {}
    for x in filled:
        by_who[x.get("filled_by")] = by_who.get(x.get("filled_by"), 0) + 1
    return {"schema": payload.get("schema"), "generated_at": payload.get("generated_at"),
            "total": len(items), "filled": len(filled), "unfilled": len(items) - len(filled),
            "by_filled_by": by_who, "provider": payload.get("provider")}


# --------------------------------------------------------------------------
# 还没实现的工具：写成明确契约，不要空函数骗人
# --------------------------------------------------------------------------
_MISSING = ("该工具依赖的模块还没建（%s）。当前已实现的是："
            "get_pending_candidates / submit_extracted_fields / pending_status。")


def search_entity(cfg, name):
    """查某个实体的全部关联（所属单位 / 参会 / 相关事项），带出处。"""
    raise NotImplementedError(_MISSING % "④ 建图与查询（调 Semantica 服务端）")


def gather_topic(cfg, keyword):
    """跨文件找相关原文段落，按时间排序，带文件名与页码。"""
    raise NotImplementedError(_MISSING % "⑤ 材料汇聚")


def export_graph(cfg):
    """生成关系图 HTML，返回文件路径。"""
    raise NotImplementedError(_MISSING % "④ 建图与 ⑥ 产出")
