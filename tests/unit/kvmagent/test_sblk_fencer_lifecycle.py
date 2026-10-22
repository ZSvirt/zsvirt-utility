"""Execute actual fencing/liveness methods with storage, network and VM effects isolated."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest


SOURCE = Path(__file__).resolve().parents[3] / 'kvmagent/kvmagent/plugins/ha_plugin.py'


def load_methods(names, namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == set(names)
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


def fixture_plugin(no_way=True, network_ok=True, next_generation=1):
    events = []
    logger = NS(**{name: lambda *args: events.append(('log', args))
                   for name in ('debug', 'info', 'warn', 'error')})
    result = NS(no_way=no_way, failed=not no_way, error='probe failure')
    created_reads = []

    def generation(vg):
        created_reads.append(vg)
        return 1 if len(created_reads) == 1 else next_generation

    checker = NS(all_vgs={'vg': object()}, fired_vgs={}, fail_if_no_path=False,
                 get_created_time=generation, write_fencer_heartbeat=lambda: {'vg': result},
                 get_fencer_fire_cnt=lambda vg: 0, inc_fencer_fire_cnt=lambda vg: None,
                 get_vg_fencer_cmd=lambda vg: NS(strategy='Force', storageCheckerTimeout=5),
                 firevg=lambda vg: checker.fired_vgs.update({vg: 1000}))
    plugin = NS(sblk_health_checker=checker, fencer_workers={
        'vg': {'generation': next_generation, 'expected': {'vg'}, 'alive': {'vg'}}},
        fencer_lock=threading.RLock(), fencer_fire_timestamp={}, storage_status={},
        STORAGE_CONNECTED='Connected', STORAGE_DISCONNECTED='Disconnected')
    env = {'logger': logger, 'time': NS(time=lambda: 1000),
           'thread': NS(AsyncThread=lambda fn: fn),
           'traceback': NS(format_exc=lambda: 'unexpected traceback'),
           'lvm': NS(get_running_vm_root_volume_on_vg=lambda vg: [NS(uuid='vm', volumes=[])],
                     remove_partial_lv_dm=lambda vg: None, check_vg_status=lambda *args: (True, '')),
           'shell': NS(run=lambda cmd: events.append(('pause', cmd))),
           'linux': NS(), 'last_multipath_run': 1000,
           'signal_sharedblock_vms': lambda vms, strategy: (events.append(('kill', strategy)) or ['vm'], vms),
           'clean_network_config': lambda uuids: None}
    names = ('do_heartbeat_on_sharedblock', 'mark_fencer_dead', 'mark_fencer_alive',
             '_mark_fencer_alive_locked', 'get_alive_fencer_ps_uuids')
    load_methods(names, env)
    for name in names[1:]:
        setattr(plugin, name, lambda *args, _name=name: env[_name](plugin, *args))
    plugin.report_storage_status = lambda *args, **kwargs: events.append(('storage', args))
    plugin.report_self_fencer_triggered = lambda *args: events.append(('fenced', args))

    def notify(vgs):
        events.append(('notify-ready', plugin.get_alive_fencer_ps_uuids()))
        if not network_ok:
            raise RuntimeError('network down')

    plugin.report_self_fencer_state_changed_to_mn = notify
    return plugin, env, events


@pytest.mark.parametrize('network_ok', [True, False])
def test_two_unknown_channels_revoke_ready_before_notification(network_ok):
    plugin, env, events = fixture_plugin(network_ok=network_ok)
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert ('notify-ready', []) in events
    assert plugin.get_alive_fencer_ps_uuids() == []
    assert not any(event[0] == 'kill' for event in events)
    assert any(event[0] == 'pause' for event in events) is (not network_ok)
    assert bool(plugin.sblk_health_checker.fired_vgs) is (not network_ok)
    abnormal = [event for event in events if 'stopped abnormally' in str(event)]
    assert not abnormal, abnormal


def test_known_storage_failure_still_uses_target_batch_fencer():
    plugin, env, events = fixture_plugin(no_way=False)
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert ('kill', 'Force') in events
    assert plugin.get_alive_fencer_ps_uuids() == ['vg']
    assert not any(event[0] in ('pause', 'notify-ready') for event in events)
    assert plugin.sblk_health_checker.fired_vgs


def test_stale_probe_does_not_change_new_worker_generation_or_fence():
    plugin, env, events = fixture_plugin(next_generation=2)
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert plugin.get_alive_fencer_ps_uuids() == ['vg']
    assert not any(event[0] in ('pause', 'kill', 'notify-ready') for event in events)


def test_recovered_backend_restores_worker_readiness():
    plugin, env, events = fixture_plugin()
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert plugin.get_alive_fencer_ps_uuids() == []
    plugin.sblk_health_checker.write_fencer_heartbeat = lambda: {'vg': NS(no_way=False, failed=False)}
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert plugin.get_alive_fencer_ps_uuids() == ['vg']


def test_recovery_after_pause_clears_fired_state_and_retry_timestamp():
    plugin, env, events = fixture_plugin(network_ok=False)
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert plugin.sblk_health_checker.fired_vgs
    assert plugin.fencer_fire_timestamp
    plugin.sblk_health_checker.write_fencer_heartbeat = lambda: {'vg': NS(no_way=False, failed=False)}
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert plugin.get_alive_fencer_ps_uuids() == ['vg']
    assert not plugin.sblk_health_checker.fired_vgs
    assert not plugin.fencer_fire_timestamp


@pytest.mark.parametrize('transition', ['recover', 'cancel', 'replace', 'recover-and-fail-again'])
def test_waiting_notification_cannot_pause_a_recovered_or_replaced_vg(transition):
    plugin, env, events = fixture_plugin(network_ok=False)

    def notify(vgs):
        if transition in ('recover', 'recover-and-fail-again'):
            plugin.sblk_health_checker.write_fencer_heartbeat = lambda: {'vg': NS(no_way=False, failed=False)}
            env['do_heartbeat_on_sharedblock'](plugin, None)
            if transition == 'recover-and-fail-again':
                # Record a new outage while keeping its async work queued.
                env['thread'].AsyncThread = lambda fn: lambda *args: None
                plugin.sblk_health_checker.write_fencer_heartbeat = lambda: {'vg': NS(no_way=True, failed=None, error='new outage')}
                env['do_heartbeat_on_sharedblock'](plugin, None)
        elif transition == 'cancel':
            plugin.fencer_workers.pop('vg')
            plugin.sblk_health_checker.all_vgs.clear()
        else:
            plugin.fencer_workers['vg'] = {'generation': 2, 'expected': {'vg'}, 'alive': {'vg'}}
            plugin.sblk_health_checker.get_created_time = lambda vg: 2
        raise RuntimeError('notification timed out after transition')

    plugin.report_self_fencer_state_changed_to_mn = notify
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert not any(event[0] == 'pause' for event in events)
    assert not plugin.sblk_health_checker.fired_vgs


def test_continuous_unknown_round_does_not_invalidate_pending_protection():
    plugin, env, events = fixture_plugin(network_ok=False)

    def notify(vgs):
        env['thread'].AsyncThread = lambda fn: lambda *args: None
        env['do_heartbeat_on_sharedblock'](plugin, None)
        raise RuntimeError('notification failed, same outage persists')

    plugin.report_self_fencer_state_changed_to_mn = notify
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert any(event[0] == 'pause' for event in events)


def test_recovery_during_vm_lookup_invalidates_pending_pause():
    plugin, env, events = fixture_plugin(network_ok=False)

    def running_vms(vg):
        plugin.sblk_health_checker.write_fencer_heartbeat = lambda: {'vg': NS(no_way=False, failed=False)}
        env['do_heartbeat_on_sharedblock'](plugin, None)
        return [NS(uuid='vm', volumes=[])]

    env['lvm'].get_running_vm_root_volume_on_vg = running_vms
    env['do_heartbeat_on_sharedblock'](plugin, None)
    assert not any(event[0] == 'pause' for event in events)
    assert not plugin.sblk_health_checker.fired_vgs
