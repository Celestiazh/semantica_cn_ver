# -*- coding: utf-8 -*-
"""reasoning_report.py —— Reasoning Engine 落地（规则推理，零模型）。

从图谱推导两类结论：
    ① 逾期风险：事项有明确期限（deadline_iso 可解析）、closed_loop=false、已过期
    ② 单位风险画像：按责任单位统计 未闭环事项 / 逾期事项，排序
产出：var/eval/reasoning_report.md + 控制台摘要

用法：
    .venv\\Scripts\\python.exe tools\\reasoning_report.py
"""
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(errors="replace")

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
OUT = ROOT / "var" / "eval" / "reasoning_report.md"
TODAY = date.today()


def _parse_deadline(s):
    """期限字符串 → date（尽力解析；解析不了返回 None）。"""
    if not s:
        return None
    m = re.search(r"(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})", str(s))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[月/.-](\d{1,2})", str(s))          # 无年份：补当前年
    if m:
        try:
            return date(TODAY.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def main():
    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    ents = {e["id"]: e for e in g["entities"]}
    id2name = lambda i: ents.get(i, {}).get("name", i)            # noqa: E731

    overdue = []          # (事项, 期限, 责任单位)
    unit_open = Counter()
    unit_overdue = Counter()

    for r in g["relationships"]:
        if r.get("type") != "属于项目":
            continue
        mid = r.get("source")
        node = ents.get(mid)
        if not node or node.get("type") != "MATTER":
            continue
        p = node.get("properties") or {}
        closed = p.get("closed_loop")
        closed = closed in (True, "true", "True", 1, "1") if closed is not None else False
        unit = p.get("责任单位(原文)") or "（未填）"
        dl = _parse_deadline(p.get("deadline_iso") or p.get("期限"))
        proj = id2name(r.get("target"))
        content = node.get("name", "")

        if not closed:
            unit_open[unit] += 1
            if dl and dl < TODAY:
                overdue.append((content[:50], dl.isoformat(), unit, proj))
                unit_overdue[unit] += 1

    lines = []
    lines.append("# Reasoning 推理报告（规则推导，生成于 %s）\n" % TODAY.isoformat())
    lines.append("> 推理规则：① 事项有可解析期限 + closed_loop=false + 期限早于今日 → **逾期**；"
                 "② 按责任单位聚合未闭环/逾期计数 → **风险画像**。\n")

    lines.append("## 一、逾期事项（%d 条）\n" % len(overdue))
    if overdue:
        lines.append("| 事项 | 期限 | 责任单位 | 项目 |")
        lines.append("| :-- | :-- | :-- | :-- |")
        for c, dl, u, pj in sorted(overdue, key=lambda x: x[1])[:50]:
            lines.append("| %s | %s | %s | %s |" % (c, dl, u, pj))
    else:
        lines.append("（无：所有带期限的事项均已闭环或未到期）")
    lines.append("")

    lines.append("## 二、单位风险画像（未闭环 Top 15）\n")
    lines.append("| 责任单位 | 未闭环 | 其中逾期 |")
    lines.append("| :-- | --: | --: |")
    for u, c in unit_open.most_common(15):
        lines.append("| %s | %d | %d |" % (u, c, unit_overdue.get(u, 0)))
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("逾期事项: %d 条 | 未闭环: %d 条" % (len(overdue), sum(unit_open.values())))
    print("Top 风险单位:", unit_open.most_common(5))
    print("报告: %s" % OUT)


if __name__ == "__main__":
    main()
