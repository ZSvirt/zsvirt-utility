import base64
import contextlib
import os
import re

from zstacklib.utils import linux
from zstacklib.utils import log

logger = log.get_logger(__name__)

KEY_AGENT_UNIX_SOCKET = 'unix:///var/run/key-agent/key-agent.sock'
KEY_AGENT_SOCKET_PATH = '/var/run/key-agent/key-agent.sock'

try:
    import grpc
    from kvmagent.keyagent import key_agent_pb2
    from kvmagent.keyagent import key_agent_pb2_grpc
    KEY_AGENT_GRPC_AVAILABLE = True
except Exception:
    KEY_AGENT_GRPC_AVAILABLE = False


def _decode_encrypted_dek(encrypted_dek_b64):
    if not encrypted_dek_b64:
        raise Exception('encryptedDek is required')

    normalized = str(encrypted_dek_b64).strip()
    if not re.match(r'^[A-Za-z0-9+/]+={0,2}$', normalized) or len(normalized) % 4 != 0:
        raise Exception('encryptedDek must be valid base64')

    encrypted_dek = base64.b64decode(normalized)
    encoded = base64.b64encode(encrypted_dek)
    if not isinstance(encoded, str):
        encoded = encoded.decode('ascii')
    if encoded.rstrip('=') != normalized.rstrip('='):
        raise Exception('encryptedDek must be canonical base64')
    return encrypted_dek


def prepare_luks_secret_material_channel(encrypted_dek_b64):
    if not KEY_AGENT_GRPC_AVAILABLE:
        raise Exception('key_agent grpc not available')
    if not os.path.exists(KEY_AGENT_SOCKET_PATH):
        raise Exception('key-agent socket not found')

    encrypted_dek = _decode_encrypted_dek(encrypted_dek_b64)
    channel = grpc.insecure_channel(KEY_AGENT_UNIX_SOCKET)
    try:
        stub = key_agent_pb2_grpc.KeyAgentServiceStub(channel)
        req = key_agent_pb2.PrepareLuksSecretMaterialChannelRequest(encrypted_dek=encrypted_dek)
        resp = stub.PrepareLuksSecretMaterialChannel(req, timeout=60)
        path = getattr(resp, 'channel_path', None) if resp else None
        path = str(path).strip() if path else ''
        if not path:
            raise Exception('key-agent PrepareLuksSecretMaterialChannel returned empty channel_path')
        return path
    except grpc.RpcError as e:
        details = e.details() if hasattr(e, 'details') and callable(getattr(e, 'details')) else str(e)
        logger.debug('key-agent PrepareLuksSecretMaterialChannel gRPC error: %s' % details)
        raise Exception(details or str(e))
    finally:
        channel.close()


def make_luks_secret_file(encrypted_dek_b64):
    """Create a one-shot FIFO for a single qemu-img operation.
    The caller passes the returned path to a *_with_secret function,
    which deletes it in its own finally block."""
    return prepare_luks_secret_material_channel(encrypted_dek_b64)


@contextlib.contextmanager
def luks_secret_channel(encrypted_dek_b64):
    if not encrypted_dek_b64:
        yield None
        return

    channel_path = prepare_luks_secret_material_channel(encrypted_dek_b64)
    try:
        yield channel_path
    finally:
        linux.rm_file_force(channel_path)
