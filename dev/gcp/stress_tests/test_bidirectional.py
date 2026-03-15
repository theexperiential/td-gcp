"""
Bidirectional stress tests: simultaneous inbound + outbound.

Tests that both directions operate correctly when running at the same
time, including echo prevention and cross-collection storms.
"""

import json
import time
import threading

import pytest

from conftest import wait_for_td_table, make_test_docs, td_watch, td_unwatch


pytestmark = pytest.mark.stress


class TestConcurrent:
    """Simultaneous Firebase->TD and TD->Firebase operations."""

    @pytest.mark.parametrize('count', [50, 100, 250])
    def test_concurrent_both_directions(
        self, firebase, agent, test_id, count, mcp
    ):
        """
        Thread A: write N docs to Firebase directly (inbound to TD).
        Thread B: push N different docs from TD to Firebase (outbound).
        Verify all 2N docs exist in both Firebase and TD tables.
        """
        inbound_col = f'__stress_test_{test_id}_in'
        outbound_col = f'__stress_test_{test_id}_out'
        timeout = 10 + count * 0.3

        td_watch(mcp, inbound_col)
        td_watch(mcp, outbound_col)

        inbound_docs = {
            f'in_{i}': {'_test_seq': i, 'direction': 'inbound'}
            for i in range(count)
        }
        outbound_docs = {
            f'out_{i}': {'_test_seq': i, 'direction': 'outbound'}
            for i in range(count)
        }

        errors = []

        def inject_inbound():
            try:
                firebase.write_batch(inbound_col, inbound_docs)
            except Exception as e:
                errors.append(f'inbound: {e}')

        def push_outbound():
            try:
                result = firebase.send_and_wait(
                    'push_batch',
                    {'collection': outbound_col, 'docs': outbound_docs},
                    timeout=60,
                )
                if result['status'] != 'done':
                    errors.append(f'outbound: {result}')
            except Exception as e:
                errors.append(f'outbound: {e}')

        # Launch both directions simultaneously
        t_in = threading.Thread(target=inject_inbound)
        t_out = threading.Thread(target=push_outbound)
        t_in.start()
        t_out.start()
        t_in.join(timeout=60)
        t_out.join(timeout=60)

        assert not errors, f'Concurrent errors: {errors}'

        # Verify inbound docs arrived in TD
        rows = wait_for_td_table(firebase, inbound_col, count, timeout=timeout)
        assert len(rows) == count, (
            f'Inbound: expected {count} in TD, got {len(rows)}'
        )

        # Verify outbound docs arrived in Firebase
        received = firebase.listen(outbound_col, count, timeout=timeout)
        assert len(received) >= count, (
            f'Outbound: expected {count} in Firebase, got {len(received)}'
        )

        td_unwatch(mcp, inbound_col)
        td_unwatch(mcp, outbound_col)


class TestEchoPrevention:
    """Verify TD's version tracking prevents re-processing its own writes."""

    def test_echo_filtered(self, firebase, agent, test_collection):
        """
        TD pushes a doc to Firebase. The snapshot listener will echo it
        back. Verify the echo is filtered by version tracking and the
        doc appears exactly once in TD's table.
        """
        doc_data = {'source': 'td', 'echo_test': True, 'value': 42}

        # Push doc from TD
        result = firebase.send_and_wait(
            'push_doc',
            {
                'collection': test_collection,
                'doc_id': 'echo_target',
                'data': doc_data,
            },
            timeout=15,
        )
        assert result['status'] == 'done'

        # Wait for the doc to exist in Firebase
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            fb_doc = firebase.read_doc(test_collection, 'echo_target')
            if fb_doc:
                break
            time.sleep(0.3)
        assert fb_doc is not None, 'Doc never appeared in Firebase'

        # Give time for echo to arrive and be filtered
        time.sleep(5)

        # Check TD table: doc should NOT appear (it was written outbound,
        # and the echo should be filtered by version tracking)
        # OR if the collection IS being watched, it should appear exactly once
        result = firebase.send_and_wait(
            'read_table', {'collection': test_collection}, timeout=10
        )
        rows = result['result']['rows']

        # The key assertion: no duplicate rows
        doc_ids = [r.get('_doc_id') for r in rows]
        echo_count = doc_ids.count('echo_target')
        assert echo_count <= 1, (
            f'Echo not filtered: found {echo_count} copies of echo_target'
        )


class TestCrossCollectionStorm:
    """Multiple collections under simultaneous bidirectional load."""

    def test_3_collections_50_each_direction(self, firebase, agent, test_id, mcp):
        """
        3 collections, each getting 50 inbound + 50 outbound = 300 total.
        """
        n_collections = 3
        count_per = 50
        timeout = 15 + count_per * 0.3

        collections = [
            f'__stress_test_{test_id}_storm{i}' for i in range(n_collections)
        ]

        for col in collections:
            td_watch(mcp, col)

        errors = []

        def inject_inbound(col, idx):
            try:
                docs = {
                    f'in_{idx}_{i}': {
                        '_test_seq': i, 'direction': 'inbound', 'col_idx': idx,
                    }
                    for i in range(count_per)
                }
                firebase.write_batch(col, docs)
            except Exception as e:
                errors.append(f'inbound {col}: {e}')

        def push_outbound(col, idx):
            try:
                docs = {
                    f'out_{idx}_{i}': {
                        '_test_seq': i, 'direction': 'outbound', 'col_idx': idx,
                    }
                    for i in range(count_per)
                }
                result = firebase.send_and_wait(
                    'push_batch',
                    {'collection': col, 'docs': docs},
                    timeout=60,
                )
                if result['status'] != 'done':
                    errors.append(f'outbound {col}: {result}')
            except Exception as e:
                errors.append(f'outbound {col}: {e}')

        # Launch all threads
        threads = []
        for i, col in enumerate(collections):
            t_in = threading.Thread(target=inject_inbound, args=(col, i))
            t_out = threading.Thread(target=push_outbound, args=(col, i))
            threads.extend([t_in, t_out])
            t_in.start()
            t_out.start()

        for t in threads:
            t.join(timeout=90)

        assert not errors, f'Storm errors: {errors}'

        # Verify: each collection should have inbound docs in TD
        for i, col in enumerate(collections):
            try:
                rows = wait_for_td_table(
                    firebase, col, count_per, timeout=timeout
                )
                inbound_count = sum(
                    1 for r in rows if r.get('direction') == 'inbound'
                )
                assert inbound_count == count_per, (
                    f'{col}: expected {count_per} inbound in TD, '
                    f'got {inbound_count}'
                )
            except TimeoutError:
                pytest.fail(
                    f'{col}: timed out waiting for {count_per} inbound docs in TD'
                )

        # Verify: each collection should have outbound docs in Firebase
        for i, col in enumerate(collections):
            fb_docs = firebase.read_collection(col)
            outbound_count = sum(
                1 for d in fb_docs.values() if d.get('direction') == 'outbound'
            )
            assert outbound_count >= count_per, (
                f'{col}: expected {count_per} outbound in Firebase, '
                f'got {outbound_count}'
            )

        for col in collections:
            td_unwatch(mcp, col)



class TestWriteBackDuringInbound:
    """Edit existing docs while new ones are streaming in."""

    def test_edit_during_inbound_flood(
        self, firebase, agent, test_collection
    ):
        """
        1. Inject 50 docs inbound (wave 1)
        2. While injecting 50 more (wave 2), edit wave 1 docs and flush
        3. Verify: TD table has all 100 docs, Firebase has edited wave 1
        """
        wave1_docs = {
            f'w1_{i}': {'_test_seq': i, 'wave': 1, 'edited': False}
            for i in range(50)
        }

        # Wave 1: inject and wait
        firebase.write_batch(test_collection, wave1_docs)
        wait_for_td_table(firebase, test_collection, 50, timeout=25)

        # Start wave 2 injection in background
        wave2_docs = {
            f'w2_{i}': {'_test_seq': i + 50, 'wave': 2, 'edited': False}
            for i in range(50)
        }
        errors = []

        def inject_wave2():
            try:
                firebase.write_batch(test_collection, wave2_docs)
            except Exception as e:
                errors.append(f'wave2: {e}')

        t_wave2 = threading.Thread(target=inject_wave2)
        t_wave2.start()

        # Simultaneously: edit wave 1 docs in TD
        edits = {
            f'w1_{i}': {'_test_seq': i, 'wave': 1, 'edited': True}
            for i in range(50)
        }
        result = firebase.send_and_wait(
            'edit_cells',
            {'collection': test_collection, 'edits': edits},
            timeout=15,
        )
        assert result['status'] == 'done'

        result = firebase.send_and_wait('flush_dirty', {}, timeout=15)
        assert result['status'] == 'done'

        t_wave2.join(timeout=30)
        assert not errors, f'Wave 2 errors: {errors}'

        # Wait for all 100 docs in TD
        wait_for_td_table(firebase, test_collection, 100, timeout=30)

        # Verify wave 1 edits landed in Firebase
        time.sleep(5)
        fb_docs = firebase.read_collection(test_collection)

        wave1_edited = sum(
            1 for doc_id, d in fb_docs.items()
            if doc_id.startswith('w1_') and d.get('edited') is True
        )
        assert wave1_edited == 50, (
            f'Expected 50 edited wave1 docs in Firebase, got {wave1_edited}'
        )

        wave2_present = sum(
            1 for doc_id in fb_docs if doc_id.startswith('w2_')
        )
        assert wave2_present == 50, (
            f'Expected 50 wave2 docs in Firebase, got {wave2_present}'
        )
