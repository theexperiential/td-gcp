"""
Inbound stress tests: Firebase -> TD propagation.

Injects documents directly into Firestore and verifies they appear
in TD's tableDATs via the stress test agent.
"""

import json
import time

import pytest

from conftest import wait_for_td_table, make_test_docs, td_watch, td_unwatch


pytestmark = pytest.mark.stress


class TestBatchAdd:
    """Write N docs to Firebase, verify all appear in TD tableDAT."""

    @pytest.mark.parametrize('count', [10, 50, 100, 250, 500])
    def test_batch_add(self, firebase, agent, test_collection, count):
        docs = make_test_docs(count)

        # Inject directly into Firestore
        firebase.write_batch(test_collection, docs)

        # Wait for TD to pick them all up via snapshot listener
        timeout = 5 + count * 0.3
        rows = wait_for_td_table(firebase, test_collection, count, timeout=timeout)

        assert len(rows) == count, (
            f'Expected {count} rows, got {len(rows)}'
        )

        # Verify all _test_seq values are present (no gaps)
        seqs = {r['_test_seq'] for r in rows}
        expected = set(range(count))
        assert seqs == expected, f'Missing sequences: {expected - seqs}'


class TestDataIntegrity:
    """Verify payload JSON matches what was written, field by field."""

    @pytest.mark.parametrize('count', [100, 500])
    def test_data_integrity(self, firebase, agent, test_collection, count):
        docs = make_test_docs(count)
        firebase.write_batch(test_collection, docs)

        timeout = 5 + count * 0.3
        rows = wait_for_td_table(firebase, test_collection, count, timeout=timeout)

        # Build lookup by _doc_id
        td_docs = {r['_doc_id']: r for r in rows}

        for doc_id, original in docs.items():
            assert doc_id in td_docs, f'Missing doc: {doc_id}'
            td_doc = td_docs[doc_id]
            for key, value in original.items():
                assert td_doc.get(key) == value, (
                    f'{doc_id}.{key}: expected {value!r}, got {td_doc.get(key)!r}'
                )


class TestRapidUpdates:
    """Update the same doc N times, verify last-write-wins in TD."""

    @pytest.mark.parametrize('count', [50, 100])
    def test_rapid_updates_same_doc(self, firebase, agent, test_collection, count):
        doc_id = 'rapid_target'

        # Write doc N times with increasing values
        for i in range(count):
            firebase.write_doc(test_collection, doc_id, {
                'iteration': i,
                'final': i == count - 1,
            })

        # Wait for at least 1 doc in TD, then give extra time for updates
        timeout = 10 + count * 0.1
        time.sleep(min(5, timeout / 3))  # let updates propagate

        result = firebase.send_and_wait(
            'read_table', {'collection': test_collection}, timeout=10
        )
        assert result['status'] == 'done'
        rows = result['result']['rows']

        assert len(rows) == 1, f'Expected 1 row, got {len(rows)}'

        # Last write should win
        doc = rows[0]
        assert doc['iteration'] == count - 1, (
            f'Expected iteration {count - 1}, got {doc["iteration"]}'
        )
        assert doc['final'] is True


class TestInterleavedAddRemove:
    """Add docs, then remove some, verify correct count remains in TD."""

    def test_add_then_remove(self, firebase, agent, test_collection):
        # Add 50 docs
        docs = make_test_docs(50)
        firebase.write_batch(test_collection, docs)
        wait_for_td_table(firebase, test_collection, 50, timeout=25)

        # Remove last 25
        for i in range(25, 50):
            firebase.delete_doc(test_collection, f'doc_{i}')

        # Wait for removals to propagate
        time.sleep(5)

        result = firebase.send_and_wait(
            'read_table', {'collection': test_collection}, timeout=10
        )
        rows = result['result']['rows']
        assert len(rows) == 25, f'Expected 25 remaining, got {len(rows)}'

        # Verify the right docs survived
        remaining_seqs = {r['_test_seq'] for r in rows}
        expected = set(range(25))
        assert remaining_seqs == expected


class TestMultiCollection:
    """Inject docs across multiple test collections simultaneously."""

    def test_3_collections_100_each(self, firebase, agent, test_id, mcp):
        collections = [f'__stress_test_{test_id}_col{i}' for i in range(3)]
        count_per = 100

        for col in collections:
            td_watch(mcp, col)

        # Inject into all 3 collections
        for col in collections:
            docs = make_test_docs(count_per, prefix=col.split('_')[-1])
            firebase.write_batch(col, docs)

        # Verify each collection
        timeout = 5 + count_per * 0.3
        for col in collections:
            rows = wait_for_td_table(firebase, col, count_per, timeout=timeout)
            assert len(rows) == count_per, (
                f'{col}: expected {count_per}, got {len(rows)}'
            )

        for col in collections:
            td_unwatch(mcp, col)

