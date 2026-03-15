"""
Tests for ConnectionExt — circuit breaker with exponential backoff.

Covers all state transitions: CLOSED → OPEN → HALF_OPEN → CLOSED,
failure counting, backoff calculations, thread safety, and reset.
"""

import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dev' / 'gcp' / 'firestore'))

from td_mocks import MockOwnerComp, MockParameter
from ext_connection import ConnectionExt


@pytest.fixture
def conn():
    """Fresh ConnectionExt with threshold=3, timeout=1s for fast tests."""
    owner = MockOwnerComp('firestore', {
        'Circuitfailurethreshold': 3,
        'Circuittimeout': 1,  # 1 second for fast HALF_OPEN transition
        'Backoffbase': 2.0,
        'Backoffmax': 60,
    })
    return ConnectionExt(owner)


# ═══════════════════════════════════════════════════════════════════════════
# Initial state
# ═══════════════════════════════════════════════════════════════════════════

class TestInitialState:
    def test_starts_closed(self, conn):
        assert conn.GetState() == 'closed'

    def test_can_attempt_initially(self, conn):
        assert conn.CanAttempt() is True

    def test_backoff_initially_one_second(self, conn):
        # 2^0 = 1
        assert conn.GetBackoffSeconds() == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# CLOSED → OPEN transition
# ═══════════════════════════════════════════════════════════════════════════

class TestClosedToOpen:
    def test_single_failure_stays_closed(self, conn):
        conn.RecordFailure()
        assert conn.GetState() == 'closed'
        assert conn.CanAttempt() is True

    def test_two_failures_stays_closed(self, conn):
        conn.RecordFailure()
        conn.RecordFailure()
        assert conn.GetState() == 'closed'

    def test_threshold_failures_opens_circuit(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        assert conn.GetState() == 'open'
        assert conn.CanAttempt() is False

    def test_more_than_threshold_stays_open(self, conn):
        for _ in range(5):
            conn.RecordFailure()
        assert conn.GetState() == 'open'


# ═══════════════════════════════════════════════════════════════════════════
# OPEN → HALF_OPEN transition
# ═══════════════════════════════════════════════════════════════════════════

class TestOpenToHalfOpen:
    def test_open_blocks_attempts(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        assert conn.CanAttempt() is False

    def test_timeout_transitions_to_half_open(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        assert conn.CanAttempt() is False

        # Wait for timeout (1 second)
        time.sleep(1.1)
        assert conn.CanAttempt() is True
        assert conn.GetState() == 'half_open'


# ═══════════════════════════════════════════════════════════════════════════
# HALF_OPEN → CLOSED (success) or → OPEN (failure)
# ═══════════════════════════════════════════════════════════════════════════

class TestHalfOpenTransitions:
    def test_half_open_success_closes_circuit(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        time.sleep(1.1)
        conn.CanAttempt()  # triggers HALF_OPEN

        conn.RecordSuccess()
        assert conn.GetState() == 'closed'
        assert conn.CanAttempt() is True

    def test_half_open_failure_reopens_circuit(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        time.sleep(1.1)
        conn.CanAttempt()  # triggers HALF_OPEN

        conn.RecordFailure()
        assert conn.GetState() == 'open'
        assert conn.CanAttempt() is False


# ═══════════════════════════════════════════════════════════════════════════
# RecordSuccess
# ═══════════════════════════════════════════════════════════════════════════

class TestRecordSuccess:
    def test_success_resets_from_closed(self, conn):
        conn.RecordFailure()
        conn.RecordSuccess()
        assert conn.GetState() == 'closed'

    def test_success_resets_failure_count(self, conn):
        conn.RecordFailure()
        conn.RecordFailure()
        conn.RecordSuccess()
        # Now it should take full threshold to reopen
        conn.RecordFailure()
        conn.RecordFailure()
        assert conn.GetState() == 'closed'

    def test_success_after_reopen_cycle(self, conn):
        """Full cycle: closed → open → half_open → closed."""
        for _ in range(3):
            conn.RecordFailure()
        assert conn.GetState() == 'open'

        time.sleep(1.1)
        assert conn.CanAttempt() is True  # half_open

        conn.RecordSuccess()
        assert conn.GetState() == 'closed'


# ═══════════════════════════════════════════════════════════════════════════
# Exponential backoff
# ═══════════════════════════════════════════════════════════════════════════

class TestBackoff:
    def test_backoff_increases_exponentially(self, conn):
        delays = []
        for _ in range(6):
            conn.RecordFailure()
            delays.append(conn.GetBackoffSeconds())

        # Base=2: 2^1=2, 2^2=4, 2^3=8, 2^4=16, 2^5=32, 2^6=60(capped)
        assert delays[0] == 2.0
        assert delays[1] == 4.0
        assert delays[2] == 8.0
        assert delays[3] == 16.0
        assert delays[4] == 32.0

    def test_backoff_capped_at_max(self, conn):
        for _ in range(20):
            conn.RecordFailure()
        assert conn.GetBackoffSeconds() <= 60.0

    def test_backoff_resets_after_success(self, conn):
        for _ in range(5):
            conn.RecordFailure()
        conn.RecordSuccess()
        # Should be back to base^0 = 1
        assert conn.GetBackoffSeconds() == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Reset
# ═══════════════════════════════════════════════════════════════════════════

class TestReset:
    def test_reset_from_open(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        assert conn.GetState() == 'open'

        conn.Reset()
        assert conn.GetState() == 'closed'
        assert conn.CanAttempt() is True

    def test_reset_clears_failure_count(self, conn):
        for _ in range(2):
            conn.RecordFailure()
        conn.Reset()
        # Should need full threshold again
        conn.RecordFailure()
        conn.RecordFailure()
        assert conn.GetState() == 'closed'

    def test_reset_from_half_open(self, conn):
        for _ in range(3):
            conn.RecordFailure()
        time.sleep(1.1)
        conn.CanAttempt()
        assert conn.GetState() == 'half_open'

        conn.Reset()
        assert conn.GetState() == 'closed'


# ═══════════════════════════════════════════════════════════════════════════
# Thread safety
# ═══════════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    def test_concurrent_failures(self, conn):
        """Hammer RecordFailure from multiple threads."""
        errors = []

        def fail_many():
            try:
                for _ in range(100):
                    conn.RecordFailure()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fail_many) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert conn.GetState() == 'open'

    def test_concurrent_mixed_operations(self, conn):
        """Mix RecordFailure, RecordSuccess, CanAttempt, GetState from threads."""
        errors = []

        def mixed_ops():
            try:
                for _ in range(50):
                    conn.CanAttempt()
                    conn.RecordFailure()
                    conn.GetState()
                    conn.GetBackoffSeconds()
                    conn.RecordSuccess()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mixed_ops) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_success_without_any_failure(self, conn):
        conn.RecordSuccess()
        assert conn.GetState() == 'closed'

    def test_rapid_open_close_cycles(self, conn):
        """Multiple open/close cycles should be stable."""
        for cycle in range(5):
            for _ in range(3):
                conn.RecordFailure()
            assert conn.GetState() == 'open'
            conn.Reset()
            assert conn.GetState() == 'closed'

    def test_different_thresholds(self):
        """Test with threshold=1 (immediate open on first failure)."""
        owner = MockOwnerComp('firestore', {
            'Circuitfailurethreshold': 1,
            'Circuittimeout': 1,
            'Backoffbase': 2.0,
            'Backoffmax': 10,
        })
        c = ConnectionExt(owner)
        c.RecordFailure()
        assert c.GetState() == 'open'

    def test_high_threshold(self):
        """Test with very high threshold."""
        owner = MockOwnerComp('firestore', {
            'Circuitfailurethreshold': 100,
            'Circuittimeout': 1,
            'Backoffbase': 2.0,
            'Backoffmax': 10,
        })
        c = ConnectionExt(owner)
        for _ in range(99):
            c.RecordFailure()
        assert c.GetState() == 'closed'
        c.RecordFailure()
        assert c.GetState() == 'open'