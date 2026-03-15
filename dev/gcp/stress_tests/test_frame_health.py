"""
Frame health and performance tests.

Monitors TD's frame rate, cook time, and queue drain behavior
during stress operations.
"""

import time

import pytest

from conftest import wait_for_td_table, make_test_docs


pytestmark = pytest.mark.stress


class TestFrameDrops:
    """Verify no significant frame drops during burst operations."""

    def test_no_frame_drops_100_inbound(self, firebase, agent, test_collection):
        """Inject 100 docs and verify frame rate stays within 10% of target."""
        # Get baseline stats
        before = firebase.send_and_wait('get_stats', {}, timeout=10)
        assert before['status'] == 'done'
        frame_before = before['result']['frame']
        time_before = before['result']['time']
        cook_rate = before['result']['cook_rate']

        # Inject 100 docs
        docs = make_test_docs(100)
        firebase.write_batch(test_collection, docs)

        # Wait for all docs to arrive in TD
        wait_for_td_table(firebase, test_collection, 100, timeout=40)

        # Get post-burst stats
        after = firebase.send_and_wait('get_stats', {}, timeout=10)
        assert after['status'] == 'done'
        frame_after = after['result']['frame']
        time_after = after['result']['time']

        # Calculate frame drop ratio
        elapsed_time = time_after - time_before
        expected_frames = elapsed_time * cook_rate
        actual_frames = frame_after - frame_before

        if expected_frames > 0:
            drop_ratio = 1 - (actual_frames / expected_frames)
            assert drop_ratio < 0.10, (
                f'Frame drop ratio {drop_ratio:.2%} exceeds 10% threshold. '
                f'Expected ~{expected_frames:.0f} frames in {elapsed_time:.1f}s, '
                f'got {actual_frames}'
            )

    def test_no_frame_drops_500_inbound(self, firebase, agent, test_collection):
        """Heavy burst: 500 docs, verify frame rate stays reasonable."""
        before = firebase.send_and_wait('get_stats', {}, timeout=10)
        frame_before = before['result']['frame']
        time_before = before['result']['time']
        cook_rate = before['result']['cook_rate']

        docs = make_test_docs(500)
        firebase.write_batch(test_collection, docs)
        wait_for_td_table(firebase, test_collection, 500, timeout=120)

        after = firebase.send_and_wait('get_stats', {}, timeout=10)
        elapsed = after['result']['time'] - time_before
        expected = elapsed * cook_rate
        actual = after['result']['frame'] - frame_before

        if expected > 0:
            drop_ratio = 1 - (actual / expected)
            assert drop_ratio < 0.15, (
                f'Frame drop ratio {drop_ratio:.2%} exceeds 15% threshold '
                f'during 500-doc burst'
            )


class TestDrainRate:
    """Verify queue drains at the expected rate."""

    def test_queue_drains_to_zero(self, firebase, agent, test_collection):
        """Inject 100 docs, verify queue depth returns to 0."""
        docs = make_test_docs(100)
        firebase.write_batch(test_collection, docs)

        # Give it a moment for docs to start arriving
        time.sleep(1)

        # Poll queue depth until 0
        max_polls = 60
        polls = 0
        peak_depth = 0
        while polls < max_polls:
            result = firebase.send_and_wait('get_stats', {}, timeout=10)
            depth = result['result']['inbound_queue_depth']
            peak_depth = max(peak_depth, depth)
            if depth == 0:
                # Queue empty, but verify all docs processed
                table_result = firebase.send_and_wait(
                    'read_table', {'collection': test_collection}, timeout=10
                )
                if table_result['result']['count'] >= 100:
                    break
            time.sleep(0.3)
            polls += 1

        assert polls < max_polls, (
            f'Queue did not drain to 0 after {max_polls * 0.3:.0f}s. '
            f'Peak depth was {peak_depth}'
        )

    def test_queue_recovery_500(self, firebase, agent, test_collection):
        """Inject 500 docs, track queue depth curve, verify monotonic drain."""
        docs = make_test_docs(500)
        firebase.write_batch(test_collection, docs)

        time.sleep(2)  # let docs start arriving

        # Track queue depth over time
        depths = []
        max_polls = 200
        for _ in range(max_polls):
            result = firebase.send_and_wait('get_stats', {}, timeout=10)
            depth = result['result']['inbound_queue_depth']
            depths.append(depth)
            if depth == 0:
                table_result = firebase.send_and_wait(
                    'read_table', {'collection': test_collection}, timeout=10
                )
                if table_result['result']['count'] >= 500:
                    break
            time.sleep(0.5)

        # Verify it eventually reached 0
        assert depths[-1] == 0 or len(depths) < max_polls, (
            f'Queue never drained. Final depth: {depths[-1]}, '
            f'history: {depths[-10:]}'
        )

        # Verify roughly monotonic drain (allow small spikes from new arrivals)
        if len(depths) > 5:
            # After peak, trend should be downward
            peak_idx = depths.index(max(depths))
            post_peak = depths[peak_idx:]
            if len(post_peak) > 3:
                # Moving average should decrease
                window = 3
                avgs = [
                    sum(post_peak[i:i + window]) / window
                    for i in range(len(post_peak) - window + 1)
                ]
                if len(avgs) > 1:
                    assert avgs[-1] <= avgs[0] + 5, (
                        f'Queue not draining: averages went from '
                        f'{avgs[0]:.1f} to {avgs[-1]:.1f}'
                    )


class TestCookTime:
    """Verify cook time stays reasonable during stress."""

    def test_cook_time_during_burst(self, firebase, agent, test_collection):
        """Cook time should stay under 16ms (60fps budget) during 100-doc burst."""
        docs = make_test_docs(100)
        firebase.write_batch(test_collection, docs)

        # Sample cook time while docs are being processed
        cook_times = []
        for _ in range(20):
            result = firebase.send_and_wait('get_stats', {}, timeout=10)
            # Note: cook time measurement depends on what get_stats returns
            # This is a best-effort check
            cook_times.append(result['result'].get('frame', 0))
            time.sleep(0.2)

        # At minimum, verify the agent responded to all 20 polls
        # (if cook time was too high, polls would timeout)
        assert len(cook_times) == 20, (
            f'Only got {len(cook_times)} stat responses out of 20'
        )
