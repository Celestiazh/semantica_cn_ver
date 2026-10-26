# -*- coding: utf-8 -*-
"""② 第 1 层 · 会议要素（**纯规则，不碰模型**）

抽出：会议时间 / 地点 / 主持人 / 记录人 / 期号 / 会议名 / 项目 / 参会单位 / 参会人 / 角色→单位
产出：entities（PERSON / ORG / MEETING / PROJECT）+ relationships，**每条关系带 evidence**

## ⭐ 规则从配置读（2026-09-28 重构）

所有**词表**都在 `settings.yaml` 的 `extract.meeting` 段：单位后缀、角色词、参会段表头的
前缀×后缀、主持人/记录人/时间/地点的叫法、章节角色词、"本文无名单"标记、内容过少门限。
→ **加词不改代码**。搬进配置的直接原因：角色词表曾在代码与配置里各存一份、**已经不一致**（bug）。

本文件里保留的只有**结构模式**（语序、括号语义、分隔符），因为那是"模板的骨架"，不是词：
  A `单位：人名`            B `（单位）：人名`
  ⭐ C `单位（角色）：人名`   （docx 常见；括号里是角色不是单位）
  第 5 套表头写法：`【会议时间】<Tab>值`（**没有冒号**）—— 由 `_fld()` 统一支持

## ⛔ 三条用血换来的教训

1. **枚举叫法救不了模板多样性**：原来写死「参会单位及人员」，而新 docx 写「**参加**单位及人员」
   → 整份文件 0 单位 0 人。凡"叫法"必做**组合覆盖**，且放进配置。
2. **换结构要加模式，加词没用**：`单位（角色）：人名` 与 A/B 两种语序都不同，加多少词都不匹配。
3. **项目名不会靠"最长"选对**：改过四轮才稳（详见 `pick_project` 的注释）。

## ⚠️ 数据质量要显式标记（不能静默空）

- 参会段写「详见签到表」→ `no_attendee_list`（**本文确实没有名单**，不是我们没抽到）
- 全文 < 200 字 → `too_short`（空壳/占位件；实测 68 个 docx 里有 23 个是 47~67 字的空壳）
"""
import re
from pathlib import Path

from . import table

# ---------------------------------------------------------------- 内置兜底
# 配置里缺哪一项就用这里的；完整词表在 config/settings.yaml
DEFAULTS = {
    "name_stopwords": ["等", "各", "单位", "无", "及", "和", "代表", "人员", "相关", "专家", "负责人", "经理"],
    "unit_suffixes": ["公司", "集团", "大学", "学院", "科学院", "监理部", "项目部", "设计院",
                      "研究院", "中心", "事务所", "管理局", "局", "站", "署", "委员会", "管委会",
                      "指挥部", "办公室", "管理处", "分局", "大队", "支队", "总队", "工区", "标段"],
    "role_words": ["建设单位", "全咨单位", "监理单位", "总包单位", "施工单位", "设计单位",
                   "勘察单位", "审计单位", "项目监理机构", "代建单位", "业主单位", "咨询单位",
                   "幕墙单位", "高低压单位", "高效机房单位", "分包单位", "项目管理单位"],
    "attend_prefix": ["参会", "参加", "与会"],
    "attend_suffix": ["单位及人员", "单位及参会人员", "单位及代表", "单位及部门", "人员", "单位", "代表"],
    "host_words": ["主持人", "会议主持", "主持"],
    "recorder_words": ["记录人", "会议记录", "记录员", "记录"],
    "time_words": ["会议时间", "时间", "开会时间", "会议日期"],
    "place_words": ["会议地点", "地点", "会议地址"],
    "period_words": ["期", "次"],
    "section_role_words": ["建设单位", "全咨单位", "监理单位", "总包单位", "施工单位",
                           "设计单位", "勘察单位", "项目监理机构", "项管单位"],
    "no_attendee_markers": ["详见签到表", "见签到表", "签到表", "详见会议签到表", "附件", "略"],
    "min_content_chars": 200,
    "meeting_aliases": {"监理例会": "监理周例会", "监理周会": "监理周例会", "监理会议纪要": "监理周例会"},
    # ⭐ 正文「角色词+单位全名」无冒号写法（广东建工/坪山/江苏都是这个写法，实测 25 份）
    "role_scan_max_gap": 40,
    "role_scan_reject": ["汇报", "要求", "整改", "完成", "督促", "尽快", "本周", "上周", "下周",
                         "情况", "工作", "事宜", "问题", "安排", "计划", "落实", "加强", "进行",
                         "组织", "负责", "参与", "分别", "等"],
    # 单位全称的"续接段"：非贪婪先取最短机构名，再用这些把它接回全称
    # ⛔ 不做这一步会抽出截断名（`广东省建筑工程集团`），那是**另一个实体**
    "role_org_continuations": ["有限责任公司", "股份有限公司", "有限公司", "公司",
                               "集团", "总院", "分公司"],
    # ⭐ 表头区「字段名+值」无冒号写法（坪山：`项目名称坪山交警大队营房建设项目`）
    "header_region_end": ["会议纪要内容", "会议内容", "会议主要内容", "纪要内容",
                          "会议要求", "会议纪要正文"],
    # 字段名 -> [写到 info 的哪个键, 值的形状]
    "header_fields": {
        "项目名称": ["项目", "text"],
        "工程名称": ["项目", "text"],
        "会议名称": ["会议", "text"],
        "会议时间": ["会议时间", "date"],
        "时间": ["会议时间", "date"],
        "会议地点": ["会议地点", "text"],
        "地点": ["会议地点", "text"],
        "主持人": ["主持人", "name"],
        "会议主持": ["主持人", "name"],
        "记录人": ["记录人", "name"],
        "记录": ["记录人", "name"],
        "会议编号": ["期号", "period"],
        "编号": ["期号", "period"],
        "签发人": ["签发人", "name"],
        "组织单位": ["组织单位", "text"],
    },
}


def _loose(word):
    """词内允许空格：「时 间」「主 持 人」都能匹配。"""
    return r"\s*".join(re.escape(c) for c in word)


def _alt(words):
    """按长度倒序拼 alternation（长词优先，避免「时间」吃掉「会议时间」）。"""
    return "|".join(_loose(w) for w in sorted(set(words), key=len, reverse=True))


def _fld(name_pat):
    """字段前缀：支持 `【会议时间】<Tab>值`（无冒号）与 `会议时间：值` 两种。

    ⛔ 踩坑（2026-09-28）：词表分支**必须包在非捕获组里**，否则 `|` 的优先级会把模式拆坏：
        想要 `(?:【会议主持】|会议主持\\s*[：:])`
        写成 `(?:【...】|会议主持|主持\\s*[：:])` ← 「会议主持」这条就**不再要求冒号**，
        于是 `会议主持人：张驰` 反而匹配不上（先匹配到"主持人"又找不到冒号）。
    症状是「主持人/记录人/参会段 大面积抽不到」，而根因只是少了两层括号。
    """
    return (r"(?:(?:【\s*(?:" + name_pat + r")\s*】\s*)"
            r"|(?:(?:" + name_pat + r")\s*[：:]\s*))")


DATE_PAT = r"[0-9]{4}\s*[年.\-]\s*[0-9]{1,2}\s*[月.\-]\s*[0-9]{1,2}\s*[日号]?"
NAME_PAT = r"[\u4e00-\u9fa5、，,和及\s]{2,60}"


class Rules:
    """按配置编译好的正则集合（同配置只编译一次）。"""

    def __init__(self, ecfg=None):
        e = dict(DEFAULTS)
        e.update({k: v for k, v in (ecfg or {}).get("meeting", {}).items() if v is not None})
        self.e = e
        self.stopwords = set(e.get("name_stopwords") or [])

        unit_core = ("[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:"
                     + _alt(e["unit_suffixes"]) + ")")
        self.unit_core = unit_core

        self.RE_TIME = re.compile(_fld(_alt(e["time_words"])) + "(" + DATE_PAT + ")")
        self.RE_PLACE = re.compile(_fld(_alt(e["place_words"]))
                                   + r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,20})")
        self.RE_HOST = re.compile(_fld(_alt(e["host_words"]))
                                  + r"([\u4e00-\u9fa5]{2,4}?)(?=[记参会签项项\s，。、；]|$)")
        self.RE_RECORDER = re.compile(_fld(_alt(e["recorder_words"]))
                                      + r"([\u4e00-\u9fa5]{2,4}?)(?=[参会签发时\s，。、；]|$)")

        # 期号三种来源：① 【2026】第1期 ② 编号：164 ③ 第七十次
        self.RE_PERIOD = re.compile(r"[【\[]\s*(\d{4})\s*[】\]]\s*第\s*(\d+)\s*期")
        self.RE_PERIOD2 = re.compile(r"编\s*号\s*[：:]?\s*([0-9]{1,6})\s*号?")
        self.RE_PERIOD3 = re.compile(r"第\s*([0-9一二三四五六七八九十百零〇]{1,5})\s*[次期]")

        # 参会段：前缀 × 后缀组合（3×7=21 种写法）
        combos = [p + s for p in e["attend_prefix"] for s in e["attend_suffix"]]
        combos += list(e["attend_suffix"])
        self.RE_ATTEND = re.compile(
            _fld(_alt(combos)) + r"(.+?)"
            r"(?=(?:会议主要内容|会议内容|会议议题|会议主题|会议议程|议题|纪要内容|一、|$))", re.S)

        # 单位-人 三种写法
        self.RE_UP_A = re.compile(r"(" + unit_core + r")\s*[：:]\s*(" + NAME_PAT + r")")
        self.RE_UP_B = re.compile(r"[（(](" + unit_core + r")[）)]\s*[：:]\s*(" + NAME_PAT + r")")
        self.RE_UP_C = re.compile(r"(" + unit_core + r")\s*[（(]([^）)]{2,10})[）)]\s*[：:]\s*"
                                  r"([\u4e00-\u9fa5、，,和及]{2,80})")

        # 角色 → 单位 / （角色）→ 单位
        self.RE_ROLE = re.compile(r"(" + _alt(e["role_words"]) + r")\s*[：:]\s*(" + unit_core + r")")
        self.RE_ROLE_B = re.compile(r"[（(]\s*(" + _alt(e["role_words"]) + r")\s*[）)]\s*[：:]\s*("
                                    + unit_core + r")")
        self.RE_ROLE_ANY = re.compile("(" + _alt(e["role_words"]) + ")")

        self.RE_PAREN_UNIT = re.compile(r"[（(](" + unit_core + r"[^）)]{0,20})[）)]")
        self.RE_CHAPTER = re.compile(r"([一二三四五六七八九十]+、[\u4e00-\u9fa5]{2,16}"
                                     r"(?:汇报|要求|发言|总结|说明|意见|情况|部署|报告|安排))")
        self.RE_PROJECT = re.compile(r"([\u4e00-\u9fa5]{4,30}?(?:项目|工程|校区))")
        self.RE_MEETING = re.compile(r"(监理\s*[周月例]\s*例会|监理例会|项目管理例会|监理会议纪要|会议纪要)")
        self.RE_PROJECT_LABEL = re.compile(r"(?:项目名称|工程名称)\s*[：:]?\s*")


RULES_CACHE = {}


def build_rules(ecfg=None):
    key = repr(sorted(((ecfg or {}).get("meeting") or {}).items(), key=lambda x: x[0]))
    if key not in RULES_CACHE:
        RULES_CACHE[key] = Rules(ecfg)
    return RULES_CACHE[key]


# ---------------------------------------------------------------- 小工具
JUNK_PROJECT = re.compile(r"会议|纪要|例会|监理|通知|清单|汇报|附件|^[0-9.]+$")
# 单位名后面被粘住的垃圾标记（表格式文本会把「详见会议签到表」直接接在单位名后面）
JUNK_IN_NAME = re.compile(r"(?:详见)?(?:会议)?签到表|附件|另行通知|略")
PROJECT_BAD = re.compile(r"汇报|会议|纪要|通知|清单|要求|例会|签到|附件|日前|上报|完成|累计|本周|上周|下周|上旬|中旬|下旬")
# ⛔ 项目名人工指认表（2026-10-13）：文件名关键词 → 项目名，pick_project 最高优先级。
# 背景：部分纪要正文不写项目名，文件名括号里只有「施工单位：xxx」——已禁止从那里抓项目名
#      （宁缺勿滥），这类文件靠本表人工指认真实项目名。条目由人工维护。
_PROJECT_OVERRIDES = None

def _project_overrides():
    global _PROJECT_OVERRIDES
    if _PROJECT_OVERRIDES is None:
        _PROJECT_OVERRIDES = {}
        p = Path("config") / "projects_override.yaml"
        if p.is_file():
            try:
                import yaml
                _PROJECT_OVERRIDES = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:                            # noqa: BLE001
                _PROJECT_OVERRIDES = {}
    return _PROJECT_OVERRIDES

UNIT_LIKE_TAIL = re.compile(r"(?:有限责任公司|有限公司|集团公司|监理咨询|设计院|研究院).*?(?:项目|工程|校区)$")


def names_of(blob, stopwords=None):
    """从「人名、人名；人名」这类串里切人名。"""
    stop = stopwords or DEFAULTS["name_stopwords"]
    out = []
    for chunk in re.split(r"[、，,和及\s]+", blob or ""):
        c = chunk.strip().strip("。；;等").strip()
        if 2 <= len(c) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]+", c) and c not in stop:
            out.append(c)
    return out


def clean_unit(unit):
    u = (unit or "").strip().strip("：:（()）")
    u = re.sub(r"^(责任|主体|建设|监理|施工|设计|勘察|总包|全咨|审计)单位", "", u)
    # ⛔ 盖章残留（实测 2026-09-28）：落款区的「…工程项目监理部 (章)」会被 RE_PAREN_UNIT
    #    抓成单位名（产出过 `EPC）工程项目监理部 (章` / `项目监理部 (章`）—— "（章）"是
    #    盖章标记，不是单位。在公共清洗函数里拦，所有抽单位名的路径都自动生效。
    if "(章" in u or "（章" in u:
        return ""
    return u.strip()


UNIT_JUNK = re.compile(r"如下|纪要|会议|签到|汇报|要求|表决|讨论|报送|提交|上报|台账|覆盖|巡查|恢复|敷设|堆载|压实")


def sanitize_units(units, persons, rels, roles, rules):
    """⭐ 单位名合法性防线（抽取层的最后一道关）—— **个例残余交给 ③ 消解去弹人工**。

    实测抓出来的 junk（2026-09-28）：OCR 噪声文件里
      「反恐）基地一期项目工程例会在警训基地项目部会议室召开，纪要如下：一、中建科工：（一」
      「1）施工月报每月25日施工单位向总监办及项目公司报送施工月报（电子版和纸质版」
    这类**正文长句 / 编号条目**被当成了单位名。

    规则全部是确定性的：
      丢 —— 超过 30 字 / 含冒号 / 含句子词（如下、纪要、会议…）/ 以闭括号开头
      切 —— 「人名+单位」粘连（前缀 2~4 字且**已在本文人名集合里**，剩余部分仍是合法单位名）
            实测：`景清涛中建科工集团有限公司` → 人名 景清涛 + 中建科工集团有限公司
    """
    pset = set(persons)
    # ⭐ 人工确认过的"人名前缀"（配置驱动）：有些文件里行级解析回退、粘连人名不在本文
    #    人名集合里 → 用这份名单兜底。只做"切开"，不做合并 —— 判据仍要求剩余部分是合法单位名。
    known_prefixes = set(rules.e.get("person_prefixes") or [])
    rename, drop = {}, set()
    for u in list(units):
        if (len(u) > 30 or "：" in u or ":" in u or UNIT_JUNK.search(u)
                or u[0] in "）)" or "(章" in u or "（章" in u):
            drop.add(u)
            continue
        # ⭐ 白名单：**长名必须整体是合法单位名结构**（名称+单位后缀收尾）。
        #    黑名单打不完 —— OCR 乱码（`上林车北投集团广西鞋通工理集团有限公司导段`）、
        #    正文句（`于8月8日前提交材料进场及送检台账至监理部`）各有各的形态。
        #   ⛔ 只对**长名**生效：表格来的简称（南京城建 / 东南监理 / 中建八局）不以标准后缀
        #      收尾，若全名都要求 fullmatch 会被误杀。
        if len(u) > 15 and not re.fullmatch(rules.unit_core, u):
            drop.add(u)
            continue
        for L in (4, 3, 2):                      # 人名粘连：优先取长前缀
            if len(u) > L + 5 and (u[:L] in pset or u[:L] in known_prefixes):
                tail = u[L:]
                if (re.fullmatch(rules.unit_core, tail)
                        and not UNIT_JUNK.search(tail)):
                    rename[u] = tail
                    persons.append(u[:L])        # 被切出来的人名归位
                    break
    if not rename and not drop:
        return units, persons, rels, roles, {}

    def fix(x):
        return rename.get(x, x)

    units2 = []
    for u in units:
        if u in drop:
            continue
        u2 = fix(u)
        if u2 not in units2:
            units2.append(u2)
    rels2 = []
    for r in rels:
        s, t = fix(r["source"]), fix(r["target"])
        if s in drop or t in drop:
            continue                              # 引用了 junk 实体的边一并丢弃
        r["source"], r["target"] = s, t
        rels2.append(r)
    roles2 = []
    for r in roles:
        u2 = fix(r["unit"])
        if u2 in drop:
            continue
        r["unit"] = u2
        roles2.append(r)
    return units2, persons, rels2, roles2, {"dropped": sorted(drop), "cut": dict(rename)}


def project_from_filename(file_name):
    """从导出文件名兜底取项目名（`{32位ID}-{日期?}{项目}{日期?}{文种}`，两种顺序都有）。"""
    stem = re.sub(r"\.(pdf|docx)$", "", file_name or "", flags=re.I)
    stem = re.sub(r"^[0-9A-Fa-f]{32}-", "", stem)
    stem = re.sub(r"^\d{4}\s*[.\-年]\s*\d{0,2}\s*[.\-月]?\s*\d{0,2}\s*[日号]?", "", stem).strip()
    m = re.match(r"^(.{3,30}?)\s*(?:\d{4}\s*[年.\-]\s*\d{1,2}|监理|会议|例会|纪要|$)", stem)
    cand = (m.group(1) if m else "").strip(" -_、,，")
    if not (3 <= len(cand) <= 30) or JUNK_PROJECT.search(cand):
        return ""
    return cand


def _clean_project_cand(c):
    c = re.sub(r"^[期号第\s]+", "", c or "").strip()
    return re.sub(r"^(?:项目名称|工程名称|工程|项目)[：:]?", "", c).strip()


def pick_project(text, file_name, rules):
    """挑项目名。**正文按位置打分 + 文件名给固定分**（改过四轮，别再动权重不看注释）。

    ⛔ 四轮教训：① 只取第一个 → 抽到章节标题；② 拿长度当主判据 → 抽到正文半句；
    ③ 文件名候选参与位置竞争 → 选出 `20260422`、`监理` 这类垃圾前缀；
    ④ 干脆删掉文件名候选 → **只在文件名里的名字也没了**（如「齐鲁工大长清校区」）。
    """
    stem = re.sub(r"\.pdf$|\.docx$", "", file_name or "")
    # ① 人工指认表最高优先级（关键词命中文件名即采用）
    for key, proj in _project_overrides().items():
        if key and key in (file_name or ""):
            return proj
    # ② 文件名括号里的「角色词：公司名」是元数据注记，不是项目名 —— 扫描前剔除
    #    （实测 2026-10-13：青岛地铁 31 份被抽成「中国建筑第八工程」）
    stem = re.sub(r"（[^（）]*?(?:施工单位|监理单位|建设单位|代建单位|设计单位|总承包单位)\s*[：:][^（）]*）",
                  "（元数据注记）", stem)
    stem = re.sub(r"\([^()]*?(?:施工单位|监理单位|建设单位|代建单位|设计单位|总承包单位)\s*[：:][^()]*\)",
                  "(元数据注记)", stem)
    lab = rules.RE_PROJECT_LABEL.search(text)
    cands = [(m.group(1), 20.0 - m.start() / 40.0) for m in rules.RE_PROJECT.finditer(text)]
    cands += [(m.group(1), 15.0) for m in rules.RE_PROJECT.finditer(stem)]
    fn = project_from_filename(file_name)
    if fn:
        cands.append((fn, 15.0))

    best, best_score, seen = None, -10 ** 6, set()
    for raw, base in cands:
        c = _clean_project_cand(raw)
        if len(c) < 3 or c in seen:
            continue
        seen.add(c)
        score = base + min(len(c), 12) * 0.5
        if c in stem:
            score += 8
        if PROJECT_BAD.search(c):
            score -= 30
        if UNIT_LIKE_TAIL.search(c):
            score -= 30
        if lab and re.search(re.escape(lab.group(0)) + re.escape(c), text):
            score += 10
        if score > best_score:
            best, best_score = c, score
    return best


def role_from_section(section, rules):
    """章节标题 → 发言方角色（`一、施工单位汇报` → 施工单位）。用来给条目定「提出方」。"""
    if not section:
        return None
    m = rules.RE_ROLE_ANY.search(section)
    return m.group(1) if m else None


def scan_role_units(text, rules):
    """正文里「角色词 + 单位全名」（**无冒号**）—— 广东建工/坪山/江苏三套模板都这么写。

    真实写法：
        监理单位召集建设单位广州建鑫嵘赋有限公司、施工总承包单位广东省建筑工程集团有限公司、
        设计单位广东省建科建筑设计院有限公司、钢结构单位中建钢构股份有限公司…
        参加单位名称参加人员建设单位深圳市坪山区建筑工务局监理单位深圳市中行建设工程顾问有限公司…

    ⭐ 做法：按角色词的**出现位置切段**，每段只取到下一个角色词之前（或 40 字以内）。
    ⛔ 不能写成 `(角色词)(单位名)` 直接匹配：`监理单位召集建设单位广州建鑫嵘赋有限公司`
       会把「召集建设单位…」整个当成单位名（末尾恰好也是"公司"）。切段后 `监理单位` 那段只剩
       「召集」→ 自动被判无效。

    ⛔ 三道防线（都是实测踩出来的）：
      1. **不能贪婪**：`{2,40}` 贪婪匹配会把「迅达（中国）电梯有限公司**在广东建工科创大厦项目部**」
         整段吞掉（因为尾巴里也有算子后缀的词）。所以用**非贪婪**取最短机构名，
         再用 `role_org_continuations`（有限公司/股份有限公司…）**定向续接**成全称。
         否则会抽出 `广东省建筑工程集团`（截断）—— 截断名会变成**另一个实体**，加重消解负担。
      2. **先在被粘住的垃圾标记处切断**：表格式文本会把「详见会议签到表」粘在单位名后面。
      3. **匹配后一个字符要是自然边界**（顿号/逗号/句号/介词/段尾）：`严格执行公司制度` 这种
         短语里的「公司」不会被当单位（后面跟着「制」）。
    """
    e = rules.e
    gap = int(e.get("role_scan_max_gap") or 40)
    reject = e.get("role_scan_reject") or []
    conts = e.get("role_org_continuations") or ["有限责任公司", "股份有限公司", "有限公司",
                                                "公司", "集团", "总院", "分公司"]
    boundary = "、，,。；;：:()（） \t\n在由于与和及对为等的/【】[]"
    hits = [(m.start(), m.end(), m.group(0)) for m in rules.RE_ROLE_ANY.finditer(text)]
    out = []
    for i, (_s, end, role) in enumerate(hits):
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        seg = text[end:min(nxt, end + gap)]
        junk = JUNK_IN_NAME.search(seg)          # ① 粘住的"详见签到表"之类先切掉
        if junk:
            seg = seg[:junk.start()]
        seg = seg.strip(" \t　、，,。；;:：()（）")
        if not seg:
            continue
        m = re.match(rules.unit_core, seg)       # ② 非贪婪：先取最短机构名
        if not m:
            continue
        unit = m.group(0)
        rest = seg[m.end():]                     # ③ 定向续接成全称（最多两段）
        for _ in range(2):
            for c in conts:
                if rest.startswith(c):
                    unit += c
                    rest = rest[len(c):]
                    break
            else:
                break
        if not (4 <= len(unit) <= 40):
            continue
        if any(w in unit for w in reject):       # ④ 「组织劳务公司」这类短语挡掉
            continue
        base = m.group(0)
        after = seg[m.end() + (len(unit) - len(base)):][:1]
        if after and after not in boundary:      # ⑤ 后面必须自然收住
            continue
        out.append({"role": re.sub(r"\s+", "", role), "unit": unit,
                    "start": end, "end": end + len(unit)})
    return out


def _shape(val, kind):
    """按字段类型截取值的形状 —— 表头区里字段是连写的，只能靠形状断句。"""
    v = (val or "").strip()
    if not v:
        return ""
    if kind == "date":
        m = re.match(DATE_PAT, v)
        return m.group(0).replace(" ", "") if m else ""
    if kind == "name":
        m = re.match(r"[\u4e00-\u9fa5]{2,4}", v)
        return m.group(0) if m else ""
    if kind == "period":
        m = re.match(r"[0-9]{1,6}", v)
        return m.group(0) if m else ""
    v = re.split(r"[\n。]", v)[0]
    return v[:40].strip()


def scan_header_fields(text, rules):
    """表头区「字段名 + 值」**无冒号**写法（坪山：`项目名称坪山交警大队营房建设项目`）。

    做法：在有界的"表头区"内，把已知字段名按**出现位置**排序（长的优先、不重叠），
          取两个字段名之间的文本作为前一个字段的值，再按字段类型截形状。

    ⛔ 必须限定在表头区（"会议纪要内容"之前）。全域扫会大面积误伤 ——
       正文里「时间」「记录」「要求」这类词到处都是。
    ⛔ 值要按类型截：`时间2026年08月21日项目部会议室参加单位名称参加人员建设单位…` 这种
       粘一串的，只有"只取日期形状"才能拿到正确值。
    """
    e = rules.e
    fields = e.get("header_fields") or {}
    if not fields:
        return {}
    end = len(text)
    for w in (e.get("header_region_end") or []):
        m = re.search(_loose(w), text)
        if m:
            end = min(end, m.start())
    head = text[:end]
    names = sorted(fields.keys(), key=len, reverse=True)
    pat = re.compile("|".join("(" + _loose(n) + ")" for n in names))
    hits = []
    for m in pat.finditer(head):
        if hits and m.start() < hits[-1][1]:        # 与上一个命中重叠 → 跳过（长词已优先）
            continue
        hits.append((m.start(), m.end(), re.sub(r"\s+", "", m.group(0))))

    out = {}
    for i, (_s, en, raw) in enumerate(hits):
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(head)
        key, kind = fields[raw]
        v = _shape(head[en:nxt].strip(" \t　:：、，,。；;"), kind)
        if v and not out.get(key):                  # 第一个拿到有效值的胜出
            out[key] = v
    return out


def _attendee_sources(dt, body, abs_start):
    """参会段的解析单元列表：(文本, 该文本在 dt.text 里的绝对起点)。

    ⭐ 能还原成"行"就**逐行**（治粘连，见 `doctext.DocText` 的说明）；
       还原不出（跨页 / 内容对不上）就**回退整段** —— 宁可回到旧行为，也不错切。

    两个对齐细节（都是实测踩出来的）：
    1. **第一行允许行内偏移** —— 字段名前缀（如「【人员】：」）不在 blob 里
       （RE_ATTEND 的 blob 从字段名之后开始），所以第一行要从"blob 首字符在行内的位置"起算；
    2. **最后一行要截断** —— blob 的终点（会议内容等）可能落在某行的中间，
       该行剩余部分是正文，拿去匹配会把正文里的内容误当名单。
    """
    flat = re.sub(r"\s+", "", body)
    if len(flat) < 8 or not getattr(dt, "line_texts", None):
        return [(body, abs_start)]
    pos = dt.lines_text.find(flat[:6])
    if pos < 0:
        return [(body, abs_start)]
    li = dt.lines_text.rfind("\n", 0, pos) + 1

    res, idx, taken = [], li, 0
    while idx < len(dt.line_texts) and taken < len(flat):
        line = dt.line_texts[idx]
        lf = re.sub(r"\s+", "", line)
        if not lf:
            idx += 1
            continue
        s = 0
        if not res:                              # 第一行：允许行内偏移
            j = lf.find(flat[:6])
            if j < 0:
                return [(body, abs_start)]
            s = j
        seg_flat = lf[s:]
        n = min(len(seg_flat), len(flat) - taken)
        if flat[taken:taken + n] != seg_flat[:n]:  # 内容对不上 → 回退
            return [(body, abs_start)]
        res.append((line, s, n))
        taken += n
        idx += 1
    if taken < len(flat):                        # 行用完了 blob 还没覆盖完 → 回退
        return [(body, abs_start)]

    out = []
    for line, s, n in res:
        src = _cut_at_nonws(line, s, n)          # 末行截掉 blob 之后的正文
        o = body.find(re.sub(r"\s+", "", src))
        if o < 0:                                # 在 blob 里定位不到 → 回退
            return [(body, abs_start)]
        out.append((src, abs_start + o))
    return out


def _cut_at_nonws(line, start_nonws, n_nonws):
    """取 line 中"第 start_nonws 个非空白字符起、共 n_nonws 个非空白字符"的那段（含其间空白）。"""
    cnt, begin, end = 0, None, None
    for i, ch in enumerate(line):
        if ch.isspace():
            continue
        cnt += 1
        if cnt == start_nonws + 1:
            begin = i
        if cnt == start_nonws + n_nonws:
            end = i + 1
            break
    if begin is None:
        return line
    return line[begin:end] if end else line[begin:]


# ---------------------------------------------------------------- 主流程
def extract_meeting(dt, ecfg=None):
    """返回 (entities, relationships, info, stats)。"""
    rules = build_rules(ecfg)
    text = dt.text
    e = rules.e
    aliases = e.get("meeting_aliases") or {}

    info, rels, warnings = {}, [], []
    units, persons, attendee_persons, person_ev, roles = [], [], [], {}, []

    def add(store, name):
        if name and name not in store:
            store.append(name)

    # ---- 会议要素 ----
    m = rules.RE_HOST.search(text)
    info["主持人"] = m.group(1) if m else None
    m = rules.RE_RECORDER.search(text)
    info["记录人"] = m.group(1) if m else None
    m = rules.RE_TIME.search(text)
    info["会议时间"] = m.group(1).replace(" ", "") if m else None
    m = rules.RE_PLACE.search(text)
    info["会议地点"] = m.group(1) if m else None

    m = rules.RE_PERIOD.search(text)
    if m:
        info["期号"] = "%s年第%s期" % (m.group(1), m.group(2))
    else:
        m2 = rules.RE_PERIOD2.search(text) or rules.RE_PERIOD3.search(text)
        yr = re.search(r"(\d{4})\s*年", info.get("会议时间") or "")
        info["期号"] = ("%s年第%s" % (yr.group(1), m2.group(1)) if (m2 and yr)
                        else (m2.group(1) if m2 else None))

    cands = [re.sub(r"\s+", "", x) for x in rules.RE_MEETING.findall(text)]
    cands = [c for c in cands if c != "会议纪要"]
    meeting = max(set(cands), key=len) if cands else (
        "会议纪要（未识别具体名称）" if "会议纪要" in text else None)
    # ⛔ 必须把归一结果**写回变量**：只改 info 不改变量，会导致
    #    "报告里写监理周例会、图里却建了监理例会节点" —— 同一会议裂成两个节点（2026-09-28 踩过）
    if meeting:
        meeting = aliases.get(meeting, meeting)
    info["会议"] = meeting
    info["项目"] = pick_project(text, dt.file, rules)

    # ⭐ 表头区「字段名 + 值」（无冒号）兜底 —— **只补正则没拿到的字段**，不覆盖已有结果
    for k, v in scan_header_fields(text, rules).items():
        if not info.get(k):
            info[k] = v

    # ---- 参会段 ----
    seg = rules.RE_ATTEND.search(text)
    info["参会段"] = re.sub(r"\s+", " ", seg.group(0))[:60] if seg else None
    if not seg:
        # ⛔ 没找到参会段时，**先分清"原文没有"还是"写法不认识"**（2026-09-28）：
        #    林宁高速写的是「总监办主要人员…派人参加（**签到表附后**）」—— 根本没有名单可抽，
        #    但旧口径报成"未找到参会段（表头写法可能是新的）" → **把数据问题说成规则问题**。
        gmark = [m for m in (e.get("no_attendee_markers") or []) if m in text]
        if gmark:
            warnings.append("本文无参会名单（原文写：%s）—— 签到表不可用，故此人/单位本来就抽不到"
                            % gmark[0])
        else:
            warnings.append("未找到参会段（表头写法可能是新的，看 info.参会段 是否为空）")
    else:
        body = seg.group(1)
        markers = [x for x in (e.get("no_attendee_markers") or []) if x in body[:60]]
        if markers or len(body.strip()) < 12:
            warnings.append("本文无参会名单（原文写：%s）—— 签到表不可用，故此人/单位本来就抽不到"
                            % (markers[0] if markers else body.strip()[:20]))

        # ⭐ 名单按"行"解析（治粘连，2026-09-28）：参会名单是"每行一个单位块"的**列表型内容**，
        #    而段落是视觉行**无缝拼接**的 → 上一行末尾的人名会粘上下一行的单位名，
        #    被 RE_UP_A 当成单位（实测产出「景清涛中建科工集团有限公司」，还顺带把真正的
        #    「中海监理有限公司」弄丢了）。行边界在 dt.line_texts 里有，还原出来逐行匹配；
        #    还原不出就回退整段（_attendee_sources 自己会回退）。
        sources = _attendee_sources(dt, body, seg.start(1))

        def collect_unit(unit, blob, bstart, sbase):
            u = clean_unit(unit)
            if len(u) < 4:
                return
            add(units, u)
            ev = dt.evidence(sbase + bstart, sbase + bstart + len(blob))
            for pn in names_of(blob, rules.stopwords):
                add(persons, pn)
                if pn not in attendee_persons:
                    attendee_persons.append(pn)
                person_ev.setdefault(pn, ev)
                rels.append({"source": pn, "target": u, "type": "所属单位",
                             "properties": {"evidence": ev}})

        for src, sbase in sources:
            for rx in (rules.RE_UP_C, rules.RE_UP_A, rules.RE_UP_B):
                for mm in rx.finditer(src):
                    if rx is rules.RE_UP_C:
                        role, blob, bstart = mm.group(2), mm.group(3), mm.start(3)
                    else:
                        role, blob, bstart = None, mm.group(2), mm.start(2)
                    collect_unit(mm.group(1), blob, bstart, sbase)
                    if role:
                        ru = clean_unit(mm.group(1))
                        if len(ru) >= 4:
                            roles.append({"role": role.strip(), "unit": ru,
                                          "evidence": dt.evidence(sbase + mm.start(),
                                                                  sbase + mm.end())})

    # ---- 角色 → 单位（全页扫）----
    for rx in (rules.RE_ROLE, rules.RE_ROLE_B):
        for mm in rx.finditer(text):
            role, unit = mm.group(1), clean_unit(mm.group(2))
            if len(unit) >= 4:
                roles.append({"role": role, "unit": unit,
                              "evidence": dt.evidence(mm.start(), mm.end())})
                add(units, unit)
    for mm in rules.RE_PAREN_UNIT.finditer(text):
        u = clean_unit(mm.group(1))
        if len(u) >= 6:
            add(units, u)

    # ---- ⭐ 正文「角色词+单位」（**无冒号**）----
    # 广东建工 / 坪山 / 江苏三套模板都是这个写法，实测 25 份文件因此 0 单位 0 人
    for r in scan_role_units(text, rules):
        if not any(x["role"] == r["role"] and x["unit"] == r["unit"] for x in roles):
            roles.append({"role": r["role"], "unit": r["unit"],
                          "evidence": dt.evidence(r["start"], r["end"])})
        add(units, r["unit"])

    # ---- ⭐ 两列表格（单位 | 人员）**结构化直出** ----
    # 江苏标准表那种：坐标本来分得开，是结构化信息，不猜不补
    tb = table.extract_paired_table(dt, ecfg)
    for u in tb["units"]:
        add(units, u)
    for p in tb["persons"]:
        add(persons, p)
        if p not in attendee_persons:
            attendee_persons.append(p)
    for org, person in tb["pairs"]:
        ev = table.evidence_for(dt, org) or table.evidence_for(dt, person)
        rels.append({"source": person, "target": org, "type": "所属单位",
                     "properties": {"evidence": ev}})
        person_ev.setdefault(person, ev)
    for r in tb["roles"]:
        if not any(x["role"] == r["role"] and x["unit"] == r["unit"] for x in roles):
            roles.append({"role": r["role"], "unit": r["unit"],
                          "evidence": table.evidence_for(dt, r["unit"])})
    for w in tb["warnings"]:
        if w not in warnings:
            warnings.append(w)

    # ---- 组装关系 ----
    if meeting:
        host, rec = info["主持人"], info["记录人"]
        mm_h = rules.RE_HOST.search(text)
        if host:
            add(persons, host)
            rels.append({"source": host, "target": meeting, "type": "主持人",
                         "properties": {"evidence": dt.evidence(mm_h.start(1), mm_h.end(1)) if mm_h else None}})
        mm_r = rules.RE_RECORDER.search(text)
        if rec:
            add(persons, rec)
            rels.append({"source": rec, "target": meeting, "type": "记录",
                         "properties": {"evidence": dt.evidence(mm_r.start(1), mm_r.end(1)) if mm_r else None}})
        for pn in attendee_persons:
            rels.append({"source": pn, "target": meeting, "type": "参会",
                         "properties": {"evidence": person_ev.get(pn)}})
        if info["项目"]:
            rels.append({"source": meeting, "target": info["项目"], "type": "属于项目",
                         "properties": {"evidence": dt.evidence(seg.start(), min(len(text), seg.start() + 60)) if seg else None}})
    if info["项目"]:
        for r in roles:
            rels.append({"source": r["unit"], "target": info["项目"], "type": "单位角色",
                         "properties": {"role": r["role"], "evidence": r["evidence"]}})

    # ---- ⭐ 单位名合法性防线（最后一道关，个例残余留给 ③ 消解）----
    units, persons, rels, roles, san = sanitize_units(units, persons, rels, roles, rules)
    if san.get("dropped") or san.get("cut"):
        warnings.append("单位名清洗：丢弃 %d 个 / 切开人名粘连 %d 个"
                        % (len(san.get("dropped") or []), len(san.get("cut") or [])))
        info["单位名清洗"] = san

    entities = ([{"id": u, "name": u, "type": "ORG"} for u in units]
                + [{"id": p, "name": p, "type": "PERSON"} for p in persons]
                + ([{"id": meeting, "name": meeting, "type": "MEETING"}] if meeting else [])
                + ([{"id": info["项目"], "name": info["项目"], "type": "PROJECT"}] if info["项目"] else []))

    info["章节"] = list(dict.fromkeys(rules.RE_CHAPTER.findall(text)))[:5]
    if len(text) < (e.get("min_content_chars") or 200):
        warnings.append("内容过少（%d 字）—— 可能是空壳/占位件，别当有效样本统计" % len(text))

    stats = {
        "units": len(units), "persons": len(persons), "meeting": meeting,
        "project": info["项目"], "host": info["主持人"], "period": info["期号"],
        "roles": len(roles), "relations": len(rels),
        "with_evidence": sum(1 for r in rels if (r.get("properties") or {}).get("evidence")),
        "warnings": warnings,
    }
    return entities, rels, info, stats
