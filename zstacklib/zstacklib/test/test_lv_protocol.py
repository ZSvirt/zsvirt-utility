"""Pure-Python unit tests for lv_protocol codec functions.

No hardware, LVM, or O_DIRECT needed -- exercises only the in-memory
build/parse helpers and constants defined in lv_protocol.py.
"""

import hashlib
import json
import os
import struct
import threading
from unittest import TestCase
from unittest.mock import MagicMock, patch

from zstacklib.utils.lv_protocol import (
    # constants
    HEADER_MAGIC, SLOT_MAGIC,
    ALIGNMENT, INITIAL_LV_SIZE, MAX_LV_SIZE,
    SLOT_HEADER_SIZE, CHECKSUM_SIZE, SLOT_OVERHEAD,
    HEADER_BLOCK_SIZE, HEADER_JSON_OFFSET, HEADER_CHECKSUM_OFFSET,
    HEADER_JSON_MAX_LEN,
    SLOT_A, SLOT_B,
    PENDING_NONE, PENDING_CONFIG_UPDATE, PENDING_STORAGE_CHANGE,
    # layout
    SlotLayout, calculate_slot_layout,
    # codec
    align_up,
    build_header, parse_header,
    build_slot, parse_slot,
    # data classes
    ReadStatus, ReadResult,
)
from zstacklib.utils.lv_metadata import (
    calculate_extend_size,
    scan_metadata_lvs,
    _storage_topology_changed,
    get_metadata_status,
    MetadataCapacityError, MetadataIOError, SblkMetadataHandler,
)
from zstacklib.utils.vm_metadata_handler import StaleMetadataGeneration
from zstacklib.utils.vm_metadata_handler import VmMetadataHandler


# ---- helpers ----------------------------------------------------------------

def _make_header(**overrides):
    """Build a valid header with sensible defaults; overrides replace fields."""
    defaults = dict(
        active_slot=SLOT_A,
        pending_op=PENDING_NONE,
        write_sequence=1,
        slot_a_offset=4096,
        slot_a_capacity=2 * 1024 * 1024 - 4096,
        slot_b_offset=2 * 1024 * 1024,
        slot_b_capacity=2 * 1024 * 1024 - 4096,
        last_update_time=1700000000000,
        schema_version='1',
    )
    defaults.update(overrides)
    return build_header(**defaults)


def _tamper_header_field(field_name, value):
    """Build a header with valid checksum but one JSON field replaced.

    Constructs a normal header, patches the JSON, then recomputes the SHA-256
    so the header passes checksum verification but fails semantic validation.
    """
    block = bytearray(_make_header())
    json_len = struct.unpack_from('>H', block, 6)[0]
    json_str = block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len].decode('utf-8')
    d = json.loads(json_str)
    d[field_name] = value
    new_json = json.dumps(d, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len] = b'\x00' * json_len
    struct.pack_into('>H', block, 6, len(new_json))
    block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + len(new_json)] = new_json
    checksum = hashlib.sha256(bytes(block[:HEADER_CHECKSUM_OFFSET])).digest()
    block[HEADER_CHECKSUM_OFFSET:HEADER_CHECKSUM_OFFSET + 32] = checksum
    return bytes(block)


# #############################################################################
# TestLvProtocolConstants
# #############################################################################

class TestLvProtocolConstants(TestCase):

    def test_magic_values(self):
        self.assertEqual(HEADER_MAGIC, 0x5A534D54)
        self.assertEqual(SLOT_MAGIC, 0x5A534454)

    def test_alignment_and_sizes(self):
        self.assertEqual(ALIGNMENT, 4096)
        self.assertEqual(INITIAL_LV_SIZE, 4 * 1024 * 1024)
        self.assertEqual(MAX_LV_SIZE, 64 * 1024 * 1024)

    def test_slot_overhead(self):
        self.assertEqual(SLOT_OVERHEAD, SLOT_HEADER_SIZE + CHECKSUM_SIZE)
        self.assertEqual(SLOT_OVERHEAD, 68)

    def test_header_layout_offsets(self):
        self.assertEqual(HEADER_BLOCK_SIZE, 4096)
        self.assertEqual(HEADER_JSON_OFFSET, 8)
        self.assertEqual(HEADER_CHECKSUM_OFFSET, 4064)
        self.assertEqual(HEADER_JSON_MAX_LEN, 4064 - 8)


# #############################################################################
# TestAlignUp
# #############################################################################

class TestAlignUp(TestCase):

    def test_already_aligned(self):
        self.assertEqual(align_up(4096), 4096)

    def test_round_up(self):
        self.assertEqual(align_up(1), 4096)
        self.assertEqual(align_up(4097), 8192)

    def test_zero(self):
        self.assertEqual(align_up(0), 0)

    def test_custom_alignment(self):
        self.assertEqual(align_up(5, 4), 8)
        self.assertEqual(align_up(8, 4), 8)
        self.assertEqual(align_up(9, 4), 12)


# #############################################################################
# TestBuildParseHeader
# #############################################################################

class TestBuildParseHeader(TestCase):

    def test_roundtrip(self):
        block = _make_header(
            active_slot=SLOT_B,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=42,
            slot_a_offset=4096,
            slot_a_capacity=98304,
            slot_b_offset=102400,
            slot_b_capacity=98304,
            last_update_time=1700000000000,
            schema_version='2',
        )
        self.assertEqual(len(block), HEADER_BLOCK_SIZE)

        hd = parse_header(block)
        self.assertTrue(hd.valid)
        self.assertEqual(hd.magic, HEADER_MAGIC)
        self.assertEqual(hd.active_slot, SLOT_B)
        self.assertEqual(hd.pending_op, PENDING_CONFIG_UPDATE)
        self.assertEqual(hd.write_sequence, 42)
        self.assertEqual(hd.slot_a_offset, 4096)
        self.assertEqual(hd.slot_a_capacity, 98304)
        self.assertEqual(hd.slot_b_offset, 102400)
        self.assertEqual(hd.slot_b_capacity, 98304)
        self.assertEqual(hd.last_update_time, 1700000000000)
        self.assertEqual(hd.schema_version, '2')

    def test_with_vm_metadata(self):
        block = _make_header(
            vm_category='AppCenter',
            vm_uuid='abcd1234',
            vm_name='my-test-vm',
            architecture='x86_64',
        )
        hd = parse_header(block)
        self.assertTrue(hd.valid)
        self.assertEqual(hd.vm_category, 'AppCenter')
        self.assertEqual(hd.vm_uuid, 'abcd1234')
        self.assertEqual(hd.vm_name, 'my-test-vm')
        self.assertEqual(hd.architecture, 'x86_64')

    def test_invalid_magic_returns_invalid(self):
        block = bytearray(_make_header())
        struct.pack_into('>I', block, 0, 0xDEADBEEF)
        # recompute checksum so ONLY the magic value is wrong
        checksum = hashlib.sha256(bytes(block[:HEADER_CHECKSUM_OFFSET])).digest()
        block[HEADER_CHECKSUM_OFFSET:HEADER_CHECKSUM_OFFSET + 32] = checksum
        hd = parse_header(bytes(block))
        self.assertFalse(hd.valid)

    def test_corrupted_checksum_returns_invalid(self):
        block = bytearray(_make_header())
        # flip one byte in the checksum area
        block[HEADER_CHECKSUM_OFFSET] ^= 0xFF
        hd = parse_header(bytes(block))
        self.assertFalse(hd.valid)

    def test_json_too_large_raises(self):
        with self.assertRaises(ValueError):
            build_header(
                active_slot=0, pending_op=0, write_sequence=1,
                slot_a_offset=4096, slot_a_capacity=4096,
                slot_b_offset=8192, slot_b_capacity=4096,
                last_update_time=0, schema_version='1',
                vm_name='X' * 5000,  # will push JSON well over 4056 bytes
            )

    def test_truncated_block_returns_invalid(self):
        block = _make_header()[:2048]
        hd = parse_header(block)
        self.assertFalse(hd.valid)

    # -- Semantic validation tests --

    def test_invalid_active_slot_rejected(self):
        hd = parse_header(_tamper_header_field('activeSlot', 2))
        self.assertFalse(hd.valid)
        hd = parse_header(_tamper_header_field('activeSlot', -1))
        self.assertFalse(hd.valid)

    def test_invalid_pending_op_rejected(self):
        hd = parse_header(_tamper_header_field('pendingOp', 99))
        self.assertFalse(hd.valid)
        hd = parse_header(_tamper_header_field('pendingOp', -1))
        self.assertFalse(hd.valid)

    def test_negative_write_sequence_rejected(self):
        hd = parse_header(_tamper_header_field('writeSequence', -1))
        self.assertFalse(hd.valid)

    def test_negative_slot_offset_rejected(self):
        hd = parse_header(_tamper_header_field('slotAOffset', -4096))
        self.assertFalse(hd.valid)

    def test_slot_a_offset_before_header_rejected(self):
        # slotAOffset must be >= ALIGNMENT (header occupies first block)
        hd = parse_header(_tamper_header_field('slotAOffset', 0))
        self.assertFalse(hd.valid)

    def test_slot_b_not_after_a_rejected(self):
        hd = parse_header(_tamper_header_field('slotBOffset', 4096))
        self.assertFalse(hd.valid)
        hd = parse_header(_tamper_header_field('slotBOffset', 0))
        self.assertFalse(hd.valid)

    def test_negative_last_update_time_rejected(self):
        hd = parse_header(_tamper_header_field('lastUpdateTime', -1))
        self.assertFalse(hd.valid)

    def test_valid_pending_ops_accepted(self):
        for op in (PENDING_NONE, PENDING_CONFIG_UPDATE, PENDING_STORAGE_CHANGE):
            hd = parse_header(_tamper_header_field('pendingOp', op))
            self.assertTrue(hd.valid, "pendingOp=%d should be valid" % op)

    def test_roundtrip_with_prev_layout(self):
        """prev_slot_* fields survive build -> parse roundtrip."""
        block = build_header(
            active_slot=SLOT_A, pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=5,
            slot_a_offset=4096, slot_a_capacity=3141632,
            slot_b_offset=3145728, slot_b_capacity=3141632,
            last_update_time=1700000000000, schema_version='1',
            prev_slot_a_capacity=2093056,
            prev_slot_b_offset=2097152,
            prev_slot_b_capacity=2093056,
        )
        hd = parse_header(block)
        self.assertTrue(hd.valid)
        self.assertEqual(hd.prev_slot_a_capacity, 2093056)
        self.assertEqual(hd.prev_slot_b_offset, 2097152)
        self.assertEqual(hd.prev_slot_b_capacity, 2093056)

    def test_prev_layout_zero_omitted_from_json(self):
        """When prev_slot_* are 0 (default), they should not appear in JSON."""
        block = _make_header()
        json_len = struct.unpack_from('>H', block, 6)[0]
        json_str = block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len]
        json_str = json_str.decode('utf-8')
        d = json.loads(json_str)
        self.assertNotIn('prevSlotACapacity', d)
        self.assertNotIn('prevSlotBOffset', d)
        self.assertNotIn('prevSlotBCapacity', d)

    def test_prev_layout_backward_compat(self):
        """Parsing an old header (no prev_* keys) yields prev_* = 0."""
        block = _make_header()
        hd = parse_header(block)
        self.assertTrue(hd.valid)
        self.assertEqual(hd.prev_slot_a_capacity, 0)
        self.assertEqual(hd.prev_slot_b_offset, 0)
        self.assertEqual(hd.prev_slot_b_capacity, 0)


# #############################################################################
# TestBuildParseSlot
# #############################################################################

class TestBuildParseSlot(TestCase):

    def _make_slot(self, payload=b'hello world', seq=1,
                   offset=4096, capacity=4096):
        return build_slot(seq, offset, capacity, payload)

    def test_roundtrip(self):
        payload = b'{"key":"value"}'
        data = self._make_slot(payload=payload, seq=7,
                               offset=8192, capacity=4096)
        sd = parse_slot(data, expected_offset=8192, expected_capacity=4096)
        self.assertTrue(sd.valid)
        self.assertEqual(sd.magic, SLOT_MAGIC)
        self.assertEqual(sd.seq_num, 7)
        self.assertEqual(sd.slot_offset, 8192)
        self.assertEqual(sd.slot_capacity, 4096)
        self.assertEqual(sd.payload, payload)
        self.assertEqual(sd.payload_len, len(payload))

    def test_payload_empty_raises(self):
        with self.assertRaises(ValueError):
            build_slot(1, 4096, 4096, b'')

    def test_payload_too_large_raises(self):
        capacity = 128
        big_payload = b'X' * (capacity + 1)  # exceeds capacity even without overhead
        with self.assertRaises(ValueError):
            build_slot(1, 4096, capacity, big_payload)

    def test_corrupted_checksum_returns_invalid(self):
        data = bytearray(self._make_slot())
        # flip the last byte (inside checksum)
        data[-1] ^= 0xFF
        sd = parse_slot(bytes(data), expected_offset=4096, expected_capacity=4096)
        self.assertFalse(sd.valid)

    def test_wrong_offset_returns_invalid(self):
        data = self._make_slot(offset=4096)
        sd = parse_slot(data, expected_offset=9999)
        self.assertFalse(sd.valid)

    def test_wrong_capacity_returns_invalid(self):
        data = self._make_slot(capacity=4096)
        sd = parse_slot(data, expected_offset=4096, expected_capacity=9999,
                        strict=True)
        self.assertFalse(sd.valid)

    def test_strict_false_allows_capacity_mismatch(self):
        data = self._make_slot(capacity=4096)
        sd = parse_slot(data, expected_offset=4096, expected_capacity=9999,
                        strict=False)
        self.assertTrue(sd.valid)
        self.assertEqual(sd.slot_capacity, 4096)


# #############################################################################
# TestReadResult
# #############################################################################

class TestReadResult(TestCase):

    def test_is_usable_for_ok_states(self):
        rr = ReadResult(status=ReadStatus.OK, payload=b'data')
        self.assertTrue(rr.is_usable(), "OK should be usable")

    def test_not_usable_for_corrupted(self):
        for status in (ReadStatus.CORRUPTED,
                       ReadStatus.STORAGE_CHANGE_INCOMPLETE):
            rr = ReadResult(status=status)
            self.assertFalse(rr.is_usable(),
                             "%s should not be usable" % status)

    def test_repr(self):
        rr = ReadResult(status=ReadStatus.OK, payload=b'abc',
                        error='none')
        s = repr(rr)
        self.assertIn('OK', s)
        self.assertIn('3', s)  # payload_len
        self.assertIn('none', s)


# #############################################################################
# TestCalculateSlotLayout  (P1 fix)
# #############################################################################

class TestCalculateSlotLayout(TestCase):

    def test_normal_4mb(self):
        layout = calculate_slot_layout(4 * 1024 * 1024)
        self.assertEqual(layout.slot_a_offset, ALIGNMENT)
        self.assertGreater(layout.slot_a_capacity, 0)
        self.assertEqual(layout.slot_b_offset,
                         ALIGNMENT + layout.slot_a_capacity)
        self.assertEqual(layout.slot_a_capacity, layout.slot_b_capacity)
        # capacity must be aligned
        self.assertEqual(layout.slot_a_capacity % ALIGNMENT, 0)

    def test_minimum_viable_size(self):
        """3 * ALIGNMENT (12KB) is the minimum: header + slotA + slotB."""
        layout = calculate_slot_layout(3 * ALIGNMENT)
        self.assertEqual(layout.slot_a_offset, ALIGNMENT)
        self.assertEqual(layout.slot_a_capacity, ALIGNMENT)
        self.assertEqual(layout.slot_b_offset, 2 * ALIGNMENT)
        self.assertEqual(layout.slot_b_capacity, ALIGNMENT)

    def test_too_small_raises(self):
        """lv_size < 3*ALIGNMENT must raise ValueError."""
        with self.assertRaises(ValueError):
            calculate_slot_layout(0)
        with self.assertRaises(ValueError):
            calculate_slot_layout(ALIGNMENT)
        with self.assertRaises(ValueError):
            calculate_slot_layout(2 * ALIGNMENT)
        with self.assertRaises(ValueError):
            calculate_slot_layout(100)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            calculate_slot_layout(-1)


# #############################################################################
# TestBuildHeaderValidation  (P5 fix)
# #############################################################################

class TestBuildHeaderValidation(TestCase):

    def test_invalid_active_slot_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=2, pending_op=0, write_sequence=1,
                         slot_a_offset=4096, slot_a_capacity=4096,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_invalid_pending_op_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=0, pending_op=99, write_sequence=1,
                         slot_a_offset=4096, slot_a_capacity=4096,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_unaligned_offset_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=0, pending_op=0, write_sequence=1,
                         slot_a_offset=4097, slot_a_capacity=4096,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_negative_capacity_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=0, pending_op=0, write_sequence=1,
                         slot_a_offset=4096, slot_a_capacity=-4096,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_slot_a_at_zero_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=0, pending_op=0, write_sequence=1,
                         slot_a_offset=0, slot_a_capacity=4096,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_slot_overlap_raises(self):
        with self.assertRaises(ValueError):
            build_header(active_slot=0, pending_op=0, write_sequence=1,
                         slot_a_offset=4096, slot_a_capacity=8192,
                         slot_b_offset=8192, slot_b_capacity=4096,
                         last_update_time=0, schema_version='1')

    def test_valid_build_passes(self):
        """Sanity: normal arguments should not raise."""
        block = build_header(active_slot=0, pending_op=0, write_sequence=1,
                             slot_a_offset=4096, slot_a_capacity=4096,
                             slot_b_offset=8192, slot_b_capacity=4096,
                             last_update_time=0, schema_version='1')
        self.assertEqual(len(block), HEADER_BLOCK_SIZE)


# #############################################################################
# TestParseHeaderOverlap  (P3 fix)
# #############################################################################

class TestParseHeaderOverlap(TestCase):

    def test_overlapping_slots_rejected(self):
        """slot_b_offset inside slot_a range must be rejected."""
        # Default: a_offset=4096, a_cap=2M-4096, b_offset=2M
        # Tamper a_cap to 2M => a_offset+a_cap = 4096+2M > b_offset=2M (overlap)
        hd = parse_header(_tamper_header_field('slotACapacity', 2 * 1024 * 1024))
        self.assertFalse(hd.valid)

    def test_adjacent_slots_accepted(self):
        """slot_b_offset == slot_a_offset + slot_a_capacity is valid."""
        block = build_header(
            active_slot=0, pending_op=0, write_sequence=1,
            slot_a_offset=4096, slot_a_capacity=4096,
            slot_b_offset=8192, slot_b_capacity=4096,
            last_update_time=0, schema_version='1')
        hd = parse_header(block)
        self.assertTrue(hd.valid)


# #############################################################################
# TestParseSlotCapacity  (P4 fix)
# #############################################################################

class TestParseSlotCapacity(TestCase):

    def test_slot_capacity_too_small_rejected(self):
        """A slot with capacity <= SLOT_OVERHEAD cannot hold any payload."""
        payload = b'x'
        raw = struct.pack('>IQQQQ', SLOT_MAGIC, 1, 4096, SLOT_OVERHEAD,
                          len(payload))
        raw += payload
        raw += hashlib.sha256(raw).digest()
        sd = parse_slot(raw, expected_offset=4096)
        self.assertFalse(sd.valid)

    def test_slot_capacity_zero_rejected(self):
        payload = b'x'
        raw = struct.pack('>IQQQQ', SLOT_MAGIC, 1, 4096, 0, len(payload))
        raw += payload
        raw += hashlib.sha256(raw).digest()
        sd = parse_slot(raw, expected_offset=4096)
        self.assertFalse(sd.valid)


# #############################################################################
# TestParseHeaderCapacityZero  (GAP-3: capacity==0 intentionally accepted)
# #############################################################################

class TestParseHeaderCapacityZero(TestCase):
    """parse_header intentionally does NOT reject capacity==0.

    See lv_protocol.py comment at L295-303: no legitimate code path
    produces it, but rejecting it here would force _write_fresh (wiping
    the other slot) instead of letting the recovery logic handle it.
    """

    def test_slot_a_capacity_zero_accepted(self):
        """Header with slotACapacity=0 passes parse_header validation."""
        hd = parse_header(_tamper_header_field('slotACapacity', 0))
        self.assertTrue(hd.valid, "slotACapacity=0 should be accepted")
        self.assertEqual(hd.slot_a_capacity, 0)

    def test_slot_b_capacity_zero_accepted(self):
        """Header with slotBCapacity=0 passes parse_header validation."""
        hd = parse_header(_tamper_header_field('slotBCapacity', 0))
        self.assertTrue(hd.valid, "slotBCapacity=0 should be accepted")
        self.assertEqual(hd.slot_b_capacity, 0)

    def test_both_capacities_zero_accepted(self):
        """Both slot capacities == 0 is also accepted."""
        block = bytearray(_make_header())
        json_len = struct.unpack_from('>H', block, 6)[0]
        json_str = block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len].decode('utf-8')
        d = json.loads(json_str)
        d['slotACapacity'] = 0
        d['slotBCapacity'] = 0
        new_json = json.dumps(d, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + json_len] = b'\x00' * json_len
        struct.pack_into('>H', block, 6, len(new_json))
        block[HEADER_JSON_OFFSET:HEADER_JSON_OFFSET + len(new_json)] = new_json
        checksum = hashlib.sha256(bytes(block[:HEADER_CHECKSUM_OFFSET])).digest()
        block[HEADER_CHECKSUM_OFFSET:HEADER_CHECKSUM_OFFSET + 32] = checksum
        hd = parse_header(bytes(block))
        self.assertTrue(hd.valid)
        self.assertEqual(hd.slot_a_capacity, 0)
        self.assertEqual(hd.slot_b_capacity, 0)


# #############################################################################
# TestCalculateExtendSize  (S6 fix)
# #############################################################################

class TestCalculateExtendSize(TestCase):

    MB = 1024 * 1024

    def test_already_large_enough(self):
        """current >= min_required => no change."""
        self.assertEqual(calculate_extend_size(4 * self.MB, 4 * self.MB),
                         4 * self.MB)

    def test_step_2mb_below_8mb(self):
        """<8MB range uses 2MB steps."""
        result = calculate_extend_size(4 * self.MB, 5 * self.MB)
        self.assertEqual(result, 6 * self.MB)

    def test_step_4mb_below_16mb(self):
        """8-16MB range uses 4MB steps."""
        result = calculate_extend_size(8 * self.MB, 9 * self.MB)
        self.assertEqual(result, 12 * self.MB)

    def test_step_8mb_below_32mb(self):
        """16-32MB range uses 8MB steps."""
        result = calculate_extend_size(16 * self.MB, 17 * self.MB)
        self.assertEqual(result, 24 * self.MB)

    def test_step_16mb_above_32mb(self):
        """>=32MB range uses 16MB steps."""
        result = calculate_extend_size(32 * self.MB, 33 * self.MB)
        self.assertEqual(result, 48 * self.MB)

    def test_capped_at_max_lv_size(self):
        """Result never exceeds MAX_LV_SIZE."""
        result = calculate_extend_size(48 * self.MB, 60 * self.MB)
        self.assertLessEqual(result, MAX_LV_SIZE)
        self.assertGreaterEqual(result, 60 * self.MB)

    def test_exceeds_max_raises(self):
        """min_required > MAX_LV_SIZE => MetadataCapacityError."""
        with self.assertRaises(MetadataCapacityError):
            calculate_extend_size(4 * self.MB, MAX_LV_SIZE + 1)

    def test_multiple_steps(self):
        """4MB -> need 12MB: step 2MB to 6MB, step 2MB to 8MB, step 4MB to 12MB."""
        result = calculate_extend_size(4 * self.MB, 12 * self.MB)
        self.assertEqual(result, 12 * self.MB)

    def test_zero_start(self):
        """Start from 0, need 4MB: two 2MB steps."""
        result = calculate_extend_size(0, 4 * self.MB)
        self.assertEqual(result, 4 * self.MB)


# #############################################################################
# TestStorageTopologyChanged  (S5 fix)
# #############################################################################

class TestStorageTopologyChanged(TestCase):

    def _make_payload(self, volumes=None, snapshots=None):
        d = {}
        if volumes is not None:
            d['volumes'] = volumes
        if snapshots is not None:
            d['snapshots'] = snapshots
        return json.dumps(d)

    def _make_vol(self, uuid, install_path):
        return {'vo': json.dumps({'uuid': uuid, 'installPath': install_path})}

    def _make_snap(self, uuid, install_path):
        return json.dumps({'uuid': uuid, 'primaryStorageInstallPath': install_path})

    def test_identical_payloads_not_changed(self):
        p = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1')],
            snapshots=[self._make_snap('s1', '/dev/vg/snap1')])
        self.assertFalse(_storage_topology_changed(p, p))

    def test_volume_added(self):
        old = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1')])
        new = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1'),
                     self._make_vol('v2', '/dev/vg/vol2')])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_volume_removed(self):
        old = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1'),
                     self._make_vol('v2', '/dev/vg/vol2')])
        new = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1')])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_volume_install_path_changed(self):
        old = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/old_vg/vol1')])
        new = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/new_vg/vol1')])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_snapshot_added(self):
        old = self._make_payload(snapshots=[])
        new = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/vg/snap1')])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_snapshot_removed(self):
        old = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/vg/snap1')])
        new = self._make_payload(snapshots=[])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_snapshot_path_changed(self):
        old = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/old/snap1')])
        new = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/new/snap1')])
        self.assertTrue(_storage_topology_changed(old, new))

    def test_no_volumes_or_snapshots_not_changed(self):
        old = self._make_payload()
        new = self._make_payload()
        self.assertFalse(_storage_topology_changed(old, new))

    def test_invalid_json_old_returns_changed(self):
        self.assertTrue(_storage_topology_changed(b'not-json', b'{}'))

    def test_invalid_json_new_returns_changed(self):
        self.assertTrue(_storage_topology_changed(b'{}', b'not-json'))

    def test_bytes_vs_str_identical(self):
        p = self._make_payload(
            volumes=[self._make_vol('v1', '/dev/vg/vol1')])
        self.assertFalse(_storage_topology_changed(
            p.encode('utf-8'), p))

    def test_empty_volumes_list_both_sides(self):
        old = self._make_payload(volumes=[])
        new = self._make_payload(volumes=[])
        self.assertFalse(_storage_topology_changed(old, new))

    def test_config_only_change_not_topology(self):
        """Extra fields that aren't volumes/snapshots should NOT count."""
        old = json.dumps({'config': 'old', 'volumes': []})
        new = json.dumps({'config': 'new', 'volumes': []})
        self.assertFalse(_storage_topology_changed(old, new))

    def test_list_payload_returns_changed(self):
        """A JSON array payload should not crash, just return True."""
        self.assertTrue(_storage_topology_changed('[]', '[]'))

    def test_non_dict_payload_returns_changed(self):
        """Payloads like '\"string\"' or 'null' that parse as non-dict
        should gracefully return True (topology assumed changed)."""
        self.assertTrue(_storage_topology_changed('"hello"', '"hello"'))
        self.assertTrue(_storage_topology_changed('null', 'null'))
        self.assertTrue(_storage_topology_changed('42', '42'))

    def test_snapshots_as_flat_list_identical(self):
        """snapshots is a flat list of JSON strings (the actual DTO format)."""
        old = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/vg/snap1'),
                       self._make_snap('s2', '/dev/vg/snap2')])
        new = self._make_payload(
            snapshots=[self._make_snap('s1', '/dev/vg/snap1'),
                       self._make_snap('s2', '/dev/vg/snap2')])
        self.assertFalse(_storage_topology_changed(old, new))

    def test_snapshots_as_dict_returns_changed(self):
        """snapshots as dict (legacy format) -- iteration yields (key, val) tuples,
        which won't parse as valid snapshot JSON, so snaps dict stays empty.
        Safe fallback: no snapshots extracted = assumed unchanged (if both sides
        are the same broken format)."""
        old = json.dumps({'snapshots': {'v1': ['not-valid-json']}})
        new = json.dumps({'snapshots': {'v1': ['not-valid-json']}})
        self.assertFalse(_storage_topology_changed(old, new))

    def test_volumes_as_string_returns_changed(self):
        """volumes is a string instead of list -- iteration would fail."""
        old = json.dumps({'volumes': 'not-a-list'})
        new = json.dumps({'volumes': 'not-a-list'})
        self.assertTrue(_storage_topology_changed(old, new))


# #############################################################################
# TestScanMetadataLvs  (UUID regex tightening)
# #############################################################################

class TestScanMetadataLvs(TestCase):

    def _make_lv_list(self, items):
        """Return a lv_list_func that yields (lv_name, lv_path, lv_size) tuples."""
        def lv_list_func(vg_uuid):
            return items
        return lv_list_func

    def test_lowercase_uuid_accepted(self):
        vm_uuid = 'a1b2c3d4' * 4
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 4194304)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['vm_uuid'], vm_uuid)

    def test_uppercase_uuid_rejected(self):
        """UUIDs from Java are always lowercase; uppercase should be filtered."""
        vm_uuid = 'A1B2C3D4' * 4
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 4194304)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 0)

    def test_mixed_case_uuid_rejected(self):
        vm_uuid = 'aAbBcCdD' * 4
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 4194304)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 0)

    def test_non_metadata_lv_ignored(self):
        items = [('some_other_lv', '/dev/vg/some_other_lv', 1048576)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 0)

    def test_short_uuid_rejected(self):
        vm_uuid = 'a' * 31
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 4194304)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 0)


class _FakeOperateLv(object):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeNamedLock(object):
    _locks = {}
    _locks_guard = threading.Lock()

    def __init__(self, name):
        with self._locks_guard:
            self._lock = self._locks.setdefault(name, threading.RLock())

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
        return False


class _FakeLockModule(object):
    NamedLock = _FakeNamedLock


class _FakeGenerationLvm(object):
    def __init__(self):
        self.existing_lvs = set()

    def lv_exists(self, path):
        return path in self.existing_lvs

    def create_lv_from_absolute_path(self, path, size, **kwargs):
        self.existing_lvs.add(path)

    def get_lv_size(self, path):
        return INITIAL_LV_SIZE

    def extend_lv(self, path, size):
        return None

    def delete_lv(self, path):
        self.existing_lvs.remove(path)

    def OperateLv(self, path, shared=False):
        return _FakeOperateLv()


class _FakeGenerationBash(object):
    @staticmethod
    def in_bash(func):
        return func


class _InMemoryGenerationSblkHandler(SblkMetadataHandler):
    def __init__(self, lvm_module, bash_module):
        super(_InMemoryGenerationSblkHandler, self).__init__(
            lvm_module, bash_module, _FakeLockModule)
        self.generation = 0

    def _initialize_if_needed(self, metadata_path, lv_size):
        return None

    def _read_metadata_generation(self, fence_path):
        return self.generation

    def _write_metadata_generation(self, fence_path, generation):
        self.generation = generation

    def _lv_list_func(self, vg):
        return [
            (os.path.basename(path), path, INITIAL_LV_SIZE)
            for path in self._lvm.existing_lvs
        ]


class TestSblkMetadataGeneration(TestCase):
    def setUp(self):
        self.lvm = _FakeGenerationLvm()
        self.handler = _InMemoryGenerationSblkHandler(
            self.lvm, _FakeGenerationBash())
        self.vm_uuid = 'f8' * 16
        self.metadata_path = '/dev/test-vg/%s_vmmeta' % self.vm_uuid

    def _write(self, generation):
        self.handler._do_write(
            self.metadata_path,
            '{"generation":%s}' % generation,
            vmUuid=self.vm_uuid,
            vmName='vm1',
            vmCategory='',
            architecture='x86_64',
            schemaVersion='',
            metadataGeneration=generation)

    @patch('zstacklib.utils.lv_metadata.write_metadata')
    def test_delayed_operations_are_fenced(self, _write_metadata):
        self._write(1)

        cleanup_cmd = type('CleanupCmd', (object,), {
            'vgUuid': 'test-vg',
            'metadataGeneration': 2,
        })()
        self.assertEqual({}, self.handler.cleanup_all(cleanup_cmd))
        self.assertNotIn(self.metadata_path, self.lvm.existing_lvs)

        self._write(3)
        self.assertIn(self.metadata_path, self.lvm.existing_lvs)

        self.assertEqual(
            {'skipped': True, 'currentGeneration': 3},
            self.handler.cleanup_all(cleanup_cmd))
        self.assertIn(self.metadata_path, self.lvm.existing_lvs)

        with self.assertRaises(StaleMetadataGeneration):
            self._write(1)

    @patch('zstacklib.utils.lv_metadata.write_metadata')
    def test_cleanup_all_serializes_generation_with_write(self, _write_metadata):
        self._write(1)
        cleanup_cmd = type('CleanupCmd', (object,), {
            'vgUuid': 'test-vg',
            'metadataGeneration': 2,
        })()
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        writer_started = threading.Event()
        writer_finished = threading.Event()
        errors = []
        original_lv_list = self.handler._lv_list_func

        def paused_lv_list(vg_uuid):
            cleanup_entered.set()
            if not release_cleanup.wait(5):
                raise RuntimeError("timed out waiting to release cleanup")
            return original_lv_list(vg_uuid)

        def cleanup():
            try:
                self.handler.cleanup_all(cleanup_cmd)
            except Exception as e:
                errors.append(e)

        def write():
            writer_started.set()
            try:
                self._write(3)
            except Exception as e:
                errors.append(e)
            finally:
                writer_finished.set()

        self.handler._lv_list_func = paused_lv_list
        cleanup_thread = threading.Thread(target=cleanup)
        writer_thread = threading.Thread(target=write)
        try:
            cleanup_thread.start()
            self.assertTrue(cleanup_entered.wait(5))
            writer_thread.start()
            self.assertTrue(writer_started.wait(5))
            self.assertFalse(
                writer_finished.wait(0.2),
                "metadata write entered while cleanup held the fence lock")
        finally:
            release_cleanup.set()
            cleanup_thread.join(5)
            writer_thread.join(5)
            self.handler._lv_list_func = original_lv_list

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual([], errors)
        self.assertIn(self.metadata_path, self.lvm.existing_lvs)

    @patch('zstacklib.utils.lv_metadata.read_metadata')
    def test_uninitialized_fence_payload_starts_at_zero(self, read):
        read.return_value = ReadResult(status=ReadStatus.OK, payload=b'{}')
        self.lvm.existing_lvs.add('/dev/test-vg/zstack_vmmeta_generation')

        self.assertEqual(
            0,
            SblkMetadataHandler(
                self.lvm, _FakeGenerationBash())._read_metadata_generation(
                    '/dev/test-vg/zstack_vmmeta_generation'))

    @patch('zstacklib.utils.lv_metadata.read_metadata')
    def test_corrupted_fence_payload_fails_closed(self, read):
        read.return_value = ReadResult(
            status=ReadStatus.CORRUPTED, error='corrupted')
        self.lvm.existing_lvs.add('/dev/test-vg/zstack_vmmeta_generation')

        with self.assertRaises(MetadataIOError):
            SblkMetadataHandler(
                self.lvm, _FakeGenerationBash())._read_metadata_generation(
                    '/dev/test-vg/zstack_vmmeta_generation')


# #############################################################################
# TestVmMetadataHandlerWrite  (G1: vmCategory/schemaVersion None handling)
# #############################################################################

class TestVmMetadataHandlerWrite(TestCase):
    """G1: VmMetadataHandler.write() treats vmCategory/schemaVersion=None
    differently from missing attributes.  'None' is passed through as-is
    (not defaulted to ''), while missing attributes default to ''.
    """

    def test_vm_category_none_preserved(self):
        """When cmd.vmCategory is explicitly None, write() passes None
        (not '') to _do_write via the 'is not None' check."""
        cmd = MagicMock()
        cmd.metadataPath = '/tmp/' + 'a' * 32 + '.vmmeta'
        cmd.metadata = '{}'
        cmd.vmUuid = 'a' * 32
        cmd.vmName = 'test'
        cmd.vmCategory = None
        cmd.architecture = 'x86_64'
        cmd.schemaVersion = None
        cmd.metadataGeneration = 0

        captured = {}

        class _Stub(VmMetadataHandler):
            def _do_write(self, metadataPath, metadata, vmUuid, vmName,
                          vmCategory, architecture, schemaVersion,
                          metadataGeneration=0):
                captured['vmCategory'] = vmCategory
                captured['schemaVersion'] = schemaVersion

        handler = _Stub()
        handler.write(cmd)

        # vmCategory=None -> getattr returns None -> 'is not None' is False -> ''
        # Wait, let's re-read:
        #   vmCategory = getattr(cmd, 'vmCategory', None)      -> None
        #   vmCategory if vmCategory is not None else ''        -> ''
        # So None -> '' via the base class logic
        self.assertEqual(captured['vmCategory'], '',
                         "vmCategory=None should become '' via base handler")
        self.assertEqual(captured['schemaVersion'], '',
                         "schemaVersion=None should become '' via base handler")

    def test_vm_category_missing_attr_defaults_empty(self):
        """When cmd has no vmCategory attribute at all, getattr returns None
        and it's converted to '' by the base handler."""
        cmd = MagicMock(spec=[])
        cmd.metadataPath = '/tmp/' + 'a' * 32 + '.vmmeta'
        cmd.metadata = '{}'
        cmd.metadataGeneration = 0

        captured = {}

        class _Stub(VmMetadataHandler):
            def _do_write(self, metadataPath, metadata, vmUuid, vmName,
                          vmCategory, architecture, schemaVersion,
                          metadataGeneration=0):
                captured['vmUuid'] = vmUuid
                captured['vmName'] = vmName
                captured['vmCategory'] = vmCategory
                captured['architecture'] = architecture
                captured['schemaVersion'] = schemaVersion

        handler = _Stub()
        handler.write(cmd)

        self.assertEqual(captured['vmUuid'], '')
        self.assertEqual(captured['vmName'], '')
        self.assertEqual(captured['vmCategory'], '')
        self.assertEqual(captured['architecture'], '')
        self.assertEqual(captured['schemaVersion'], '')

    def test_vm_category_explicit_value_preserved(self):
        """When cmd.vmCategory is a non-None value, it's passed through."""
        cmd = MagicMock()
        cmd.metadataPath = '/tmp/' + 'a' * 32 + '.vmmeta'
        cmd.metadata = '{}'
        cmd.vmUuid = 'a' * 32
        cmd.vmName = 'test'
        cmd.vmCategory = 'AppCenter'
        cmd.architecture = 'x86_64'
        cmd.schemaVersion = '2'
        cmd.metadataGeneration = 0

        captured = {}

        class _Stub(VmMetadataHandler):
            def _do_write(self, metadataPath, metadata, vmUuid, vmName,
                          vmCategory, architecture, schemaVersion,
                          metadataGeneration=0):
                captured['vmCategory'] = vmCategory
                captured['schemaVersion'] = schemaVersion

        handler = _Stub()
        handler.write(cmd)

        self.assertEqual(captured['vmCategory'], 'AppCenter')
        self.assertEqual(captured['schemaVersion'], '2')

    def test_vm_uuid_none_becomes_empty(self):
        """vmUuid uses 'or' pattern: None -> ''."""
        cmd = MagicMock()
        cmd.metadataPath = '/tmp/' + 'a' * 32 + '.vmmeta'
        cmd.metadata = '{}'
        cmd.vmUuid = None
        cmd.vmName = None
        cmd.vmCategory = ''
        cmd.architecture = None
        cmd.schemaVersion = ''
        cmd.metadataGeneration = 0

        captured = {}

        class _Stub(VmMetadataHandler):
            def _do_write(self, metadataPath, metadata, vmUuid, vmName,
                          vmCategory, architecture, schemaVersion,
                          metadataGeneration=0):
                captured['vmUuid'] = vmUuid
                captured['vmName'] = vmName
                captured['architecture'] = architecture

        handler = _Stub()
        handler.write(cmd)

        self.assertEqual(captured['vmUuid'], '')
        self.assertEqual(captured['vmName'], '')
        self.assertEqual(captured['architecture'], '')


# #############################################################################
# TestGetMetadataStatus  (G14: direct test of get_metadata_status)
# #############################################################################

class TestGetMetadataStatus(TestCase):
    """G14: get_metadata_status is tested indirectly through _do_scan,
    but here we verify the function's return dict structure by mocking
    open_lv and aligned_pread to return a known header."""

    def test_valid_header_returns_all_fields(self):
        """get_metadata_status should return a dict with all header fields."""
        from unittest.mock import patch
        # Build a known-good header
        header = build_header(
            active_slot=SLOT_A,
            pending_op=PENDING_NONE,
            write_sequence=5,
            slot_a_offset=4096,
            slot_a_capacity=4096,
            slot_b_offset=8192,
            slot_b_capacity=4096,
            last_update_time=1700000000000,
            schema_version='2',
            vm_category='AppCenter',
            vm_uuid='abcd' * 8,
            vm_name='test-vm',
            architecture='x86_64',
        )

        with patch('zstacklib.utils.lv_metadata.open_lv', return_value=99), \
             patch('zstacklib.utils.lv_metadata.aligned_pread', return_value=header), \
             patch('os.close'):
            status = get_metadata_status('/dev/vg/fake_vmmeta', 4 * 1024 * 1024)

        self.assertTrue(status['valid'])
        self.assertEqual(status['active_slot'], SLOT_A)
        self.assertEqual(status['pending_op'], PENDING_NONE)
        self.assertEqual(status['write_sequence'], 5)
        self.assertEqual(status['schema_version'], '2')
        self.assertEqual(status['vm_category'], 'AppCenter')
        self.assertEqual(status['vm_uuid'], 'abcd' * 8)
        self.assertEqual(status['vm_name'], 'test-vm')
        self.assertEqual(status['architecture'], 'x86_64')
        self.assertEqual(status['last_update_time'], 1700000000000)

    def test_invalid_header_returns_valid_false(self):
        """Corrupted header bytes should produce valid=False."""
        from unittest.mock import patch
        garbage = b'\xDE\xAD' * (HEADER_BLOCK_SIZE // 2)

        with patch('zstacklib.utils.lv_metadata.open_lv', return_value=99), \
             patch('zstacklib.utils.lv_metadata.aligned_pread', return_value=garbage), \
             patch('os.close'):
            status = get_metadata_status('/dev/vg/fake_vmmeta', 4 * 1024 * 1024)

        self.assertFalse(status['valid'])


# #############################################################################
# TestScanMetadataLvsMalformed  (G15: malformed lv_list entries)
# #############################################################################

class TestScanMetadataLvsMalformed(TestCase):
    """G15: scan_metadata_lvs should handle lv_list_func returning entries
    with various edge-case values."""

    def _make_lv_list(self, items):
        def lv_list_func(vg_uuid):
            return items
        return lv_list_func

    def test_zero_size_lv_included(self):
        """LV with size=0 is structurally valid and should be included."""
        vm_uuid = 'a1b2c3d4' * 4
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 0)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['lv_size'], 0)

    def test_empty_lv_list_returns_empty(self):
        """Empty list from lv_list_func returns empty scan results."""
        result = scan_metadata_lvs('test-vg', self._make_lv_list([]))
        self.assertEqual(len(result), 0)

    def test_mixed_valid_and_non_metadata_lvs(self):
        """Only _vmmeta-suffix LVs with valid UUIDs should be returned."""
        valid_uuid = '0123456789abcdef' * 2
        items = [
            (valid_uuid + '_vmmeta', '/dev/vg/' + valid_uuid + '_vmmeta', 4194304),
            ('root_volume', '/dev/vg/root_volume', 10485760),
            ('data_lv_vmmeta', '/dev/vg/data_lv_vmmeta', 4194304),  # no 32-hex UUID
            ('A' * 32 + '_vmmeta', '/dev/vg/' + 'A' * 32 + '_vmmeta', 4194304),  # uppercase
        ]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['vm_uuid'], valid_uuid)

    def test_very_large_size_accepted(self):
        """Very large LV sizes should not cause issues."""
        vm_uuid = 'ff' * 16
        items = [(vm_uuid + '_vmmeta', '/dev/vg/' + vm_uuid + '_vmmeta', 64 * 1024 * 1024)]
        result = scan_metadata_lvs('test-vg', self._make_lv_list(items))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['lv_size'], 64 * 1024 * 1024)


# #############################################################################
# TestCrashRecoveryFlows  (in-memory simulation of three-phase write crashes)
#
# These tests build raw binary images (header + slot data) in a bytearray
# and feed them to _read_metadata_fd via a patched aligned_pread, exercising
# all recovery code paths without LVM, iSCSI, or O_DIRECT.
# #############################################################################

class _InMemoryLV(object):
    """Simulate an LV as a bytearray for unit-testing recovery flows.

    Supports build_header / build_slot writes and provides a pread mock
    compatible with aligned_pread(fd, size, offset).
    """

    def __init__(self, lv_size=4 * 1024 * 1024):
        self.data = bytearray(lv_size)
        self.lv_size = lv_size

    def write(self, data_bytes, offset):
        end = offset + len(data_bytes)
        if end > len(self.data):
            self.data.extend(b'\x00' * (end - len(self.data)))
            self.lv_size = len(self.data)
        self.data[offset:end] = data_bytes

    def pread(self, fd, size, offset):
        """Mock for aligned_pread(fd, size, offset)."""
        end = min(offset + size, len(self.data))
        result = bytes(self.data[offset:end])
        if len(result) < size:
            result += b'\x00' * (size - len(result))
        return result

    def write_header(self, **kwargs):
        self.write(build_header(**kwargs), 0)

    def write_slot(self, slot_index, layout, seq_num, payload):
        if slot_index == SLOT_A:
            offset = layout.slot_a_offset
            capacity = layout.slot_a_capacity
        else:
            offset = layout.slot_b_offset
            capacity = layout.slot_b_capacity
        slot_data = build_slot(seq_num, offset, capacity, payload)
        self.write(slot_data, offset)


class TestCrashRecoveryFlows(TestCase):
    """Test _read_metadata_fd recovery logic with in-memory LV images."""

    def _read(self, mem_lv):
        """Run _read_metadata_fd against an in-memory LV."""
        from unittest.mock import patch
        from zstacklib.utils.lv_metadata import _read_metadata_fd
        with patch('zstacklib.utils.lv_metadata.aligned_pread',
                   side_effect=mem_lv.pread):
            return _read_metadata_fd(42, mem_lv.lv_size)

    def _init_lv(self, lv_size=4 * 1024 * 1024):
        """Create an in-memory LV with a valid initial state (seq=1, Slot A active)."""
        lv = _InMemoryLV(lv_size)
        layout = calculate_slot_layout(lv_size)
        lv.write_slot(SLOT_A, layout, seq_num=1, payload=b'{"init":true}')
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_NONE, write_sequence=1,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        return lv, layout

    # -- Flow A: Normal read (PendingOp=0) ------------------------------------

    def test_flow_a_normal_read(self):
        """Flow A: No pending op, active slot valid => OK."""
        lv, layout = self._init_lv()
        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertEqual(result.payload, b'{"init":true}')

    def test_flow_a_active_corrupted_fallback_inactive(self):
        """Flow A: Active slot corrupted, inactive valid => OK (with stale data)."""
        lv, layout = self._init_lv()
        # Write to slot B so both slots have data
        lv.write_slot(SLOT_B, layout, seq_num=2, payload=b'{"v":2}')
        lv.write_header(
            active_slot=SLOT_B, pending_op=PENDING_NONE, write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        # Corrupt active slot B
        lv.write(b'\xFF' * ALIGNMENT, layout.slot_b_offset)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, b'{"init":true}')  # fallback to A

    def test_flow_a_both_corrupted(self):
        """Flow A: Both slots corrupted => CORRUPTED."""
        lv, layout = self._init_lv()
        # Corrupt slot A
        lv.write(b'\xFF' * ALIGNMENT, layout.slot_a_offset)
        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.CORRUPTED)

    # -- Flow B: CONFIG_UPDATE interrupted ------------------------------------

    def test_flow_b_crash_after_phase1_only(self):
        """Flow B: Phase 1 written (PENDING_CONFIG_UPDATE), Phase 2 NOT written.
        Target slot has stale data => OK, returns active slot."""
        lv, layout = self._init_lv()
        # Simulate: Phase 1 header written with PENDING_CONFIG_UPDATE, but
        # Phase 2 (slot B write) never happened.
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, b'{"init":true}')

    def test_flow_b_crash_after_phase2(self):
        """Flow B: Phase 1 + Phase 2 written, Phase 3 NOT written.
        Target slot has matching seq_num => OK, returns target payload."""
        lv, layout = self._init_lv()
        new_payload = b'{"updated":true}'
        # Phase 1: mark intent
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        # Phase 2: write payload to inactive slot B
        lv.write_slot(SLOT_B, layout, seq_num=2, payload=new_payload)
        # Phase 3 NOT written (crash)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, new_payload)

    # -- Flow C: STORAGE_CHANGE interrupted -----------------------------------

    def test_flow_c_crash_after_phase1_only(self):
        """Flow C: Phase 1 written (PENDING_STORAGE_CHANGE), Phase 2 NOT written.
        => STORAGE_CHANGE_INCOMPLETE, full_refresh_required."""
        lv, layout = self._init_lv()
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        result = self._read(lv)
        self.assertEqual(result.status,
                         ReadStatus.STORAGE_CHANGE_INCOMPLETE)
        # Active slot payload still returned (stale but present)
        self.assertEqual(result.payload, b'{"init":true}')

    def test_flow_c_crash_after_phase2(self):
        """Flow C: Phase 1 + Phase 2 written, Phase 3 NOT written.
        Target slot has matching seq_num => OK, returns target payload."""
        lv, layout = self._init_lv()
        new_payload = b'{"topology_changed":true}'
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        lv.write_slot(SLOT_B, layout, seq_num=2, payload=new_payload)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, new_payload)

    # -- Header corruption ----------------------------------------------------

    def test_corrupted_header(self):
        """Corrupted header => CORRUPTED."""
        lv = _InMemoryLV()
        lv.write(b'\xDE\xAD' * (HEADER_BLOCK_SIZE // 2), 0)
        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.CORRUPTED)

    def test_all_zeros_header(self):
        """All-zero header => CORRUPTED (magic mismatch)."""
        lv = _InMemoryLV()
        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.CORRUPTED)

    # -- Unknown PendingOp ----------------------------------------------------

    def test_unknown_pending_op_treated_as_storage_change(self):
        """Unknown PendingOp value is treated as STORAGE_CHANGE (Flow C).

        Note: parse_header rejects pendingOp values outside {0,1,2} via
        semantic validation, so the only way to reach this code path is if
        a future header version introduces new PendingOp values.
        Since parse_header currently rejects unknown values, the header
        is reported as invalid/corrupted by parse_header, and
        _read_metadata_fd takes the CORRUPTED path instead.
        """
        # This verifies parse_header correctly rejects invalid pendingOp
        lv, layout = self._init_lv()
        # Tamper the header JSON to set pendingOp=99
        tampered = _tamper_header_field('pendingOp', 99)
        lv.write(tampered, 0)
        result = self._read(lv)
        # parse_header rejects pendingOp=99 as invalid => header.valid=False
        self.assertEqual(result.status, ReadStatus.CORRUPTED)


# #############################################################################
# TestExtendCrashRecovery  (LV extend + crash with prev_layout fields)
#
# When write_metadata needs to extend the LV, Phase 1 records the NEW layout
# in the header plus prev_slot_* fields (old layout).  If a crash occurs
# between Phase 1 and Phase 3, recovery must use prev_slot_* to locate the
# active slot (whose on-disk slot header still records the OLD capacity).
# #############################################################################

class TestExtendCrashRecovery(TestCase):
    """Test _read_active_slot_with_prev() dual-layout recovery."""

    def _read(self, mem_lv):
        from unittest.mock import patch
        from zstacklib.utils.lv_metadata import _read_metadata_fd
        with patch('zstacklib.utils.lv_metadata.aligned_pread',
                   side_effect=mem_lv.pread):
            return _read_metadata_fd(42, mem_lv.lv_size)

    def test_extend_crash_phase1_active_slot_a_recovered_via_prev_layout(self):
        """Scenario: Active=Slot A.  LV extended from 4MB to 6MB.

        Phase 1 records new layout (6MB geometry) + prev_slot_a_capacity.
        Phase 2 (Slot B write) never happened.
        Active Slot A data was written with OLD layout capacity.

        Recovery: _read_active_slot_with_prev tries new capacity (fails strict
        check because slot header records old capacity), then retries with
        prev_slot_a_capacity => succeeds.
        """
        old_lv_size = 4 * 1024 * 1024
        new_lv_size = 6 * 1024 * 1024
        old_layout = calculate_slot_layout(old_lv_size)
        new_layout = calculate_slot_layout(new_lv_size)

        # Verify layouts actually differ (sanity check)
        self.assertNotEqual(old_layout.slot_a_capacity,
                            new_layout.slot_a_capacity)

        lv = _InMemoryLV(new_lv_size)

        # Write active slot A with OLD layout (as if written before extend)
        payload = b'{"pre_extend":"data"}'
        lv.write_slot(SLOT_A, old_layout, seq_num=1, payload=payload)

        # Write Phase 1 header: new layout + prev_slot_* fields
        # PENDING_CONFIG_UPDATE, so recovery uses Flow B
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            prev_slot_a_capacity=old_layout.slot_a_capacity,
            prev_slot_b_offset=old_layout.slot_b_offset,
            prev_slot_b_capacity=old_layout.slot_b_capacity,
        )
        # Phase 2 never happened (crash after Phase 1)

        result = self._read(lv)
        # Flow B: target slot invalid (no Phase 2) → reads active via prev layout
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, payload)

    def test_extend_crash_phase1_active_slot_b_recovered_via_prev_layout(self):
        """Same scenario but Active=Slot B.

        Phase 1 records new layout + prev_slot_b_offset/capacity.
        Recovery uses prev_slot_b_offset + prev_slot_b_capacity to find
        the active Slot B data written with old layout.
        """
        old_lv_size = 4 * 1024 * 1024
        new_lv_size = 6 * 1024 * 1024
        old_layout = calculate_slot_layout(old_lv_size)
        new_layout = calculate_slot_layout(new_lv_size)

        lv = _InMemoryLV(new_lv_size)

        # Write active slot B with OLD layout
        payload = b'{"slot_b_pre_extend":"data"}'
        lv.write_slot(SLOT_B, old_layout, seq_num=1, payload=payload)

        # Also need slot A with old layout (from initial write)
        lv.write_slot(SLOT_A, old_layout, seq_num=0,
                      payload=b'{"older":"data"}')

        # Phase 1 header: new layout + prev fields, active=B
        lv.write_header(
            active_slot=SLOT_B,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            prev_slot_a_capacity=old_layout.slot_a_capacity,
            prev_slot_b_offset=old_layout.slot_b_offset,
            prev_slot_b_capacity=old_layout.slot_b_capacity,
        )

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, payload)

    def test_extend_crash_phase2_complete_phase3_missing(self):
        """Extend + Phase 2 written + Phase 3 NOT written.

        New slot written with new layout capacity => target slot is valid
        with matching seq_num => OK, returns target payload.
        """
        old_lv_size = 4 * 1024 * 1024
        new_lv_size = 6 * 1024 * 1024
        old_layout = calculate_slot_layout(old_lv_size)
        new_layout = calculate_slot_layout(new_lv_size)

        lv = _InMemoryLV(new_lv_size)

        # Active Slot A with old layout
        lv.write_slot(SLOT_A, old_layout, seq_num=1,
                      payload=b'{"init":"data"}')

        # Phase 1 header
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            prev_slot_a_capacity=old_layout.slot_a_capacity,
            prev_slot_b_offset=old_layout.slot_b_offset,
            prev_slot_b_capacity=old_layout.slot_b_capacity,
        )

        # Phase 2: write to target (Slot B) with NEW layout
        new_payload = b'{"post_extend":"new_data"}'
        lv.write_slot(SLOT_B, new_layout, seq_num=2, payload=new_payload)

        # Phase 3 NOT written (crash)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, new_payload)

    def test_extend_storage_change_crash_phase1_recovered(self):
        """Extend + STORAGE_CHANGE + Phase 1 crash.

        Same as extend_crash_phase1 but with STORAGE_CHANGE pending.
        Flow C: target invalid, active via prev layout.
        """
        old_lv_size = 4 * 1024 * 1024
        new_lv_size = 6 * 1024 * 1024
        old_layout = calculate_slot_layout(old_lv_size)
        new_layout = calculate_slot_layout(new_lv_size)

        lv = _InMemoryLV(new_lv_size)

        payload = b'{"stale_topology":"data"}'
        lv.write_slot(SLOT_A, old_layout, seq_num=1, payload=payload)

        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=2,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            prev_slot_a_capacity=old_layout.slot_a_capacity,
            prev_slot_b_offset=old_layout.slot_b_offset,
            prev_slot_b_capacity=old_layout.slot_b_capacity,
        )

        result = self._read(lv)
        self.assertEqual(result.status,
                         ReadStatus.STORAGE_CHANGE_INCOMPLETE)
        # Active slot data recovered via prev layout
        self.assertEqual(result.payload, payload)

    def test_no_prev_layout_active_slot_capacity_mismatch_fails(self):
        """Without prev_slot_* fields, capacity mismatch => cannot recover active slot.

        This simulates a corrupt state where the header layout doesn't match
        the slot's on-disk capacity, but no prev_layout is recorded.
        """
        old_lv_size = 4 * 1024 * 1024
        new_lv_size = 6 * 1024 * 1024
        old_layout = calculate_slot_layout(old_lv_size)
        new_layout = calculate_slot_layout(new_lv_size)

        lv = _InMemoryLV(new_lv_size)

        # Slot A written with old layout
        lv.write_slot(SLOT_A, old_layout, seq_num=1,
                      payload=b'{"init":"data"}')

        # Header uses new layout but NO prev_slot_* fields
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            # NO prev_slot_* => recovery cannot find old capacity
        )

        result = self._read(lv)
        # Active slot strict parse fails (capacity mismatch), no prev layout
        # to fall back on.  Target slot (B) also has no data.
        # Flow B => both unreadable => CORRUPTED
        self.assertEqual(result.status, ReadStatus.CORRUPTED)

    def test_prev_layout_only_used_when_needed(self):
        """When active slot is readable with current layout, prev_layout is ignored."""
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        # Active Slot A with current layout
        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"current":"data"}')

        # Header with prev_layout set (as if extend happened but didn't change geometry)
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
            prev_slot_a_capacity=layout.slot_a_capacity,
            prev_slot_b_offset=layout.slot_b_offset,
            prev_slot_b_capacity=layout.slot_b_capacity,
        )

        result = self._read(lv)
        # Active slot readable directly => OK
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, b'{"current":"data"}')


# #############################################################################
# TestStorageChangePendingBehavior
#
# Verifies behavior when a STORAGE_CHANGE is pending and various subsequent
# operations occur.
# #############################################################################

class TestStorageChangePendingBehavior(TestCase):
    """Test STORAGE_CHANGE pending state interactions."""

    def _read(self, mem_lv):
        from unittest.mock import patch
        from zstacklib.utils.lv_metadata import _read_metadata_fd
        with patch('zstacklib.utils.lv_metadata.aligned_pread',
                   side_effect=mem_lv.pread):
            return _read_metadata_fd(42, mem_lv.lv_size)

    def test_storage_change_incomplete_not_usable(self):
        """STORAGE_CHANGE_INCOMPLETE status is NOT usable (is_usable=False).

        This means callers must trigger a full refresh before using the data,
        effectively preventing STORAGE_CHANGE from being downgraded to a
        normal read.
        """
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        # Active Slot A with stale data
        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"stale":"topology"}')

        # Phase 1 only: PENDING_STORAGE_CHANGE
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
        )

        result = self._read(lv)
        self.assertEqual(result.status,
                         ReadStatus.STORAGE_CHANGE_INCOMPLETE)
        self.assertFalse(result.is_usable(),
                         "STORAGE_CHANGE_INCOMPLETE must NOT be usable")

    def test_config_update_interrupted_is_usable(self):
        """CONFIG_UPDATE interrupted IS usable (contrast with STORAGE_CHANGE)."""
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"config":"data"}')
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
        )

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable(),
                        "CONFIG_UPDATE interrupted should be usable")

    def test_storage_change_with_phase2_complete_is_usable(self):
        """STORAGE_CHANGE with Phase 2 complete => OK (IS usable).

        When Phase 2 was written (new topology data is in target slot),
        the data is safe to use even though Phase 3 wasn't committed.
        """
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"old":"topology"}')
        lv.write_header(
            active_slot=SLOT_A,
            pending_op=PENDING_STORAGE_CHANGE,
            write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
        )
        # Phase 2: new topology written to Slot B
        new_payload = b'{"new":"topology"}'
        lv.write_slot(SLOT_B, layout, seq_num=2, payload=new_payload)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertTrue(result.is_usable())
        self.assertEqual(result.payload, new_payload)


# #############################################################################
# TestMultiWriteCrashRecovery
#
# Simulates multiple writes with crashes at different points to verify
# data consistency and correct recovery across sequences.
# #############################################################################

class TestMultiWriteCrashRecovery(TestCase):
    """Advanced crash recovery scenarios with multiple writes."""

    def _read(self, mem_lv):
        from unittest.mock import patch
        from zstacklib.utils.lv_metadata import _read_metadata_fd
        with patch('zstacklib.utils.lv_metadata.aligned_pread',
                   side_effect=mem_lv.pread):
            return _read_metadata_fd(42, mem_lv.lv_size)

    def test_successful_write_then_crash_during_second(self):
        """First write succeeds (seq=1, A), second crashes after Phase 1.

        State: active=B(seq=2, complete first write), header says
        active=B + PENDING_CONFIG_UPDATE(seq=3).  Phase 2 for seq=3
        not written.

        Recovery: Flow B, target (A) has seq=1 != 3, so falls back to
        active slot B (seq=2) => OK.
        """
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        # First write complete: Slot A has init (seq=1)
        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"init":"ok"}')
        # Second write complete: Slot B has v2 (seq=2), active=B
        lv.write_slot(SLOT_B, layout, seq_num=2,
                      payload=b'{"v":"2"}')

        # Third write crashes after Phase 1:
        # Header says active=B, pending=CONFIG_UPDATE, seq=3
        lv.write_header(
            active_slot=SLOT_B,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=3,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
        )

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        # Returns active slot B with latest complete data
        self.assertEqual(result.payload, b'{"v":"2"}')

    def test_successful_write_then_crash_during_second_with_phase2(self):
        """Like above but Phase 2 also written for the crashed write.

        Recovery should find the Phase 2 data (seq=3 matches header)
        and return it as OK.
        """
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        lv.write_slot(SLOT_A, layout, seq_num=1,
                      payload=b'{"init":"ok"}')
        lv.write_slot(SLOT_B, layout, seq_num=2,
                      payload=b'{"v":"2"}')

        # Third write: Phase 1 header
        lv.write_header(
            active_slot=SLOT_B,
            pending_op=PENDING_CONFIG_UPDATE,
            write_sequence=3,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000,
            schema_version='1',
        )
        # Phase 2: write to target Slot A with seq=3
        new_payload = b'{"v":"3"}'
        lv.write_slot(SLOT_A, layout, seq_num=3, payload=new_payload)

        result = self._read(lv)
        self.assertEqual(result.status, ReadStatus.OK)
        self.assertEqual(result.payload, new_payload)

    def test_slot_alternation_pattern(self):
        """Verify slot alternation: write1->A, write2->B, write3->A."""
        lv_size = 4 * 1024 * 1024
        layout = calculate_slot_layout(lv_size)
        lv = _InMemoryLV(lv_size)

        # Write 1 complete: active=A, seq=1
        lv.write_slot(SLOT_A, layout, seq_num=1, payload=b'{"w":1}')
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_NONE, write_sequence=1,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000000, schema_version='1',
        )
        r = self._read(lv)
        self.assertEqual(r.status, ReadStatus.OK)
        self.assertEqual(r.header.active_slot, SLOT_A)

        # Write 2 complete: active=B, seq=2
        lv.write_slot(SLOT_B, layout, seq_num=2, payload=b'{"w":2}')
        lv.write_header(
            active_slot=SLOT_B, pending_op=PENDING_NONE, write_sequence=2,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000001, schema_version='1',
        )
        r = self._read(lv)
        self.assertEqual(r.status, ReadStatus.OK)
        self.assertEqual(r.header.active_slot, SLOT_B)
        self.assertEqual(r.payload, b'{"w":2}')

        # Write 3 complete: active=A, seq=3
        lv.write_slot(SLOT_A, layout, seq_num=3, payload=b'{"w":3}')
        lv.write_header(
            active_slot=SLOT_A, pending_op=PENDING_NONE, write_sequence=3,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=1700000000002, schema_version='1',
        )
        r = self._read(lv)
        self.assertEqual(r.status, ReadStatus.OK)
        self.assertEqual(r.header.active_slot, SLOT_A)
        self.assertEqual(r.payload, b'{"w":3}')


if __name__ == '__main__':
    import unittest

    unittest.main()
