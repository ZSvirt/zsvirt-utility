import base64
import json
import mock
import os
import unittest


def _load_function_source(function_name):
    test_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_path = os.path.abspath(os.path.join(test_dir, '..', 'plugins', 'vm_plugin.py'))
    with open(plugin_path, 'r') as fd:
        lines = fd.readlines()

    start = None
    for index, line in enumerate(lines):
        if line.startswith('def %s(' % function_name):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found' % function_name)

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith('def ') or line.startswith('class '):
            end = index
            break

    return ''.join(lines[start:end])


class Logger(object):
    def __init__(self):
        self.messages = []

    def debug(self, message):
        self.messages.append(message)


class QmpStub(object):
    def __init__(self):
        self.get_block_job_ids_kwargs = None

    def get_block_job_ids(self, vm, **kwargs):
        self.get_block_job_ids_kwargs = kwargs
        raise Exception('query failed')

    def do_yank(self, vm):
        return True


class TimeStub(object):
    def sleep(self, interval):
        raise AssertionError('sleep should not run after qmp query exception')


def _load_vm_block_job_functions():
    namespace = {
        'logger': Logger(),
        'qmp': QmpStub(),
        'time': TimeStub(),
    }
    exec _load_function_source('vm_block_job_yank') in namespace
    namespace['vm_block_job_yank_actual'] = namespace['vm_block_job_yank']
    namespace['vm_block_job_yank_stub_called'] = False

    def vm_block_job_yank_stub(vm):
        namespace['vm_block_job_yank_stub_called'] = True

    namespace['vm_block_job_yank'] = vm_block_job_yank_stub
    exec _load_function_source('vm_block_job_cancel') in namespace
    return namespace


class Test(unittest.TestCase):
    def test_vm_block_job_cancel_catches_qmp_query_exception_without_changing_call(self):
        namespace = _load_vm_block_job_functions()

        namespace['vm_block_job_cancel']('vm-uuid')

        self.assertEqual({}, namespace['qmp'].get_block_job_ids_kwargs)
        self.assertFalse(namespace['vm_block_job_yank_stub_called'])

    def test_vm_block_job_yank_catches_qmp_query_exception_without_changing_call(self):
        namespace = _load_vm_block_job_functions()

        namespace['vm_block_job_yank_actual']('vm-uuid')

        self.assertEqual({}, namespace['qmp'].get_block_job_ids_kwargs)

    def test_windows_guest_tools_state_requires_running_service(self):
        from kvmagent.plugins import vm_plugin

        plugin = vm_plugin.VmPlugin()
        vm = mock.Mock(state=vm_plugin.Vm.VM_STATE_RUNNING)
        with mock.patch.object(vm_plugin, 'get_vm_by_uuid_no_retry', return_value=vm), \
                mock.patch.object(vm_plugin, 'is_qga_connected', return_value=True), \
                mock.patch.object(vm_plugin, 'VmQga') as qga, \
                mock.patch.object(vm_plugin.bash, 'bash_roe') as bash_roe:
            for service_state, expected in [('NotInstalled', 'NotRunning'),
                                            ('Stopped', 'NotRunning'),
                                            ('Running', 'Running')]:
                rsp = vm_plugin.GetVmGuestToolsInfoRsp()
                qga.return_value.guest_exec_powershell.return_value = (0, service_state + '\r\n')
                bash_roe.side_effect = [
                    (0, json.dumps({'return': 7}), ''),
                    (0, json.dumps({'return': {
                        'buf-b64': base64.b64encode(b'1.6.0').decode('ascii'),
                    }}), ''),
                    (0, '', ''),
                ]

                plugin.get_vm_guest_tools_info_for_windows_guest('vm-uuid', rsp)

                self.assertTrue(rsp.success, service_state)
                self.assertEqual(expected, rsp.status, service_state)
                self.assertEqual(b'1.6.0', rsp.version)


if __name__ == "__main__":
    unittest.main()
