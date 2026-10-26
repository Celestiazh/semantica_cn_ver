# -*- coding: utf-8 -*-
"""跨文件互证的"人名+单位"粘连剥离（Layer 1，治未来的新粘连，不靠黑名单打地鼠）。

背景（2026-09-30 实测）：A 类参会名单「…、骆永辉湖北妙润启力建设工程有限公司：李小林」
—— 人名列表末尾与下一家公司名之间**没有分隔符**，规则取"最短机构名"会把人名一起吞掉。
单文件内无法判定"骆永辉"是人名；但**剥离后的公司名在其它文件里独立出现过**（17E9E2D9
写的是干净的「湖北妙润启力建设工程有限公司：李小林」）—— 这就是拆分依据。

判据（全部确定性）：
  E = P + S，P 为 2~4 字纯中文且**不以行政区划词开头**（深圳/广东/…—— 防止把
  「深圳市博大建设集团」拆成 深圳+博大），S 能 fullmatch 单位名模式（unit_core），
  且 S 在**至少一个不包含 E 的文件**里作为单位实体出现过（跨文件互证）。
命中 → 实体 E 改名 S、边跟着改、新增 PERSON 节点 P（不带边 —— 归属待后续证据）。

配置：`extract.glue_split: true`（默认开）。全程留痕：每次拆分打印一条 INFO。
"""
import re

# 行政区划/集团词头 —— 这些开头**不是**人名（防止把合法全名拆坏）
GEO_WORDS = ("中国", "中建", "中铁", "中冶", "深圳", "广东", "广州", "北京", "上海",
             "湖北", "湖南", "山东", "山西", "南京", "苏州", "杭州", "福州", "厦门",
             "香港", "重庆", "成都", "武汉", "青岛", "郑州", "长沙", "江苏", "浙江",
             "广西", "云南", "贵州", "四川", "河南", "河北", "陕西", "安徽", "辽宁",
             "福建", "江西", "海尔", "华润")


def _is_person_like(p):
    return (2 <= len(p) <= 4) and all("\u4e00" <= ch <= "\u9fff" for ch in p) \
        and not p.startswith(GEO_WORDS)


def split_glued(graph, file_units, cfg):
    """graph: {entities, relationships}；file_units: {文件名: {ORG 名集合}}。

    返回 (graph, splits)；splits = [(人名, 剥离后单位名, 命中文件数)]。
    """
    mc = (cfg.get("extract.meeting") or {})
    alt = "|".join(mc.get("unit_suffixes") or
                   ["公司", "集团", "院", "所", "中心", "部", "局", "署", "大学"])
    core = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:" + alt + ")")

    ents = graph.get("entities") or []
    rels = graph.get("relationships") or []

    def files_of(name):
        return file_units.get(name) or set()

    splits, renames = [], {}
    for e in list(ents):
        if e.get("type") != "ORG":
            continue
        u = e.get("name") or ""
        if len(u) < 8 or u in renames:
            continue
        for L in (4, 3, 2):                      # 优先取长前缀（人名多为 3 字）
            p, s = u[:L], u[L:]
            if not _is_person_like(p):
                continue
            if not re.fullmatch(core, s):
                continue
            # 跨文件互证：S 在"没有 E"的文件里独立出现过
            if (files_of(s) - files_of(u)):
                renames[u] = s
                splits.append((p, s, len(files_of(s) - files_of(u))))
                break

    if not renames:
        return graph, splits

    # 实体改名（若 S 本身已是实体则合并掉 E）
    by_name = {e.get("name"): e for e in ents}
    for old, new in renames.items():
        e0 = by_name.get(old)
        if e0 is None:
            continue
        e0["name"] = new
        e0["id"] = new
        if new in by_name and by_name[new] is not e0:
            # S 已有实体：删掉改名后的重复项
            ents.remove(e0)
        else:
            by_name[new] = e0
    for r in rels:
        for k in ("source", "target"):
            if r.get(k) in renames:
                r[k] = renames[r[k]]

    # person 节点补充（上面循环里拆过的地方再兜一遍，避免重名误判）
    have = {e.get("name") for e in ents}
    for p, _s, _n in splits:
        if p not in have:
            ents.append({"id": p, "name": p, "type": "PERSON"})
            have.add(p)

    graph["entities"] = ents
    graph["relationships"] = rels
    return graph, splits
