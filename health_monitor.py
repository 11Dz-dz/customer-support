"""按需只读健康检查；不接受用户提供的URL，不执行修复或系统命令。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from postgres_config import PROJECT_DIR, connect_database, load_project_env
from tool_result import business_payload, exception_result


CGROUP_ROOT = Path('/sys/fs/cgroup')
PROC_ROOT = Path('/proc')
API_URL = 'https://api.deepseek.com/models'
MAX_RESPONSE_BYTES = 262144
SAMPLE_SECONDS = 0.25


def _result(component, started, **fields):
    return {'component': component, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 2), **fields}


def _database():
    # 连接、事务和查询均有超时；不读业务表，也不创建表或执行维护。
    with connect_database() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        row = conn.execute('SELECT 1 AS probe').fetchone()
    if not row or row.get('probe') != 1:
        return {'success': False, 'status': 'unhealthy', 'error_code': 'UNEXPECTED_DATABASE_RESPONSE'}
    return {'success': True, 'status': 'healthy', 'scope': 'connection_and_readonly_select_1',
            'note': '仅验证本次连接和SELECT 1，不代表业务查询、吞吐或所有数据库功能正常。'}


def _read(path):
    return path.read_text(encoding='utf-8').strip()


def _cpu_snapshot():
    try:
        quota, period = _read(CGROUP_ROOT / 'cpu.max').split()
        if quota != 'max' and int(quota) > 0 and int(period) > 0:
            fields = dict(line.split() for line in _read(CGROUP_ROOT / 'cpu.stat').splitlines())
            return {'source': 'cgroup_v2', 'usage': int(fields['usage_usec']),
                    'capacity_cores': int(quota) / int(period), 'clock': time.monotonic()}
    except (OSError, ValueError, KeyError):
        pass
    # 没有可读的容器CPU配额时只展示宿主机视图，明确不能当作容器使用率。
    values = [int(v) for v in _read(PROC_ROOT / 'stat').splitlines()[0].split()[1:9]]
    return {'source': 'host_view', 'usage': sum(values), 'idle': values[3] + values[4],
            'capacity_cores': None, 'clock': time.monotonic()}


def _memory():
    try:
        limit = _read(CGROUP_ROOT / 'memory.max')
        if limit != 'max' and int(limit) > 0:
            used = int(_read(CGROUP_ROOT / 'memory.current'))
            return {'source': 'cgroup_v2', 'used_bytes': used, 'limit_bytes': int(limit),
                    'used_percent': round(used / int(limit) * 100, 2),
                    'note': '容器计量内存视图，包含文件缓存，不等于Python进程RSS。'}
    except (OSError, ValueError):
        pass
    fields = {line.split(':', 1)[0]: int(line.split()[1]) * 1024
              for line in _read(PROC_ROOT / 'meminfo').splitlines() if line.startswith(('MemTotal:', 'MemAvailable:'))}
    total, available = fields['MemTotal'], fields['MemAvailable']
    return {'source': 'host_view', 'used_bytes': total - available, 'limit_bytes': total,
            'used_percent': round((total - available) / total * 100, 2),
            'note': '宿主机内存视图，不能代表当前容器配额或使用量。'}


def _server():
    metrics, warnings, unavailable = {}, [], []
    try:
        first = _cpu_snapshot()
        time.sleep(SAMPLE_SECONDS)
        second = _cpu_snapshot()
        if first['source'] != second['source']:
            raise ValueError('CPU采样来源发生变化')
        if second['source'] == 'cgroup_v2':
            duration = second['clock'] - first['clock']
            if duration <= 0 or first['capacity_cores'] != second['capacity_cores']:
                raise ValueError('CPU采样无效')
            percent = (second['usage'] - first['usage']) / 1_000_000 / duration / second['capacity_cores'] * 100
        else:
            duration = second['usage'] - first['usage']
            if duration <= 0:
                raise ValueError('CPU采样无效')
            percent = (duration - second['idle'] + first['idle']) / duration * 100
        if percent < 0:
            raise ValueError('CPU计数器被重置')
        metrics['cpu'] = {'source': second['source'], 'used_percent': round(min(percent, 100), 2),
                          'capacity_cores': second['capacity_cores'], 'sample_seconds': SAMPLE_SECONDS}
        if second['source'] == 'host_view':
            warnings.append('CPU仅能读取宿主机视图，容器配额与使用率未核实。')
    except (OSError, ValueError, IndexError, KeyError):
        unavailable.append('cpu')
    try:
        metrics['memory'] = _memory()
        if metrics['memory']['source'] == 'host_view':
            warnings.append('内存仅能读取宿主机视图，容器内存未核实。')
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        unavailable.append('memory')
    disks = []
    for label, path in [('system', Path('/')), ('project_data', PROJECT_DIR)]:
        try:
            total, used, free = shutil.disk_usage(path)
            disks.append({'label': label, 'total_bytes': total, 'used_bytes': used,
                          'free_bytes': free, 'used_percent': round(used / total * 100, 2)})
        except (OSError, ZeroDivisionError):
            unavailable.append('disk_' + label)
    metrics['disks'] = disks
    for name in ['cpu', 'memory']:
        if metrics.get(name, {}).get('used_percent', 0) >= 90:
            warnings.append(name + '使用率达到90%关注阈值。')
    for disk in disks:
        if disk['used_percent'] >= 90:
            warnings.append(disk['label'] + '文件系统使用率达到90%关注阈值。')
    status = 'degraded' if warnings else ('unknown' if unavailable else 'healthy')
    return {'success': bool(disks or 'cpu' in metrics or 'memory' in metrics), 'status': status,
            'scope': 'current_runtime_snapshot', 'metrics': metrics, 'warnings': warnings,
            'unavailable_metrics': unavailable,
            'note': '按需快照，不证明持续故障；文件系统可能共用磁盘。未检测应用进程存活或业务请求成功率。'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api():
    load_project_env()
    key = os.getenv('DEEPSEEK_API_KEY', '').strip()
    scope = {'scope': 'deepseek_models_endpoint', 'endpoint': 'GET /models',
             'inference_status': 'not_tested', 'balance_status': 'not_checked',
             'note': '只检测模型列表接口与本次鉴权；未测试对话生成、账户余额或其他端点。'}
    if not key:
        return {**scope, 'success': False, 'status': 'unknown', 'error_code': 'API_KEY_NOT_CONFIGURED'}
    request = Request(API_URL, headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
    try:
        # 固定官方HTTPS目标；不跟随重定向，不传用户对话，不进行生成或自动重试。
        from request_deadline import check_deadline, remaining_seconds
        check_deadline()
        with build_opener(_NoRedirect()).open(request, timeout=max(.001, remaining_seconds(5))) as response:
            code = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        check_deadline()
        if code != 200:
            return {**scope, 'success': False, 'status': 'unhealthy', 'http_status': code, 'error_code': 'HTTP_ERROR'}
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('response_too_large')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ValueError('invalid_models_response')
        ids = [item['id'] for item in data['data'] if isinstance(item, dict) and isinstance(item.get('id'), str)]
        configured = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash').strip()
        listed = configured in ids
        return {**scope, 'success': True, 'status': 'healthy' if listed else 'degraded',
                'http_status': code, 'configured_model': configured, 'configured_model_listed': listed,
                'model_count': len(ids)}
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return {**scope, 'success': False, 'status': 'unhealthy', 'http_status': code,
                'error_code': {401: 'AUTHENTICATION_REJECTED', 403: 'ACCESS_FORBIDDEN',
                               402: 'ACCOUNT_REQUIRES_ATTENTION', 429: 'RATE_LIMITED'}.get(code, 'HTTP_ERROR')}
    except (URLError, TimeoutError, OSError):
        return {**scope, 'success': False, 'status': 'unhealthy', 'error_code': 'NETWORK_OR_TLS_ERROR'}
    except (ValueError, UnicodeDecodeError):
        return {**scope, 'success': False, 'status': 'unknown', 'error_code': 'INVALID_API_RESPONSE'}


def check_health(component):
    started = time.perf_counter()
    if component not in {'database', 'server', 'api'}:
        return _result('unsupported', started, success=False, status='unknown', error_code='UNSUPPORTED_COMPONENT')
    try:
        fields = {'database': _database, 'server': _server, 'api': _api}[component]()
    except Exception as exc:
        # 异常详情可能包含连接串、请求头或地址，不能进入工具结果。
        classified=exception_result(exc)
        fields = {'success': False, 'status': 'unknown',
                  'error_code':classified.error_code if classified.retryable else 'PROBE_FAILED', 'error_type': type(exc).__name__}
    return _result(component, started, **fields)


def render_health_messages(messages):
    """当前轮健康事实直接展示；历史快照不能冒充本轮检测。"""
    last_human = max((i for i, m in enumerate(messages) if getattr(m, 'type', None) == 'human'), default=-1)
    reports = []
    for message in messages[last_human + 1:]:
        if getattr(message, 'type', None) == 'tool' and getattr(message, 'name', None) == 'check_system_status':
            try:
                value = json.loads(message.content) if isinstance(message.content, str) else message.content
                if isinstance(value,dict) and 'schema_version' in value:
                    value=business_payload(value,'check_system_status',transport_error=getattr(message,'status',None)=='error')
            except (ValueError, TypeError):
                value = None
            reports.append(value if isinstance(value, dict) else {})
    if not reports:
        return None
    labels = {'healthy': '本次检查通过', 'degraded': '需要关注', 'unhealthy': '本次检查失败', 'unknown': '未能确认'}
    sections = []
    for value in reports:
        component = value.get('component')
        heading = {'database': 'PostgreSQL', 'server': '服务器资源', 'api': 'DeepSeek接口'}.get(component, '组件检测')
        lines = [f"{heading}：{labels.get(value.get('status'), '未能确认')}。"]
        if value.get('checked_at'):
            lines.append(f"检测时间（UTC）：{value['checked_at']}。")
            lines.append(f"本次检测耗时：{value['elapsed_ms']} ms。")
        if value.get('error_code'):
            lines.append(f"检测错误：{value['error_code']}。未获取到的状态不能视为正常。")
        if component == 'database':
            lines.append('范围：只读连接与SELECT 1；不代表业务查询性能或整个数据库全部正常。')
        elif component == 'server':
            metrics = value.get('metrics', {})
            cpu, memory = metrics.get('cpu'), metrics.get('memory')
            if cpu:
                source = '容器配额' if cpu['source'] == 'cgroup_v2' else '宿主机视图，非容器使用率'
                lines.append(f"CPU：{cpu['used_percent']}%；{source}；采样{cpu['sample_seconds']}秒。")
                if cpu.get('capacity_cores') is not None:
                    lines.append(f"容器CPU配额：{cpu['capacity_cores']}核。")
            if memory:
                source = '容器计量（含缓存）' if memory['source'] == 'cgroup_v2' else '宿主机视图，非容器内存'
                lines.append(f"内存：{memory['used_percent']}%；{memory['used_bytes']/2**30:.2f} / {memory['limit_bytes']/2**30:.2f} GiB；{source}。")
            for disk in metrics.get('disks', []):
                label = '系统目录所在文件系统' if disk['label'] == 'system' else '项目目录所在文件系统'
                lines.append(f"{label}：已用{disk['used_percent']}%，剩余{disk['free_bytes']/2**30:.2f} GiB。")
            lines.extend(value.get('warnings', []))
            if value.get('unavailable_metrics'):
                lines.append('未能读取的指标：' + '、'.join(value['unavailable_metrics']) + '。')
            lines.append('这是短时资源快照，不证明持续故障；未检测应用进程或业务请求成功率。')
        elif component == 'api':
            if value.get('http_status') is not None:
                lines.append(f"GET /models：HTTP {value['http_status']}。")
            if 'configured_model_listed' in value:
                lines.append('当前配置模型' + ('已出现在返回列表中。' if value['configured_model_listed'] else '未出现在返回列表中；别名兼容性需要另行确认。'))
            lines.append('只验证模型列表接口与本次鉴权；对话生成、账户余额及其他端点未检测。')
        sections.append('\n'.join(lines))
    sections.append('本轮检测未执行重启、修改配置或其他修复操作。')
    return '\n\n'.join(sections)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='不经过LLM的只读实时健康检查')
    parser.add_argument('--component', choices=['database', 'server', 'api', 'all'], default='all')
    args = parser.parse_args()
    names = ['database', 'server', 'api'] if args.component == 'all' else [args.component]
    results = [check_health(name) for name in names]
    print(json.dumps({'checks': results}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(r['status'] == 'healthy' for r in results) else 1)
