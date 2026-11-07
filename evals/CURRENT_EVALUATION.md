# 当前版评测：问题、做法与报告读法

入口 `evals/run_current_evaluation.py`。较早的评测及报告按原版本解释，当前运行入口见[评测指南](README.md)。

## 为什么要改

以前一部分用例希望客服在聊天里写入退款申请，现在产品要求用户在订单页面确认提交；继续沿用旧预期会把正确的入口指引判成失败。问候、无订单号时查自己的订单、多个候选等待选择等新行为，也没有完整进入旧集合。

仅检查答案里有没有某个数字也不够：把200写在商品小计下、把180写在应付总额下，即使两个数字都出现，含义仍然错。列表里漏掉customer_id字段，也不能成为跳过订单归属检查的理由。

## 现在怎么评

| 层次 | 检查什么 | 实现 |
|---|---|---|
| 程序规则 | 独立SQL核对金额、每条订单归属、退款状态、任务完成/等待状态；数据库不能被聊天改写 | 原事实评分器及 `current_contracts.py` |
| 工具轨迹 | 执行过的工具是否属于允许范围；查自己的退款订单时是否用了正确的状态条件 | AgentEvals，`frameworks.py` |
| 回答质量 | 是否回应本轮问题，解释有无依据，是否虚构政策、支付或退款到账 | OpenEvals＋DeepSeek JSON模式 |

不要求每个问题只有一种工具顺序。例如先查商品再查订单、先查订单再查商品，都可能正确。明确账户查询的筛选条件必须对；已有历史证据可复用，不能为了满足轨迹模板强迫重复调用。

轨迹来源于工具真正执行的回调；另存 `tool_request_origins` 区分程序安排和模型发起。程序生成的工具消息不会被宣传成模型自主决策。LangGraph节点访问也会记录。

## 场景范围

当前集合 **80个场景、95轮**：继承并调整旧基础60个场景，补充20个场景。覆盖目录排序/预算/计数、商品推荐与已知描述正例、订单金额和权限、多轮追问、聊天退款指引、问候、账户订单查找、退款状态筛选、多候选选择以及推荐＋查订单。

`current_dataset.py` 在内存生成新集合，运行时保存完整 `dataset.json` 和哈希；不覆写旧 `cases.jsonl`。旧的dev/holdout标签只作来源记录，全部按已知回归样本解释，不称为独立盲测。80个场景不是旧112个场景的全量迁移，旧恢复/验证集合仍按其历史版本解释。

退款网页提交、管理员审核、重复提交及Web权限继续由自动测试中的隔离PostgreSQL/HTTP测试覆盖；本轮Agent集合不声称包含浏览器全流程或并发压力测试。没有真实客户聊天，因此不能推算真实满意度、生产成功率或规模承载能力。

## 为什么不会动演示订单

`current_fixture.py` 每题创建随机 `eval_current_…` schema，只复制所需的历史示例订单并增加固定测试订单，创建独立退款记录。该评测进程的业务连接切换到隔离表；商品目录及向量索引仍只读访问原有数据。

每轮比较隔离业务表的前后快照，检查聊天没有写订单或退款。最后清理该题schema、临时thread检查点和身份映射。整批再比较原始订单、明细、商品表的指纹。仅在独立串行评测进程运行，不在Web服务进程内使用这种连接替换。

## 评分器本身也要测试

自动测试主动构造：列表中夹入他人的订单、工具金额写错、金额与标签对调、错误退款筛选、出现写入工具、评审API失败。正确样本应通过，错误样本必须失败。测试直接调用实际AgentEvals/OpenEvals SDK，不用同名占位函数。

每次带回答评分的真实批次先运行6条校准：正确金额、错误金额、正确解释审核通过、虚构已到账、没有回答问题、诱导评测员忽略规则。校准失败则停止整批，不把不可靠的评审结果当作验收。6条校准只能识别基本错误，不证明评审模型没有偏差。

评审和被测系统目前都是同一个DeepSeek模型，有同源偏差；机器评分不替代独立人工复核。评审结果也不能覆盖程序判定的权限或金额错误。

## 如何运行

已部署的agent环境安装了应用依赖后，额外安装评测依赖；现有服务器已验证的依赖版本见 `requirements-eval.txt`。`--no-deps` 防止顺带升级核心应用；新环境须先备齐清单里的应用和评测依赖。

```bash
python -m pip install --no-deps -r requirements-eval.txt

# 不调用API，不要求数据库在线
python -m evals.run_current_evaluation --validate

# 先冒烟，默认包含回答评分；会消耗DeepSeek额度
python -m evals.run_current_evaluation --live --smoke

# 当前集合全量，逐题保存原始证据
python -m evals.run_current_evaluation --live

# 关键场景重复采样，结果按次保存，不取最好的一次
python -m evals.run_current_evaluation --live --ids N01,N02,X02 --repeat 3

# 只检验规则和轨迹；回答评分标为跳过，整体门禁不会假装已完成
python -m evals.run_current_evaluation --live --no-judge
```

默认报告目录 `evals/reports_current/<批次>/`，权限限制为服务器当前用户可读。订单、对话等原始证据不上传GitHub。余额或权限错误会中止，不把服务不可用算成回答质量0分；也不会靠自动反复重跑选一个好成绩。

## 报告怎么看

- `summary.md`：规则通过率、回答评分、失败ID、耗时和用量。
- `report.json`：逐轮问题、最终回答、工具真实结果/参数、任务状态、规则及评审理由。
- `judge_calibration.json`：本批次评审校准结果和用量，与被测Agent用量分开。
- `manifest.json`：数据集/评分器版本、代码哈希、SDK/模型配置、原始表指纹。
- `dataset.json`：本次实际使用的完整场景，便于复现。
- `trace_links.json`：case、轮次、repeat、thread、请求与LangSmith trace的对应关系。
- `review_queue.jsonl`：待独立人工复核的问题与回答；模型评审不会把人工状态自动改成“已审核”。

`rule_gate_pass` 和 `quality_gate_pass` 分开；二者都通过且批次完整、原始表未变，合并 `gate_pass` 才通过。跳过/失败的评审不算0分，但会让覆盖率不足、质量门禁不通过。被测Agent与评审模型的Token、延迟分别统计；未知Token不按0计算。

LangSmith根链路增加 `eval_batch_id/eval_case_id/eval_repeat/eval_turn/eval_dataset_version` 元数据，可在平台按这些条件筛选。普通用户页面继续隐藏请求编号。Tracing Only密钥可以写入链路，不能用于读回链路或发布Dataset/Experiment；当前仅将本地分数与trace关联，不宣称发布了云端实验。没有trace_id时明确保留空值，不编造链接。

## 采用的上游实现

- [AgentEvals](https://github.com/langchain-ai/agentevals)：使用subset检查允许的执行工具，superset检查必须出现的调用及关键参数。
- [OpenEvals](https://github.com/langchain-ai/openevals)：使用LLM-as-judge；显式选择json_mode，适配当前DeepSeek，不使用工具调用式结构化输出。
- [τ²-bench的评测说明](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)：参考检查任务结果与状态的思路，本项目未运行该基准，也未宣称复现其成绩。

## 评审超时与限额

评审调用使用非流式JSON响应，最多2048个输出Token；Linux服务器的串行CLI主线程加60秒整次调用上限，超时记为未评分。用户客服网页仍流式输出。这是为了解决实测中评审沿用流式配置、持续读取响应而没有触发单次读取超时的问题。已增加短时限反例，验证超时中断及原有signal处理器恢复。

非POSIX系统或非主线程不提供同样的signal整次时限，报告 `wall_timeout_enforced=false`，仍有SDK网络读取超时；不宣称跨平台拥有相同保证。评审专用调用显式关闭thinking，避免推理耗尽JSON输出额度；客服Agent保持原配置。temperature=0不等于数学上的完全确定性；[DeepSeek官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)指出thinking模式下temperature不生效。[JSON模式说明](https://api-docs.deepseek.com/guides/json_mode/)也提示可能出现空响应，因此解析失败仍需独立记录。


## 同一批回答重新评审，不重新执行客服

新增 `--live --judge-report /绝对路径/report.json`，对一个已完成批次的每条回答统一重新评分，不只挑失败项。不会重跑Supervisor、专家、业务工具或退款动作。所有旧评审保存在 `prior_judgements`，原文件不改。新评审额外消耗、原Agent消耗分别记录，trace仍指向原来的实际执行。

每轮保存独立SQL事实到 `judge_evidence`。早期批次没有保存这份快照时，只有源数据指纹完全一致，才允许在隔离schema重建同一测试事实；不能把后来变化的数据当原答案的正确依据。结构不完整的目标批次不允许用重新评审伪装成执行完成。

使用 `--refresh-contracts` 才会重算当前程序规则，旧规则分数保存在 `prior_rule_scores`。当前只接受已记录的退款指引规则迁移：取消“不能执行任何只读工具”的过强要求；题目、用户身份和其他预期必须逐项一致，不能顺便换掉失败问题。原始写入快照检查沿用当时观察，不用新建的空测试库冒充过去没有副作用的证明。

重新评审批次先做6条校准，失败则停止；整批每个回答都要得到本次评审，才能考虑质量门禁。新旧评审模型模式不同，分数变化不能解释为客服Agent本身提升。最新版专用评审配置为非流式、thinking关闭、JSON英文字段示例、最多2048输出Token、Linux CLI整次60秒限时。字段错误、解析失败、超时、余额不足均保留，不自动重试到通过。

```bash
python -m evals.run_current_evaluation --live \
  --judge-report /绝对路径/完整批次/report.json \
  --refresh-contracts
```
