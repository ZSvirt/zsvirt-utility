import os

from zstacklib.test.utils import env
from zstacklib.utils import linux, uuidhelper

if not os.path.isdir(env.SNAPSHOT_DIR):
    os.makedirs(env.SNAPSHOT_DIR)

take_snapshot_cmd_body = {
    "vmUuid": None,  # must fill
    "volumeUuid": None,  # must fill
    "volume": {
        "installPath": None,  # must fill
        "deviceId": 0,
        "deviceType": "file",
        "volumeUuid": None,  # must fill
        "useVirtio": True,
        "useVirtioSCSI": False,
        "shareable": False,
        "cacheMode": "none",
        "wwn": "0x000fb964dbc7a10a",
        "bootOrder": 0,
        "physicalBlockSize": 0,
        "type": "Root",
        "format": "qcow2",
        "primaryStorageType": "LocalStorage"
    },
    "installPath": None,  # must fill
    "online": True,
    "fullSnapshot": False,
    "volumeInstallPath": None,  # must fill
    "isBaremetal2InstanceOnlineSnapshot": False,
    "kvmHostAddons": {
        "qcow2Options": " -o cluster_size=2097152 "
    }
}

merge_snapshot_cmd_body = {
    "vmUuid": None,  # must fill
    "volume": {
        "installPath": None,  # must fill
        "deviceId": 0,
        "deviceType": "file",
        "volumeUuid": None,  # must fill
        "useVirtio": True,
        "useVirtioSCSI": False,
        "shareable": False,
        "cacheMode": "none",
        "wwn": "0x000fb964dbc7a10a",
        "bootOrder": 0,
        "physicalBlockSize": 0,
        "type": "Root",
        "format": "qcow2",
        "primaryStorageType": "LocalStorage"
    },
    "srcPath": None,  # must fill
    "destPath": None,  # must fill
    "fullRebase": True,
    "kvmHostAddons": {
        "qcow2Options": " -o cluster_size=2097152 "
    }
}

take_volumes_snapshots_default_cmd_body = {
    "snapshotJobs": [],
    "timeout": 10800,
    "threadContext": {
        "task-name": "org.zstack.header.volume.APICreateVolumeSnapshotGroupMsg",
        "api": uuidhelper.uuid()
    },
    "threadContextStack": [],
    "taskContext": {
        "__messagetimeout__": str(10800 * 1000),
        "__messagedeadline__": str(linux.get_current_timestamp() * 1000 + 10800 * 1000),
    },
    "kvmHostAddons": {
        "qcow2Options": " -o cluster_size=2097152 "
    }
}


def build_snapshot_job(vm_uuid, vol_uuid, previous_install_path, new_install_path, memory=False):
    return {
        "volumeUuid": vol_uuid,
        "installPath": new_install_path,
        "vmInstanceUuid": vm_uuid,
        "previousInstallPath": previous_install_path,
        "snapshotUuid": uuidhelper.uuid(),
        "memory": memory,
        "live": True,
        "full": False,
        "volume": {
            "volumeUuid": vol_uuid,
            "installPath": previous_install_path,
            "deviceType": 'file',
            "useVirtio": True
        }
    }
