"""Agent 统一评测入口；默认只列出场景，--live 才调用模型。"""
from evals.runner import main


if __name__ == "__main__":
    raise SystemExit(main())
