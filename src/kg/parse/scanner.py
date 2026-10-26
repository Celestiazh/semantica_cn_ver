# -*- coding: utf-8 -*-
"""扫描 + 质检门：列出目录里的 PDF 与 DOCX，判断哪些 PDF 页需要 OCR。

⭐ 2026-09-28 扩展：**同时支持 .docx**（Word 版纪要，直接读 XML，不需要 OCR）。
   之前只 glob `*.pdf` —— 实测把 154 个文件里的 **68 个 docx 静默丢掉**（44% 语料）。
   ⛔ 教训：扫描器要**显式报告被忽略的扩展名**，别让"没扫到"看起来像"没有"。

产出仍然很便宜：不渲染位图、不跑 OCR。
"""
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = (".pdf", ".docx")


@dataclass
class PageInfo:
    page: int
    source: str          # "textlayer" | "ocr" | "docx"
    chars: int


@dataclass
class PdfInfo:
    path: Path
    pages: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def kind(self) -> str:
        return self.path.suffix.lower().lstrip(".")

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def n_ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "ocr")

    @property
    def n_text_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "textlayer")

    @property
    def n_docx_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "docx")


def _scan_docx(path):
    from .docx_reader import read_docx
    doc = read_docx(path)
    return [PageInfo(page=p["page"], source="docx", chars=p["chars"]) for p in doc["pages"]]


def scan_documents(input_dir, text_layer_min_chars=30, recursive=True):
    """列目录下所有 PDF / DOCX，逐页判断来源。

    返回 (list[PdfInfo], errors, ignored)——`ignored` 是**被忽略的扩展名统计**（别再静默丢文件）。
    """
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise FileNotFoundError("输入目录不存在：%s" % input_dir)

    files = [p for p in input_dir.glob("**/*" if recursive else "*")
             if p.is_file() and p.suffix.lower() in SUPPORTED]
    files.sort()

    ignored = {}
    for p in input_dir.glob("**/*" if recursive else "*"):
        if p.is_file() and p.suffix.lower() not in SUPPORTED:
            ignored[p.suffix.lower() or "(无扩展名)"] = ignored.get(p.suffix.lower() or "(无扩展名)", 0) + 1

    infos, errors = [], []
    for path in files:
        try:
            if path.suffix.lower() == ".docx":
                infos.append(PdfInfo(path=path, pages=_scan_docx(path)))
                continue
            import pymupdf
            doc = pymupdf.open(str(path))
            try:
                pages = []
                for i, pg in enumerate(doc):
                    n = len((pg.get_text() or "").strip())
                    pages.append(PageInfo(page=i + 1,
                                          source="textlayer" if n >= text_layer_min_chars else "ocr",
                                          chars=n))
                infos.append(PdfInfo(path=path, pages=pages))
            finally:
                doc.close()
        except Exception as e:                                   # noqa: BLE001
            errors.append((path.name, "%s: %s" % (type(e).__name__, e)))
    return infos, errors, ignored


def scan_pdfs(input_dir, text_layer_min_chars: int = 30, recursive: bool = True):
    """兼容旧调用：只返回 (infos, errors)。"""
    infos, errors, _ignored = scan_documents(input_dir, text_layer_min_chars, recursive)
    return infos, errors


def summarize(infos, ignored=None):
    total_pages = sum(i.n_pages for i in infos)
    ocr_pages = sum(i.n_ocr_pages for i in infos)
    docx_pages = sum(i.n_docx_pages for i in infos)
    text_pages = sum(i.n_text_pages for i in infos)
    return {
        "n_files": len(infos),
        "n_pdf": sum(1 for i in infos if i.kind == "pdf"),
        "n_docx": sum(1 for i in infos if i.kind == "docx"),
        "n_pages": total_pages,
        "n_ocr_pages": ocr_pages,
        "n_text_pages": text_pages,
        "n_docx_pages": docx_pages,
        "ocr_ratio": (ocr_pages / total_pages) if total_pages else 0.0,
        "ignored_ext": ignored or {},
        "files": [
            {"file": i.name, "kind": i.kind, "n_pages": i.n_pages,
             "ocr_pages": [p.page for p in i.pages if p.source == "ocr"]}
            for i in infos
        ],
    }
