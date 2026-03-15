"""
Shared fixtures for Firestore stress tests.

Session-scoped: MCP config read, Firebase client init.
Per-test: unique collection names, cleanup.
"""

import uuid
import time

import pytest

from mcp_client import McpClient
from firebase_client import FirebaseTestClient


# Track collections watched during this session for cleanup
_watched_collections = set()


def td_watch(mcp, collection):
    """Watch a collection in TD and track it for cleanup."""
    mcp.execute_python(
        f"op('/gcp/firestore').ext.FirestoreExt.WatchCollection('{collection}')"
    )
    _watched_collections.add(collection)


def td_unwatch(mcp, collection):
    """Unwatch a collection in TD."""
    try:
        mcp.execute_python(
            f"op('/gcp/firestore').ext.FirestoreExt.UnwatchCollection('{collection}')"
        )
    except Exception:
        pass
    _watched_collections.discard(collection)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def mcp():
    """MCP client for direct TD communication."""
    client = McpClient()
    yield client
    # Session teardown: unwatch everything we touched
    for col in list(_watched_collections):
        td_unwatch(client, col)


@pytest.fixture(scope='session')
def td_config(mcp):
    """Read service account path + database ID from TD via MCP (one-time)."""
    if not mcp.is_connected():
        pytest.skip('Firestore COMP is not connected in TD')
    return {
        'service_account': mcp.get_service_account_path(),
        'database_id': mcp.get_database_id(),
    }


@pytest.fixture(scope='session')
def firebase(td_config):
    """Direct Firebase client using same service account as TD."""
    client = FirebaseTestClient(
        td_config['service_account'],
        td_config['database_id'],
    )
    yield client
    try:
        client.cleanup_control_collections()
    except Exception:
        pass
    client.close()


@pytest.fixture(scope='session')
def agent(firebase):
    """
    Verify the TD-side stress test agent is running.
    Send a ping and confirm we get a pong.
    """
    try:
        result = firebase.send_and_wait('ping', {}, timeout=10)
        assert result['status'] == 'done', f'Agent ping failed: {result}'
    except TimeoutError:
        pytest.skip(
            'StressTestAgent not responding. '
            'Start it in TD: op("stress_test_agent").ext.StressTestAgent.Start()'
        )
    return True


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_id():
    """Unique ID for this test run."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
def test_collection(test_id, mcp, agent):
    """Unique collection name for test isolation. Auto-watches in TD via MCP."""
    name = f'__stress_test_{test_id}'
    td_watch(mcp, name)
    yield name
    td_unwatch(mcp, name)


@pytest.fixture(autouse=True)
def cleanup(firebase):
    """Clean command bus after each test."""
    yield
    try:
        firebase.cleanup_control_collections()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers available to all tests
# ---------------------------------------------------------------------------

def wait_for_td_table(firebase, collection, expected_count, timeout=30):
    """
    Poll TD agent's read_table command until row count matches expected.
    Returns the table rows.
    """
    deadline = time.monotonic() + timeout
    interval = 0.3
    last_count = 0
    while time.monotonic() < deadline:
        result = firebase.send_and_wait(
            'read_table', {'collection': collection}, timeout=10
        )
        if result['status'] != 'done':
            time.sleep(interval)
            continue
        rows = result['result']['rows']
        last_count = len(rows)
        if last_count >= expected_count:
            return rows
        time.sleep(interval)
        interval = min(interval * 1.2, 0.5)
    raise TimeoutError(
        f'Expected {expected_count} rows in TD table {collection}, '
        f'got {last_count} after {timeout}s'
    )


def make_test_docs(count, prefix='doc'):
    """Generate test documents with sequential _test_seq field."""
    return {
        f'{prefix}_{i}': {'_test_seq': i, 'value': f'data_{i}', 'batch': True}
        for i in range(count)
    }
