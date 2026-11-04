"""Exercise the actual shared-block HA parameter-file update path."""
import ast
import json
import os
from pathlib import Path
import threading
import types

import pytest


@pytest.fixture
def params(tmp_path):
    root = Path(__file__).resolve().parents[3]
    path = root / 'kvmagent/kvmagent/plugins/ha_plugin.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in
                 ('create_shareblock_vm_ha_params', 'update_shareblock_vm_ha_params')]
    checker = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == 'SblkHealthChecker')
    checker.bases = []
    checker.body = [node for node in checker.body
                    if isinstance(node, ast.FunctionDef) and node.name in
                    ('addvg', 'delvg', 'update_vm_ha_params')]
    output = tmp_path / 'shareBlockVmHaParams'
    namespace = dict(
        os=os, json=json, WRITE_SHAREBLOCKVMHAPARAMS_LOCK=threading.Lock(),
        SHAREBLOCK_VM_HA_PARAMS_PATH=str(output),
        jsonobject=types.SimpleNamespace(
            dumps=lambda obj: json.dumps(obj, default=lambda value: vars(value))))
    exec(compile(ast.Module(body=functions + [checker], type_ignores=[]),
                 str(path), 'exec'), namespace)
    instance = namespace['SblkHealthChecker']()
    for field in ('all_vgs', 'fencer_created_time', 'vg_failures',
                  'fencer_fire_cnt', 'fired_vgs'):
        setattr(instance, field, {})
    return instance, namespace['create_shareblock_vm_ha_params'], output


def command(vg, interval, attempts, timeout):
    return types.SimpleNamespace(vgUuid=vg, vgUuids=[vg], hostUuid='host',
                                 interval=interval, maxAttempts=attempts,
                                 storageCheckerTimeout=timeout)


def test_reconfiguration_refreshes_all_agent_timing_parameters(params):
    checker, create, output = params
    initial = command('vg1', 5, 6, 60)
    create(initial)
    checker.addvg(1, initial)
    updated = command('vg1', 2, 3, 10)
    create(updated)
    checker.addvg(2, updated)
    saved = json.loads(output.read_text())
    assert (saved['interval'], saved['maxAttempts'], saved['storageCheckerTimeout']) == (2, 3, 10)
    assert checker.all_vgs['vg1'] is updated


def test_parameter_merge_preserves_existing_fields_and_all_vgs(params):
    checker, create, output = params
    first = command('vg1', 5, 6, 60)
    first.existingOnly = 'keep'
    create(first)
    checker.addvg(1, first)
    checker.addvg(2, command('vg2', 2, 3, 10))
    saved = json.loads(output.read_text())
    assert saved['existingOnly'] == 'keep'
    assert set(saved['vgUuids']) == {'vg1', 'vg2'}
    assert saved['interval'] == 2


def test_removing_vg_preserves_latest_parameters(params):
    checker, create, output = params
    first = command('vg1', 5, 6, 60)
    create(first)
    checker.addvg(1, first)
    checker.addvg(2, command('vg2', 2, 3, 10))
    checker.delvg('vg1')
    saved = json.loads(output.read_text())
    assert saved['vgUuids'] == ['vg2']
    assert saved['interval'] == 2
