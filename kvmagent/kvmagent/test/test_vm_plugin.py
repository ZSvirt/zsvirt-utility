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


if __name__ == "__main__":
    unittest.main()
