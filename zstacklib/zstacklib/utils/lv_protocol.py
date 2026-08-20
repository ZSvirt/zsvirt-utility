import collections
import hashlib
import json
import struct

# Python 2/3 integer compatibility: Py2 has separate int/long types
try:
    _integer_types = (int, long)
except NameError:
    _integer_types = (int,)

# ---- Magic Numbers ----
HEADER_MAGIC = 0x5A534D54  # "ZSMT"
SLOT_MAGIC = 0x5A534454  # "ZSDT"

# ---- Version ----
CURRENT_HEADER_VERSION = 1

# ---- PendingOp ----
PENDING_NONE = 0
PENDING_CONFIG_UPDATE = 1
PENDING_STORAGE_CHANGE = 2

# ---- Slot Index ----
SLOT_A = 0
SLOT_B = 1

# ---- Alignment & LV Size ----
ALIGNMENT = 4096
INITIAL_LV_SIZE = 4 * 1024 * 1024  # 4 MB
MAX_LV_SIZE = 64 * 1024 * 1024  # 64 MB

# ---- Slot Structure Sizes ----
SLOT_HEADER_SIZE = 36  # Magic(4)+SeqNum(8)+Offset(8)+Cap(8)+PayloadLen(8)
CHECKSUM_SIZE = 32  # SHA-256 digest
SLOT_OVERHEAD = SLOT_HEADER_SIZE + CHECKSUM_SIZE  # 68

# ---- Read Tuning ----
OPTIMISTIC_READ_SIZE = 1 * 1024 * 1024

# ---- JSON Header Layout ----
# [0:4)      Magic         uint32 BE   0x5A534D54
# [4:6)      HeaderVersion uint16 BE   1
# [6:8)      JsonLen       uint16 BE   JSON byte length
# [8:8+N)    JSON Body     UTF-8       control fields + VM summary
# [8+N:4064) Zero Padding
# [4064:4096) Checksum     SHA-256(bytes[0:4064])
HEADER_BLOCK_SIZE = 4096
HEADER_JSON_OFFSET = 8
HEADER_CHECKSUM_OFFSET = 4064
HEADER_JSON_MAX_LEN = HEADER_CHECKSUM_OFFSET - HEADER_JSON_OFFSET  # 4056

# ---- Slot header [0:36) ----
SLOT_HEADER_FORMAT = '>IQQQQ'
SLOT_HEADER_STRUCT_SIZE = struct.calcsize(SLOT_HEADER_FORMAT)
assert SLOT_HEADER_STRUCT_SIZE == 36

# ---- I/O Sanity Check ----
IO_CHECK_PATTERN = b'ZS_IOCHECK_OK'
IO_CHECK_PATTERN_LEN = len(IO_CHECK_PATTERN)

# ---- LV Naming ----
LV_METADATA_SUFFIX = '_vmmeta'
LV_METADATA_TAG = 'zs::sharedblock::vmmeta'

# ---- Data Classes ----
SlotLayout = collections.namedtuple('SlotLayout', [
    'slot_a_offset', 'slot_a_capacity',
    'slot_b_offset', 'slot_b_capacity',
])


def calculate_slot_layout(lv_size):
    """Compute SlotA/SlotB offset & capacity for a given LV size.
    All values 4 KB-aligned. slot_capacity = floor((lv_size - 4096) / 2 / 4096) * 4096.

    Raises ValueError if lv_size is too small to hold header + two slots.
    """
    lv_size = int(lv_size)
    # Minimum viable LV: header(4K) + slotA(4K) + slotB(4K) = 12K
    min_viable = ALIGNMENT * 3
    if lv_size < min_viable:
        raise ValueError(
            "lv_size %d is too small (minimum %d = header + 2 slots)"
            % (lv_size, min_viable))
    header_reserved = ALIGNMENT
    available = lv_size - header_reserved
    slot_capacity = (available // 2 // ALIGNMENT) * ALIGNMENT

    return SlotLayout(
        slot_a_offset=header_reserved,
        slot_a_capacity=slot_capacity,
        slot_b_offset=header_reserved + slot_capacity,
        slot_b_capacity=slot_capacity,
    )


HeaderData = collections.namedtuple('HeaderData', [
    'magic', 'header_version', 'active_slot', 'pending_op',
    'write_sequence',
    'slot_a_offset', 'slot_a_capacity',
    'slot_b_offset', 'slot_b_capacity',
    'last_update_time',
    'checksum', 'valid',
    'vm_category', 'vm_uuid', 'architecture',
    'schema_version', 'vm_name',
    # Previous layout - recorded in Phase 1 when LV extend changes the
    # slot geometry.  Recovery flows use these to locate the active slot
    # whose on-disk capacity still matches the pre-extend layout.
    # Zero means "not applicable" (no prior extend in progress).
    'prev_slot_a_capacity', 'prev_slot_b_offset', 'prev_slot_b_capacity',
])

SlotData = collections.namedtuple('SlotData', [
    'magic', 'seq_num', 'slot_offset', 'slot_capacity',
    'payload_len', 'payload', 'checksum', 'valid',
])


class ReadStatus(object):
    OK = 'OK'
    STORAGE_CHANGE_INCOMPLETE = 'STORAGE_CHANGE_INCOMPLETE'
    CORRUPTED = 'CORRUPTED'


class ReadResult(object):
    __slots__ = ('status', 'payload', 'header', 'error')

    def __init__(self, status, payload=None, header=None, error=None):
        self.status = status
        self.payload = payload
        self.header = header
        self.error = error

    def is_usable(self):
        return self.status == ReadStatus.OK

    def __repr__(self):
        return ("ReadResult(status=%s, payload_len=%s, error=%s)"
                % (self.status,
                   len(self.payload) if self.payload else 0,
                   self.error))


# ---- Exceptions ----
class MetadataError(Exception):
    pass


class MetadataIOError(MetadataError):
    pass


class MetadataCapacityError(MetadataError):
    pass


# ###################################################################
# Helpers
# ###################################################################

def align_up(size, alignment=ALIGNMENT):
    """Round *size* up to the nearest multiple of *alignment*."""
    if size == 0:
        return 0
    return (size + alignment - 1) // alignment * alignment


# ###################################################################
# Codec
# ###################################################################

def build_header(active_slot, pending_op, write_sequence,
                 slot_a_offset, slot_a_capacity,
                 slot_b_offset, slot_b_capacity,
                 last_update_time, schema_version,
                 vm_category='', vm_uuid='', vm_name='',
                 architecture='',
                 prev_slot_a_capacity=0, prev_slot_b_offset=0,
                 prev_slot_b_capacity=0):
    """Serialise a 4096-byte Header Block (JSON layout).

    [0:4)      Magic         uint32 BE   0x5A534D54
    [4:6)      HeaderVersion uint16 BE   1
    [6:8)      JsonLen       uint16 BE   JSON byte length
    [8:8+N)    JSON Body     UTF-8       control fields + VM summary
    [8+N:4064) Zero Padding
    [4064:4096) Checksum     SHA-256(bytes[0:4064])
    """
    # Input validation - symmetric with parse_header
    if active_slot not in (SLOT_A, SLOT_B):
        raise ValueError("active_slot must be %d or %d, got %r"
                         % (SLOT_A, SLOT_B, active_slot))
    if pending_op not in (PENDING_NONE, PENDING_CONFIG_UPDATE, PENDING_STORAGE_CHANGE):
        raise ValueError("pending_op must be 0/1/2, got %r" % (pending_op,))
    for name, val in [('slot_a_offset', slot_a_offset),
                      ('slot_a_capacity', slot_a_capacity),
                      ('slot_b_offset', slot_b_offset),
                      ('slot_b_capacity', slot_b_capacity)]:
        if val < 0 or val % ALIGNMENT != 0:
            raise ValueError("%s must be non-negative and 4KB-aligned, got %d" % (name, val))
    if slot_a_offset < ALIGNMENT:
        raise ValueError("slot_a_offset must be >= %d (header block), got %d"
                         % (ALIGNMENT, slot_a_offset))
    if slot_b_offset < slot_a_offset + slot_a_capacity:
        raise ValueError("slot_b_offset (%d) overlaps slot_a [%d, %d)"
                         % (slot_b_offset, slot_a_offset, slot_a_offset + slot_a_capacity))
    header_dict = {
        'activeSlot': active_slot,
        'pendingOp': pending_op,
        'writeSequence': write_sequence,
        'slotAOffset': slot_a_offset,
        'slotACapacity': slot_a_capacity,
        'slotBOffset': slot_b_offset,
        'slotBCapacity': slot_b_capacity,
        'lastUpdateTime': last_update_time,
        'schemaVersion': str(schema_version) if not isinstance(schema_version, str) else schema_version,
    }
    if vm_category:
        header_dict['vmCategory'] = vm_category
    if vm_uuid:
        header_dict['vmUuid'] = vm_uuid
    if vm_name:
        header_dict['vmName'] = vm_name
    if architecture:
        header_dict['architecture'] = architecture
    if prev_slot_a_capacity > 0:
        header_dict['prevSlotACapacity'] = prev_slot_a_capacity
    if prev_slot_b_offset > 0:
        header_dict['prevSlotBOffset'] = prev_slot_b_offset
    if prev_slot_b_capacity > 0:
        header_dict['prevSlotBCapacity'] = prev_slot_b_capacity

    json_str = json.dumps(header_dict, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    json_bytes = json_str.encode('utf-8')
    json_len = len(json_bytes)

    if json_len > HEADER_JSON_MAX_LEN:
        raise ValueError("Header JSON too large: %d bytes (max %d)" % (json_len, HEADER_JSON_MAX_LEN))

    block = bytearray(HEADER_BLOCK_SIZE)
    struct.pack_into('>I', block, 0, HEADER_MAGIC)
    struct.pack_into('>H', block, 4, CURRENT_HEADER_VERSION)
    struct.pack_into('>H', block, 6, json_len)
    block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len] = json_bytes
    checksum = hashlib.sha256(bytes(block[:HEADER_CHECKSUM_OFFSET])).digest()
    block[HEADER_CHECKSUM_OFFSET:HEADER_CHECKSUM_OFFSET + CHECKSUM_SIZE] = checksum
    return bytes(block)


def parse_header(block):
    """Deserialise a 4096-byte Header Block."""
    if len(block) < HEADER_BLOCK_SIZE:
        return _invalid_header()

    magic = struct.unpack_from('>I', block, 0)[0]
    if magic != HEADER_MAGIC:
        return _invalid_header()

    header_version = struct.unpack_from('>H', block, 4)[0]
    if header_version != CURRENT_HEADER_VERSION:
        return _invalid_header()

    json_len = struct.unpack_from('>H', block, 6)[0]
    if json_len == 0 or json_len > HEADER_JSON_MAX_LEN:
        return _invalid_header()

    # Checksum verification
    stored_checksum = bytes(block[HEADER_CHECKSUM_OFFSET:HEADER_CHECKSUM_OFFSET + CHECKSUM_SIZE])
    computed = hashlib.sha256(block[:HEADER_CHECKSUM_OFFSET]).digest()
    if computed != stored_checksum:
        return _invalid_header()

    # JSON parsing
    try:
        json_str = block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len].decode('utf-8')
        d = json.loads(json_str)
    except (ValueError, UnicodeDecodeError):
        return _invalid_header()

    if not isinstance(d, dict):
        return _invalid_header()

    # Semantic validation: reject checksum-correct but logically invalid headers
    active_slot = d.get('activeSlot', 0)
    if active_slot not in (SLOT_A, SLOT_B):
        return _invalid_header()

    pending_op = d.get('pendingOp', 0)
    if pending_op not in (PENDING_NONE, PENDING_CONFIG_UPDATE, PENDING_STORAGE_CHANGE):
        return _invalid_header()

    write_sequence = d.get('writeSequence', 0)
    if not isinstance(write_sequence, _integer_types) or write_sequence < 0:
        return _invalid_header()

    slot_a_offset = d.get('slotAOffset', 0)
    slot_a_capacity = d.get('slotACapacity', 0)
    slot_b_offset = d.get('slotBOffset', 0)
    slot_b_capacity = d.get('slotBCapacity', 0)
    # Note: capacity == 0 is intentionally NOT rejected here.
    # No legitimate code path produces it (calculate_slot_layout guarantees
    # capacity > 0), and if it ever appears the downstream handles it safely:
    #   - write_metadata: required > target_cap triggers _extend_for_payload
    #   - read_metadata: parse_slot strict check fails on capacity mismatch
    #   - _initialize_if_needed: valid header skips re-init, write_metadata
    #     takes over with the extend path
    # Rejecting it here would force _write_fresh, which wipes the other slot
    # -- worse than letting the existing recovery logic handle it.
    for val in (slot_a_offset, slot_a_capacity, slot_b_offset, slot_b_capacity):
        if not isinstance(val, _integer_types) or val < 0:
            return _invalid_header()
        if val % ALIGNMENT != 0:  # must be 4KB-aligned for O_DIRECT
            return _invalid_header()

    if slot_a_offset < ALIGNMENT:  # must be after header block
        return _invalid_header()
    if slot_b_offset <= slot_a_offset:  # B must follow A
        return _invalid_header()
    if slot_b_offset < slot_a_offset + slot_a_capacity:  # B must not overlap A
        return _invalid_header()

    last_update_time = d.get('lastUpdateTime', 0)
    if not isinstance(last_update_time, _integer_types + (float,)) or last_update_time < 0:
        return _invalid_header()

    return HeaderData(
        magic=magic,
        header_version=header_version,
        active_slot=active_slot,
        pending_op=pending_op,
        write_sequence=write_sequence,
        slot_a_offset=slot_a_offset,
        slot_a_capacity=slot_a_capacity,
        slot_b_offset=slot_b_offset,
        slot_b_capacity=slot_b_capacity,
        last_update_time=last_update_time,
        checksum=stored_checksum,
        valid=True,
        vm_category=d.get('vmCategory', ''),
        vm_uuid=d.get('vmUuid', ''),
        architecture=d.get('architecture', ''),
        schema_version=d.get('schemaVersion', '0'),
        vm_name=d.get('vmName', ''),
        prev_slot_a_capacity=d.get('prevSlotACapacity', 0),
        prev_slot_b_offset=d.get('prevSlotBOffset', 0),
        prev_slot_b_capacity=d.get('prevSlotBCapacity', 0),
    )


def _invalid_header():
    return HeaderData(
        magic=0, header_version=0, active_slot=0, pending_op=0,
        write_sequence=0,
        slot_a_offset=0, slot_a_capacity=0,
        slot_b_offset=0, slot_b_capacity=0,
        last_update_time=0,
        checksum=b'', valid=False,
        vm_category='', vm_uuid='', architecture='',
        schema_version='',
        vm_name='',
        prev_slot_a_capacity=0, prev_slot_b_offset=0,
        prev_slot_b_capacity=0,
    )


def build_slot(seq_num, slot_offset, slot_capacity, payload):
    if not payload:
        raise ValueError("payload must not be empty")
    payload_len = len(payload)
    total_len = SLOT_HEADER_STRUCT_SIZE + payload_len + CHECKSUM_SIZE  # header + payload + SHA-256
    if total_len > slot_capacity:
        raise ValueError("payload too large: total %d bytes exceeds slot capacity %d" % (total_len, slot_capacity))
    header_bytes = struct.pack(
        SLOT_HEADER_FORMAT,
        SLOT_MAGIC, seq_num, slot_offset, slot_capacity, payload_len,
    )
    data_without_checksum = header_bytes + payload
    checksum = hashlib.sha256(data_without_checksum).digest()
    return data_without_checksum + checksum


def parse_slot(data, expected_offset=None, expected_capacity=None,
               strict=True):
    if len(data) < SLOT_HEADER_STRUCT_SIZE:
        return _invalid_slot()

    try:
        values = struct.unpack(SLOT_HEADER_FORMAT,
                               data[:SLOT_HEADER_STRUCT_SIZE])
    except struct.error:
        return _invalid_slot()

    magic, seq_num, slot_offset, slot_capacity, payload_len = values

    if magic != SLOT_MAGIC:
        return _invalid_slot()
    if expected_offset is not None and slot_offset != expected_offset:
        return _invalid_slot()
    if strict and expected_capacity is not None \
            and slot_capacity != expected_capacity:
        return _invalid_slot()
    if payload_len == 0:
        return _invalid_slot()
    if slot_capacity <= SLOT_OVERHEAD:
        return _invalid_slot()
    max_payload = slot_capacity - SLOT_OVERHEAD
    if payload_len > max_payload:
        return _invalid_slot()

    total_needed = SLOT_HEADER_STRUCT_SIZE + payload_len + CHECKSUM_SIZE
    if len(data) < total_needed:
        return _invalid_slot()

    payload = data[SLOT_HEADER_STRUCT_SIZE:
                   SLOT_HEADER_STRUCT_SIZE + payload_len]
    cs_start = SLOT_HEADER_STRUCT_SIZE + payload_len
    stored_checksum = data[cs_start:cs_start + CHECKSUM_SIZE]

    computed = hashlib.sha256(
        data[:SLOT_HEADER_STRUCT_SIZE + payload_len]).digest()
    if computed != stored_checksum:
        return _invalid_slot()

    return SlotData(
        magic=magic, seq_num=seq_num,
        slot_offset=slot_offset, slot_capacity=slot_capacity,
        payload_len=payload_len, payload=payload,
        checksum=stored_checksum, valid=True,
    )


def _invalid_slot():
    return SlotData(
        magic=0, seq_num=0, slot_offset=0, slot_capacity=0,
        payload_len=0, payload=b'', checksum=b'', valid=False,
    )
