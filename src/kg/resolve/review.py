# -*- coding: utf-8 -*-
"""③-③ 人工确认：CSV 生成 / 读回

设计（不改）：不做界面、不做对话式 —— CSV 可留痕、可复查、可重复跑。
列：A | B | 相似度 | 依据 | A的出处 | B的出处 | 工具建议 | 决定 | 保留哪条
人只填最后两列：决定 ∈ {合并, 驳回, 存疑}（留空 = 本轮跳过）。

⭐ 「A/B的出处」（2026-09-29 用户指出后加）：每条 = 《完整文件名》第N页「原文前40字」，
   同一文件只取第一条，一个名字最多 3 条 —— 完整文件名可复制到语料目录原样搜索定位。
⭐ 重新生成时**保留已填的决定**（按 A+B 对匹配）—— 人填过的活绝不让人重填。
"""
import csv
import re
from pathlib import Path

HEADER = ["A", "B", "相似度", "依据", "A的出处", "B的出处", "工具建议", "决定", "保留哪条"]


def _fmt_ev(ev_list, name=None, k=3, quote_max=40, paths=None):
    """证据列表 → 单元格文本。同一文件去重（一条边可能带同文件多页）。

    ⭐⭐ 显示**完整绝对路径**（`paths`：文件名 → 磁盘位置，由 `_locate_files` 扫描
    语料目录得出）—— 只给文件名，人可能在任何一批语料目录里都搜不到
    （2026-09-29 用户实测：坪山那批在旧目录 监理会议纪要原文，不在 会议纪要154）。
    未定位的退回显示文件名本身。
    排序：优先含该名字的节选，同级按文件名稳定排序（不用"遇到顺序"这种隐式序）。
    """
    ev_list = list(ev_list or [])
    if name:
        ev_list.sort(key=lambda e: (name not in (e.get("quote") or ""),
                                    e.get("file") or ""))
    else:
        ev_list.sort(key=lambda e: e.get("file") or "")
    out, seen = [], set()
    for e in ev_list:
        f = e.get("file") or "?"
        if f in seen:
            continue
        seen.add(f)
        loc = (paths or {}).get(f, f)        # 有完整路径给路径，没有退回文件名
        q = re.sub(r"\s+", " ", e.get("quote") or "")
        q = q[:quote_max] + ("…" if len(q) > quote_max else "")
        out.append("《%s》第%s页「%s」" % (loc, e.get("page", "?"), q))
        if len(out) >= k:
            break
    return "\n".join(out)


def _key(a, b):
    return tuple(sorted((a, b)))


def _read_previous(p):
    """读旧 CSV 里已填的（决定, 保留哪条），按 A+B 对索引。

    ⚠️ 格式/编码容错见 kg/csvio.py：Excel 另存会变 GBK、选"保留工作簿"会变
    xlsx（.csv 后缀装工作簿，openpyxl 按扩展名拒收需拷贝改名）—— 全部实测。
    """
    if not p.is_file():
        return {}
    prev = {}
    try:
        from ..csvio import read_rows
        for r in read_rows(p):
            d = (r.get("决定") or "").strip()
            if not d:
                continue
            prev[_key((r.get("A") or "").strip(), (r.get("B") or "").strip())] = (
                d, (r.get("保留哪条") or "").strip())
    except Exception:
        return {}                        # 旧文件坏了 → 宁可让人重填也不写错决定
    return prev


def write_review_csv(path, cands, alias_map, file_paths=None):
    p = Path(path)
    prev = _read_previous(p)             # 先读旧决定，再覆盖写
    p.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 双击打开中文不乱码
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for c in cands:
            hit = "是" if (c["a"] in alias_map or c["b"] in alias_map) else "否"
            old = prev.get(_key(c["a"], c["b"]), ("", ""))
            w.writerow([c["a"], c["b"], c["score"], c["basis"],
                        _fmt_ev(c.get("ev_a"), c["a"], paths=file_paths),
                        _fmt_ev(c.get("ev_b"), c["b"], paths=file_paths),
                        "疑似同一家（%s）" % c["basis"], old[0], old[1]])
    return p


def read_review_csv(path):
    """读回人工决定。返回 rows（原列）+ 汇总。只认填了「决定」的行。"""
    p = Path(path)
    if not p.is_file():
        return [], {"合并": 0, "驳回": 0, "存疑": 0, "未填": 0}
    rows, stat = [], {"合并": 0, "驳回": 0, "存疑": 0, "未填": 0}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = (r.get("决定") or "").strip()
            keep = (r.get("保留哪条") or "").strip()
            row = {"a": (r.get("A") or "").strip(), "b": (r.get("B") or "").strip(),
                   "决定": d, "保留": keep}
            rows.append(row)
            if d in stat:
                stat[d] += 1
    return rows, stat
