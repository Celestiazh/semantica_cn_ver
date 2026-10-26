# -*- coding: utf-8 -*-
"""回放 pending_filled.json：把历史已填字段重新灌回候选（--extract 重建后用）。

背景：`run.py --extract` 会重建 pending_candidates.json（字段清空）；
已填字段全部留痕在 var/extract/pending_filled.json —— 本脚本按批回放，
filled_by 原样保留（留痕不丢）。quote 逐字校验天然通过（同一缓存重建）。

用法：python tools/replay_filled.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg.config import Config                                    # noqa: E402
from kg.serve import tools as S                                 # noqa: E402

TRAIL = ROOT / "var" / "extract" / "pending_filled.json"


def main():
    if not TRAIL.is_file():
        sys.exit("没有留痕文件：%s" % TRAIL)
    hist = json.loads(TRAIL.read_text(encoding="utf-8"))
    cfg = Config(ROOT)
    total_acc = total_rej = 0
    for batch in hist:
        src = batch.get("source") or "replay"
        items = batch.get("items") or []
        if not items:
            continue
        res = S.submit_extracted_fields(cfg, {"schema": batch.get("schema"),
                                              "items": items, "source": src},
                                        source=src + "(replay)")
        total_acc += res["accepted"]
        total_rej += res["rejected"]
        print("回放批次 %s：接受 %d ｜ 拒收 %d" % (src[:40], res["accepted"], res["rejected"]))
        for reason in (res.get("reasons") or [])[:5]:
            print("   -", reason)
    st = S.pending_status(cfg)
    print("合计：接受 %d ｜ 拒收 %d" % (total_acc, total_rej))
    print("状态：%s" % json.dumps(st, ensure_ascii=False)[:260])


if __name__ == "__main__":
    main()
