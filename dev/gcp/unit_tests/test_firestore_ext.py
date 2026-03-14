"""
Integration tests for the redesigned FirestoreExt (ext_firestore.py).

Tests the main extension's queue processing, inbound/outbound pipelines,
version tracking, self-echo prevention, table management, and lifecycle.
All Firebase/TD interactions are mocked -- only the extension logic is tested.
"""

import json
import sys
import queue
import datetime
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'firestore'))

import builtins
builtins.debug = lambda *a, **kw: None
builtins.project = MagicMock()
builtins.project.folder = '/tmp/test_project'
builtins.op = MagicMock()
builtins.tableDAT = MagicMock()
builtins.datexecDAT = MagicMock()
builtins.run = MagicMock()

from ext_firestore import FirestoreExt, _execute_write
from td_mocks import (
    MockOwnerComp, MockTableDAT, MockParameter, MockCell,
    FakeDatetimeWithNanoseconds, FakeDocumentReference,
    FakeDocumentChange, FakeDocumentSnapshot, FakeWriteResult,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ext():
    """A fully wired FirestoreExt with mocked sub-extensions."""
    owner = MockOwnerComp('firestore', {
        'Filterfields': '',
        'Filterfieldsmode': 'exclude',
        'Filterdocs': '',
        'Filterdocsmode': 'include',
        'Enablecache': True,
        'Callbacksdat': '',
        'Logop': '',
        'Autoconnect': False,
        'Enablelistener': False,
        'Collections': 'users scenes',
        'Privatekey': '/fake/key.json',
        'Databaseid': '(default)',
        'Cachehydrate': False,
        'Cachepath': 'cache',
        'Enablewriteback': True,
        'Circuitfailurethreshold': 3,
        'Circuittimeout': 30,
        'Backoffbase': 2.0,
        'Backoffmax': 60,
    })

    # Status DAT
    status = MockTableDAT('status')
    status.appendRow(['state', 'circuit', 'last_error', 'queue_depth', 'connected_at', 'collections'])
    status.appendRow(['disconnected', 'closed', '', '0', '', ''])
    owner.register_op('status', status)
    owner.register_op('log', MockTableDAT('log'))

    # Collections COMP
    collections_comp = MagicMock()
    def create_table(op_type, name):
        if op_type is builtins.tableDAT:
            dat = MockTableDAT(name)
            owner.register_op(f'collections/{name}', dat)
            return dat
        # datexecDAT or other types -- return a MagicMock
        mock_op = MagicMock(name=f'mock_{name}')
        mock_op.name = name
        return mock_op
    collections_comp.create = create_table
    owner.register_op('collections', collections_comp)

    # Mock sub-extensions
    conn_ext = MagicMock()
    conn_ext.CanAttempt.return_value = True
    conn_ext.GetState.return_value = 'closed'
    conn_ext.GetBackoffSeconds.return_value = 1.0
    owner.ext.ConnectionExt = conn_ext

    wq_ext = MagicMock()
    wq_ext.Count.return_value = 0
    owner.ext.WriteQueueExt = wq_ext

    bootstrap_ext = MagicMock()
    owner.ext.BootstrapExt = bootstrap_ext

    e = FirestoreExt(owner)
    return e


# ═══════════════════════════════════════════════════════════════════════════
# Inbound queue processing (_process_inbound via _drain_inbound)
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessInbound:
    def test_added_doc_written_to_table(self, ext):
        """An 'added' change should create a row in the collection tableDAT."""
        payload = {'name': 'Alice', 'age': 30}
        item = ('users', 'user1', 'added', payload, '2025-06-15T12:00:00Z')
        ext._inbound_queue.put(item)
        ext._drain_inbound()

        dat = ext.my.op('collections/users')
        assert dat is not None
        assert dat.numRows == 2  # header + 1 data row
        # Verify payload stored as JSON string
        stored = dat[1, 'payload'].val
        assert json.loads(stored) == payload

    def test_modified_doc_updates_existing_row(self, ext):
        """A 'modified' change should update the existing row."""
        # First add
        ext._inbound_queue.put(('users', 'user1', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        # Then modify with newer version
        ext._inbound_queue.put(('users', 'user1', 'modified', {'v': 2}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        dat = ext.my.op('collections/users')
        assert dat.numRows == 2  # Still just 1 data row
        stored = json.loads(dat[1, 'payload'].val)
        assert stored['v'] == 2

    def test_removed_doc_deletes_row(self, ext):
        """A 'removed' change should delete the row."""
        ext._inbound_queue.put(('users', 'user1', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._drain_inbound()
        assert ext.my.op('collections/users').numRows == 2

        ext._inbound_queue.put(('users', 'user1', 'removed', {}, ''))
        ext._drain_inbound()
        assert ext.my.op('collections/users').numRows == 1  # Only header

    def test_version_tracking_prevents_self_echo(self, ext):
        """If inbound update_time <= stored version, skip it."""
        ext._inbound_queue.put(('users', 'user1', 'added', {'v': 1}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        # Same or older timestamp should be skipped
        ext._inbound_queue.put(('users', 'user1', 'modified', {'v': 99}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        dat = ext.my.op('collections/users')
        stored = json.loads(dat[1, 'payload'].val)
        assert stored['v'] == 1  # NOT updated

    def test_newer_version_passes(self, ext):
        """Newer update_time should pass version check."""
        ext._inbound_queue.put(('users', 'user1', 'added', {'v': 1}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        ext._inbound_queue.put(('users', 'user1', 'modified', {'v': 2}, '2025-01-01T00:00:02'))
        ext._drain_inbound()

        dat = ext.my.op('collections/users')
        stored = json.loads(dat[1, 'payload'].val)
        assert stored['v'] == 2

    def test_error_signal_recorded(self, ext):
        """__error__ items should trigger ConnectionExt.RecordFailure."""
        ext._inbound_queue.put(('__error__', 'users', 'error', {}, 'Connection refused'))
        ext._drain_inbound()
        ext.my.ext.ConnectionExt.RecordFailure.assert_called_once()

    def test_max_items_per_tick(self, ext):
        """_drain_inbound should process at most MAX_INBOUND_PER_FRAME items."""
        for i in range(20):
            ext._inbound_queue.put(('users', f'u{i}', 'added', {'i': i}, f'2025-01-01T00:00:{i:02d}'))

        ext._drain_inbound()
        # Should have processed MAX_INBOUND_PER_FRAME = 10
        assert ext._inbound_queue.qsize() == 10

    def test_multiple_collections(self, ext):
        """Items for different collections go to different tableDATs."""
        ext._inbound_queue.put(('users', 'u1', 'added', {'type': 'user'}, '2025-01-01T00:00:00'))
        ext._inbound_queue.put(('scenes', 's1', 'added', {'type': 'scene'}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        users_dat = ext.my.op('collections/users')
        scenes_dat = ext.my.op('collections/scenes')
        assert users_dat.numRows == 2
        assert scenes_dat.numRows == 2

    def test_cache_save_on_inbound(self, ext):
        """When Enablecache is True, inbound docs should be saved to cache."""
        ext._inbound_queue.put(('users', 'u1', 'added', {'cached': True}, '2025-01-01T00:00:00'))
        ext._drain_inbound()
        ext.my.ext.WriteQueueExt.SaveDocument.assert_called_once()

    def test_callback_fired_on_change(self, ext):
        """onFirestoreChange callback should fire for inbound changes."""
        ext.my.par.Callbacksdat = MockParameter('callbacks_dat')
        cb_dat = MagicMock()
        builtins.op = MagicMock(return_value=cb_dat)

        ext._inbound_queue.put(('users', 'u1', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        cb_dat.run.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# Write result processing (_process_write_result via hooks)
# ═══════════════════════════════════════════════════════════════════════════

class TestProcessWriteResult:
    def test_success_result_stores_version(self, ext):
        """Successful write should store version for echo prevention."""
        result = {
            'queue_id': 'qid1', 'doc_id': 'user1', 'collection': 'users',
            'success': True, 'update_time': '2025-01-01T00:00:05', 'error': None,
        }
        ext._process_write_result(result)
        assert ext._versions[('users', 'user1')] == '2025-01-01T00:00:05'

    def test_success_removes_from_queue(self, ext):
        """Successful write with queue_id should remove from WriteQueueExt."""
        result = {
            'queue_id': 'qid1', 'doc_id': 'user1', 'collection': 'users',
            'success': True, 'update_time': '2025-01-01T00:00:05', 'error': None,
        }
        ext._process_write_result(result)
        ext.my.ext.WriteQueueExt.Remove.assert_called_with('qid1')

    def test_failure_records_retry(self, ext):
        """Failed write with queue_id should record retry."""
        result = {
            'queue_id': 'qid1', 'doc_id': 'user1', 'collection': 'users',
            'success': False, 'update_time': '', 'error': 'timeout',
        }
        ext._process_write_result(result)
        ext.my.ext.WriteQueueExt.RecordRetry.assert_called_with('qid1', 'timeout')

    def test_failure_records_connection_failure(self, ext):
        """Failed write should trigger ConnectionExt.RecordFailure."""
        result = {
            'queue_id': '', 'doc_id': 'user1', 'collection': 'users',
            'success': False, 'update_time': '', 'error': 'connection error',
        }
        ext._process_write_result(result)
        ext.my.ext.ConnectionExt.RecordFailure.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# Bootstrap / reconnect hooks (ThreadManager callbacks)
# ═══════════════════════════════════════════════════════════════════════════

class TestSignalHandling:
    def test_bootstrap_ok_imports_firebase(self, ext):
        """_on_bootstrap_success should attempt Firebase import."""
        with patch.object(ext, '_import_firebase') as mock_import:
            ext._on_bootstrap_success()
            mock_import.assert_called_once()

    def test_bootstrap_fail_sets_error_status(self, ext):
        ext._on_bootstrap_error('uv not found')
        status = ext.my.op('status')
        assert status[1, 'state'].val == 'error'
        assert 'uv not found' in status[1, 'last_error'].val

    def test_reconnect_signal_calls_connect(self, ext):
        with patch.object(ext, 'Connect') as mock_connect:
            ext._on_reconnect_done()
            mock_connect.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Outbound write routing
# ═══════════════════════════════════════════════════════════════════════════

class TestOutboundRouting:
    def _setup_capture(self, ext):
        """Mock _submit_write_item to capture items instead of using ThreadManager."""
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)
        return captured

    def test_online_write_goes_to_submit(self, ext):
        captured = self._setup_capture(ext)
        ext.PushDoc('users', 'u1', {'online': True})
        assert len(captured) == 1
        assert captured[0]['collection'] == 'users'
        assert captured[0]['op_type'] == 'set'

    def test_offline_write_goes_to_sqlite(self, ext):
        ext.my.ext.ConnectionExt.CanAttempt.return_value = False
        ext.my.ext.WriteQueueExt.Enqueue.return_value = 'qid-offline'

        captured = self._setup_capture(ext)
        ext.PushDoc('users', 'u1', {'offline': True})
        assert len(captured) == 0
        ext.my.ext.WriteQueueExt.Enqueue.assert_called_once_with(
            'users', 'u1', 'set', {'offline': True}
        )

    def test_all_write_types(self, ext):
        captured = self._setup_capture(ext)
        ext.PushDoc('c', 'd', {'a': 1})
        ext.MergeDoc('c', 'd', {'b': 2})
        ext.UpdateDoc('c', 'd', {'c': 3})
        ext.DeleteDoc('c', 'd')

        ops = [item['op_type'] for item in captured]
        assert ops == ['set', 'set_merge', 'update', 'delete']


# ═══════════════════════════════════════════════════════════════════════════
# _execute_write (module-level worker function)
# ═══════════════════════════════════════════════════════════════════════════

class TestExecuteWrite:
    def test_set_operation(self):
        mock_db = MagicMock()
        mock_result = FakeWriteResult(
            FakeDatetimeWithNanoseconds(2025, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        )
        mock_db.collection.return_value.document.return_value.set.return_value = mock_result

        item = {'queue_id': 'q1', 'collection': 'users', 'doc_id': 'u1',
                'op_type': 'set', 'payload': {'name': 'Alice'}}
        result = _execute_write(mock_db, item)
        assert result['success'] is True

    def test_set_merge_operation(self):
        mock_db = MagicMock()
        mock_result = FakeWriteResult(None)
        mock_db.collection.return_value.document.return_value.set.return_value = mock_result

        item = {'queue_id': '', 'collection': 'users', 'doc_id': 'u1',
                'op_type': 'set_merge', 'payload': {'name': 'Bob'}}
        _execute_write(mock_db, item)

        mock_db.collection.return_value.document.return_value.set.assert_called_with(
            {'name': 'Bob'}, merge=True
        )

    def test_update_operation(self):
        mock_db = MagicMock()
        mock_result = FakeWriteResult(None)
        mock_db.collection.return_value.document.return_value.update.return_value = mock_result

        item = {'queue_id': '', 'collection': 'users', 'doc_id': 'u1',
                'op_type': 'update', 'payload': {'age': 31}}
        _execute_write(mock_db, item)

        mock_db.collection.return_value.document.return_value.update.assert_called_with({'age': 31})

    def test_delete_operation(self):
        mock_db = MagicMock()

        item = {'queue_id': '', 'collection': 'users', 'doc_id': 'u1',
                'op_type': 'delete', 'payload': {}}
        result = _execute_write(mock_db, item)

        mock_db.collection.return_value.document.return_value.delete.assert_called_once()
        assert result['success'] is True

    def test_unknown_op_type_fails(self):
        mock_db = MagicMock()

        item = {'queue_id': '', 'collection': 'c', 'doc_id': 'd',
                'op_type': 'invalid', 'payload': {}}
        result = _execute_write(mock_db, item)
        assert result['success'] is False
        assert 'Unknown op_type' in result['error']

    def test_write_without_db_fails(self):
        item = {'queue_id': 'q1', 'collection': 'c', 'doc_id': 'd',
                'op_type': 'set', 'payload': {}}
        result = _execute_write(None, item)
        assert result['success'] is False
        assert 'Not connected' in result['error']

    def test_write_exception_returns_error(self):
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value.set.side_effect = Exception('Network error')

        item = {'queue_id': 'q1', 'collection': 'c', 'doc_id': 'd',
                'op_type': 'set', 'payload': {}}
        result = _execute_write(mock_db, item)
        assert result['success'] is False
        assert 'Network error' in result['error']


# ═══════════════════════════════════════════════════════════════════════════
# Table management
# ═══════════════════════════════════════════════════════════════════════════

class TestTableManagement:
    def test_ensure_collection_dat_creates_table(self, ext):
        dat = ext._ensure_collection_dat('users')
        assert dat is not None
        assert dat.numRows == 1  # header row
        headers = [dat[0, c].val for c in range(dat.numCols)]
        assert headers == ['doc_id', 'payload', '_version', '_synced_at', '_dirty']

    def test_ensure_collection_dat_reuses_existing(self, ext):
        dat1 = ext._ensure_collection_dat('users')
        dat2 = ext._ensure_collection_dat('users')
        assert dat1 is dat2

    def test_write_to_table_insert(self, ext):
        ext._ensure_collection_dat('users')
        ext._write_to_table('users', 'u1', '{"a":1}', 'v1', 's1', dirty=0)

        dat = ext.my.op('collections/users')
        assert dat.numRows == 2
        assert dat[1, 'doc_id'].val == 'u1'
        assert dat[1, 'payload'].val == '{"a":1}'

    def test_write_to_table_update(self, ext):
        ext._ensure_collection_dat('users')
        ext._write_to_table('users', 'u1', '{"v":1}', 'v1', 's1', dirty=0)
        ext._write_to_table('users', 'u1', '{"v":2}', 'v2', 's2', dirty=0)

        dat = ext.my.op('collections/users')
        assert dat.numRows == 2  # Still 1 data row
        assert json.loads(dat[1, 'payload'].val)['v'] == 2

    def test_delete_from_table(self, ext):
        ext._ensure_collection_dat('users')
        ext._write_to_table('users', 'u1', '{}', 'v1', 's1', dirty=0)
        ext._write_to_table('users', 'u2', '{}', 'v1', 's1', dirty=0)

        ext._delete_from_table('users', 'u1')
        dat = ext.my.op('collections/users')
        assert dat.numRows == 2  # header + u2
        assert dat[1, 'doc_id'].val == 'u2'


# ═══════════════════════════════════════════════════════════════════════════
# GetDoc / GetCollection (local reads)
# ═══════════════════════════════════════════════════════════════════════════

class TestLocalReads:
    def test_get_doc_found(self, ext):
        ext._ensure_collection_dat('users')
        ext._write_to_table('users', 'u1', '{"name":"Alice"}', 'v1', 's1', dirty=0)

        result = ext.GetDoc('users', 'u1')
        assert result == {'name': 'Alice'}

    def test_get_doc_not_found(self, ext):
        ext._ensure_collection_dat('users')
        assert ext.GetDoc('users', 'nonexistent') is None

    def test_get_doc_no_collection(self, ext):
        assert ext.GetDoc('nonexistent_collection', 'doc1') is None

    def test_get_collection_empty(self, ext):
        ext._ensure_collection_dat('users')
        assert ext.GetCollection('users') == []

    def test_get_collection_multiple_docs(self, ext):
        ext._ensure_collection_dat('users')
        ext._write_to_table('users', 'u1', '{"name":"Alice"}', 'v1', 's1', dirty=0)
        ext._write_to_table('users', 'u2', '{"name":"Bob"}', 'v1', 's1', dirty=0)

        result = ext.GetCollection('users')
        assert len(result) == 2
        names = {d['name'] for d in result}
        assert names == {'Alice', 'Bob'}
        # Each doc should have _doc_id injected
        assert all('_doc_id' in d for d in result)

    def test_get_collection_nonexistent(self, ext):
        assert ext.GetCollection('nonexistent') == []


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    def test_disconnect_sets_status(self, ext):
        ext.Disconnect()
        status = ext.my.op('status')
        assert status[1, 'state'].val == 'disconnected'

    def test_disconnect_clears_keepalive(self, ext):
        ext._keepalive_shutdown.set()
        ext.Disconnect()
        status = ext.my.op('status')
        assert status[1, 'state'].val == 'disconnected'

    def test_ondestroytd(self, ext):
        ext.onDestroyTD()
        assert ext._keepalive_shutdown.is_set()
        assert ext._initialized is False


# ═══════════════════════════════════════════════════════════════════════════
# Flush / Clear write queue
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteQueueManagement:
    def test_flush_re_enqueues_pending(self, ext):
        ext.my.ext.WriteQueueExt.GetPending.return_value = [
            {'queue_id': 'q1', 'collection': 'c', 'doc_id': 'd1', 'op_type': 'set', 'payload': {}},
            {'queue_id': 'q2', 'collection': 'c', 'doc_id': 'd2', 'op_type': 'update', 'payload': {}},
        ]
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext.FlushWriteQueue()
        assert len(captured) == 2

    def test_clear_delegates_to_wq(self, ext):
        ext.ClearWriteQueue()
        ext.my.ext.WriteQueueExt.Clear.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Bidirectional payload roundtrip through the full pipeline
# ═══════════════════════════════════════════════════════════════════════════

class TestFullPipelineRoundtrip:
    """
    Simulate the complete lifecycle:
      1. Firestore snapshot arrives (inbound)
      2. Written to tableDAT
      3. Read back via GetDoc
      4. PushDoc sends it back out (outbound)
      5. Verify payload integrity at each stage
    """

    @pytest.mark.parametrize('payload', [
        {'simple': 'string'},
        {'number': 42, 'float': 3.14, 'bool': True, 'null': None},
        {'nested': {'deep': {'value': [1, 2, 3]}}},
        {'array': [{'a': 1}, {'b': 2}]},
        {'unicode': '\U0001f525 \u00e9\u00e0\u00fc \u4e2d\u6587'},
        {'empty_map': {}, 'empty_array': []},
        {'mixed': [None, True, 0, '', {'k': 'v'}]},
    ])
    def test_roundtrip_through_pipeline(self, ext, payload):
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        # 1. Inbound: simulate snapshot
        ext._inbound_queue.put(('test_col', 'doc1', 'added', payload, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        # 2. Read back from local table
        result = ext.GetDoc('test_col', 'doc1')
        assert result == payload

        # 3. Push back out
        ext.PushDoc('test_col', 'doc1', result)
        assert len(captured) == 1
        assert captured[0]['payload'] == payload

    def test_large_document_roundtrip(self, ext):
        """Test a document with many fields."""
        payload = {f'field_{i}': f'value_{i}' for i in range(100)}
        ext._inbound_queue.put(('big_col', 'big_doc', 'added', payload, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        result = ext.GetDoc('big_col', 'big_doc')
        assert result == payload

    def test_rapid_updates_last_value_wins(self, ext):
        """Multiple rapid updates to the same doc -- last value should persist."""
        for i in range(10):
            ext._inbound_queue.put(('col', 'doc1', 'modified', {'v': i}, f'2025-01-01T00:00:{i:02d}'))
        ext._drain_inbound()

        result = ext.GetDoc('col', 'doc1')
        assert result == {'v': 9}


# ═══════════════════════════════════════════════════════════════════════════
# Write-back: cell edit → Firestore
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteBack:
    """Tests for auto write-back when payload cell is edited."""

    @pytest.fixture(autouse=True)
    def reset_run_mock(self):
        """Reset the global run mock between tests."""
        builtins.run.reset_mock()

    def _setup_collection(self, ext, collection='users', doc_id='u1', payload=None):
        """Populate a collection with one doc and return the tableDAT."""
        payload = payload or {'name': 'Alice'}
        ext._active_collections.add(collection)
        ext._inbound_queue.put((collection, doc_id, 'added', payload, '2025-01-01T00:00:00'))
        ext._drain_inbound()
        return ext.my.op(f'collections/{collection}')

    def _make_info(self, cells_changed):
        """Create a mock info object for onTableChange."""
        info = MagicMock()
        info.cellsChanged = cells_changed
        return info

    def test_on_cell_edit_marks_dirty(self, ext):
        """Editing payload cell should set _dirty=1 and schedule a debounced write."""
        dat = self._setup_collection(ext)
        assert dat[1, '_dirty'].val == '0'

        # Simulate editing the payload cell (col 1, row 1)
        cell = MockCell('{"name":"Bob"}', row=1, col=1)
        info = self._make_info([cell])
        ext._on_cell_edit(dat, info)

        assert dat[1, '_dirty'].val == '1'
        builtins.run.assert_called_once()

    def test_on_cell_edit_skipped_during_remote(self, ext):
        """Cell edit callback should be suppressed during remote writes."""
        dat = self._setup_collection(ext)

        ext._applying_remote = True
        cell = MockCell('{"name":"Bob"}', row=1, col=1)
        info = self._make_info([cell])
        ext._on_cell_edit(dat, info)
        ext._applying_remote = False

        assert dat[1, '_dirty'].val == '0'
        builtins.run.assert_not_called()

    def test_on_cell_edit_skipped_when_disabled(self, ext):
        """Cell edit callback should be suppressed when Enablewriteback is off."""
        dat = self._setup_collection(ext)
        ext.my.par.Enablewriteback = MockParameter(False)

        cell = MockCell('{"name":"Bob"}', row=1, col=1)
        info = self._make_info([cell])
        ext._on_cell_edit(dat, info)

        assert dat[1, '_dirty'].val == '0'

    def test_on_cell_edit_ignores_non_payload_cols(self, ext):
        """Edits to non-payload columns should be ignored."""
        dat = self._setup_collection(ext)

        # col 0 is doc_id
        cell = MockCell('new_id', row=1, col=0)
        info = self._make_info([cell])
        ext._on_cell_edit(dat, info)

        assert dat[1, '_dirty'].val == '0'
        builtins.run.assert_not_called()

    def test_on_cell_edit_ignores_header_row(self, ext):
        """Edits to the header row should be ignored."""
        dat = self._setup_collection(ext)

        cell = MockCell('payload', row=0, col=1)
        info = self._make_info([cell])
        ext._on_cell_edit(dat, info)

        assert dat[1, '_dirty'].val == '0'
        builtins.run.assert_not_called()

    def test_debounced_write_valid_json(self, ext):
        """_execute_debounced_write should parse JSON and call _submit_write."""
        dat = self._setup_collection(ext)
        # Manually set dirty and modify payload
        dat[1, '_dirty'] = '1'
        dat[1, 'payload'] = '{"name":"Bob"}'

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext._execute_debounced_write('users', 'u1')

        assert len(captured) == 1
        assert captured[0]['payload'] == {'name': 'Bob'}
        assert captured[0]['op_type'] == 'set'
        assert dat[1, '_dirty'].val == '0'

    def test_debounced_write_invalid_json(self, ext):
        """Invalid JSON should log warning and leave _dirty=1."""
        dat = self._setup_collection(ext)
        dat[1, '_dirty'] = '1'
        dat[1, 'payload'] = 'not valid json{'

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext._execute_debounced_write('users', 'u1')

        assert len(captured) == 0
        assert dat[1, '_dirty'].val == '1'

    def test_debounced_write_skips_if_not_dirty(self, ext):
        """If _dirty was reset (by remote update), debounced write should skip."""
        dat = self._setup_collection(ext)
        # _dirty is already '0' from inbound

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext._execute_debounced_write('users', 'u1')

        assert len(captured) == 0

    def test_flush_dirty(self, ext):
        """FlushDirty should push all dirty docs."""
        dat = self._setup_collection(ext, 'users', 'u1', {'a': 1})
        # Add a second doc
        ext._inbound_queue.put(('users', 'u2', 'added', {'b': 2}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        # Mark both dirty with modified payloads
        dat[1, '_dirty'] = '1'
        dat[1, 'payload'] = '{"a":10}'
        dat[2, '_dirty'] = '1'
        dat[2, 'payload'] = '{"b":20}'

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext.FlushDirty()

        assert len(captured) == 2
        assert dat[1, '_dirty'].val == '0'
        assert dat[2, '_dirty'].val == '0'

    def test_flush_dirty_skips_invalid_json(self, ext):
        """FlushDirty should skip docs with invalid JSON."""
        dat = self._setup_collection(ext, 'users', 'u1', {'a': 1})
        ext._inbound_queue.put(('users', 'u2', 'added', {'b': 2}, '2025-01-01T00:00:01'))
        ext._drain_inbound()

        dat[1, '_dirty'] = '1'
        dat[1, 'payload'] = 'bad json'
        dat[2, '_dirty'] = '1'
        dat[2, 'payload'] = '{"b":20}'

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        ext.FlushDirty()

        assert len(captured) == 1  # Only u2
        assert dat[1, '_dirty'].val == '1'  # Still dirty -- bad JSON
        assert dat[2, '_dirty'].val == '0'

    def test_remote_write_does_not_trigger_writeback(self, ext):
        """_write_to_table sets _applying_remote, so cell callback should be inert."""
        dat = self._setup_collection(ext)

        # Verify _applying_remote is False after _write_to_table completes
        assert ext._applying_remote is False

        # Verify that during _write_to_table, _applying_remote is True
        states = []
        original_setitem = dat.__class__.__setitem__
        def tracking_setitem(self_dat, key, value):
            if isinstance(key, tuple) and key[1] == 'payload':
                states.append(ext._applying_remote)
            original_setitem(self_dat, key, value)
        dat.__class__.__setitem__ = tracking_setitem

        ext._write_to_table('users', 'u1', '{"v":99}', 'v2', 's2', dirty=0)

        dat.__class__.__setitem__ = original_setitem
        assert states == [True]  # Was True during the write
        assert ext._applying_remote is False  # Reset after


# ═══════════════════════════════════════════════════════════════════════════
# Document & Field Filtering via _make_snapshot_callback
# ═══════════════════════════════════════════════════════════════════════════

class TestSnapshotFiltering:
    """Test document and field filtering in the snapshot callback."""

    @staticmethod
    def _make_change(doc_id, data, change_type='ADDED'):
        ut = FakeDatetimeWithNanoseconds(2025, 1, 1)
        return FakeDocumentChange(doc_id, data, change_type, update_time=ut)

    def _drain(self, ext):
        items = []
        while not ext._inbound_queue.empty():
            items.append(ext._inbound_queue.get_nowait())
        return items

    # ── Document filter: include mode ─────────────────────────────────────

    def test_doc_filter_include_passes_listed(self, ext):
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'d1', 'd2'}, 'include')
        cb([], [self._make_change('d1', {'x': 1})], None)
        items = self._drain(ext)
        assert len(items) == 1
        assert items[0][1] == 'd1'

    def test_doc_filter_include_blocks_unlisted(self, ext):
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'d1'}, 'include')
        cb([], [self._make_change('d99', {'x': 1})], None)
        items = self._drain(ext)
        assert len(items) == 0

    # ── Document filter: exclude mode ─────────────────────────────────────

    def test_doc_filter_exclude_blocks_listed(self, ext):
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'bad'}, 'exclude')
        cb([], [self._make_change('bad', {'x': 1})], None)
        items = self._drain(ext)
        assert len(items) == 0

    def test_doc_filter_exclude_passes_unlisted(self, ext):
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'bad'}, 'exclude')
        cb([], [self._make_change('good', {'x': 1})], None)
        items = self._drain(ext)
        assert len(items) == 1
        assert items[0][1] == 'good'

    # ── Empty doc filter passes all ───────────────────────────────────────

    def test_doc_filter_empty_set_passes_all(self, ext):
        cb = ext._make_snapshot_callback('col', set(), 'exclude', set(), 'include')
        cb([], [
            self._make_change('a', {'x': 1}),
            self._make_change('b', {'x': 2}),
        ], None)
        items = self._drain(ext)
        assert len(items) == 2

    # ── Removed events respect doc filter ─────────────────────────────────

    def test_doc_filter_removed_respects_include(self, ext):
        """Removal of a doc NOT in include set should be skipped."""
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'d1'}, 'include')
        cb([], [self._make_change('d99', None, 'REMOVED')], None)
        items = self._drain(ext)
        assert len(items) == 0

    def test_doc_filter_removed_passes_included(self, ext):
        """Removal of a doc IN include set should pass through."""
        cb = ext._make_snapshot_callback('col', set(), 'exclude', {'d1'}, 'include')
        cb([], [self._make_change('d1', None, 'REMOVED')], None)
        items = self._drain(ext)
        assert len(items) == 1
        assert items[0][2] == 'removed'

    # ── Field filter mode ─────────────────────────────────────────────────

    def test_field_filter_include_mode(self, ext):
        cb = ext._make_snapshot_callback('col', {'name', 'age'}, 'include', set(), 'include')
        cb([], [self._make_change('d1', {'name': 'A', 'age': 30, 'secret': 'x'})], None)
        items = self._drain(ext)
        payload = items[0][3]
        assert set(payload.keys()) == {'name', 'age'}

    def test_field_filter_exclude_mode(self, ext):
        cb = ext._make_snapshot_callback('col', {'secret'}, 'exclude', set(), 'include')
        cb([], [self._make_change('d1', {'name': 'A', 'secret': 'x'})], None)
        items = self._drain(ext)
        payload = items[0][3]
        assert 'secret' not in payload
        assert 'name' in payload

    # ── Combined doc + field filter ───────────────────────────────────────

    def test_combined_doc_and_field_filter(self, ext):
        """Doc include filter + field include filter applied together."""
        cb = ext._make_snapshot_callback('col', {'name'}, 'include', {'d1'}, 'include')
        cb([], [
            self._make_change('d1', {'name': 'A', 'secret': 'x'}),
            self._make_change('d2', {'name': 'B', 'secret': 'y'}),
        ], None)
        items = self._drain(ext)
        assert len(items) == 1
        assert items[0][1] == 'd1'
        assert set(items[0][3].keys()) == {'name'}