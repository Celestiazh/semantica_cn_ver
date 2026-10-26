# -*- coding: utf-8 -*-
"""模型侧验收：用 Hy3 对照图谱回答问题，答案必须带出处、不得超出图谱事实。

原理（GraphRAG 极简版）：
  问题 → 本地图里匹配实体（前缀匹配，解决"中建科工"vs 全称）→ 收集该实体的边+证据
       → 连同问题发给模型 → 模型只依据给定事实作答，每个结论标注《文件》第N页。

用法：
    python tools/ask_graph.py "中建科工参与过哪些项目？"
    python tools/ask_graph.py --days 30 "拉普拉斯项目近期有哪些待办？"   # 只看近 30 天纪要的事实
    python tools/ask_graph.py --since 2026-09-01 --until 2026-09-30 "..."
    python tools/ask_graph.py            # 交互式，逐个提问，q 退出

时间窗（可选）：按每份纪要的会议时间（per_file info.会议时间）过滤事实；
    无日期的纪要不受过滤影响（保守保留）。展示层的"时间视角"用 Explorer 自带时间轴。
"""
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kg.config import Config                                   # noqa: E402
from kg.extract import llm_provider as L                       # noqa: E402

GRAPH = ROOT / "var" / "extract" / "graph_resolved.json"
SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司")


def facts_of(ent, rels, paths, id2ent=None):
    id2ent = id2ent or {}
    keys = {ent.get("id"), ent.get("name")} - {None, ""}

    def disp(x):
        e = id2ent.get(x)
        return e.get("name", x) if e else x

    out = []
    for r in rels:
        if not (keys & {r.get("source"), r.get("target")}):
            continue
        ev = (r.get("properties") or {}).get("evidence") or []
        lines = ["- 事实：%s ——%s--> %s" % (disp(r.get("source")), r.get("type"),
                                             disp(r.get("target")))]
        props = {k: v for k, v in (r.get("properties") or {}).items() if k != "evidence"}
        if props:
            lines.append("  附加属性：%s" % json.dumps(props, ensure_ascii=False))
        for end in (r.get("source"), r.get("target")):
            e = id2ent.get(end) or {}
            if e.get("type") == "MATTER":
                mp = e.get("properties") or {}
                parts = ["%s=%s" % (k, mp.get(k, "")) for k in ("期限", "责任单位(原文)")
                         if mp.get(k)]
                if e.get("name"):
                    parts.insert(0, "内容=%s" % e["name"])
                if parts:
                    lines.append("  事项属性：" + "；".join(parts))
        for v in (ev if isinstance(ev, list) else [ev])[:3]:
            f = v.get("file", "")
            loc = paths.get(f, f)
            lines.append("  出处：《%s》第%s页「%s」" % (loc, v.get("page"),
                                                       (v.get("quote") or "")[:60]))
        out.append("\n".join(lines))
    return out


SYSTEM = (
    "你是工程监理知识图谱的问答验收助手。**只依据【图谱事实】作答**，禁止使用任何图谱之外的知识。\n"
    "规则：\n"
    "  1. 每个结论后面标注出处：《文件名》第N页（事实里给了什么就引什么）；\n"
    "  2. 图谱事实不够回答时，明确说\"图谱中信息不足\"，不要猜；\n"
    "  3. 简洁分点，不要展开想象；\n"
    "  4. ⛔ 文件名、日期、期号、数字必须**逐字照抄**事实原文的写法，禁止自行补全或推算"
    "（例：文件名只写\"8月27日\"就引\"8月27日\"，不得写成\"2024年8月27日\"——语料年份不在事实里，补了就是编造）；\n"
    "  5. 事实里没有出现的年份、单位、人名、数量，一律不得出现在回答里；\n"
    "  6. ⛔ 问题主体命中多个不同法人实体时：回答必须覆盖每一个实体（无数据的单独明说），"
    "或列出候选请用户澄清——禁止只答其一。"
)


def build_file_dates():
    """文件名(含扩展名) → 会议日期(date)。来源：var/extract/per_file/*.json 的 info.会议时间；
    解析不出再用文件名里的 8 位日期/点分日期。失败的文件不进表（视为无日期）。"""
    out = {}
    pf = ROOT / "var" / "extract" / "per_file"
    if not pf.is_dir():
        return out
    for j in pf.glob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        base = Path(str(d.get("file", j.stem))).name
        t = str((d.get("info") or {}).get("会议时间") or "")
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", t)
        if not m:
            m = re.search(r"(20\d{2})[.\-/年]?(\d{1,2})[.\-/月]?(\d{1,2})", base)
        if m:
            try:
                out[base] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return out


def fact_in_window(fact_text, fdates, since, until):
    """事实是否落在时间窗内。依据事实行里的《文件》文件名查会议日期；
    查不到日期的文件保守保留（不受过滤影响）。"""
    if not since and not until:
        return True
    for m in re.finditer(r"《([^》]+)》", fact_text):
        base = m.group(1).replace("\\", "/").split("/")[-1]
        d = fdates.get(base)
        if d is None:
            return True                                       # 无日期 → 保守保留
        if since and d < since:
            return False
        if until and d > until:
            return False
        return True
    return True                                               # 行里没有《》引用 → 保留


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from kg.serve.query import get_graph, match_entities_ex
    g = get_graph()                                   # ★ 统一供给口（neo4j 执政 / json 兜底）
    cfg = Config(ROOT)
    from kg import resolve as res_mod
    paths = res_mod._locate_files(cfg)
    provider = L.build_provider(cfg)
    if provider.name == "none":
        sys.exit("settings.yaml 的 llm.provider 是 none，先配置模型")

    # ---- 参数解析：--days N / --since / --until，其余位置参数为问题 ----
    args = sys.argv[1:]
    days, since_s, until_s = None, None, None
    questions = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1]); i += 1
        elif a == "--since" and i + 1 < len(args):
            since_s = args[i + 1]; i += 2
        elif a.startswith("--since="):
            since_s = a.split("=", 1)[1]; i += 1
        elif a == "--until" and i + 1 < len(args):
            until_s = args[i + 1]; i += 2
        elif a.startswith("--until="):
            until_s = a.split("=", 1)[1]; i += 1
        else:
            questions.append(a); i += 1
    since = date.fromisoformat(since_s) if since_s else \
        (date.today() - timedelta(days=days) if days else None)
    until = date.fromisoformat(until_s) if until_s else None
    fdates = build_file_dates() if (since or until) else {}

    interactive = not questions
    while True:
        if interactive:
            q = input("\n问题（q 退出）> ").strip()
            if q.lower() in ("q", "quit", "exit"):
                break
        else:
            if not questions:
                break                                   # 修：单问题答完即止（原会在空列表上再 pop 崩溃）
            q = questions.pop(0)
        mr = match_entities_ex(q, g["entities"], max_n=5)   # 共享匹配器（exact 守卫/截断信号/别名）
        ents = mr["entities"]
        if not ents:
            print("⚠ 图谱实体未匹配到，跳过（试试用实体名的一部分）")
        else:
            from kg.serve.query import disambiguation_info
            dis = disambiguation_info([e.get("name") for e in ents])
            dis_lines = []
            if dis["same_legal_entity_groups"]:
                dis_lines.append("同一法人（可合并陈述）：" + "、".join(
                    "/".join(g_) for g_ in dis["same_legal_entity_groups"]))
            if dis["distinct_legal_entity_pairs"]:
                dis_lines.append("⛔ 不同法人（禁止合并陈述，须分别作答）：" + "；".join(
                    "%s vs %s" % (d["pair"][0], d["pair"][1])
                    for d in dis["distinct_legal_entity_pairs"]))
            dis_text = ("\n【实体归属提示（来自人工拍板沉淀，必须遵守）】\n" + "\n".join(dis_lines) + "\n") if dis_lines else ""
            facts = []
            for e in ents:
                id2ent = {e["id"]: e for e in g["entities"]}
                facts += facts_of(e, g["relationships"], paths, id2ent)
            if since or until:
                total = len(facts)
                facts = [f for f in facts if fact_in_window(f, fdates, since, until)]
                win = "%s ~ %s" % (since or "…", until or "今")
                print("【时间窗】%s ｜ 事实 %d → %d 条（无日期的纪要不受影响）"
                      % (win, total, len(facts)))
                if not facts:
                    print("⚠ 时间窗内没有该实体的事实，试试放宽窗口或不带时间参数")
                    continue
            cut = ""
            if len(facts) > 80:
                cut = "\n⚠【截断声明】事实共 %d 条，以下仅展示前 80 条：请按类别概述并注明「未逐条展开，可指定类型查看全部」，禁止当作全部。\n" % len(facts)
                facts = facts[:80]                   # 控制长度，但必须声明
            user = "【问题】%s\n%s\n\n【图谱事实】\n%s%s" % (q, dis_text, "\n".join(facts), cut)
            payload = {"model": provider.model, "temperature": 0, "top_p": 1,
                       "max_tokens": 1600,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": user}]}
            if getattr(provider, "reasoning_effort", ""):
                payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
            try:
                data = provider._post(provider.base_url + "/chat/completions", payload,
                                      {"Authorization": "Bearer " + provider.api_key
                                       if provider.api_key else ""})
                import re as _re
                content = L.strip_thinking(L.message_text((data.get("choices") or [{}])[0].get("message") or {}))
                print("\n【匹配实体】%s ｜【依据事实】%d 条" % ("、".join(e["name"] for e in ents), len(facts)))
                print("【模型回答】\n%s\n" % _re.sub(r"\n{3,}", "\n\n", content))
                # ⛔ 出处硬校验：模型给的每条《文件》第N页，必须能在本次事实里找到，否则标红
                # 事实行里是绝对路径，模型常只引文件名 → 全路径与文件名都登记
                fact_set = set()
                for f in facts:
                    for m in _re.finditer(r"《([^》]+)》\s*第\s*(\d+)\s*页", f):
                        loc = m.group(1).replace("\\", "/").split("/")[-1]
                        fact_set.add((loc, m.group(2)))
                        fact_set.add((m.group(1), m.group(2)))
                bad, cited = [], 0
                for m in _re.finditer(r"《([^》]+)》\s*第\s*(\d+)\s*页", content):
                    cited += 1
                    loc = m.group(1).replace("\\", "/").split("/")[-1]
                    if (loc, m.group(2)) not in fact_set and (m.group(1), m.group(2)) not in fact_set:
                        bad.append("《%s》第%s页" % (m.group(1), m.group(2)))
                if cited == 0:
                    print("【出处校验】⚠ 回答中没有任何《文件》第N页引用 —— 不合规格，需人工核查")
                elif bad:
                    print("【出处校验】⚠ %d/%d 条引用在图谱事实中不存在（疑似编造）：%s"
                          % (len(bad), cited, "；".join(bad[:5])))
                else:
                    print("【出处校验】✓ %d 条引用全部可溯源" % cited)
                # 智能层：问答结论沉淀进 Agent 记忆（语义检索用，见 src/kg/serve/intelligence.py）
                try:
                    from kg.serve.intelligence import remember
                    _src = list(dict.fromkeys(
                        m.group(1).replace("\\", "/").split("/")[-1]
                        for m in _re.finditer(r"《([^》]+)》\s*第\s*(\d+)\s*页", content)))[:5]
                    remember(q, _re.sub(r"\n{3,}", "\n\n", content), sources=_src)
                    print("【记忆】✓ 结论已沉淀（后续可 search_context 语义检索）")
                except Exception as _e:          # noqa: BLE001 记忆失败不影响回答
                    print("【记忆】沉淀失败: %s" % _e)
            except Exception as e:                       # noqa: BLE001
                print("调用失败：%s: %s" % (type(e).__name__, e))
        if not interactive:
            if not questions:
                break


if __name__ == "__main__":
    main()
