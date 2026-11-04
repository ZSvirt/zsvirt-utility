"""Exercise real persistence function bodies with no libvirt connection.

Extract only the Python 2/3-compatible functions under test, because importing
the complete plugin initializes host services. Device operations are mocks;
this suite does not replace the multi-storage libvirt integration Cases.
"""
import os
import re
import textwrap
import traceback
import unittest
from contextlib import contextmanager
try:
    from unittest.mock import Mock, call
except ImportError:
    from mock import Mock, call


def load_function(name, namespace, parent=None):
    path = os.environ.get('ZSV_VM_TEST_SOURCE_FILE') or os.path.join(
        os.path.dirname(__file__), '..', 'plugins', 'vm_plugin.py')
    with open(path) as source:
        text = source.read()
    if parent:
        start = text.index('    def %s(' % parent)
        end = text.find('\n    def ', start + 1)
        text = text[start:] if end < 0 else text[start:end]
    match = re.search(r'^( *)def ' + re.escape(name) + r'\(', text, re.M)
    if match is None:
        raise AssertionError('production function is missing: %s' % name)
    indent = len(match.group(1))
    lines = text[match.start():].splitlines(True)
    body = [lines[0]]
    for line in lines[1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    exec(compile(textwrap.dedent(''.join(body)), path, 'exec'), namespace)
    return namespace[name]


@contextmanager
def ignore_exception(*args):
    yield


class VmPersistentDefinitionHelpersTest(unittest.TestCase):
    def operation(self, name, persistent=True, config_error=False, live_error=False):
        domain = Mock()
        domain.isPersistent.return_value = persistent
        operation = getattr(domain, name + 'DeviceFlags')
        def execute(xml, flag):
            if (flag == 1 and live_error) or (flag == 2 and config_error):
                raise RuntimeError('libvirt operation failed')
        operation.side_effect = execute
        vm = Mock(uuid='vm1', domain=domain)
        current = Mock()
        attached = (name == 'attach') != live_error
        current._get_target_disk.return_value = (object() if attached else None, 'vdb')
        linux = Mock()
        linux.wait_callback_success.side_effect = lambda f, arg, *rest: f(arg)
        namespace = dict(self=vm, volume=Mock(volumeUuid='vol1', installPath='/dev/vg/lv'),
                         xml='<disk/>', xmlstr='<disk/>', linux=linux,
                         get_vm_by_uuid=lambda _: current, logger=Mock(), traceback=traceback,
                         misc=Mock(ignore_exception=ignore_exception),
                         libvirt=Mock(VIR_DOMAIN_AFFECT_LIVE=1, VIR_DOMAIN_AFFECT_CONFIG=2,
                                      libvirtError=RuntimeError))
        fn = load_function(name, namespace, '_' + name + '_data_volume')
        return fn, operation, namespace['logger']

    def test_live_success_updates_persistent_config_in_order(self):
        for name in ('attach', 'detach'):
            fn, operation, logger = self.operation(name)
            fn()
            self.assertEqual([call('<disk/>', 1), call('<disk/>', 2)], operation.call_args_list)
            logger.warn.assert_not_called()

    def test_transient_vm_only_updates_live_device(self):
        for name in ('attach', 'detach'):
            fn, operation, _ = self.operation(name, persistent=False)
            fn()
            self.assertEqual([call('<disk/>', 1)], operation.call_args_list)

    def test_config_failure_does_not_fail_successful_live_operation(self):
        for name in ('attach', 'detach'):
            fn, operation, logger = self.operation(name, config_error=True)
            fn()
            self.assertEqual([call('<disk/>', 1), call('<disk/>', 2)], operation.call_args_list)
            self.assertEqual(1, logger.warn.call_count)

    def test_live_failure_does_not_update_persistent_config(self):
        for name in ('attach', 'detach'):
            fn, operation, _ = self.operation(name, live_error=True)
            with self.assertRaises(RuntimeError):
                fn()
            self.assertEqual([call('<disk/>', 1)], operation.call_args_list)

    def test_startup_uses_live_xml_and_continues_after_one_vm_fails(self):
        conn = Mock()
        conn.defineXML.side_effect = [RuntimeError('first VM failed'), None]
        vms = [Mock(uuid='vm1', domain_xml='<live-one/>'), Mock(uuid='vm2', domain_xml='<live-two/>')]
        logger = Mock()
        sync = load_function('sync_vm_persistent_definitions', dict(
            get_running_vms=lambda: vms, logger=logger, traceback=traceback,
            LibvirtAutoReconnect=lambda func: lambda: func(conn)))
        sync()
        self.assertEqual([call('<live-one/>'), call('<live-two/>')], conn.defineXML.call_args_list)
        self.assertEqual(1, logger.warn.call_count)
        self.assertEqual(1, logger.info.call_count)


if __name__ == '__main__':
    unittest.main()
