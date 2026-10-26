# -*- coding: utf-8 -*-
"""金标问答回归：把 config/qa_golden.yaml 的用例逐个喂给 ask_graph.py，
按断言判 PASS/FAIL，产出报告 var/eval/qa_regress_report.md。

断言维度：
  ① 匹配行为：expect_match=true 必须匹配实体并回答；false 必须婉拒（图谱没有的）
  ② 出处硬校验：回答必须带「【出处校验】✓」
  ③ must_contain：关键词（或关系）
  ④ must_source：出处文件名片段（或关系）
  ⑤ forbidden：禁止出现的字串（编造年份等）

用法：.venv\\Scripts\\python.exe tools\\qa_regress.py
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GOLD = ROOT / "config" / "qa_golden.yaml"
OUT = ROOT / "var" / "eval" / "qa_regress_report.md"
ASK = ROOT / "tools" / "ask_graph.py"
PY = sys.executable


def run_case(case):
    """跑一个用例，返回 (stdout 全文, 退出码)。"""
    cmd = [PY, str(ASK)]
    if case.get("days"):
        cmd += ["--days", str(case["days"])]
    if case.get("since"):
        cmd += ["--since", str(case["since"])]
    cmd.append(case["q"])
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    return r.stdout.decode("utf-8", errors="replace"), r.returncode


def judge(case, out):
    """返回 (PASS/FAIL, 明细行列表)。"""
    checks = []
    q = case["q"]
    matched = "【匹配实体】" in out
    declined = ("未匹配到" in out) or ("信息不足" in out)

    # ① 匹配行为（tolerate_decline=true：简称别名未沉淀等场景，部分婉拒可接受）
    if case.get("expect_match", True):
        if case.get("tolerate_decline"):
            checks.append(("匹配到实体", matched, "婉拒可容忍（别名未沉淀期）"))
        else:
            checks.append(("匹配到实体并回答", matched and not declined,
                           "匹配=%s 婉拒=%s" % (matched, declined)))
    else:
        checks.append(("图谱没有→婉拒", declined,
                       "婉拒=%s（不许编造）" % declined))

    # ② 出处硬校验（仅正向用例）
    if case.get("expect_match", True):
        ok = "【出处校验】✓" in out
        checks.append(("出处校验 ✓", ok, "无引用/编造都会失败"))

    # ③ 关键词
    ans = out
    for kw in case.get("must_contain") or []:
        checks.append(("含关键词「%s」" % kw, kw in ans, ""))

    # ④ 出处文件片段（任一命中即过 —— 40 条事实是采样，不应要求全命中）
    srcs = case.get("must_source") or []
    if srcs:
        checks.append(("出处含任一指定片段", any(src in ans for src in srcs),
                       "共 %d 个候选片段" % len(srcs)))

    # ⑤ 禁止字串
    for f in case.get("forbidden") or []:
        checks.append(("禁止出现「%s」" % f, f not in ans, ""))

    fail = [c for c in checks if not c[1]]
    return (not fail), checks, fail


def main():
    cases = (yaml.safe_load(GOLD.read_text(encoding="utf-8")) or {}).get("cases") or []
    if not cases:
        sys.exit("金标集为空：" + str(GOLD))
    npass = nfail = 0
    lines = ["# 问答金标回归报告", "",
             "- 日期：" + date.today().isoformat(),
             "- 用例：%d 个（金标集：config/qa_golden.yaml）" % len(cases), ""]
    for i, case in enumerate(cases, 1):
        q = case["q"]
        print("[%d/%d] %s" % (i, len(cases), q))
        out, rc = run_case(case)
        ok, checks, fail = judge(case, out)
        if ok:
            npass += 1
        else:
            nfail += 1
        mark = "PASS" if ok else "FAIL"
        lines.append("## %s ｜ %s" % (mark, q))
        lines.append("")
        for name, oki, detail in checks:
            lines.append("- %s %s %s" % ("✓" if oki else "⛔", name, detail))
        if fail:
            lines.append("- ⛔ 未通过：" + "；".join(f[0] for f in fail))
        lines.append("")
        print("  ->", mark, ("（%d 项检查）" % len(checks)))

    lines.append("---")
    lines.append("**结论：%d PASS / %d FAIL**" % (npass, nfail))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("=" * 40)
    print("结论：%d PASS / %d FAIL → %s" % (npass, nfail, OUT))
    sys.exit(0 if nfail == 0 else 1)


if __name__ == "__main__":
    main()
