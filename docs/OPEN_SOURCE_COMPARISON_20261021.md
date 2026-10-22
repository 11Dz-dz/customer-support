# 当前客服架构的开源方案参照

源码调研日期：2026-10-21；与本项目当前实现对齐：2026-10-22。以下使用此前已阅读的固定版本，不声称重新检查了外部项目最新代码，也没有安装或运行这些参考系统。

本页删除已过时的“本项目只有messages/next”“尚无任务状态”“尚无工单”等差距表述，保留仍有用的设计依据和来源。

## 1. 固定版本来源

| 项目 | 定位 | 主要查看的内容 | 适合借鉴什么 |
|---|---|---|---|
| [LangGraph 官方客服教程](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb) | 航空客服教学示例；这是归档教程版本 | State、专家进入/退出、工作流恢复、敏感工具暂停 | 任务状态和多轮专家交接 |
| [Chatwoot](https://github.com/chatwoot/chatwoot) | 完整客服平台 | 会话状态、机器人和人工分配、接管、事件通知 | 真正的人工客服处理流程 |
| [Dify](https://github.com/langgenius/dify) | 通用 LLM 应用与知识检索平台 | 检索配置、重排、阈值、来源信息 | 检索质量和证据追溯 |
| [langgraph-support-agent](https://github.com/nitanandsinghupw/langgraph-support-agent) | 小型 LangGraph 客服参考项目 | 工具内规则、interrupt、审批恢复、身份传递 | 小规模审批实现的阅读参考 |

它们的定位不同，不能简单按功能数量或测试通过率排名。官方教程不是最新 SDK 使用指南；小型参考项目也不是经过本次验证的生产系统。Chatwoot 的核心代码与 enterprise 目录适用范围不同，Dify 有自己的许可附加条件，采用整套系统前应查看各自 [Chatwoot LICENSE](https://github.com/chatwoot/chatwoot/blob/39314c979aa4192bc1c6bced9cbbebcabde2fbb1/LICENSE) 和 [Dify LICENSE](https://github.com/langgenius/dify/blob/a101bbb305d50243adae56655a0e9143911be73c/LICENSE)。本文比较的 Chatwoot 接管和分配代码位于核心 app 目录，不把 Captain 企业功能作为免费能力。

此前调研读取的固定版本：

- LangGraph 教程：`23961cff61a42b52525f3b20b4094d8d2fba1744`。
- Chatwoot develop：`39314c979aa4192bc1c6bced9cbbebcabde2fbb1`。
- Dify main：`a101bbb305d50243adae56655a0e9143911be73c`。
- 小型客服示例 master：`b6fe9faebff8a1af549ae7b8cabcb0b6e6479cec`。

Chatwoot、Dify 和小型示例已下载源码供静态阅读；没有执行其中的程序。官方教程读取的是上述固定版本的 notebook。比较结论不包括部署压力测试、第三方代码安全审计或真实客服业务效果验证。


## 2. 任务状态与多专家交接

官方客服教程用dialog_state、工作流进入/退出和恢复机制管理当前职责。借鉴的是“显式保存进度”，不是直接复制其航空业务工具或旧SDK接口。

本项目现在使用TaskPlan、ConversationTask、TaskParameters、OperationRecord和任务清单。Supervisor识别语义，scheduler控制依赖与执行，专家返回collect再继续调度，answer统一汇总。多轮继承、取消、补充和候选选择由程序规则处理。实现见task_state.py、task_scheduler.py和主图；完整说明第12、13、17节。

## 3. 人工处理需要持久化流程

Chatwoot的会话状态、负责人分配及机器人交接体现“人工接管不是一句回复”。本项目采用最小数据库工单：human_handoffs和human_handoff_events，活动工单去重、领取、完成与审计；/human、/handoffs和manage_handoffs.py提供入口。

实现范围仍小于完整客服平台：没有坐席工作台、实时会话接管、通知和登录；工单完成不会自动执行资金退款。完整说明第16节。

## 4. 工具权限、审批和重试

官方教程及小型示例的敏感工具暂停/恢复提供了权限边界的参考。本项目当前使用角色工具范围、服务器身份、SQL归属过滤、退款登记幂等、写入失败拦截和不确定提交转人工。

当前没有实现通用interrupt审批界面或任意业务事务恢复。不能把参考方案的能力写成已经移植。失败只读操作按记录原参数、临时错误码和尝试次数有限重试，详见第14、15节。

## 5. 检索与证据

Dify提供检索配置、来源与重排等设计参照。本项目实际采用关键词＋本地向量召回、价格/类别过滤、RRF融合、来源描述引用和受控事实展示；没有独立reranker，也没有接入商家政策库。

价格和订单状态由SQL读取，不由相似度证明。是否需要后续重排，应根据检索评测判断，当前不把未做功能列成已完成。实现见product_knowledge.py、answer_contract.py和evals/README.md。

## 6. 当前采用原则

语义判断交给模型，确定规则交给程序；保留任务进度，先验证再执行；有证据才展示，失败不吞掉成功结果。采用的是这些通用设计原则，而不是声称已复刻Chatwoot或Dify。

本项目的能力、限制和最新验证以[当前版本总览](CURRENT_PROJECT_STATUS.md)为准，具体问题与实现见[完整改进说明](IMPROVEMENTS_READABLE_20261021.md)。
