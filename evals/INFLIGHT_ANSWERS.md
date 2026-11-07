# 追加消息后的最终答案验收

## 为什么增加

新入口把transport_pass（流程）、business_pass（任务与最终答案）和acceptance_pass（两者均通过）分开，不能仅以输入phase=answered、Web运行completed或failure_code为空认定业务成功。

## 独立依据与评分范围

`evals/inflight_answers.py`直接读取product_catalog，按AdventureWorks整车子类别1/2/3及正标价/预算查询全量候选。不复用search_products、向量检索、排序、业务渲染器或模型输出制造标准答案，不额外调用模型裁判。

有符合商品时，检查最终任务预算、整车类别、一款数量；最后实际搜索是否应用预算和类别；答复的商品ID、名称、目录标价、数量和引用描述是否与独立SQL一致。无符合商品时，必须准确说明该预算下没有匹配整车，不接受“已检索到候选但无法确认适合”的兜底，也不能编造缺货。旧答案已经确认交付的场景，只要求追加条件后的下一份答案符合新预算，不能用后来的预算追溯判错此前合法答案。

这是一组已编写的精确业务回归，解析范围限本项目明确的“商品ID：名称”“历史目录标价”格式。未识别表达按覆盖不足失败，不冒充通用自然语言理解，也不涵盖任意骑行用途相关性、真实价格/库存或负载测试。

## 如何执行

```bash
# 默认仅显示7个用例，不访问模型或数据库
python -m evals.run_inflight_answer_evaluation

# 显式执行真实图、模型、工具和独立SQL，并另外复核旧报告
python -m evals.run_inflight_answer_evaluation --live

# 可选：同时复核自己保存的旧报告
python -m evals.run_inflight_answer_evaluation --live --historical path/to/report.json

# 只补测指定未执行项；失败不反复重跑挑分
python -m evals.run_inflight_answer_evaluation --live --ids after_initial_search-1000
```

每个场景创建独立PostgreSQL Web schema及未绑定客户的临时账号，会话结束后清理自身检查点及临时schema，不改订单、商品或退款。只通过定时屏障延迟原节点，以确保第二条在目标检查点到达，不替换DS、检索返回或回答。报告保存独立SQL快照、任务状态、工具请求与证据、模型调用、最终答复、代码哈希和数据指纹。

报告目录默认evals/reports_inflight_answers，包含report.json、各场景JSON、summary.md、事件和独立historical_audit.json。原报告、原判定保持不变；旧回答复核使用当前SQL快照，不能由此声称已证明历史目录从未变化。
