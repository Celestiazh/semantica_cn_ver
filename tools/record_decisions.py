# -*- coding: utf-8 -*-
"""record_decisions.py —— 把历史人工拍板补记为正式决策链（DecisionRecorder）。

数据源（既有留痕，幂等：已记录的 scenario 跳过）：
    config/roster_extra.yaml          单位合并决定
    config/abbrev_decided_extra.yaml  简称决定（合并/驳回/部门保留）
    var/resolve/project_merge_log.csv 项目归并留痕

用法：
    .venv\\Scripts\\python.exe tools\\record_decisions.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sem2_libs"))

import yaml  # noqa: E402


def _load(p):
    f = ROOT / p
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.is_file() else {}


def main():
    from kg.serve.intelligence import record_decision, list_decisions
    seen = {r.get("scenario") for r in list_decisions(limit=100000)}
    n = 0

    # ① 单位合并（roster_extra 的 aliases 即拍板产物）
    roster = _load("config/roster_extra.yaml")
    for u in roster.get("units") or []:
        name = u.get("name", "")
        for alias in u.get("aliases") or []:
            sc = "单位合并：%s → %s" % (alias, name)
            if sc in seen:
                continue
            record_decision("数据治理/消解", sc,
                            "人工确认两家写法为同一单位，证据见消解确认清单",
                            "合并入名册正名", "人工确认", entities=[alias, name])
            seen.add(sc)
            n += 1

    # ② 简称决定
    ab = _load("config/abbrev_decided_extra.yaml")
    for d in ab.get("abbrevs") or []:
        short = d.get("简称", "")
        dec = d.get("决定", "")
        sc = "简称确认：%s（%s）" % (short, dec)
        if sc in seen:
            continue
        record_decision("数据治理/简称", sc,
                        "简称提名人工拍板，依据见 review_alias 清单与提名证据",
                        dec, "人工确认", entities=[short, d.get("提名全称", "")])
        seen.add(sc)
        n += 1

    # ③ 项目归并
    import csv
    pm = ROOT / "var" / "resolve" / "project_merge_log.csv"
    if pm.is_file():
        with open(pm, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                sc = "项目归并：%s → %s" % (row.get("变体节点", ""), row.get("并入正名", ""))
                if sc in seen:
                    continue
                record_decision("数据治理/项目归一", sc,
                                "日期前缀剥离后同名合并（宁缺勿滥，人工/规则确认）",
                                "归并", "规则+人工确认",
                                entities=[row.get("变体节点", ""), row.get("并入正名", "")])
                seen.add(sc)
                n += 1

    print("补记决策 %d 条 | 决策链总数 %d" % (n, len(list_decisions(limit=100000))))


if __name__ == "__main__":
    main()
