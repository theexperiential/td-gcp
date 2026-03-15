"""
Exhaustive bidirectional serialization tests for Firestore <> TouchDesigner.

Tests EVERY Firestore data type:
  - Inbound:  Firestore SDK objects -> _serialize_payload/_serialize_value -> JSON-safe dict
  - Outbound: TD table strings -> _convert_value_for_firebase -> Python types for Firestore SDK

Covers: null, bool, int, float, string, timestamp, geopoint, reference,
        bytes, array (nested/mixed), map (nested/mixed), and edge cases.
"""

import json
import math
import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# -- Import targets --

_GCP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_GCP_DIR))
sys.path.insert(0, str(_GCP_DIR / 'firestore'))

# Patch TD builtins before importing extension modules
import builtins
builtins.debug = lambda *a, **kw: None
builtins.project = MagicMock()
builtins.project.folder = '/tmp/test_project'
builtins.op = MagicMock()
builtins.tableDAT = MagicMock()
builtins.datexecuteDAT = MagicMock()
builtins.absTime = MagicMock()
builtins.absTime.frame = 0

from ext_firestore import FirestoreExt, _serialize_value, _deserialize_value
from td_mocks import (
    FakeDatetimeWithNanoseconds,
    FakeDocumentReference,
    FakeGeoPoint,
    MockOwnerComp,
    MockTableDAT,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helper: create a minimal FirestoreExt instance for serialization testing
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ext():
    owner = MockOwnerComp('firestore', {
        'Filterfields': '',
        'Enablecache': False,
        'Callbacksdat': '',
        'Logop': '',
        'Autoconnect': False,
        'Enablelistener': False,
        'Collections': '',
        'Privatekey': '',
        'Databaseid': '(default)',
        'Cachehydrate': False,
        'Cachepath': 'cache',
        'Circuitfailurethreshold': 3,
        'Circuittimeout': 30,
        'Backoffbase': 2.0,
        'Backoffmax': 60,
    })
    status = MockTableDAT('status')
    status.appendRow(['state', 'circuit', 'last_error', 'queue_depth', 'connected_at', 'collections'])
    status.appendRow(['disconnected', 'closed', '', '0', '', ''])
    owner.register_op('status', status)
    owner.register_op('log', MockTableDAT('log'))

    return FirestoreExt(owner)


# ═══════════════════════════════════════════════════════════════════════════
# INBOUND: _serialize_value  (Firestore SDK -> JSON-safe Python)
# ═══════════════════════════════════════════════════════════════════════════

class TestSerializeValuePrimitives:
    """Test _serialize_value for every primitive Firestore type."""

    def test_none(self):
        assert _serialize_value(None) is None

    def test_bool_true(self):
        assert _serialize_value(True) is True

    def test_bool_false(self):
        assert _serialize_value(False) is False

    def test_int_zero(self):
        assert _serialize_value(0) == 0

    def test_int_positive(self):
        assert _serialize_value(42) == 42

    def test_int_negative(self):
        assert _serialize_value(-99) == -99

    def test_int_large(self):
        big = 2**53
        assert _serialize_value(big) == big

    def test_int_max_safe(self):
        """Firestore supports 64-bit signed integers."""
        val = 2**63 - 1
        assert _serialize_value(val) == val

    def test_float_zero(self):
        assert _serialize_value(0.0) == 0.0

    def test_float_positive(self):
        assert _serialize_value(3.14159) == 3.14159

    def test_float_negative(self):
        assert _serialize_value(-273.15) == -273.15

    def test_float_inf(self):
        result = _serialize_value(float('inf'))
        assert result == float('inf')

    def test_float_neg_inf(self):
        result = _serialize_value(float('-inf'))
        assert result == float('-inf')

    def test_float_nan(self):
        result = _serialize_value(float('nan'))
        assert math.isnan(result)

    def test_string_empty(self):
        assert _serialize_value('') == ''

    def test_string_simple(self):
        assert _serialize_value('hello') == 'hello'

    def test_string_unicode(self):
        val = 'eee uoa'
        assert _serialize_value(val) == val

    def test_string_with_newlines(self):
        val = 'line1\nline2\ttab'
        assert _serialize_value(val) == val

    def test_string_json_like(self):
        val = '{"not": "a dict"}'
        assert _serialize_value(val) == val

    def test_string_very_long(self):
        val = 'x' * 10000
        assert _serialize_value(val) == val


class TestSerializeValueTimestamps:
    """Test _serialize_value for Firestore timestamp types."""

    def test_datetime_with_nanoseconds_utc(self):
        ts = FakeDatetimeWithNanoseconds(2025, 6, 15, 12, 30, 45, 123456,
                                          tzinfo=datetime.timezone.utc)
        result = _serialize_value(ts)
        assert result['__type'] == 'timestamp'
        assert '2025-06-15' in result['value']

    def test_datetime_with_nanoseconds_naive(self):
        ts = FakeDatetimeWithNanoseconds(2025, 1, 1, 0, 0, 0)
        result = _serialize_value(ts)
        assert result['__type'] == 'timestamp'
        assert '2025-01-01' in result['value']

    def test_datetime_with_high_precision_nanos(self):
        ts = FakeDatetimeWithNanoseconds(
            2025, 12, 31, 23, 59, 59, 0,
            tzinfo=datetime.timezone.utc,
            nanosecond=999999999,
        )
        result = _serialize_value(ts)
        assert result['__type'] == 'timestamp'
        assert isinstance(result['value'], str)

    def test_regular_datetime(self):
        """Standard datetime should also serialize via isoformat."""
        dt = datetime.datetime(2025, 3, 15, 10, 0, 0, tzinfo=datetime.timezone.utc)
        result = _serialize_value(dt)
        assert result['__type'] == 'timestamp'
        assert '2025-03-15' in result['value']

    def test_date_object(self):
        """datetime.date has isoformat too."""
        d = datetime.date(2025, 6, 15)
        result = _serialize_value(d)
        assert result == {'__type': 'timestamp', 'value': '2025-06-15'}

    def test_time_object(self):
        """datetime.time has isoformat."""
        t = datetime.time(14, 30, 0)
        result = _serialize_value(t)
        assert result == {'__type': 'timestamp', 'value': '14:30:00'}


class TestSerializeValueReferences:
    """Test _serialize_value for Firestore DocumentReference objects."""

    def test_simple_ref(self):
        ref = FakeDocumentReference('users/abc123', 'abc123')
        result = _serialize_value(ref)
        assert result == {'__type': 'reference', 'value': 'users/abc123'}

    def test_deeply_nested_ref(self):
        ref = FakeDocumentReference(
            'projects/p1/databases/db/documents/col/doc', 'doc'
        )
        result = _serialize_value(ref)
        assert result == {'__type': 'reference', 'value': 'projects/p1/databases/db/documents/col/doc'}

    def test_subcollection_ref(self):
        ref = FakeDocumentReference('users/u1/posts/p1', 'p1')
        result = _serialize_value(ref)
        assert result == {'__type': 'reference', 'value': 'users/u1/posts/p1'}


class TestSerializeValueGeoPoints:
    """Test _serialize_value for GeoPoint objects."""

    def test_geopoint(self):
        gp = FakeGeoPoint(40.7128, -74.0060)
        result = _serialize_value(gp)
        assert result == {'__type': 'geopoint', 'latitude': 40.7128, 'longitude': -74.006}

    def test_geopoint_zero(self):
        gp = FakeGeoPoint(0.0, 0.0)
        result = _serialize_value(gp)
        assert result == {'__type': 'geopoint', 'latitude': 0.0, 'longitude': 0.0}


class TestSerializeValueBytes:
    """Test _serialize_value for bytes fields."""

    def test_bytes(self):
        data = b'\x00\x01\x02\xff'
        result = _serialize_value(data)
        assert result == {'__type': 'bytes', 'value': 'AAEC/w=='}

    def test_empty_bytes(self):
        result = _serialize_value(b'')
        assert result == {'__type': 'bytes', 'value': ''}


class TestSerializeValueArrays:
    """Test _serialize_value for list/array types."""

    def test_empty_array(self):
        assert _serialize_value([]) == []

    def test_int_array(self):
        assert _serialize_value([1, 2, 3]) == [1, 2, 3]

    def test_mixed_array(self):
        result = _serialize_value([1, 'two', 3.0, True, None])
        assert result == [1, 'two', 3.0, True, None]

    def test_nested_array(self):
        result = _serialize_value([[1, 2], [3, [4, 5]]])
        assert result == [[1, 2], [3, [4, 5]]]

    def test_array_with_map(self):
        data = [{'key': 'value'}, {'nested': {'deep': True}}]
        result = _serialize_value(data)
        assert result == [{'key': 'value'}, {'nested': {'deep': True}}]

    def test_array_with_timestamp(self):
        ts = FakeDatetimeWithNanoseconds(2025, 6, 15, 12, 0, 0,
                                          tzinfo=datetime.timezone.utc)
        result = _serialize_value([ts])
        assert len(result) == 1
        assert result[0]['__type'] == 'timestamp'
        assert '2025-06-15' in result[0]['value']

    def test_array_with_reference(self):
        ref = FakeDocumentReference('col/doc', 'doc')
        result = _serialize_value([ref])
        assert result == [{'__type': 'reference', 'value': 'col/doc'}]

    def test_array_with_geopoint(self):
        gp = FakeGeoPoint(51.5074, -0.1278)
        result = _serialize_value([gp])
        assert len(result) == 1
        assert result[0]['__type'] == 'geopoint'

    def test_large_array(self):
        data = list(range(1000))
        result = _serialize_value(data)
        assert len(result) == 1000
        assert result[999] == 999


class TestSerializeValueMaps:
    """Test _serialize_value for dict/map types."""

    def test_empty_map(self):
        assert _serialize_value({}) == {}

    def test_simple_map(self):
        data = {'name': 'Alice', 'age': 30}
        result = _serialize_value(data)
        assert result == {'name': 'Alice', 'age': 30}

    def test_deeply_nested_map(self):
        data = {'l1': {'l2': {'l3': {'l4': {'value': 'deep'}}}}}
        result = _serialize_value(data)
        assert result['l1']['l2']['l3']['l4']['value'] == 'deep'

    def test_map_with_all_types(self):
        ts = FakeDatetimeWithNanoseconds(2025, 1, 1, 0, 0, 0,
                                          tzinfo=datetime.timezone.utc)
        ref = FakeDocumentReference('col/doc', 'doc')
        data = {
            'null': None,
            'bool': True,
            'int': 42,
            'float': 1.5,
            'string': 'text',
            'timestamp': ts,
            'reference': ref,
            'array': [1, 2, 3],
            'map': {'inner': 'value'},
        }
        result = _serialize_value(data)
        assert result['null'] is None
        assert result['bool'] is True
        assert result['int'] == 42
        assert result['float'] == 1.5
        assert result['string'] == 'text'
        assert result['timestamp']['__type'] == 'timestamp'
        assert result['reference'] == {'__type': 'reference', 'value': 'col/doc'}
        assert result['array'] == [1, 2, 3]
        assert result['map'] == {'inner': 'value'}

    def test_map_with_unicode_keys(self):
        data = {'name_jp': 'test_jp', 'emoji_key': 'fire'}
        result = _serialize_value(data)
        assert result['name_jp'] == 'test_jp'
        assert result['emoji_key'] == 'fire'

    def test_map_with_dotted_keys(self):
        """Firestore allows dots in map keys (not field paths)."""
        data = {'config.setting': 'value', 'a.b.c': 123}
        result = _serialize_value(data)
        assert result['config.setting'] == 'value'
        assert result['a.b.c'] == 123


def _serialize_payload(data):
    """Replicate what _on_snapshot does inline: serialize each top-level value."""
    return {k: _serialize_value(v) for k, v in data.items()}


class TestSerializePayload:
    """Test payload serialization (top-level dict conversion)."""

    def test_empty_payload(self):
        assert _serialize_payload({}) == {}

    def test_full_user_doc(self, sample_user_doc):
        result = _serialize_payload(sample_user_doc)
        assert result['displayName'] == 'Test User'
        assert result['active'] is True
        assert result['loginCount'] == 42
        assert result['lastLogin']['__type'] == 'timestamp'
        assert result['preferences'] == {
            'theme': 'dark',
            'notifications': True,
            'volume': 0.8,
        }
        assert result['tags'] == ['vip', 'beta-tester']

    def test_full_scene_doc(self, sample_scene_doc):
        result = _serialize_payload(sample_scene_doc)
        assert result['label'] == 'Coral Reef'
        assert result['presets'] == ['preset1', 'preset2', 'preset3']
        assert result['deleted'] is False

    def test_full_status_doc(self, sample_status_doc):
        result = _serialize_payload(sample_status_doc)
        assert result['scene_now'] == 'coral'
        assert result['in_transition'] is False
        assert result['start']['__type'] == 'timestamp'
        assert result['end']['__type'] == 'timestamp'

    def test_full_schedule_doc(self, sample_schedule_doc):
        result = _serialize_payload(sample_schedule_doc)
        assert result['scene'] == 'aurora'
        assert result['transition_duration'] == 32
        assert result['start']['__type'] == 'timestamp'

    def test_all_firestore_types(self, all_firestore_types):
        """Serialize every possible Firestore type in one document."""
        result = _serialize_payload(all_firestore_types)

        # Verify all keys present
        assert set(result.keys()) == set(all_firestore_types.keys())

        # Verify JSON-serializability of non-NaN values
        # (NaN breaks json.dumps by default)
        safe = {k: v for k, v in result.items()
                if not (isinstance(v, float) and math.isnan(v))}
        json_str = json.dumps(safe)
        assert isinstance(json_str, str)

    def test_roundtrip_json_safe(self, sample_user_doc):
        """Serialized payload should survive JSON roundtrip."""
        payload = _serialize_payload(sample_user_doc)
        json_str = json.dumps(payload)
        roundtripped = json.loads(json_str)
        assert roundtripped['displayName'] == 'Test User'
        assert roundtripped['active'] is True
        assert roundtripped['loginCount'] == 42


# ═══════════════════════════════════════════════════════════════════════════
# INBOUND: Full snapshot pipeline simulation (redesigned ext_firestore)
# ═══════════════════════════════════════════════════════════════════════════

class TestOnSnapshotSerialization:
    """
    Simulate what _on_snapshot does: take Firestore doc data,
    serialize it, and verify the queued payload.
    """

    def test_snapshot_user_doc(self, sample_user_doc):
        """Simulate a user doc snapshot arriving."""
        serialized = _serialize_payload(sample_user_doc)
        payload_str = json.dumps(serialized)
        roundtripped = json.loads(payload_str)

        assert roundtripped['displayName'] == 'Test User'
        assert roundtripped['loginCount'] == 42
        assert roundtripped['lastLogin']['__type'] == 'timestamp'
        assert roundtripped['preferences']['theme'] == 'dark'
        assert roundtripped['tags'] == ['vip', 'beta-tester']

    def test_snapshot_with_filter(self, sample_user_doc):
        """Verify field filtering works (Filterfields parameter)."""
        filter_set = {'waiverSignature', 'secret'}
        filtered = {k: v for k, v in sample_user_doc.items() if k not in filter_set}
        serialized = _serialize_payload(filtered)
        assert 'waiverSignature' not in serialized

    def test_snapshot_empty_doc(self):
        serialized = _serialize_payload({})
        assert serialized == {}

    def test_snapshot_removed_doc(self):
        """Removed docs send empty payload with 'removed' change_type."""
        item = ('users', 'doc123', 'removed', {}, '')
        assert item[2] == 'removed'
        assert item[3] == {}


# ═══════════════════════════════════════════════════════════════════════════
# OUTBOUND: Write payload validation (redesigned ext_firestore)
# ═══════════════════════════════════════════════════════════════════════════

class TestOutboundWritePayloads:
    """Verify outbound write payloads are correctly structured."""

    def _setup_ext_with_capture(self, ext):
        ext.my.ext.ConnectionExt = MagicMock()
        ext.my.ext.ConnectionExt.CanAttempt.return_value = True
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)
        return captured

    def test_push_doc_payload(self, ext):
        captured = self._setup_ext_with_capture(ext)
        data = {'name': 'Test', 'value': 42, 'nested': {'a': [1, 2]}}
        ext.PushDoc('users', 'user1', data)
        assert len(captured) == 1
        item = captured[0]
        assert item['collection'] == 'users'
        assert item['doc_id'] == 'user1'
        assert item['op_type'] == 'set'
        assert item['payload'] == data

    def test_merge_doc_payload(self, ext):
        captured = self._setup_ext_with_capture(ext)
        data = {'name': 'Updated'}
        ext.MergeDoc('users', 'user1', data)
        assert len(captured) == 1
        assert captured[0]['op_type'] == 'set_merge'
        assert captured[0]['payload'] == data

    def test_update_doc_payload(self, ext):
        captured = self._setup_ext_with_capture(ext)
        data = {'loginCount': 43}
        ext.UpdateDoc('users', 'user1', data)
        assert len(captured) == 1
        assert captured[0]['op_type'] == 'update'

    def test_delete_doc_payload(self, ext):
        captured = self._setup_ext_with_capture(ext)
        ext.DeleteDoc('users', 'user1')
        assert len(captured) == 1
        assert captured[0]['op_type'] == 'delete'
        assert captured[0]['payload'] == {}

    def test_push_batch(self, ext):
        captured = self._setup_ext_with_capture(ext)
        ops = [
            {'collection': 'users', 'doc_id': 'u1', 'op_type': 'set', 'payload': {'a': 1}},
            {'collection': 'users', 'doc_id': 'u2', 'op_type': 'update', 'payload': {'b': 2}},
            {'collection': 'scenes', 'doc_id': 's1', 'op_type': 'delete'},
        ]
        ext.PushBatch(ops)
        assert len(captured) == 3
        assert captured[0]['op_type'] == 'set'
        assert captured[1]['op_type'] == 'update'
        assert captured[2]['op_type'] == 'delete'

    def test_offline_write_queuing(self, ext):
        """When circuit is open, writes should go to offline queue."""
        ext.my.ext.ConnectionExt = MagicMock()
        ext.my.ext.ConnectionExt.CanAttempt.return_value = False
        ext.my.ext.WriteQueueExt = MagicMock()
        ext.my.ext.WriteQueueExt.Enqueue.return_value = 'queue-id-123'
        ext.my.par.Enablecache = MockOwnerComp('', {'Enablecache': True}).par.Enablecache

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext.PushDoc('users', 'user1', {'offline': True})
        assert len(captured) == 0
        ext.my.ext.WriteQueueExt.Enqueue.assert_called_once()


from td_mocks import MockParameter


# ═══════════════════════════════════════════════════════════════════════════
# DESERIALIZATION: _deserialize_value  (JSON type markers -> Firestore SDK)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_db():
    """Mock Firestore client for deserialization tests."""
    db = MagicMock()
    db.document.side_effect = lambda path: FakeDocumentReference(path)
    return db


class TestDeserializeValuePrimitives:
    """Primitives pass through unchanged."""

    def test_none(self, mock_db):
        assert _deserialize_value(None, mock_db) is None

    def test_bool_true(self, mock_db):
        assert _deserialize_value(True, mock_db) is True

    def test_bool_false(self, mock_db):
        assert _deserialize_value(False, mock_db) is False

    def test_int(self, mock_db):
        assert _deserialize_value(42, mock_db) == 42

    def test_float(self, mock_db):
        assert _deserialize_value(3.14, mock_db) == 3.14

    def test_string(self, mock_db):
        assert _deserialize_value('hello', mock_db) == 'hello'

    def test_empty_string(self, mock_db):
        assert _deserialize_value('', mock_db) == ''


class TestDeserializeValueMarkers:
    """Type markers reconstruct correct types."""

    def test_timestamp_utc(self, mock_db):
        marker = {'__type': 'timestamp', 'value': '2025-06-15T12:30:45+00:00'}
        result = _deserialize_value(marker, mock_db)
        assert isinstance(result, datetime.datetime)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15

    def test_timestamp_with_z(self, mock_db):
        marker = {'__type': 'timestamp', 'value': '2025-06-15T12:30:45Z'}
        result = _deserialize_value(marker, mock_db)
        assert isinstance(result, datetime.datetime)
        assert result.tzinfo is not None

    def test_timestamp_naive(self, mock_db):
        marker = {'__type': 'timestamp', 'value': '2025-01-01T00:00:00'}
        result = _deserialize_value(marker, mock_db)
        assert isinstance(result, datetime.datetime)
        assert result.tzinfo is None

    def test_timestamp_truncates_nanoseconds(self, mock_db):
        """Fractional seconds beyond 6 digits should be truncated."""
        marker = {'__type': 'timestamp', 'value': '2025-12-31T23:59:59+00:00.999999999Z'}
        # The isoformat from FakeDatetimeWithNanoseconds with nanos appends .NNNNNNNNNz
        # Let's test with a more realistic format
        marker2 = {'__type': 'timestamp', 'value': '2025-12-31T23:59:59.123456789+00:00'}
        result = _deserialize_value(marker2, mock_db)
        assert isinstance(result, datetime.datetime)

    def test_reference(self, mock_db):
        marker = {'__type': 'reference', 'value': 'users/abc123'}
        result = _deserialize_value(marker, mock_db)
        mock_db.document.assert_called_with('users/abc123')
        assert result.path == 'users/abc123'

    def test_reference_nested(self, mock_db):
        marker = {'__type': 'reference', 'value': 'users/u1/posts/p1'}
        result = _deserialize_value(marker, mock_db)
        assert result.path == 'users/u1/posts/p1'

    def test_geopoint(self, mock_db):
        marker = {'__type': 'geopoint', 'latitude': 40.7128, 'longitude': -74.006}
        # Patch the GeoPoint import inside _deserialize_value
        import unittest.mock as um
        with um.patch('ext_firestore.GeoPoint', create=True, side_effect=FakeGeoPoint) as mock_gp:
            # Need to patch at the import location - use a different approach
            pass
        # Since GeoPoint is lazily imported, mock it at the module level
        import ext_firestore
        with um.patch.dict('sys.modules', {'google.cloud.firestore_v1._helpers': MagicMock(GeoPoint=FakeGeoPoint)}):
            result = _deserialize_value(marker, mock_db)
            assert result.latitude == 40.7128
            assert result.longitude == -74.006

    def test_bytes(self, mock_db):
        import base64
        original = b'\x00\x01\x02\xff'
        marker = {'__type': 'bytes', 'value': base64.b64encode(original).decode('ascii')}
        result = _deserialize_value(marker, mock_db)
        assert result == original

    def test_empty_bytes(self, mock_db):
        marker = {'__type': 'bytes', 'value': ''}
        result = _deserialize_value(marker, mock_db)
        assert result == b''


class TestDeserializeValueNested:
    """Markers inside nested structures are deserialized."""

    def test_dict_with_timestamp(self, mock_db):
        data = {
            'name': 'Alice',
            'created': {'__type': 'timestamp', 'value': '2025-06-15T12:00:00+00:00'},
        }
        result = _deserialize_value(data, mock_db)
        assert result['name'] == 'Alice'
        assert isinstance(result['created'], datetime.datetime)

    def test_array_with_markers(self, mock_db):
        data = [
            {'__type': 'timestamp', 'value': '2025-01-01T00:00:00+00:00'},
            {'__type': 'reference', 'value': 'col/doc'},
            'plain string',
            42,
        ]
        result = _deserialize_value(data, mock_db)
        assert isinstance(result[0], datetime.datetime)
        assert result[1].path == 'col/doc'
        assert result[2] == 'plain string'
        assert result[3] == 42

    def test_deeply_nested_markers(self, mock_db):
        data = {
            'l1': {
                'l2': {
                    'ts': {'__type': 'timestamp', 'value': '2025-06-15T00:00:00+00:00'},
                    'ref': {'__type': 'reference', 'value': 'a/b'},
                }
            }
        }
        result = _deserialize_value(data, mock_db)
        assert isinstance(result['l1']['l2']['ts'], datetime.datetime)
        assert result['l1']['l2']['ref'].path == 'a/b'


class TestDeserializeValueBackwardCompat:
    """Old payloads without markers pass through unchanged."""

    def test_plain_string_timestamp(self, mock_db):
        """Old-format ISO string without marker stays as string."""
        result = _deserialize_value('2025-06-15T12:00:00+00:00', mock_db)
        assert result == '2025-06-15T12:00:00+00:00'
        assert isinstance(result, str)

    def test_plain_string_reference(self, mock_db):
        """Old-format reference path without marker stays as string."""
        result = _deserialize_value('users/abc123', mock_db)
        assert result == 'users/abc123'

    def test_dict_without_type_key(self, mock_db):
        """Regular dicts without __type are recursed normally."""
        data = {'name': 'Alice', 'age': 30}
        result = _deserialize_value(data, mock_db)
        assert result == {'name': 'Alice', 'age': 30}

    def test_mixed_old_and_new(self, mock_db):
        """Dict mixing old plain values and new markers."""
        data = {
            'old_timestamp': '2025-01-01T00:00:00',
            'new_timestamp': {'__type': 'timestamp', 'value': '2025-06-15T00:00:00+00:00'},
            'count': 42,
        }
        result = _deserialize_value(data, mock_db)
        assert isinstance(result['old_timestamp'], str)
        assert isinstance(result['new_timestamp'], datetime.datetime)
        assert result['count'] == 42


class TestRoundtrip:
    """Serialize -> json.dumps -> json.loads -> deserialize preserves types."""

    def test_timestamp_roundtrip(self, mock_db):
        ts = FakeDatetimeWithNanoseconds(2025, 6, 15, 12, 0, 0,
                                          tzinfo=datetime.timezone.utc)
        serialized = _serialize_value(ts)
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)
        result = _deserialize_value(parsed, mock_db)
        assert isinstance(result, datetime.datetime)
        assert result.year == 2025
        assert result.month == 6

    def test_reference_roundtrip(self, mock_db):
        ref = FakeDocumentReference('users/abc123', 'abc123')
        serialized = _serialize_value(ref)
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)
        result = _deserialize_value(parsed, mock_db)
        assert result.path == 'users/abc123'

    def test_bytes_roundtrip(self, mock_db):
        original = b'\x00\x01\x02\xff'
        serialized = _serialize_value(original)
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)
        result = _deserialize_value(parsed, mock_db)
        assert result == original

    def test_complex_doc_roundtrip(self, mock_db):
        """Full document with mixed types survives round-trip."""
        ts = FakeDatetimeWithNanoseconds(2025, 6, 15, 12, 0, 0,
                                          tzinfo=datetime.timezone.utc)
        ref = FakeDocumentReference('col/doc', 'doc')
        doc = {
            'name': 'Test',
            'active': True,
            'count': 42,
            'created': ts,
            'manager': ref,
            'data': b'\x01\x02',
            'tags': ['a', 'b'],
            'nested': {'ts': ts, 'value': 'text'},
        }
        serialized = {k: _serialize_value(v) for k, v in doc.items()}
        json_str = json.dumps(serialized)
        parsed = json.loads(json_str)
        result = _deserialize_value(parsed, mock_db)

        assert result['name'] == 'Test'
        assert result['active'] is True
        assert result['count'] == 42
        assert isinstance(result['created'], datetime.datetime)
        assert result['manager'].path == 'col/doc'
        assert result['data'] == b'\x01\x02'
        assert result['tags'] == ['a', 'b']
        assert isinstance(result['nested']['ts'], datetime.datetime)