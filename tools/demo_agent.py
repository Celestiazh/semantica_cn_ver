# -*- coding: utf-8 -*-
"""迷你 Agent 演示：对话模型自主调用 kg 工具查数据 → 组织带出处的回答。

与正式交付的 kg_mcp.py **同一套工具实现**（直接复用其 _HANDLERS 与 TOOLS schema），
本脚本是"模型自主调工具"的本地演示与预测试 —— 不需要 OpenClaw 就能跑通联动。

用法：
    .venv\\Scripts\\python.exe tools\\demo_agent.py "工务署参与了什么项目？"
    .venv\\Scripts\\python.exe tools\\demo_agent.py output\\演示问题.txt   # 一行一个问题，逐个跑
    .venv\\Scripts\\python.exe tools\\demo_agent.py            # 交互式
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from kg.config import Config                                    # noqa: E402
from kg.extract import llm_provider as L                        # noqa: E402
import kg_mcp as M                                              # noqa: E402  （工具实现同源）

MAX_TURNS = 6


#   优先级：命令行参数 > 这里的列表 > 交互式提问

DEMO_QUESTIONS = [
    "中建科工有些什么数据",
]


def openai_tools(mcp_tools):
    """MCP 工具 schema（name/description/inputSchema）→ OpenAI tools 格式。"""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("inputSchema") or {"type": "object"}}}
            for t in mcp_tools]

SYSTEM = (
    "你是工程监理知识图谱问答助手。回答规则：\n"
    "1. 涉及单位/人员/项目/事项的事实类问题，必须先调用工具查数据，禁止凭记忆回答；\n"
    "2. 每条结论标注出处《文件》第N页（用工具返回的 evidence）；\n"
    "3. 工具返回里没有的信息禁止出现；取数为 0 就明说「图谱中信息不足」；\n"
    "4. 用户问「近期/最近」时传 days=30 时间窗；\n"
    "5. 回答结构：先用 2~3 句白话给出结论性总述（谁、参与几项、主力角色是什么），"
    "让不听细节的人也能带走结论；然后再附明细表格。总述里只允许出现明细中有的事实，"
    "不得推测或评价任何单位。\n"
    "6. ⛔ 命中多个不同法人实体（search_entity 返回的 disambiguation.distinct_legal_entity_pairs 有记录）："
    "**回答必须覆盖其中每一个实体**（该实体无数据的也要单独一小节明说「无记录」），"
    "要么分别作答要么列出候选请用户澄清——禁止只答其一、禁止合并不同法人的事实；"
    "same_legal_entity_groups 里的名字是同一法人，可合并陈述。\n"
    "7. ⛔ 开放盘点类问题（「有些什么数据/介绍一下」）：按 facts_summary 的类型分布**逐类汇报**，"
    "**每类最多列 5 条代表样例（优先内容完整的），并标注该类总数**"
    "——例如「事项责任单位：共 43 条，代表样例：…（5 条）」，"
    "**禁止在盘点回答中逐条枚举超过 5 条同类事实**（把 token 预算花在枚举上会导致后面的实体/类别被截断）；"
    "用户明确说「列出全部」时才逐条展开；"
    "search_entity 的 truncated=true 时注明「候选已截断」并建议用户缩小范围。"
)


def run_agent(provider, question):
    """一轮完整 Agent 循环。返回最终回答文本。"""
    global OPENAI_TOOLS
    OPENAI_TOOLS = openai_tools(M.TOOLS)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    for turn in range(1, MAX_TURNS + 1):
        payload = {"model": provider.model, "temperature": 0, "max_tokens": 4096,
                   "messages": messages, "tools": OPENAI_TOOLS}
        if getattr(provider, "reasoning_effort", ""):
            payload["chat_template_kwargs"] = {"reasoning_effort": provider.reasoning_effort}
        data = provider._post(provider.base_url + "/chat/completions", payload,
                              {"Authorization": "Bearer " + provider.api_key
                               if provider.api_key else ""})
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            content = L.strip_thinking(L.message_text(msg))
            return content, turn
        messages.append(msg)
        for c in calls:
            fn = (c.get("function") or {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:                                # noqa: BLE001
                args = {}
            handler = M._HANDLERS.get(name)
            result = handler(args) if handler else {"error": "未知工具 %s" % name}
            brief = json.dumps(result, ensure_ascii=False)
            print("  🔧 [%d] %s(%s) → %s" % (
                turn, name, json.dumps(args, ensure_ascii=False)[:60],
                brief[:100] + ("…" if len(brief) > 100 else "")))
            messages.append({"role": "tool", "tool_call_id": c.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)})
    return "（超过最大轮数仍未给出最终回答）", MAX_TURNS


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config(ROOT)
    provider = L.build_provider(cfg)
    if provider.name == "none":
        sys.exit("settings.yaml 的 llm.provider 是 none，先配置模型")

    # 问题来源优先级：命令行参数 > 文件顶部 DEMO_QUESTIONS > 交互式
    questions = list(sys.argv[1:])
    if not questions and DEMO_QUESTIONS:
        questions = [q for q in DEMO_QUESTIONS if q.strip()]
        print("【使用文件内 DEMO_QUESTIONS】%d 问（在 demo_agent.py 顶部编辑）" % len(questions))
    if len(questions) == 1 and questions[0].lower().endswith(".txt"):
        qfile = Path(questions[0])
        if not qfile.is_absolute():
            qfile = ROOT / qfile
        questions = [ln.strip() for ln in
                     qfile.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
        print("【问题清单】%s（%d 问）" % (qfile, len(questions)))
    interactive = not questions
    while True:
        if interactive:
            q = input("\n问题（q 退出）> ").strip()
            if q.lower() in ("q", "quit", "exit"):
                break
        else:
            if not questions:
                break
            q = questions.pop(0)
        print("【问题】%s" % q)
        answer, turns = run_agent(provider, q)
        print("【最终回答】（%d 轮）\n%s\n" % (turns, answer))
        if not interactive and questions:
            print("─" * 60)


if __name__ == "__main__":
    main()
