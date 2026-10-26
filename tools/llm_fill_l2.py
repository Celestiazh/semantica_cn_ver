# -*- coding: utf-8 -*-
"""L2 字段填充抽样跑批：取 300 条候选 → Hy3 填责任单位/期限 → 回灌。

走项目既有契约：--pending-out 的结构 → llm_provider.fill（批量+缓存+留痕）
→ submit_extracted_fields 回灌（quote 逐字校验挡改写）。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg.config import Config                                    # noqa: E402
from kg.extract import llm_provider as L                        # noqa: E402
from kg.serve import tools as S                                 # noqa: E402

cfg = Config(ROOT)
provider = L.build_provider(cfg)
if provider.name == "none":
    sys.exit("llm.provider=none")

# ① 取全部未填候选
pending = S.get_pending_candidates(cfg)
items = pending["items"]
unfilled = [it for it in items if not (it.get("fields") or {}).get("responsible_org")]
print("候选总数 %d ｜ 未填 %d" % (len(items), len(unfilled)))

# ② 抽样或全量（argv[1] == "all" → 填全部未填）
mode = sys.argv[1] if len(sys.argv) > 1 else "sample"
if mode == "all":
    sample = unfilled
    print("模式：全量填充 %d 条" % len(sample))
else:
    import random
    rng = random.Random(20261010)
    sample = rng.sample(unfilled, min(300, len(unfilled)))
    print("模式：抽样 %d 条（seed=20261010）" % len(sample))

# ③ 构造 fill 输入（id/quote/section —— 与 test_llm_connection 同构）
fill_items = [{"id": it["id"], "quote": it.get("quote") or "",
               "section": it.get("section")} for it in sample]
cache_path = ROOT / "var" / "extract" / "llm_cache.json"
cache = L.load_cache(cache_path)
t0 = time.time()
got = provider.fill(fill_items, cache=cache, max_workers=6)
print("填充完成：%.0f 秒 ｜ 返回 %d 条" % (time.time() - t0, len(got)))

# ④ 回灌（契约：字段平铺在 item 层；不带 quote = 不触发原文校验）
# ⭐ 顺序：先 submit 再存缓存 —— submit 成功后字段已落 pending_candidates.json，
#    即使 save_cache 失败（实测踩过：文件锁）也不会丢掉整批模型结果。
payload = {"items": [], "source": "llm:%s:%s" % (provider.name, provider.model)}
ok = 0
for it in sample:
    f = got.get(it["id"]) or {}
    if not f or f.get("_error"):
        continue
    payload["items"].append({"id": it["id"], **f})
    ok += 1
res = S.submit_extracted_fields(cfg, payload, source="llm:%s:%s" % (provider.name, provider.model))
print("回灌：提交 %d ｜ 应答 %s" % (ok, json.dumps(res, ensure_ascii=False)[:200]))

# ⑤ 存缓存（原子写：temp + os.replace，防文件锁写坏半截）
try:
    import os
    tmp = str(cache_path) + ".tmp"
    Path(tmp).write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, cache_path)
    print("缓存已保存：%d 条" % len(cache))
except Exception as e:                                       # noqa: BLE001
    print("⚠ 缓存保存失败（不影响已回灌字段）：%s: %s" % (type(e).__name__, e))

# ⑥ 速览
st = S.pending_status(cfg)
print("状态：%s" % json.dumps(st, ensure_ascii=False)[:300])
