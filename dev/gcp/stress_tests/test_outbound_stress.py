"""
Outbound stress tests: TD -> Firebase writes.

Triggers writes from TD via the stress test agent and verifies
they land in Firestore using the test script's own on_snapshot listener.
"""

import json
import time

import pytest

from conftest import wait_for_td_table, make_test_docs


pytestmark = pytest.mark.stress


class TestPushBatch:
    """PushBatch N docs from TD, verify all appear in Firebase."""

    @pytest.mark.parametrize('count', [10, 50, 100, 250, 500])
    def test_push_batch(self, firebase, agent, test_collection, count):
        docs = make_test_docs(count)
        timeout = 5 + count * 0.2

        # Tell TD agent to push all docs
        result = firebase.send_and_wait(
            'push_batch',
            {'collection': test_collection, 'docs': docs},
            timeout=30,
        )
        assert result['status'] == 'done'
        assert result['result']['count'] == count

        # Verify docs appear in Firebase via our own listener
        received = firebase.listen(test_collection, count, timeout=timeout)
        assert len(received) >= count, (
            f'Expected {count} docs in Firebase, got {len(received)}'
        )

        # Verify data integrity
        for doc_id, original in docs.items():
            assert doc_id in received, f'Missing doc: {doc_id}'
            for key, value in original.items():
                assert received[doc_id].get(key) == value, (
                    f'{doc_id}.{key}: expected {value!r}, '
                    f'got {received[doc_id].get(key)!r}'
                )


class TestWriteBackFlush:
    """Inject docs inbound, edit cells in TD, flush, verify in Firebase."""

    @pytest.mark.parametrize('count', [50, 100])
    def test_write_back_flush(self, firebase, agent, test_collection, count):
        # 1. Inject docs into Firebase (they'll arrive in TD via listener)
        original_docs = make_test_docs(count)
        firebase.write_batch(test_collection, original_docs)
        wait_for_td_table(firebase, test_collection, count, timeout=30)

        # 2. Edit cells in TD (modify payload, mark dirty)
        edits = {
            f'doc_{i}': {'_test_seq': i, 'value': f'edited_{i}', 'edited': True}
            for i in range(count)
        }
        result = firebase.send_and_wait(
            'edit_cells',
            {'collection': test_collection, 'edits': edits},
            timeout=15,
        )
        assert result['status'] == 'done'
        assert result['result']['edited'] == count

        # 3. Flush dirty docs
        result = firebase.send_and_wait('flush_dirty', {}, timeout=15)
        assert result['status'] == 'done'

        # 4. Verify edited docs land in Firebase
        timeout = 10 + count * 0.2
        time.sleep(3)  # give writes time to propagate

        fb_docs = firebase.read_collection(test_collection)
        edited_count = sum(1 for d in fb_docs.values() if d.get('edited') is True)
        assert edited_count == count, (
            f'Expected {count} edited docs in Firebase, got {edited_count}'
        )


class TestDeletePropagation:
    """Push docs from TD, then delete them, verify gone from Firebase."""

    def test_delete_50(self, firebase, agent, test_collection):
        count = 50
        docs = make_test_docs(count)

        # Push docs from TD
        result = firebase.send_and_wait(
            'push_batch',
            {'collection': test_collection, 'docs': docs},
            timeout=30,
        )
        assert result['status'] == 'done'

        # Wait for them to arrive in Firebase
        firebase.listen(test_collection, count, timeout=30)

        # Delete all docs from TD
        doc_ids = list(docs.keys())
        result = firebase.send_and_wait(
            'delete_docs',
            {'collection': test_collection, 'doc_ids': doc_ids},
            timeout=30,
        )
        assert result['status'] == 'done'
        assert result['result']['deleted'] == count

        # Verify all gone from Firebase
        time.sleep(5)
        remaining = firebase.read_collection(test_collection)
        assert len(remaining) == 0, (
            f'Expected 0 docs after delete, got {len(remaining)}'
        )


class TestMixedOps:
    """Mix of push, update, delete operations."""

    @pytest.mark.parametrize('count', [100, 250])
    def test_mixed_ops(self, firebase, agent, test_collection, count):
        # Phase 1: Push all docs
        docs = make_test_docs(count)
        result = firebase.send_and_wait(
            'push_batch',
            {'collection': test_collection, 'docs': docs},
            timeout=30,
        )
        assert result['status'] == 'done'

        # Wait for docs in Firebase
        timeout = 5 + count * 0.2
        firebase.listen(test_collection, count, timeout=timeout)

        # Phase 2: Delete the first quarter
        delete_count = count // 4
        delete_ids = [f'doc_{i}' for i in range(delete_count)]
        result = firebase.send_and_wait(
            'delete_docs',
            {'collection': test_collection, 'doc_ids': delete_ids},
            timeout=30,
        )
        assert result['status'] == 'done'

        # Phase 3: Edit the second quarter
        edit_start = delete_count
        edit_end = delete_count * 2
        edits = {
            f'doc_{i}': {'_test_seq': i, 'value': f'updated_{i}', 'updated': True}
            for i in range(edit_start, edit_end)
        }
        # Need the docs to be in TD's table first
        wait_for_td_table(
            firebase, test_collection, count - delete_count, timeout=20
        )
        result = firebase.send_and_wait(
            'edit_cells',
            {'collection': test_collection, 'edits': edits},
            timeout=15,
        )
        assert result['status'] == 'done'
        result = firebase.send_and_wait('flush_dirty', {}, timeout=15)
        assert result['status'] == 'done'

        # Phase 4: Verify final state
        time.sleep(5)
        fb_docs = firebase.read_collection(test_collection)
        expected_remaining = count - delete_count
        assert len(fb_docs) == expected_remaining, (
            f'Expected {expected_remaining}, got {len(fb_docs)}'
        )

        # Verify deleted docs are gone
        for doc_id in delete_ids:
            assert doc_id not in fb_docs, f'{doc_id} should be deleted'

        # Verify edited docs have updated values
        edited_count = sum(
            1 for d in fb_docs.values() if d.get('updated') is True
        )
        expected_edits = edit_end - edit_start
        assert edited_count >= expected_edits, (
            f'Expected {expected_edits} edited docs, got {edited_count}'
        )
