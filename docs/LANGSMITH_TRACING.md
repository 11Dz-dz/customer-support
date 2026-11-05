# 用 LangSmith 排查客服执行链路

## 解决什么问题

此前 logs/web-events.jsonl 能看到路由、工具开始结束和部分结果摘要，但排查一条回答常要拼接日志、会话状态和代码。现在把一次聊天请求作为一个 LangSmith 根链路，主图节点、路由模型、专业 Agent、实际工具调用以及 collect/answer 的输入输出挂在同一条链路下。流式与一次性调用共用这一入口。

这能帮助区分“规划错了”“没有执行工具”“工具被权限/预算拦截”“工具执行失败”“数据正确但展示计划解析失败”。追踪提供证据，不会自动判断每个业务结论正确，也不是用户会话存储、退款审计或支付系统。

## 现在怎么用

1. 登录 [LangSmith](https://smith.langchain.com/)，打开 customer-support 对应的 Tracing 项目；若控制台采用 Agent 工作区，则打开配置的 Agent/environment。
2. 在网页客服重新发送一个问题，再到 LangSmith 刷新。查看最新的 customer_support.turn；输入为该轮用户消息，输出为最终回答和任务结果摘要。
3. 展开 customer_support.graph，按执行顺序看 identity_guard、supervisor、scheduler、sales/billing/tech_support、collect、answer 等实际经过的节点。节点按需要执行，不保证每轮都经过所有专家。
4. 查看路由模型的实际输入和结构化规划输出；再检查任务类型、订单/商品编号、focus、等待补充信息等字段。
5. 在工具节点看名称、参数、返回的 ToolResult、耗时和异常。缓存命中、预算/权限拦截属于事件，可能没有实际工具执行节点，不能把它们误读成漏日志。
6. 如果工具数据正确但回答不对，看 collect 的回答计划、任务结果、answer_verified 中的 plan_fallback，以及最后 answer 节点的正文。

例如“查询退款进度”：合理路径是规划为 order_query＋focus=[refund]，核对会话身份，通过 query_refund_status 读数据库，程序展示已核实状态。用户问申请退款则应指引订单页，不应看到 request_refund 写入执行。

根节点技术执行完成，不代表业务全部成功；任务还可能是等待输入、部分完成、权限拒绝或失败。需要同时看工具返回和 task_results，不能只看链路是否绿色。

## 和本地日志怎样对应

每次请求已有的 request_id 与 LangSmith 根 trace_id 是同一个 UUID 的两种表示（本地一般省略横线）。日志增加 langsmith_trace_id 便于直接对应；thread_id/session_id 用于关联同一会话的多轮，web_run_id 对应网页运行记录。后台保留这些关联字段，普通用户页面不展示请求编号。

已有日志仍在 logs/web-events.jsonl。LangSmith 只记录接入后执行的新请求，不能补出过去未采集的真实模型调用、耗时或完整链路。不要为了补链路重新执行退款等写操作。订单页申请与管理员审核仍由数据库申请/审计表记录，它们不属于聊天 Agent 链路。

## 配置和检查

真实密钥仅写服务器项目 .env，不提交 Git。SDK 已在当前环境中，requirements.txt 显式固定 langsmith==0.14.8。

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=填写自己的密钥
LANGSMITH_PROJECT=customer-support
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
# 需要指定工作区时填写：
# LANGSMITH_WORKSPACE_ID=控制台中的工作区ID
# 若工作区采用注册Agent模式：
# LANGSMITH_AGENT_ID=控制台中的Agent标识
# LANGSMITH_AGENT_ENVIRONMENT=development
```

区域地址必须与账号一致，EU 等其他区域请使用控制台提供的 API Endpoint。修改配置后正常重启 Web 服务。不要把密钥贴在聊天、代码或命令参数里。

```bash
cd /root/rivermind-data/Agent/example/langgraph_olist_example
/opt/conda/envs/agent/bin/python manage_tracing.py status
/opt/conda/envs/agent/bin/python manage_tracing.py check
# 只有具备读取权限的密钥才可选：
/opt/conda/envs/agent/bin/python manage_tracing.py check --read
```

check 只上传一条连接检查，不调用 DeepSeek，不查询或变更订单。write=accepted 表示接口接受写入；读取确认是另一步。Tracing Only 密钥不能读取链路，读取返回 403 不代表写入失败，也不需要为了追踪更换密钥；在你登录的 LangSmith 页面查看即可。

## 上传内容与边界

默认上传排查需要的聊天/提示词、节点状态、模型和工具输入输出、耗时、模型提供的 Token 用量及安全事件。模型未返回用量时不补造；Token 数不等于已确认的实际账单。当前业务是样例数据。未来接真实客户资料时需要按业务要求进一步控制上传范围。

发送前对副本过滤已配置的凭证、密码/API Key/登录令牌等已知字段和常见密钥格式；排除模型 reasoning_content/thinking 及可能包含正文的模型 repr。嵌套 JSON 工具正文也做字段过滤。过滤不修改实际模型消息、工具返回或持久化的检查点，所以不改变 DeepSeek 工具协议。它不是对所有自由文本个人信息的完整匿名化保证。

如只保留层级、耗时和元数据，可另设置 LANGSMITH_HIDE_INPUTS=true、LANGSMITH_HIDE_OUTPUTS=true。关闭 LANGSMITH_TRACING 并重启即可停用。未配置密钥或初始化失败时，使用原有客服流程；上传使用 SDK 后台队列，上传错误记录安全告警，不重试业务操作、不阻断聊天。正常退出会尽力排空队列，进程被强制杀死或网络不可用仍可能丢失尚未送达的事件。

## 代码在哪里

| 文件 | 作用 |
|---|---|
| langsmith_tracing.py | 统一客户端、请求根链路、事件关联、上传过滤及后台队列收尾。 |
| chat_runtime.py | 流式/一次性聊天入口都包住完整执行过程，保存最终回答。 |
| chat_events.py | 保留本地事件，并将路由、缓存、拦截、结果验证等事件关联到当前链路。 |
| webapp/runtime.py | 工作线程传递网页运行关联信息，用户页面继续只接收公开事件。 |
| manage_tracing.py | 安全配置检查与无模型连接测试。 |
| tests/test_tracing.py | 真实 SDK＋假网络传输，验证层级、异步模型、流式、隔离、过滤和失败降级。 |

官方依据：[LangGraph 追踪](https://docs.langchain.com/langsmith/trace-with-langgraph)、[上传过滤](https://docs.langchain.com/langsmith/mask-inputs-outputs)、[密钥权限](https://docs.langchain.com/langsmith/create-account-api-key)。

### 当前版评测关联（2026-10-30）

`run_current_evaluation.py` 给每轮根链路添加 `eval_batch_id`、`eval_case_id`、`eval_repeat`、`eval_turn`、`eval_dataset_version`，可以在LangSmith控制台按批次与场景筛选。报告的 `trace_links.json` 保存本地评分到trace的对应关系。普通用户页面仍不展示请求编号。

评审分数在服务器本地报告中保存；Tracing Only密钥只写链路，不能发布Dataset/Experiment或回读运行。没有把“写入追踪”说成“已经在LangSmith完成云端实验”。OpenEvals/AgentEvals的默认独立追踪关闭，目标业务仍使用原有过滤后的显式根链路。说明见 [当前评测](../evals/CURRENT_EVALUATION.md)。


## 本次验证（2026-10-29）

264项自动测试通过，其中新增8项追踪测试；使用实际SDK和假的HTTP传输，不上传测试中的业务数据。验证流式/一次性、异步模型线程、同一根链路、并发隔离、过滤不改原对象，以及缺密钥/初始化失败时业务继续。

真实DeepSeek的只读商品问题记录到1条根链路、15个步骤，包含路由模型、商品详情工具、collect和answer，父子trace_id一致，结束状态已提交；LangSmith接收接口返回202，未发现已配置凭证出现在上传副本中。只写密钥无runs:read权限，未通过读取接口宣称完成远端回读；可在控制台查看。原始商品和订单指纹未改变。

早期手动连接探测请求失败，改用固定内容的同步探测后写入/更新成功，读取403确认是仅写权限。早期实测脚本误将SDK运行字典按不存在的op字段分类，导致把已收到202的运行统计为0；修正验证脚本后取得上述15步结果。早期记录保留，不作为业务失败或最终通过记录。

部署后6项网页检查通过：登录、提交只读商品问题、回答、隐藏请求编号、刷新恢复历史和无浏览器异常；本地request_id与LangSmith追踪编号匹配，当前服务日志无上传错误，临时验收会话已限定清理。详细私有验证记录位于logs/langsmith_verification_20261029.json。
