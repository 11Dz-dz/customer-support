# Web客服应用

更新：2026-10-31。现有命令行入口继续可用；新增FastAPI接口和Vue 3＋TypeScript页面。

## 解决什么问题

原项目只能在终端使用，身份映射需管理员配置，人工工单也只有命令行操作。现在增加账号登录、会话列表、公开历史、流式聊天、用户工单、订单退款申请和管理员审核/工单处理页面。底层继续复用原LangGraph流程、工具权限、PostgreSQL检查点及工单服务。

本版本使用本地账号认证：管理员创建账号时指定样例customer_id，用户不能在页面或请求参数中修改身份。它不是第三方身份认证、真实店铺接入或生产验收。

## 页面与后端怎样工作

1. 登录后，服务器设置HttpOnly、SameSite Cookie；密码使用带随机盐的scrypt哈希。会话有效期12小时。HTTPS站点自动使用Secure Cookie。
2. 新建会话生成服务器端thread_id，并按登录账号的客户映射绑定。每次历史、消息、运行事件和用户工单查询都核对所有权；管理员也不能绕过聊天会话归属。
3. 发送消息时前端生成client_request_id，后端在事务中登记运行；同一编号重复发送返回原run_id，不重复执行。相同编号更换内容会拒绝。
4. 一个专用工作线程从头到尾消费现有同步stream_turn，不在异步HTTP事件循环上阻塞，也不把生成器的ContextVar上下文拆到不同线程。
5. 浏览器读取SSE事件。事件只包括经过筛选的进度、公开回答片段、输入回执、结束与安全错误；不返回原始工具正文、参数、推理字段或图状态。
6. 浏览器断线不取消已经开始的业务。事件有持久化游标，重连读取同一个run_id，刷新后可恢复运行中的输出。服务重启会将遗留运行标为interrupted，提示核实已有结果，绝不自动重放写入。

运行结束状态completed表示Web请求已执行完毕，不等于本轮所有业务任务都成功；缺订单号、部分失败或待人工处理仍由原任务状态和回答说明。

## 并发与边界

默认最多同时运行2个Web Agent请求，支持不同账号同时使用；同一会话只有一个执行者，但处理中可继续接收补充或排队消息。接收、去重和最终交付边界由PostgreSQL事务保护；每个活动运行最多8条输入，容量超额返回429，停止收尾或答案已确认交付等冲突返回409。详见[处理中追加消息](INFLIGHT_MESSAGES.md)。

当前仅支持**一个Uvicorn worker**，通过数据库独占服务锁防止误开第二个进程。单进程内使用受限线程池并发；这不是已完成多实例扩展或高并发容量验证。

其他边界：账号支持用户自行注册，也保留管理员创建；尚无忘记密码、外部SSO；历史列表暂取最近100个会话；工单页提供领取和完成记录，没有真人即时聊天或主动通知。事件和登录资料需要后续按实际使用量配置清理策略。SSH隧道版本只监听回环地址；公开部署需HTTPS和正确代理配置。

## 新环境部署

先按根README准备Python、PostgreSQL、pgvector、样例数据和模型，再安装`requirements-web.txt`。新增表来自`database/schema_web.sql`及`database/schema_refunds.sql`。前者存Web身份和运行，后者补充退款提交去重、申请范围/金额快照和审核审计；保留原refund_requests，不覆盖订单、商品或原会话。应用启动会执行幂等建表。

前端构建需要Node.js兼容版本和pnpm：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm run build
pnpm test
```

从项目的 `frontend/dist` 部署构建产物，FastAPI 会同源提供静态文件。可以在构建环境安装 Node，部署环境只需 Python 服务。代理配置示例见 `deploy/nginx.conf.example`。启用代理或域名时，将 `WEB_BASE_URL` 设为浏览器使用的准确 Origin，转发 Host 并关闭流式缓冲。

```bash
# 密码通过交互输入，不写在命令参数中
python -m scripts.manage_web_users customer --display-name 演示客户 --customer-id 29825
python -m scripts.manage_web_users operator --display-name 管理员 --role admin
python -m uvicorn webapp.api:app --host 127.0.0.1 --port 8000 --workers 1
```

WEB_BASE_URL默认http://127.0.0.1:8000；WEB_MAX_CONCURRENT_RUNS默认2，范围1–8。切换为公网HTTPS后Secure Cookie自动启用。不要通过开放PostgreSQL或暴露DeepSeek Key让浏览器直接连接业务服务。

## 关键接口

| 方法和路径 | 功能 |
|---|---|
| POST /api/auth/login、/api/auth/logout；GET /api/me | 登录、退出及当前用户 |
| GET/POST /api/conversations | 个人会话列表与新建 |
| GET /api/conversations/{id}/messages | 授权公开历史与活动运行 |
| POST /api/conversations/{id}/messages | 提交/追加消息；需client_request_id，可选mode=steer/followup |
| POST /api/conversations/{id}/stop | 请求在安全检查点停止后续处理 |
| GET /api/runs/{id}/events?after=游标 | SSE进度与回答；可重连 |
| GET /api/orders；GET /api/orders/{order_id} | 当前账号订单列表（游标分页）及订单/申请详情 |
| POST /api/orders/{order_id}/refunds | 用户确认整单申请；原因、scope、confirmed和client_request_id必填 |
| GET /api/admin/refunds?status=pending | 管理员查看申请 |
| POST /api/admin/refunds/{refund_id}/review | 管理员审核；approved/rejected及说明必填 |
| GET /api/tickets | 当前账号的人工工单 |
| GET /api/admin/tickets、/{ticket_id} | 管理员列表及详情 |
| POST /api/admin/tickets/{id}/claim、/resolve | 管理员领取和完成；处理人取自登录身份 |

所有写接口校验Origin；登录后的写操作还校验CSRF Token，接口没有开放跨域。登录失败会限制尝试次数；校验错误不回显密码等输入。前端通过Vue文本插值展示回答，不执行模型返回的HTML。

## 文件与验证

- `webapp/store.py`：账号、登录会话、所有权、去重及公开事件持久化。
- `webapp/runtime.py`：受限工作线程、同进程ContextVar、重启中断及公开历史。
- `webapp/api.py`：HTTP接口、认证、CSRF和流式事件。
- `frontend/`：登录、会话、聊天、工单与管理员页面；`src/OrdersPanel.vue`负责订单与退款审核。
- `backend/orders/refund_service.py`、`database/schema_refunds.sql`：订单归属检查、整单申请事务、提交去重与审核审计。
- `tests/test_refund_web.py`：隔离PostgreSQL中的申请、审核、并发、权限、回滚和旧申请兼容检查。
- `scripts/manage_web_users.py`、`scripts/manage_web_service.py`：可信本地管理入口。
- `tests/test_web.py`：真实PostgreSQL临时表、模拟模型下的Web功能与权限检查。
- `frontend/tests/sse.test.mjs`：网络分块、事件拼接及游标内容解析。

验证时包含：重复请求不重复执行、两个用户并发访问同一订单只允许所属客户看到、跨用户会话/运行不可读、普通用户不能管理工单、领取/完成留痕、重启不自动重放。新增测试只操作临时账号、会话和工单，结束限定清理；不把自动检查等同于生产容量或真实客户验收。

## 退款申请与审核

现在用户操作：左侧「我的订单」→查看订单→「申请退款」→填写原因→勾选整单确认→「确认提交申请」。后台先根据登录账号找客户，再锁住归属订单；从数据库取订单应付总额作参考快照，不接受浏览器指定客户、审核状态或退款金额。数据源缺少支付流水与退款政策，不能把参考快照当成实付/实际可退金额，也不据发货状态自动批准退款。

同一提交编号重试返回原申请；更换内容沿用同编号返回409。不同编号对同一活动申请也只复用，原因不会被覆盖。拒绝后可新申请；旧编号重试仍返回旧的拒绝记录。事务把申请、范围/快照与提交编号一起保存，任何一步失败整体回滚。

管理员登录后打开「退款审核」，选择申请并填写说明，点击审核通过或拒绝。审核状态改变与审计记录同一事务提交；行锁保证两名管理员不能互相覆盖结论。当前仅支持pending→approved/rejected；没有撤销审核、上传附件、逐件退款或通知接口。退款申请和人工工单是两类记录，完成工单不会改变申请或执行资金退款。

聊天只负责指引入口、解释和查询。billing_tools移除request_refund，改为query_refund_status；旧函数即使直接被调用也只返回CHAT_WRITE_NOT_ALLOWED，工具执行边界同样禁止。任务名refund_request为兼容历史检查点而保留，现在只表示“申请入口指引”。退款进度用order_query＋focus=[refund]，缺订单号先问，随后程序调用只读查询读取数据库中的当前申请及审核说明；网页创建的申请也能查到，不依赖聊天先有记录。历史申请正文继续可读，旧的受信任业务适配器保留用于兼容测试，不作为Agent能力或HTTP接口。

页面显示申请编号作为业务凭证，不显示聊天request_id或提交去重编号。服务未接入支付渠道，任何状态都不表示本系统已退款到账。

自动测试中原来“聊天可以写退款”的断言改为“只指引且零写入”，保留原业务方法的权限/幂等兼容检查；增加网页流程检查。旧评测中要求Agent调用request_refund的用例属于旧能力验收，不能直接当作本版本通过标准，也没有宣称本次重跑全部历史DeepSeek集合。


## 订单列表与退款状态联动

之前订单列表只读取orders.status_code，申请记录虽已保存，列表却一直仅显示“已发货”；返回列表也可能沿用旧缓存。现在列表和详情都根据数据库中的退款申请生成display_status：待审核显示“退款待审核”，通过显示“退款审核通过”，拒绝/取消也显示对应结果；同时保留独立的履约状态。没有申请时继续显示原订单状态。活动申请优先于历史拒绝或取消记录，避免重新申请后被旧记录遮住。

提交后详情重新读取，返回列表重新查询；「刷新列表」也会同步打开的详情。当前根据持久化的申请状态展示，无需另写一份订单退款状态，既有申请也能立即显示；不会把orders.status_code的发货事实改成退款状态，更不会把审核通过当作已退钱。

新增两项真实PostgreSQL检查覆盖无申请、待审核、拒绝、再次申请、通过、历史取消及旧记录干扰，并校验履约状态始终保留。


## LangSmith执行链路排查

网页工作线程中每个顺序对话段对应一条LangSmith根链路；排队输入可在同一个web_run_id下产生多个顺序根链路，metadata包含web_run_id与thread_id；普通用户页面不展示请求编号，也不直接持有LangSmith密钥。服务端统一过滤上传副本，流式输出及会话持久化保持原入口。配置、排查与仅写密钥的边界见[LangSmith说明](LANGSMITH_TRACING.md)。退款申请/审核接口仍用数据库审计，不伪装成模型工具执行。


## 寒暄与按当前账号定位订单

**遇到的问题：**用户说“你好”进入业务规划后返回格式错误；说“我有一个正在退款的订单”又被要求先提供编号。日志确认前者发生了OutputParserException，后者原本只有按订单号查详情的能力，没有按当前客户定位订单的工具。

**怎样改：**新增conversation任务和基础意图，常见的独立问候、感谢、告别和能力询问直接回复，不依赖业务规划模型；其他表达可由模型归为同一意图。混合“你好，查我的订单”仍走业务规划，不能因为含问候词而吞掉查询。普通寒暄不调用商品或订单工具，也不冠以销售顾问标签。

新增只读query_my_orders，由系统绑定的客户身份限制SQL范围，不接受模型或聊天提供customer_id。order_query增加order_scope：specific为指定订单；mine为当前客户的候选查询。refund_filter支持活动申请（pending/approved）、精确状态或所有有退款记录的订单，不把它当成支付渠道正在退钱。

查到唯一一笔时，把经工具验证的订单号写入该任务，后续“这个订单/这个申请”可直接继续；多笔时列出订单号、状态与订单应付金额，等待用户选择，不猜一笔。即便只展示1笔，只要总候选多于1笔也不自动认定唯一。订单应付金额不等于实付或实际可退金额。

重新查询账户范围时会清除之前选定的单笔编号及旧的退款筛选；无结果只说明当前账号在该筛选下没有匹配，不复用旧申请掩盖本轮结果。给出错误编号或明确说稍后补编号时，仍保留等待补充，不擅自扩大为账户查询。游客/未绑定账号无法枚举订单。

**文件职责：**backend/conversation/conversation_replies.py管理常规对话；backend/orders/adventureworks_service.py新增按当前客户的只读列表；backend/tasks/task_state.py与backend/tasks/task_execution.py管理查询范围及执行映射；backend/agent/langgraph_supervisor_1.py接入意图、工具及单笔/多笔的状态交接；backend/agent/answer_contract.py只按工具事实展示候选。账单工具由4个增为5个，总计12个业务工具。没有改动数据库表结构或原始订单。


## 处理中连续发送

**问题：**第一条未答复时，预算等补充被前后端忙碌控制挡住。

**改进：**新增数据库收件箱，继续保留单会话单执行者；默认在图检查点纳入补充并更新受影响任务，也可明确排到下一轮。确认交付后的消息自动排队，不覆盖已开始输出的回答；新增协作式停止和回执。队列、时序、容量、重启边界及文件职责见[完整说明](INFLIGHT_MESSAGES.md)。


## 自行注册

新增登录页注册入口和POST /api/auth/register，新账号固定普通用户且不绑定订单客户；密码校验、重复账号、数据库限流和客户权限边界详见[账号注册说明](ACCOUNT_REGISTRATION.md)。
