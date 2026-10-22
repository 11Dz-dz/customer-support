"""Trusted local administrator entry. Not a public API or model tool."""
import argparse
import json
from human_handoff import admin_list, admin_show, admin_transition


def main():
    parser = argparse.ArgumentParser(description='查看、领取和完成人工工单（服务器管理员入口）')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--status', choices=['pending', 'in_progress', 'resolved'], default='pending')
    listing.add_argument('--limit', type=int, default=20)
    showing = commands.add_parser('show')
    showing.add_argument('ticket_id')
    for name in ['claim', 'resolve']:
        command = commands.add_parser(name)
        command.add_argument('ticket_id')
        command.add_argument('--operator', required=True)
        if name == 'resolve':
            command.add_argument('--note', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'list':
            result = admin_list(args.status, args.limit)
        elif args.command == 'show':
            result = admin_show(args.ticket_id)
        else:
            result = admin_transition(args.ticket_id, args.command, args.operator, getattr(args, 'note', None))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
