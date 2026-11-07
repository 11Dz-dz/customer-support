# Agent 评测指南

## 运行入口

先按 [项目首页](../README.md) 准备应用、样例数据和索引。真实模型评测需安装 `requirements-eval.txt` 并使用独立评测进程和测试数据库。

| 范围 | 命令 | 核对内容 |
| --- | --- | --- |
| 当前基线静态校验 | `python -m evals.run_current_evaluation --validate` | 80 个场景、95 轮的定义和契约，无需调用模型 |
| 当前基线实测 | `python -m evals.run_current_evaluation --live` | 独立 SQL、权限和业务结果、AgentEvals 执行轨迹及 OpenEvals 回答评审 |
| 追加消息实测 | `python -m evals.run_inflight_answer_evaluation --live` | 7 个 HTTP/SSE 时序场景及最终答案 |
| 条件更新专项 | `python -m evals.run_constraint_evaluation --live` | 规划字段依据、条件继承及业务结果 |
| 范围与目录专项 | `python -m evals.scope_runner --live`、`python -m evals.catalog_query_runner --live` | 无关请求、混合任务、目录查询 |
| 扩展稳定性回归 | `python -m evals.reliability_runner --live` | 多轮状态、条件和报价 |
| 评分参考集校验 | `python -m evals.run_judge_reference` | 固定事实、金额匹配及评分参考样例 |

真实调用会消耗模型额度。基线可用 `--smoke` 或 `--ids` 缩小范围。详情见 [基线规则与报告](CURRENT_EVALUATION.md)、[追加消息专项](INFLIGHT_ANSWERS.md) 和 [评分参考集](judge_reference/README.md)。

## 如何验收改动

- 权限、写入边界与事实正确性分别检查。效率告警单独统计。
- 用同一套数据、配置及规则比较改动前后。对依赖模型的边界问法保留每次重复结果。
- 评审模型与客服模型使用同一服务时，结合独立 SQL、执行记录和人工复核解释分数。
- 场景是人工编写的回归样例。原始回答、运行轨迹及报告由测试环境管理。
