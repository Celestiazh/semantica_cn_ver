# -*- coding: utf-8 -*-
"""量一下不同 batch_size 的真实速度与 token 消耗，并外推到全量。

    .venv\\Scripts\\python.exe tools\\time_llm_run.py                 # 默认 batch=1 与 3，各 12 条
    .venv\\Scripts\\python.exe tools\\time_llm_run.py --n 20 --sizes 1,3,5

报告内容：每条延迟（中位数/最大）、每请求 token、外推到 297 条（旧语料）与 1000 条的时间与花费。
⛔ 不打印 key。
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.logutil import setup_console, write_report          # noqa: E402
from kg.config import load                                   # noqa: E402
from kg.extract import llm_provider                          # noqa: E402

CANDIDATES = ROOT / "var" / "extract" / "pending_candidates.json"


def run_case(provider, items):
    """返回 (总秒数, 每请求秒数列表, prompt_tokens, completion_tokens, 失败数)"""
    lat, pt, ct, fail = [], 0, 0, 0
    t_all = time.time()
    size = provider.batch_size
    for i in range(0, len(items), size):
        batch = items[i:i + size]
        t0 = time.time()
        try:
            provider._chat_batch([(it["id"], it.get("quote", "")) for it in batch])
            ok = True
        except Exception:                                      # noqa: BLE001
            ok = False
            fail += 1
        lat.append(time.time() - t0)
        if not ok:
            continue
        # 为了拿 token 用量，等价再发一次最小请求统计（只统计不打点）—— 改用 usage 累加：
    # usage 需要从 _post 拿；这里用一次带 usage 的采样代替（见 sample_usage）
    return time.time() - t_all, lat, pt, ct, fail


def sample_usage(provider, n_items, batch_size):
    """发一个批次，读回 usage —— 用实测 token 外推，不拍脑袋。"""
    items = json.loads(CANDIDATES.read_text(encoding="utf-8"))["items"][:n_items]
    batch = [(it["id"], it.get("quote", "")) for it in items][:batch_size]
    shorts = [str(i + 1) for i in range(len(batch))]
    if len(batch) == 1:
        system, user = llm_provider.PROMPT_SYSTEM_ONE, batch[0][1]
    else:
        system = llm_provider.PROMPT_SYSTEM_BATCH
        user = "\n\n".join("[%s] %s" % (s, q) for s, (_i, q) in zip(shorts, batch))
    payload = {
        "model": provider.model, "temperature": 0, "top_p": 1,
        "max_tokens": max(512, 320 * len(batch)),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if provider.reasoning_effort:
        payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
    data = provider._post(provider.base_url + "/chat/completions", payload,
                          {"Authorization": "Bearer " + provider.api_key})
    u = data.get("usage") or {}
    return u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0, len(batch)


def main(argv=None):
    setup_console()
    cfg = load(ROOT)
    lcfg = cfg.settings["llm"]
    key = llm_provider.resolve_api_key(lcfg, ROOT)
    if not key:
        print("没读到 key（config/settings.yaml 的 llm.api_key 或环境变量）")
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12, help="每个档位跑多少条候选")
    ap.add_argument("--sizes", default="1,3,5", help="要测的 batch_size，逗号分隔")
    args = ap.parse_args(argv)

    items = json.loads(CANDIDATES.read_text(encoding="utf-8"))["items"][:args.n]
    lines = []
    def w(s=""):
        lines.append(str(s)); print(s)

    w("候选样本：%d 条（取自 %s，平均 %d 字）"
      % (len(items), CANDIDATES.name,
         statistics.mean(len(it.get("quote") or "") for it in items)))
    w("端点：%s  模型：%s" % (lcfg.get("base_url"), lcfg.get("model")))
    w()
    w("%-8s %-8s %-10s %-9s %-9s %-11s %-11s %-9s" %
      ("batch", "请求数", "总耗时s", "每请求中位", "每请求最大", "prompt/条", "compl/条", "失败"))
    w("-" * 88)

    summary = {}
    for size in [int(x) for x in args.sizes.split(",")]:
        prov = llm_provider.OpenAICompatProvider(
            lcfg["base_url"], lcfg["model"], key,
            timeout=lcfg.get("timeout_s", 120), batch_size=size,
            reasoning_effort=lcfg.get("reasoning_effort", "no_think"))
        total, lat, _p, _c, fail = run_case(prov, items)
        pt, ct, got = sample_usage(prov, len(items), size)
        per_prompt, per_compl = pt / max(got, 1), ct / max(got, 1)
        w("%-8d %-8d %-10.1f %-9.2f %-9.2f %-11.0f %-11.0f %-9d"
          % (size, len(lat), total, statistics.median(lat), max(lat), per_prompt, per_compl, fail))
        summary[size] = dict(total=total, n_req=len(lat), med=statistics.median(lat),
                             max=max(lat), per_prompt=per_prompt, per_compl=per_compl, fail=fail)

    w()
    w("=== 外推（按上面实测的每请求耗时与每条 token）===")
    w("%-8s %-16s %-16s %-18s" % ("batch", "297 条(旧语料)", "1000 条", "token 合计(1000条)"))
    w("-" * 74)
    for size, s in summary.items():
        per_item_time = s["med"] / size
        t297 = per_item_time * 297
        t1000 = per_item_time * 1000
        tok = (s["per_prompt"] + s["per_compl"]) * 1000
        w("%-8d %-16s %-16s %-18s"
          % (size, "%d 分 %d 秒" % (int(t297 // 60), int(t297 % 60)),
             "%d 分 %d 秒" % (int(t1000 // 60), int(t1000 % 60)),
             "%.0f 万 token" % (tok / 10000)))
    w()
    w("费用参考（按 输入 1 元/百万、输出 4 元/百万 估）：")
    for size, s in summary.items():
        c = (s["per_prompt"] * 1000 * 1 + s["per_compl"] * 1000 * 4) / 1_000_000
        w("  batch=%-2d  1000 条 ≈ %.1f 元" % (size, c))

    out = write_report(ROOT / "var" / "eval" / "llm_timing.md", lines)
    print("\n[report] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
