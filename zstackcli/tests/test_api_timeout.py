"""Client timeout regressions; run with Python 3 and no MN or installed SDK.

Only HTTP, sleep, logging and the generated inventory are replaced. The real
binding and CLI timeout adapter execute against JSON request/response fixtures.
"""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
API_NAME = 'org.zstack.header.storage.snapshot.APIBatchDeleteVolumeSnapshotMsg'
QUERY_NAME = 'org.zstack.core.config.APIQueryGlobalConfigMsg'
EVENT_NAME = 'org.zstack.header.storage.snapshot.APIBatchDeleteVolumeSnapshotEvent'


class Message:
    FULL_NAME = API_NAME
    RESPONSE_NAME = 'APIBatchDeleteVolumeSnapshotEvent'

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.session = SimpleNamespace(uuid='session-uuid')
        self.uuids = ['snapshot-uuid']


class QueryMessage:
    FULL_NAME = QUERY_NAME
    RESPONSE_NAME = 'APIQueryGlobalConfigReply'

    def __init__(self):
        self.timeout = None
        self.session = None
        self.conditions = None


def dumps(value):
    return json.dumps(value, default=lambda obj: {
        key: val for key, val in vars(obj).items() if val is not None
    })


class ClientFixture:
    def __init__(self, done_after=1, initial_done=False, values=None):
        self.done_after = done_after
        self.initial_done = initial_done
        self.values = values or ['24h']
        self.inventories = None
        self.query_error = None
        self.query_success = True
        self.event_success = True
        self.posts = []
        self.gets = []
        self.slept_ms = 0
        inventory = ModuleType('apibinding.inventory')
        inventory.APIQueryGlobalConfigMsg = QueryMessage
        for name in ('NotNoneField', 'NotNoneList', 'NotNoneMap',
                     'OptionalList', 'OptionalMap'):
            setattr(inventory, name, type(name, (), {}))
        binding = ModuleType('apibinding')
        binding.__path__ = [str(ROOT / 'apibinding' / 'apibinding')]
        binding.inventory = inventory
        utils = ModuleType('zstacklib.utils')
        utils.jsonobject = SimpleNamespace(
            dumps=dumps,
            loads=lambda value: json.loads(value, object_hook=lambda obj: SimpleNamespace(**obj)),
        )
        utils.log = SimpleNamespace(
            get_logger=lambda name: SimpleNamespace(debug=lambda msg: None),
            mask_sensitive_field=lambda event, text: text,
        )
        http_source = (ROOT / 'zstacklib/zstacklib/utils/http.py').read_text(encoding='utf-8')
        url_namespace = {}
        exec(compile(http_source[http_source.index('class UriBuilder(object):'):
                                 http_source.index('def path_msg(')],
                     'http.py URL builder', 'exec'), url_namespace)
        utils.http = SimpleNamespace(
            build_url=url_namespace['build_url'],
            json_dump_post=self.post,
            json_dump_get=self.get,
        )
        modules = {'apibinding': binding, 'apibinding.inventory': inventory,
                   'zstacklib': ModuleType('zstacklib'), 'zstacklib.utils': utils}
        with patch.dict(sys.modules, modules):
            self.api = self.load('apibinding.api', ROOT / 'apibinding/apibinding/api.py')
            binding.api = self.api
            adapter_path = ROOT / 'zstackcli/zstackcli/api_timeout.py'
            self.adapter = self.load('cli_api_timeout', adapter_path)
        self.api.time = SimpleNamespace(sleep=self.sleep)
        self.client = self.api.Api()

    @staticmethod
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def sleep(self, seconds):
        self.slept_ms += round(seconds * 1000)

    def event_response(self):
        return {'state': 'Done', 'uuid': 'job-uuid', 'result': json.dumps({
            EVENT_NAME: {'success': self.event_success,
                         'error': {'code': 'TEST.ERROR', 'details': 'business failed'}}
        })}

    def post(self, url, command, **kwargs):
        payload = json.loads(dumps(command))
        self.posts.append(payload)
        if QUERY_NAME in payload:
            if self.query_error:
                raise self.api.ApiError(self.query_error)
            rows = self.inventories
            if rows is None:
                value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
                rows = [{'category': 'apiTimeout', 'name': API_NAME,
                         'value': value, 'defaultValue': '21600000'}]
            return json.dumps({'state': 'Done', 'result': json.dumps({
                'org.zstack.core.config.APIQueryGlobalConfigReply':
                    {'success': self.query_success, 'inventories': rows,
                     'error': {'code': 'TEST.DENIED', 'details': 'permission denied'}}
            })})
        if self.initial_done:
            return json.dumps(self.event_response())
        return json.dumps({'state': 'Processing', 'uuid': 'job-uuid'})

    def get(self, url, **kwargs):
        self.gets.append(url)
        if self.done_after is not None and len(self.gets) >= self.done_after:
            return json.dumps(self.event_response())
        return json.dumps({'state': 'Processing', 'uuid': 'job-uuid'})

    def call_cli(self, message=None, skip_config=False):
        return self.adapter.call_with_timeout(self.client, message or Message(),
                                              skip_config=skip_config)


class BindingCompatibilityTest(unittest.TestCase):
    def test_immediate_done_does_not_poll(self):
        f = ClientFixture(initial_done=True)
        self.assertTrue(f.client.async_call_wait_for_complete(Message())[1].success)
        self.assertEqual(f.gets, [])

    def test_old_caller_retains_thirty_minute_default(self):
        f = ClientFixture(done_after=None)
        with self.assertRaises(f.api.ApiError):
            f.client.async_call_wait_for_complete(Message())
        self.assertEqual(f.slept_ms, 1800000)
        self.assertEqual(len(f.posts), 1)
        self.assertEqual(len(f.gets), 3600)

    def test_existing_explicit_timeout_still_applies(self):
        f = ClientFixture(done_after=None)
        with self.assertRaises(f.api.ApiError):
            f.client.async_call_wait_for_complete(Message(1000))
        self.assertEqual(f.slept_ms, 1000)
        self.assertEqual(f.posts[0][API_NAME]['timeout'], 1000)

    def test_business_failure_is_preserved(self):
        f = ClientFixture(initial_done=True)
        f.event_success = False
        with self.assertRaisesRegex(f.api.ApiError, 'business failed'):
            f.client.async_call_wait_for_complete(Message())

    def test_error_event_can_be_returned_when_exceptions_are_disabled(self):
        f = ClientFixture()
        f.event_success = False
        result = f.client.async_call_wait_for_complete(Message(), exception_on_error=False)
        self.assertFalse(result[1].success)


class BindingWaitTimeoutTest(unittest.TestCase):
    def test_done_at_deadline_returns_completed_event(self):
        f = ClientFixture(done_after=2)
        try:
            result = f.client.async_call_wait_for_complete(Message(1000))
        except f.api.ApiError as error:
            self.fail('Done at deadline must win over timeout: %s' % error)
        self.assertTrue(result[1].success)

    def test_separate_budget_allows_completion_after_thirty_minutes(self):
        f = ClientFixture(done_after=3601)
        msg = Message()
        try:
            result = f.client.async_call_wait_for_complete(msg, wait_timeout=86400000)
        except TypeError as error:
            self.fail('Binding needs an independent wait budget: %s' % error)
        self.assertTrue(result[1].success)
        self.assertEqual(f.slept_ms, 1800500)
        self.assertIsNone(msg.timeout)
        self.assertNotIn('timeout', f.posts[0][API_NAME])
        self.assertNotIn('wait_timeout', f.posts[0][API_NAME])

    def test_timeout_identifies_wait_job_state_and_result_url_without_resubmit(self):
        f = ClientFixture(done_after=None)
        with self.assertRaises(f.api.ApiError) as raised:
            f.client.async_call_wait_for_complete(Message(1000))
        message = str(raised.exception)
        for detail in ('Client wait', 'job-uuid', 'Processing',
                       'http://localhost:8080/zstack/api/result/job-uuid'):
            self.assertIn(detail, message)
        self.assertEqual(len(f.posts), 1)
        self.assertEqual(len(f.gets), 2)

    def test_invalid_wait_budget_fails_before_submission(self):
        for budget in (0, -1, '1000', 1.5, True):
            with self.subTest(budget=budget):
                f = ClientFixture()
                with self.assertRaises(f.api.ApiError):
                    f.client.async_call_wait_for_complete(Message(), wait_timeout=budget)
                self.assertEqual(f.posts, [])

    def test_local_budget_can_differ_from_explicit_backend_timeout(self):
        f = ClientFixture(done_after=None)
        with self.assertRaises(f.api.ApiError):
            f.client.async_call_wait_for_complete(Message(1000), wait_timeout=2000)
        self.assertEqual(f.slept_ms, 2000)
        self.assertEqual(f.posts[0][API_NAME]['timeout'], 1000)


class CliTimeoutTest(unittest.TestCase):
    def test_current_value_overrides_default_and_waits_beyond_thirty_minutes(self):
        f = ClientFixture(done_after=3601)
        msg = Message()
        self.assertTrue(f.call_cli(msg)[1].success)
        self.assertEqual(f.slept_ms, 1800500)
        self.assertIsNone(msg.timeout)
        self.assertEqual(f.posts[0][QUERY_NAME]['conditions'], [
            {'name': 'category', 'op': '=', 'value': 'apiTimeout'},
            {'name': 'name', 'op': '=', 'value': API_NAME},
        ])
        self.assertEqual(f.posts[0][QUERY_NAME]['session']['uuid'], 'session-uuid')
        self.assertNotIn('timeout', f.posts[1][API_NAME])
        self.assertNotIn('wait_timeout', f.posts[1][API_NAME])
        self.assertEqual(len(f.posts), 2)

    def test_next_call_reads_changed_configuration(self):
        f = ClientFixture(values=['6h', '24h'])
        budgets = []
        call = f.client.async_call_wait_for_complete

        def record(*args, **kwargs):
            budgets.append(kwargs['wait_timeout'])
            return call(*args, **kwargs)

        f.client.async_call_wait_for_complete = record
        f.call_cli()
        f.call_cli()
        self.assertEqual(budgets, [21600000, 86400000])
        self.assertEqual(len(f.posts), 4)

    def test_explicit_timeout_is_integer_in_business_request_and_needs_no_query(self):
        f = ClientFixture()
        f.query_error = 'permission denied'
        msg = Message('3600000')
        self.assertTrue(f.call_cli(msg)[1].success)
        self.assertEqual(type(msg.timeout), int)
        self.assertEqual(msg.timeout, 3600000)
        self.assertEqual(len(f.posts), 1)
        self.assertEqual(f.posts[0][API_NAME]['timeout'], 3600000)

    def test_invalid_explicit_timeout_never_submits(self):
        for value in ('0', '-1', '1.5', '24h', '', True):
            with self.subTest(value=value):
                f = ClientFixture()
                with self.assertRaises(f.api.ApiError):
                    f.call_cli(Message(value))
                self.assertEqual(f.posts, [])

    def test_explicit_timeout_controls_local_waiting_too(self):
        f = ClientFixture(done_after=None)
        with self.assertRaises(f.api.ApiError):
            f.call_cli(Message('3600000'))
        self.assertEqual(f.slept_ms, 3600000)
        self.assertEqual(len(f.posts), 1)

    def test_synchronous_and_no_session_calls_do_not_query_configuration(self):
        for response, session_uuid, skip in (
                ('APIQueryGlobalConfigReply', 'session-uuid', False),
                ('APILogInByAccountReply', None, True),
                ('APIGetVersionReply', 'session-uuid', False),
                ('APICustomEvent', None, False),
                ('APICustomEvent', 'old-session-uuid', True)):
            with self.subTest(response=response, skip=skip):
                f = ClientFixture(initial_done=True)
                msg = Message()
                msg.RESPONSE_NAME = response
                msg.session.uuid = session_uuid
                f.call_cli(msg, skip_config=skip)
                self.assertEqual(len(f.posts), 1)
                self.assertIn(API_NAME, f.posts[0])

    def test_query_denied_stops_before_business_submission(self):
        f = ClientFixture()
        f.query_success = False
        with self.assertRaisesRegex(f.api.ApiError, '(?s)apiTimeout.*permission denied'):
            f.call_cli()
        self.assertEqual(len(f.posts), 1)
        self.assertIn(QUERY_NAME, f.posts[0])
        self.assertEqual(f.gets, [])

    def test_query_transport_failure_stops_before_business_submission(self):
        f = ClientFixture()
        f.query_error = 'connection failed'
        with self.assertRaisesRegex(f.api.ApiError, 'apiTimeout.*connection failed'):
            f.call_cli()
        self.assertEqual(len(f.posts), 1)
        self.assertIn(QUERY_NAME, f.posts[0])

    def test_missing_duplicate_and_invalid_configuration_stop_before_business_submission(self):
        for rows in ([], [{'value': '6h'}, {'value': '24h'}],
                     [{'value': 'bad'}], [{'value': None}], [{'value': '0'}]):
            with self.subTest(rows=rows):
                f = ClientFixture()
                f.inventories = rows
                with self.assertRaisesRegex(f.api.ApiError, 'apiTimeout'):
                    f.call_cli()
                self.assertEqual(len(f.posts), 1)
                self.assertIn(QUERY_NAME, f.posts[0])

    def test_server_time_units_are_parsed(self):
        f = ClientFixture()
        cases = [('21600000', 21600000), ('24h', 86400000), ('1s', 1000),
                 ('2m', 120000), ('1d', 86400000), ('1w', 604800000),
                 ('1y', 31536000000), ('24 H ', 86400000), ('1M', 60000),
                 ('1S', 1000), ('1D', 86400000), ('1W', 604800000),
                 ('1Y', 31536000000), ('123 ', 123)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(f.adapter.parse_api_timeout(value), expected)
        for value in ('', None, '0', '-1s', '+1h', ' 24h', '1.5h', '1ms', '1h extra'):
            with self.subTest(value=value):
                with self.assertRaises(f.api.ApiError):
                    f.adapter.parse_api_timeout(value)


if __name__ == '__main__':
    unittest.main()
