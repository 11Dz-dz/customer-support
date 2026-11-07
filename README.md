# Customer Support · 多智能体客服系统

基于 LangGraph、DeepSeek、PostgreSQL、pgvector、FastAPI 和 Vue 3 的客服演示项目。支持商品推荐与目录查询、订单和退款进度、人工工单，以及可恢复的多轮会话。

- **多任务协作**：Supervisor 将请求拆成任务，由销售、账单和技术支持节点处理。各任务可独立等待补充、执行和汇总。
- **商品检索**：精确查询使用参数化 SQL，开放式推荐使用关键词与本地向量召回。程序用目录证据判断商品覆盖范围。
- **Web 会话**：SSE 输出、会话恢复、处理中补充条件，以及账号与订单归属校验。退款从订单页提交并由管理员审核。

## 快速开始

需要 Python 3.11+、PostgreSQL 与 pgvector、一个 DeepSeek 兼容 API Key。网页还需要 Node.js 和 pnpm 构建前端。

1. 在虚拟环境中运行 `python -m pip install -r requirements-web.txt`。
2. 将 [配置模板](.env.example) 复制为 `.env`，配置 `DATABASE_URL` 与模型服务。DeepSeek 官方与阿里云百炼配置见模板。
3. 按 [样例数据](data/README.md) 和 [本地模型](models/README.md) 说明准备文件。Embedding 在 CPU 上运行。
4. 在仓库根目录导入数据、构建索引并启动命令行：

```bash
python import_adventureworks.py --validate-only
python import_adventureworks.py
python build_product_knowledge.py
python run_multiturn.py --thread-id demo-guest --verbose
```

命令行访客可咨询商品。查询订单需要为会话配置样例客户，见 [会话身份与订单权限](docs/SESSION_USERS_20261020.md)。

### 启动网页

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm run build
cd ..
python -m uvicorn webapp.api:app --host 127.0.0.1 --port 8000 --workers 1
```

浏览器访问 `http://127.0.0.1:8000/`。账号管理、样例客户绑定、退款审核及反向代理配置见 [Web 部署与管理](docs/WEB_APP.md)。

## 仓库导览

| 位置 | 内容 |
| --- | --- |
| [langgraph_supervisor_1.py](langgraph_supervisor_1.py)、[workflow_builder.py](workflow_builder.py) | Supervisor 和 LangGraph 流程 |
| [chat_runtime.py](chat_runtime.py)、[task_state.py](task_state.py)、[task_scheduler.py](task_scheduler.py) | 会话执行入口、任务状态和调度 |
| [prompts](prompts)、[skills](skills)、[config](config) | 路由、专家提示词、业务操作说明及工具配置 |
| [webapp](webapp)、[frontend](frontend)、[deploy](deploy) | Web 接口、界面和部署配置 |
| [data](data)、[models](models)、根目录 SQL | 数据与模型准备说明、数据库结构 |
| [tests](tests)、[evals](evals) | 自动测试、评测代码与可复现样例 |
| [docs](docs/README.md) | 维护手册和设计说明 |

## 验证

```bash
python -B -m unittest discover -s tests -t .
python run_current_evaluation.py --validate
```

部分 Python 测试需要 PostgreSQL。真实模型评测需配置模型服务并消耗额度，入口、分项结果及发布标准见 [评测指南](evals/README.md)。

项目使用 AdventureWorks 历史样例数据。实时库存、支付流水和物流轨迹未接入，退款审核也不执行资金退款。密钥、数据源文件、模型权重、运行日志、会话与原始评测报告在本地或部署环境管理。
