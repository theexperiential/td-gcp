"""
Shared pytest fixtures for td-gcp test suite.
Classes live in td_mocks.py; this file only contains fixtures.
"""

import sys
import datetime
from pathlib import Path

import pytest

# Ensure project paths are importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'tests'))
sys.path.insert(0, str(PROJECT_ROOT / 'dev' / 'gcp' / 'firestore'))

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'
FAKE_SERVICE_ACCOUNT = str(FIXTURES_DIR / 'fake_service_account.json')

from td_mocks import (
    FakeDatetimeWithNanoseconds,
    FakeDocumentReference,
    FakeGeoPoint,
    MockOwnerComp,
    MockTableDAT,
)


@pytest.fixture
def fake_service_account_path():
    return FAKE_SERVICE_ACCOUNT


@pytest.fixture
def tmp_project_folder(tmp_path):
    return str(tmp_path)


@pytest.fixture
def mock_owner():
    pars = {
        'Privatekey': FAKE_SERVICE_ACCOUNT,
        'Databaseid': '(default)',
        'Autoconnect': False,
        'Enablelistener': False,
        'Collections': 'users scenes status',
        'Filterfields': 'waiverSignature secret',
        'Enablecache': True,
        'Cachehydrate': False,
        'Cachepath': 'cache',
        'Callbacksdat': '',
        'Logop': '',
        'Circuitfailurethreshold': 3,
        'Circuittimeout': 30,
        'Backoffbase': 2.0,
        'Backoffmax': 60,
    }
    owner = MockOwnerComp('firestore', pars)
    status_dat = MockTableDAT('status')
    status_dat.appendRow(['state', 'circuit', 'last_error', 'queue_depth', 'connected_at', 'collections'])
    status_dat.appendRow(['disconnected', 'closed', '', '0', '', ''])
    owner.register_op('status', status_dat)
    owner.register_op('log', MockTableDAT('log'))
    return owner


@pytest.fixture
def status_dat(mock_owner):
    return mock_owner.op('status')


@pytest.fixture
def all_firestore_types():
    now = FakeDatetimeWithNanoseconds(2025, 6, 15, 12, 30, 45, 123456,
                                       tzinfo=datetime.timezone.utc,
                                       nanosecond=123456789)
    return {
        'null_field': None,
        'bool_true': True,
        'bool_false': False,
        'int_zero': 0,
        'int_positive': 42,
        'int_negative': -99,
        'int_large': 2**53,
        'float_zero': 0.0,
        'float_positive': 3.14159,
        'float_negative': -273.15,
        'float_inf': float('inf'),
        'float_neg_inf': float('-inf'),
        'float_nan': float('nan'),
        'string_empty': '',
        'string_simple': 'hello world',
        'string_unicode': '\u00e9\u00e8\u00ea \u00fc\u00f6\u00e4 \U0001f525\U0001f680',
        'string_newlines': 'line1\nline2\ttab',
        'string_json_like': '{"not": "a real dict"}',
        'string_long': 'x' * 10000,
        'timestamp_utc': now,
        'timestamp_naive': FakeDatetimeWithNanoseconds(2025, 1, 1, 0, 0, 0),
        'timestamp_with_nanos': FakeDatetimeWithNanoseconds(
            2025, 12, 31, 23, 59, 59, 0,
            tzinfo=datetime.timezone.utc, nanosecond=999999999),
        'doc_ref_simple': FakeDocumentReference('users/abc123', 'abc123'),
        'doc_ref_nested': FakeDocumentReference('projects/p1/databases/db/documents/col/doc', 'doc'),
        'geopoint': FakeGeoPoint(40.7128, -74.0060),
        'geopoint_zero': FakeGeoPoint(0.0, 0.0),
        'array_empty': [],
        'array_ints': [1, 2, 3],
        'array_mixed': [1, 'two', 3.0, True, None],
        'array_nested': [[1, 2], [3, [4, 5]]],
        'array_with_map': [{'key': 'value'}, {'nested': {'deep': True}}],
        'array_with_timestamp': [now],
        'array_with_ref': [FakeDocumentReference('col/ref1', 'ref1')],
        'map_empty': {},
        'map_simple': {'name': 'Alice', 'age': 30},
        'map_nested': {'level1': {'level2': {'level3': {'deep_value': 'found_it'}}}},
        'map_with_array': {'tags': ['a', 'b', 'c'], 'count': 3},
        'map_with_all_types': {
            'n': None, 'b': True, 'i': 42, 'f': 1.5,
            's': 'text', 'a': [1, 2], 'd': {'inner': 'map'},
        },
        'bytes_field': b'\x00\x01\x02\xff',
    }


@pytest.fixture
def sample_user_doc():
    return {
        'displayName': 'Test User',
        'email': 'test@example.com',
        'photoURL': 'https://example.com/photo.jpg',
        'role': 'admin',
        'active': True,
        'loginCount': 42,
        'lastLogin': FakeDatetimeWithNanoseconds(
            2025, 6, 15, 10, 30, 0, tzinfo=datetime.timezone.utc),
        'preferences': {'theme': 'dark', 'notifications': True, 'volume': 0.8},
        'tags': ['vip', 'beta-tester'],
        'waiverSignature': 'SHOULD_BE_FILTERED',
    }


@pytest.fixture
def sample_scene_doc():
    return {
        'label': 'Coral Reef',
        'presets': ['preset1', 'preset2', 'preset3'],
        'deleted': False,
    }


@pytest.fixture
def sample_status_doc():
    return {
        'scene_now': 'coral',
        'preset_now': 'preset1',
        'in_transition': False,
        'blackout': False,
        'pattern': False,
        'takeover': False,
        'start': FakeDatetimeWithNanoseconds(2025, 6, 15, 18, 0, 0, tzinfo=datetime.timezone.utc),
        'end': FakeDatetimeWithNanoseconds(2025, 6, 16, 2, 0, 0, tzinfo=datetime.timezone.utc),
        'scene_id': 'scene_001',
    }


@pytest.fixture
def sample_schedule_doc():
    return {
        'scene': 'aurora',
        'preset': 'preset2',
        'start': FakeDatetimeWithNanoseconds(2025, 6, 15, 20, 0, 0, tzinfo=datetime.timezone.utc),
        'transition_duration': 32,
        'transition': 'noiseSmall',
    }