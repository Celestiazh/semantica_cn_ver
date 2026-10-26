# -*- coding: utf-8 -*-
"""① 分级取文本

    PDF ──► 质检门（这一页有没有文字层？）
              ├─ 有 ──► 直接取文字层
              └─ 无 ──► RapidOCR（渲染位图 → 识别 → 按坐标重建行与段落）
                        └─► 清洗（噪声行 / 页眉页脚 / 跨页重复行）
    DOCX ──► 直接读 word/document.xml（**不需要 OCR、不需要洗页眉页脚**）
    ──► var/parse/<文件名>.text.json + <文件名>.txt

已实测的依据：
  - 19 份 PDF / 113 页里 **62% 没有文字层** → OCR 是主干路（3.7s/页 @220dpi，纯 CPU）
  - ⭐ **DOCX 必须支持**：2026-09-28 实测 `会议纪要154` 里 154 个文件有 **68 个 docx**，
    旧扫描器只 glob `*.pdf` → **静默丢掉 44% 语料**。现在扫描器会显式报告被忽略的扩展名。

模块：
  scanner.py     质检门：PDF + DOCX，几秒给出"哪些页要 OCR / 多少 docx"的体检表
  clean.py       行重建 / 噪声行 / 页眉页脚（**纯函数，可单测**，只用于 PDF/OCR）
  docx_reader.py 读 .docx（zip + XML；页码取 Word 的真分页标记，没有就整篇算 1 页）
  extract.py     跑 OCR / 读 docx、落缓存

⭐ 缓存：源文件 size/mtime + 参数指纹都没变 → 复用 `<stem>.text.json`，不重跑 OCR。
   这是"全量重建图不重抽"的基础。

⚠️ 已知边界：手写内容（签到表）读不准；印章/水印叠压处会错乱；
   不还原阅读顺序、不懂表格语义（RapidOCR 是轻量 OCR，不是文档理解）。
   DOCX 的页码依赖 Word 的分页标记，没有标记时整篇算 1 页。

POC 来源：kg-poc/scripts-0921/kg_parse1/2/3.py、scripts-0922/kg_parse4.py
"""
from .scanner import scan_documents, scan_pdfs, summarize, PdfInfo, PageInfo
from .extract import (
    extract_pdf, extract_docx, extract_one, parse_dir, pages_to_txt, get_ocr,
    is_cache_valid, source_signature,
)
from .docx_reader import read_docx, count_pages
from . import clean

__all__ = [
    "scan_documents", "scan_pdfs", "summarize", "PdfInfo", "PageInfo",
    "extract_pdf", "extract_docx", "extract_one", "parse_dir", "pages_to_txt", "get_ocr",
    "is_cache_valid", "source_signature", "read_docx", "count_pages", "clean",
]
