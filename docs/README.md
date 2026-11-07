# 项目文档

首次搭建从 [仓库首页](../README.md) 开始。以下按维护任务查阅。

## 部署与排查

- [Web 部署与管理](WEB_APP.md) · 启动、账号、权限、退款及代理
- [账号注册](ACCOUNT_REGISTRATION.md) · 注册、登录与限流
- [会话身份与订单权限](SESSION_USERS_20261020.md) · CLI 身份绑定及数据访问
- [LangSmith 执行链路](LANGSMITH_TRACING.md) · 追踪配置和逐节点排查
- [按需健康检查](REAL_MONITORING_20261020.md) · 数据库、模型服务与资源探针

## 架构与数据

- [Agent 组成与 Skill](AGENT_COMPOSITION.md) · 提示词、专家和工具配置
- [客服服务范围](SERVICE_SCOPE.md) · 无关请求及混合业务
- [条件与字段更新](CONSTRAINT_UPDATES.md) · 按原文修改已保存的任务
- [处理中追加消息](INFLIGHT_MESSAGES.md) · 消息入库、检查点和答案版本
- [目录查询协议](CATALOG_QUERY_CONTRACT.md) · 精确筛选和目录覆盖
- [商品推荐检索](RETRIEVAL_PIPELINE.md) · 关键词与向量召回、候选精选
- [向量构建与索引切换](KNOWLEDGE_BUILDS.md) · 增量构建、原子发布和迁移
- [相对价格](RELATIVE_PRICE.md)、[同款规格](PRODUCT_VARIANTS.md) · 多轮商品追问
- [订单事实展示](FACT_OUTPUT_20261020.md) · 金额、日期、退款和数量
- [DeepSeek 消息兼容](DEEPSEEK_PROTOCOL.md) · Thinking 模式工具消息协议

## 验证

- [评测指南](../evals/README.md) · 当前入口、评分和验收规则
- [稳定性改进](RELIABILITY_IMPROVEMENTS_20261103.md) · 问题、原因和后续回归

[历次测试结果和开发记录](https://github.com/11Dz-dz/customer-support/tree/04a921eedddf8006e93a6ce4adf416ac6abe6e9b)可按当时版本查阅。
