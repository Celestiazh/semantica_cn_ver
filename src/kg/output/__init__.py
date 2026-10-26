# -*- coding: utf-8 -*-
"""⑥ 产出

    graph.json              机器可读的关系图 = Semantica `POST /api/import` 的入参格式，直接喂它
    graph.html              可交互关系图，**点边看得到出处**（独立单文件，Agent 里给用户看的就是它）
                            ⚠️ **待定**：可视化已交给它的 Explorer —— 若 Explorer 能给业务方看，这一项可省；
                               若不能（它偏开发者调试界面），仍要自己出。等你看过 Explorer 再定。
    report.md               一页结论 + 量化数字（覆盖率 / 错误率 / 自动判定率）
    entities_to_confirm.csv 消解的人工确认清单（③ 的输入）
    unreviewed_names.txt    "不在名册的名字"清单（名册靠它长大）

⚠️ 溯源口径纪律：报告里说"跑通"时**必须说明喂进去的是自动抽取还是手工数据**。

TODO：output/graph_html.py（自包含 HTML，不引 CDN）、output/report.py
"""

STATUS = "stub"
