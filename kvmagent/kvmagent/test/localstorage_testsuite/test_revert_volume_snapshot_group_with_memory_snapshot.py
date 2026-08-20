# encoding: utf-8
import copy
from unittest import TestCase

import pytest

from kvmagent.test.utils import vm_utils, network_utils, volume_utils, pytest_utils, snapshot_utils
from kvmagent.test.utils.stub import *
from kvmagent.test.utils.vm_utils import start_vm_data_vol, startVmCmdBody, CDROM_UUID, ROOT_VOLUME_UUID
from zstacklib.utils import linux, uuidhelper, jsonobject
from zstacklib.utils import log
import platform

init_kvmagent()
vm_utils.init_vm_plugin()

__ENV_SETUP__ = {
    'self': {'timeout': 120}
}

logger = log.get_logger(__name__)


class TestVolumeWithIoThreadPin(TestCase, vm_utils.VmPluginTestStub):

    @classmethod
    def setUpClass(cls):
        network_utils.create_default_bridge_if_not_exist()

    @pytest.mark.run()
    @pytest_utils.ztest_decorater
    def test_revert_volume_snapshot_group_with_memory_snapshot(self):
        virtio1_uuid, virtio1_path = volume_utils.create_empty_volume()
        virtio2_uuid, virtio2_path = volume_utils.create_empty_volume()

        virtio_scsi1_uuid, virtio_scsi1_path = volume_utils.create_empty_volume()
        virtio_scsi2_uuid, virtio_scsi2_path = volume_utils.create_empty_volume()

        deviceinfo = None
        virtualDeviceInfoListByUuid = {}

        def build_start_vm_body(with_memory_and_vm_xml=True):
            body = copy.deepcopy(startVmCmdBody)
            body['rootVolume']['deviceAddress'] = virtualDeviceInfoListByUuid[ROOT_VOLUME_UUID]
            body['dataVolumes'] = build_start_data_volume_body()
            if with_memory_and_vm_xml:
                body['vmXml'] = deviceinfo.vmXml
                body['memorySnapshotPath'] = memorySnapshotPath
            body['cdRoms'] = [{
                'bootOrder': 0,
                'deviceId': 0,
                'isEmpty': True,
                'resourceUuid': CDROM_UUID,
                'deviceAddress': virtualDeviceInfoListByUuid[CDROM_UUID]
            }]
            return jsonobject.loads(jsonobject.dumps(body))

        def build_start_data_volume_body():
            def build_data_volume(vol_uuid, vol_path, use_virtio, use_virtio_scsi, device_address, device_id):
                volume = copy.deepcopy(start_vm_data_vol)
                volume['volumeUuid'] = vol_uuid
                volume['installPath'] = vol_path
                volume['useVirtio'] = use_virtio
                volume['useVirtioSCSI'] = use_virtio_scsi
                volume['deviceAddress'] = device_address
                volume['deviceId'] = device_id
                return volume

            return [
                build_data_volume(virtio1_uuid, virtio1_new_path, True, False,
                                  virtualDeviceInfoListByUuid[virtio1_uuid], 1),
                build_data_volume(virtio_scsi1_uuid, virtio_scsi1_new_path, False, True,
                                  virtualDeviceInfoListByUuid[virtio_scsi1_uuid], 2),
                build_data_volume(virtio2_uuid, virtio2_new_path, True, False,
                                  virtualDeviceInfoListByUuid[virtio2_uuid], 3),
                build_data_volume(virtio_scsi2_uuid, virtio_scsi2_new_path, False, True,
                                  virtualDeviceInfoListByUuid[virtio_scsi2_uuid], 4)
            ]

        # create vm with one nic and one cdRom
        vm = jsonobject.loads(jsonobject.dumps(startVmCmdBody))
        vm['cdRoms'].append({
            'bootOrder': 0,
            'deviceId': 0,
            'isEmpty': True,
            'resourceUuid': CDROM_UUID,
        })
        vm_uuid = vm.vmInstanceUuid
        vm_utils.create_vm(vm)

        # attach data volume to vm
        _, virtio1_vol = vm_utils.build_attach_volume_to_vm_body(vm_uuid, virtio1_uuid, virtio1_path, 1)
        _, virtio_scsi1_vol = vm_utils.build_attach_volume_to_vm_body(vm_uuid, virtio_scsi1_uuid, virtio_scsi1_path, 2,
                                                                      use_virtio_scsi=True)
        _, virtio2_vol = vm_utils.build_attach_volume_to_vm_body(vm_uuid, virtio2_uuid, virtio2_path, 3)
        _, virtio_scsi2_vol = vm_utils.build_attach_volume_to_vm_body(vm_uuid, virtio_scsi2_uuid, virtio_scsi2_path, 4,
                                                                      use_virtio_scsi=True)
        rsp = vm_utils.check_volume(vm_uuid, [virtio1_vol, virtio_scsi1_vol, virtio2_vol, virtio_scsi2_vol])
        self.assertTrue(rsp.success)

        # check disk order in xml
        vm_xmlobject = vm_utils.get_vm_xmlobject_from_virsh_dump(vm_uuid)

        # total 6 disk : 5 volume 1 cdrom
        self.assertTrue(len(vm_xmlobject.devices.get_child_node_as_list('disk')) == 6)

        if platform.machine() == 'x86_64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vde')
                    self.assertEqual(disk.source.file_, virtio2_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'hdc')
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdd')
                    self.assertEqual(disk.source.file_, virtio_scsi1_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi2_path)
        if platform.machine() == 'aarch64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vdg')
                    self.assertEqual(disk.source.file_, virtio2_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'sdc')
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi1_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'sdh')
                    self.assertEqual(disk.source.file_, virtio_scsi2_path)

        # create snapshot install path
        memorySnapshotUuid = uuidhelper.uuid()
        memorySnapshotPath = os.path.join(env.SNAPSHOT_DIR, '%s.qcow2' % memorySnapshotUuid)
        virtio1_new_path = os.path.join(env.SNAPSHOT_DIR, '%s.qcow2' % uuidhelper.uuid())
        virtio2_new_path = os.path.join(env.SNAPSHOT_DIR, '%s.qcow2' % uuidhelper.uuid())
        virtio_scsi1_new_path = os.path.join(env.SNAPSHOT_DIR, '%s.qcow2' % uuidhelper.uuid())
        virtio_scsi2_new_path = os.path.join(env.SNAPSHOT_DIR, '%s.qcow2' % uuidhelper.uuid())
        newPath = {virtio1_uuid: virtio1_new_path,
                   virtio2_uuid: virtio2_new_path,
                   virtio_scsi1_uuid: virtio_scsi1_new_path,
                   virtio_scsi2_uuid: virtio_scsi2_new_path,
                   memorySnapshotUuid: memorySnapshotPath}

        # create snapshot body
        snapshot_jobs = [
            snapshot_utils.build_snapshot_job(vm_uuid, virtio1_uuid, virtio1_path, newPath[virtio1_uuid]),
            snapshot_utils.build_snapshot_job(vm_uuid, virtio2_uuid, virtio2_path, newPath[virtio2_uuid]),
            snapshot_utils.build_snapshot_job(vm_uuid, virtio_scsi1_uuid, virtio_scsi1_path,
                                              newPath[virtio_scsi1_uuid]),
            snapshot_utils.build_snapshot_job(vm_uuid, virtio_scsi2_uuid, virtio_scsi2_path,
                                              newPath[virtio_scsi2_uuid]),
            snapshot_utils.build_snapshot_job(vm_uuid, memorySnapshotUuid, env.SNAPSHOT_DIR,
                                              newPath[memorySnapshotUuid], memory=True)]

        # take volumes snapshots
        vm_utils.take_volumes_snapshots(snapshot_jobs)

        # sync vm deviceinfo
        deviceinfo = vm_utils.sync_vm_deviceinfo(vm_uuid)
        self.assertTrue(deviceinfo.success, "deviceinfo.success should be true: deviceinfo = %s" % deviceinfo.to_dict())
        self.assertIsNotNone(deviceinfo.virtualDeviceInfoList, "deviceinfo.virtualDeviceInfoList should be not None: deviceinfo = %s" % deviceinfo.to_dict())

        # get disk deviceAddress
        virtualDeviceInfoListByUuid = {}
        for vdi in deviceinfo.virtualDeviceInfoList:
            deviceAddress = {}
            if vdi.deviceAddress.type:
                deviceAddress['type'] = vdi.deviceAddress.type
            if vdi.deviceAddress.slot:
                deviceAddress['slot'] = vdi.deviceAddress.slot
            if vdi.deviceAddress.bus:
                deviceAddress['bus'] = vdi.deviceAddress.bus
            if vdi.deviceAddress.domain:
                deviceAddress['domain'] = vdi.deviceAddress.domain
            if vdi.deviceAddress.function:
                deviceAddress['function'] = vdi.deviceAddress.function
            if vdi.deviceAddress.controller:
                deviceAddress['controller'] = vdi.deviceAddress.controller
            if vdi.deviceAddress.target:
                deviceAddress['target'] = vdi.deviceAddress.target
            if vdi.deviceAddress.unit:
                deviceAddress['unit'] = vdi.deviceAddress.unit
            virtualDeviceInfoListByUuid[vdi.resourceUuid] = deviceAddress

        # stop vm
        vm_utils.stop_vm(vm_uuid)
        pid = linux.find_vm_pid_by_uuid(vm_uuid)
        self.assertTrue(not pid, 'vm[%s] vm still running' % vm_uuid)

        # start vm with memory snapshot
        vm_utils.create_vm(build_start_vm_body())
        self.vm_uuid = vm.vmInstanceUuid
        pid = linux.find_vm_pid_by_uuid(vm_uuid)
        self.assertFalse(not pid, 'cannot find pid of vm[%s]' % vm_uuid)

        vm_xmlobject = vm_utils.get_vm_xmlobject_from_virsh_dump(vm_uuid)

        if platform.machine() == 'x86_64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_new_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vde')
                    self.assertEqual(disk.source.file_, virtio2_new_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'hdc')
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdd')
                    self.assertEqual(disk.source.file_, virtio_scsi1_new_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi2_new_path)
        if platform.machine() == 'aarch64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_new_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vdg')
                    self.assertEqual(disk.source.file_, virtio2_new_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'sdc')
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi1_new_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'sdh')
                    self.assertEqual(disk.source.file_, virtio_scsi2_new_path)

        # stop vm
        vm_utils.stop_vm(vm_uuid)
        pid = linux.find_vm_pid_by_uuid(vm_uuid)
        self.assertTrue(not pid, 'vm[%s] vm still running' % vm_uuid)

        # start vm without memory snapshot and vm xml
        vm_utils.create_vm(build_start_vm_body(with_memory_and_vm_xml=False))
        self.vm_uuid = vm.vmInstanceUuid
        pid = linux.find_vm_pid_by_uuid(vm_uuid)
        self.assertFalse(not pid, 'cannot find pid of vm[%s]' % vm_uuid)

        vm_xmlobject = vm_utils.get_vm_xmlobject_from_virsh_dump(vm_uuid)
        if platform.machine() == 'x86_64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_new_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vde')
                    self.assertEqual(disk.source.file_, virtio2_new_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'sdd')
                    self.assertEqual(disk.source.file_, virtio_scsi2_new_path)
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi1_new_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'hdc')
        if platform.machine() == 'aarch64':
            # check disk order in xml
            for idx, disk in enumerate(vm_xmlobject.devices.get_child_node_as_list('disk')):
                if idx == 0:
                    self.assertEqual(disk.target.dev_, 'vda')
                elif idx == 1:
                    self.assertEqual(disk.target.dev_, 'vdb')
                    self.assertEqual(disk.source.file_, virtio1_new_path)
                elif idx == 2:
                    self.assertEqual(disk.target.dev_, 'vdg')
                    self.assertEqual(disk.source.file_, virtio2_new_path)
                elif idx == 3:
                    self.assertEqual(disk.target.dev_, 'sdc')
                elif idx == 4:
                    self.assertEqual(disk.target.dev_, 'sdf')
                    self.assertEqual(disk.source.file_, virtio_scsi2_new_path)
                elif idx == 5:
                    self.assertEqual(disk.target.dev_, 'sdh')
                    self.assertEqual(disk.source.file_, virtio_scsi1_new_path)
