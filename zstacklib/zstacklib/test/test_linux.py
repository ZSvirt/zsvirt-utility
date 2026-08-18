import sys
import types
import unittest


_MISSING = object()


def _install_linux_import_stubs():
    import ctypes
    import zstacklib.utils as utils_pkg

    original_cdll = ctypes.CDLL
    original_modules = {}
    original_attrs = {}

    def remember_module(name):
        if name not in original_modules:
            original_modules[name] = sys.modules.get(name, _MISSING)

    def remember_attr(name):
        if name not in original_attrs:
            original_attrs[name] = getattr(utils_pkg, name, _MISSING)

    remember_attr('linux')
    remember_module('zstacklib.utils.linux')

    class Libc(object):
        def syncfs(self, fd):
            return 0

    ctypes.CDLL = lambda *args, **kwargs: Libc()

    for name in ['fcntl', 'resource', 'netaddr', 'xxhash']:
        remember_module(name)
        sys.modules[name] = types.ModuleType(name)

    remember_module('simplejson')
    sys.modules['simplejson'] = __import__('json')

    for name in ['iproute', 'lock', 'netconfig', 'qemu_img', 'thread', 'xmlobject']:
        remember_attr(name)
        remember_module('zstacklib.utils.%s' % name)
        module = types.ModuleType('zstacklib.utils.%s' % name)
        if name == 'lock':
            module.lock = lambda lock_name: (lambda func: func)
        sys.modules['zstacklib.utils.%s' % name] = module
        setattr(utils_pkg, name, module)

    log = types.ModuleType('zstacklib.utils.log')
    remember_attr('log')
    remember_module('zstacklib.utils.log')

    class Logger(object):
        def warn(self, *args, **kwargs):
            pass

    log.get_logger = lambda name: Logger()
    sys.modules['zstacklib.utils.log'] = log
    utils_pkg.log = log

    shell = types.ModuleType('zstacklib.utils.shell')
    remember_attr('shell')
    remember_module('zstacklib.utils.shell')
    shell.run_without_log = lambda *args, **kwargs: 0
    sys.modules['zstacklib.utils.shell'] = shell
    utils_pkg.shell = shell

    def restore():
        ctypes.CDLL = original_cdll
        for name, module in original_modules.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        for name, value in original_attrs.items():
            if value is _MISSING:
                if hasattr(utils_pkg, name):
                    delattr(utils_pkg, name)
            else:
                setattr(utils_pkg, name, value)

    return restore


def _import_linux_with_stubs():
    restore = _install_linux_import_stubs()
    try:
        from zstacklib.utils import linux
        return linux
    finally:
        restore()


linux = _import_linux_with_stubs()


class Test(unittest.TestCase):
    def test_get_process_start_time_returns_none_when_stat_disappears(self):
        original_exists = linux.os.path.exists
        original_open = getattr(linux, 'open', None)

        def fake_exists(path):
            return path == '/proc/123/stat'

        def fake_open(path, mode='r'):
            raise IOError('gone')

        try:
            linux.os.path.exists = fake_exists
            linux.open = fake_open
            self.assertIsNone(linux.get_process_start_time(123))
        finally:
            linux.os.path.exists = original_exists
            if original_open is None:
                del linux.open
            else:
                linux.open = original_open

    def test_linux_import_stubs_do_not_pollute_import_cache(self):
        import zstacklib.utils as utils_pkg

        thread_module = sys.modules.get('zstacklib.utils.thread')
        linux_module = sys.modules.get('zstacklib.utils.linux')
        self.assertFalse(linux_module is linux)
        self.assertFalse(getattr(utils_pkg, 'linux', None) is linux)
        self.assertFalse(thread_module is getattr(linux, 'thread', None))
        self.assertFalse(getattr(utils_pkg, 'thread', None) is getattr(linux, 'thread', None))

        real_thread = types.ModuleType('zstacklib.utils.thread')

        class ThreadFacade(object):
            pass

        real_thread.ThreadFacade = ThreadFacade
        original_thread_module = sys.modules.get('zstacklib.utils.thread', _MISSING)
        original_thread_attr = getattr(utils_pkg, 'thread', _MISSING)
        try:
            sys.modules['zstacklib.utils.thread'] = real_thread
            utils_pkg.thread = real_thread
            from zstacklib.utils.thread import ThreadFacade as imported
            self.assertIs(imported, ThreadFacade)
        finally:
            if original_thread_module is _MISSING:
                sys.modules.pop('zstacklib.utils.thread', None)
            else:
                sys.modules['zstacklib.utils.thread'] = original_thread_module
            if original_thread_attr is _MISSING:
                if hasattr(utils_pkg, 'thread'):
                    delattr(utils_pkg, 'thread')
            else:
                utils_pkg.thread = original_thread_attr


if __name__ == "__main__":
    unittest.main()
