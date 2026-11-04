# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import unittest

try:
    from importlib import util as importlib_util
except ImportError:
    importlib_util = None

try:
    import imp
except ImportError:
    imp = None


REPOSITORY_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..'))
ANSIBLE_ROOT = os.path.join(REPOSITORY_ROOT, 'kvmagent', 'ansible')
CONTROL_PATH = os.path.join(ANSIBLE_ROOT, 'libvirt_control.py')


class LibvirtControlOrderingTest(unittest.TestCase):

    def _load_control(self):
        self.assertTrue(
            os.path.isfile(CONTROL_PATH),
            'production libvirt selector is missing: %s' % CONTROL_PATH)
        if importlib_util is not None:
            spec = importlib_util.spec_from_file_location(
                'task7_libvirt_control', CONTROL_PATH)
            module = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        return imp.load_source('task7_libvirt_control', CONTROL_PATH)

    def test_active_virtqemud_socket_is_selected(self):
        control = self._load_control()
        observations = [
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'active', 'unit_file': 'enabled'},
            {'unit': 'virtqemud.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'static'},
            {'unit': 'libvirtd.service', 'load': 'not-found',
             'active': 'inactive', 'unit_file': 'disabled'},
        ]
        self.assertEqual(
            'virtqemud.socket', control.select_control_unit(observations))

    def test_active_traditional_and_modular_units_are_ambiguous(self):
        control = self._load_control()
        observations = [
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'active', 'unit_file': 'enabled'},
            {'unit': 'libvirtd.service', 'load': 'loaded',
             'active': 'active', 'unit_file': 'enabled'},
        ]
        with self.assertRaises(control.LibvirtControlAmbiguous):
            control.select_control_unit(observations)

    def test_active_traditional_unit_wins_over_inactive_modular_units(self):
        control = self._load_control()
        observations = [
            {'unit': 'libvirtd.service', 'load': 'loaded',
             'active': 'active', 'unit_file': 'enabled'},
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'enabled'},
            {'unit': 'virtqemud.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'static'},
        ]
        self.assertEqual(
            'libvirtd.service', control.select_control_unit(observations))

    def test_service_only_modular_host_selects_virtqemud_service(self):
        control = self._load_control()
        observations = [
            {'unit': 'libvirtd.service', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
            {'unit': 'virtqemud.socket', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
            {'unit': 'virtqemud.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'static'},
        ]
        self.assertEqual(
            'virtqemud.service', control.select_control_unit(observations))

    def test_inactive_modular_host_prefers_usable_socket(self):
        control = self._load_control()
        observations = [
            {'unit': 'virtqemud.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'static'},
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'enabled-runtime'},
            {'unit': 'libvirtd.service', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
        ]
        self.assertEqual(
            'virtqemud.socket', control.select_control_unit(observations))

    def test_inactive_usable_traditional_and_modular_families_are_ambiguous(self):
        control = self._load_control()
        observations = [
            {'unit': 'libvirtd.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'enabled'},
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'enabled'},
        ]
        with self.assertRaises(control.LibvirtControlAmbiguous):
            control.select_control_unit(observations)

    def test_missing_units_are_unavailable(self):
        control = self._load_control()
        observations = [
            {'unit': 'libvirtd.service', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
            {'unit': 'virtqemud.socket', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
            {'unit': 'virtqemud.service', 'load': 'not-found',
             'active': 'inactive', 'unit_file': ''},
        ]
        with self.assertRaises(control.LibvirtControlUnavailable):
            control.select_control_unit(observations)

    def test_parser_normalizes_three_fixed_unit_rows(self):
        control = self._load_control()
        text = (u' libvirtd.service | loaded | inactive | enabled \n'
                u'virtqemud.socket|loaded|active|enabled-runtime\n'
                u'virtqemud.service|loaded|inactive|static\n')
        self.assertEqual([
            {'unit': 'libvirtd.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'enabled'},
            {'unit': 'virtqemud.socket', 'load': 'loaded',
             'active': 'active', 'unit_file': 'enabled-runtime'},
            {'unit': 'virtqemud.service', 'load': 'loaded',
             'active': 'inactive', 'unit_file': 'static'},
        ], control.parse_unit_observations(text))

    def test_parser_accepts_empty_unit_file_only_for_not_found_unit(self):
        control = self._load_control()
        text = ('libvirtd.service|not-found|inactive|\n'
                'virtqemud.socket|not-found|inactive|\n'
                'virtqemud.service|loaded|active|enabled\n')
        parsed = control.parse_unit_observations(text)
        self.assertEqual('', parsed[0]['unit_file'])
        self.assertEqual('virtqemud.service',
                         control.select_control_unit(parsed))

    def test_active_unit_is_authoritative_with_empty_unit_file_state(self):
        control = self._load_control()
        try:
            parsed = control.parse_unit_observations(
                'libvirtd.service|loaded|active|\n'
                'virtqemud.socket|not-found|inactive|\n'
                'virtqemud.service|not-found|inactive|\n')
        except control.LibvirtControlMalformed as error:
            self.fail('active unit was rejected before selection: %s' % error)

        self.assertEqual('libvirtd.service',
                         control.select_control_unit(parsed))

    def test_parser_rejects_malformed_unknown_duplicate_or_incomplete_rows(self):
        control = self._load_control()
        malformed = [
            'libvirtd.service|loaded|active\n',
            'unknown.service|loaded|active|enabled\n',
            ('libvirtd.service|loaded|active|enabled\n'
             'libvirtd.service|loaded|inactive|enabled\n'),
            'libvirtd.service|loaded||enabled\n',
        ]
        for text in malformed:
            with self.assertRaises(control.LibvirtControlMalformed):
                control.parse_unit_observations(text)

    def test_fixed_unit_template_mapping_rejects_arbitrary_names(self):
        control = self._load_control()
        expected = {
            'libvirtd.service':
                'zstack-kvmagent-libvirtd-ordering.conf',
            'virtqemud.socket':
                'zstack-kvmagent-virtqemud-socket-ordering.conf',
            'virtqemud.service':
                'zstack-kvmagent-virtqemud-service-ordering.conf',
        }
        for unit, filename in expected.items():
            self.assertEqual(filename, control.dropin_filename(unit))
        with self.assertRaises(control.LibvirtControlUnavailable):
            control.dropin_filename('attacker-controlled.service')

    def test_templates_order_only_their_fixed_endpoint(self):
        expected = {
            'zstack-kvmagent-libvirtd-ordering.conf': 'libvirtd.service',
            'zstack-kvmagent-virtqemud-socket-ordering.conf':
                'virtqemud.socket',
            'zstack-kvmagent-virtqemud-service-ordering.conf':
                'virtqemud.service',
        }
        for filename, unit in expected.items():
            path = os.path.join(ANSIBLE_ROOT, filename)
            self.assertTrue(os.path.isfile(path),
                            'fixed ordering template is missing: %s' % path)
            with open(path, 'r') as stream:
                content = stream.read().replace('\r\n', '\n')
            self.assertEqual(
                '[Unit]\nWants=%s\nAfter=%s\n' % (unit, unit), content)
            self.assertNotIn('Requires=', content)

    def test_install_observes_before_copy_and_reloads_after_copy(self):
        control = self._load_control()
        events = []
        observation = (
            'libvirtd.service|not-found|inactive|\n'
            'virtqemud.socket|loaded|active|enabled\n'
            'virtqemud.service|loaded|inactive|static\n')

        def run(command, host, **kwargs):
            events.append(('run', command, dict(kwargs)))
            if 'systemctl show ' in command:
                return True, observation
            return True

        def copy(source, destination, mode, host):
            events.append(('copy', source, destination, mode))
            return True

        selected = control.install_ordering_dropin(
            'files/kvm', object(), run, copy)

        self.assertEqual('virtqemud.socket', selected)
        self.assertEqual('run', events[0][0])
        self.assertIn('systemctl show ', events[0][1])
        self.assertIn('libvirtd.service', events[0][1])
        self.assertIn('virtqemud.socket', events[0][1])
        self.assertIn('virtqemud.service', events[0][1])
        self.assertEqual(
            ('copy',
             os.path.join(
                 'files/kvm',
                 'zstack-kvmagent-virtqemud-socket-ordering.conf'),
             '/etc/systemd/system/zstack-kvmagent.service.d/'
             '10-libvirt-ordering.conf',
             'mode=0644'),
            events[2])
        self.assertEqual(('run', 'systemctl daemon-reload', {}), events[3])

    def test_invalid_observation_preserves_existing_dropin(self):
        control = self._load_control()
        copied = []

        def run(command, host, **kwargs):
            return True, 'libvirtd.service|loaded|active\n'

        def copy(source, destination, mode, host):
            copied.append(destination)

        with self.assertRaises(control.LibvirtControlMalformed):
            control.install_ordering_dropin(
                'files/kvm', object(), run, copy)
        self.assertEqual([], copied)

    def test_ambiguous_observation_preserves_existing_dropin(self):
        control = self._load_control()
        copied = []
        observation = (
            'libvirtd.service|loaded|active|enabled\n'
            'virtqemud.socket|loaded|active|enabled\n'
            'virtqemud.service|loaded|inactive|static\n')

        def run(command, host, **kwargs):
            return True, observation

        def copy(source, destination, mode, host):
            copied.append(destination)

        with self.assertRaises(control.LibvirtControlAmbiguous):
            control.install_ordering_dropin(
                'files/kvm', object(), run, copy)
        self.assertEqual([], copied)

    def test_copy_failure_does_not_reload_systemd(self):
        control = self._load_control()
        commands = []
        observation = (
            'libvirtd.service|loaded|active|enabled\n'
            'virtqemud.socket|not-found|inactive|\n'
            'virtqemud.service|not-found|inactive|\n')

        def run(command, host, **kwargs):
            commands.append(command)
            if 'systemctl show ' in command:
                return True, observation
            return True

        def copy(source, destination, mode, host):
            raise RuntimeError('copy failed')

        with self.assertRaises(RuntimeError):
            control.install_ordering_dropin(
                'files/kvm', object(), run, copy)
        self.assertNotIn('systemctl daemon-reload', commands)

    def test_falsey_copy_result_fails_closed_without_reload(self):
        control = self._load_control()
        commands = []
        existing_dropin = ['original-content']
        observation = (
            'libvirtd.service|loaded|active|enabled\n'
            'virtqemud.socket|not-found|inactive|\n'
            'virtqemud.service|not-found|inactive|\n')

        def run(command, host, **kwargs):
            commands.append(command)
            if 'systemctl show ' in command:
                return True, observation
            return True

        def copy(source, destination, mode, host):
            # The real Ansible helper returns None when it did not publish a
            # replacement.  The prior remote content therefore remains.
            return None

        with self.assertRaises(control.LibvirtControlUnavailable):
            control.install_ordering_dropin(
                'files/kvm', object(), run, copy)

        self.assertEqual(['original-content'], existing_dropin)
        self.assertNotIn('systemctl daemon-reload', commands)


if __name__ == '__main__':
    unittest.main()
