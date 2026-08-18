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


class StaleMetadataGeneration(Exception):
    pass


def command_metadata_generation(cmd):
    value = getattr(cmd, 'metadataGeneration', 0)
    if value is None:
        return 0
    try:
        generation = int(value)
    except (TypeError, ValueError):
        raise ValueError("metadataGeneration must be a non-negative integer: %r" % value)
    if generation < 0:
        raise ValueError("metadataGeneration must be non-negative: %s" % generation)
    return generation


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
            metadataGeneration=command_metadata_generation(cmd),
        )

    def get(self, cmd):
        """Read a single VM metadata file's content.  Returns ``dict`` with key ``metadata``."""
        return self._do_get(cmd.metadataPath)

    def scan(self, cmd):
        return self._do_scan(cmd.metadataDir)

    def cleanup(self, cmd):
        """Delete a VM's metadata.  Returns ``dict``."""
        return self._do_cleanup(cmd.metadataPath)

    def cleanup_all(self, cmd):
        """Delete all VM metadata under metadataDir.  Returns ``dict``."""
        return self._do_cleanup_all(
            cmd.metadataDir,
            metadataGeneration=command_metadata_generation(cmd))

    def _do_write(self, metadataPath, metadata, vmUuid, vmName, vmCategory,
                  architecture, schemaVersion, metadataGeneration=0):
        raise NotImplementedError

    def _do_get(self, metadataPath):
        """Read a single metadata file.  Returns ``dict`` with key ``metadata``."""
        raise NotImplementedError

    def _do_scan(self, metadataDir):
        """Returns list of VmMetadataScanEntry."""
        raise NotImplementedError

    def _do_cleanup(self, metadataPath):
        raise NotImplementedError

    def _do_cleanup_all(self, metadataDir, metadataGeneration=0):
        raise NotImplementedError
