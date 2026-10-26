# -*- coding: utf-8 -*-
"""② 第 2 层 · 正文事项 —— **规则只筛候选，字段留给模型或 Agent**

    全文 ──► 切编号条目（1、/ （1） / ① / 一、）
           ──► 触发词过滤（要求 / 应 / 需 / 整改 / 限期 …）
           ──► 排除纯陈述进度（"完成 100%"，除非同句有强要求词）
           ──► 规则版「责任单位 / 期限」只作**参考值**（实测责任单位只到 51%）
    ──► 候选条目（**带 evidence**）→ 交给 llm.provider 或 Agent 补字段

⭐ 顺序价值（**用实测数字说，别夸大**）：规则把"要交给模型的文本"从全文 49,625 字
   压到候选 21,613 字（19 份 / 297 条候选，**省 56%**）—— 省得没想象中多，
   但它买到的东西更值钱：每条候选自带页码与原文片段 → 模型只填字段、不改写原文，
   溯源链不会断；条目边界清楚，输出可以逐条校验。
   要再压 token 走批量调用（20 条/次，提示开销摊薄 20 倍，总输入还能再降约一半）。

实测基线（19 份）：候选 297 条；对 0822 人工标注 32 条主条目 **召回 29/32 = 90.6%**，
精确率约 90%。POC 来源：`kg-poc/scripts-0922/kg_rules30.py`（v3）。

⛔ 两条纪律：
  1. **不让模型改写正文** —— 只允许在候选条目的字段上填值，`quote` 原文一字不改（否则溯源断链）。
  2. 规则给出的字段只是**参考**，必须带 `by` 说明来源（句式 / 简称 / 角色映射 / 全称），
     别让下游把它当成确定结论。
"""
import re

from .meeting import build_rules, role_from_section

EXPECTED_FIELDS = ("responsible_org", "deadline", "matter")

SEC_TITLE = re.compile(r"[一二三四五六七八九十]{1,2}、[^。，,；;：:]{0,22}[：:]?")
ORG_FULL = re.compile(r"[\u4e00-\u9fa5]{2,16}?(?:股份有限公司|集团有限公司|有限公司|公司|集团|设计院|研究院|事务所)")
ORG_BAD = re.compile(r"应|等|经过|须|需|要求|推动|开展|配合|根据|针对|关于|按照|加强|确保")


def _compiled(patterns, flags=0):
    return [re.compile(p, flags) for p in (patterns or [])]


def blank_noise(text, res):
    """等长替换噪声为空格。**必须等长** —— 否则后续所有偏移（也就所有页码）全错。"""
    for rx in res:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


def role_map_of(text, role_set, head_chars=1200):
    """⭐ 用**精确闭集**抽「角色：单位」。

    v2 用泛匹配 `[\\u4e00-\\u9fa5]{2,6}单位` 把「有限公司全咨单位」当成一个角色词 →
    **映射表整体错位**。这条是实测踩出来的，别改回泛匹配。
    """
    pat = re.compile("(" + "|".join(re.escape(r) for r in role_set) + r")\s*[：:]\s*")
    head = text[:head_chars]
    out = {}
    hits = list(pat.finditer(head))
    for i, h in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(head), h.end() + 40)
        val = head[h.end():end]
        val = re.split(r"[\n；;。]", val)[0]
        val = re.sub(r"[（(][^）)]{0,12}[）)]", "", val).strip().strip("、，, ")
        if val and len(val) <= 30 and h.group(1) not in out:
            out[h.group(1)] = val
    return out


def _sections(text):
    return [(m.start(), m.group(0).strip()) for m in SEC_TITLE.finditer(text)]


def _section_at(sections, pos):
    last = None
    for s, title in sections:
        if s <= pos:
            last = title
        else:
            break
    return last


def split_items(text, starts, min_len, max_len):
    """切条目。返回 [(start, end, chunk, section)]，start/end 是**绝对偏移**（供证据定位）。"""
    sections = _sections(text)
    hits = list(starts.finditer(text))
    out = []
    for i, h in enumerate(hits):
        start = h.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(text), start + max_len)
        chunk = text[start:end]
        # ⭐ 把 chunk 内的下一个章节标题截掉，避免条目粘连（v2 的真问题）
        m = SEC_TITLE.search(chunk)
        if m and m.start() > 0:
            chunk = chunk[:m.start()]
        # 去掉首尾空白与标点，并同步修正偏移
        lead = len(chunk) - len(chunk.lstrip())
        chunk = chunk.strip("：: ；;、,。\n\t")
        start = start + lead
        end = start + len(chunk)
        if not (min_len <= len(chunk) <= max_len):
            continue
        out.append((start, end, chunk, _section_at(sections, start)))
    return out


def find_org(item, role_map, pcfg):
    """v3 顺序：句式 → 简称 → 角色词（查映射）→ 公司全称（严格）。返回 (值, 来源说明)。"""
    for rx in _compiled(pcfg.get("subject_patterns")):
        m = rx.search(item)
        if m:
            s = m.group(1)
            for k, v in role_map.items():
                if s in k or k in s:
                    return v, "句式(%s)->%s" % (s, v)
            return s, "句式(%s)" % s
    for s in (pcfg.get("short_orgs") or []):
        if s in item:
            return s, "简称"
    for r in sorted(pcfg.get("role_set") or [], key=len, reverse=True):
        if r in item:
            if r in role_map:
                return role_map[r], "角色(%s)->%s" % (r, role_map[r])
            return r, "角色(%s)未映射" % r
    for m in ORG_FULL.finditer(item):
        cand = m.group(0)
        if not ORG_BAD.search(cand):
            return cand, "全称"
    return "", ""


def find_deadline(item, patterns):
    for rx in _compiled(patterns):
        m = rx.search(item)
        if m:
            return m.group(0).replace(" ", "")
    return ""


def extract_pending(dt, cfg):
    """返回 (candidates, stats)。candidates 每条的字段只作参考，等待模型/Agent 补。"""
    pcfg = (cfg or {}).get("pending") or {}
    if not pcfg.get("enabled", True):
        return [], {"items": 0, "note": "pending 未启用"}

    raw = dt.text
    # ⚠️ 清洗只能做**等长**替换（噪声烤成同长度空格）—— 压缩空白会改变偏移，页码就全错了
    text = blank_noise(raw, _compiled((cfg or {}).get("clean_patterns")))

    min_len = pcfg.get("min_len", 10)
    max_len = pcfg.get("max_len", 500)
    starts = _compiled(pcfg.get("item_start_patterns"))
    if not starts:
        return [], {"items": 0, "note": "没有 item_start_patterns"}
    starts_re = re.compile("|".join("(?:%s)" % p for p in pcfg["item_start_patterns"]))

    triggers = _compiled(pcfg.get("trigger_words")) or None
    progress = _compiled(pcfg.get("progress_only_patterns"))
    strong = re.compile("|".join(pcfg.get("strong_requirement_words") or []) or r"(?!x)x")
    closes = _compiled(pcfg.get("close_patterns"))
    role_map = role_map_of(text, pcfg.get("role_set") or [])
    # 章节角色用第 1 层的同一套规则（配置里的 role_words），避免两处词表漂移
    rules = build_rules(cfg)

    stem = re.sub(r"\.pdf$", "", dt.file or "")
    cands, skipped = [], {"no_trigger": 0, "progress_only": 0, "len": 0}
    for start, end, chunk, section in split_items(text, starts_re, min_len, max_len):
        if triggers and not any(rx.search(chunk) for rx in triggers):
            skipped["no_trigger"] += 1
            continue
        if any(rx.search(chunk) for rx in progress) and not strong.search(chunk):
            skipped["progress_only"] += 1
            continue
        org, by = find_org(chunk, role_map, pcfg)
        deadline = find_deadline(chunk, pcfg.get("deadline_patterns"))
        closed = any(rx.search(chunk) for rx in closes)
        # ⭐ 章节标题 →「提出方」上下文（"一、施工单位汇报" → 施工单位）
        # ⛔ **不能**拿它当责任单位：汇报方 ≠ 要干活的那一方
        #    （"施工单位汇报"段里完全可能写着"要求监理单位…"）。
        #    所以单独成字段：既产出「事项—提出方」关系，也给模型当上下文。
        raised_by = role_from_section(section, rules)
        cands.append({
            "id": "%s#%03d" % (stem, len(cands) + 1),
            "file": dt.file,
            "page": dt.page_of(start),
            "quote": chunk[:300],
            "section": section,
            "rules": {
                "responsible_org": org,
                "responsible_by": by,
                "raised_by": raised_by,
                "deadline_raw": deadline,
                "closed_loop": closed,
            },
            "fields_expected": list(pcfg.get("fields_expected") or EXPECTED_FIELDS),
            "filled_by": None,
            "filled_at": None,
            "fields": {},
            "evidence": dt.evidence(start, end),
        })

    stats = {
        "items": len(cands),
        "role_map": len(role_map),
        "with_org_rule": sum(1 for c in cands if c["rules"]["responsible_org"]),
        "with_deadline_rule": sum(1 for c in cands if c["rules"]["deadline_raw"]),
        "closed_loop": sum(1 for c in cands if c["rules"]["closed_loop"]),
        "skipped": skipped,
        "pages_of_items": sorted({c["page"] for c in cands if c["page"]}),
    }
    return cands, stats
