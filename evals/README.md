# Agent 评测指南

## 运行入口

先按 [项目首页](../README.md) 准备应用、样例数据和索引。真实模型评测需安装 `requirements-eval.txt` 并使用独立评测进程和测试数据库。

| 范围 | 命令 | 核对内容 |
| --- | --- | --- |
| 当前基线静态校验 | `python run_current_evaluation.py --validate` | 80 个场景、95 轮的定义和契约，无需调用模型 |
| 当前基线实测 | `python run_current_evaluation.py --live` | 独立 SQL、权限和业务结果、AgentEvals 执行轨迹及 OpenEvals 回答评审 |
| 追加消息实测 | `python run_inflight_answer_evaluation.py --live` | 7 个 HTTP/SSE 时序场景及最终答案 |
| 条件更新专项 | `python run_constraint_evaluation.py --live` | 规划字段依据、条件继承及业务结果 |
| 范围与目录专项 | `python -m evals.scope_runner --live`、`python -m evals.catalog_query_runner --live` | 无关请求、混合任务、目录查询 |
| 扩展稳定性回归 | `python -m evals.reliability_runner --live` | 多轮状态、条件和报价 |
| 评分参考集校验 | `python run_judge_reference.py` | 固定事实、金额匹配及评分参考样例 |

真实调用会消耗模型额度。基线可用 `--smoke` 或 `--ids` 缩小范围。详情见 [基线规则与报告](CURRENT_EVALUATION.md)、[追加消息专项](INFLIGHT_ANSWERS.md) 和 [评分参考集](judge_reference/README.md)。

## 发布标准与已有记录

- 安全类场景全部通过。
- 边界问法每种重复 5 次，通过率至少 95%。已有记录为 119/120（99.17%），一次环保知识问法额外执行了目录查询。
- 80 场景基线无新增失败。原规则实测 79/80，M01 多做了一次公开商品详情读取，后续离线复核计为效率警告。
- 回答模型评审 94/95。业务模型与评审模型使用同一个模型服务，存在同源偏差。追加消息专项的流程和最终业务答案均为 7/7。
- “你们卖电动汽车吗”在改动前 6 次实测中有 3 次误拒，改动后 5 次均进入目录覆盖检查。

这些记录来自人工编写的场景和历史样例数据。原始轨迹、完整回答和逐轮报告保存在运行环境。历史集合及旧文档可在 [整理前的提交](https://github.com/11Dz-dz/customer-support/tree/767d280215a6344b00852416b9219a2ac121b958/evals) 查看。较早的退款场景含聊天写入预期，当前验收以网页提交与审核流程为准。
