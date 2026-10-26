# -*- coding: utf-8 -*-
"""语料体检：把"没抽到"和"原文本来就没有"分开，逐类列出文件清单。

    .venv\\Scripts\\python.exe tools\\corpus_quality.py

为什么要它：模板一多，"某份文件 0 单位 0 人"到底是**我们的规则不吃**还是**文件本身没内容**，
必须能一眼分开。否则会把数据问题记成规则问题（2026-09-28 已踩过两次）。
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass
from kg.logutil import write_report                      # noqa: E402

PER_FILE = ROOT / "var" / "extract" / "per_file"
PARSE = ROOT / "var" / "parse"


def main():
    if not PER_FILE.is_dir():
        print("还没有抽取产物：先跑 run.py --extract")
        return 2
    rows = []
    for f in sorted(PER_FILE.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        st = (d.get("stats") or {}).get("meeting") or {}
        pend = (d.get("stats") or {}).get("pending") or {}
        # ⛔ 注意：per_file 里的文件名形如 `xxx.text.json`，`Path(...).stem` 只会去掉 `.json`
        #    得到 `xxx.text` —— 必须显式去掉 ".text.json" 后缀（2026-09-28 踩过：
        #    拼错路径导致"所有文件字符数=0"，把 170 份全判成空壳）
        stem = f.name[:-len(".text.json")] if f.name.endswith(".text.json") else f.name
        pj = PARSE / (stem + ".text.json")
        chars = 0
        kind = "?"
        if pj.is_file():
            pd = json.loads(pj.read_text(encoding="utf-8"))
            chars = sum(p.get("chars", 0) for p in pd.get("pages") or [])
            kind = (pd.get("meta") or {}).get("kind", "?")
        rows.append({
            "file": d.get("file") or f.name,
            "kind": kind, "chars": chars,
            "pages": len((d.get("pages") or [])) if isinstance(d.get("pages"), list) else None,
            "units": st.get("units", 0), "persons": st.get("persons", 0),
            "relations": len(d.get("relationships") or []),
            "items": pend.get("items", 0),
            "warnings": st.get("warnings") or [],
        })

    empty = [r for r in rows if r["chars"] < 200]
    no_attend = [r for r in rows if any("无参会名单" in w for w in r["warnings"])]
    no_seg = [r for r in rows if any("未找到参会段" in w for w in r["warnings"])]
    zero_unit = [r for r in rows if r["units"] == 0 and r["chars"] >= 200]

    lines = ["# 语料体检报告", "",
             "- 文件数：%d" % len(rows),
             "- 总字符：%s" % format(sum(r["chars"] for r in rows), ","),
             "- 空壳（<200 字）：**%d 份**" % len(empty),
             "- 原文无参会名单（写「详见签到表」）：**%d 份**" % len(no_attend),
             "- 未找到参会段（可能是新表头写法）：**%d 份**" % len(no_seg),
             "- 字数够但 0 单位（真需要查规则）：**%d 份**" % len(zero_unit),
             ""]

    def table(title, rs, note):
        lines.append("## %s（%d 份）" % (title, len(rs)))
        lines.append("")
        if note:
            lines.append("> %s" % note)
            lines.append("")
        if not rs:
            lines.append("（无）")
            lines.append("")
            return
        lines.append("| 文件 | 类型 | 字数 | 页 | 单位 | 人 | 候选 | 告警 |")
        lines.append("| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |")
        for r in rs[:60]:
            lines.append("| %s | %s | %d | %s | %d | %d | %d | %s |"
                         % (r["file"][:46], r["kind"], r["chars"], r["pages"] or "—",
                            r["units"], r["persons"], r["items"],
                            "；".join(w[:18] for w in r["warnings"]) or "—"))
        if len(rs) > 60:
            lines.append("")
            lines.append("…… 其余 %d 份省略" % (len(rs) - 60))
        lines.append("")

    table("空壳文件", empty, "文件本身几乎没有内容（可能是占位/封面，或正文在图片里）→ **不是规则问题**")
    table("原文无参会名单", no_attend, "原文写「详见签到表」。签到表是手写，**按红线不可用** → 这份文件的人/单位本来就抽不到")
    table("未找到参会段", no_seg, "可能是**新的表头写法** → 值得人工看一份，并把叫法加进 settings.yaml")
    table("字数够但 0 单位", zero_unit, "⭐ **这类才是我们该修的**：有内容却没抽出单位")

    lines += ["", "## 全量一览（按候选条数排序，前 40）", "",
              "| 文件 | 字数 | 单位 | 人 | 关系 | 候选 |", "| :--- | ---: | ---: | ---: | ---: | ---: |"]
    for r in sorted(rows, key=lambda x: -x["items"])[:40]:
        lines.append("| %s | %d | %d | %d | %d | %d |"
                     % (r["file"][:46], r["chars"], r["units"], r["persons"],
                        r["relations"], r["items"]))

    out = write_report(ROOT / "var" / "eval" / "corpus_quality.md", lines)
    print("文件 %d | 空壳 %d | 无名单 %d | 未找到参会段 %d | 字数够但0单位 %d"
          % (len(rows), len(empty), len(no_attend), len(no_seg), len(zero_unit)))
    print("[report] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
