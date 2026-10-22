import ast
import os
import re
import textwrap
import types
import json
import shlex
import shutil
import socket
import subprocess
import time
from unittest.mock import mock_open, patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
VG = 'vg-1'
LOCKSPACE = 'lvm_vg-1'
IO_TIMEOUT = 20
LAST_SUCCESS = 71507
LAST_ATTEMPT = 71612


class _LoggerStub(object):
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warn(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def _load_sanlock():
    # Load the actual pure parser/judgment code without importing storage services.
    module_path = os.path.join(ROOT, 'zstacklib', 'zstacklib', 'utils', 'sanlock.py')
    tree = ast.parse(open(module_path, encoding='utf-8').read())
    names = {'SanlockClientStatus', 'read_monotonic_uptime', 'io_failed',
             'calc_id_renewal_fail_seconds'}
    selected = [node for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    selected += [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id in
                         ('SANLK_EIO', 'SANLK_AIO_TIMEOUT') for target in node.targets)]
    namespace = {'logger': _LoggerStub()}
    exec(compile(ast.Module(body=selected, type_ignores=[]), module_path, 'exec'), namespace)
    return types.SimpleNamespace(**namespace)


sanlock = _load_sanlock()


def _load_health_check(uptime):
    namespace = {
        'logger': _LoggerStub(),
        'sanlock': types.SimpleNamespace(**vars(sanlock)),
        'linux': types.SimpleNamespace(
            ignore_error_retry=lambda *args, **kwargs: (lambda fn: fn)),
        'bash': types.SimpleNamespace(bash_errorout=lambda _cmd: 's %s\n' % LOCKSPACE),
    }
    path = os.path.join(ROOT, 'kvmagent', 'kvmagent', 'plugins', 'ha_plugin.py')
    lines = open(path, encoding='utf-8').read().splitlines(True)
    names = ('inc_vg_failure_cnt', 'reset_vg_failure_cnt', '_do_health_check_vg',
             '_do_sanlock_heartbeat_check', '_do_health_check_vg_by_sanlock',
             '_do_health_check_vg_by_zsblkagent', '_do_zsblkagent_heartbeat_check',
             '_do_health_check', 'fencer_backend_ready')
    selected = []
    indent = 4
    for index, line in enumerate(lines):
        match = re.match(r'^    def (\w+)\(', line)
        if not match or match.group(1) not in names:
            continue
        end = index + 1
        while end < len(lines):
            next_line = lines[end]
            if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= indent:
                break
            end += 1
        selected.extend(ast.parse(textwrap.dedent(''.join(lines[index:end]))).body)
    source_tree = ast.parse(''.join(lines))
    selected += [node for node in source_tree.body
                 if isinstance(node, ast.ClassDef) and node.name == 'HealthCheckResult']
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, path, 'exec'), namespace)

    class Checker(object):
        def __init__(self):
            self.vg_failures = {}
            self.all_vgs = {VG: object()}

    for name in names:
        setattr(Checker, name, namespace[name])

    Checker.ZSBLKAGENT_HEARTBEAT_STATUS_PATH = 'http://127.0.0.1:7276/zsblk-agent/vg/heartbeat/status'
    namespace['shell'] = types.SimpleNamespace()
    namespace['jsonobject'] = types.SimpleNamespace(loads=lambda text: json.loads(
        text, object_hook=lambda fields: types.SimpleNamespace(**fields)))
    namespace['linux'].get_current_timestamp = lambda: 1000
    clock = {'now': uptime}
    namespace['sanlock'].read_monotonic_uptime = lambda: clock['now']
    return Checker(), clock


def test_known_backend_readiness_does_not_probe_unresponsive_agent():
    checker, _clock = _load_health_check(0)
    checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: _result()}
    def unexpected(vgs):
        raise AssertionError('known sanlock backend must not wait for HTTP')
    checker._do_health_check_vg_by_zsblkagent = unexpected
    assert checker.fencer_backend_ready(VG) is True


def test_unresponsive_local_http_probe_is_bounded_and_keeps_other_vg_failure():
    checker, _clock = _load_health_check(0)
    checker.health_check_interval = 0.2
    checker.storage_timeout = 5
    checker.max_failure = 6
    calls = []
    curl = shutil.which('curl.exe') or shutil.which('curl')
    assert curl, 'curl is required to exercise the actual local transport deadline'
    def call(command):
        args = shlex.split(command)
        calls.append(args)
        return subprocess.check_output([curl] + args[1:], stderr=subprocess.PIPE)
    checker._do_health_check_vg_by_zsblkagent.__globals__['shell'].call = call
    # The original helper has a 120-second timeout; reject it without hanging
    # the RED run for minutes. The GREEN path below exercises real curl.
    def legacy_get(*args, **kwargs):
        raise AssertionError('legacy HTTP helper has timeout=120 and Retry(15)')
    checker._do_health_check_vg_by_zsblkagent.__globals__['http'] = types.SimpleNamespace(json_dump_get=legacy_get)
    # Complete the TCP handshake but never return HTTP headers or data.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        checker.ZSBLKAGENT_HEARTBEAT_STATUS_PATH = 'http://127.0.0.1:%d/status' % listener.getsockname()[1]
        checker.all_vgs['failed-vg'] = object()
        failure = _result(failed=True)
        checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: _result(no_way=True), 'failed-vg': failure}
        start = time.monotonic()
        results = checker._do_health_check(5, 6)
        assert time.monotonic() - start < 1.5
    assert results[VG].no_way is True
    assert results['failed-vg'] is failure
    assert len(calls) == 1
    assert calls[0][calls[0].index('--retry') + 1] == '0'


class Record(object):
    def __init__(self, result=1, space_dead=0, attempt=LAST_ATTEMPT, success=LAST_SUCCESS,
                 io_timeout=IO_TIMEOUT, lockspace=LOCKSPACE, is_adding=False):
        self.renewal_last_result = result
        self.renewal_last_attempt = attempt
        self.renewal_last_success = success
        self.io_timeout = io_timeout
        self.space_dead = space_dead
        self.lockspace = lockspace
        self.is_adding = is_adding

    def get_lockspace(self):
        return self.lockspace

    def get_renewal_last_result(self):
        return self.renewal_last_result

    def get_renewal_last_attempt(self):
        return self.renewal_last_attempt

    def get_renewal_last_success(self):
        return self.renewal_last_success

    def get_io_timeout(self):
        return self.io_timeout

    def is_space_dead(self):
        return bool(self.space_dead)


def test_missing_space_dead_defaults_to_alive():
    record = sanlock.SanlockClientStatus([
        's %s:1:/dev/mapper/vg-lvmlock:0' % LOCKSPACE,
        'renewal_last_result=1',
        'io_timeout=20',
    ])
    assert record.space_dead == 0
    assert record.is_space_dead() is False


def test_space_dead_flag_is_parsed():
    record = sanlock.SanlockClientStatus([
        's %s:1:/dev/mapper/vg-lvmlock:0' % LOCKSPACE,
        'space_dead=1',
    ])
    assert record.is_space_dead() is True


def test_read_monotonic_uptime_uses_first_field():
    with patch('builtins.open', mock_open(read_data='71671.15 100.00\n')):
        assert sanlock.read_monotonic_uptime() == 71671.15


def test_read_monotonic_uptime_failure_returns_none():
    with patch('builtins.open', side_effect=OSError('unread')):
        assert sanlock.read_monotonic_uptime() is None


def test_io_failed_accepts_only_eio_and_aio_timeout():
    assert sanlock.io_failed(-5) is True
    assert sanlock.io_failed(-202) is True
    assert sanlock.io_failed(1) is False
    assert sanlock.io_failed(-201) is False


def test_aged_io_error_kills_without_accumulating():
    checker, _clock = _load_health_check(71671.15)
    checker.vg_failures[VG] = 2
    assert LAST_ATTEMPT - LAST_SUCCESS == 105
    assert sanlock.calc_id_renewal_fail_seconds(IO_TIMEOUT) == 160

    victims = _run(checker, Record(result=-202, space_dead=0))

    assert VG in victims
    assert '71671.15' in victims[VG]
    assert str(LAST_SUCCESS) in victims[VG]
    assert checker.vg_failures[VG] == 2

    victims = _run(checker, Record(result=-5, space_dead=0))
    assert VG in victims


def test_space_dead_kills_before_renewal_threshold():
    checker, _clock = _load_health_check(LAST_SUCCESS + 120)
    victims = _run(checker, Record(result=-202, space_dead=1))

    assert VG in victims
    assert 'space_dead=1' in victims[VG]
    assert VG not in checker.vg_failures


def test_successful_renewal_resets_even_if_space_dead():
    checker, _clock = _load_health_check(LAST_SUCCESS + 1000)
    checker.vg_failures[VG] = 4
    victims = _run(checker, Record(result=1, space_dead=1))

    assert victims == {}
    assert VG not in checker.vg_failures


def test_short_io_error_resets_failure_count():
    checker, _clock = _load_health_check(LAST_SUCCESS + 30)
    checker.vg_failures[VG] = 3
    victims = _run(checker, Record(result=-202, space_dead=0))

    assert victims == {}
    assert VG not in checker.vg_failures


def test_non_io_result_leaves_failure_count_unchanged():
    checker, _clock = _load_health_check(LAST_SUCCESS + 1000)
    checker.vg_failures[VG] = 3
    victims = _run(checker, Record(result=-201, space_dead=0))

    assert victims == {}
    assert checker.vg_failures[VG] == 3


def test_uptime_read_failure_leaves_failure_count_unchanged():
    checker, _clock = _load_health_check(None)
    checker.vg_failures[VG] = 2
    victims = _run(checker, Record(result=-202, space_dead=0))

    assert victims == {}
    assert checker.vg_failures[VG] == 2


def test_monotonic_wrap_kills_when_uptime_resets():
    checker, _clock = _load_health_check(10)
    victims = _run(checker, Record(result=-202, success=1000, attempt=10))
    assert VG in victims
    assert VG not in checker.vg_failures


def test_adding_lockspace_resets_even_if_space_dead():
    checker, _clock = _load_health_check(LAST_SUCCESS + 1000)
    checker.vg_failures[VG] = 2
    victims = _run(checker, Record(space_dead=1, is_adding=True))

    assert victims == {}
    assert VG not in checker.vg_failures


def test_missing_lockspace_is_unknown_instead_of_counting_to_kill():
    checker, _clock = _load_health_check(0)
    result = checker._do_sanlock_heartbeat_check(VG, None, [LOCKSPACE])
    assert result.no_way is True
    assert result.failed is None
    assert VG not in checker.vg_failures


def _run(checker, record):
    class Parser(object):
        def get_lockspace_record(self, _needle):
            return record

    sanlock.SanlockClientStatusParser = Parser
    result = checker._do_sanlock_heartbeat_check(VG, record, [LOCKSPACE])
    return {VG: result.error} if result.failed else {}


def _result(no_way=False, failed=False):
    return types.SimpleNamespace(no_way=no_way, failed=failed)


def test_unknown_sanlock_uses_independent_heartbeat():
    checker, _clock = _load_health_check(0)
    fallback = _result(failed=False)
    checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: _result(no_way=True)}
    checker._do_health_check_vg_by_zsblkagent = lambda vgs: {VG: fallback}
    assert checker._do_health_check(5, 6)[VG] is fallback


def test_known_sanlock_failure_is_not_overridden_by_healthy_fallback():
    checker, _clock = _load_health_check(0)
    failure = _result(failed=True)
    checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: failure}
    def unexpected(vgs):
        raise AssertionError('known storage failure must not use fallback')
    checker._do_health_check_vg_by_zsblkagent = unexpected
    assert checker._do_health_check(5, 6)[VG] is failure


def test_two_unknown_channels_remain_unknown():
    checker, _clock = _load_health_check(0)
    checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: _result(no_way=True)}
    checker._do_health_check_vg_by_zsblkagent = lambda vgs: {VG: _result(no_way=True)}
    assert checker._do_health_check(5, 6)[VG].no_way is True


def test_backend_ready_requires_at_least_one_known_channel():
    checker, _clock = _load_health_check(0)
    for sanlock_unknown, agent_unknown, expected in ((True, True, False),
            (True, False, True), (False, True, True), (False, False, True)):
        checker._do_health_check_vg_by_sanlock = lambda vgs: {VG: _result(no_way=sanlock_unknown)}
        checker._do_health_check_vg_by_zsblkagent = lambda vgs: {VG: _result(no_way=agent_unknown)}
        assert checker.fencer_backend_ready(VG) is expected


def test_agent_heartbeat_distinguishes_stale_success_and_timeout():
    checker, _clock = _load_health_check(0)
    checker.max_failure = 6
    checker.health_check_interval = 5
    checker.storage_timeout = 5
    assert checker._do_zsblkagent_heartbeat_check(VG, {}).no_way is True
    stale = types.SimpleNamespace(lastCheck=1, lastSuccess=1, code=0, error='')
    assert checker._do_zsblkagent_heartbeat_check(VG, {VG: stale}).no_way is True
    success = types.SimpleNamespace(lastCheck=1000, lastSuccess=1000, code=0, error='')
    assert checker._do_zsblkagent_heartbeat_check(VG, {VG: success}).failed is False
    timeout = types.SimpleNamespace(lastCheck=1000, lastSuccess=900, code=-2, error='timeout')
    result = checker._do_zsblkagent_heartbeat_check(VG, {VG: timeout})
    assert result.failed is True
    assert result.io_timeout is True
