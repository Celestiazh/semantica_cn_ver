# -*- coding: utf-8 -*-
"""开发用：抽取质量的评测脚本。

    .venv\\Scripts\\python.exe tools\\eval_extract.py

## 考什么、用什么当答案

| 层 | 指标 | 标准答案 |
| :--- | :--- | :--- |
| 第 1 层 会议要素 | 关系命中 / 漏 / 多 | `config/annotations/1A1E808D_relations.json`（18 条人工标注） |
| 第 2 层 正文事项 | 召回（32 条主条目） | `config/annotations/46121789_pending.json` 的 `兼容对照_32主条目关键词` |
| 第 2 层 责任单位 | 与人工标注的"提出方"对不对得上 | 同上文件的 `章节[].条目[].提出方` |

## ⚠️ 报口径必须带的三件事（不然数字会被误读）

1. **样本量**：关系对照只有 1 份（18 条）；待办对照只有 1 份（32 条）→ 只能当**下界参考**，不是验收结论。
2. **字段来源**：`provider=none` 时责任单位/期限是**规则参考值**，不是模型结论。
3. **"召回"是关键词包含判定**（GT 关键词是否出现在候选文本里），不是语义等价判定。

⛔ 不要把这里的百分比当"验收通过"，也不要把"没找到"说成"没闭环"。
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.logutil import setup_console, write_report                    # noqa: E402

ANNO = ROOT / "config" / "annotations"
PER_FILE = ROOT / "var" / "extract" / "per_file"


def norm(s):
    return re.sub(r"[\s，。、：；·．,.:;（）()\[\]【】\-—_/\\]", "", s or "")


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def per_file_of(file_name):
    p = PER_FILE / (Path(file_name).stem + ".text.json")
    return load(p) if p.is_file() else None


def eval_relations(lines):
    f = ANNO / "1A1E808D_relations.json"
    if not f.is_file():
        return None
    anno = load(f)
    pf = per_file_of(anno["file"])
    if not pf:
        lines.append("## 第 1 层 · 关系对照")
        lines.append("")
        lines.append("找不到该文件的抽取明细（先跑 `run.py --extract`）")
        return None

    alias = anno.get("type_aliases") or {}
    # ⚠️ 三个字段都要过 norm（类型里的"/"会被 norm 去掉）—— 漏了就对不上 gold（实测踩过）
    ext = {(norm(r[0]), norm(alias.get(r[1], r[1])), norm(r[2]))
           for r in (anno.get("requires_external_knowledge") or [])}

    def tup(s, t, o):
        t = alias.get(t, t)
        return (norm(s), norm(t), norm(o))

    gold_all = {tup(*r) for r in anno["relations"]}
    gold_ext = {t for t in gold_all if t in ext}      # 原文里没有、需外部知识的那几条
    gold = {t for t in gold_all if t not in ext}      # 真正"可从原文抽出来"的金标
    auto = set()
    for r in pf["relationships"]:
        props = r.get("properties") or {}
        t = r["type"]
        if t == "单位角色" and props.get("role"):
            t = "单位角色/" + props["role"]          # 与标注的"监理"对齐（见 type_aliases）
        auto.add(tup(r["source"], t, r["target"]))

    hit, miss, extra = gold & auto, gold - auto, auto - gold

    # ⭐ 宽松口径：目标名**相似度 >= 0.8** 或互相包含即算命中 ——
    #    用来把"实体写法不同（消解问题）"与"真的没抽到（抽取问题）"分开。
    #    否则会把消解缺口记成抽取缺口（实测：OCR 把"创新园"读成"创新国"）。
    def _sim(a, b):
        return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0

    def loose_match(g, auto_set):
        gs, gt, go = g
        best, best_r = None, 0.0
        for a in auto_set:
            if a[0] != gs or a[1] != gt:
                continue
            if a[2] == go:
                return a, 1.0
            r = _sim(go, a[2])
            if go and (go in a[2] or a[2] in go):
                r = max(r, 0.9)
            if r > best_r:
                best, best_r = a, r
        return (best, best_r) if best_r >= 0.8 else (None, best_r)

    loose = [(g,) + loose_match(g, auto) for g in sorted(miss)]
    loose_hit = [(g, a, r) for g, a, r in loose if a]

    lines.append("## 第 1 层 · 关系对照（%s）" % anno["file"][:44])
    lines.append("")
    lines.append("- 人工标注 %d 条，其中 **%d 条原文里本来就没有这个信息**（需外部知识，已单列）→ 可抽金标 %d 条"
                 % (len(gold_all), len(gold_ext), len(gold)))
    lines.append("- 抽出 %d 条" % len(auto))
    lines.append("- **严格命中 %d/%d = %.0f%%**（主体/类型/目标都得字面一致）"
                 % (len(hit), len(gold), 100.0 * len(hit) / max(len(gold), 1)))
    lines.append("- **宽松命中 %d/%d = %.0f%%**（目标名相似度 >= 0.8 视为同一实体）"
                 % (len(hit) + len(loose_hit), len(gold),
                    100.0 * (len(hit) + len(loose_hit)) / max(len(gold), 1)))
    if gold_ext:
        lines.append("- ⚠️ 单列（不参与上面分母）：**原文无此信息** ——")
        for row in (anno.get("requires_external_knowledge") or []):
            lines.append("  - `%s --%s--> %s`：%s" % (row[0], row[1], row[2], row[3] if len(row) > 3 else ""))
    if loose_hit:
        lines.append("- 以下 %d 条属**写法差异（消解问题，不是抽漏）**：" % len(loose_hit))
        for g, a, r in loose_hit:
            lines.append("  - 标注 `%s`  vs  抽出 `%s`（相似度 %.2f）" % (g[2], a[2], r))
    strict_miss = [g for g, a, r in loose if not a]
    if strict_miss:
        lines.append("- 真漏（%d 条）：" % len(strict_miss))
        for s, t, o in sorted(strict_miss):
            lines.append("  - `%s --%s--> %s`" % (s, t, o))
    if extra:
        lines.append("- 多抽（%d 条，前 20）—— ⚠️ 抽查过：这里包含**标注遗漏的真实参会人**，" % len(extra))
        lines.append("  例如原文参会名单里确有 `张超/曾岳鑫/李昌锦/郑宇新/韩志勤/马晓波`，标注只标了一部分。")
        lines.append("  所以\"多抽\"不等于抽错，报数字时要一起说明。")
        for s, t, o in sorted(extra)[:20]:
            lines.append("  - `%s --%s--> %s`" % (s, t, o))
    lines.append("")
    return {"gold": len(gold), "gold_all": len(gold_all), "gold_ext": len(gold_ext),
            "auto": len(auto), "hit": len(hit),
            "hit_loose": len(hit) + len(loose_hit), "extra": len(extra),
            "recall": len(hit) / max(len(gold), 1),
            "recall_loose": (len(hit) + len(loose_hit)) / max(len(gold), 1)}


def eval_pending(lines):
    f = ANNO / "46121789_pending.json"
    if not f.is_file():
        return None
    anno = load(f)
    pf = per_file_of(anno["file"])
    if not pf:
        lines.append("## 第 2 层 · 待办对照")
        lines.append("")
        lines.append("找不到该文件的抽取明细（先跑 `run.py --extract`）")
        return None

    cands = pf.get("pending") or []
    joined = "\n".join(norm(c["quote"]) for c in cands)

    keys = ((anno.get("兼容对照_32主条目关键词") or {}).get("keys")) or []
    hit = [(g, k) for g, k in keys if norm(k) in joined]
    miss = [(g, k) for g, k in keys if norm(k) not in joined]

    lines.append("## 第 2 层 · 待办召回（%s）" % anno["file"][:44])
    lines.append("")
    lines.append("- 候选条目：**%d 条**（人工标注该文档待办总数为 %s，其中 32 条为主条目）"
                 % (len(cands), anno.get("待办总数")))
    lines.append("- **召回 %d/%d = %.1f%%**（判定口径：标注关键词是否出现在候选文本里）"
                 % (len(hit), len(keys), 100.0 * len(hit) / max(len(keys), 1)))
    if miss:
        lines.append("- 未命中（%d 条）：" % len(miss))
        for g, k in miss:
            lines.append("  - `[%s] %s`" % (g, k))
    lines.append("")

    # 责任单位：与人工标注的"提出方"比对（这才是"对不对"，不是"有没有值"）
    # ⚠️ 标注里 `提出方` 为空的，是**标注没记**，不能算"对不上" —— 单独计一类
    gold_org = {}
    for sec in (anno.get("章节") or []):
        for it in (sec.get("条目") or []):
            k = norm(it.get("事项") or "")
            if k:
                gold_org[k] = (it.get("提出方") or "", it.get("n"))
    total, right, wrong, no_label = 0, 0, [], 0
    for c in cands:
        q = norm(c["quote"])
        for k, (asker, n) in gold_org.items():
            if not k or k not in q:
                continue
            val = (c["rules"] or {}).get("responsible_org") or ""
            if not val:
                break
            if not asker or asker.strip() in ("—", "-", "无", "未记录"):
                no_label += 1
                break
            total += 1
            nv, na = norm(val), norm(asker)
            if nv and na and (nv in na or na in nv):
                right += 1
            else:
                wrong.append((asker, val, (c["quote"] or "")[:60]))
            break
    lines.append("### 责任单位（规则版）与人工标注「提出方」的对照")
    lines.append("")
    if total:
        lines.append("- 双方都有值的 %d 条里，对得上 **%d 条 = %.0f%%**"
                     % (total, right, 100.0 * right / total))
    else:
        lines.append("- 可与标注比对的条目为 0（规则没给出责任单位，或标注未记录提出方）")
    if no_label:
        lines.append("- 另有 %d 条：**标注未记录「提出方」**，不计入上面的分母" % no_label)
    if wrong:
        lines.append("- 对不上的（前 10）：")
        for asker, val, q in wrong[:10]:
            lines.append("  - 标注=%s / 规则=%s ｜ %s" % (asker or "—", val, q))
    lines.append("")
    return {"cands": len(cands), "hit": len(hit), "of": len(keys),
            "recall": len(hit) / max(len(keys), 1),
            "org_total": total, "org_right": right, "org_unlabeled": no_label}


def eval_by_project(lines):
    """按项目分组看候选规模 —— 模板差异会掩盖在总数里。"""
    rows = []
    for p in sorted(PER_FILE.glob("*.json")):
        d = load(p)
        info = d.get("info") or {}
        st = (d.get("stats") or {}).get("meeting") or {}
        rows.append({
            "file": (d.get("file") or "")[:40],
            "project": info.get("项目") or st.get("project") or "—",
            "items": len(d.get("pending") or []),
            "rels": len(d.get("relationships") or []),
            "persons": st.get("persons", 0),
        })
    lines.append("## 按项目分组（看模板差异）")
    lines.append("")
    lines.append("| 项目 | 文件 | 候选条目 | 关系 | 人数 |")
    lines.append("| :--- | :--- | ---: | ---: | ---: |")
    for r in rows:
        lines.append("| %s | %s | %d | %d | %d |"
                     % (r["project"], r["file"], r["items"], r["rels"], r["persons"]))
    lines.append("")
    return rows


def main(argv=None):
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "var" / "eval" / "eval_extract.md"))
    args = ap.parse_args(argv)

    if not PER_FILE.is_dir():
        print("还没有抽取产物，先跑：run.py --extract")
        return 2

    lines = [
        "# 抽取评测报告",
        "",
        "> ⚠️ **口径**：关系对照仅 1 份（18 条）、待办对照仅 1 份（32 条）→ 只能当**下界参考**；",
        "> 字段来源 = 规则（`llm.provider=none`）；召回用**关键词包含**判定，不是语义等价。",
        "",
    ]
    r1 = eval_relations(lines)
    r2 = eval_pending(lines)
    eval_by_project(lines)

    lines += ["## 结论（一句话）", ""]
    bits = []
    if r1:
        bits.append("第 1 层关系 严格命中 %d/%d（%.0f%%）/ 宽松 %d/%d（%.0f%%）、多抽 %d 条（含标注遗漏）"
                    % (r1["hit"], r1["gold"], 100 * r1["recall"],
                       r1["hit_loose"], r1["gold"], 100 * r1["recall_loose"], r1["extra"]))
    if r2:
        bits.append("第 2 层待办召回 %d/%d（%.1f%%）" % (r2["hit"], r2["of"], 100 * r2["recall"]))
        if r2["org_total"]:
            bits.append("规则版责任单位与标注提出方对得上 %.0f%%（%d/%d）"
                        % (100 * r2["org_right"] / r2["org_total"], r2["org_right"], r2["org_total"]))
    lines.append("；".join(bits) + "。" if bits else "无可用对照。")

    out = write_report(args.out, lines)
    for L in lines[:60]:
        print(L)
    print("\n[report] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
