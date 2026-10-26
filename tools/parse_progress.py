# -*- coding: utf-8 -*-
"""看全量解析跑到哪了 + 还剩多久。**数产物文件**，不看日志（日志是块缓冲，会滞后）。

    .venv\\Scripts\\python.exe tools\\parse_progress.py

判据：`var/parse/<文件名>.text.json` 存在即视为该文件已解析完（解析器逐份写盘）。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

PARSE = ROOT / "var" / "parse"
SCAN = PARSE / "scan.json"


def main():
    if not SCAN.is_file():
        print("还没有 scan.json：先跑一次 `run.py`（扫描）")
        return 2
    scan = json.loads(SCAN.read_text(encoding="utf-8"))
    files = scan.get("files") or []
    total = len(files)

    done, pending_ocr, done_names = [], [], []
    for f in files:
        stem = Path(f["file"]).stem
        if (PARSE / (stem + ".text.json")).is_file():
            done.append(f)
            done_names.append(f["file"])
        else:
            pending_ocr.append(f)

    # 进度百分比（PDF 与 DOCX 加权：docx 几乎瞬时，用页数近似工作量）
    tot_pages = sum(x.get("n_pages", 0) for x in files)
    done_pages = sum(x.get("n_pages", 0) for x in done)
    tot_ocr = sum(len(x.get("ocr_pages") or []) for x in files)
    done_ocr = sum(len(x.get("ocr_pages") or []) for x in done)

    print("=== 解析进度 ===")
    print("  文件：%d / %d（%.0f%%）" % (len(done), total, 100.0 * len(done) / max(total, 1)))
    print("  页数：%d / %d（%.0f%%）" % (done_pages, tot_pages, 100.0 * done_pages / max(tot_pages, 1)))
    print("  OCR 页：%d / %d（%.0f%%）  ← 决定总耗时的主要项" %
          (done_ocr, tot_ocr, 100.0 * done_ocr / max(tot_ocr, 1)))

    # 用产物文件的最后修改时间估速率
    mtimes = []
    for d in PARSE.glob("*.text.json"):
        try:
            mtimes.append(d.stat().st_mtime)
        except OSError:
            pass
    if mtimes:
        newest, oldest = max(mtimes), min(mtimes)
        span = newest - oldest
        print("  最近一次写盘：%s（%d 秒前）"
              % (time.strftime("%H:%M:%S", time.localtime(newest)), int(time.time() - newest)))

    remain_files = len(pending_ocr)
    if done_ocr and len(done) and remain_files:
        # 用"每 OCR 页耗时"外推（实测约 3.7s/页 + 少量渲染开销）
        elapsed_since_start = None
        print()
        print("=== 还剩多久（按 3.7s/OCR 页 估）===")
        for x in pending_ocr[:5]:
            n = len(x.get("ocr_pages") or [])
            print("   待办：%-50s %s 页 OCR" % (x["file"][:50], n))
        if len(pending_ocr) > 5:
            print("   …… 还有 %d 个文件" % (len(pending_ocr) - 5))
        left_ocr = tot_ocr - done_ocr
        print("   剩余 OCR 页 %d → 约 %.0f 分钟" % (left_ocr, left_ocr * 3.7 / 60))

    # 判断是不是已经跑完了 / 卡住了
    if not pending_ocr:
        print()
        print("  ✅ 全部解析完成，可以跑 `run.py --extract`")
    elif mtimes and time.time() - max(mtimes) > 600:
        print()
        print("  ⚠️ 超过 10 分钟没有新产物 → 可能卡住或已中断，看 %TEMP%\\kg_fullparse.log 末尾")
    return 0


if __name__ == "__main__":
    sys.exit(main())
