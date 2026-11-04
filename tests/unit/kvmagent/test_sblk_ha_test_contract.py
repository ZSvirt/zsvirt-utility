"""Run the HA integration fixture against its actual checker without devices."""
import ast
import json
import os
import types
from unittest import TestCase, mock

import pytest

from test_sblk_health_check import ROOT, VG, _load_health_check


@pytest.fixture
def ha_case():
    path = os.path.join(ROOT, 'kvmagent', 'kvmagent', 'test',
                        'shareblock_testsuite', 'test_sharedblock_ha.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'TestSharedBlockPlugin')
    cls.bases = [ast.Name(id='TestCase', ctx=ast.Load())]
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name != 'test_sharedblock_ha']
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id.startswith(
                     ('LOCKSPACE_', 'SELF_FENCER_', 'STORAGE_STATUS_'))
                         for target in node.targets)]

    def external_shell(command, *args, **kwargs):
        raise RuntimeError('unmocked external command: ' + command)

    def external_post(uri, body=None, headers=None, fail_soon=False, print_curl=False):
        return 'forwarded post'

    namespace = dict(TestCase=TestCase, mock=mock, vgUuid=VG,
        http=types.SimpleNamespace(json_dump_get=lambda *a: '', json_dump_post=external_post),
        shell=types.SimpleNamespace(call=external_shell),
        linux=types.SimpleNamespace(get_current_timestamp=lambda: 1000),
        jsonobject=types.SimpleNamespace(dumps=json.dumps), lvm=types.SimpleNamespace())
    module = ast.fix_missing_locations(ast.Module(body=constants + [cls], type_ignores=[]))
    exec(compile(module, path, 'exec'), namespace)
    case_class = namespace['TestSharedBlockPlugin']
    case_class.setUpClass()
    case = case_class()
    yield case, namespace
    case.doCleanups()


@pytest.mark.parametrize('heartbeat,unknown,failed', [
    ('success', False, False), ('fail', False, True), ('no_way', True, None)])
def test_integration_heartbeat_fixture_reaches_actual_checker(ha_case, heartbeat, unknown, failed):
    case, namespace = ha_case
    case.mock_zsblk_agent()
    case.zsblk_agent_heart_result = heartbeat
    checker, _ = _load_health_check(0)
    checker.health_check_interval = checker.storage_timeout = 5
    checker.max_failure = 6
    checker._do_health_check_vg_by_zsblkagent.__globals__['shell'] = namespace['shell']
    result = checker._do_health_check_vg_by_zsblkagent([VG])[VG]
    assert result.no_way is unknown
    assert result.failed is failed


def test_heartbeat_fixture_does_not_swallow_unrelated_shell_commands(ha_case):
    case, namespace = ha_case
    case.mock_zsblk_agent()
    with pytest.raises(RuntimeError, match='unmocked external command: lvs'):
        namespace['shell'].call('lvs')


def test_management_fixture_handles_storage_report_and_forwards_other_posts(ha_case):
    case, namespace = ha_case
    case.mock_management_work()
    post = namespace['http'].json_dump_post
    assert post('xx', {}, {'commandpath': '/kvm/reportstoragestatus'}, True) == ''
    assert post('xx', {}, {'commandpath': '/other'}, True) == 'forwarded post'


def test_management_fixture_preserves_unreachable_mn_signal(ha_case):
    case, namespace = ha_case
    case.mock_management_work()
    post = namespace['http'].json_dump_post
    path = {'commandpath': '/kvm/reportselffencerstatechanged'}
    assert post('xx', {}, path, True) == ''
    case.management_network_ok = False
    with pytest.raises(Exception, match='cannot report self fencer state changed'):
        post('xx', {}, path, True)


class Clock(object):
    now = 0.0

    def time(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def test_lock_settle_waits_for_lockspace_and_cleanup_then_two_stable_checks(ha_case):
    case, namespace = ha_case
    clock = Clock()
    replies = iter(['lvm_vg ADD', '', '', 'lvmlockctl --drop', '', '', '', ''])
    namespace.update(time=clock,
                     bash=types.SimpleNamespace(bash_ro=lambda _: (0, next(replies))))
    case.wait_sharedblock_lock_settled()
    assert clock.now == 1.5
    with pytest.raises(StopIteration):
        next(replies)


def test_lock_settle_fails_instead_of_reconnecting_while_cleanup_is_pending(ha_case):
    case, namespace = ha_case
    clock = Clock()
    namespace.update(time=clock,
        bash=types.SimpleNamespace(bash_ro=lambda _: (0, 'pending')))
    with pytest.raises(AssertionError, match='still changing'):
        case.wait_sharedblock_lock_settled()
    assert clock.now == 15


def test_per_case_reset_removes_previous_outage_without_clearing_other_vgs(ha_case):
    case, namespace = ha_case
    fire_counts, failures = {VG: 4, 'other': 2}, {VG: 6, 'other': 3}
    checker = types.SimpleNamespace(fired_vgs={VG: 1, 'other': 1},
        reset_fencer_fire_cnt=lambda vg: fire_counts.pop(vg, None),
        reset_vg_failure_cnt=lambda vg: failures.pop(vg, None))
    plugin = types.SimpleNamespace(fencer_fire_timestamp={VG: 10, 'other': 20},
        storage_status={VG: 'Disconnected', 'other': 'Connected'}, sblk_health_checker=checker)
    namespace['ha_utils'] = types.SimpleNamespace(HA_PLUGIN=plugin)
    case.fencer_triggered = 1
    case.zsblk_agent_heart_result = 'fail'
    case.management_network_ok = False
    case.reset_sharedblock_fencer_state()
    assert (case.fencer_triggered, case.zsblk_agent_heart_result, case.management_network_ok) == (0, 'success', True)
    assert plugin.fencer_fire_timestamp == {'other': 20}
    assert plugin.storage_status == {'other': 'Connected'}
    assert checker.fired_vgs == {'other': 1}
    assert fire_counts == {'other': 2}
    assert failures == {'other': 3}
