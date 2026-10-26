# -*- coding: utf-8 -*-
"""分级取文本：PDF → text.json / txt（含页码与来源）

    逐页 ──► 质检门（这一页有没有文字层？）
              ├─ 有 ──► 取文字层（blocks → 行框）
              └─ 无 ──► 渲染位图 → RapidOCR → 行框
             ──► clean.build_pages（行重建 / 噪声行 / 页眉页脚）
             ──► 写缓存

产物（两种，都要）：
    var/parse/<stem>.text.json   {"file", "pages":[{page,source,chars,paras,rows}], "meta":{...}}
    var/parse/<stem>.txt         "===== 第 N 页（来源：xxx）=====" + 段落   ← 与 POC 产物格式一致

⭐ **缓存**：`<stem>.text.json` 存在且源文件 size/mtime 与参数指纹都没变 → 直接复用，不重跑 OCR。
   这是"全量重建图不重抽"的基础（重建只耗 CPU，模型只在新文件上花钱）。

⛔ 铁律：**不在这里做任何"理解"或改写** —— 一旦改写，页码 + 坐标对不上，溯源链就断了。
   清洗只做确定性的事（去水印行、拼行、分页眉页脚）。
"""
import json
import time
from pathlib import Path

from . import clean

_OCR_ENGINE = None


def get_ocr():
    """RapidOCR 引擎单例（初始化 0.4~0.9s，不要每页都建）。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _bbox(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def lines_from_textlayer(page):
    """有文字层 → 直接读，**优先走这条**（更干净、更快、没有识别错误）。

    ⭐ 用 `get_text("dict")` 取**行级**坐标（2026-09-28 改）。
    ⛔ 原来用 `get_text("blocks")`：一个块里所有行都拿到**同一个块坐标** →
       行级 x 信息等于没有。而江苏标准表的「单位 | 人员」两列**必须靠 x 才能分开**
       （实测：单位列 x≈204~257，人员列 x≈396~438，同一 y）。丢掉 x 就等于把表格
       压成一串无分隔文字（`南京城建居松东南监理丁国华…`）。
    ⛔ 这一改动会改变缓存产物 → 已同步提升 `_params_fingerprint` 的 `_v`（否则读到旧缓存却不知道）。
    """
    out = []
    for blk in (page.get_text("dict") or {}).get("blocks") or []:
        if blk.get("type") != 0:             # 只要文本块，跳过图片块
            continue
        for ln in blk.get("lines") or []:
            t = "".join(sp.get("text") or "" for sp in (ln.get("spans") or []))
            if not t.strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            out.append({"text": t.strip(), "bbox": [int(x0), int(y0), int(x1), int(y1)],
                        "score": None})
    return out


def lines_from_ocr(page, dpi, engine):
    """无文字层 → 渲染位图走 OCR。返回行框（带 score，便于事后看识别质量）。"""
    import numpy as np
    pix = page.get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    res, _elapse = engine(arr)
    out = []
    for it in (res or []):
        t = str(it[1]).strip()
        if t:
            out.append({"text": t, "bbox": _bbox(it[0]), "score": round(float(it[2]), 4)})
    return out


def _params_fingerprint(p):
    """参数指纹：解析参数变了 → 缓存作废（否则改了 dpi/阈值却读到旧结果）。

    `_v` 是**解析器版本号**：读取逻辑本身变了（不是参数变了）时手动 +1，
    以显式作废缓存。⛔ 不要为了"省一次重跑"而不升它 —— 那会读到旧结构的缓存却不自知。
    v2（2026-09-28）：文字层改用 `get_text("dict")` 取行级坐标，并在视觉行里保留 `cells`（x 区间）。
    """
    keys = ["dpi", "text_layer_min_chars", "drop_repeated_lines", "header_footer_ratio",
            "row_tol_min", "row_tol_factor", "para_gap_min", "para_gap_factor",
            "repeat_key_min_len", "repeat_key_max_len", "drop_patterns"]
    d = {k: p.get(k) for k in keys}
    d["_v"] = 2
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def source_signature(pdf):
    st = Path(pdf).stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def extract_pdf(pdf, pcfg):
    """解析单个 PDF。返回 (doc, stats)。doc 结构与 POC 的 .text.json 对齐。"""
    import pymupdf

    dpi = pcfg.get("dpi", 220)
    min_chars = pcfg.get("text_layer_min_chars", 30)

    raw_pages = []
    t0 = time.time()
    ocr_pages = 0
    ocr_seconds = 0.0

    doc = pymupdf.open(str(pdf))
    try:
        for i, page in enumerate(doc):
            text = (page.get_text() or "").strip()
            if len(text) >= min_chars:
                rows = lines_from_textlayer(page)
                source = "textlayer"
            else:
                t1 = time.time()
                rows = lines_from_ocr(page, dpi, get_ocr())
                ocr_seconds += time.time() - t1
                source = "ocr"
                ocr_pages += 1
            raw_pages.append({"page": i + 1, "source": source, "rows": rows})
    finally:
        doc.close()

    pages, cstats = clean.build_pages(raw_pages, pcfg.get("drop_patterns"), pcfg)

    doc_out = {
        "file": Path(pdf).name,
        "pages": pages,
        "meta": {
            "kind": "pdf",
            "source": source_signature(pdf),
            "params": _params_fingerprint(pcfg),
            "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    stats = {
        "pages": len(pages),
        "textlayer": sum(1 for x in pages if x["source"] == "textlayer"),
        "ocr": ocr_pages,
        "chars": sum(x["chars"] for x in pages),
        "ocr_seconds": round(ocr_seconds, 1),
        "seconds": round(time.time() - t0, 1),
        **cstats,
    }
    return doc_out, stats


def extract_docx(docx, pcfg):
    """解析单个 .docx —— 直接读 XML，**不渲染、不 OCR、不洗页眉页脚**。

    产物结构与 PDF 一致，所以抽取层完全不用改。
    ⚠️ 页码含义：优先用 Word 的 `w:lastRenderedPageBreak` 真分页标记；
       没有分页标记时整篇算 1 页（evidence 里另带 para_index 定位）。
    """
    from .docx_reader import read_docx

    t0 = time.time()
    doc = read_docx(docx)
    # 统一缓存指纹与源签名，让 PDF / DOCX 共用同一套缓存判定
    doc["meta"]["params"] = _params_fingerprint(pcfg)
    doc["meta"]["source"] = source_signature(docx)
    pages = doc["pages"]
    stats = {
        "pages": len(pages),
        "textlayer": 0,
        "ocr": 0,
        "docx_pages": len(pages),
        "chars": sum(x["chars"] for x in pages),
        "ocr_seconds": 0.0,
        "seconds": round(time.time() - t0, 1),
        "rows_raw": sum(len(x.get("paras") or []) for x in pages),
        "noise_dropped": 0,
        "header_dropped": 0,
        "rows_kept": sum(len(x.get("paras") or []) for x in pages),
    }
    return doc, stats


def extract_one(path, pcfg):
    """按扩展名分派：.docx 走 XML，其余（.pdf）走分级取文本。"""
    if Path(path).suffix.lower() == ".docx":
        return extract_docx(path, pcfg)
    return extract_pdf(path, pcfg)


def pages_to_txt(doc):
    """与 POC 产物格式一致的可读文本（规则抽取器直接吃这个）。"""
    body = []
    for p in doc["pages"]:
        body.append("\n\n===== 第 %s 页（来源：%s）=====\n" % (p["page"], p["source"]))
        body.append("\n".join(p["paras"]))
    return "".join(body)


def is_cache_valid(text_json, pdf, pcfg):
    """缓存是否可用：源文件 size/mtime 没变 **且** 参数指纹一致。

    ⛔ 指纹必须**按文件类型取**：docx 与 PDF 的读取器不同、参数集也不同。
    踩过（2026-09-28）：这里只比 PDF 指纹，而 docx 存的是自己的指纹 → 永远不相等 →
    **68 个 docx 每次全量重解析，缓存形同虚设**（因为只需 0.0s，一直没被发现）。
    """
    if not Path(text_json).is_file():
        return False
    try:
        meta = json.loads(Path(text_json).read_text(encoding="utf-8")).get("meta") or {}
    except Exception:
        return False
    if Path(pdf).suffix.lower() == ".docx":
        from .docx_reader import PARAMS_FINGERPRINT
        want = PARAMS_FINGERPRINT
    else:
        want = _params_fingerprint(pcfg)
    return (meta.get("source") == source_signature(pdf)
            and meta.get("params") == want)


def parse_dir(input_dir, cfg, force=False, limit=None, on_file=None):
    """解析整个目录。返回 (rows, total)。"""
    from .scanner import scan_documents       # 复用质检门（PDF + DOCX，先给体检表）

    input_dir = Path(input_dir)
    work = cfg.work_dir / "parse"
    work.mkdir(parents=True, exist_ok=True)

    infos, errors, ignored = scan_documents(
        input_dir, text_layer_min_chars=cfg.get("parse.text_layer_min_chars", 30))
    if limit:
        infos = infos[:limit]

    pcfg = cfg.settings.get("parse") or {}
    rows, failures = [], list(errors)
    reuse = 0
    t_all = time.time()

    for idx, info in enumerate(infos, 1):
        pdf = info.path
        stem = pdf.stem
        tjson = work / (stem + ".text.json")
        ttxt = work / (stem + ".txt")

        if not force and is_cache_valid(tjson, pdf, pcfg):
            try:
                doc = json.loads(tjson.read_text(encoding="utf-8"))
                row = {
                    "file": doc["file"], "reused": True,
                    "pages": len(doc["pages"]),
                    "textlayer": sum(1 for x in doc["pages"] if x["source"] == "textlayer"),
                    "ocr": sum(1 for x in doc["pages"] if x["source"] == "ocr"),
                    "chars": sum(x["chars"] for x in doc["pages"]),
                    "ocr_seconds": 0.0, "seconds": 0.0,
                    "noise_dropped": 0, "header_dropped": 0,
                }
                rows.append(row)
                reuse += 1
                if on_file:
                    on_file(idx, len(infos), row)
                if not ttxt.is_file():
                    ttxt.write_text(pages_to_txt(doc), encoding="utf-8")
                continue
            except Exception as e:
                failures.append((pdf.name, "缓存读取失败，改为重跑：%s" % e))

        try:
            doc, st = extract_one(pdf, pcfg)
        except Exception as e:
            failures.append((pdf.name, "%s: %s" % (type(e).__name__, e)))
            if on_file:
                on_file(idx, len(infos), {"file": pdf.name, "error": str(e)[:80]})
            continue

        tjson.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        ttxt.write_text(pages_to_txt(doc), encoding="utf-8")

        row = {"file": doc["file"], "reused": False, **st}
        rows.append(row)
        if on_file:
            on_file(idx, len(infos), row)

    total = {
        "n_files": len(infos),
        "n_parsed": len(rows),
        "n_reused": reuse,
        "pages": sum(r.get("pages", 0) for r in rows),
        "textlayer": sum(r.get("textlayer", 0) for r in rows),
        "ocr": sum(r.get("ocr", 0) for r in rows),
        "docx": sum(1 for i in infos if i.kind == "docx"),
        "chars": sum(r.get("chars", 0) for r in rows),
        "noise_dropped": sum(r.get("noise_dropped", 0) for r in rows),
        "header_dropped": sum(r.get("header_dropped", 0) for r in rows),
        "ocr_seconds": round(sum(r.get("ocr_seconds", 0) for r in rows), 1),
        "seconds": round(time.time() - t_all, 1),
        "ignored_ext": ignored,          # ⭐ 被忽略的扩展名统计（别再静默丢文件）
        "failures": failures,
        "work_dir": str(work),
    }
    return rows, total
