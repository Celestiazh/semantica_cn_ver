# -*- coding: utf-8 -*-
"""监理纪要知识图谱 · 核心包

分层（与《Semantica-独立功能-设计与现状.md》一致）：

    ① parse    分级取文本：有文字层读文字层，无文字层走 RapidOCR
    ② extract  抽取：第 1 层会议要素（规则）/ 第 2 层正文事项（候选 + 可选模型）
    ③ resolve  消解：规则归一 → 名册锚定 → dedup 提候选 → 人工确认 → 合并
    ④ graph    建图 + 溯源：边带证据（文件 / 页 / 原文片段）
    ⑤ gather   材料汇聚：跨文件句子级检索（带出处）
    ⑥ output   产出：graph.json / graph.html / report.md

铁律：
  - **不让 LLM 改写正文** —— 一旦重写，页码 + 坐标对不上，溯源链就断了
  - **每条边必须带证据** —— 否则抽错了没法追、没法撤
  - **加关系不改代码** —— 关系定义在 config/relations.yaml
"""

__version__ = "0.1.0"
