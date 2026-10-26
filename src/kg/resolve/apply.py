# -*- coding: utf-8 -*-
"""③-④⑤ 执行 + 沉淀

④ 合并：读回 CSV → 「合并」对按"保留哪条"定正名 → 重新归一 → 产出归并后的图
⑤ 沉淀：合并 → `roster_extra.yaml`（别名追加 / 新单位）；驳回 → `denied_extra.yaml`
        → 下一轮第①步自动归并、第②步直接过滤，**人的活不再重复**

⛔ 一律写 extra 文件，不回写人工维护的 roster.yaml / denied_pairs.yaml（保注释）。
"""
import time
from pathlib import Path

import yaml


def _dump(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                                default_flow_style=False), encoding="utf-8")


def _load(path):
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def settle(rows, cfg_dir, decided_by="人工确认(_csv)"):
    """把人工决定沉淀进 extra 配置。返回 {"merged": n, "rejected": n, "skipped": n}。"""
    cfg_dir = Path(cfg_dir)
    extra_roster = _load(cfg_dir / "roster_extra.yaml") or {"units": []}
    units_by_name = {u["name"]: u for u in extra_roster.get("units") or []}
    extra_denied = _load(cfg_dir / "denied_extra.yaml") or {"denied": []}
    denied_keys = {(d.get("a"), d.get("b")) for d in extra_denied.get("denied") or []}

    today = time.strftime("%Y-%m-%d")
    stat = {"合并": 0, "驳回": 0, "存疑": 0, "未填": 0}
    for r in rows:
        d = r["决定"]
        if d not in stat:
            continue
        stat[d] += 1
        a, b = r["a"], r["b"]
        if not a or not b:
            continue
        if d == "合并":
            canon = r["保留"] or a          # 没填"保留哪条"就默认保留 A
            if canon not in (a, b):
                canon = a
            alias = b if canon == a else a
            if canon in units_by_name:
                if alias not in units_by_name[canon]["aliases"]:
                    units_by_name[canon]["aliases"].append(alias)
            else:
                units_by_name[canon] = {"name": canon, "aliases": [alias], "type": ""}
                extra_roster["units"].append(units_by_name[canon])
            extra_roster.setdefault("updated", today)
        elif d == "驳回":
            k = (a, b)
            if k not in denied_keys:
                extra_denied.setdefault("denied", []).append(
                    {"a": a, "b": b, "reason": r.get("备注") or "人工驳回",
                     "decided_by": decided_by, "decided_at": today})
                denied_keys.add(k)
                extra_denied["updated"] = today

    _dump(cfg_dir / "roster_extra.yaml", extra_roster)
    _dump(cfg_dir / "denied_extra.yaml", extra_denied)
    return {"合并": stat["合并"], "驳回": stat["驳回"],
            "存疑": stat["存疑"], "未填": stat["未填"]}
