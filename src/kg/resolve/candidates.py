# -*- coding: utf-8 -*-
"""③-② 提候选 —— **相似度算法，没有大模型**（设计决定，见 `__init__.py`）。

两条线索：
  包含   短名出现在长名里（`中建科工集团` ⊂ `中建科工集团有限公司`）
  相似   difflib.SequenceMatcher（标准库，**不引入 rapidfuzz 依赖**）

⛔ 只"提名"，不"判定" —— 它的自动去重实测误报 40%，决定权在人（③ CSV 勾选）。
"""
import re
from difflib import SequenceMatcher


def _similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def propose_candidates(names, alias_map, denied_pairs, threshold=0.72, limit=80,
                       ev_index=None):
    """在实体名两两之间找"疑似同一家"。返回按分数降序的候选对列表。

    ev_index：名字 → 证据列表（由 `__init__._build_evidence_index` 建）。
    传了就给每个候选对附 `ev_a / ev_b`（原始列表，展示格式由 review 层定）——
    ⭐ 人工决定者必须能从 CSV 直接看到"这个名字出现在哪份文件、原文是什么"，
      否则对语料不熟的人没法做决定（2026-09-29 用户指出的缺陷）。
    """
    names = sorted(set(n for n in names if n))
    ev_index = ev_index or {}
    out, seen = [], set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (a, b) in denied_pairs:
                continue
            short, long = (a, b) if len(a) <= len(b) else (b, a)
            basis, score = None, 0.0
            if len(short) >= 4 and short in long:
                basis, score = "包含", 0.6 + 0.35 * len(short) / max(len(long), 1)
            else:
                r = _similar(a, b)
                if r >= threshold:
                    basis, score = "相似", r
            if not basis:
                continue
            k = tuple(sorted((a, b)))
            if k in seen:
                continue
            seen.add(k)
            hint = "名册已含（第①步应已归并，若仍出现说明有遗漏）"
            out.append({"a": a, "b": b, "score": round(score, 3),
                        "basis": basis, "hint": hint,
                        "ev_a": ev_index.get(a, []), "ev_b": ev_index.get(b, [])})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def uncovered_names(names, alias_map):
    """不在名册里的名字（消解真正要处理的）。"""
    return [n for n in names if n not in alias_map]
