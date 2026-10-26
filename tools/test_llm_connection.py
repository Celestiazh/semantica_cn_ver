# -*- coding: utf-8 -*-
"""大模型接入测试（阶段 1：连通性 + 质量底线）

    .venv\\Scripts\\python.exe tools\\test_llm_connection.py ^
        --base-url http://123.184.7.210:8007/v1 --model hy3 --key-env HY3_API_KEY

它做四件事：
  1. 探活 GET /v1/models（顺便拿到准确的模型名）
  2. 取 N 条真实候选，让模型填字段：看 **JSON 能不能稳定解析**
  3. ⭐ **编造检查**：模型给出的责任单位，是否真的出现在原文片段里 / 名册里 ——
     这是最危险的失败模式（硬编一个单位名，会把错误结论写进图里）
  4. ⭐ **可复现检查**：同一条跑两次，字段必须一致（temperature=0 的验证）

⛔ 不打印 API key。所有输出落在 `var/eval/llm_conn_test.md`。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.logutil import setup_console, write_report                 # noqa: E402
from kg.config import load                                          # noqa: E402
from kg.extract import llm_provider                                 # noqa: E402

CANDIDATES = ROOT / "var" / "extract" / "pending_candidates.json"


def load_items(limit, hard=False):
    if not CANDIDATES.is_file():
        raise FileNotFoundError("没有候选产物：先跑 `run.py --extract`")
    items = json.loads(CANDIDATES.read_text(encoding="utf-8"))["items"]
    if not hard:
        return items[:limit]
    # 挑最难的：一半取最长的，一半取"规则没给出责任单位"的，**合起来要够 limit 条**
    # ⛔ 修正：上一版写死了各取 3/2 条，导致 --hard --limit 20 实际只跑 5 条
    n_long = max(3, limit // 2)
    longest = sorted(items, key=lambda x: -len(x.get("quote") or ""))[:n_long]
    noorg = [x for x in items if not (x.get("rules") or {}).get("responsible_org")]
    noorg = sorted(noorg, key=lambda x: -len(x.get("quote") or ""))[:max(2, limit - len(longest))]
    picked, seen = [], set()
    for x in longest + noorg:
        if x["id"] not in seen:
            picked.append(x)
            seen.add(x["id"])
    return picked[:limit]


def fabrication_check(item, fields, aliases):
    """责任单位是否真有出处：出现在原文片段里，或命中名册别名。"""
    org = (fields.get("responsible_org") or "").strip()
    if not org:
        return None, "留空（安全）"
    quote = item.get("quote") or ""
    core = org.strip("（）()有限公司集团")
    if org in quote or (core and core in quote):
        return True, "原文里有"
    if org in aliases:
        return True, "命中名册别名"
    for a in aliases:
        if a and (a in org or org in a):
            return True, "名册近似（%s）" % a
    return False, "⛔ 原文与名册里都没有 → 疑似编造"


def one_round(provider, items, fields_expected, tag):
    t0 = time.time()
    got = provider.fill(items, fields_expected)
    dt = time.time() - t0
    rows = []
    for it in items:
        v = got.get(it["id"]) or {}
        rows.append({"id": it["id"], "page": it.get("page"),
                     "quote": it.get("quote") or "",          # 保留全文，编造检查要用
                     "quote_show": (it.get("quote") or "")[:90],
                     "rule_org": (it.get("rules") or {}).get("responsible_org") or "",
                     "fields": {k: v.get(k) for k in fields_expected},
                     "error": v.get("_error")})
    return rows, dt


def main(argv=None):
    setup_console()
    cfg = load(ROOT)
    lcfg = cfg.settings.get("llm") or {}

    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=lcfg.get("base_url") or "")
    ap.add_argument("--model", default=lcfg.get("model") or "hy3")
    ap.add_argument("--key-env", default=lcfg.get("api_key_env") or "HY3_API_KEY")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--batch-size", type=int, default=0, help="0 = 用配置值；1 = 强制逐条")
    ap.add_argument("--hard", action="store_true", help="挑最难的候选（最长 + 规则没给出责任单位）")
    ap.add_argument("--rounds", type=int, default=2, help="跑几轮做一致性比对")
    ap.add_argument("--out", default=str(ROOT / "var" / "eval" / "llm_conn_test.md"))
    args = ap.parse_args(argv)

    # key 解析要带上完整 llm 配置 —— 否则写在 settings.yaml 里的 llm.api_key 会被漏掉
    keycfg = dict(lcfg)
    keycfg.setdefault("api_key_env", args.key_env)
    key = llm_provider.resolve_api_key(keycfg, ROOT)
    lines, problems = [], []

    lines.append("# 大模型接入测试（阶段 1）")
    lines.append("")
    lines.append("- 端点：`%s`  模型：`%s`  batch_size：%s  reasoning_effort：`%s`"
                 % (args.base_url or "(未填)", args.model,
                    args.batch_size or lcfg.get("batch_size") or 20,
                    lcfg.get("reasoning_effort", "")))
    lines.append("- API key：%s（只报有无，不打印内容）" % ("已从环境变量 %s 读到" % args.key_env if key else "**没读到**"))
    lines.append("")

    if not args.base_url:
        print("没有 base_url。用 --base-url 指定，或写进 config/settings.yaml 的 llm.base_url")
        return 2

    # ---------- 1. 探活 ----------
    ok, info = llm_provider.probe(args.base_url, args.model, key)
    lines.append("## 1) 探活 `GET /v1/models`")
    lines.append("")
    lines.append("- 结果：%s —— %s" % ("✅ 在线" if ok else "❌ 不通", info))
    lines.append("")
    if not ok:
        if not key:
            lines.append("> 401 通常是缺 key。先把 key 放进环境变量 `%s` 再跑。" % args.key_env)
        write_report(args.out, lines)
        print("\n".join(lines[-6:]))
        return 2
    if args.model not in (info or ""):
        lines.append("> ⚠️ 端点返回的模型列表里没有 `%s`，以列表为准填 llm.model。" % args.model)

    # ---------- 2. 造 provider ----------
    provider = llm_provider.OpenAICompatProvider(
        args.base_url, args.model, key,
        timeout=lcfg.get("timeout_s") or 120,
        batch_size=args.batch_size or lcfg.get("batch_size") or 20,
        reasoning_effort=lcfg.get("reasoning_effort", "no_think"),
    )

    items = load_items(args.limit, hard=args.hard)
    fields_expected = ["responsible_org", "deadline", "matter"]
    aliases = set(cfg.alias_map().keys())

    lines.append("## 2) 抽取实测（%d 条真实候选%s）" % (len(items), "，挑最难的" if args.hard else ""))
    lines.append("")

    rounds = []
    for r in range(max(1, args.rounds)):
        rows, dt = one_round(provider, items, fields_expected, "round%d" % (r + 1))
        rounds.append((rows, dt))
        nerr = sum(1 for x in rows if x["error"])
        lines.append("- 第 %d 轮：%.1fs，%d 条，失败 %d 条" % (r + 1, dt, len(rows), nerr))
        if nerr:
            for x in rows:
                if x["error"]:
                    lines.append("  - ⛔ %s：%s" % (x["id"], str(x["error"])[:160]))
    lines.append("")

    rows = rounds[0][0]
    lines.append("### 逐条结果")
    lines.append("")
    lines.append("| 候选 | 页 | 规则给的责任单位 | **模型给的责任单位** | 期限 | 事项 | 编造检查 |")
    lines.append("| :--- | ---: | :--- | :--- | :--- | :--- | :--- |")
    for x in rows:
        f = x["fields"]
        verdict, why = fabrication_check(
            {"quote": x["quote"]}, {"responsible_org": f.get("responsible_org")}, aliases)
        if verdict is False:
            problems.append("疑似编造责任单位：%s → %s" % (x["id"], f.get("responsible_org")))
        lines.append("| %s | %s | %s | **%s** | %s | %s | %s |"
                     % (x["id"][-12:], x["page"], x["rule_org"] or "—",
                        f.get("responsible_org") or "—", f.get("deadline") or "—",
                        (f.get("matter") or "—")[:24], why))
    lines.append("")

    # ---------- 3. 可复现 ----------
    lines.append("## 3) 可复现检查（同一条跑 %d 轮，字段必须一致）" % len(rounds))
    lines.append("")
    if len(rounds) > 1:
        bad = []
        for i, x in enumerate(rows):
            first = x["fields"]
            for other, _ in rounds[1:]:
                if i < len(other) and other[i]["fields"] != first:
                    bad.append((x["id"], first, other[i]["fields"]))
        if bad:
            lines.append("- ❌ **%d 条不一致** —— temperature=0 没生效，或服务端有随机性：" % len(bad))
            for iid, a, b in bad[:5]:
                lines.append("  - %s：`%s` vs `%s`" % (iid, json.dumps(a, ensure_ascii=False),
                                                       json.dumps(b, ensure_ascii=False)))
            problems.append("同一条两轮结果不一致 %d 条" % len(bad))
        else:
            lines.append("- ✅ **%d 条全部一致** —— temperature=0 生效，可复现" % len(rows))
    lines.append("")

    # ---------- 4. token 与结论 ----------
    lines.append("## 4) 结论")
    lines.append("")
    if problems:
        lines.append("- ⚠️ 有 %d 个问题：%s" % (len(problems), "；".join(problems[:5])))
    else:
        lines.append("- ✅ 未发现编造与不可复现问题")
    lines.append("- 下一步：阶段 2 用 `--hard --limit 20`，阶段 3 去掉 --limit 跑全量 297 条，")
    lines.append("  然后拿 `tools/eval_extract.py` 对照 0822 人工标注，看责任单位从规则的 52% 提到多少。")

    out = write_report(args.out, lines)
    for L in lines:
        print(L)
    print("\n[report] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
