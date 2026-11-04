import email
import json
import os
import shutil
import tempfile
import unittest
import yaml

from kvmagent.plugins import mevoco
from zstacklib.utils import http


class TestUserdataMetadata(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='userdata-metadata-')
        self.agent = mevoco.Mevoco.__new__(mevoco.Mevoco)
        self.agent.USERDATA_ROOT = self.root
        self.agent.userData_vms = {}
        self.agent._apply_userdata_xtables = lambda to: None
        self.agent._apply_userdata_restart_httpd = lambda to: None

    def tearDown(self):
        shutil.rmtree(self.root)

    def _check(self, route):
        script = '<script>echo MARKER > C:\\marker.txt</script>'
        config = '#cloud-config\nchpasswd:\n  list: |\n    root:Test:pass!\n  expire: False\n'
        combinations = [([], None, ''), ([script], None, script),
                        ([config], 'Test:pass!', config),
                        ([script, config], 'Test:pass!', None),
                        ([script+'\n'+config], 'Test:pass!', None)]
        index = 0
        for hostname in ('CBQA01', None, '', 'quote"backslash\\name'):
            for payloads, password, windows_body in combinations:
                index += 1
                data = dict(namespaceName='ns', vmIp='192.0.2.%d' % index,
                            metadata=dict(vmUuid='test-uuid', vmHostname=hostname),
                            userdataList=payloads, agentConfig=None, l3NetworkUuid='l3')
                command = dict(userdata=data if route == 'apply_userdata' else [data], rebuild=False)
                response = json.loads(getattr(self.agent, route)({http.REQUEST_BODY: json.dumps(command)}))
                self.assertTrue(response['success'], response)
                root = os.path.join(self.root, 'ns', 'html', data['vmIp'])
                def read(name):
                    with open(os.path.join(root, name)) as stream:
                        return stream.read()
                self.assertEqual('test-uuid', read('meta-data/instance-id'))
                self.assertEqual(bool(hostname), os.path.exists(os.path.join(root, 'meta-data/local-hostname')))
                if hostname:
                    self.assertEqual(hostname, read('meta-data/local-hostname'))
                self.assertEqual(bool(payloads), os.path.exists(os.path.join(root, 'meta_data.json')))
                if not payloads:
                    continue
                expected = {'uuid': 'test-uuid'}
                if hostname:
                    expected['hostname'] = hostname
                if password:
                    expected['admin_pass'] = password
                self.assertEqual(expected, json.loads(read('meta_data.json')))
                if windows_body is not None:
                    self.assertEqual(windows_body.strip(), read('user_data').strip())
                else:
                    windows_parts = email.message_from_string(read('user_data')).get_payload()
                    self.assertEqual(['text/x-shellscript', 'text/cloud-config'],
                                     [part.get_content_type() for part in windows_parts])
                    self.assertEqual(script.encode(), windows_parts[0].get_payload(decode=True).strip())
                    self.assertEqual(yaml.safe_load(config), yaml.safe_load(windows_parts[1].get_payload(decode=True)))
                # Linux/EC2 consumers must retain their original payloads.
                ec2 = read('user-data')
                if len(payloads) == 1:
                    self.assertEqual(payloads[0], ec2)
                else:
                    message = email.message_from_string(ec2)
                    self.assertEqual([part.get_payload() for part in message.get_payload()],
                                     [value.strip() for value in payloads])

    def test_single_apply(self):
        self._check('apply_userdata')

    def test_batch_apply(self):
        self._check('batch_apply_userdata')

    def test_deep_yaml_single_and_batch_rebuild(self):
        deep = '#cloud-config\nx: ' + '[' * 600 + '0' + ']' * 600 + '\n'
        self._check_unconverted_single_and_batch(deep)

    def test_merge_alias_chain_single_and_batch_rebuild(self):
        raw = '#cloud-config\na0: &a0 {value: 1}\n'
        for index in range(1, 21):
            raw += 'a%d: &a%d {<<: [*a%d, *a%d]}\n' % (index, index, index-1, index-1)
        raw += 'chpasswd:\n  list: "root:DoNotExtract!"\n'
        self._check_unconverted_single_and_batch(raw)

    def _check_unconverted_single_and_batch(self, deep):
        config = '#cloud-config\nchpasswd:\n  list: "root:Expected!"\n'
        def data(ip, content):
            return dict(namespaceName='ns', vmIp=ip,
                        metadata=dict(vmUuid=ip, vmHostname='CBQA01'),
                        userdataList=[content], agentConfig=None, l3NetworkUuid='l3',
                        dhcpServerIp='192.0.2.254', bridgeName='br-test', port=80)
        restarted = []
        self.agent._apply_userdata_restart_httpd = lambda to: restarted.append(to.namespaceName)
        first = data('192.0.2.1', deep)
        response = json.loads(self.agent.apply_userdata(
            {http.REQUEST_BODY: json.dumps(dict(userdata=first))}))
        self.assertTrue(response['success'], response)
        with open(os.path.join(self.root, 'ns', 'html', first['vmIp'], 'user_data')) as stream:
            self.assertEqual(deep.strip(), stream.read().strip())
        with open(os.path.join(self.root, 'ns', 'html', first['vmIp'], 'meta_data.json')) as stream:
            self.assertNotIn('admin_pass', json.load(stream))
        restarted[:] = []
        original = mevoco.shell.call
        stopped = []
        try:
            mevoco.shell.call = lambda cmd: stopped.append(cmd)
            response = json.loads(self.agent.batch_apply_userdata({http.REQUEST_BODY: json.dumps(
                dict(rebuild=True, userdata=[first, data('192.0.2.2', config)]))}))
        finally:
            mevoco.shell.call = original
        self.assertTrue(response['success'], response)
        self.assertEqual(1, len(stopped))
        self.assertEqual(['ns'], restarted)
        root = os.path.join(self.root, 'ns', 'html')
        with open(os.path.join(root, '192.0.2.1', 'user_data')) as stream:
            self.assertEqual(deep.strip(), stream.read().strip())
        with open(os.path.join(root, '192.0.2.2', 'meta_data.json')) as stream:
            self.assertEqual('Expected!', json.load(stream)['admin_pass'])


if __name__ == '__main__':
    unittest.main()
