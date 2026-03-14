"""
Burst and stress tests for FirestoreExt.

Tests high-volume scenarios that simulate real-world frame pressure:
  - 10/50/100 docs added in a single drain cycle
  - Same doc modified 50/100 times rapidly
  - Interleaved add/modify/remove in one batch
  - Multiple collections under burst load
  - Concurrent inbound + outbound operations
  - Large batch writes (PushBatch with 50-100 ops)
  - Write-back burst: many cells edited in same frame
  - Queue overflow: inbound faster than drain rate
"""

import json
import sys
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dev' / 'gcp' / 'firestore'))

import builtins
builtins.debug = lambda *a, **kw: None
builtins.project = MagicMock()
builtins.project.folder = '/tmp/test_project'
builtins.op = MagicMock()
builtins.tableDAT = MagicMock()
builtins.datexecDAT = MagicMock()
builtins.datexecuteDAT = MagicMock()
builtins.absTime = MagicMock()
builtins.absTime.frame = 0
builtins.run = MagicMock()

from ext_firestore import FirestoreExt, _execute_write
from td_mocks import (
    MockOwnerComp, MockTableDAT, MockParameter, MockCell,
    FakeWriteResult, FakeDatetimeWithNanoseconds,
)

import datetime


# ═══════════════════════════════════════════════════════════════════════════
# Shared fixture
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ext():
    """A fully wired FirestoreExt with mocked sub-extensions."""
    owner = MockOwnerComp('firestore', {
        'Filterfields': '',
        'Enablecache': False,
        'Callbacksdat': '',
        'Logop': '',
        'Autoconnect': False,
        'Enablelistener': False,
        'Collections': 'users scenes status config',
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

    status = MockTableDAT('status')
    status.appendRow(['state', 'circuit', 'last_error', 'queue_depth', 'connected_at', 'collections'])
    status.appendRow(['disconnected', 'closed', '', '0', '', ''])
    owner.register_op('status', status)
    owner.register_op('log', MockTableDAT('log'))

    collections_comp = MagicMock()
    def create_table(op_type, name):
        if op_type is builtins.tableDAT:
            dat = MockTableDAT(name)
            owner.register_op(f'collections/{name}', dat)
            return dat
        mock_op = MagicMock(name=f'mock_{name}')
        mock_op.name = name
        return mock_op
    collections_comp.create = create_table
    owner.register_op('collections', collections_comp)

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
# Burst inbound: many unique docs in one drain cycle
# ═══════════════════════════════════════════════════════════════════════════

class TestBurstInboundUniqueDocs:
    """Test adding many unique documents in rapid succession."""

    @pytest.mark.parametrize('count', [10, 50, 100])
    def test_burst_unique_docs(self, ext, count):
        """Add N unique docs and drain until all processed."""
        for i in range(count):
            ext._inbound_queue.put((
                'users', f'user_{i}', 'added',
                {'index': i, 'name': f'User {i}'},
                f'2025-01-01T00:00:{i % 60:02d}'
            ))

        # Drain multiple times (MAX_INBOUND_PER_FRAME = 10)
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        dat = ext.my.op('collections/users')
        assert dat.numRows == count + 1  # header + all docs

        # Verify all docs are present and readable
        for i in range(count):
            doc = ext.GetDoc('users', f'user_{i}')
            assert doc is not None
            assert doc['index'] == i

    def test_50_docs_correct_final_state(self, ext):
        """50 docs should all be retrievable via GetCollection."""
        for i in range(50):
            ext._inbound_queue.put((
                'users', f'u{i}', 'added',
                {'v': i}, f'2025-01-01T00:{i // 60:02d}:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        collection = ext.GetCollection('users')
        assert len(collection) == 50
        values = {d['v'] for d in collection}
        assert values == set(range(50))

    def test_100_docs_across_collections(self, ext):
        """Spread 100 docs across 4 collections."""
        collections = ['users', 'scenes', 'status', 'config']
        for i in range(100):
            col = collections[i % 4]
            ext._inbound_queue.put((
                col, f'doc_{i}', 'added',
                {'i': i, 'col': col},
                f'2025-01-01T00:00:{i % 60:02d}'
            ))

        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        # Each collection should have 25 docs
        for col in collections:
            result = ext.GetCollection(col)
            assert len(result) == 25


# ═══════════════════════════════════════════════════════════════════════════
# Burst inbound: same doc modified many times
# ═══════════════════════════════════════════════════════════════════════════

class TestBurstSameDocUpdates:
    """Test rapid modifications to the same document."""

    @pytest.mark.parametrize('count', [10, 50, 100])
    def test_same_doc_modified_n_times(self, ext, count):
        """Modify the same doc N times -- only the last value should persist."""
        # First add
        ext._inbound_queue.put((
            'users', 'target', 'added',
            {'v': 0}, '2025-01-01T00:00:00'
        ))
        ext._drain_inbound()

        # Then N modifications with strictly increasing timestamps
        for i in range(1, count + 1):
            ts_sec = i % 60
            ts_min = i // 60
            ext._inbound_queue.put((
                'users', 'target', 'modified',
                {'v': i, 'iteration': i},
                f'2025-01-01T00:{ts_min:02d}:{ts_sec:02d}'
            ))

        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        # Only 1 data row should exist
        dat = ext.my.op('collections/users')
        assert dat.numRows == 2  # header + 1

        # Last value wins
        result = ext.GetDoc('users', 'target')
        assert result['v'] == count
        assert result['iteration'] == count

    def test_50_updates_version_tracking(self, ext):
        """Version dict should hold final version after 50 updates."""
        ext._inbound_queue.put(('users', 'vdoc', 'added', {'v': 0}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        for i in range(1, 51):
            ext._inbound_queue.put((
                'users', 'vdoc', 'modified',
                {'v': i}, f'2025-01-01T00:{i // 60:02d}:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        # Version should be the last timestamp
        assert ('users', 'vdoc') in ext._versions

    def test_interleaved_docs_same_collection(self, ext):
        """Alternate updates between 2 docs -- both should have correct final state."""
        ext._inbound_queue.put(('users', 'a', 'added', {'v': 0}, '2025-01-01T00:00:00'))
        ext._inbound_queue.put(('users', 'b', 'added', {'v': 0}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        for i in range(1, 51):
            doc_id = 'a' if i % 2 == 0 else 'b'
            ext._inbound_queue.put((
                'users', doc_id, 'modified',
                {'v': i}, f'2025-01-01T00:{i // 60:02d}:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        a = ext.GetDoc('users', 'a')
        b = ext.GetDoc('users', 'b')
        assert a['v'] == 50  # last even
        assert b['v'] == 49  # last odd


# ═══════════════════════════════════════════════════════════════════════════
# Interleaved add/modify/remove in one burst
# ═══════════════════════════════════════════════════════════════════════════

class TestInterleavedOperations:
    """Mix add, modify, and remove operations in a single burst."""

    def test_add_then_remove_same_frame(self, ext):
        """Add and remove same doc within one drain cycle."""
        ext._inbound_queue.put(('users', 'ephemeral', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._inbound_queue.put(('users', 'ephemeral', 'removed', {}, ''))
        ext._drain_inbound()

        assert ext.GetDoc('users', 'ephemeral') is None
        dat = ext.my.op('collections/users')
        assert dat.numRows == 1  # header only

    def test_add_modify_remove_cycle(self, ext):
        """Full lifecycle: add -> modify -> remove within one burst."""
        ext._inbound_queue.put(('users', 'cycle', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._inbound_queue.put(('users', 'cycle', 'modified', {'v': 2}, '2025-01-01T00:00:01'))
        ext._inbound_queue.put(('users', 'cycle', 'removed', {}, ''))
        ext._drain_inbound()

        assert ext.GetDoc('users', 'cycle') is None

    def test_mixed_operations_50_docs(self, ext):
        """Add 50, modify 25, remove 10 -- verify final counts."""
        # Add 50
        for i in range(50):
            ext._inbound_queue.put((
                'users', f'd{i}', 'added',
                {'v': i}, f'2025-01-01T00:00:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        assert len(ext.GetCollection('users')) == 50

        # Modify first 25
        for i in range(25):
            ext._inbound_queue.put((
                'users', f'd{i}', 'modified',
                {'v': i * 10, 'modified': True},
                f'2025-01-01T00:01:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        # Remove last 10 (d40-d49)
        for i in range(40, 50):
            ext._inbound_queue.put(('users', f'd{i}', 'removed', {}, ''))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        collection = ext.GetCollection('users')
        assert len(collection) == 40  # 50 - 10 removed

        # Verify modifications stuck
        d0 = ext.GetDoc('users', 'd0')
        assert d0['v'] == 0  # modified: 0*10 = 0
        assert d0.get('modified') is True

        d24 = ext.GetDoc('users', 'd24')
        assert d24['v'] == 240
        assert d24.get('modified') is True

        # Unmodified doc still has original value
        d30 = ext.GetDoc('users', 'd30')
        assert d30['v'] == 30
        assert d30.get('modified') is None

    def test_re_add_after_remove(self, ext):
        """Remove then re-add same doc_id -- should exist with new data."""
        ext._inbound_queue.put(('users', 'phoenix', 'added', {'v': 1}, '2025-01-01T00:00:00'))
        ext._drain_inbound()

        ext._inbound_queue.put(('users', 'phoenix', 'removed', {}, ''))
        ext._drain_inbound()
        assert ext.GetDoc('users', 'phoenix') is None

        ext._inbound_queue.put(('users', 'phoenix', 'added', {'v': 2, 'reborn': True}, '2025-01-01T00:00:02'))
        ext._drain_inbound()

        result = ext.GetDoc('users', 'phoenix')
        assert result == {'v': 2, 'reborn': True}


# ═══════════════════════════════════════════════════════════════════════════
# Burst outbound: large batch writes
# ═══════════════════════════════════════════════════════════════════════════

class TestBurstOutbound:
    """Test high-volume outbound write operations."""

    def _setup_capture(self, ext):
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)
        return captured

    @pytest.mark.parametrize('count', [10, 50, 100])
    def test_push_batch_n_ops(self, ext, count):
        """PushBatch with N operations -- all should be submitted."""
        captured = self._setup_capture(ext)

        ops = [
            {'collection': 'users', 'doc_id': f'u{i}', 'op_type': 'set', 'payload': {'i': i}}
            for i in range(count)
        ]
        ext.PushBatch(ops)

        assert len(captured) == count
        # Verify ordering preserved
        for i, item in enumerate(captured):
            assert item['payload']['i'] == i

    def test_rapid_individual_pushes(self, ext):
        """50 individual PushDoc calls in succession."""
        captured = self._setup_capture(ext)

        for i in range(50):
            ext.PushDoc('users', f'u{i}', {'rapid': True, 'i': i})

        assert len(captured) == 50

    def test_mixed_write_types_burst(self, ext):
        """Burst of mixed set/merge/update/delete operations."""
        captured = self._setup_capture(ext)

        for i in range(100):
            op_type = i % 4
            if op_type == 0:
                ext.PushDoc('users', f'u{i}', {'v': i})
            elif op_type == 1:
                ext.MergeDoc('users', f'u{i}', {'v': i})
            elif op_type == 2:
                ext.UpdateDoc('users', f'u{i}', {'v': i})
            else:
                ext.DeleteDoc('users', f'u{i}')

        assert len(captured) == 100
        # Verify correct op_types
        expected_types = ['set', 'set_merge', 'update', 'delete'] * 25
        actual_types = [c['op_type'] for c in captured]
        assert actual_types == expected_types


# ═══════════════════════════════════════════════════════════════════════════
# Write-back burst: many cells edited in "same frame"
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteBackBurst:
    """Test many cell edits triggering write-back."""

    @pytest.fixture(autouse=True)
    def reset_run_mock(self):
        builtins.run.reset_mock()

    def _populate(self, ext, count):
        """Add N docs to users collection."""
        for i in range(count):
            ext._inbound_queue.put((
                'users', f'u{i}', 'added',
                {'v': i}, f'2025-01-01T00:00:{i % 60:02d}'
            ))
        ext._active_collections.add('users')
        while not ext._inbound_queue.empty():
            ext._drain_inbound()
        return ext.my.op('collections/users')

    def test_flush_dirty_10_docs(self, ext):
        """Mark 10 docs dirty and flush -- all should submit."""
        dat = self._populate(ext, 10)

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        for i in range(1, 11):
            dat[i, '_dirty'] = '1'
            dat[i, 'payload'] = json.dumps({'v': i * 100})

        ext.FlushDirty()
        assert len(captured) == 10
        assert all(dat[i, '_dirty'].val == '0' for i in range(1, 11))

    def test_flush_dirty_50_docs(self, ext):
        """Mark 50 docs dirty and flush."""
        dat = self._populate(ext, 50)

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        for i in range(1, 51):
            dat[i, '_dirty'] = '1'
            dat[i, 'payload'] = json.dumps({'v': i * 100})

        ext.FlushDirty()
        assert len(captured) == 50

    def test_flush_dirty_100_docs(self, ext):
        """Mark 100 docs dirty and flush -- stress test."""
        dat = self._populate(ext, 100)

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        for i in range(1, 101):
            dat[i, '_dirty'] = '1'
            dat[i, 'payload'] = json.dumps({'v': i * 100})

        ext.FlushDirty()
        assert len(captured) == 100
        # All dirty flags reset
        assert all(dat[i, '_dirty'].val == '0' for i in range(1, 101))

    def test_partial_dirty_mixed(self, ext):
        """50 docs, only 20 dirty -- only 20 should flush."""
        dat = self._populate(ext, 50)

        captured = []
        ext._submit_write_item = lambda item: captured.append(item)

        dirty_indices = list(range(1, 21))
        for i in dirty_indices:
            dat[i, '_dirty'] = '1'
            dat[i, 'payload'] = json.dumps({'v': i * 100})

        ext.FlushDirty()
        assert len(captured) == 20


# ═══════════════════════════════════════════════════════════════════════════
# Queue overflow: inbound faster than drain rate
# ═══════════════════════════════════════════════════════════════════════════

class TestQueueOverflow:
    """Test behavior when inbound items exceed per-frame drain limit."""

    def test_multi_frame_drain(self, ext):
        """100 items should require exactly 10 drain cycles (MAX=10)."""
        for i in range(100):
            ext._inbound_queue.put((
                'users', f'u{i}', 'added',
                {'i': i}, f'2025-01-01T00:{i // 60:02d}:{i % 60:02d}'
            ))

        drain_count = 0
        while not ext._inbound_queue.empty():
            ext._drain_inbound()
            drain_count += 1

        assert drain_count == 10  # 100 / 10 per frame
        assert len(ext.GetCollection('users')) == 100

    def test_continuous_feed_with_drain(self, ext):
        """Simulate continuous feed: add items between drains."""
        total_added = 0
        for batch in range(5):
            # Add 15 items per batch
            for i in range(15):
                idx = total_added
                ext._inbound_queue.put((
                    'users', f'u{idx}', 'added',
                    {'batch': batch, 'i': i},
                    f'2025-01-01T00:{idx // 60:02d}:{idx % 60:02d}'
                ))
                total_added += 1
            # Drain once (processes 10)
            ext._drain_inbound()

        # Drain remaining
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        assert len(ext.GetCollection('users')) == 75

    def test_drain_empty_queue_is_noop(self, ext):
        """Draining an empty queue should not error."""
        ext._drain_inbound()
        ext._drain_inbound()
        ext._drain_inbound()
        # No exception = pass


# ═══════════════════════════════════════════════════════════════════════════
# Write result processing burst
# ═══════════════════════════════════════════════════════════════════════════

class TestWriteResultBurst:
    """Process many write results in rapid succession."""

    def test_50_success_results(self, ext):
        """50 successful write results should all store versions."""
        for i in range(50):
            result = {
                'queue_id': f'q{i}', 'doc_id': f'u{i}', 'collection': 'users',
                'success': True, 'update_time': f'2025-01-01T00:00:{i % 60:02d}',
                'error': None,
            }
            ext._process_write_result(result)

        assert len(ext._versions) >= 50
        for i in range(50):
            assert ('users', f'u{i}') in ext._versions

    def test_mixed_success_failure_burst(self, ext):
        """Alternate success and failure results."""
        for i in range(100):
            result = {
                'queue_id': f'q{i}', 'doc_id': f'u{i}', 'collection': 'users',
                'success': i % 2 == 0,
                'update_time': f'2025-01-01T00:00:{i % 60:02d}' if i % 2 == 0 else '',
                'error': None if i % 2 == 0 else f'error_{i}',
            }
            ext._process_write_result(result)

        # 50 successes should have versions
        success_count = sum(1 for k in ext._versions if k[0] == 'users')
        assert success_count == 50


# ═══════════════════════════════════════════════════════════════════════════
# Full roundtrip burst: inbound -> table -> edit -> outbound
# ═══════════════════════════════════════════════════════════════════════════

class TestFullRoundtripBurst:
    """End-to-end burst: receive docs, edit locally, push back."""

    def test_receive_edit_push_50_docs(self, ext):
        """Receive 50 docs, modify all payloads, flush back."""
        # Receive
        for i in range(50):
            ext._inbound_queue.put((
                'users', f'u{i}', 'added',
                {'original': i}, f'2025-01-01T00:00:{i % 60:02d}'
            ))
        ext._active_collections.add('users')
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        # Modify all
        dat = ext.my.op('collections/users')
        for i in range(1, 51):
            dat[i, 'payload'] = json.dumps({'original': i - 1, 'edited': True})
            dat[i, '_dirty'] = '1'

        # Flush
        captured = []
        ext._submit_write_item = lambda item: captured.append(item)
        ext.FlushDirty()

        assert len(captured) == 50
        for item in captured:
            assert item['payload']['edited'] is True
            assert 'original' in item['payload']

    def test_receive_delete_readd_burst(self, ext):
        """Add 20, remove all, re-add 20 -- table should have 20."""
        for i in range(20):
            ext._inbound_queue.put(('users', f'u{i}', 'added', {'v': i}, f'2025-01-01T00:00:{i:02d}'))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()
        assert len(ext.GetCollection('users')) == 20

        for i in range(20):
            ext._inbound_queue.put(('users', f'u{i}', 'removed', {}, ''))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()
        assert len(ext.GetCollection('users')) == 0

        for i in range(20):
            ext._inbound_queue.put(('users', f'u{i}', 'added', {'v': i + 100}, f'2025-01-01T00:01:{i:02d}'))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()
        assert len(ext.GetCollection('users')) == 20

        # Verify new data
        for i in range(20):
            doc = ext.GetDoc('users', f'u{i}')
            assert doc['v'] == i + 100


# ═══════════════════════════════════════════════════════════════════════════
# Payload size edge cases under burst
# ═══════════════════════════════════════════════════════════════════════════

class TestPayloadSizeUnderBurst:
    """Test with varying payload sizes at volume."""

    def test_50_large_payloads(self, ext):
        """50 docs each with 100-field payloads."""
        for i in range(50):
            payload = {f'field_{j}': f'value_{j}_{i}' for j in range(100)}
            ext._inbound_queue.put((
                'users', f'big_{i}', 'added',
                payload, f'2025-01-01T00:00:{i % 60:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        assert len(ext.GetCollection('users')) == 50
        # Spot check
        doc = ext.GetDoc('users', 'big_25')
        assert len(doc) == 100
        assert doc['field_0'] == 'value_0_25'

    def test_deeply_nested_burst(self, ext):
        """20 docs with 5 levels of nesting."""
        for i in range(20):
            payload = {
                'l1': {
                    'l2': {
                        'l3': {
                            'l4': {
                                'l5': {'value': i, 'data': list(range(10))}
                            }
                        }
                    }
                }
            }
            ext._inbound_queue.put((
                'users', f'nested_{i}', 'added',
                payload, f'2025-01-01T00:00:{i:02d}'
            ))
        while not ext._inbound_queue.empty():
            ext._drain_inbound()

        doc = ext.GetDoc('users', 'nested_15')
        assert doc['l1']['l2']['l3']['l4']['l5']['value'] == 15
        assert doc['l1']['l2']['l3']['l4']['l5']['data'] == list(range(10))