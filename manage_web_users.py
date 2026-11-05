"""Create trusted web accounts locally. Passwords are never command arguments."""
import argparse
import getpass
import json
from webapp.store import WebStore


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('username')
    p.add_argument('--display-name',required=True)
    p.add_argument('--role',choices=['user','admin'],default='user')
    p.add_argument('--customer-id',type=int)
    a=p.parse_args()
    password=getpass.getpass('密码（至少12字符）：')
    if getpass.getpass('再次输入：')!=password:p.error('两次密码不一致')
    store=WebStore();store.initialize()
    try:result=store.create_user(a.username,password,a.display_name,a.role,a.customer_id)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__=='__main__':main()
