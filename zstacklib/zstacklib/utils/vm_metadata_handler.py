class VmMetadataScanEntry(object):
    def __init__(self, vmUuid, metadataPath, sizeBytes=0, lastUpdateTime=0,
                 vmName='', vmCategory='', architecture='', schemaVersion='',
                 incomplete=False):
        self.vmUuid = vmUuid
        self.vmName = vmName
        self.vmCategory = vmCategory
        self.architecture = architecture
        self.schemaVersion = schemaVersion
        self.metadataPath = metadataPath
        self.sizeBytes = sizeBytes
        self.lastUpdateTime = lastUpdateTime
        self.incomplete = incomplete


class VmMetadataHandler(object):
    def write(self, cmd):
        vmCategory = getattr(cmd, 'vmCategory', None)
        schemaVersion = getattr(cmd, 'schemaVersion', None)
        return self._do_write(
            metadataPath=cmd.metadataPath,
            metadata=cmd.metadata,
            vmUuid=getattr(cmd, 'vmUuid', None) or '',
            vmName=getattr(cmd, 'vmName', None) or '',
            vmCategory=vmCategory if vmCategory is not None else '',
            architecture=getattr(cmd, 'architecture', None) or '',
            schemaVersion=schemaVersion if schemaVersion is not None else '',
        )

    def get(self, cmd):
        """Read a single VM metadata file's content.  Returns ``dict`` with key ``metadata``."""
        return self._do_get(cmd.metadataPath)

    def scan(self, cmd):
        return self._do_scan(cmd.metadataDir)

    def cleanup(self, cmd):
        """Delete a VM's metadata.  Returns ``dict``."""
        return self._do_cleanup(cmd.metadataPath)

    def _do_write(self, metadataPath, metadata, vmUuid, vmName, vmCategory, architecture, schemaVersion):
        raise NotImplementedError

    def _do_get(self, metadataPath):
        """Read a single metadata file.  Returns ``dict`` with key ``metadata``."""
        raise NotImplementedError

    def _do_scan(self, metadataDir):
        """Returns list of VmMetadataScanEntry."""
        raise NotImplementedError

    def _do_cleanup(self, metadataPath):
        raise NotImplementedError
