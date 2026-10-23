# Customer Support · 多智能体客服系统

基于 **LangGraph、DeepSeek、PostgreSQL 和 pgvector** 的多 Agent 客服项目。覆盖商品咨询、订单账单、技术支持、多任务调度、持久化多轮、混合检索、受控事实展示和最小人工工单流程。

本仓库包含当前源码、SQL、配置模板、测试、评测集合和文档。**不包含真实密钥、数据库内容、会话记录、运行日志、原始评测对话、样例CSV或模型权重**。历史样例业务不是实时店铺，项目不执行资金退款。

## 克隆后准备

1. 使用 Python 3.11 或更新的兼容版本，在隔离环境中执行 `python -m pip install -r requirements.txt`。现有验证环境的直接依赖版本已记录在清单中。
2. 准备 PostgreSQL 和 pgvector 服务端扩展，创建数据库及专用用户。将 `.env.example` 复制为 `.env`，填写自己的 `DEEPSEEK_API_KEY` 和 `DATABASE_URL`。模板中的地址、账号和口令仅为示例；不要提交真实 `.env`。
3. 按 [数据准备说明](data/README.md) 放入五份 AdventureWorks 原文件；按 [模型准备说明](models/README.md) 放入本地 ONNX 模型和分词文件。
4. 从仓库根目录依次运行：

```bash
python import_adventureworks.py --validate-only
python import_adventureworks.py
python build_product_knowledge.py
python run_multiturn.py --thread-id demo-guest --verbose
```

游客可咨询商品；订单访问需先由管理员为一个没有历史的新会话绑定样例客户，详见下面的身份说明。没有前端页面，不会自动启动真人接待服务。

## 已有部署说明

下面的 `/root/...` 与 `/opt/conda/...` 是原有服务器部署路径，仅用于理解和维护该部署。换机器时使用自己的项目路径和 Python 环境；不需要创建相同的绝对目录。原部署可直接运行，克隆的新环境须先完成上面的准备步骤。


当前说明核对日期：2026-10-23。项目目录：`/root/rivermind-data/Agent/example/langgraph_olist_example/`。

这是基于虚构样例数据的客服系统：销售、订单账单、技术支持三个专家，Supervisor生成任务清单，程序管理多任务、依赖、多轮与工具调用。没有接入真实店铺，不执行资金退款。

## 文档入口

- [当前版本总览](docs/CURRENT_PROJECT_STATUS.md)：现有服务器使用、人工工单、能力和限制。
- [完整改进说明](docs/IMPROVEMENTS_READABLE_20261021.md)：问题、当前方法与代码；保留此前八项疑问。
- [专项文档目录](docs/README.md)。
- [评测使用与最新证据](evals/README.md)。

## 当前执行流程

```text
identity_guard → supervisor → scheduler → sales/billing/tech_support
                                ↑                 ↓
                                └──── collect ────┘
没有可执行任务 → answer汇总与工单 → END
```

明确只读任务由task_execution直接映射工具；开放式推荐先取证再由专家ReAct判断。collect更新各项任务并生成事实展示，answer发布一份最终回答。任务和消息由PostgreSQL检查点持久化，END结束一轮而非整个会话。

## 已有服务器日常运行

环境、数据、模型和向量均已配置，无需重复执行安装或导入。

```bash
cd /root/rivermind-data/Agent/example/langgraph_olist_example
/opt/conda/envs/agent/bin/python run_multiturn.py --thread-id demo-guest-20261022 --verbose
```

游客可查商品，不能查订单。管理员如需演示订单，先为一个没有历史的新会话绑定已有样例客户：

```bash
/opt/conda/envs/agent/bin/python manage_session_users.py bind --thread-id demo-bound-20261022 --customer-id 29825
/opt/conda/envs/agent/bin/python run_multiturn.py --thread-id demo-bound-20261022 --verbose
```

示例订单43659属于29825，43660属于29672。会话ID已用时另选新ID；已绑定会话不能换绑，已有游客历史不能补绑。映射是可信管理员演示配置，thread_id不是登录凭证，真实登录尚未实现。

quit/exit/退出结束；/history显示授权公开历史；/new为同一用户新建会话；/human创建或复用工单；/handoffs查当前会话最近5条工单。相同thread_id和绑定可跨进程恢复。--no-stream为一次性展示，--verbose显示工具进度，结构化日志始终启用。

## 主要模块

| 范围 | 文件 |
|---|---|
| 主图及入口 | langgraph_supervisor_1.py、run_multiturn.py、chat_runtime.py |
| 任务与调度 | task_state.py、task_scheduler.py、task_execution.py、task_result.py、task_operations.py |
| 结果协议、预算及异常恢复 | tool_result.py、tool_execution.py、request_deadline.py、task_evidence.py |
| SQL与检索 | adventureworks_service.py、catalog_filters.py、product_knowledge.py、embedding_client.py |
| 向量入库 | build_product_knowledge.py、schema_product_knowledge.sql |
| 数据导入 | import_adventureworks.py、schema_postgres.sql |
| 多轮与身份 | conversation_persistence.py、session_identity.py、manage_session_users.py |
| 事实展示与输入预算 | answer_contract.py、context_budget.py |
| 工单与诊断 | human_handoff.py、manage_handoffs.py、health_monitor.py、chat_events.py |
| 测试及评测 | tests/、evals/、run_evaluation.py |

完整57个Python文件职责与2个SQL文件在改进说明17.9。源码位于根目录，说明归docs，测试归tests；数据、模型、日志分别在data、models、logs。

## 数据与检索边界

AdventureWorks样例含31,465个订单、121,317条明细、504种商品；订单涉及266种商品，其中265种有描述。商品型号关联中英文描述，当前不依赖Olist。

全目录排序、计数、统计和精确订单使用参数化SQL；用途推荐使用关键词＋本地向量召回，RRF按名次融合。价格和类别在两路召回截断前应用，颜色和尺寸当前只在精确目录查询中支持。目录默认排除0标价，语义Top K不能证明全局最高价或完整数量。

本地multilingual-e5-small ONNX量化模型为384维，CPU推理，不需要GPU或Embedding API Key。每件有描述商品一份短文档，带passage前缀；查询带query前缀。建库按SHA-256复用未变文档，并在完整build_id构建就绪后原子切换活动指针。价格从商品表读取，未写入向量文本。

目录价与历史成交价分开。源数据缺少可靠币种、支付流水、独立发票、实时库存、优惠、物流轨迹和退款政策时保持未知。发货不等于签收或付款。退款工具只登记/复用pending申请，订单行锁与唯一约束保证幂等，不执行资金退款。

## 运行控制与日志

每任务最多6次工具请求、2次实际商品搜索、4轮专家模型调用；整轮共享最多12次工具请求、4次搜索、8轮专家模型调用，规划调用另计。相同工具和规范参数的成功只读请求本轮去重，在途请求共享结果；写入不走普通缓存，失败不当成功缓存。

REQUEST_TIMEOUT_SECONDS默认90秒，范围1–300。模型单次完整响应最多等待60秒且不超过剩余时间，SDK隐藏重试为0；SQL和ONNX也衔接剩余期限。检查点、工单、取消和资源释放有额外开销，不是精确90秒结束进程。异常后保留已完成工具证据，失败项只按权限、原参数、明确临时错误和次数规则有限重试。

普通日志位于logs/events.jsonl，包含request_id、thread_id、任务/工具及耗时、状态、缓存和拦截原因；不记录问题、完整参数、订单正文或密钥。详细原始会话保存在检查点中。模型输入有预算的副本不覆盖原历史。外部应用应使用chat_runtime.invoke_turn/stream_turn，以复用会话锁、统一期限和日志；不要把底层app.invoke当作完整应用入口。

## 人工工单与健康检查

明确人工请求、不确定的申请提交和同一只读操作持续失败可建工单；空结果、缺参数或一次普通错误不一律转人工。工单保存在human_handoffs及human_handoff_events，支持去重、领取、完成和审计。

```bash
/opt/conda/envs/agent/bin/python manage_handoffs.py list
/opt/conda/envs/agent/bin/python manage_handoffs.py show H-替换为完整工单号
/opt/conda/envs/agent/bin/python manage_handoffs.py claim H-替换为完整工单号 --operator admin
/opt/conda/envs/agent/bin/python manage_handoffs.py resolve H-替换为完整工单号 --operator admin --note "实际核对结论"
/opt/conda/envs/agent/bin/python health_monitor.py --component all
```

人工目前由可信管理员用CLI处理，没有实时坐席或自动通知，处理完成不代表退款到账。健康检查按需检测PostgreSQL、容器资源和DeepSeek模型列表；列表接口成功不代表有余额或对话生成正常，没有持续监控告警。

## 初次部署、导入与更新

以下用于新环境准备，当前服务器无需重复执行。使用requirements-postgres.txt与.env.example配置所需依赖和环境变量：

```bash
/opt/conda/envs/agent/bin/python -m pip install -r requirements-postgres.txt
/opt/conda/envs/agent/bin/python import_adventureworks.py --validate-only
/opt/conda/envs/agent/bin/python import_adventureworks.py
/opt/conda/envs/agent/bin/python build_product_knowledge.py
```

先创建可连接的PostgreSQL数据库及专用用户，安装pgvector扩展所需组件，并配置项目.env。五份原数据在data/dataset_audit下，文件虽叫csv，按无表头TSV读取；导入检查摘要、主外键与金额，事务失败回滚，相同文件重复导入不清空数据。不要执行微软原始instawdb.sql，它是SQL Server脚本。

模型目录默认为models/multilingual-e5-small/，可用EMBEDDING_MODEL_PATH指定。描述改变后运行建库脚本增量更新；更换模型或配置版本需准备完整新profile。日常聊天无需重新建库。

业务连接读取DATABASE_URL，检查点默认复用，可通过CHECKPOINT_DATABASE_URL分库。DeepSeek配置兼容读取相邻langgraph_example/.env；文档不复制私有值。身份映射、工单和PostgresSaver检查点由代码幂等初始化。详细12项配置见完整说明17.7。

## 本机PostgreSQL位置

现有部署PostgreSQL 17，127.0.0.1:5432，数据库和专用用户均为agent_customer。私有口令在项目.env，权限600；数据库仅监听本机。

数据目录/root/rivermind-data/postgresql/17/main，配置/etc/postgresql/17/main/。postgres拥有数据目录，权限700；因挂载在/root下，已有穿越目录的ACL配置，不应简单开放整个/root权限。

```bash
pg_lsclusters
pg_isready -h 127.0.0.1 -p 5432
# 重启服务器后仅在原集群未运行时启动
pg_ctlcluster 17 main start
```

## 验证

前一业务版本记录logs/recovery_verification_20261022.json：200项自动测试、112个模拟场景157轮通过自动检查。2026-10-23索引隔离修复后共210项自动测试通过，本次未重跑模型场景。这不是生产准确率或独立人工验收。

自动测试使用真实PostgreSQL、假模型和临时资料，不调用DeepSeek：

```bash
/opt/conda/envs/agent/bin/python -B -m unittest discover -s tests -t . -v
```

全部五组真实模型回归会消耗额度，按顺序运行，详见[evals/README.md](evals/README.md)。最新报告统一从当前版本总览第7节进入。原始失败报告保留用于追溯，不在使用文档中将它们写成当前未完成问题。

## 索引构建隔离（2026-10-23）

每次增量更新写入独立build_id，完整校验后原子切换active_build_id；中途失败不覆盖活动向量。已有部署先运行`python build_product_knowledge.py --migrate-only`，不重算向量。过程、测试与限制见[构建隔离说明](docs/KNOWLEDGE_BUILDS.md)。
