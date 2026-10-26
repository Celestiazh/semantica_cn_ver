# -*- coding: utf-8 -*-
"""行重建 / 噪声行 / 页眉页脚 —— **纯函数，不碰 PDF、不碰 OCR**，所以可单测。

为什么单独拆出来：这三件事占解析质量的大头（OCR 是按"行框"吐字的，不重建段落就没法读），
但它们全是**确定性清洗** —— 属于"规则该干的活"，不交给模型。

阈值全部从 `config/settings.yaml` 传入，**不硬编码**（加规则不改代码）。

POC 来源：`kg-poc/scripts-0922/kg_parse4.py` 的 rebuild / norm / DROP / 跨页重复行 三段。
"""
import re
import statistics

# 全角/半角标点与空白，去掉后再比对"是不是同一行"（用于跨页重复行判定）
_PUNCT = r"[\s，。、：；·．,.:;\-—_/\\()（）\[\]【】]"
_RE_PUNCT = re.compile(_PUNCT)


def compile_drop(patterns):
    """把配置里的正则串编译好。支持在串里用 (?i) 开大小写不敏感。"""
    out = []
    for p in (patterns or []):
        try:
            out.append(re.compile(p))
        except re.error as e:
            raise ValueError("drop_patterns 里有非法正则 %r：%s" % (p, e))
    return out


def norm_key(s):
    """归一化后用作"同一行"的比较键。"""
    return _RE_PUNCT.sub("", s or "")


def drop_noise(rows, drop_res):
    """剔除水印行 / 试用版标记 / 纯编号行等噪声。返回 (保留, 被删的原文)。"""
    kept, dropped = [], []
    for r in rows:
        t = r.get("text", "")
        if any(p.search(t) for p in drop_res):
            dropped.append(t)
        else:
            kept.append(r)
    return kept, dropped


def repeated_keys(pages, ratio=0.6, min_len=4, max_len=40):
    """跨页重复行 → 判定为页眉/页脚（同一归一化文本在 >=ratio 的页上出现）。

    pages: [[row, ...], ...]（已剔过噪声）
    """
    npages = len(pages)
    if npages < 3:                     # 页数太少，统计不可靠，不做判定
        return set()
    seen = {}
    for i, rows in enumerate(pages):
        for r in rows:
            k = norm_key(r.get("text", ""))
            if min_len <= len(k) <= max_len:
                seen.setdefault(k, set()).add(i)
    return {k for k, v in seen.items() if len(v) / npages >= ratio}


def drop_repeated(pages, keys, min_len=4):
    """把判定为页眉/页脚的行删掉。返回 (保留, 被删的原文)。"""
    kept_pages, dropped = [], []
    for rows in pages:
        keep = []
        for r in rows:
            k = norm_key(r.get("text", ""))
            if k in keys and len(k) >= min_len:
                dropped.append(r.get("text", ""))
            else:
                keep.append(r)
        kept_pages.append(keep)
    return kept_pages, dropped


def group_rows(rows, tol_min=6.0, tol_factor=0.6):
    """把行框按 y 坐标聚成"视觉行"，同一行内按 x 从左到右拼接。

    OCR 是按各自的行框吐字的，同一视觉行可能被切成多个框 —— 不拼就断句。

    ⭐ `cells`（2026-09-28 新增）：除了拼好的 `text`，**额外保留每个单元的 x 区间**。
    `text` 一字不改（不影响已被验证的清洗链与 POC 对照），`cells` 只增不改 ——
    表格类模板（江苏标准表「单位 | 人员」两列）靠它才能把两列分开，
    否则 `南京城建` 和 `居松` 会被粘成 `南京城建居松`，人和单位就配不上对了。
    """
    if not rows:
        return []
    rows = sorted(rows, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    hs = [b["bbox"][3] - b["bbox"][1] for b in rows] or [20]
    tol = max(tol_min, statistics.median(hs) * tol_factor)

    groups, cur, cy = [], [], None
    for b in rows:
        yc = (b["bbox"][1] + b["bbox"][3]) / 2
        if cy is None or abs(yc - cy) <= tol:
            cur.append(b)
            cy = yc if cy is None else (cy + yc) / 2
        else:
            groups.append(cur)
            cur, cy = [b], yc
    if cur:
        groups.append(cur)

    out = []
    for g in groups:
        g = sorted(g, key=lambda b: b["bbox"][0])
        out.append({
            "text": "".join(x["text"] for x in g),
            "y": g[0]["bbox"][1],
            "h": g[0]["bbox"][3] - g[0]["bbox"][1],
            "cells": [{"t": x["text"], "x0": x["bbox"][0], "x1": x["bbox"][2]} for x in g],
        })
    return out


def group_paragraphs(rows, gap_min=24.0, gap_factor=1.9):
    """把视觉行按纵向间距聚成段落（间距明显变大 = 换段）。

    ⚠️ **已知局限**：阈值 = `max(gap_min, 行距中位数 * gap_factor)`，
       行数很少时中位数会被那个大间距自己抬高 → 判不出换段。
       真实页面有几十行，中位数稳定，不受影响；**别拿 3 行样本判断它坏了**。
    """
    if not rows:
        return []
    gaps = [rows[j + 1]["y"] - rows[j]["y"] for j in range(len(rows) - 1)]
    tol = max(gap_min, statistics.median(gaps) * gap_factor) if gaps else 30.0

    paras, buf, prev = [], [], None
    for r in rows:
        if prev is not None and (r["y"] - prev) > tol:
            paras.append("".join(buf))
            buf = []
        buf.append(r["text"])
        prev = r["y"]
    if buf:
        paras.append("".join(buf))
    return paras


def build_pages(raw_pages, drop_patterns=None, cfg_parse=None):
    """完整清洗链：**先合并视觉行 → 噪声行 → 跨页重复行（页眉页脚）→ 段落**。

    raw_pages: [{"page": 1, "source": "textlayer"|"ocr", "rows": [{"text","bbox","score"}]}]
    返回 (pages, stats)：
      pages = [{"page","source","chars","paras","rows"}]   # rows 保留，供抽取阶段定位坐标
      stats = {"rows_raw","noise_dropped","header_dropped","rows_kept"}

    ⛔ **顺序不能反（2026-09-22 踩过）**：必须在**合并后的视觉行**上判"跨页重复行"。
       如果先判重复、后合并，页眉被 OCR 切成碎片时（如 "中国五矿" / "CHONGQING…" / "会议纪要"），
       每个碎片都会单独命中"重复"而被删掉 → **正文内容被误删**（实测全库少 1.6% 字，
       全集中在页眉英文抬头）。先合并后，整条页眉是一个长行（归一化后 >40 字），
       按长度门槛自然排除在"重复行"之外，与已验证的 POC 行为一致。
    """
    p = cfg_parse or {}
    drop_res = compile_drop(drop_patterns if drop_patterns is not None else p.get("drop_patterns"))

    # ① 先按 y 坐标合并视觉行（OCR 会把同一行切成多个框）
    merged = []
    for pg in raw_pages:
        merged.append(group_rows(
            pg.get("rows") or [],
            tol_min=p.get("row_tol_min", 6.0),
            tol_factor=p.get("row_tol_factor", 0.6),
        ))

    # ② 噪声行（水印 / 试用标记 / 纯编号行）
    noise_total = 0
    stage1 = []
    for rows in merged:
        kept, dropped = drop_noise(rows, drop_res)
        noise_total += len(dropped)
        stage1.append(kept)

    # ③ 跨页重复行 → 页眉/页脚
    keys = set()
    if p.get("drop_repeated_lines", True):
        keys = repeated_keys(stage1,
                             ratio=p.get("header_footer_ratio", 0.6),
                             min_len=p.get("repeat_key_min_len", 4),
                             max_len=p.get("repeat_key_max_len", 40))
    if keys:
        stage2, hdr_dropped = drop_repeated(stage1, keys,
                                           min_len=p.get("repeat_key_min_len", 4))
    else:
        stage2, hdr_dropped = stage1, []

    # ④ 段落
    pages = []
    rows_kept = 0
    for pg, rows in zip(raw_pages, stage2):
        paras = group_paragraphs(rows,
                                 gap_min=p.get("para_gap_min", 24.0),
                                 gap_factor=p.get("para_gap_factor", 1.9))
        rows_kept += len(rows)
        pages.append({
            "page": pg.get("page"),
            "source": pg.get("source"),
            "chars": sum(len(x) for x in paras),
            "paras": paras,
            "rows": rows,          # 保留坐标：抽取阶段要给"原文片段"定位
        })

    stats = {
        "rows_raw": sum(len(x) for x in merged),
        "noise_dropped": noise_total,
        "header_dropped": len(hdr_dropped),
        "header_keys": len(keys),
        "rows_kept": rows_kept,
    }
    return pages, stats
