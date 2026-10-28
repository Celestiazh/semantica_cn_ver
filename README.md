# 监理会议纪要知识图谱（KG）· 部署与运行手册

> **阅读指引**
> - 本文档自包含：按本文可在全新 Linux 服务器上重建环境并跑通验收，无需额外口头信息。
> - 分流：部署运维看 §3/§4/§8；验收测试看 §6/§9；应用对接另见《接口文档.md》；**调参数看 §7**。
> - 所有"预期输出"均为真实运行结果摘录；不符时按对应工具的"失败排查"处理。

---

# 一、项目概况

## 1.1 项目定位

把 154 份（持续增量）监理例会纪要，加工成可查询、可统计、可溯源的知识图谱：大模型通过标准接口查数，回答每条挂《原文文件》第 N 页出处；治理采用"AI 提名、人拍板、配置沉淀"的流程。

## 1.2 交付内容

| 交付物 | 位置 | 说明 |
| :-- | :-- | :-- |
| 图谱数据本体 | Neo4j 图库（运行时供给）+ `var/extract/graph_resolved.json`（产物/备份） | 每条关系带《原文文件》+ 页码 + 原文引文 |
| 看板 | `tools/serve_dashboard.py` → http://127.0.0.1:8000 | Semantica 引擎驱动，已汉化 |
| MCP 取数服务 | `tools/kg_mcp.py` | 4 个工具，装配给大模型/Agent 平台（见《接口文档.md》） |
| 实体台账 | `tools/export_inventory.py` 生成 | 单位/项目/人员/事项四分表 xlsx |
| 数据库 | Neo4j（图库）+ Qdrant（语义记忆），Docker 部署 | 见 §3.5 |
| 文档 | README.md（本文）+ 接口文档.md | 部署运行 / 对接 |

## 1.3 当前基线（验收对照数字）

| 指标 | 数值 |
| :-- | :-- |
| 图谱规模 | 1,404 实体 / 1,806 关系（单位 70 / 人员 126 / 项目 18 / 会议 3 / 事项 1,187） |
| 溯源 | 每条关系带《文件》第 N 页 + 原文引文；问答出处硬校验 |
| 金标问答回归 | 8 用例全过 |
| MCP 自测 | 14 项断言全过 |
| 存储对账 | Neo4j 与 json 逐项一致 PASS |

---

# 二、架构与数据流

## 2.1 数据流主线

```
input_dir（纪要 PDF/Word）
  → ① --parse        文字提取（扫描件走 RapidOCR），带页码坐标      → var/parse/
  → ② --extract      L1 规则抽会议要素 + L2 大模型填事项三字段      → var/extract/
  → ③ 字段填充       tools/llm_fill_l2.py（模型填）或 --pending-out/in（Agent 代填）
  → ④ --qc/--resolve 质检消解：AI 提名 → 人工拍板 → 配置沉淀        → var/resolve/ + config/*.yaml
  → ⑤ --matter --consolidate  事项层 + 归一
  = 最终产物 var/extract/graph_resolved.json
```

## 2.2 运行时图供给与消费通道

运行时图数据由 Neo4j 供给（`query.get_graph()` 统一入口，内容指纹缓存）；json 是流水线产物和备份。切回 json 直读模式改 `graph_backend.backend: memory`。

| 通道 | 入口 | 数据供给 |
| :-- | :-- | :-- |
| MCP 4 工具 | `tools/kg_mcp.py` | Neo4j |
| 问答/智能层 | `tools/ask_graph.py` | Neo4j |
| 看板 | `tools/serve_dashboard.py` | graph_resolved.json（与图库同源，逐项对账一致） |

## 2.3 智能层

| 能力 | 存储 | 说明 |
| :-- | :-- | :-- |
| Agent 记忆 | Qdrant（服务器库）或 faiss（本机文件） | 问答结论自动沉淀，语义检索"之前查过什么" |
| 决策追踪 | 同上 | 消解拍板/治理决定留痕，可语义检索 |
| 规则推理 | 内存图 | 逾期推导 + 单位风险画像（零模型） |

---

# 三、环境搭建（从零重建服务器环境）

交付包不包含运行环境（.venv / sem2_libs 均不打包），按本节在服务器上在线重建。全程约 30-60 分钟（视网速）。

## 3.0 国内镜像源

| 资源 | 首选 | 回退（首选缺货时） |
| :-- | :-- | :-- |
| pip 包 | 清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` | 官方源 `https://pypi.org/simple`（清华缺包：semantica / openai / neo4j） |
| Docker 镜像 | 配置镜像加速器（/etc/docker/daemon.json → registry-mirrors） | 有网机器 `docker pull` + `docker save -o xx.tar` → 拷贝到服务器 `docker load -i xx.tar`（镜像名不变，run 命令不变） |

## 3.1 硬件与软件要求

| 项 | 要求 | 说明 |
| :-- | :-- | :-- |
| 系统 | Linux（Ubuntu 20.04+ 实测）或 Windows | 代码跨平台 |
| Python | 3.10+（实测 3.11 / 3.13） | 流水线与引擎各一套 venv，见下 |
| 内存 | ≥ 8GB 空闲（图库 2GB + 引擎 + 流水线） | 千级实体规模无需更多 |
| GPU | 不需要 | 全链路 CPU：OCR 是本地小模型，LLM/Embedding 走远程服务 |
| 磁盘 | ≥ 10GB（引擎 ~2GB + 数据） | 数据目录建议独立数据盘 |
| Docker | 20.10+（含 compose 更佳） | 仅数据库用 |
| 网络 | 能访问 LLM 服务、Embedding 服务、PyPI | 内网需配镜像源（见 §3.0） |

## 3.2 模型部署与选型（1 个本地小模型 + 2 个外部模型服务）

### 3.2.1 PDF 识别本地小模型（RapidOCR，随包部署、零配置）

本项目 62% 的纪要是无文字层的扫描件 PDF，依赖本地 OCR 读图取字。选型 RapidOCR（PaddleOCR 的 PP-OCRv4-mobile 模型 ONNX 化运行时）。

| 项 | 说明 |
| :-- | :-- |
| 选型理由 | ① 私有化硬要求：监理纪要不出门，排除云 OCR；② 纯 CPU 离线推理，无 GPU、无模型服务进程；③ PP-OCRv4 中文优化，实测 5.2 秒/页 @200dpi；④ 模型文件随 pip 包内置，装完即用；⑤ 开源免费 |
| 备选 | PaddleOCR 完整版（效果相当但依赖重）/ 云 OCR（数据要出门，排除） |
| 部署方式 | 无独立步骤：随 §3.3 `pip install -r requirements.txt` 一并安装（rapidocr_onnxruntime + onnxruntime + opencv-python-headless + pymupdf），模型权重内置 |
| 验证 | 跑 §4 `run.py --parse`：扫描件逐页输出"行文字+页码坐标"；预期全部提取成功（历史实测 154 份 0 静默丢失） |

### 3.2.2 LLM（对话/抽取，外部服务）

| 项 | 说明 |
| :-- | :-- |
| 协议 | OpenAI 兼容 `/v1/chat/completions`（支持 function calling 更佳） |
| 用途 | L2 事项填空、问答组织语言 |
| 部署形态 | 自部署 vLLM / 云 API 均可；也支持 `none`（由对接方 Agent 代填字段） |
| 示例 | hy3（vLLM） |

### 3.2.3 Embedding（向量化，外部服务）

| 项 | 说明 |
| :-- | :-- |
| 协议 | OpenAI 兼容 `/v1/embeddings` |
| 用途 | 智能层记忆/决策/相似检索（Qdrant 落库） |
| 部署形态 | 自部署或云 API，维度不限（系统启动自动探测，换模型见 §8） |
| 示例 | qwen3-embedding-8B（4096 维） |

## 3.3 流水线环境（.venv）

```bash
cd /opt/kg                                   # 交付包解压目录
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# 缺包时换官方源：-i https://pypi.org/simple
```

验证（离线自检，不依赖网络）：

```bash
.venv/bin/python run.py --check        # 预期：各项 OK
```

## 3.4 Semantica 引擎环境（sem2_libs，看板与智能层用）

```bash
# ① 主体（清华源缺 semantica → 官方源）
python3 -m pip install --target /opt/kg/sem2_libs "semantica==0.6.8" --index-url https://pypi.org/simple

# ② 服务端四件套（semantica 不声明这些依赖，必须补装）
python3 -m pip install --target /opt/kg/sem2_libs \
    "fastapi==0.143.0" "uvicorn==0.54.0" "python-multipart==0.0.32" "websockets==17.2" \
    --index-url https://pypi.org/simple

# ③ embedding / 图库 / 向量库客户端（清单见 deploy/requirements-sem2.lock.txt）
python3 -m pip install --target /opt/kg/sem2_libs "openai" "neo4j" "qdrant-client" --index-url https://pypi.org/simple

# ④ 导入验证
PYTHONPATH=/opt/kg/sem2_libs python3 -c "import semantica.server; print('IMPORT OK')"
```

**sem2_libs 安装/重装后必须重跑三个补丁**（幂等，支持 `--undo` 回退；详见 §6.4）：

| 补丁 | 作用 |
| :-- | :-- |
| `tools/ui_i18n_patch.py` | Explorer 界面汉化 + 隐藏无用组件 |
| `tools/patch_semantica_search.py` | 修复搜索"事项淹没实体"的缺陷（见 §7.4） |
| `tools/patch_semantica_embedding.py` | 解锁 OpenAIStore 指定 base_url（内网必需） |

预期输出均为"补丁完成"字样。

## 3.5 数据库（Neo4j + Qdrant，Docker 单容器）

选型均为开源免费、私有部署。运行时图数据由 Neo4j 供给（`graph_backend.backend: neo4j`）；json 降级为流水线产物+备份。切回 json 模式改 `backend: memory`。

```bash
docker run -d --name neo4j --restart always -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/<密码> -v /data/neo4j:/data neo4j:5-community

docker run -d --name qdrant --restart always -p 6333:6333 \
  -e QDRANT__SERVICE__API_KEY=<密钥> -v /data/qdrant:/qdrant/storage qdrant/qdrant:v1.12.4
```

- 无需建库建表：约束/集合由脚本自动创建（幂等）
- 国内落地：镜像拉取按 §3.0；数据目录 `-v` 落数据盘
- 端口冲突：host 端口被占时容器启动失败并明确报错（非静默），换端口后同步改 settings.yaml
- 安全：公网暴露时在云安全组限制 6333/7474/7687 来源 IP

## 3.6 配置文件与密钥清单

| 文件 | 内容 | 交付状态 |
| :-- | :-- | :-- |
| `config/settings.yaml` | 全部配置：paths / llm / embeddings / vector_store / graph_backend / intelligence | 随包（地址按现场改） |
| `config/llm_key.local` | LLM API Key（可选） | 不在包内，现场创建 |
| `config/embed_key.local` | Embedding API Key | 同上 |
| `config/neo4j_pwd.local` / `config/qdrant_key.local` | 数据库凭据 | 同上 |
| `config/roster*.yaml` 等治理沉淀 | 名册/别名/拍板记录 | 随包（数据治理成果） |

密钥文件格式：一行纯文本。代码读 `settings.yaml` 里对应的 `*_file` 键。

---

# 四、数据初始化与流水线（每步预期输出）

入口统一 `run.py`，按序执行。增量机制：解析/抽取按文件内容指纹缓存，新纪要来了只处理新文件。

| 步骤 | 命令 | 预期输出（关键行） | 产物 |
| :-- | :-- | :-- | :-- |
| ① 解析 | `run.py --parse` | INFO 逐文件进度；结束报成功/失败计数 | `var/parse/*.json`（文字+页码） |
| ② 抽取 | `run.py --extract` | INFO 实体/事项候选统计 | `var/extract/pending_candidates.json` |
| ③ 字段填充 | `tools/llm_fill_l2.py`（需 LLM）或 `run.py --pending-out` → 外部 Agent 填 → `--pending-in` | 填充计数 + 拒收计数（quote 校验） | `var/extract/pending_filled.json` |
| ④ 质检消解 | `run.py --qc` → `--resolve` | 生成人工确认清单 `var/resolve/review_*.xlsx` | 同左 |
| ④' 应用拍板 | `run.py --resolve --apply` | `INFO 沉淀：config/roster_extra.yaml ...` | 决定沉淀进配置 |
| ⑤ 事项层+归一 | `run.py --matter` → `--consolidate` | `INFO 幂等：重跑只做增量`；`PROJECT 归一：... 剩余项目 N 个` | `var/extract/graph_resolved.json` |
| 看板重灌 | `tools/serve_dashboard.py` | `对账：PASS ✓`（内容指纹比对） | 内存图 + 浏览器 |

说明：`--resolve --apply` 采用先读后写——流程先把人工已填的确认清单收割进配置（幂等），再重新生成仅含未决项的清单，人工输入不会被覆盖。备份仍是好习惯。

---

# 五、服务形态

| 服务 | 启动 | 预期 |
| :-- | :-- | :-- |
| 看板 | `tools/serve_dashboard.py`（或 `output/启动看板.bat`） | 自动导图 → `对账：PASS ✓` → 开浏览器 http://127.0.0.1:8000 |
| MCP | 无需手动启动——Agent 平台按《接口文档.md》配置后自动拉起 | 自测：`tools/test_kg_mcp.py` → `全部通过（14 项）` |
| 问答验收 | `tools/ask_graph.py "问题"` | 见 §6.4 |

---

# 六、测试与验收工具（分四类，逐个含预期输出）

约定：命令均在交付目录执行，Python 用 `.venv/bin/python`（Windows `.venv\Scripts\python.exe`）。

## 6.1 环境与连通性类（部署后先跑这一组）

| 工具 | 命令 | 用途 | 预期输出（成功） | 失败排查 |
| :-- | :-- | :-- | :-- | :-- |
| `run.py --check` | 同左 | 流水线环境自检（离线） | 各项 `OK` | 缺包 → 重跑 §3.3 |
| `tools/test_llm_connection.py --limit 1` | 同左 | LLM 连通+质量底线 | 打分摘要；报告落 `var/eval/llm_conn_test.md` | 401=密钥错；超时=网络/地址 |
| `tools/test_embedding.py` | 同左 | Embedding 链路验证 | `向量维度: 4096 ｜ ... 验证通过 ✓`（维度随模型） | 报错 → 查 settings.embeddings 与 key |
| `tools/test_backends.py` | 同左 | 数据库对接端到端：Neo4j 对账 + Qdrant 记忆/决策读写检索 | `[✓] 对账 1404/1806`；`[✓] ... 全部 PASS ✓` | 见《接口文档.md》§5.2 |
| `tools/test_kg_mcp.py` | 同左 | MCP 服务自测（工具函数 + stdio 协议） | `全部通过（14 项）` | 逐项信息定位 |

## 6.2 流水线质量类（数据加工后跑）

| 工具 | 命令 | 用途 | 预期输出 |
| :-- | :-- | :-- | :-- |
| `tools/import_neo4j.py` | 同左（`--no-wipe` 增量） | 图谱灌 Neo4j + 对账 | `对账结果： PASS ✓`（当前 1,404/1,806，约 4 秒） |
| `tools/test_qdrant.py` | 同左 | Qdrant 全链路冒烟（真实调 embedding→写入→语义检索） | `检索 ... score=0.xxxx` + `Qdrant 冒烟 PASS ✓` |
| `tools/export_inventory.py` | 同左 | 生成实体台账 xlsx | `output/实体台账_*.xlsx` |
| `tools/reasoning_report.py` | 同左 | 规则推理：逾期事项推导 + 单位风险画像 | `var/eval/reasoning_report.md` |
| `tools/corpus_quality.py` | 同左 | 语料体检：区分"没抽到"与"原文没有" | 分类清单 |
| `tools/acceptance_sample.py` | 同左 | 验收抽样：分层随机抽边生成人工核对清单 | 核对清单 |

## 6.3 问答与回归类（验收核心）

| 工具 | 命令 | 用途 | 预期输出 |
| :-- | :-- | :-- | :-- |
| `tools/ask_graph.py "问题"` | 支持 `--days 30` 时间窗 | 单问验收：匹配实体 → 模型回答 → 出处硬校验 → 记忆自动沉淀 | `【匹配实体】...`、`【出处校验】✓ N 条引用全部可溯源` |
| `tools/qa_regress.py` | 同左 | 金标回归：8 用例自动跑（含婉拒不编造场景） | `结论：8 PASS / 0 FAIL`；报告 `var/eval/qa_regress_report.md`。全程约 16 分钟（每问真实调模型 2-4 轮），用后台方式跑 |
| `tools/demo_agent.py` | 问题写在文件顶部 `DEMO_QUESTIONS` | 演示：模型自主调 MCP 工具查数 → 组织带出处回答 | `🔧 [n] 工具名(参数)` 轨迹 + `【最终回答】` |

婉拒场景是验收项：图谱没有的问题（如"公司食堂菜单"）必须回答"信息不足"，模型编造即 FAIL——这是防幻觉设计的核心验收点。

## 6.4 开发/运维辅助类（日常不跑）

| 工具 | 用途 |
| :-- | :-- |
| `tools/parse_progress.py` | 大批量解析进度 |
| `tools/eval_extract.py` / `tools/time_llm_run.py` | 抽取质量评测 / batch_size 速度测试（换模型后用） |
| `tools/llm_org_classify.py` / `tools/llm_resolve.py` / `tools/llm_fill_l2.py` | LLM 辅助消解/填充（历史轮次工具） |
| `tools/replay_filled.py` / `tools/check_against_poc.py` | 历史填充回灌 / POC 对照（迁移场景） |
| `tools/record_decisions.py` | 历史拍板补记为正式决策链 |
| `tools/reembed_memory.py` | 换 embedding 模型后重嵌入回放（先预览，`--run` 执行） |
| `tools/make_delivery.py` | 生成交付包 zip（自检密钥排除）→ `自检结果： 干净 ✓` |
| `tools/ui_i18n_patch.py` / `patch_semantica_search.py` / `patch_semantica_embedding.py` / `patch_semantica_ui.py` | sem2_libs 补丁（§3.4，幂等可回退） |

离线冒烟自测：`python tests/smoke.py`（不依赖网络，秒级）。

---

# 七、参数与限制总表

系统中所有影响输出长度、截断行为、检索规模的参数集中列在这里。**改任何一项后，跑对应自测验证**（各行的"改后验证"列）。

## 7.1 模型生成（问答输出）

| 参数 | 当前值 | 作用 | 在哪改 | 改后验证 |
| :-- | :-- | :-- | :-- | :-- |
| max_tokens（demo_agent） | 4096 | 演示/Agent 每轮生成的 token 上限，含最终回答；过小会截断长表格回答 | `tools/demo_agent.py` 搜索 `max_tokens` | 跑一问看回答是否完整收尾 |
| MAX_TURNS（demo_agent） | 6 | 模型最多几轮工具调用（超出即强制收尾） | `tools/demo_agent.py` 顶部 `MAX_TURNS` | 盘点类问题能查完所有实体 |
| max_tokens（ask_graph） | 1600 | ask_graph 通道生成上限 | `tools/ask_graph.py` 搜索 `max_tokens` | 同上 |
| facts 展示上限（ask_graph） | 80 条 | 组提示词最多带多少条事实，超出附截断声明（模型被明示"共 N 条未逐条展开"） | `tools/ask_graph.py` 搜索 `facts[:80]` | 大实体问题回答末尾有截断说明 |
| 盘点样例上限 | 5 条/类 | 盘点类回答每类最多列几条代表样例（防 token 被枚举耗尽） | `tools/demo_agent.py` SYSTEM 规则 7 | 盘点回答每类 ≤5 条且带总数 |
| temperature | 0 | 生成随机性（0=同问同答，可复现） | 两个 agent 文件的 payload | —— |
| qa_regress 单例超时 | 300 秒 | 回归中单用例超时上限 | `tools/qa_regress.py` `run_case` | —— |

## 7.2 检索与取数

| 参数 | 当前值 | 作用 | 在哪改 | 改后验证 |
| :-- | :-- | :-- | :-- | :-- |
| search_entity 候选数 | 10 个 | 实体搜索最多返回数（超出带 `truncated=true` + `total_matched` 总数） | `tools/kg_mcp.py` `t_search_entity` 的 `max_n` | 搜索宽泛词看 truncated 字段 |
| 前缀匹配最低字数 | 3 字 | 关键词与实体名共享前缀低于 3 字不算命中（防"中建八局"蹭"中建科工"的候选） | `src/kg/serve/query.py` `match_entities_ex` 的 range 下限 | 搜"中建"候选不含无关单位 |
| get_entity_facts 精确直查 | 开启 | 实体名精确匹配时绕过模糊匹配（防同名/别名误替换） | `tools/kg_mcp.py` `t_get_entity_facts` 开头的 by_name 直查 | 查两个相似名公司各自返回自己的事实 |
| 每条事实出处数 | 3 条 | 每条事实最多带几条《文件》出处（冗余佐证裁剪，事实本身完整） | `src/kg/serve/query.py` `facts_struct` 的 `ev[:3]` | —— |
| 引文截断 | 60~120 字 | 出处引文展示长度（完整原文在图库中） | `query.py` / `ask_graph.py` 渲染处的 `[:N]` | —— |
| 事项列表条数 | 80 条 | `list_matters` 最多返回数（带 `truncated` 标志） | `tools/kg_mcp.py` `t_list_matters` 的 `rows[:80]` | 无过滤调 list_matters 看 truncated |
| 记忆检索条数 | 5 条 | `search_context` 默认返回的历史结论数（模型可传 limit 覆盖） | `tools/kg_mcp.py` `t_search_context` 的 `limit` | —— |
| 记忆单条摘要 | 400 字 | 检索结果单条内容截断 | `src/kg/serve/intelligence.py` `search_context` | —— |

## 7.3 数据与治理

| 参数 | 当前值 | 作用 | 在哪改 | 改后验证 |
| :-- | :-- | :-- | :-- | :-- |
| 向量维度 | 自动探测 | 随 embedding 模型（qwen3-embedding-8B=4096）；settings 里的 dimension 是校验项，填错启动报错 | `config/settings.yaml` `vector_store.dimension` | `tools/test_embedding.py` |
| 消解提名阈值 | 0.72 | 疑似同一实体的相似度门槛 | `run.py --resolve --threshold` | 提名清单数量变化 |
| 去重相似度 | 0.8 | dedup 自动合并门槛 | `config/settings.yaml` | `--qc` 输出 |
| LLM 超时 / batch_size | 120 秒 / 1 | 抽取调用超时；batch=1 防跨条串味（实测依据，勿随意调大） | `config/settings.yaml` `llm` 段 | `test_llm_connection.py --limit 5` |
| 看板搜索事项降权 | -15 分 | 事项节点在搜索结果中降权，实体置顶 | `tools/patch_semantica_search.py`（sem2_libs 重装后重跑） | 看板搜"科创大厦"项目置顶 |
| 记忆保留天数 | 不限 | 问答结论保留时长 | `config/settings.yaml` `intelligence.retention_days` | —— |

调整原则：截断类参数调大 → 注意模型上下文与 max_tokens 是否同步跟上；阈值类参数调大 → 放宽/收紧后都跑一次对应回归。

---

# 八、例行维护

| 场景 | 操作 |
| :-- | :-- |
| 每周新纪要 | 新文件放入 `input_dir` → 依序跑 §4 流水线 → `serve_dashboard.py` 重灌 → `import_neo4j.py` 同步图库。老文件走缓存秒过 |
| 纪要撤回/替换 | 按文件出处精准撤销（重算该文件）或全量重灌——全量靠 LLM 缓存兜底，未变更文件不重新调模型 |
| 换 LLM | 改 `settings.llm` 三行 → `test_llm_connection.py --limit 5` → `qa_regress.py` 回归 → 过了才上线 |
| 换 Embedding 模型 | 改 `settings.embeddings` 三行 → `test_embedding.py` → `reembed_memory.py --run`（维度自动探测，历史重嵌入回放） |
| 备份 | 备份三样即备份全部：`graph_resolved.json` + `config/`（治理沉淀）+ `var/intelligence/`（记忆镜像）；数据库可随时由这三样重建 |
| 重装 sem2_libs | 重跑 §3.4 的补装命令与三个补丁 |

---

# 九、已知限制

| 限制 | 说明/对策 |
| :-- | :-- |
| 看板前端交互较脆 | 开源 Explorer 自带 UI；查询主力走 MCP/问答，前端定位"看" |
| 看板时间轴已隐藏 | 节点无 valid_from 字段（css 隐藏，可回退） |
| 孤立实体 14 个 | 真实语料记录，新纪要进来自然连上，不清理 |
| 金标回归偶发抖动 | LLM 随机性致单例出处校验偶发 FAIL，复测即过 |
| Docker 29 端口转发缺陷 | 新建容器端口映射可能失效 → 数据库容器用 `--network host` 规避（已验证） |

---

# 十、FAQ

**Q1：不配 LLM 能用吗？**
能。`settings.llm.provider: none` → 事项字段由对接方 Agent 按"Agent 双向契约"（接口文档.md）代填；MCP 查询/看板不受影响。

**Q2：内网连不上 PyPI？**
缺包清单（semantica/openai/neo4j）走官方源；再不行用有网机器 `pip download` + 离线安装。

**Q3：neo4j/qdrant 必须装吗？**
标准形态：需要（当前为执政存储，部署见 §3.5）。特殊场景可切 `backend: memory` + `faiss` 离线兜底（配置切换、数据可由 json 重建）。

**Q4：图谱数据能导入其他引擎吗？**
能。`graph_resolved.json` 是自定义通用格式（entities + relationships + evidence），数据主权完全在交付方。

**Q5：怎么判断交付验收通过？**
§6.1 全绿 + `import_neo4j.py` 对账 PASS + `qa_regress.py` 全过 + §1.3 基线数字对上。

---

# 十一、本体扩展（加节点/边类型的标准路径）

图谱的 5 类节点 7 类边来自监理业务结构（纪要天然含"谁开会、涉及哪个项目、谁负责什么、什么期限"），已用 154 份样本验证完备。交付后灌入真实数据时，数据每周自动生长，结构按需扩展。

## 11.1 先分清两层

| 层 | 在哪 | 来源 | 交付后 |
| :-- | :-- | :-- | :-- |
| 本体层（结构） | `config/relations.yaml` | 监理业务结构设计，样本验证 | 大概率不变；新业务类别出现时按 §11.2 扩展 |
| 数据层（事实） | graph_resolved.json / Neo4j | 全部来自文件抽取 | 持续生长（每周灌库，增量 upsert） |

## 11.2 加新类型四步（以「安全隐患 HAZARD」为例）

1. **声明本体**（`config/relations.yaml`）——该文件即"加关系不改代码"的落点：

   ```yaml
   nodes:
     - type: HAZARD
       desc: 检查/巡检发现的具体隐患（区别于 MATTER 整改事项）
       key: 隐患描述片段
   relations:
     - type: 隐患引发
       from: HAZARD
       to: MATTER
     - type: 存在于
       from: HAZARD
       to: PROJECT
   ```

   边定义同时声明两端类型——不合法组合在类型检查处当场拒收。

2. **补识别规则**（`src/kg/extract/`）：L1 加隐患段落的句式/特征词；L2 填空字段扩展。quote 逐字对账、防编造校验自动对新类别生效，无需另做。

3. **小样本验证**：抽 10~20 份纪要人工核对识别准确率，达标再全量。

4. **看板显示**：`config/ui_i18n.json` 加 HAZARD 的图例配色/汉化条目（纯配置）。

## 11.3 全自动环节（扩展时不用动）

消解拍板闭环、Neo4j 灌库、MCP 取数、防编造校验、记忆/决策沉淀——全部按声明工作，不认具体类型。

## 11.4 升级触发：轻量本体 → Semantica Ontology Hub

多项目域接入、第三方要往图里写自己的类型体系、或类型数明显增长时启用（接口已实测可用）。升级后 relations.yaml 内容迁移为正式本体声明，SHACL 校验门挂在入库口——不合规边自动拒收。数据层零重建。

工作量参考：每加一类 ≈ 0.5~1 天（大头在抽取规则的验证样本）。
