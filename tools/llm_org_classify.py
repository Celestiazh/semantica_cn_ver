# -*- coding: utf-8 -*-
"""实验：让 Hy3 判别 ORG 实体名是"具体机构名"还是"泛称/角色描述"。

背景（2026-09-30）：图里发现「配合设计院」「协调物业公司」这类泛称实体 —— 它们来自
表格单元格原文，静态规则无法判断"是不是一个具体机构"。本实验量化 LLM 在这个环节的
准确率，决定是否把"规则抽 + LLM 裁判"固化进链路。

方法：59 个 ORG 名 + 各自证据片段 → Hy3（temperature=0，10 条/批，短序号防错配）
     → JSON {id, 判定: 具体名|泛称|不确定, 理由}
评估：与启发式预期对照（动词前缀→应判泛称；含法人后缀→应判具体名），
     分歧项列出来人工复核。全程留痕报告 + 判定明细 CSV。

产出：var/eval/org_classify_test.md + var/eval/org_classify.csv
"""
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg.config import Config                                   # noqa: E402
from kg.extract import llm_provider as L                       # noqa: E402

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
OUT_MD = ROOT / "var" / "eval" / "org_classify_test.md"
OUT_CSV = ROOT / "var" / "eval" / "org_classify.csv"
BATCH = 10

VERB_PREFIX = ("配合", "协调", "督促", "组织", "会同", "协助", "联合", "牵头")
LEGAL_SUFFIX = ("有限责任公司", "股份有限公司", "有限公司", "研究院", "工程院",
                "事务所", "管理局", "委员会", "工务署", "中心")


def heuristic(name):
    """启发式预期：动词开头→泛称；含法人后缀→具体名；否则 None（无预期，纯靠模型+人）。"""
    if name.startswith(VERB_PREFIX):
        return "泛称"
    if any(s in name for s in LEGAL_SUFFIX):
        return "具体名"
    return None


PROMPT = (
    "你是机构名称判别器。下面每条给出一个从工程监理纪要中抽出的「单位名」，"
    "以及它出现处的原文片段。判断这个名字：\n"
    "  - 具体名：能唯一指向一家真实机构（法人/单位），如「中建科工集团有限公司」「深圳市坪山区建筑工务署」\n"
    "  - 泛称：是角色/类别描述，不指向特定机构，如「配合设计院」「协调物业公司」「施工单位」\n"
    "  - 不确定：证据不足无法判断\n"
    "对每条输出 JSON 对象：\n"
    '  "id": 原样带回编号, "判定": "具体名"/"泛称"/"不确定", "理由": 不超过 16 字\n'
    '只输出 JSON：{"items":[...]}，不要其它文字。'
)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    g = json.loads(GRAPH.read_text(encoding="utf-8"))
    cfg = Config(ROOT)
    from kg import resolve as res_mod
    paths = res_mod._locate_files(cfg)
    provider = L.build_provider(cfg)
    if provider.name == "none":
        sys.exit("llm.provider 是 none")
    print("模型 %s:%s" % (provider.name, provider.model))

    # 组装样本：名 + 证据（最多 2 条）
    ev_of = {}
    for r in g["relationships"]:
        ev = (r.get("properties") or {}).get("evidence") or []
        for side in (r.get("source"), r.get("target")):
            if side:
                ev_of.setdefault(side, []).extend(ev)
    orgs = sorted(e["name"] for e in g["entities"] if e["type"] == "ORG")
    samples = []
    for name in orgs:
        quotes = []
        for v in ev_of.get(name, [])[:2]:
            f = v.get("file", "")
            quotes.append("《%s》p%s「%s」" % (paths.get(f, f)[-40:], v.get("page"),
                                              (v.get("quote") or "")[:50]))
        samples.append({"name": name, "ctx": " ｜ ".join(quotes) or "（无证据）"})
    print("样本 %d 个" % len(samples))

    results, calls = {}, 0
    t0 = time.time()
    for bi in range(0, len(samples), BATCH):
        chunk = samples[bi:bi + BATCH]
        user = "\n\n".join(
            "[%d] 名称：%s\n原文片段：%s" % (i + 1, s["name"], s["ctx"])
            for i, s in enumerate(chunk))
        payload = {"model": provider.model, "temperature": 0, "top_p": 1,
                   "max_tokens": 1200,
                   "messages": [{"role": "system", "content": PROMPT},
                                {"role": "user", "content": user}]}
        if getattr(provider, "reasoning_effort", ""):
            payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
        got = {}
        try:
            calls += 1
            t1 = time.time()
            data = provider._post(provider.base_url + "/chat/completions", payload,
                                  {"Authorization": "Bearer " + provider.api_key
                                   if provider.api_key else ""})
            obj = L.parse_json_payload(L.message_text((data.get("choices") or [{}])[0].get("message") or {}))
            items = obj.get("items") if isinstance(obj, dict) else obj
            for it in items or []:
                got[str(it.get("id"))] = it
            print("批 %d-%d：%.1fs，返回 %d/%d" % (bi + 1, bi + len(chunk), time.time() - t1, len(got), len(chunk)))
        except Exception as e:                               # noqa: BLE001
            print("批 %d 失败：%s: %s" % (bi + 1, type(e).__name__, e))
        for i, s in enumerate(chunk):
            it = got.get(str(i + 1)) or {}
            d = (it.get("判定") or "").strip()
            if d not in ("具体名", "泛称", "不确定"):
                d, why = "未判定", "输出非法/批次失败"
            else:
                why = (it.get("理由") or "").strip()[:24]
            results[s["name"]] = {"判定": d, "理由": why,
                                  "启发式预期": heuristic(s["name"]), "ctx": s["ctx"]}

    # ---- 评估 ----
    dist = {}
    for v in results.values():
        dist[v["判定"]] = dist.get(v["判定"], 0) + 1
    agree = disagree = no_expect = 0
    rows = []
    for name, v in sorted(results.items(), key=lambda x: (x[1]["判定"], x[0])):
        exp = v["启发式预期"]
        mark = ""
        if exp is None:
            no_expect += 1
        elif exp == v["判定"]:
            agree += 1
            mark = "与启发式一致"
        else:
            disagree += 1
            mark = "⚠ 与启发式不一致"
        rows.append({"名称": name, "模型判定": v["判定"], "理由": v["理由"],
                     "启发式预期": exp or "—", "对照": mark, "原文片段": v["ctx"]})

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    lines = ["# ORG 名称判别实验（Hy3，%s）" % time.strftime("%Y-%m-%d %H:%M"), "",
             "- 样本：%d 个 ORG 实体 ｜ %d 次请求（%d 条/批，temperature=0）｜ 耗时 %.0f 秒"
             % (len(samples), calls, BATCH, time.time() - t0),
             "- 模型判定分布：%s" % dist,
             "- 与启发式预期对照：一致 %d ｜ 不一致 %d ｜ 启发式无预期 %d" % (agree, disagree, no_expect),
             "", "## 模型判为「泛称」的（重点复核）", ""]
    for r in rows:
        if r["模型判定"] == "泛称":
            lines.append("- **%s**（%s）｜ %s" % (r["名称"], r["理由"], r["原文片段"][:70]))
    lines += ["", "## 与启发式不一致的（人工裁决）", ""]
    dis = [r for r in rows if r["对照"].startswith("⚠")]
    if not dis:
        lines.append("（无）")
    for r in dis:
        lines.append("- %s：模型=%s（%s），启发式预期=%s ｜ %s" % (
            r["名称"], r["模型判定"], r["理由"], r["启发式预期"], r["原文片段"][:60]))
    lines += ["", "## 「不确定」的", ""]
    for r in rows:
        if r["模型判定"] == "不确定":
            lines.append("- %s（%s）" % (r["名称"], r["理由"]))
    lines += ["", "明细见 org_classify.csv。判定规则：泛称→从图中移除或转角色标注；不确定→人工裁决。"]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("\n分布：%s ｜ 一致 %d / 不一致 %d / 无预期 %d" % (dist, agree, disagree, no_expect))
    print("报告：%s" % OUT_MD)


if __name__ == "__main__":
    main()
