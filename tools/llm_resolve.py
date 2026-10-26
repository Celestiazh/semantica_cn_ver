# -*- coding: utf-8 -*-
"""LLM 代判消解候选 —— **仅测试轮**（2026-09-29 用户拍板临时放开）

⛔ 与既定设计的冲突要讲清：项目设计是「消解不加 LLM」（可复现/可审计/零成本），
   本工具是**用户明确要求的一次性例外**，用于"不人工勾选、直接看后续结果"的测试轮。
   留痕措施（不破坏可审计性）：
     - 每行决定写进 review.csv 的「决定/保留哪条」列（apply 照常读）
     - 新增「决定人」列 = llm:<模型名>、「理由」列 = 模型原话 —— 事后可人工复核
     - 校验器拦截模型输出里的非法值（决定不在枚举内 / 保留不是 A 或 B → 降级"存疑"）

用法：
    python tools/llm_resolve.py            # 逐条判定并回写 review.csv
    python run.py --resolve --apply        # 照常执行合并 → 归并后的图
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.config import Config                                   # noqa: E402
from kg.extract import llm_provider as L                       # noqa: E402

CSV_PATH = ROOT / "var" / "resolve" / "review.csv"

PROMPT_SYSTEM = (
    "你是工程监理会议纪要知识图谱的实体消解判定器。给你两个单位名和它们在语料里的"
    "出处节选，判断它们是否指同一家机构。\n"
    "判定规则（按优先级）：\n"
    "  1. **法人形式不同就不是同一家**：`股份有限公司` 与 `有限责任公司` 是两种法人组织形式，\n"
    "     名字只有这一点不同也必须判「驳回」；\n"
    "  2. 名字互为**简称/全称**（如「中建科工集团」⊂「中建科工集团有限公司」）→ 合并；\n"
    "  3. 名字高度相似但指不同业务的（如「建筑工程」vs「建筑装饰工程」）→ 驳回；\n"
    "  4. 出处节选互相矛盾或证据不足以确定 → 存疑。\n"
    "对每一对输出 JSON 对象：\n"
    '  "id"     : 原样带回编号\n'
    '  "决定"   : "合并" / "驳回" / "存疑" 三选一\n'
    '  "保留"   : 合并时填要保留的那个**完整名字**（A 或 B 的原文），驳回/存疑填空串\n'
    '  "理由"   : 不超过 40 字\n'
    "只输出 JSON：{\"items\": [...]}，不要其它文字。"
)

HEADER_NEW = None          # 运行时按旧表头 + 两列生成


def main():
    if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台 GBK：↔/→ 会崩，强制 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig", newline="")))
    todo = [r for r in rows if not (r.get("决定") or "").strip()]
    print("待判定 %d / 共 %d 行" % (len(todo), len(rows)))
    if not todo:
        print("没有待判定行")
        return

    cfg = Config(ROOT)
    provider = L.build_provider(cfg)
    if provider.name == "none":
        print("⛔ settings.yaml 的 llm.provider 是 none —— 先配置模型再跑")
        return
    print("provider = %s:%s @ %s" % (provider.name, provider.model,
                                     getattr(provider, "base_url", "-")))

    # 逐条判定（判定是判断题不是抽取题，batch=1 防错配；18 条成本低）
    results = []
    for i, r in enumerate(todo, 1):
        user = ("【编号】%d\n【A】%s\n【B】%s\n【相似度】%s（%s）\n"
                "【A 的出处节选】\n%s\n【B 的出处节选】\n%s"
                % (i, r["A"], r["B"], r["相似度"], r["依据"],
                   r.get("A的出处") or "（无）", r.get("B的出处") or "（无）"))
        payload = {
            "model": provider.model, "temperature": 0, "top_p": 1,
            "max_tokens": 700,
            "messages": [{"role": "system", "content": PROMPT_SYSTEM},
                         {"role": "user", "content": user}],
        }
        if getattr(provider, "reasoning_effort", ""):
            payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
        try:
            data = provider._post(provider.base_url + "/chat/completions", payload,
                                  {"Authorization": "Bearer " + provider.api_key
                                   if provider.api_key else ""})
            choice = (data.get("choices") or [{}])[0]
            obj = L.parse_json_payload(L.message_text(choice.get("message") or {}))
            item = (obj.get("items") or [obj])[0] if isinstance(obj, dict) else (obj[0] if obj else {})
            d = (item.get("决定") or "").strip()
            keep = (item.get("保留") or "").strip()
            why = (item.get("理由") or "").replace("\n", " ")[:80]
            # ---- 校验器：非法输出降级为存疑，绝不把坏值写进图 ----
            if d not in ("合并", "驳回", "存疑"):
                d, why = "存疑", "模型输出非法决定 %r，已降级" % d[:20]
            if d == "合并" and keep not in (r["A"], r["B"]):
                d, keep, why = "存疑", "", "模型给的保留名不是 A/B，已降级"
            if d == "合并" and keep == r["A"] and len(r["B"]) > len(r["A"]):
                why += "（注：保留了较短名）"
        except Exception as e:                               # noqa: BLE001
            d, keep, why = "存疑", "", "调用失败 %s: %s" % (type(e).__name__, e)
        r["决定"], r["保留哪条"], r["决定人"], r["理由"] = d, keep, "llm:" + provider.model, why
        results.append((r["A"], r["B"], d, keep, why))
        print("[%2d] %s ↔ %s → %s %s | %s" % (i, r["A"][:18], r["B"][:18], d,
                                              ("→保留 " + keep[:14]) if keep else "", why[:40]))

    # ---- 回写 CSV（加 决定人/理由 两列）----
    header = list(rows[0].keys())
    for col in ("决定人", "理由"):
        if col not in header:
            header.append(col)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    n = {"合并": 0, "驳回": 0, "存疑": 0}
    for _, _, d, _, _ in results:
        n[d] += 1
    print("判定完成：%s → 下一步 `run.py --resolve --apply`" % n)


if __name__ == "__main__":
    main()
