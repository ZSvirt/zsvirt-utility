"""Render the production SharedBlock LVM config without accessing a host."""
import difflib
import logging
import os
import re
import unittest

from test_lock_recovery_helpers import Mock, identity, load_definitions


class SharedBlockLvmConfigTest(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.linux = Mock()
        self.linux.retry = lambda *args, **kwargs: identity
        self.linux.read_file.side_effect = self.files.get
        self.linux.write_file.side_effect = self.write_file
        self.linux.get_hostname.return_value = 'test-host'
        self.bash = Mock()
        # lvmconfig --type default for a build with notify-dbus enabled.
        self.bash.bash_errorout.return_value = '''global {
    notify_dbus=1
    use_lvmlockd=0
    use_lvmetad=0
}
local {
    host_id=0
}
activation {
    udev_rules=1
    udev_sync=1
}
devices {
    filter=[ "a|.*|" ]
}
'''
        self.changed = Mock()
        self.filter_config = Mock(side_effect=self.apply_filter)
        self.ns = load_definitions('lvm.py', {'get_lvm_default_config', 'config_lvm'},
            dict(linux=self.linux, bash=self.bash, re=re, os=os, difflib=difflib,
                 logger=logging.getLogger(__name__), shell=Mock(),
                 backup_lvm_config=Mock(), config_lvm_filter=self.filter_config,
                 modify_sanlock_config=Mock(), report_config_changed=self.changed,
                 LVM_CONFIG_TMP_FILE='/etc/lvm/zstack.conf',
                 LVM_CONFIG_FILE='/etc/lvm/lvm.conf',
                 LVM_LOCAL_CONFIG_FILE='/etc/lvm/lvmlocal.conf'))

    def write_file(self, path, content, **kwargs):
        self.files[path] = content

    def apply_filter(self, files, preserve_disks):
        self.assertEqual(['zstack.conf'], files)
        self.assertEqual(['/dev/mapper/mpatha'], preserve_disks)
        path = '/etc/lvm/zstack.conf'
        self.files[path] = self.files[path].replace(
            'filter=[ "a|.*|" ]', 'filter=[ "a|/dev/mapper/mpatha|", "r|.*|" ]')

    def configure(self, os_type='rhel', enable_lvmetad=True):
        self.ns['config_lvm'](36, ['/dev/mapper/mpatha'], 'vg-uuid', 'host-uuid',
                              40, os_type, enable_lvmetad)

    def test_rendered_configs_disable_dbus_and_preserve_host_and_device_settings(self):
        self.configure()
        for path in ('/etc/lvm/lvm.conf', '/etc/lvm/lvmlocal.conf'):
            rendered = self.files[path]
            self.assertIn('notify_dbus=0', rendered)
            self.assertNotIn('notify_dbus=1', rendered)
            self.assertIn('use_lvmlockd=1', rendered)
            self.assertIn('use_lvmetad=1', rendered)
            self.assertIn('host_id=36', rendered)
            self.assertIn('filter=[ "a|/dev/mapper/mpatha|", "r|.*|" ]', rendered)
        self.changed.assert_called_once_with()

    def test_reconnect_keeps_dbus_disabled_without_reporting_another_change(self):
        self.configure()
        saved = dict(self.files)
        self.changed.reset_mock()
        self.configure()
        self.assertEqual(saved, self.files)
        self.assertIn('notify_dbus=0', self.files['/etc/lvm/lvm.conf'])
        self.changed.assert_not_called()

    def test_debian_configuration_still_disables_udev_and_optional_lvmetad(self):
        self.configure(os_type='debian', enable_lvmetad=False)
        rendered = self.files['/etc/lvm/lvmlocal.conf']
        for setting in ('notify_dbus=0', 'udev_rules=0', 'udev_sync=0', 'use_lvmetad=0'):
            self.assertIn(setting, rendered)


if __name__ == '__main__':
    unittest.main()
