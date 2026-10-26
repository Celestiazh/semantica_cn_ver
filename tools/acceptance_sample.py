# -*- coding: utf-8 -*-
"""验收抽样：从归并后的图里分层随机抽 N 条边，生成人工核对清单。

每条 = 结论 + 完整出处（绝对路径 + 页码 + 原文）。验收人逐条：
打开文件翻到页码 → 对照原文是否支持该结论 → 在「核对」列填 ✓/✗。
这是图谱质量验收的核心动作：不逐条全查（500 条不现实），靠分层随机抽样。

用法：
    python tools/acceptance_sample.py            # 默认抽 24 条（每类关系至少 3 条）
    python tools/acceptance_sample.py 40         # 抽 40 条
产出：var/eval/acceptance_check.csv + 打印汇总
"""
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg.config import Config                                   # noqa: E402

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
OUT_CSV = ROOT / "var" / "eval" / "acceptance_check.csv"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    n_target = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 24
    seed = 20260929                       # 固定种子：验收清单可复现（换 seed = 换一批）
    g = json_load(GRAPH)

    # 出处文件定位（与消解同一套：input_dir + corpus_search_dirs）
    from kg import resolve as res_mod
    cfg = Config(ROOT)
    paths = res_mod._locate_files(cfg)

    # 分层：按关系类型分组，每组随机抽，保证每类至少 min(3, 组内数) 条
    by_type = defaultdict(list)
    for r in g["relationships"]:
        ev = (r.get("properties") or {}).get("evidence") or []
        if ev:
            by_type[r.get("type", "?")].append((r, ev))

    rng = random.Random(seed)
    picked = []
    types = sorted(by_type)
    per = {t: max(3, round(n_target * len(by_type[t]) / len(g["relationships"]))) for t in types}
    for t in types:
        picked += rng.sample(by_type[t], min(per[t], len(by_type[t])))
    picked = picked[:n_target]

    rows = []
    for r, ev in picked:
        v = ev[0]
        f = v.get("file", "")
        rows.append({
            "结论（待核对）": "%s ——%s--> %s" % (r.get("source"), r.get("type"), r.get("target")),
            "打开文件": paths.get(f, "（未定位：%s）" % f),
            "页码": v.get("page"),
            "原文节选": (v.get("quote") or "")[:80],
            "核对结果": "",       # 验收人填：✓ / ✗ / 存疑
            "备注": "",
        })

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fcsv:
        w = csv.DictWriter(fcsv, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 汇总：验收基线数字
    total = len(g["relationships"])
    with_ev = sum(1 for r in g["relationships"] if (r.get("properties") or {}).get("evidence"))
    print("验收抽样清单：%s" % OUT_CSV)
    print("  抽样 %d 条（分层按关系类型，seed=%d 可复现）" % (len(rows), seed))
    print("  各类条数：", {t: len(v) for t, v in by_type.items()})
    print("  溯源覆盖率：%d / %d = %.1f%%" % (with_ev, total, 100.0 * with_ev / total))
    located = sum(1 for x in rows if not x["打开文件"].startswith("（未定位"))
    print("  抽样出处可定位：%d / %d" % (located, len(rows)))
    print("  通过标准建议：抽查 ✗（结论与原文不符）≤ 5%% 为通过；✗ 出现在哪类，就修哪条链路")


def json_load(p):
    import json
    return json.loads(Path(p).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
