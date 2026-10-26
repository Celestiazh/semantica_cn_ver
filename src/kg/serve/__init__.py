# -*- coding: utf-8 -*-
"""对接层：把这个组件"挂进 Agent"时用的接口。

⭐ 关键设计（2026-09-22 定）：**Agent 本身就是模型**，所以服务不必自己接模型 ——
   可以把"要模型判断的候选条目"作为工具接口**暴露出去**，让 Agent 调走自己处理、再回灌。

## 工具清单（`serve/tools.py`）

| 工具 | 作用 | 状态 |
| :--- | :--- | :--- |
| `get_pending_candidates` | 取"待办候选 + 原文片段 + 待抽字段 schema" | ✅ 已实现 |
| `submit_extracted_fields` | 读回 Agent 填好的字段并合并、落盘、留痕 | ✅ 已实现 |
| `pending_status` | 看还有多少条没填（Agent 循环用） | ✅ 已实现 |
| `search_entity` | 某个人的全部关联（所属单位 / 参会 / 相关事项），带出处 | ⏳ 依赖 ④ 图与查询 |
| `gather_topic` | 跨文件找相关原文段落，按时间排、带出处 | ⏳ 依赖 ⑤ 材料汇聚 |
| `export_graph` | 生成关系图 HTML，返回路径 | ⏳ 依赖 ④ |

## 为什么先做"候选导出 + 字段回灌"这一对

因为它是**唯一现在就能闭环的**：解析已跑通、候选已能产出，缺的只是"字段由谁来填"。
它同时也把 `llm.provider` 那条路留好了 —— 两种模式共用同一份候选数据结构：

    provider=none  → 服务只出候选，字段由 Agent 填（本模块的路）
    provider=ollama/openai-compatible → 服务自己填（`extract.llm_provider`）

## 后续形态

`serve/` 是**唯一的对外层**，上面两个出口都只是薄壳：
  - MCP 工具组（主）：每个工具对应 `tools.py` 里一个函数
  - HTTP 接口（备）：与印章识别 `stamp_api_service.py` 同构（提交 → 查状态 → 取结果）

⛔ 红线：**回灌只允许填字段，不允许改 `quote` 原文** —— 改了，溯源链就断了（`tools.py` 会拒绝）。
"""

from . import tools                                             # noqa: F401

__all__ = ["tools"]
