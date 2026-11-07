"""Check LangSmith configuration/ingestion without a model call or business operation."""
import argparse
from datetime import datetime,timezone
import json
from uuid import uuid4

from backend.postgres_config import load_project_env
from backend.observability.langsmith_tracing import settings
from langsmith import Client


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['status','check'],nargs='?',default='status')
    p.add_argument('--read',action='store_true',help='Also read back the created probe (requires trace-read permission)')
    args=p.parse_args();load_project_env();cfg=settings()
    safe={k:v for k,v in cfg.items() if k!='api_key'}
    safe['api_key_configured']=bool(cfg['api_key'])
    # Endpoint is validated before any output; reject credentials embedded in URLs.
    from urllib.parse import urlsplit
    parts=urlsplit(cfg['endpoint'])
    if parts.username or parts.password or parts.query or parts.fragment:
        print('LANGSMITH_ENDPOINT_INVALID: 不要在地址中写入口令或令牌');return 2
    print(json.dumps(safe,ensure_ascii=False,indent=2))
    if args.action=='status':return 0
    if not cfg['enabled'] or not cfg['api_key']:
        print('未启用或缺少LANGSMITH_API_KEY；未发送数据。');return 2
    client=None
    try:
        # A fixed-content probe uses synchronous SDK ingestion so accepted means HTTP success.
        client=Client(api_key=cfg['api_key'],api_url=cfg['endpoint'],workspace_id=cfg['workspace_id'],
                      auto_batch_tracing=False,timeout_ms=(2000,5000))
        rid=uuid4();now=datetime.now(timezone.utc)
        kwargs={'agent_id':cfg['agent_id'],'agent_environment':cfg['agent_environment']} if cfg['agent_id'] else {}
        client.create_run('customer_support.connectivity_check',
            inputs={'purpose':'Tracing connectivity check; no model or business data'},
            run_type='chain',project_name=cfg['project'],id=rid,start_time=now,
            extra={'metadata':{'source':'manage_tracing','request_id':rid.hex}},**kwargs)
        client.update_run(rid,outputs={'ok':True},end_time=datetime.now(timezone.utc))
        print('write=accepted trace_id='+str(rid))
        if args.read:
            from time import sleep
            for attempt in range(4):
                try:
                    result=client.read_run(rid)
                    print('read=confirmed name='+result.name);break
                except Exception as exc:
                    if type(exc).__name__=='LangSmithNotFoundError' and attempt<3:sleep(.5);continue
                    print('read=unconfirmed error_type='+type(exc).__name__+
                          '；Tracing Only密钥没有读取权限时，请在LangSmith页面核对。');return 3
        return 0
    except Exception as exc:
        print('check=failed error_type='+type(exc).__name__+
              '；请核对密钥、区域地址、工作区和Agent配置。异常正文不输出。')
        return 1
    finally:
        if client is not None:
            try:client.close(timeout=2)
            except Exception:pass


if __name__=='__main__':raise SystemExit(main())
