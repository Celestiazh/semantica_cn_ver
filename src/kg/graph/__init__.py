# -*- coding: utf-8 -*-
"""④ 建图 + 溯源 —— ⭐ **这一段交给 Semantica**（这是它出彩的地方，不是我们自建）

## 分工（既定，不是待定项）

    我们负责 ──► 产出 graph.json（实体 + 关系，**每条边带 evidence**）
    它负责   ──► 建图存储 · 图算法 · 关系查询 · 可视化 · 流程血统

理由（已实测，不是推断）：
  - 它的图算法是**真材实料**：`kg` 模块约 2 万行（`graph_builder.py` 单文件 ≈1950 行），
    PageRank 自研、networkx 只是可选加速 —— 不是"把两个列表组装起来"
  - 社区检测实测**自动聚成三组 = 三家单位的人**（正是要的"同单位的人"）
  - 关系查询开箱可用：`GET /api/graph/node/{id}/neighbors` → "这家单位参与过哪些项目"
  - 79 个端点 / Explorer 界面 / 流程血统 PROV-O：自建等于重写，没必要

我们只做"最后一公里"三件事：
  1. 产出符合它 `POST /api/import` 入参格式的 `graph.json`（含 `properties.evidence`）
  2. 走 HTTP 导入 + **对账**（导入后拉 `/api/graph/stats` 核节点/边数）
  3. 把它的查询结果**翻译成我们的对外契约**（不把它的原始结构直接暴露出去）

## ⚠️ 接入必须处理的四个坑（全部实测）

1. ⛔ **边会被去重**：喂 253 条 → 导入后只剩 93 条（同一条边在多份文件出现被并成 1 条）
   → 所以 `evidence` **必须支持多出处（数组）**，否则"这条关系出自哪几份文件"会丢
2. ⚠️ **证据必须放 `edge.properties`**：放**顶层** `evidence` 字段会被丢弃（properties 实测能完整往返）
3. ⚠️ **导入 / 导出格式不对称**：我们导入用 `{id,name,type}`，它导出给的是 `{id,type,text,metadata}`
4. ⚠️ **端点未声明类型时它会自动补建 `entity` 节点**（实测补了 11 个、类型丢了）
   → 我们产出时**必须把两端节点都显式带上**

## 它方端点（0.6.8 实测 79 路径 / 83 操作，这里只列要用的）

    导入导出   POST /api/import · POST /api/export
    查询       GET  /api/graph/stats · /api/graph/nodes · /api/graph/edges
               GET  /api/graph/node/{id} · /api/graph/node/{id}/neighbors · /api/graph/path
               POST /api/graph/search
    流程血统   GET  /api/provenance · GET /api/provenance/report  （**只有 GET，无写入端点**）

## 部署形态

**服务独立环境跑**（它要 300+ 个包）→ 本项目**只走 HTTP**，venv 保持 7 个包，
**不 `import semantica`**。上线必须配 `SEMANTICA_API_KEY`（别开 anonymous）。

TODO：`graph/semantica_client.py`（探活 / 导入 / 对账 / 查询封装）、
      `graph/builder.py`（产出带 evidence 的 graph.json；evidence 支持多出处）
"""

STATUS = "stub"
