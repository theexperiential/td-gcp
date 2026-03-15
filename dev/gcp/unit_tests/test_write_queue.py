"""
Tests for WriteQueueExt — SQLite-backed offline write persistence.

Uses real SQLite (in-memory or tmp_path) to test:
  - Enqueue/dequeue/remove/retry/clear lifecycle
  - Document cache (SaveDocument, LoadDocuments, DeleteDocument)
  - Concurrent access from multiple threads
  - Edge cases (empty queue, duplicate IDs, large payloads)
"""

import json
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dev' / 'gcp' / 'firestore'))

from td_mocks import MockOwnerComp, MockTableDAT
from ext_write_queue import WriteQueueExt


@pytest.fixture
def wq(tmp_path):
    """A WriteQueueExt backed by a real SQLite DB in tmp_path."""
    owner = MockOwnerComp('firestore', {
        'Cachepath': 'cache',
    })
    # Register the write_queue_dat mock
    wq_dat = MockTableDAT('write_queue_dat')
    wq_dat.appendRow(['queue_id', 'collection', 'doc_id', 'op_type', 'payload', 'queued_at', 'retry_count', 'last_error'])
    owner.register_op('write_queue_dat', wq_dat)

    ext = WriteQueueExt(owner)
    ext.Init(str(tmp_path))
    return ext


# ═══════════════════════════════════════════════════════════════════════════
# Basic enqueue / dequeue
# ═══════════════════════════════════════════════════════════════════════════

class TestEnqueue:
    def test_enqueue_returns_uuid(self, wq):
        qid = wq.Enqueue('users', 'user1', 'set', {'name': 'Alice'})
        assert isinstance(qid, str)
        assert len(qid) == 36  # UUID format

    def test_enqueue_increments_count(self, wq):
        assert wq.Count() == 0
        wq.Enqueue('users', 'u1', 'set', {'a': 1})
        assert wq.Count() == 1
        wq.Enqueue('users', 'u2', 'update', {'b': 2})
        assert wq.Count() == 2

    def test_enqueue_empty_payload(self, wq):
        qid = wq.Enqueue('users', 'u1', 'delete', {})
        pending = wq.GetPending()
        assert len(pending) == 1
        assert pending[0]['payload'] == {}

    def test_enqueue_none_payload(self, wq):
        qid = wq.Enqueue('users', 'u1', 'delete', None)
        pending = wq.GetPending()
        assert pending[0]['payload'] == {}

    def test_enqueue_complex_payload(self, wq):
        data = {
            'nested': {'deep': {'value': [1, 2, {'x': True}]}},
            'array': [None, 'str', 42, 3.14],
            'unicode': 'fire eau',
        }
        wq.Enqueue('users', 'u1', 'set', data)
        pending = wq.GetPending()
        assert pending[0]['payload'] == data

    def test_enqueue_large_payload(self, wq):
        data = {'big_field': 'x' * 100000}
        wq.Enqueue('users', 'u1', 'set', data)
        pending = wq.GetPending()
        assert len(pending[0]['payload']['big_field']) == 100000


class TestGetPending:
    def test_empty_queue(self, wq):
        assert wq.GetPending() == []

    def test_ordering_by_queued_at(self, wq):
        wq.Enqueue('a', 'a1', 'set', {'order': 1})
        wq.Enqueue('b', 'b1', 'set', {'order': 2})
        wq.Enqueue('c', 'c1', 'set', {'order': 3})
        pending = wq.GetPending()
        assert [p['payload']['order'] for p in pending] == [1, 2, 3]

    def test_pending_has_all_fields(self, wq):
        wq.Enqueue('users', 'u1', 'set_merge', {'key': 'val'})
        item = wq.GetPending()[0]
        assert 'queue_id' in item
        assert item['collection'] == 'users'
        assert item['doc_id'] == 'u1'
        assert item['op_type'] == 'set_merge'
        assert item['payload'] == {'key': 'val'}
        assert 'queued_at' in item
        assert item['retry_count'] == 0
        assert item['last_error'] == ''


class TestRemove:
    def test_remove_by_queue_id(self, wq):
        qid = wq.Enqueue('users', 'u1', 'set', {'a': 1})
        assert wq.Count() == 1
        wq.Remove(qid)
        assert wq.Count() == 0

    def test_remove_nonexistent_id(self, wq):
        wq.Remove('nonexistent-id')
        assert wq.Count() == 0

    def test_remove_only_target(self, wq):
        qid1 = wq.Enqueue('users', 'u1', 'set', {'a': 1})
        qid2 = wq.Enqueue('users', 'u2', 'set', {'b': 2})
        wq.Remove(qid1)
        assert wq.Count() == 1
        remaining = wq.GetPending()
        assert remaining[0]['doc_id'] == 'u2'


class TestRecordRetry:
    def test_retry_increments_count(self, wq):
        qid = wq.Enqueue('users', 'u1', 'set', {'a': 1})
        wq.RecordRetry(qid, 'connection timeout')
        pending = wq.GetPending()
        assert pending[0]['retry_count'] == 1
        assert pending[0]['last_error'] == 'connection timeout'

    def test_multiple_retries(self, wq):
        qid = wq.Enqueue('users', 'u1', 'set', {'a': 1})
        for i in range(5):
            wq.RecordRetry(qid, f'error_{i}')
        pending = wq.GetPending()
        assert pending[0]['retry_count'] == 5
        assert pending[0]['last_error'] == 'error_4'


class TestClear:
    def test_clear_empties_queue(self, wq):
        for i in range(10):
            wq.Enqueue('col', f'doc{i}', 'set', {'i': i})
        assert wq.Count() == 10
        wq.Clear()
        assert wq.Count() == 0

    def test_clear_empty_queue(self, wq):
        wq.Clear()
        assert wq.Count() == 0


class TestCount:
    def test_count_zero(self, wq):
        assert wq.Count() == 0

    def test_count_accurate(self, wq):
        for i in range(25):
            wq.Enqueue('col', f'd{i}', 'set', {})
        assert wq.Count() == 25


# ═══════════════════════════════════════════════════════════════════════════
# All operation types
# ═══════════════════════════════════════════════════════════════════════════

class TestOperationTypes:
    @pytest.mark.parametrize('op_type', ['set', 'update', 'delete', 'set_merge'])
    def test_all_op_types_stored(self, wq, op_type):
        wq.Enqueue('col', 'doc1', op_type, {'x': 1})
        pending = wq.GetPending()
        assert pending[0]['op_type'] == op_type


# ═══════════════════════════════════════════════════════════════════════════
# Document cache
# ═══════════════════════════════════════════════════════════════════════════

class TestDocumentCache:
    def test_save_and_load(self, wq):
        wq.SaveDocument('users', 'u1', '{"name": "Alice"}', '2025-01-01T00:00:00', '2025-01-01T00:00:01')
        docs = wq.LoadDocuments()
        assert len(docs) == 1
        assert docs[0] == ('users', 'u1', '{"name": "Alice"}', '2025-01-01T00:00:00', '2025-01-01T00:00:01')

    def test_upsert_overwrites(self, wq):
        wq.SaveDocument('users', 'u1', '{"v": 1}', 'v1', 's1')
        wq.SaveDocument('users', 'u1', '{"v": 2}', 'v2', 's2')
        docs = wq.LoadDocuments()
        assert len(docs) == 1
        assert json.loads(docs[0][2]) == {'v': 2}

    def test_multiple_collections(self, wq):
        wq.SaveDocument('users', 'u1', '{}', 'v1', 's1')
        wq.SaveDocument('scenes', 's1', '{}', 'v1', 's1')
        wq.SaveDocument('status', 'st1', '{}', 'v1', 's1')
        docs = wq.LoadDocuments()
        assert len(docs) == 3

    def test_delete_document(self, wq):
        wq.SaveDocument('users', 'u1', '{}', 'v1', 's1')
        wq.SaveDocument('users', 'u2', '{}', 'v1', 's1')
        wq.DeleteDocument('users', 'u1')
        docs = wq.LoadDocuments()
        assert len(docs) == 1
        assert docs[0][1] == 'u2'

    def test_delete_nonexistent_document(self, wq):
        wq.DeleteDocument('users', 'nonexistent')
        assert wq.LoadDocuments() == []

    def test_load_empty_cache(self, wq):
        assert wq.LoadDocuments() == []


# ═══════════════════════════════════════════════════════════════════════════
# Thread safety
# ═══════════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    def test_concurrent_enqueue(self, wq):
        errors = []
        ids = []
        lock = threading.Lock()

        def enqueue_many():
            try:
                for i in range(50):
                    qid = wq.Enqueue('col', f'doc_{threading.current_thread().name}_{i}', 'set', {'i': i})
                    with lock:
                        ids.append(qid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_many, name=f't{i}') for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert wq.Count() == 250
        # All queue IDs should be unique
        assert len(set(ids)) == 250

    def test_concurrent_enqueue_and_remove(self, wq):
        errors = []

        def enqueue_and_remove():
            try:
                for i in range(20):
                    qid = wq.Enqueue('col', f'doc_{i}', 'set', {})
                    wq.Remove(qid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_and_remove) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_document_cache(self, wq):
        errors = []

        def save_docs():
            try:
                for i in range(50):
                    wq.SaveDocument(f'col_{threading.current_thread().name}', f'doc_{i}', '{}', 'v', 's')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=save_docs, name=f't{i}') for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        docs = wq.LoadDocuments()
        assert len(docs) == 250


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_special_characters_in_doc_id(self, wq):
        wq.Enqueue('users', 'user/with/slashes', 'set', {'a': 1})
        wq.Enqueue('users', 'user with spaces', 'set', {'b': 2})
        wq.Enqueue('users', 'user"with"quotes', 'set', {'c': 3})
        assert wq.Count() == 3

    def test_unicode_collection_name(self, wq):
        wq.Enqueue('users_intl', 'doc1', 'set', {'name': 'test'})
        pending = wq.GetPending()
        assert pending[0]['collection'] == 'users_intl'

    def test_empty_string_fields(self, wq):
        wq.Enqueue('', '', 'set', {})
        assert wq.Count() == 1

    def test_payload_with_special_json(self, wq):
        """Payload with quotes, backslashes, newlines."""
        data = {
            'quote': 'He said "hello"',
            'backslash': 'C:\\Users\\path',
            'newline': 'line1\nline2',
            'tab': 'col1\tcol2',
            'null_char': 'before\x00after',
        }
        wq.Enqueue('col', 'doc1', 'set', data)
        pending = wq.GetPending()
        assert pending[0]['payload']['quote'] == 'He said "hello"'
        assert pending[0]['payload']['backslash'] == 'C:\\Users\\path'

    def test_db_persistence_across_instances(self, tmp_path):
        """Data persists if we create a new WriteQueueExt pointing to same DB."""
        owner = MockOwnerComp('firestore', {'Cachepath': 'cache'})
        wq_dat = MockTableDAT('write_queue_dat')
        wq_dat.appendRow(['queue_id', 'collection', 'doc_id', 'op_type', 'payload', 'queued_at', 'retry_count', 'last_error'])
        owner.register_op('write_queue_dat', wq_dat)

        ext1 = WriteQueueExt(owner)
        ext1.Init(str(tmp_path))
        qid = ext1.Enqueue('col', 'doc1', 'set', {'persisted': True})

        # New instance, same path
        owner2 = MockOwnerComp('firestore', {'Cachepath': 'cache'})
        wq_dat2 = MockTableDAT('write_queue_dat')
        wq_dat2.appendRow(['queue_id', 'collection', 'doc_id', 'op_type', 'payload', 'queued_at', 'retry_count', 'last_error'])
        owner2.register_op('write_queue_dat', wq_dat2)

        ext2 = WriteQueueExt(owner2)
        ext2.Init(str(tmp_path))

        assert ext2.Count() == 1
        pending = ext2.GetPending()
        assert pending[0]['payload'] == {'persisted': True}