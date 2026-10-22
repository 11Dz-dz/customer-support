"""健康检查的资源口径、只读约束、失败处理和正文隔离测试。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import health_monitor as health


def human(text):
    return SimpleNamespace(type='human', content=text)


def message(value):
    return SimpleNamespace(type='tool', name='check_system_status', content=json.dumps(value))


class HealthMonitorTest(unittest.TestCase):
    def api_response(self, payload, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return response

    def api(self, response=None, error=None, **env):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'secret-test-key', 'DEEPSEEK_MODEL': 'example', **env}), \
                patch.object(health, 'load_project_env'), patch.object(health, 'build_opener') as opener:
            if error is not None:
                opener.return_value.open.side_effect = error
            else:
                opener.return_value.open.return_value = response
            result = health.check_health('api')
            return result, opener

    def test_database_probe_is_readonly_and_bounded(self):
        statements = []
        class Connection:
            def execute(self, sql):
                statements.append(sql)
                return SimpleNamespace(fetchone=lambda: {'probe': 1})
        @contextmanager
        def connect():
            yield Connection()
        with patch.object(health, 'connect_database', connect):
            result = health.check_health('database')
        self.assertTrue(result['success'])
        self.assertEqual(statements, ['SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout = '2000ms'", 'SELECT 1 AS probe'])
        self.assertIn('checked_at', result)
        self.assertGreaterEqual(result['elapsed_ms'], 0)

    def test_exception_details_and_credentials_are_never_returned(self):
        with patch.object(health, 'connect_database', side_effect=RuntimeError('secret-password secret-test-key')):
            result = health.check_health('database')
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))

    def test_cgroup_resource_usage_does_not_use_host_memory_or_cpu_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'cpu.max').write_text('100000 100000')
            (root/'cpu.stat').write_text('usage_usec 123456\nuser_usec 100000')
            (root/'memory.max').write_text(str(16 * 2**30))
            (root/'memory.current').write_text(str(2 * 2**30))
            with patch.object(health, 'CGROUP_ROOT', root):
                sample = health._cpu_snapshot()
                memory = health._memory()
        self.assertEqual(sample['capacity_cores'], 1)
        self.assertEqual(sample['usage'], 123456)
        self.assertEqual(memory['source'], 'cgroup_v2')
        self.assertEqual(memory['used_percent'], 12.5)
        self.assertEqual(memory['limit_bytes'], 16 * 2**30)

    def test_server_snapshot_percentage_and_warning_threshold(self):
        samples = [{'source':'cgroup_v2','usage':0,'capacity_cores':1,'clock':1},
                   {'source':'cgroup_v2','usage':125000,'capacity_cores':1,'clock':1.25}]
        memory = {'source':'cgroup_v2','used_percent':95,'used_bytes':95,'limit_bytes':100}
        with patch.object(health, '_cpu_snapshot', side_effect=samples), patch.object(health.time, 'sleep'), \
                patch.object(health, '_memory', return_value=memory), patch.object(health.shutil, 'disk_usage', return_value=(100,50,50)):
            result = health.check_health('server')
        self.assertEqual(result['metrics']['cpu']['used_percent'], 50)
        self.assertEqual(result['status'], 'degraded')
        self.assertTrue(any('memory' in w for w in result['warnings']))

    def test_host_fallback_is_explicitly_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'stat').write_text('cpu  10 0 20 70 0 0 0 0\n')
            (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 750 kB\n')
            with patch.object(health, 'CGROUP_ROOT', root/'missing'), patch.object(health, 'PROC_ROOT', root):
                cpu, memory = health._cpu_snapshot(), health._memory()
        self.assertEqual(cpu['source'], 'host_view')
        self.assertIsNone(cpu['capacity_cores'])
        self.assertEqual(memory['source'], 'host_view')
        self.assertEqual(memory['used_percent'], 25)

    def test_missing_metrics_do_not_report_healthy(self):
        with patch.object(health, '_cpu_snapshot', side_effect=OSError()), \
                patch.object(health, '_memory', side_effect=OSError()), patch.object(health.shutil, 'disk_usage', side_effect=OSError()):
            result = health.check_health('server')
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(result['unavailable_metrics']), 4)

    def test_api_probe_scope_auth_no_redirect_and_size_bound(self):
        response = self.api_response({'data':[{'id':'example'}]})
        result, opener = self.api(response)
        self.assertTrue(result['success'])
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(result['inference_status'], 'not_tested')
        self.assertEqual(result['balance_status'], 'not_checked')
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/models')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 5)
        response.read.assert_called_once_with(health.MAX_RESPONSE_BYTES + 1)
        self.assertNotIn('secret-test-key', json.dumps(result))
        self.assertIsInstance(opener.call_args.args[0], health._NoRedirect)
        self.assertIsNone(health._NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))

    def test_api_auth_rate_limit_and_redirect_failures_are_distinct(self):
        for code, expected in [(401,'AUTHENTICATION_REJECTED'),(403,'ACCESS_FORBIDDEN'),
                               (429,'RATE_LIMITED'),(302,'HTTP_ERROR')]:
            result, _ = self.api(error=HTTPError(health.API_URL,code,'secret-test-key',{},None))
            self.assertFalse(result['success'])
            self.assertEqual(result['error_code'], expected)
            self.assertEqual(result['http_status'], code)
            self.assertNotIn('secret-test-key', json.dumps(result))

    def test_api_timeout_bad_json_oversize_and_missing_model(self):
        result, _ = self.api(error=URLError('secret-test-key'))
        self.assertEqual(result['error_code'], 'NETWORK_OR_TLS_ERROR')
        for payload in [b'invalid', b'x'*(health.MAX_RESPONSE_BYTES+1), {'data':{}}, []]:
            result, _ = self.api(self.api_response(payload))
            self.assertFalse(result['success'])
            self.assertEqual(result['error_code'], 'INVALID_API_RESPONSE')
        result, _ = self.api(self.api_response({'data':[{'id':'other-model'}]}))
        self.assertEqual(result['status'], 'degraded')
        self.assertFalse(result['configured_model_listed'])

    def test_missing_key_and_invalid_component_make_no_network_request(self):
        result, opener = self.api(DEEPSEEK_API_KEY='')
        self.assertEqual(result['error_code'], 'API_KEY_NOT_CONFIGURED')
        opener.assert_not_called()
        self.assertEqual(health.check_health('https://evil.example')['error_code'], 'UNSUPPORTED_COMPONENT')

    def test_renderer_does_not_reuse_old_health_as_current(self):
        value = {'component':'database','success':True,'status':'healthy','checked_at':'2026-01-01T00:00:00+00:00','elapsed_ms':12.5}
        self.assertIsNone(health.render_health_messages([human('上次检查'), message(value), human('现在呢')]))
        text = health.render_health_messages([human('检查'), message(value)])
        self.assertIn('12.5 ms', text)
        self.assertIn('2026-01-01', text)
        self.assertNotIn('< 10ms', text)
        api={'component':'api','success':False,'status':'unhealthy','checked_at':'now','elapsed_ms':3,'http_status':401,'error_code':'AUTHENTICATION_REJECTED'}
        text=health.render_health_messages([human('检查接口'), message(api)])
        self.assertIn('本次检查失败',text)
        self.assertIn('账户余额及其他端点未检测',text)
        self.assertNotIn('所有端点可访问',text)


class HealthGraphIntegrationTest(unittest.TestCase):
    def test_raw_technical_draft_never_leaks_and_saved_reply_matches_stream(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from chat_runtime import stream_turn
        from evals.fixtures import CaseFixture
        import langgraph_supervisor_1 as project
        value={'component':'api','success':False,'status':'unhealthy','checked_at':'2026-01-01T00:00:00+00:00',
               'elapsed_ms':10,'http_status':401,'error_code':'AUTHENTICATION_REJECTED'}
        class Agent:
            def invoke(self,state,config):
                draft=FakeListChatModel(responses=['所有端点可访问，CPU 85%，余额充足'], tags=['technical_draft']).invoke(state['messages'],config)
                return {'messages':state['messages']+[
                    AIMessage(content='',tool_calls=[{'name':'check_system_status','args':{'component':'api'},'id':'health-test'}]),
                    ToolMessage(content=json.dumps(value),name='check_system_status',tool_call_id='health-test'),draft]}
        with CaseFixture('guest') as fixture:
            with patch.object(project,'tech_agent',Agent()),patch.object(project,'supervisor_router') as router:
                router.invoke.return_value=project.RouteDecision(next='tech_support')
                parts=list(stream_turn(project.app,'检测接口状态',fixture.thread_id))
                text=''.join(p['text'] for p in parts if p['type']=='text')
                saved=project.app.get_state({'configurable':{'thread_id':fixture.thread_id}}).values['messages'][-1].content
                self.assertEqual(text,saved)
                self.assertIn('HTTP 401',text)
                for wrong in ['所有端点可访问','CPU 85%','余额充足']:
                    self.assertNotIn(wrong,text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
