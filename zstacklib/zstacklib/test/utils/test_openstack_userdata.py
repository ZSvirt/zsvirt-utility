import email
import unittest

from zstacklib.utils import openstack_userdata


class TestOpenStackUserdata(unittest.TestCase):
    password_config = '#cloud-config\nchpasswd:\n  list: |\n    root:Test:pass!\n  expire: False\n'
    xml = '<script>echo MARKER > C:\\marker.txt</script>'

    def test_password_becomes_native_metadata_without_losing_cloud_config(self):
        self.assertEqual(('Test:pass!', self.password_config), openstack_userdata.prepare([self.password_config]))

    def assert_script_and_config(self, password, body, script):
        self.assertEqual('Test:pass!', password)
        parts = email.message_from_string(body).get_payload()
        self.assertEqual('text/x-shellscript', parts[0].get_content_type())
        self.assertEqual(script.strip().encode(), parts[0].get_payload(decode=True).strip())
        self.assertEqual('text/cloud-config', parts[1].get_content_type())
        self.assertIn(b'root:Test:pass!', parts[1].get_payload(decode=True))

    def test_qa_mixed_xml_and_yaml(self):
        data = self.xml + '\n  chpasswd:\n    list: |\n      root:Test:pass!\n    expire: False\n'
        password, body = openstack_userdata.prepare([data])
        self.assert_script_and_config(password, body, self.xml)

    def test_separate_script_and_password(self):
        password, body = openstack_userdata.prepare([self.xml, self.password_config])
        self.assert_script_and_config(password, body, self.xml)

    def test_powershell_and_password(self):
        script = '#ps1_sysnative\nWrite-Output MARKER'
        password, body = openstack_userdata.prepare([script, self.password_config])
        self.assert_script_and_config(password, body, script)

    def test_single_unrelated_userdata_remains_identical(self):
        for body in (self.xml, '#cloud-config\nwrite_files: []\n', 'unrecognized data', '#ps1_sysnative\nWrite-Output chpasswd:'):
            self.assertEqual((None, body), openstack_userdata.prepare([body]))

    def test_encrypted_or_named_account_is_not_retargeted(self):
        for body in ('#cloud-config\nchpasswd:\n  list: "alice:secret"\n',
                     '#cloud-config\nchpasswd:\n  list: "root:hash"\n  encrypted: true\n'):
            self.assertEqual((None, body), openstack_userdata.prepare([body]))

    def test_malformed_or_object_yaml_is_not_loaded(self):
        for body in ('#cloud-config\nchpasswd: [', '#cloud-config\n!!python/object:os.system {}', '#cloud-config\n- root:secret'):
            self.assertEqual((None, body), openstack_userdata.prepare([body]))

    def test_last_password_wins_without_changing_source(self):
        inputs = [self.password_config, self.password_config.replace('Test:pass!', 'Last!')]
        original = list(inputs)
        password, body = openstack_userdata.prepare(inputs)
        self.assertEqual('Last!', password)
        self.assertEqual(2, len(email.message_from_string(body).get_payload()))
        self.assertEqual(original, inputs)

    def test_other_config_and_accounts_are_preserved(self):
        body = self.password_config.replace('    root:Test:pass!', '    root:Test:pass!\n    alice:Another!') + 'write_files: []\n'
        password, remaining = openstack_userdata.prepare([body])
        self.assertEqual('Test:pass!', password)
        self.assertIn('alice:Another!', remaining)
        self.assertIn('write_files:', remaining)
        self.assertIn('root:Test:pass!', remaining)

    def test_multipart_types_and_payloads_are_preserved(self):
        explicit = 'Content-Type: application/octet-stream\nContent-Transfer-Encoding: base64\nContent-Disposition: attachment; filename="file.bin"\n\nAP8=\n'
        password, body = openstack_userdata.prepare([self.xml, self.password_config, explicit])
        self.assertEqual('Test:pass!', password)
        parts = email.message_from_string(body).get_payload()
        self.assertEqual('text/x-shellscript', parts[0].get_content_type())
        self.assertEqual(self.xml.encode(), parts[0].get_payload(decode=True))
        self.assertEqual('part-000', parts[0].get_filename())
        self.assertEqual('text/cloud-config', parts[1].get_content_type())
        self.assertEqual('application/octet-stream', parts[2].get_content_type())
        self.assertEqual(b'\x00\xff', parts[2].get_payload(decode=True))
        self.assertEqual('file.bin', parts[2].get_filename())

    def test_existing_multipart_password(self):
        raw = 'Content-Type: multipart/mixed; boundary="boundary"\n\n--boundary\nContent-Type: text/cloud-config\n\n' + self.password_config + '\n--boundary--\n'
        password, body = openstack_userdata.prepare([raw])
        self.assertEqual('Test:pass!', password)
        self.assertEqual(raw, body)

    def test_explicit_cloud_config_headers_are_preserved(self):
        raw = ('Content-Type: text/cloud-config; charset="utf-8"\n'
               'Content-Disposition: attachment; filename="custom.yaml"\n'
               'X-Merge-Type: dict(recurse_array)+list(append)\n'
               'X-Custom: keep\n\n' + self.password_config)
        self.assertEqual(('Test:pass!', raw), openstack_userdata.prepare([raw]))
        password, body = openstack_userdata.prepare([self.xml, raw])
        self.assertEqual('Test:pass!', password)
        part = email.message_from_string(body).get_payload()[1]
        original = email.message_from_string(raw)
        self.assertEqual(original.items(), part.items())
        self.assertEqual(original.get_payload(), part.get_payload())

    def test_deep_and_large_yaml_falls_back_without_blocking_next_entry(self):
        deep = '#cloud-config\nx: ' + '[' * 600 + '0' + ']' * 600 + '\n'
        large = self.password_config + '# ' + 'x' * openstack_userdata._MAX_YAML_SIZE
        for raw in (deep, large):
            self.assertEqual((None, raw), openstack_userdata.prepare([raw]))
            password, body = openstack_userdata.prepare([raw, self.password_config])
            self.assertEqual('Test:pass!', password)
            self.assertEqual(raw.encode(), email.message_from_string(body).get_payload()[0].get_payload(decode=True))

    def test_parser_resource_errors_fall_back(self):
        original = openstack_userdata.yaml.safe_load
        try:
            for error in (RuntimeError, MemoryError, OverflowError):
                def fail(content):
                    raise error('resource limit')
                openstack_userdata.yaml.safe_load = fail
                self.assertEqual((None, self.password_config),
                                 openstack_userdata.prepare([self.password_config]))
        finally:
            openstack_userdata.yaml.safe_load = original

    def test_empty_and_non_string_password_lists(self):
        for body in ('#cloud-config\nchpasswd:\n  list: "root:"\n', '#cloud-config\nchpasswd:\n  list: []\n'):
            self.assertEqual((None, body), openstack_userdata.prepare([body]))
        self.assertEqual((None, ''), openstack_userdata.prepare([]))

    def test_unicode_and_password_punctuation(self):
        password = u'\u5bc6\u7801:quote"backslash\\!'
        body = u'#cloud-config\nchpasswd:\n  list: |\n    root:' + password + u'\n'
        actual, remaining = openstack_userdata.prepare([body])
        self.assertEqual(password, actual)
        if not isinstance(remaining, type(body)):
            remaining = remaining.decode('utf-8')
        self.assertEqual(body, remaining)

    def test_script_text_is_not_scanned_for_passwords(self):
        script = '<script>echo chpasswd: root:NotAPassword</script>'
        self.assertEqual((None, script), openstack_userdata.prepare([script]))

    def test_multiple_scripts_keep_their_types(self):
        password, body = openstack_userdata.prepare([self.xml, '#ps1_sysnative\nWrite-Output MARKER', self.password_config])
        self.assertEqual('Test:pass!', password)
        parts = email.message_from_string(body).get_payload()
        self.assertEqual(['text/x-shellscript', 'text/x-shellscript', 'text/cloud-config'], [part.get_content_type() for part in parts])
        self.assertEqual(b'#ps1_sysnative\nWrite-Output MARKER', parts[1].get_payload(decode=True))


if __name__ == '__main__':
    unittest.main()
