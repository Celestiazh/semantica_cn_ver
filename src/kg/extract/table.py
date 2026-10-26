# -*- coding: utf-8 -*-
"""表格两列配对 —— 「单位 | 人员」表（江苏标准表、签到栏）的结构化直出

## 为什么需要它

江苏那套标准表把单位和人写成两列：

```
与会单位      与会人员
南京城建      居松
东南监理      丁国华
中建八局      方舟
```

**PDF 文字层里这两列本来分得清清楚楚**（实测：单位列 x≈204~257，人员列 x≈396~438，同一 y）。
问题出在我们自己身上 —— 解析时把同一视觉行的单元 `"".join()` 成 `南京城建居松`，x 被丢掉了。
所以 2026-09-28 在 parse 侧给行加了 `cells`（每个单元的 x 区间），本模块负责用它把两列还原。

⭐ **这一档是「结构化直出」（A 档）**：不加词、不猜、不调模型 —— 信息本来就在文件里。

## 判定条件（都要求，宁可漏也不错配）

1. **同一行里同时出现左表头与右表头**（左 ∈ 与会单位/参加单位/单位名称，右 ∈ 与会人员/参加人员/姓名）
2. 左表头 `x1` **严格小于**右表头 `x0`（两列真的分开）
3. 数据行按 x 归列后，**左侧必须是一个像机构名的串**（过 `org_keys` 校验），右侧必须切得出人名
4. 遇到 `注：` / `监制` 等收尾行就停

## ⛔ 不做的事

- **不把简称补成全称**（`中建八局` → `中国建筑第八工程局有限公司` 是**消解**问题，交给名册）
- **不硬切粘连值**：切不出人名就不产出，**宁缺不错配**（错配会把错误关系写进图里）
- 左栏竖排标签（`各与会单位及人员`、`签到栏`）**天然没有右列**，自动被跳过 —— 不会当成人名
"""
import re

DEFAULTS = {
    "enabled": True,
    "left_headers": ["与会单位", "参加单位", "单位名称", "参加单位名称"],
    "right_headers": ["与会人员", "参加人员", "人员姓名", "姓名", "人员名单"],
    "org_keys": ["公司", "集团", "局", "院", "部", "署", "中心", "队", "站", "所",
                 "监理", "城建", "建工", "设计", "咨询", "市政", "建设", "施工",
                 "工程", "管理", "研究院", "设计院", "总承包"],
    "skip_left_words": ["签到栏", "与会单位及人员", "与会单位", "与会人员", "参加单位",
                        "参加人员", "单位名称", "人数", "备注", "签字", "第X版"],
    "stop_markers": ["注：", "注:", "监制", "本表与"],
    "header_max_len": 8,      # 表头单元本身的长度上限（防长句被当成表头）
    "max_row_drop": 400,      # 距表头行最多往下找多少 pt（防跨出表格区）
    "min_org_len": 2,
    "max_org_len": 20,
}

PERSON_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")


def _cfg(ecfg):
    c = dict(DEFAULTS)
    c.update({k: v for k, v in ((ecfg or {}).get("table") or {}).items() if v is not None})
    return c


def _hit_any(text, words, max_len):
    """表头单元是否命中（子串匹配，且单元本身很短 —— 长句不算表头）。"""
    t = (text or "").strip()
    if not t or len(t) > max_len:
        return None
    for w in words:
        if w in t:
            return w
    return None


def _valid_org(s, c, stopwords):
    s = (s or "").strip()
    if not (c["min_org_len"] <= len(s) <= c["max_org_len"]):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9（）()·、\s]+", s):
        return False
    if any(w and w in s for w in c["skip_left_words"]):
        return False
    if set(s) & set("".join(stopwords or [])):
        # 只挡"纯停用词"，机构名里含"部/院/局"这类不能误挡
        if len(s) <= 2:
            return False
    return any(k in s for k in c["org_keys"])


def _pair_on_page(pg, c, stopwords):
    """在一个页面上找「两列成对」表，返回 [(org, person), ...]。"""
    rows = pg.get("rows") or []
    if not rows:
        return [], None
    if not any(r.get("cells") for r in rows):
        return [], "本页的行没有 cells（x 坐标）—— 需要重跑解析（parse `_v>=2`）"

    hdr_i = None
    for i, r in enumerate(rows):
        cells = r.get("cells") or []
        if len(cells) < 2:
            continue
        left = right = None
        for cl in cells:
            if left is None and _hit_any(cl.get("t"), c["left_headers"], c["header_max_len"]):
                left = cl
            elif right is None and _hit_any(cl.get("t"), c["right_headers"], c["header_max_len"]):
                right = cl
        # 两列真的分开才算（同一单元里就都命中 → 不是两列表）
        if left and right and left["x1"] < right["x0"]:
            hdr_i = i
            mid = (left["x1"] + right["x0"]) / 2.0
            lo = left["x0"] - 25
            break
    if hdr_i is None:
        return [], None

    y0 = rows[hdr_i].get("y") or 0
    pairs = []
    for r in rows[hdr_i + 1:]:
        txt = r.get("text") or ""
        if any(m in txt for m in c["stop_markers"]):
            break
        if (r.get("y") or 0) - y0 > c["max_row_drop"]:
            break
        lt, rt = [], []
        for cl in r.get("cells") or []:
            x0 = cl.get("x0") or 0
            if x0 >= mid:
                rt.append(cl.get("t") or "")
            elif x0 >= lo:
                lt.append(cl.get("t") or "")
        org = "".join(lt).strip(" \t　、，,。；;:：()（）")
        person_blob = "".join(rt).strip()
        if not org or not person_blob:
            continue
        if not _valid_org(org, c, stopwords):
            continue
        names = [m.group(0) for m in PERSON_RE.finditer(person_blob)]
        names = [n for n in names if n not in set(stopwords or [])]
        for n in names:
            pairs.append((org, n))
    return pairs, None


def extract_paired_table(dt, ecfg=None):
    """扫全文所有页，产出 {units, persons, pairs, roles, evidence, stats, warnings}。

    只产出**原文里明确成对**的（单位, 人），不做任何补全。
    """
    c = _cfg(ecfg)
    out = {"units": [], "persons": [], "pairs": [], "roles": [],
           "evidence": {}, "tables": 0, "warnings": []}
    if not c["enabled"]:
        return out

    stopwords = ((ecfg or {}).get("meeting") or {}).get("name_stopwords") or []
    role_hints = c.get("role_hints") or {}

    for pg in (dt.doc.get("pages") or []):
        pairs, err = _pair_on_page(pg, c, stopwords)
        if err:
            if err not in out["warnings"]:
                out["warnings"].append(err)
            continue
        if not pairs:
            continue
        out["tables"] += 1
        for org, person in pairs:
            if (org, person) in out["pairs"]:
                continue
            out["pairs"].append((org, person))
            if org not in out["units"]:
                out["units"].append(org)
            if person not in out["persons"]:
                out["persons"].append(person)
            # 角色只能从名称里**明确**看得出来时才推断（监理/设计/勘察/建设）
            for k, v in role_hints.items():
                if k in org:
                    if {"role": v, "unit": org} not in out["roles"]:
                        out["roles"].append({"role": v, "unit": org})
                    break
    return out


def evidence_for(dt, s):
    """在全文里找 s 的位置当出处；找不到就退回"第 1 页"。"""
    p = dt.text.find(s)
    if p < 0:
        return None
    return dt.evidence(p, p + len(s))
