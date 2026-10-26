# -*- coding: utf-8 -*-
"""开发用：把当前解析产物与 POC 归档产物逐份对照。

为什么要留着它：2026-09-22 就是靠这个对照揪出一个真 bug ——
清洗顺序写反了（先判重复行、后合并视觉行），导致 OCR 切碎的页眉碎片
（"中国五矿" / "CHONGQING…" / "会议纪要"）被逐个当页眉删掉，正文被误删 1.6%。

用法：
    .venv\\Scripts\\python.exe tools\\check_against_poc.py
    .venv\\Scripts\\python.exe tools\\check_against_poc.py --diff 3   # 再展开差异最大的 3 份

判定口径：**页数与页来源（textlayer/ocr）必须完全一致**；字符数差异应集中在页眉/页脚区。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.logutil import setup_console                                   # noqa: E402

NEW_DIR = ROOT / "var" / "parse"
POC_DIR = ROOT.parent / "kg-poc" / "outputs-0922" / "kg_out19"
TOL = 30          # 单份字符数差异超过这个值就标出来


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", type=int, default=0, help="展开差异最大的 N 份明细")
    ap.add_argument("--poc-dir", default=str(POC_DIR))
    args = ap.parse_args(argv)

    poc_dir = Path(args.poc_dir)
    if not NEW_DIR.is_dir():
        print("还没有解析产物，先跑：run.py --parse")
        return 2
    if not poc_dir.is_dir():
        print("找不到 POC 归档产物：%s" % poc_dir)
        return 2

    print("%-30s %4s %2s %9s %9s %7s" % ("文件", "页", "=", "新字数", "POC字数", "差"))
    print("-" * 74)

    tn = to = 0
    diffs, structural, missing = [], [], []
    for nf in sorted(NEW_DIR.glob("*.text.json")):
        of = poc_dir / nf.name
        if not of.is_file():
            missing.append(nf.name)
            continue
        nd, od = load(nf), load(of)
        stem = nf.name[:-len(".text.json")]
        nc = sum(p["chars"] for p in nd["pages"])
        oc = sum(p["chars"] for p in od["pages"])
        tn += nc
        to += oc
        same = (len(nd["pages"]) == len(od["pages"])
                and [p["source"] for p in nd["pages"]] == [p["source"] for p in od["pages"]])
        if not same:
            structural.append(stem)
        flag = "" if same else "  结构不一致!"
        if abs(nc - oc) > TOL:
            flag += "  字数差>%d" % TOL
        print("%-30s %4d %2s %9d %9d %+7d%s"
              % (stem[33:63] if len(stem) > 33 else stem[:30], len(nd["pages"]),
                 "Y" if same else "N", nc, oc, nc - oc, flag))
        diffs.append((stem, nc - oc))

    print("-" * 74)
    print("合计：新 %d / POC %d（差 %+d，%.2f%%）" % (tn, to, tn - to, (tn - to) / max(to, 1) * 100))
    if structural:
        print("!! 页数或页来源不一致的：%s" % structural)
    else:
        print("页数与页来源：全部一致")
    if missing:
        print("POC 里没有的：%s" % missing)

    if args.diff:
        worst = sorted(diffs, key=lambda x: -abs(x[1]))[:args.diff]
        print("\n差异最大的 %d 份明细：" % len(worst))
        for stem, _ in worst:
            nd, od = load(NEW_DIR / (stem + ".text.json")), load(poc_dir / (stem + ".text.json"))
            print("\n  %s" % stem[:60])
            for np_, op_ in zip(nd["pages"], od["pages"]):
                d = np_["chars"] - op_["chars"]
                if abs(d) > 15:
                    print("    p%-3s %5d / %5d / %+5d" % (np_["page"], np_["chars"], op_["chars"], d))
                    ns = "".join(np_["paras"])
                    os_ = "".join(op_["paras"])
                    for x in [p for p in op_["paras"] if p[:12] and p[:12] not in ns][:2]:
                        print("       [POC 独有] %s" % x[:110])
                    for x in [p for p in np_["paras"] if p[:12] and p[:12] not in os_][:2]:
                        print("       [新 独有] %s" % x[:110])
    return 0


if __name__ == "__main__":
    sys.exit(main())
