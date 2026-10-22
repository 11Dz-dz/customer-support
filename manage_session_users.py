"""可信服务器管理员入口：持久化会话用户映射；不向模型注册这些操作。"""
import argparse
import json

from conversation_persistence import close_persistence
from session_identity import bind_session_customer, get_session_customer, SessionIdentityError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="把尚无历史的新会话绑定到客户；已有映射只允许同用户幂等调用")
    bind.add_argument("--thread-id", required=True)
    bind.add_argument("--customer-id", required=True, type=int)
    show = sub.add_parser("show", help="查看指定会话的绑定")
    show.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "bind":
            customer_id = bind_session_customer(args.thread_id, args.customer_id)
        else:
            customer_id = get_session_customer(args.thread_id)
        print(json.dumps({"thread_id": args.thread_id, "customer_id": customer_id,
                          "bound": customer_id is not None}, ensure_ascii=False))
    except (SessionIdentityError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    finally:
        close_persistence()


if __name__ == "__main__":
    main()
