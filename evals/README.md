# Agent 评测体系

项目：`/root/rivermind-data/Agent/example/langgraph_olist_example/`。

基础集合入口：`run_evaluation.py`；其余四组从对应模块启动。全部入口见第10节。

## 1. 范围与设计

本地执行固定场景，记录 Agent 的最终回答、路由、工具参数和结果、数据库副作用、模型调用与 Token 用量，再生成逐题报告和版本对比。不依赖外部评测平台；`--live` 调用已有 DeepSeek 服务，会消耗 API Token。

基础集合有60个场景、71轮，开发划分45个，holdout划分15个；12个开发场景标记为冒烟集。连同后续四组共112个模拟场景、157轮。holdout已参与多轮回归，名称不代表现在仍是未见盲测。

| 类别 | 场景数 | 检查内容 |
|---|---:|---|
| catalog | 12 | 全目录极值、价格排序、预算、计数、统计、颜色尺寸和空结果 |
| orders | 10 | 订单状态、金额、数量、支付、发票、签收及客户范围 |
| retrieval | 10 | 商品用途与描述召回、预算过滤、已知参考正例 |
| multiturn | 10 | 订单到商品、连续追问、预算变化、话题切换和失败后补充 |
| permissions | 10 | 越权读取、伪造身份、游客、退款权限与重复申请 |
| boundaries | 8 | 政策缺失、不申请退款、缺少订单号、未知库存优惠、错误订单号 |

同一题目家族不跨数据划分，运行时检查一致性。现有集合属于样例业务回归，不等于生产流量分布或独立未见基准。

## 2. 运行命令

```bash
cd /root/rivermind-data/Agent/example/langgraph_olist_example

# 列出场景；默认不调用模型
/opt/conda/envs/agent/bin/python run_evaluation.py --list --split all

# 静态检查测试集与参考文件；不调用模型
/opt/conda/envs/agent/bin/python run_evaluation.py --validate --split all

# 12个冒烟场景，调用DeepSeek
/opt/conda/envs/agent/bin/python run_evaluation.py --live --smoke

# 开发集；日常修改后先使用这一组
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split dev

# holdout划分
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split holdout

# 全部60个场景
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split all

# 指定开发场景
/opt/conda/envs/agent/bin/python run_evaluation.py --live --ids C01,M01,P04

# 重复采样；会增加模型消耗
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split dev --repeat 3
```

数据库需要运行，Python 环境继续使用现有 `agent`。不需要重新向量化、不需要额外 Embedding API Key。

## 3. 自动评分的含义

### 数据库事实与回答分别检查

价格、计数、统计和订单金额通过独立 SQL 取得参考事实，不把待测工具自身返回的内容当作唯一正确答案。除了核对工具结果，还检查最终回答中是否包含被要求的关键数值或身份信息。

例如：工具返回正确价格，但最终回答把价格写错，仍然判为自动规则失败；局部型号搜索碰巧找到了最高价格，也不能作为全目录排名依据。

### 允许不同的合理执行路径

不要求固定调用顺序。已有订单结果包含商品描述时可以直接复用；已知数据集没有支付流水或独立发票时，可以说明无法核实，不强制多查一次。相同查询同时返回列表和统计时，评分器按任务选择对应证据，避免后来的统计结果覆盖正确的列表证据。

价格条件写法不同时，由独立SQL比较有效商品集合，只有集合一致才认可等价筛选；不会直接放行缺少商品的价格区间。查询前三项后扩大候选来确认并列价位，也可以用正确排序的候选前缀提供证据。

每题的 `allowed_tools`、路由集合和执行次数预算在测试集里声明。预算是该类任务的效率检查，不代表 Agent 永远只能调用一次工具。

### 权限与副作用

检查成功订单工具的客户归属，同时检查临时订单中的退款申请数量。只说“没有申请”但实际产生写入，不能通过。游客或其他客户的结果不能返回成功。权限、非预期写入、评测隔离或清理失败会阻止自动回归通过。

报告中的“自动规则通过率”不是完整任务质量准确率。自然语言语义、推荐适用性以及每一项陈述的依据，目前仍需人工审查。

## 4. 检索评测

参考文件：`/root/rivermind-data/Agent/example/langgraph_olist_example/evals/retrieval_gold.json`。

10组参考正例基于固定商品名称、型号和原始描述编写选择规则，并保存源描述证据。标签不由待测模型或待测检索结果生成，但它仍是需要独立人工复核的初稿；没有声称穷尽全部相关商品。

报告分开给出两个通道：

- `agent_candidates`：Agent 实际通过搜索、精确查询或详情工具取得的候选。
- `retriever_probe`：在该轮 Agent 完成后，对原问题直接调用混合检索，固定取前5条；不消耗额外LLM Token。

两个通道都报告已知正例 `Hit@5`、`MRR@5` 和“参考集合 Recall@5”。后者的分母是初稿参考正例数量，不能称为完整相关商品召回率。指标不能单独证明推荐理由或适用性。

后置检索探针单独记录耗时，不计入 Agent 本轮响应延迟；它可能使后续场景的 Embedding 缓存变热。首次检索可能包含模型冷启动，比较延迟时需结合这一条件。

首次准备参考文件的命令如下，当前服务器已经执行过，不要每次运行：

```bash
/opt/conda/envs/agent/bin/python run_evaluation.py --prepare-gold
```

已有参考文件不会被命令覆盖。调整标签应明确更新版本和文件校验值，并重新建立基线，不能用新标签去宣称旧版效果提升。

## 5. 性能与消耗

记录每轮首段公开正文延迟、整轮耗时、工具请求数、实际开始执行数、缓存命中和模型调用次数，汇总 P50/P95。

- 首段正文可能是工具调用前的说明，不等于最终答案已经就绪。
- 整轮耗时包含路由与模型调用，不含临时订单准备、后置检索探针和报告写盘。
- 并发工具耗时不能直接相加当作整轮耗时。
- 模型调用数指框架回调记录，不等于 HTTP 重试次数。
- Token 优先读取模型返回的 usage；缺失记为未知，并报告覆盖率，不记作免费或零消耗。
- 不按可能过时的单价估算货币费用。
- 样本量只有几十个场景；P95是描述性统计，不代表生产SLA。

## 6. 报告与版本对比

每次运行创建独立目录：

```text
/root/rivermind-data/Agent/example/langgraph_olist_example/evals/reports/<UTC时间-随机后缀>/
├── manifest.json
├── report.json
├── summary.md
├── review_queue.jsonl
└── events.jsonl
```

- `manifest.json`：测试集、参考标签、评分器版本、代码哈希、模型配置、依赖版本及源数据快照指纹。
- `report.json`：逐轮问题、最终回答、工具参数和结果、评分原因、Token、耗时和副作用。
- `summary.md`：汇总指标、失败列表、自动门禁状态与版本比较。
- `review_queue.jsonl`：待人工审查的回答、证据引用及审查要点，默认 `pending`。
- `events.jsonl`：该次评测独立的结构化事件，不混入正常命令行日志。

每完成一题落盘。运行中断时保留已完成结果，未完成整个计划不能通过门禁。报告按开发集与holdout划分保留逐题标签，不把二者混为调参结果。

模型返回HTTP 401、402或403时会停止后续模型请求及剩余场景，保留报告并标记服务不可用；这些场景不进入Agent质量、路由和延迟评分的分母，整次运行也不能通过门禁。补充余额或修正鉴权后重新运行，不把两次不完整结果拼成一次基线。

下一版使用相同样本与配置进行比较：

```bash
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split all \
  --compare /root/rivermind-data/Agent/example/langgraph_olist_example/evals/reports/<原基线目录>/report.json
```

只有测试集、标签、评分器版本、源数据、选中场景、重复次数和实际完成场景一致时才计算通过率变化与新增失败；不一致时明确标记不可比。模型和代码可以是被比较的变量，但需要结合记录解释，不能把描述性变化当作统计显著提升。

退出码0表示全部选定自动规则通过且源数据未变化；1表示有失败或门禁未通过；参数和静态校验错误会提前报错。退出码1仍然保留完整报告，不等于没有完成评测。

## 7. 数据隔离

每个场景创建带随机标记的临时订单、明细、会话和客户映射；合法退款与重复退款只针对临时订单。未授权退款也只允许触及临时订单，以便观察归属校验是否真的阻止写入。

回调在工具函数体执行前阻断任何指向临时范围之外的写入，包括模型意外要求退款原始订单；拦截异常必须传播，不能只记日志后继续执行。运行后严格按本场景随机标记清理，不按宽泛前缀删除其他人的数据。

运行前后对原始订单、明细和商品视图做数量及内容摘要比较，源数据变化时门禁不通过。数据库连接或清理异常不会被悄悄忽略。若同一数据库同时有外部程序改写原始数据，摘要变化也需要人工核对原因。

报告可能包含固定测试问题、虚构样例订单和工具证据，目录权限700，JSON/Markdown报告权限600；不记录API Key、HTTP凭据或模型reasoning字段，不上传第三方评测平台。普通生产事件日志仍保持不记录问题、参数及结果正文的行为。

## 8. 文件与自动检查

| 文件 | 作用 |
|---|---|
| [cases.jsonl](cases.jsonl) | 固定场景、身份、划分、期望与预算 |
| [build_cases.py](build_cases.py) | 可审查的场景构建来源；日常评测不自动重写题目 |
| [dataset.py](dataset.py) | 数据集约束与参考标签准备 |
| [fixtures.py](fixtures.py) | 临时订单和会话生命周期、源数据指纹 |
| [observer.py](observer.py) | 执行轨迹、Token采集与写入拦截 |
| [grading.py](grading.py) | 独立参考事实与分项规则 |
| [reporting.py](reporting.py) | 指标、人工队列和可比性检查 |
| [runner.py](runner.py) | 批量运行、逐题落盘和清理 |
| [run_evaluation.py](../run_evaluation.py) | 统一命令入口 |
| [test_evaluation.py](../tests/test_evaluation.py) | 评测器自身的防误判检查 |

评测从chat_runtime公共入口观察实际任务规划、调度、工具与回答；独立参考SQL和评分器不代替业务实现。回调记录实际调用，写入观察器阻止越出临时范围的副作用。

```bash
/opt/conda/envs/agent/bin/python -m unittest -v tests.test_evaluation tests.test_session_identity tests.test_postgres_service tests.test_chat_runtime tests.test_product_queries tests.test_tool_execution
```

这些自动检查不调用DeepSeek，覆盖错误答案、局部最高价冒充全局、并列证据被统计覆盖、等价价格筛选与真正遗漏商品的区分、零计数证明无商品、重复召回、历史复用、越权成功结果、写入拦截、未知Token、服务不可用、数据变化后的比较拒绝、部分运行门禁及临时数据清理。部分数据库集成检查会创建并清理临时测试数据。

## 9. 阅读结果时的限制

完整自然语言事实一致性、中文翻译是否准确、商品适用性、是否出现未被规则覆盖的编造，仍由人工队列检查。没有部署LLM裁判，也没有把规则通过率当作“幻觉率”或人工准确率。

技术支持使用按需真实健康探针和静态错误码解释；探针只证明本次检测范围，不是完整故障根因诊断或持续监控。缺失的退款政策、实时库存等数据，正确的“无法核实”应得到认可，不把缺失知识包装成已有能力。

## 10. 当前完整评测入口与最新证据

五组实际入口如下。最新已部署版200项自动测试通过，112个模拟场景、157轮自动检查通过；报告见[当前版本总览](../docs/CURRENT_PROJECT_STATUS.md)第7节，代码核验记录为logs/recovery_verification_20261022.json。旧报告保留作追溯，不代表当前待办；这次文档整理未新运行评测。

| 集合 | 实际入口 | 场景/轮数 |
|---|---|---|
| 基础集合 | run_evaluation.py --live --split all | 60 / 71 |
| 复合任务 | python -m evals.multitask_runner --live | 10 / 15 |
| v1回归 | python -m evals.validation_runner --live --dataset validation_v1 | 24 / 40 |
| v2回归 | python -m evals.validation_runner --live --dataset validation_v2 | 8 / 17 |
| 工单功能验收 | python -m evals.recovery_runner --live | 10 / 14 |

在项目根目录使用原有agent环境，完整命令为：

```bash
/opt/conda/envs/agent/bin/python run_evaluation.py --live --split all
/opt/conda/envs/agent/bin/python -m evals.multitask_runner --live
/opt/conda/envs/agent/bin/python -m evals.validation_runner --live --dataset validation_v1
/opt/conda/envs/agent/bin/python -m evals.validation_runner --live --dataset validation_v2
/opt/conda/envs/agent/bin/python -m evals.recovery_runner --live
```

以上命令会调用DeepSeek并消耗额度，按顺序运行，避免多个集合同时创建临时订单干扰源数据指纹。不可只用python -m evals.runner代替基础入口：该模块不负责触发完整main流程。基础入口缺少--split all时默认仅开发集，不是全部60项。

故障注入与临时申请只用于测试，测试结束仅按生成的标记和thread_id清理本次临时数据/工单，不清理正常用户资料。遇到服务不可用或未完成场景，应如实记录，不能算通过。

v1/v2已经参与修复，继续使用时是回归；新增工单集合在实现后编写，也是模拟功能验收。上文早期“holdout划分”是最初数据划分名称，后续多次运行后的成绩不能再当作新的独立盲测或生产正确率。AI辅助复核也不等于独立人工验收。
