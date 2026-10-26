# -*- coding: utf-8 -*-
"""读 .docx —— Word 版纪要直接读 XML，**不需要 OCR**

为什么划算：docx 本身就是结构化文本（word/document.xml），
- 不用渲染、不用 OCR → 比扫描件 PDF 快几个数量级
- 页眉/页脚在 `header*.xml` / `footer*.xml` 里，`document.xml` 天然不含 → **不用做页眉页脚剔除**
- 表格（签到表、参会名单常在表格里）能单独取出来

## 页码怎么来（诚实说明）

docx **没有固定页**（页是渲染结果）。我们按这个顺序取：
1. ⭐ `w:lastRenderedPageBreak` —— Word 上次排版时插的分页标记，**这是真页码边界**（Word 存过的文档通常都有）
2. `w:br w:type="page"` —— 手工插入的分页符
3. 都没有 → **整篇算 1 页**，并在 evidence 里带 `para_index`（第几段）作为定位

⛔ 绝不"按字数假装分页" —— 那会给出**看似精确的假页码**，比没有页码更糟。

产物结构与 PDF 完全一致：{"file", "pages":[{page, source:"docx", chars, paras, rows}], "meta"}
"""
import json
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# ⭐ docx 自己的参数指纹（缓存判定要用它，**不能拿 PDF 的指纹去比**）。
#    ⛔ 踩过（2026-09-28）：缓存校验只比 PDF 指纹 → docx 的 `meta.params` 永远对不上 →
#    **68 个 docx 每次都被重解析、缓存从来没生效过**（因为只要 0.0 秒所以一直没人发现）。
#    `v` 是读取器版本号：读取逻辑变了就 +1（v2 = 行里带 cells / y）。
PARAMS_FINGERPRINT = json.dumps({"reader": "docx", "v": 2}, ensure_ascii=False, sort_keys=True)


def _para_text(p):
    """取一个段落/单元格的文本（含制表符）。"""
    parts = []
    for node in p.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
    return re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()


def _has_page_break(p):
    for node in p.iter():
        if node.tag == W + "lastRenderedPageBreak":
            return True
        if node.tag == W + "br" and (node.get(W + "type") == "page"):
            return True
    return False


def _blocks(body):
    """按文档顺序产出 (text, page_break_before)。表格按行产出，单元格用制表符连接。"""
    for el in list(body):
        if el.tag == W + "p":
            yield _para_text(el), _has_page_break(el)
        elif el.tag == W + "tbl":
            for tr in el.findall(W + "tr"):
                cells = [_para_text(tc) for tc in tr.findall(W + "tc")]
                line = "\t".join(c for c in cells if c)
                if line:
                    yield line, _has_page_break(tr)


def read_docx(path):
    """读一个 docx → 与 PDF 解析产物同结构的 dict。"""
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        xml = z.read("word/document.xml")
        meta_extra = {"has_header": any(n.startswith("word/header") for n in names),
                      "has_footer": any(n.startswith("word/footer") for n in names)}
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    if body is None:
        raise ValueError("docx 里没有 body：%s" % path.name)

    pages = []
    cur = []
    page_no = 1
    para_index = 0
    for text, brk in _blocks(body):
        if brk and cur:                      # 遇到分页标记 → 收上一页
            pages.append(_mk_page(page_no, cur)); page_no += 1; cur = []
        if not text:
            continue
        para_index += 1
        cur.append({"text": text, "para_index": para_index})
    if cur:
        pages.append(_mk_page(page_no, cur))
    if not pages:
        pages = [_mk_page(1, [])]

    return {
        "file": path.name,
        "pages": pages,
        "meta": {
            "kind": "docx",
            "source": {"size": path.stat().st_size, "mtime": int(path.stat().st_mtime)},
            "params": PARAMS_FINGERPRINT,
            "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **meta_extra,
        },
    }


def _cells_of(text):
    """把一行拆成 cells（与 PDF 行同构，供 `extract/table.py` 分列用）。

    docx 的表格单元在 `_blocks()` 里已经用制表符连好了，所以**不需要坐标** ——
    用"字符位置"当伪 x 即可：`与会单位\\t与会人员` → 左列 [0,4)，右列 [5,9)。
    ⛔ 没有制表符的行（正文段落）只有 1 个 cell —— `table.py` 要求表头行至少 2 个 cell，
       所以正文段落**天然不会被误当表格**（这是免费的护栏，别去掉）。
    """
    if "\t" not in text:
        return [{"t": text, "x0": 0, "x1": len(text)}]
    out, pos = [], 0
    for part in text.split("\t"):
        out.append({"t": part, "x0": pos, "x1": pos + len(part)})
        pos += len(part) + 1
    return out


def _mk_page(page_no, paras):
    texts = [p["text"] for p in paras]
    return {
        "page": page_no,
        "source": "docx",
        "chars": sum(len(t) for t in texts),
        "paras": texts,
        # `y` 用**行号**当伪坐标：docx 没有版面坐标，但表格分列要靠"同一行/后续行"的顺序，
        # 行号足够表达顺序，也能让 `max_row_drop` 起到"别越出表格"的作用。
        "rows": [{"text": t, "bbox": [0, 0, 0, 0], "score": None,
                  "y": i, "h": 1, "cells": _cells_of(t)}
                 for i, t in enumerate(texts)],
        "para_indexes": [p["para_index"] for p in paras],
    }


def count_pages(path):
    """给质检门用：只数页数，不建完整产物（快）。"""
    try:
        return len(read_docx(path)["pages"])
    except Exception:
        return 1
