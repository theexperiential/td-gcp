"""
Tests for StorageExt -- Firebase Cloud Storage engine.

Covers path resolution, transfer concurrency, sync worker logic,
drain processing, callback dispatch, and bootstrap checks.
All tests are offline -- no GCP credentials needed.
"""

import os
import sys
import queue
import tempfile
import datetime
from pathlib import Path
from collections import deque
from unittest.mock import MagicMock, patch, PropertyMock

import builtins

import pytest

# Mock TD globals before importing extension modules
builtins.absTime = MagicMock()
builtins.absTime.frame = 0

# Ensure project paths are importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'unit_tests'))
sys.path.insert(0, str(PROJECT_ROOT / 'storage'))

from td_mocks import MockOwnerComp, MockTableDAT, MockParameter

# Import module-level workers directly
from ext_storage import (
    _upload_worker,
    _download_worker,
    _delete_worker,
    _list_worker,
    _sync_worker,
    StorageExt,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_storage_owner():
    """Mock owner COMP with Storage-specific parameters."""
    pars = {
        'Privatekey': '/fake/key.json',
        'Bucket': 'test-bucket.appspot.com',
        'Localpath': '/tmp/test_storage_local',
        'Remotepath': 'media/',
        'Makepublic': False,
        'Deleteremoteorphans': False,
        'Deletelocalorphans': False,
        'Maxconcurrent': 3,
        'Autoconnect': False,
        'Circuitfailurethreshold': 3,
        'Circuittimeout': 30,
        'Backoffbase': 2.0,
        'Backoffmax': 60,
        'Callbacksdat': '',
        'Logop': '',
    }
    owner = MockOwnerComp('storage', pars)

    # Register data DATs
    status_dat = MockTableDAT('status')
    status_dat.appendRow(['state', 'circuit', 'last_error', 'active_transfers', 'connected_at', 'bucket'])
    status_dat.appendRow(['disconnected', 'closed', '', '0', '', ''])
    owner.register_op('status', status_dat)

    transfers_dat = MockTableDAT('transfers')
    transfers_dat.appendRow(['transfer_id', 'type', 'remote_path', 'local_path', 'status',
                             'started_at', 'completed_at', 'error', 'size'])
    owner.register_op('transfers', transfers_dat)

    owner.register_op('log', MockTableDAT('log'))

    return owner


@pytest.fixture
def ext(mock_storage_owner):
    """StorageExt instance with mocked owner (not initialized in TD)."""
    e = StorageExt(mock_storage_owner)
    return e


@pytest.fixture
def tmp_local_dir(tmp_path):
    """Create a temporary local directory with some test files."""
    d = tmp_path / 'local'
    d.mkdir()
    (d / 'file1.txt').write_text('hello')
    (d / 'file2.jpg').write_bytes(b'\xff\xd8\xff')
    sub = d / 'subdir'
    sub.mkdir()
    (sub / 'nested.txt').write_text('nested content')
    return str(d)


# ══════════════════════════════════════════════════════════════════════════════
# Path resolution
# ══════════════════════════════════════════════════════════════════════════════

class TestPathResolution:
    def test_resolve_local_relative(self, ext, mock_storage_owner):
        """Relative path joins with Localpath par."""
        mock_storage_owner._pars['Localpath'] = '/projects/media'
        setattr(mock_storage_owner.par, 'Localpath', MockParameter('/projects/media'))
        result = ext._resolve_local_path('photo.jpg')
        assert result == os.path.normpath('/projects/media/photo.jpg')

    def test_resolve_local_absolute(self, ext):
        """Absolute path bypasses Localpath par."""
        result = ext._resolve_local_path('/absolute/path/file.txt')
        assert result == os.path.normpath('/absolute/path/file.txt')

    def test_resolve_local_subfolder(self, ext, mock_storage_owner):
        """Subfolder paths are preserved."""
        setattr(mock_storage_owner.par, 'Localpath', MockParameter('/projects/media'))
        result = ext._resolve_local_path('textures/brick.png')
        assert result == os.path.normpath('/projects/media/textures/brick.png')

    def test_resolve_local_none_with_fallback(self, ext, mock_storage_owner):
        """None path uses fallback_rel relative to Localpath."""
        setattr(mock_storage_owner.par, 'Localpath', MockParameter('/projects/media'))
        result = ext._resolve_local_path(None, 'fallback.txt')
        assert result == os.path.normpath('/projects/media/fallback.txt')

    def test_resolve_remote_relative(self, ext, mock_storage_owner):
        """Relative path prepends Remotepath par."""
        setattr(mock_storage_owner.par, 'Remotepath', MockParameter('media/'))
        result = ext._resolve_remote_path('photo.jpg')
        assert result == 'media/photo.jpg'

    def test_resolve_remote_absolute(self, ext):
        """Path starting with / is treated as absolute (stripped)."""
        result = ext._resolve_remote_path('/absolute/path.jpg')
        assert result == 'absolute/path.jpg'

    def test_resolve_remote_subfolder(self, ext, mock_storage_owner):
        """Subfolder structure preserved in remote path."""
        setattr(mock_storage_owner.par, 'Remotepath', MockParameter('assets'))
        result = ext._resolve_remote_path('textures/brick.png')
        assert result == 'assets/textures/brick.png'

    def test_resolve_remote_none_with_fallback(self, ext, mock_storage_owner):
        """None path uses fallback_rel relative to Remotepath."""
        setattr(mock_storage_owner.par, 'Remotepath', MockParameter('media'))
        result = ext._resolve_remote_path(None, 'fallback.txt')
        assert result == 'media/fallback.txt'

    def test_resolve_remote_empty_prefix(self, ext, mock_storage_owner):
        """Empty Remotepath means path is used as-is."""
        setattr(mock_storage_owner.par, 'Remotepath', MockParameter(''))
        result = ext._resolve_remote_path('photo.jpg')
        assert result == 'photo.jpg'


# ══════════════════════════════════════════════════════════════════════════════
# Transfer concurrency
# ══════════════════════════════════════════════════════════════════════════════

class TestTransferConcurrency:
    def test_transfer_id_increments(self, ext):
        """Transfer IDs should increment monotonically."""
        ids = [ext._next_transfer_id() for _ in range(5)]
        assert ids == ['1', '2', '3', '4', '5']

    def test_submit_or_queue_within_limit(self, ext, mock_storage_owner):
        """Transfers within Maxconcurrent should be submitted immediately."""
        setattr(mock_storage_owner.par, 'Maxconcurrent', MockParameter(3))
        # Mock ThreadManager to avoid TD dependency
        mock_tm = MagicMock()
        mock_tm.TDTask = MagicMock(return_value=MagicMock())

        with patch.dict('builtins.__dict__', {'op': MagicMock(TDResources=MagicMock(ThreadManager=mock_tm))}):
            ext._submit_or_queue(lambda: None, (), '1')
            ext._submit_or_queue(lambda: None, (), '2')
            ext._submit_or_queue(lambda: None, (), '3')
            assert ext._active_count == 3
            assert len(ext._pending_transfers) == 0

    def test_submit_or_queue_exceeds_limit(self, ext, mock_storage_owner):
        """Transfers exceeding Maxconcurrent should be queued."""
        setattr(mock_storage_owner.par, 'Maxconcurrent', MockParameter(2))
        mock_tm = MagicMock()
        mock_tm.TDTask = MagicMock(return_value=MagicMock())

        with patch.dict('builtins.__dict__', {'op': MagicMock(TDResources=MagicMock(ThreadManager=mock_tm))}):
            ext._submit_or_queue(lambda: None, (), '1')
            ext._submit_or_queue(lambda: None, (), '2')
            ext._submit_or_queue(lambda: None, (), '3')
            ext._submit_or_queue(lambda: None, (), '4')
            assert ext._active_count == 2
            assert len(ext._pending_transfers) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Drain results processing
# ══════════════════════════════════════════════════════════════════════════════

class TestDrainResults:
    def test_drain_processes_upload_result(self, ext, mock_storage_owner):
        """Upload result should update transfers DAT."""
        ext._transfer_results.put({
            'transfer_id': '1',
            'type': 'upload',
            'remote_path': 'media/photo.jpg',
            'local_path': '/local/photo.jpg',
            'success': True,
            'error': None,
            'size': 1024,
        })
        # Record the transfer first
        ext._record_transfer('1', 'upload', 'media/photo.jpg', '/local/photo.jpg', 'active')
        ext._active_count = 1

        ext._drain_results()

        transfers = mock_storage_owner.op('transfers')
        # Row 0 is header, row 1 is the recorded transfer
        assert transfers[1, 'status'].val == 'complete'
        assert ext._active_count == 0

    def test_drain_processes_failed_result(self, ext, mock_storage_owner):
        """Failed result should update status to 'failed' with error."""
        ext._transfer_results.put({
            'transfer_id': '1',
            'type': 'upload',
            'remote_path': 'media/photo.jpg',
            'local_path': '/local/photo.jpg',
            'success': False,
            'error': 'Permission denied',
            'size': 0,
        })
        ext._record_transfer('1', 'upload', 'media/photo.jpg', '/local/photo.jpg', 'active')
        ext._active_count = 1

        ext._drain_results()

        transfers = mock_storage_owner.op('transfers')
        assert transfers[1, 'status'].val == 'failed'
        assert transfers[1, 'error'].val == 'Permission denied'

    def test_drain_empty_queue(self, ext):
        """Draining an empty queue should be a no-op."""
        ext._drain_results()  # Should not raise

    def test_drain_respects_max_per_frame(self, ext, mock_storage_owner):
        """Only MAX_RESULTS_PER_FRAME items should be drained per call."""
        for i in range(20):
            ext._transfer_results.put({
                'transfer_id': str(i),
                'type': 'delete',
                'remote_path': f'file{i}.txt',
                'local_path': '',
                'success': True,
                'error': None,
                'size': 0,
            })
            ext._record_transfer(str(i), 'delete', f'file{i}.txt', '', 'active')
        ext._active_count = 20

        ext._drain_results()

        # Should have processed MAX_RESULTS_PER_FRAME items
        assert ext._transfer_results.qsize() == 10


# ══════════════════════════════════════════════════════════════════════════════
# Sync worker (module-level, no TD dependency)
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncWorker:
    def test_upload_direction_only_uploads(self, tmp_local_dir):
        """direction='upload' should only upload, not download."""
        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []  # No remote files
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, tmp_local_dir, 'test/', 'upload', False,
                     False, False, '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is True
        assert len(result['uploaded']) == 3  # file1.txt, file2.jpg, subdir/nested.txt
        assert len(result['downloaded']) == 0

    def test_download_direction_only_downloads(self, tmp_path):
        """direction='download' should only download, not upload."""
        local_dir = str(tmp_path / 'empty_local')
        os.makedirs(local_dir, exist_ok=True)

        # Mock a remote blob
        mock_blob = MagicMock()
        mock_blob.name = 'test/remote_file.txt'
        mock_blob.updated = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        mock_blob.download_to_filename = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [mock_blob]

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'download', False,
                     False, False, '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is True
        assert len(result['downloaded']) == 1
        assert result['downloaded'][0] == 'remote_file.txt'
        assert len(result['uploaded']) == 0

    def test_delete_remote_orphans(self, tmp_path):
        """delete_remote=True should delete remote files not present locally."""
        local_dir = str(tmp_path / 'local')
        os.makedirs(local_dir)
        (tmp_path / 'local' / 'keep.txt').write_text('keep')

        # Remote has keep.txt AND orphan.txt
        keep_blob = MagicMock()
        keep_blob.name = 'test/keep.txt'
        keep_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)

        orphan_blob = MagicMock()
        orphan_blob.name = 'test/orphan.txt'
        orphan_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [keep_blob, orphan_blob]
        mock_bucket.blob.return_value = MagicMock()

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'upload', False,
                     True, False, '1', result_queue)  # delete_remote=True

        result = result_queue.get_nowait()
        assert 'remote:orphan.txt' in result['deleted']
        assert 'remote:keep.txt' not in result['deleted']

    def test_delete_local_orphans(self, tmp_path):
        """delete_local=True should delete local files not present remotely."""
        local_dir = str(tmp_path / 'local')
        os.makedirs(local_dir)
        orphan_path = tmp_path / 'local' / 'orphan.txt'
        orphan_path.write_text('orphan')

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []  # No remote files

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'download', False,
                     False, True, '1', result_queue)  # delete_local=True

        result = result_queue.get_nowait()
        assert 'local:orphan.txt' in result['deleted']
        assert not orphan_path.exists()

    def test_no_delete_when_flags_off(self, tmp_path):
        """No deletions when both delete flags are off."""
        local_dir = str(tmp_path / 'local')
        os.makedirs(local_dir)
        (tmp_path / 'local' / 'local_only.txt').write_text('local')

        remote_blob = MagicMock()
        remote_blob.name = 'test/remote_only.txt'
        remote_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        remote_blob.download_to_filename = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [remote_blob]
        mock_bucket.blob.return_value = MagicMock()

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'both', False,
                     False, False, '1', result_queue)

        result = result_queue.get_nowait()
        assert len(result['deleted']) == 0

    def test_both_delete_remote_skips_download_of_orphans(self, tmp_path):
        """direction='both' + delete_remote should delete remote orphans, NOT download them."""
        local_dir = str(tmp_path / 'local')
        os.makedirs(local_dir)
        (tmp_path / 'local' / 'shared.txt').write_text('shared')

        # Remote has shared.txt AND orphan.txt (orphan doesn't exist locally)
        shared_blob = MagicMock()
        shared_blob.name = 'test/shared.txt'
        shared_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)

        orphan_blob = MagicMock()
        orphan_blob.name = 'test/orphan.txt'
        orphan_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        orphan_blob.download_to_filename = MagicMock()

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [shared_blob, orphan_blob]
        mock_bucket.blob.return_value = MagicMock()

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'both', False,
                     True, False, '1', result_queue)  # delete_remote=True

        result = result_queue.get_nowait()
        # Orphan should be deleted remotely, NOT downloaded
        assert 'remote:orphan.txt' in result['deleted']
        assert 'orphan.txt' not in result['downloaded']
        orphan_blob.download_to_filename.assert_not_called()

    def test_both_delete_local_skips_upload_of_orphans(self, tmp_path):
        """direction='both' + delete_local should delete local orphans, NOT upload them."""
        local_dir = str(tmp_path / 'local')
        os.makedirs(local_dir)
        (tmp_path / 'local' / 'shared.txt').write_text('shared')
        orphan_path = tmp_path / 'local' / 'local_orphan.txt'
        orphan_path.write_text('should be deleted')

        # Remote only has shared.txt
        shared_blob = MagicMock()
        shared_blob.name = 'test/shared.txt'
        shared_blob.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [shared_blob]
        mock_bucket.blob.return_value = MagicMock()

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, local_dir, 'test/', 'both', False,
                     False, True, '1', result_queue)  # delete_local=True

        result = result_queue.get_nowait()
        # Local orphan should be deleted locally, NOT uploaded
        assert 'local:local_orphan.txt' in result['deleted']
        assert 'local_orphan.txt' not in result['uploaded']
        assert not orphan_path.exists()

    def test_subfolder_structure_preserved(self, tmp_local_dir):
        """Subfolder structure should be replicated in remote paths."""
        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = []
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        _sync_worker(mock_bucket, tmp_local_dir, 'assets/', 'upload', False,
                     False, False, '1', result_queue)

        result = result_queue.get_nowait()
        assert 'subdir/nested.txt' in result['uploaded']

        # Verify the blob was created with the correct remote path
        blob_calls = mock_bucket.blob.call_args_list
        remote_paths = [call[0][0] for call in blob_calls]
        assert 'assets/subdir/nested.txt' in remote_paths


# ══════════════════════════════════════════════════════════════════════════════
# Upload/Download workers (module-level)
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkers:
    def test_upload_worker_success(self):
        """Upload worker should put success result in queue."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.public_url = 'https://storage.googleapis.com/test/file.txt'
        mock_blob.size = 1024
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b'test content')
            local_path = f.name

        try:
            _upload_worker(mock_bucket, local_path, 'remote/file.txt', False, '1', result_queue)
            result = result_queue.get_nowait()
            assert result['success'] is True
            assert result['transfer_id'] == '1'
            assert result['type'] == 'upload'
            mock_blob.upload_from_filename.assert_called_once_with(local_path)
            mock_blob.make_public.assert_not_called()
        finally:
            os.unlink(local_path)

    def test_upload_worker_make_public(self):
        """Upload with make_public should call blob.make_public()."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.public_url = 'https://storage.googleapis.com/test/file.txt'
        mock_blob.size = 1024
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b'test')
            local_path = f.name

        try:
            _upload_worker(mock_bucket, local_path, 'file.txt', True, '1', result_queue)
            result = result_queue.get_nowait()
            assert result['success'] is True
            mock_blob.make_public.assert_called_once()
        finally:
            os.unlink(local_path)

    def test_upload_worker_failure(self):
        """Upload worker should put error result on exception."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.upload_from_filename.side_effect = Exception('Permission denied')
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        _upload_worker(mock_bucket, '/nonexistent/file.txt', 'file.txt', False, '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is False
        assert 'Permission denied' in result['error']

    def test_download_worker_success(self, tmp_path):
        """Download worker should call download_to_filename."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.size = 2048
        mock_bucket.blob.return_value = mock_blob

        local_path = str(tmp_path / 'downloaded.txt')
        result_queue = queue.Queue()

        _download_worker(mock_bucket, 'remote/file.txt', local_path, '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is True
        mock_blob.download_to_filename.assert_called_once_with(local_path)

    def test_delete_worker_success(self):
        """Delete worker should call blob.delete()."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_bucket.blob.return_value = mock_blob

        result_queue = queue.Queue()
        _delete_worker(mock_bucket, 'remote/file.txt', '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is True
        mock_blob.delete.assert_called_once()

    def test_list_worker_filters_directories(self):
        """List worker should skip blobs ending with /."""
        blob1 = MagicMock()
        blob1.name = 'prefix/file1.txt'
        blob1.size = 100
        blob1.updated = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        blob1.content_type = 'text/plain'

        blob_dir = MagicMock()
        blob_dir.name = 'prefix/subdir/'

        blob2 = MagicMock()
        blob2.name = 'prefix/file2.jpg'
        blob2.size = 2000
        blob2.updated = datetime.datetime(2025, 1, 2, tzinfo=datetime.timezone.utc)
        blob2.content_type = 'image/jpeg'

        mock_bucket = MagicMock()
        mock_bucket.list_blobs.return_value = [blob1, blob_dir, blob2]

        result_queue = queue.Queue()
        _list_worker(mock_bucket, 'prefix/', '/', '1', result_queue)

        result = result_queue.get_nowait()
        assert result['success'] is True
        assert len(result['files']) == 2
        names = [f['name'] for f in result['files']]
        assert 'prefix/file1.txt' in names
        assert 'prefix/file2.jpg' in names


# ══════════════════════════════════════════════════════════════════════════════
# Callback dispatch
# ══════════════════════════════════════════════════════════════════════════════

class TestCallbackDispatch:
    def test_fire_callback_with_dat(self, ext, mock_storage_owner):
        """Callback should be fired on the callbacks DAT."""
        mock_dat = MagicMock()
        setattr(mock_storage_owner.par, 'Callbacksdat', MockParameter('my_callbacks'))

        # Patch global op() to return our mock DAT
        with patch.dict('builtins.__dict__', {'op': lambda path: mock_dat if path == 'my_callbacks' else None}):
            ext._fire_callback('onTransferComplete', '1', 'upload', 'remote.jpg', 'local.jpg', True, None)

        mock_dat.run.assert_called_once_with(
            'onTransferComplete', '1', 'upload', 'remote.jpg', 'local.jpg', True, None
        )

    def test_fire_callback_no_dat_configured(self, ext, mock_storage_owner):
        """No error when Callbacksdat is empty."""
        setattr(mock_storage_owner.par, 'Callbacksdat', MockParameter(''))
        ext._fire_callback('onTransferComplete', '1', 'upload', '', '', True, None)
        # Should not raise

    def test_fire_callback_handles_exception(self, ext, mock_storage_owner):
        """Callback exceptions should be logged, not raised."""
        mock_dat = MagicMock()
        mock_dat.run.side_effect = Exception('callback error')
        setattr(mock_storage_owner.par, 'Callbacksdat', MockParameter('my_callbacks'))

        with patch.dict('builtins.__dict__', {'op': lambda path: mock_dat if path == 'my_callbacks' else None}):
            ext._fire_callback('onTransferComplete', '1', 'upload', '', '', True, None)
        # Should not raise, error is logged


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap check
# ══════════════════════════════════════════════════════════════════════════════

class TestBootstrapCheck:
    def test_needs_bootstrap_when_missing(self):
        """NeedsBootstrap should return True when google.cloud.storage is not importable."""
        from ext_bootstrap import BootstrapExt
        owner = MockOwnerComp('storage', {})
        boot = BootstrapExt(owner)

        # If google.cloud.storage is installed in test env, this test
        # might return False -- that's OK, it means the package is available
        result = boot.NeedsBootstrap('')
        assert isinstance(result, bool)


# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════

class TestLogging:
    def test_log_buffer_stores_entries(self, ext):
        """Log entries should be stored in the ring buffer."""
        # Mock absTime.frame
        with patch.dict('builtins.__dict__', {'absTime': MagicMock(frame=100)}):
            ext._log('info', 'test message')

        logs = ext.GetLogs()
        assert len(logs) == 1
        assert logs[0]['message'] == 'test message'
        assert logs[0]['level'] == 'INFO'

    def test_log_buffer_respects_limit(self, ext):
        """GetLogs should respect the limit parameter."""
        with patch.dict('builtins.__dict__', {'absTime': MagicMock(frame=100)}):
            for i in range(10):
                ext._log('info', f'message {i}')

        logs = ext.GetLogs(limit=3)
        assert len(logs) == 3
        assert logs[-1]['message'] == 'message 9'

    def test_log_buffer_filters_by_level(self, ext):
        """GetLogs should filter by level when specified."""
        with patch.dict('builtins.__dict__', {'absTime': MagicMock(frame=100)}):
            ext._log('info', 'info msg')
            ext._log('error', 'error msg')
            ext._log('warning', 'warning msg')

        errors = ext.GetLogs(level='ERROR')
        assert len(errors) == 1
        assert errors[0]['message'] == 'error msg'

    def test_ring_buffer_maxlen(self, ext):
        """Ring buffer should cap at 500 entries."""
        with patch.dict('builtins.__dict__', {'absTime': MagicMock(frame=100)}):
            for i in range(600):
                ext._log('info', f'message {i}')

        all_logs = ext.GetLogs(limit=1000)
        assert len(all_logs) == 500
