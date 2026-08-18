from kvmagent.plugins import vm_plugin
from kvmagent.test.utils import vm_utils, network_utils, pytest_utils
from kvmagent.test.utils.stub import *
from zstacklib.test.utils import misc
from zstacklib.utils import jsonobject
from unittest import TestCase

init_kvmagent()
vm_utils.init_vm_plugin()

__ENV_SETUP__ = {
    'self': {
    }
}


class TestVmCpuQuota(TestCase, vm_utils.VmPluginTestStub):
    @classmethod
    def setUpClass(cls):
        network_utils.create_default_bridge_if_not_exist()

    @misc.test_for(handlers=[
        vm_plugin.VmPlugin.KVM_START_VM_PATH
    ])
    @pytest_utils.ztest_decorater
    def test_start_vm_with_cpu_quota(self):
        vm = vm_utils.create_startvm_body_jsonobject()
        vm.vmCpuQuota = 50000
        created = False

        try:
            vm_utils.create_vm(vm)
            created = True

            xml = vm_utils.get_vm_xmlobject_from_virsh_dump(vm.vmInstanceUuid)
            self.assertEqual(int(vm_plugin.MAX_PERIOD), int(xml.cputune.period.text_))
            self.assertEqual(vm.vmCpuQuota, int(xml.cputune.quota.text_))
        finally:
            if created:
                self._destroy_vm(vm.vmInstanceUuid)

    @misc.test_for(handlers=[
        vm_plugin.VmPlugin.VM_UPDATE_CPU_QUOTA_PATH
    ])
    @pytest_utils.ztest_decorater
    def test_update_vm_cpu_quota(self):
        vm_uuid = 'b610bb34fca64fd2a49ce91deefb1a49'
        vm_cpu_quota = 60000
        called_commands = []

        def bash_roe(command):
            called_commands.append(command)
            return 0, '', ''

        original_bash_roe = vm_plugin.bash.bash_roe

        try:
            vm_plugin.bash.bash_roe = bash_roe
            rsp = jsonobject.loads(vm_utils.VM_PLUGIN.set_vm_cpu_quota(misc.make_a_request({
                'vmUuid': vm_uuid,
                'vmCpuQuota': vm_cpu_quota
            })))
        finally:
            vm_plugin.bash.bash_roe = original_bash_roe

        self.assertTrue(rsp.success)
        self.assertEqual(
            ['virsh schedinfo %s --set vcpu_quota=%s' % (vm_uuid, vm_cpu_quota)],
            called_commands
        )
