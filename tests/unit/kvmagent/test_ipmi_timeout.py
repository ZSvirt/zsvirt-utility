import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[3]
COLLECTORS = ('collect_ipmi_state', 'collect_equipment_state_from_ipmi', 'collect_equipment_state')
TIME_SCALE = 20
clock_spec = importlib.util.spec_from_file_location(
    'ipmi_test_restart_fence', ROOT / 'zstacklib/zstacklib/utils/restart_fence.py')
production_clock = importlib.util.module_from_spec(clock_spec)
clock_spec.loader.exec_module(production_clock)


def load_functions(relative_path, names, namespace, class_name=None, constants=(), clock=None):
    path = ROOT / relative_path
    lines = path.read_text().splitlines(True)
    selected = []
    indent = 4 if class_name else 0
    for index, line in enumerate(lines):
        if any(re.match(r'^%s\s*=' % name, line) for name in constants):
            selected.extend(ast.parse(line).body)
        if line.startswith('from zstacklib.utils.restart_fence import '):
            for alias in ast.parse(line).body[0].names:
                if alias.name == 'monotonic_time':
                    namespace[alias.asname or alias.name] = clock or production_clock.monotonic_time
        match = re.match(r'^%sdef (\w+)\(' % (' ' * indent), line)
        if not match or match.group(1) not in names:
            continue
        end = index + 1
        while end < len(lines):
            next_line = lines[end]
            if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= indent:
                break
            end += 1
        selected.extend(ast.parse(textwrap.dedent(''.join(lines[index:end]))).body)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class Metric:
    def __init__(self, name, *args):
        self.name = name
        self.samples = []

    def add_metric(self, labels, value):
        self.samples.append((labels, value))


def host_namespace(clock=None, command=None):
    namespace = dict(
        re=re, logger=MagicMock(), bash_roe=command, shell_quote=shlex.quote,
        shell=SimpleNamespace(ShellCmd=MagicMock(), TIMEOUT_EXIT_CODE=124, SIGKILL_EXIT_CODE=137),
        http=SimpleNamespace(REQUEST_BODY='body'),
        jsonobject=SimpleNamespace(loads=lambda value: SimpleNamespace(**json.loads(value)),
                                   dumps=lambda value: json.dumps(vars(value))),
        kvmagent=SimpleNamespace(AgentResponse=lambda: SimpleNamespace(success=True)))
    return load_functions('kvmagent/kvmagent/plugins/host_plugin.py',
                          ('identify_host', '_collect_ipmi_info'), namespace, 'HostPlugin',
                          constants=('IPMI_COMMAND',), clock=clock)


def collector_namespace(command, clock=None, sensor_info=''):
    form = load_functions('zstacklib/zstacklib/utils/form.py', ('_load', 'load'), dict(logger=MagicMock()))
    namespace = dict(
        GaugeMetricFamily=Metric, time=time, re=re, shell=SimpleNamespace(TIMEOUT_EXIT_CODE=124),
        bash_ro=lambda cmd, **kw: command(cmd, **kw)[:2],
        bash_r=lambda cmd, **kw: command(cmd, **kw)[0],
        form=SimpleNamespace(load=form['load']), jsonobject=SimpleNamespace(loads=json.loads),
        is_virtual_machine=lambda: False, get_sensor_info_from_ipmi=lambda: sensor_info,
        check_equipment_state_from_ipmitool=MagicMock(),
        collect_equipment_state_last_time=None, collect_equipment_state_last_result=None,
        send_cpu_status_alarm_to_mn=MagicMock(), remove_cpu_status_abnormal=MagicMock())
    return load_functions('kvmagent/kvmagent/plugins/prometheus.py',
                          COLLECTORS + ('_collect_hardware_command', 'get_power_info_from_ipmi',
                                        'to_float_or_none'), namespace,
                          constants=('HARDWARE_COLLECTION_TIMEOUT',), clock=clock)


@pytest.mark.parametrize('interval', [0, 60, 255, '10'])
def test_identify_preserves_supported_intervals_and_bounds_command(interval):
    namespace = host_namespace()
    result = namespace['identify_host'](None, {'body': json.dumps({'interval': interval})})
    assert json.loads(result)['success'] is True
    namespace['shell'].ShellCmd.assert_called_once_with(
        'timeout -k 5s 30s ipmitool chassis identify %s' % int(interval))
    namespace['shell'].ShellCmd.return_value.assert_called_once_with(True)


@pytest.mark.parametrize('interval', [-1, 256, 1.5, True, None, '10; echo injected', '10\n', 'force',
                                    pytest.param('9' * 10000, id='oversized-number')])
def test_identify_rejects_invalid_input_before_command_execution(interval):
    namespace = host_namespace()
    result = namespace['identify_host'](None, {'body': json.dumps({'interval': interval})})
    assert json.loads(result)['success'] is False
    assert '0 and 255' in json.loads(result)['error']
    namespace['shell'].ShellCmd.assert_not_called()


def test_discovery_failures_share_one_budget():
    command = MagicMock(return_value=(1, '', 'BMC failure'))
    namespace = host_namespace(MagicMock(side_effect=[100, 100, 125, 130]), command)
    assert '_collect_ipmi_info' in namespace, 'host discovery must have a bounded collection path'
    response = SimpleNamespace()
    namespace['_collect_ipmi_info'](None, response)
    assert [call.args[0] for call in command.call_args_list] == [
        'timeout -k 5s 30.000s ipmitool mc info',
        'timeout -k 5s 5.000s ipmitool lan print 1']
    assert (response.bmcVersion, response.ipmiAddress) == ('unknown', 'None')


def test_submillisecond_discovery_budget_never_disables_timeout():
    command = MagicMock(return_value=(1, '', 'BMC failure'))
    namespace = host_namespace(MagicMock(side_effect=[100, 129.9999, 130]), command)
    assert '_collect_ipmi_info' in namespace, 'host discovery must have a bounded collection path'
    namespace['_collect_ipmi_info'](None, SimpleNamespace())
    command.assert_called_once_with('timeout -k 5s 0.001s ipmitool mc info')


def test_exhausted_metric_budget_does_not_start_command():
    command = MagicMock()
    namespace = collector_namespace(command, clock=lambda: 100)
    assert '_collect_hardware_command' in namespace, 'hardware commands must use the shared budget'
    assert namespace['_collect_hardware_command']('ipmitool mc info', 100) == (124, '')
    command.assert_not_called()


@pytest.fixture
def hardware(tmp_path, monkeypatch):
    if not sys.platform.startswith('linux'):
        pytest.skip('requires Linux GNU timeout, fork and /proc')
    real_timeout = shutil.which('timeout')
    assert real_timeout, 'GNU timeout is required'
    scripts = {
        'timeout': '''#!/usr/bin/env python3
import os, sys
assert sys.argv[1:3] == ['-k', '5s']
duration = float(sys.argv[3][:-1])
assert 0 < duration <= 30
os.execv(%r, ['timeout', '-k', '0.05s', str(duration / %s) + 's'] + sys.argv[4:])
''' % (real_timeout, TIME_SCALE),
        'ipmitool': '''#!/usr/bin/env python3
import os, signal, sys, time
with open(os.environ['IPMI_CALLS'], 'a') as stream:
    stream.write(' '.join(sys.argv[1:]) + '\\n')
if os.environ.get('IPMI_HANG') in ('all', sys.argv[1]):
    if os.environ.get('IGNORE_TERM') == '1':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.alarm(5)
    if os.fork() == 0:
        signal.alarm(5)
    with open(os.environ['IPMI_PIDS'], 'a') as stream:
        stream.write(str(os.getpid()) + '\\n')
    print('CPU1 Status | 3Ch | ok | 3.96 | Absent', flush=True)
    print('PS1 Status | fail', flush=True)
    while True:
        time.sleep(0.01)
elif sys.argv[1] == 'mc':
    print('Firmware Revision : 1.23')
    sys.exit(int(os.environ.get('IPMI_EXIT', '0')))
elif sys.argv[1] == 'lan':
    if sys.argv[-1] != '2':
        sys.exit(1)
    print('IP Address Source : Static Address')
    print('IP Address : 192.0.2.10')
else:
    print('PS1 Status | ok')
''',
        'sensors': '#!/bin/sh\nexit 1\n',
        'hd_ctl': '#!/bin/sh\nexit 1\n',
    }
    for name, source in scripts.items():
        path = tmp_path / name
        path.write_text(source)
        path.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('IPMI_CALLS', str(tmp_path / 'calls'))
    monkeypatch.setenv('IPMI_PIDS', str(tmp_path / 'pids'))
    monkeypatch.setenv('IPMI_HANG', 'all')
    monkeypatch.setenv('IGNORE_TERM', '1')
    bash = load_functions('zstacklib/zstacklib/utils/bash.py', ('bash_roe', 'bash_ro', 'bash_r'), dict(
        __collect_locals_on_stack=lambda: {}, bash_eval=lambda command, _: command,
        shell=SimpleNamespace(get_process=lambda command, **kwargs: subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True))))
    yield SimpleNamespace(path=tmp_path, command=bash['bash_roe'],
                          clock=lambda: production_clock.monotonic_time() * TIME_SCALE)
    pids = tmp_path / 'pids'
    if pids.exists():
        for pid in pids.read_text().splitlines():
            stat = Path('/proc') / pid / 'stat'
            deadline = time.monotonic() + 2
            while stat.exists():
                try:
                    state = stat.read_text().split(') ')[1]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state.startswith('Z '):
                    break
                assert time.monotonic() < deadline, 'timed-out child %s is still running' % pid
                time.sleep(0.01)


@pytest.mark.parametrize('collector', COLLECTORS)
def test_hung_commands_do_not_publish_partial_hardware_failures(hardware, collector):
    namespace = collector_namespace(hardware.command, hardware.clock)
    started = time.monotonic()
    metrics = {metric.name: metric for metric in namespace[collector]()}
    assert time.monotonic() - started < 3
    assert metrics['ipmi_status'].samples[0][1] in (124, 137)
    assert all(not metric.samples for name, metric in metrics.items() if name != 'ipmi_status')
    namespace['send_cpu_status_alarm_to_mn'].assert_not_called()
    assert len((hardware.path / 'calls').read_text().splitlines()) == 1


@pytest.mark.parametrize('ignore_term, expected_status', [('0', 124), ('1', 137)])
def test_pipeline_reports_timeout_and_terminates_descendants(hardware, monkeypatch, ignore_term, expected_status):
    monkeypatch.setenv('IGNORE_TERM', ignore_term)
    namespace = collector_namespace(hardware.command, hardware.clock)
    status, output = namespace['_collect_hardware_command'](
        'ipmitool sdr elist | grep CPU', hardware.clock() + 30)
    assert status == expected_status
    assert 'Absent' in output


@pytest.mark.parametrize('hang, expected_calls, version, address', [
    ('all', ['mc info'], 'unknown', 'None'),
    ('lan', ['mc info', 'lan print 1'], '1.23', 'None'),
    ('', ['mc info', 'lan print 1', 'lan print 2'], '1.23', '192.0.2.10'),
])
def test_discovery_stops_retries_on_timeout(hardware, monkeypatch, hang, expected_calls, version, address):
    monkeypatch.setenv('IPMI_HANG', hang)
    namespace = host_namespace(hardware.clock, hardware.command)
    response = SimpleNamespace()
    started = time.monotonic()
    namespace['_collect_ipmi_info'](None, response)
    assert time.monotonic() - started < 3
    assert (response.bmcVersion, response.ipmiAddress) == (version, address)
    assert (hardware.path / 'calls').read_text().splitlines() == expected_calls


def test_normal_zsv_sensor_output_preserves_metrics(hardware, monkeypatch):
    monkeypatch.setenv('IPMI_HANG', '')
    sensors = "4|CPU1_Temp|Temperature|34.00|C|'OK'\n54|CPU1_Status|Processor|N/A|N/A|'Processor Presence detected'"
    namespace = collector_namespace(hardware.command, hardware.clock, sensors)
    metrics = {metric.name: metric for metric in namespace['collect_equipment_state_from_ipmi']()}
    assert metrics['ipmi_status'].samples == [([], 0)]
    assert metrics['cpu_temperature'].samples == [(['CPU1'], 34.0)]
    assert metrics['cpu_status'].samples == [(['CPU1'], 0)]
    namespace['send_cpu_status_alarm_to_mn'].assert_not_called()


@pytest.fixture
def bmc_probe():
    logger = MagicMock()
    path = ROOT / 'zstacklib/zstacklib/utils/shell.py'
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in ('get_process', 'ShellError', 'ShellCmd')]
    for node in nodes:
        node.decorator_list = []
    shell = dict(subprocess=subprocess, log=SimpleNamespace(get_logger=lambda _: logger),
                 TIMEOUT_EXIT_CODE=124, SIGKILL_EXIT_CODE=137, SIGKILL_RETURN_CODE=-9)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), shell)
    return load_functions('zstacklib/zstacklib/utils/linux.py', ('is_support_bmc',),
                          dict(shell=SimpleNamespace(**shell), logger=logger))


@pytest.mark.parametrize('ignore_term, codes', [('0', (124,)), ('1', (137, -9))])
def test_public_bmc_probe_timeout_and_recovery(hardware, bmc_probe, monkeypatch, ignore_term, codes):
    monkeypatch.setenv('IGNORE_TERM', ignore_term)
    commands = []
    shell_cmd = bmc_probe['shell'].ShellCmd

    def record(command):
        cmd = shell_cmd(command)
        commands.append(cmd)
        return cmd

    monkeypatch.setattr(bmc_probe['shell'], 'ShellCmd', record)
    started = time.monotonic()
    assert bmc_probe['is_support_bmc']() is False
    assert time.monotonic() - started < 3
    assert commands[0].return_code in codes
    bmc_probe['logger'].warn.assert_called_once()
    assert len((hardware.path / 'pids').read_text().splitlines()) == 2
    monkeypatch.setenv('IPMI_HANG', '')
    assert bmc_probe['is_support_bmc']() is True
    assert (hardware.path / 'calls').read_text().splitlines() == ['mc info', 'mc info']


@pytest.mark.parametrize('exit_code, supported', [('0', True), ('1', False)])
def test_public_bmc_probe_uses_exit_status(hardware, bmc_probe, monkeypatch, exit_code, supported):
    monkeypatch.setenv('IPMI_HANG', '')
    monkeypatch.setenv('IPMI_EXIT', exit_code)
    assert bmc_probe['is_support_bmc']() is supported
    bmc_probe['logger'].warn.assert_not_called()


def bind_public_probe(namespace, bmc_probe, monkeypatch):
    monkeypatch.setitem(sys.modules, 'zstacklib.utils.linux', SimpleNamespace(
        is_support_bmc=bmc_probe['is_support_bmc'], is_virtual_machine=lambda: False))
    source = ROOT / 'kvmagent/kvmagent/plugins/prometheus.py'
    for line in source.read_text().splitlines():
        if line.startswith('from zstacklib.utils.linux import '):
            exec(line, namespace)
    assert namespace['is_support_bmc'] is bmc_probe['is_support_bmc']
    return source


@pytest.mark.parametrize('hang', ['all', ''])
def test_module_registration_binds_public_bmc_probe(hardware, bmc_probe, monkeypatch, hang):
    monkeypatch.setenv('IPMI_HANG', hang)
    registered = []
    namespace = dict(misc=SimpleNamespace(isMiniHost=lambda: False, isHyperConvergedHost=lambda: False),
                     _register_metric_collector=lambda collector: registered.append(collector))
    source = bind_public_probe(namespace, bmc_probe, monkeypatch)
    block = source.read_text().split('_register_metric_collector(collect_host_network_statistics)', 1)[1]
    block = block.split('class PrometheusPlugin', 1)[0]
    for name in re.findall(r'_register_metric_collector\((\w+)\)', block):
        namespace[name] = name
    started = time.monotonic()
    exec(compile(block, str(source), 'exec'), namespace)
    assert time.monotonic() - started < 3
    assert ('collect_equipment_state_from_ipmi' in registered) is (not hang)
    assert 'collect_raid_state' in registered
    assert (hardware.path / 'calls').read_text().splitlines() == ['mc info']


@pytest.mark.parametrize('hang, virtual', [('all', False), ('', False), ('all', True)])
def test_exporter_start_binds_public_probe_and_continues(hardware, bmc_probe, monkeypatch, hang, virtual):
    monkeypatch.setenv('IPMI_HANG', hang)
    commands = []
    namespace = dict(
        logger=MagicMock(), in_bash=lambda f: f,
        lock=SimpleNamespace(file_lock=lambda *a, **kw: lambda f: f, Flock=lambda: None),
        os=SimpleNamespace(path=SimpleNamespace(dirname=os.path.dirname, join=os.path.join,
                                                exists=lambda _: False),
                           listdir=lambda _: [], chmod=lambda *a: None),
        linux=SimpleNamespace(write_file=lambda *a: None), shell=SimpleNamespace(run=lambda _: 1),
        bash_errorout=commands.append, http=SimpleNamespace(REQUEST_BODY='body'),
        kvmagent=SimpleNamespace(AgentResponse=lambda: SimpleNamespace(success=True)),
        jsonobject=SimpleNamespace(loads=lambda s: json.loads(s, object_hook=lambda d: SimpleNamespace(**d)),
                                   dumps=lambda obj: json.dumps(vars(obj))))
    bind_public_probe(namespace, bmc_probe, monkeypatch)
    namespace['is_virtual_machine'] = lambda: virtual
    source = ROOT / 'kvmagent/kvmagent/plugins/prometheus.py'
    method = source.read_text().split('    def start_prometheus_exporter', 1)[1]
    method = '    def start_prometheus_exporter' + method.split('    def install_colletor', 1)[0]
    exec(compile('if True:\n' + method, str(source), 'exec'), namespace)
    req = {'body': json.dumps({'cmds': [
        {'binaryPath': str(hardware.path / name), 'startupArguments': ''}
        for name in ('ipmi_exporter', 'node_exporter')]})}
    started = time.monotonic()
    response = namespace['start_prometheus_exporter'](None, req)
    assert time.monotonic() - started < 3
    assert json.loads(response)['success'] is True
    assert 'systemctl daemon-reload && systemctl restart node_exporter.service' in commands
    assert ('systemctl daemon-reload && systemctl restart ipmi_exporter.service' in commands) is (not hang and not virtual)
    messages = [call.args[0] for call in namespace['logger'].info.call_args_list]
    if virtual:
        assert any('virtualized environment' in message for message in messages)
        assert not (hardware.path / 'calls').exists()
    elif hang:
        assert any('BMC capability probe failed' in message for message in messages)
        assert not any('virtualized environment' in message for message in messages)
