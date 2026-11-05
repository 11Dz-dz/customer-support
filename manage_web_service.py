"""Start/stop the web service with an explicit bind address on servers without systemd."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
PIDFILE=ROOT/'logs/web-service.pid'


def live_pid():
    if not PIDFILE.exists():return None
    pid=int(PIDFILE.read_text())
    proc=Path('/proc')/str(pid)
    try:
        if proc.joinpath('cwd').resolve()!=ROOT or b'webapp.api:app' not in proc.joinpath('cmdline').read_bytes():
            return None
        return pid
    except OSError:return None


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['start','stop','status'])
    a=p.parse_args();pid=live_pid()
    if a.action=='status':print('running pid='+str(pid) if pid else 'stopped');return
    if a.action=='stop':
        if not pid:print('already stopped');return
        os.kill(pid,signal.SIGTERM)
        for _ in range(130):
            if live_pid() is None:PIDFILE.unlink(missing_ok=True);print('stopped');return
            time.sleep(1)
        raise SystemExit('仍在收尾；未强制终止可能执行中的业务操作')
    if pid:print('already running pid='+str(pid));return
    from postgres_config import load_project_env
    load_project_env()
    host=os.getenv('WEB_BIND_HOST','127.0.0.1').strip()
    if host not in {'127.0.0.1','0.0.0.0'}:
        raise SystemExit('WEB_BIND_HOST must be 127.0.0.1 or 0.0.0.0')
    PIDFILE.parent.mkdir(exist_ok=True)
    with (ROOT/'logs/web-service.log').open('a') as log:
        process=subprocess.Popen([sys.executable,'-m','uvicorn','webapp.api:app','--host',host,
            '--port','8000','--workers','1','--no-access-log','--timeout-graceful-shutdown','120'],
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
            env={**os.environ,'PYTHONUTF8':'1'})
    PIDFILE.write_text(str(process.pid));PIDFILE.chmod(0o600)
    time.sleep(2)
    if process.poll() is not None:raise SystemExit('启动失败，请查看logs/web-service.log')
    print('started pid='+str(process.pid)+'; bind='+host+':8000')


if __name__=='__main__':main()
