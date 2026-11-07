# DeepSeek 工具消息兼容修复与评测补强

更新日期：2026-10-24。适用当前客服系统；保留 thinking 模式，没有安装或升级环境。

## 1. 出了什么问题

用户说“推荐一款自行车，再查一下我的订单”，程序先检索出商品，再请销售模型挑选商品。原先检索成功，模型请求却返回 HTTP 400，错误指出缺少 reasoning_content。程序保留商品结果作为降级回答，所以看起来仍有回答，但推荐任务实际是 partial，并未完整完成。

本次定位到两个相连的问题：

1. 程序把自己执行的检索包装成 AIMessage(tool_calls) 和 ToolMessage。这两条用于保存调用及结果本身没错，但直接作为 DeepSeek Chat Completions 的模型历史发送，会声称那次工具调用由模型发起，而实际没有对应的模型推理响应。
2. 当前安装的 langchain-deepseek 适配器能从响应中读取 reasoning_content，却在请求序列化时漏掉它。即使模型真正发起工具调用，下一次请求也可能丢失必需字段。

不是“工具结果没传”，也不是“程序发一条调用消息让工具再执行”：工具已经执行，调用记录和实际结果都存在。问题在发送给模型时使用了不合适的消息表示。

## 2. 现在怎么处理

### 程序执行的工具：作为资料提供

数据库依旧保存工具调用记录、参数、调用ID和真实结果，供权限、操作账本、事实展示、恢复与审计使用。发送给模型时另建输入视图，把这些记录投影为明确标记的资料消息：已经执行了哪个工具、参数是什么、返回了什么、是否失败。模型可据此回答，不必重新检索。

程序生成的历史客服答复也以“历史答复记录”提供，避免把程序渲染的文字伪装成带推理过程的模型原始响应。这些资料消息虽在接口上使用 user 角色，内容会明确标记为资料，不是用户的新请求；系统约束仍由系统消息提供。

新程序调用带 program_execution 标记；没有推理字段的旧记录也兼容投影，不要求清空数据库或丢弃旧会话。上下文只使用本任务允许的工具证据，不带入其他任务的正文。

### 模型执行的工具：保留真实协议

模型真正返回的 AIMessage(tool_calls) 和配对 ToolMessage 继续按原协议发送。新增 ThinkingChatDeepSeek 适配器，把模型原始 reasoning_content 传回 API；不填空字符串来冒充推理，也不编造推理内容。

如果某组模型调用经过任务隔离后只剩部分调用，或者完整推理超出上下文预算，就把允许的完整结果作为资料提供，避免截断推理或伪造修改过的模型响应。原始数据库记录不修改。预算统计包含推理字段，投影后的模型输入仍受长度上限控制。

### 具体文件

| 文件 | 做什么 |
|---|---|
| `backend/tasks/task_execution.py` | 标记程序主动执行的工具记录；保留先检索后回答的机制。 |
| `backend/agent/context_budget.py` | 在模型输入中区分程序资料与真实模型调用；保持顺序、隔离和上下文预算。 |
| `backend/agent/deepseek_model.py` | 补上传输时遗漏的原始推理字段。 |
| `backend/tasks/task_scheduler.py` | 标记只保留了部分调用的历史，防止误当完整模型响应重放。 |
| `backend/agent/langgraph_supervisor_1.py` | 使用兼容适配器；异常后的收集事件不再错误标为执行成功。 |
| `evals/runtime_checks.py` | 检查执行结果、完成状态和模型故障。 |
| `tests/test_deepseek_protocol.py` | 程序资料、旧记录、隔离、推理回传和流式/非流式实际HTTP请求格式等12项自动测试。 |
| `evals/check_deepseek_protocol.py` | 使用真实DeepSeek，使用自动工具选择，完成两次“模型调用→实际查商品→模型继续回答”；报告不保存推理正文。 |

两项已有测试适配了真实协议要求：`test_reliability.py` 的原生消息保留测试、`test_session_identity.py` 的假模型工具响应现在带测试用的模拟提供方推理字段；实际业务代码不会生成这个字段。

## 3. 评测原来为什么漏掉

原检查确认检索工具执行了、展示了一件商品、下一轮能查订单，就可能认为通过；没有确认销售模型请求成功，也没有确认推荐任务最终为 completed。

现在普通成功场景同时检查：

- 不能有未预期的模型请求失败或任务 failure_code。
- 推荐任务必须完整结束，且销售模型有成功响应；仅保留检索结果的 partial 不算推荐完成。
- HTTP 400 协议错误单独失败，不会因为屏幕上有商品而放过。
- 故意注入超时的恢复场景按其预期 partial/failed 检查，不能要求所有任务一律 completed；等待订单号、拒绝越权、无结果也有各自合法状态。

## 4. 如何验证

不调用模型的自动测试：

```bash
python -B -m unittest discover -s tests -t .
```

真实模型回归需要额度，顺序执行，避免临时测试订单影响数据指纹：

```bash
python -m evals.run_current_evaluation --live
python -B -m evals.check_deepseek_protocol --live
```

## 5. 后续边界

该适配器使用当前固定版本SDK的序列化入口。以后升级SDK或更换接口格式，需要重新运行协议测试；不要单纯关闭thinking或给所有AI消息补空推理字段来掩盖问题。检索数据和商品描述属于事实资料，不能提升为系统指令。

官方依据：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 与 [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。本次错误与SDK行为同时通过实际请求和本地序列化检查定位。

补充接口检查：首次额外协议测试使用`tool_choice=required`，服务端明确拒绝（thinking不支持强制工具选择）。正常应用一直使用auto，未受这项测试参数影响。已将测试改为与应用一致的auto，并保留失败记录；详见[接口参数说明](https://api-docs.deepseek.com/api/create-chat-completion/)。
