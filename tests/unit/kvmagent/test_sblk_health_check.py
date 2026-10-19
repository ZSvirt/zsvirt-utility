import ast
import importlib.util
import os
import re
import sys
import textwrap
import types
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


def _load_sanlock():
    package = types.ModuleType('zstacklib')
    package.__path__ = []
    utils = types.ModuleType('zstacklib.utils')
    utils.__path__ = []
    log_module = types.ModuleType('zstacklib.utils.log')
    linux_module = types.ModuleType('zstacklib.utils.linux')
    thread_module = types.ModuleType('zstacklib.utils.thread')
    bash_module = types.ModuleType('zstacklib.utils.bash')

    log_module.get_logger = lambda _name: _LoggerStub()
    bash_module.in_bash = lambda fn: fn
    modules = {
        'zstacklib': package,
        'zstacklib.utils': utils,
        'zstacklib.utils.log': log_module,
        'zstacklib.utils.linux': linux_module,
        'zstacklib.utils.thread': thread_module,
        'zstacklib.utils.bash': bash_module,
    }
    module_path = os.path.join(ROOT, 'zstacklib', 'zstacklib', 'utils', 'sanlock.py')
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('zstacklib.utils.sanlock_under_test', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


sanlock = _load_sanlock()


def _load_health_check(uptime):
    namespace = {
        'logger': _LoggerStub(),
        'sanlock': sanlock,
        'linux': types.SimpleNamespace(
            ignore_error_retry=lambda *args, **kwargs: (lambda fn: fn)),
        'bash': types.SimpleNamespace(bash_errorout=lambda _cmd: 's %s\n' % LOCKSPACE),
    }
    path = os.path.join(ROOT, 'kvmagent', 'kvmagent', 'plugins', 'ha_plugin.py')
    lines = open(path, encoding='utf-8').read().splitlines(True)
    names = ('inc_vg_failure_cnt', 'reset_vg_failure_cnt', '_do_health_check_vg', '_do_health_check')
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
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, path, 'exec'), namespace)

    class Checker(object):
        def __init__(self):
            self.vg_failures = {}
            self.all_vgs = {VG: object()}

    for name in names:
        setattr(Checker, name, namespace[name])

    clock = {'now': uptime}
    sanlock.read_monotonic_uptime = lambda: clock['now']
    return Checker(), clock


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


def test_missing_lockspace_counts_until_max_failure():
    checker, _clock = _load_health_check(0)

    class Parser(object):
        def get_lockspace_record(self, _needle):
            return None

    sanlock.SanlockClientStatusParser = Parser
    for expected in range(1, 6):
        assert checker._do_health_check(5, 6) == {}
        assert checker.vg_failures[VG] == expected

    victims = checker._do_health_check(5, 6)
    assert VG in victims
    assert checker.vg_failures[VG] == 6


def _run(checker, record):
    class Parser(object):
        def get_lockspace_record(self, _needle):
            return record

    sanlock.SanlockClientStatusParser = Parser
    return checker._do_health_check(5, 6)
