import json
import os
import queue
import threading
import datetime
import time
from collections import deque


# -- Module-level worker functions -- NO TD object access -----------------------

def _bootstrap_worker(ensure_venv_fn, get_error_fn, project_folder, python_version):
	"""Pool thread: run uv venv bootstrap. Raises on failure."""
	ok = ensure_venv_fn(project_folder, python_version=python_version)
	if not ok:
		raise RuntimeError(get_error_fn() or 'Unknown bootstrap error')


def _keepalive_worker(shutdown_event):
	"""Standalone thread: blocks until shutdown_event is set."""
	shutdown_event.wait()


def _reconnect_sleep(delay):
	"""Pool thread: sleep before reconnect attempt."""
	time.sleep(delay)


def _upload_worker(bucket, local_path, remote_path, make_public, transfer_id, result_queue):
	"""Pool thread: upload a file to Storage. Puts result dict in queue."""
	try:
		blob = bucket.blob(remote_path)
		blob.upload_from_filename(local_path)
		if make_public:
			blob.make_public()
		blob.reload()
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'upload',
			'remote_path': remote_path,
			'local_path': local_path,
			'success': True,
			'error': None,
			'public_url': blob.public_url if make_public else '',
			'size': blob.size or 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'upload',
			'remote_path': remote_path,
			'local_path': local_path,
			'success': False,
			'error': str(e),
			'size': 0,
		})


def _download_worker(bucket, remote_path, local_path, transfer_id, result_queue):
	"""Pool thread: download a file from Storage. Puts result dict in queue."""
	try:
		blob = bucket.blob(remote_path)
		os.makedirs(os.path.dirname(local_path), exist_ok=True)
		blob.download_to_filename(local_path)
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'download',
			'remote_path': remote_path,
			'local_path': local_path,
			'success': True,
			'error': None,
			'size': blob.size or 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'download',
			'remote_path': remote_path,
			'local_path': local_path,
			'success': False,
			'error': str(e),
			'size': 0,
		})


def _delete_worker(bucket, remote_path, transfer_id, result_queue):
	"""Pool thread: delete a remote blob. Puts result dict in queue."""
	try:
		blob = bucket.blob(remote_path)
		blob.delete()
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'delete',
			'remote_path': remote_path,
			'local_path': '',
			'success': True,
			'error': None,
			'size': 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'delete',
			'remote_path': remote_path,
			'local_path': '',
			'success': False,
			'error': str(e),
			'size': 0,
		})


def _list_worker(bucket, prefix, delimiter, transfer_id, result_queue):
	"""Pool thread: list blobs under prefix. Puts result dict in queue."""
	try:
		blobs = bucket.list_blobs(prefix=prefix, delimiter=delimiter)
		files = []
		for blob in blobs:
			if blob.name.endswith('/'):
				continue
			files.append({
				'name': blob.name,
				'size': blob.size or 0,
				'updated': blob.updated.isoformat() if blob.updated else '',
				'content_type': blob.content_type or '',
			})
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'list',
			'remote_path': prefix,
			'local_path': '',
			'success': True,
			'error': None,
			'files': files,
			'size': 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'list',
			'remote_path': prefix,
			'local_path': '',
			'success': False,
			'error': str(e),
			'files': [],
			'size': 0,
		})


def _metadata_worker(bucket, remote_path, transfer_id, result_queue):
	"""Pool thread: get blob metadata. Puts result dict in queue."""
	try:
		blob = bucket.blob(remote_path)
		blob.reload()
		metadata = {
			'name': blob.name,
			'size': blob.size or 0,
			'content_type': blob.content_type or '',
			'updated': blob.updated.isoformat() if blob.updated else '',
			'created': blob.time_created.isoformat() if blob.time_created else '',
			'md5_hash': blob.md5_hash or '',
			'public_url': blob.public_url,
			'metadata': dict(blob.metadata) if blob.metadata else {},
		}
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'metadata',
			'remote_path': remote_path,
			'local_path': '',
			'success': True,
			'error': None,
			'metadata': metadata,
			'size': blob.size or 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'metadata',
			'remote_path': remote_path,
			'local_path': '',
			'success': False,
			'error': str(e),
			'metadata': {},
			'size': 0,
		})


def _signed_url_worker(bucket, remote_path, expiration_minutes, transfer_id, result_queue):
	"""Pool thread: generate a signed URL. Puts result dict in queue."""
	import datetime as _dt
	try:
		blob = bucket.blob(remote_path)
		url = blob.generate_signed_url(
			expiration=_dt.timedelta(minutes=expiration_minutes),
		)
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'signed_url',
			'remote_path': remote_path,
			'local_path': '',
			'success': True,
			'error': None,
			'signed_url': url,
			'size': 0,
		})
	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'signed_url',
			'remote_path': remote_path,
			'local_path': '',
			'success': False,
			'error': str(e),
			'signed_url': '',
			'size': 0,
		})


def _sync_worker(bucket, local_root, remote_prefix, direction, make_public,
				 delete_remote, delete_local, transfer_id, result_queue):
	"""
	Pool thread: bidirectional sync between local_root and remote_prefix.
	Compares files by name and modified time. No TD access.
	"""
	uploaded = []
	downloaded = []
	deleted = []
	errors = []

	try:
		# Normalize paths
		local_root = os.path.normpath(local_root)
		if remote_prefix and not remote_prefix.endswith('/'):
			remote_prefix += '/'

		# Build remote file index: relative_path -> blob
		remote_files = {}
		try:
			blobs = list(bucket.list_blobs(prefix=remote_prefix))
			for blob in blobs:
				if blob.name.endswith('/'):
					continue
				rel = blob.name[len(remote_prefix):] if remote_prefix else blob.name
				if rel:
					remote_files[rel] = blob
		except Exception as e:
			errors.append({'path': remote_prefix, 'error': f'Failed to list remote: {e}'})

		# Build local file index: relative_path -> mtime
		local_files = {}
		if os.path.isdir(local_root):
			for dirpath, _dirnames, filenames in os.walk(local_root):
				for fname in filenames:
					full = os.path.join(dirpath, fname)
					rel = os.path.relpath(full, local_root).replace('\\', '/')
					local_files[rel] = os.path.getmtime(full)

		# Upload: local files missing or newer than remote
		if direction in ('upload', 'both'):
			for rel, local_mtime in local_files.items():
				remote_blob = remote_files.get(rel)
				need_upload = False
				if remote_blob is None:
					# In bidirectional mode, skip uploading local-only files
					# if they will be deleted by delete_local
					if delete_local and direction == 'both':
						continue
					need_upload = True
				elif remote_blob.updated:
					remote_mtime = remote_blob.updated.timestamp()
					if local_mtime > remote_mtime:
						need_upload = True

				if need_upload:
					local_path = os.path.join(local_root, rel)
					remote_path = (remote_prefix or '') + rel
					try:
						blob = bucket.blob(remote_path)
						blob.upload_from_filename(local_path)
						if make_public:
							blob.make_public()
						uploaded.append(rel)
					except Exception as e:
						errors.append({'path': rel, 'error': f'Upload failed: {e}'})

		# Download: remote files missing or newer than local
		if direction in ('download', 'both'):
			for rel, remote_blob in remote_files.items():
				local_path = os.path.join(local_root, rel)
				need_download = False
				if rel not in local_files:
					# In bidirectional mode, skip downloading remote-only files
					# if they will be deleted by delete_remote
					if delete_remote and direction == 'both':
						continue
					need_download = True
				elif remote_blob.updated:
					remote_mtime = remote_blob.updated.timestamp()
					if remote_mtime > local_files[rel]:
						need_download = True

				if need_download:
					try:
						os.makedirs(os.path.dirname(local_path), exist_ok=True)
						remote_blob.download_to_filename(local_path)
						downloaded.append(rel)
					except Exception as e:
						errors.append({'path': rel, 'error': f'Download failed: {e}'})

		# Delete remote orphans (files in remote not in local)
		if delete_remote and direction in ('upload', 'both'):
			for rel in remote_files:
				if rel not in local_files:
					remote_path = (remote_prefix or '') + rel
					try:
						bucket.blob(remote_path).delete()
						deleted.append(f'remote:{rel}')
					except Exception as e:
						errors.append({'path': f'remote:{rel}', 'error': f'Delete failed: {e}'})

		# Delete local orphans (files in local not in remote)
		if delete_local and direction in ('download', 'both'):
			for rel in local_files:
				if rel not in remote_files:
					local_path = os.path.join(local_root, rel)
					try:
						os.remove(local_path)
						deleted.append(f'local:{rel}')
					except Exception as e:
						errors.append({'path': f'local:{rel}', 'error': f'Delete failed: {e}'})

		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'sync',
			'remote_path': remote_prefix,
			'local_path': local_root,
			'success': len(errors) == 0,
			'error': f'{len(errors)} error(s)' if errors else None,
			'uploaded': uploaded,
			'downloaded': downloaded,
			'deleted': deleted,
			'errors': errors,
			'size': 0,
		})

	except Exception as e:
		result_queue.put({
			'transfer_id': transfer_id,
			'type': 'sync',
			'remote_path': remote_prefix,
			'local_path': local_root,
			'success': False,
			'error': str(e),
			'uploaded': uploaded,
			'downloaded': downloaded,
			'deleted': deleted,
			'errors': errors,
			'size': 0,
		})


_CALLBACKS_TEMPLATE = '''\
# Storage Callbacks
# Called by the storage COMP when events occur.
# Uncomment and modify the examples in each function.


def onTransferComplete(transfer_id, transfer_type, remote_path, local_path, success, error):
	"""
	Called when an upload, download, or delete completes.

	Args:
		transfer_id (str):    Unique transfer identifier.
		transfer_type (str):  'upload', 'download', or 'delete'.
		remote_path (str):    Remote blob path.
		local_path (str):     Local file path (empty for delete).
		success (bool):       True if the operation succeeded.
		error (str|None):     Error message on failure, None on success.
	"""
	# Example: log completed uploads
	# if transfer_type == 'upload' and success:
	# 	debug(f'Uploaded: {local_path} -> {remote_path}')
	return


def onListComplete(transfer_id, prefix, files, success, error):
	"""
	Called when a ListFiles operation completes.

	Args:
		transfer_id (str):  Unique transfer identifier.
		prefix (str):       The prefix that was listed.
		files (list[dict]): List of file metadata dicts with keys:
		                    name, size, updated, content_type.
		success (bool):     True if the operation succeeded.
		error (str|None):   Error message on failure, None on success.
	"""
	# Example: print file names
	# for f in files:
	# 	debug(f'{f["name"]} ({f["size"]} bytes)')
	return


def onSyncComplete(transfer_id, direction, uploaded, downloaded, deleted, errors, success, error):
	"""
	Called when a SyncFolder operation completes.

	Args:
		transfer_id (str):     Unique transfer identifier.
		direction (str):       'both', 'upload', or 'download'.
		uploaded (list[str]):  Relative paths of files uploaded.
		downloaded (list[str]): Relative paths of files downloaded.
		deleted (list[str]):   Paths removed (prefixed 'remote:' or 'local:').
		errors (list[dict]):   List of dicts with 'path' and 'error' keys.
		success (bool):        True if all operations succeeded.
		error (str|None):      Summary error message, or None.
	"""
	# Example: log sync summary
	# debug(f'Sync {direction}: {len(uploaded)} up, {len(downloaded)} down, {len(deleted)} deleted')
	return


def onConnectionStateChange(state, error):
	"""
	Called when the connection state changes.

	Args:
		state (str):      'connecting', 'connected', 'disconnected',
		                  'error', or 'reconnecting'.
		error (str|None): Error description, or None.
	"""
	# Example: update a status display
	# op('status_text').par.text = state
	return
'''


# -- Extension -----------------------------------------------------------------

class StorageExt:
	"""
	Firebase Cloud Storage engine for TouchDesigner.

	Threading model (no asyncio, no TDAsyncIO):
	  Main thread (TD cook):
	    - _drain_results()      RefreshHook -- drains transfer queue every frame
	    - Connect/Disconnect    manage Firebase lifecycle

	  ThreadManager pool tasks (workers pass results via queue.Queue,
	  processed by _drain_results on the main thread every frame):
	    - Bootstrap             uv venv setup --> SuccessHook/ExceptHook
	    - Upload/Download       per-file transfers --> _transfer_results queue
	    - Delete/List/Metadata  per-operation --> _transfer_results queue
	    - SyncFolder            walk + compare + transfer --> _transfer_results queue
	    - Reconnect             sleep(delay) --> SuccessHook

	  ThreadManager standalone task:
	    - Keepalive             blocks on shutdown_event; RefreshHook=_drain_results
	"""

	MAX_RESULTS_PER_FRAME = 10

	def __init__(self, ownerComp):
		self.my = ownerComp

		# Thread-safe bridge: workers -> main thread via RefreshHook
		self._transfer_results = queue.Queue()

		# Keepalive task lifecycle
		self._keepalive_shutdown = threading.Event()

		# Firebase state -- main thread only
		self._firebase_app = None
		self._bucket = None

		# Lazy-imported Firebase modules (populated after bootstrap)
		self._firebase_admin = None
		self._credentials_cls = None
		self._storage_mod = None

		self._initialized = False

		# Transfer concurrency control
		self._pending_transfers = deque()   # queued transfers waiting for a slot
		self._active_count = 0
		self._transfer_counter = 0

		# Structured log ring buffer (all levels, for programmatic access)
		self._log_buffer = deque(maxlen=500)
		self._log_counter = 0

		self._prompt_run = None   # delayed run for bootstrap prompt

	# -- Lifecycle -------------------------------------------------------------

	def onInitTD(self):
		"""Called automatically by TD when extensions initialize."""
		self.Init()

	def Init(self):
		"""Called from onInitTD. Main thread."""
		if self._initialized:
			return
		self._initialized = True
		self._log('info', 'StorageExt initializing')

		# Fast path: deps already available -- skip prompt and bootstrap
		if not self.my.ext.BootstrapExt.NeedsBootstrap(project.folder):
			self._log('info', 'Dependencies already installed -- skipping bootstrap')
			self._import_firebase()
			self._start_keepalive()
			if self.my.par.Autoconnect.eval():
				self.Connect()
			return

		# Deps not available -- prompt user before installing
		self._update_status('connecting', error='Waiting for dependency install...')
		self._prompt_run = run(f"op('{self.my.path}').ext.StorageExt._prompt_bootstrap()", delayFrames=5)

	def _prompt_bootstrap(self):
		"""Show messageBox asking user to approve dependency install. Main thread."""
		if self.my.ext.StorageExt is not self:
			return

		choice = ui.messageBox(
			'Storage -- Install Dependencies',
			'This component requires Python packages:\n'
			'  - firebase-admin\n'
			'  - google-cloud-storage\n\n'
			'A virtual environment will be created using "uv"\n'
			'(a fast Python package manager). If uv is not\n'
			'installed, it will be downloaded automatically.\n\n'
			'This only happens once.',
			buttons=['Install', 'Cancel'],
		)

		if choice == 1:  # Cancel
			self._log('warning', 'User cancelled dependency installation')
			self._update_status('error', error='Dependency install cancelled')
			return

		self._log('info', 'User approved dependency installation')
		self._run_bootstrap()

	def _run_bootstrap(self):
		"""Kick off the background bootstrap worker. Main thread."""
		import sys as _sys
		project_folder = project.folder
		python_version = f'{_sys.version_info.major}.{_sys.version_info.minor}'

		ensure_venv_fn = self.my.ext.BootstrapExt.ensure_venv
		get_error_fn = self.my.ext.BootstrapExt.GetError

		self._start_keepalive()
		self._update_status('connecting', error='Installing dependencies...')

		tm = op.TDResources.ThreadManager
		bootstrap_task = tm.TDTask(
			target=_bootstrap_worker,
			args=(ensure_venv_fn, get_error_fn, project_folder, python_version),
			SuccessHook=self._on_bootstrap_success,
			ExceptHook=self._on_bootstrap_error,
		)
		tm.EnqueueTask(bootstrap_task)

	def _start_keepalive(self):
		"""Start the keepalive standalone task. Main thread."""
		self._keepalive_shutdown.clear()
		tm = op.TDResources.ThreadManager
		keepalive_task = tm.TDTask(
			target=_keepalive_worker,
			args=(self._keepalive_shutdown,),
			RefreshHook=self._drain_results,
		)
		tm.EnqueueTask(keepalive_task, standalone=True)

	def onDestroyTD(self):
		"""Called by TD when extension is torn down."""
		if self._prompt_run is not None:
			try:
				self._prompt_run.kill()
			except Exception:
				pass
			self._prompt_run = None
		self._keepalive_shutdown.set()
		self._cleanup_firebase()
		self._initialized = False

	# -- Public: Connection ----------------------------------------------------

	def Connect(self):
		"""Connect to Firebase Storage. Main thread (blocks during Firebase init)."""
		if self.my.ext.StorageExt is not self:
			return
		conn = self.my.ext.ConnectionExt
		if not conn.CanAttempt():
			self._log('warning', f'Circuit {conn.GetState()} -- skipping connect')
			return

		if not self.my.par.Privatekey.eval():
			self._log('warning', 'Privatekey parameter is not set -- skipping connect')
			self._update_status('disconnected')
			return

		self._cleanup_firebase()
		self._update_status('connecting')

		try:
			self._connect_firebase()
		except Exception as e:
			conn.RecordFailure()
			delay = conn.GetBackoffSeconds()
			self._log('error', f'Connect failed: {e} -- retry in {delay:.0f}s')
			self._update_status('error', error=str(e))
			self._schedule_reconnect(delay)
			return

		conn.RecordSuccess()
		bucket_name = self._bucket.name if self._bucket else ''
		self._update_status('connected', bucket=bucket_name)
		self._log('info', f'Connected to bucket: {bucket_name}')

	def Disconnect(self):
		"""Clean disconnect. Main thread."""
		self._cleanup_firebase()
		self._update_status('disconnected')
		self._log('info', 'Disconnected')

	def Reset(self):
		"""Full teardown to disconnected state. Main thread."""
		choice = ui.messageBox(
			'Storage -- Reset',
			'This will:\n'
			'  - Disconnect from Firebase Storage\n'
			'  - Cancel all pending transfers\n'
			'  - Clear the transfers table\n'
			'  - Reset circuit breaker\n\n'
			'Use Connect to re-establish when ready.',
			buttons=['Reset', 'Cancel'],
		)
		if choice == 1:  # Cancel
			self._log('info', 'Reset cancelled by user')
			return

		self._keepalive_shutdown.set()
		self._cleanup_firebase()
		self.my.ext.ConnectionExt.Reset()
		self._pending_transfers.clear()
		self._active_count = 0
		self._clear_transfers_dat()
		self._initialized = False
		self._update_status('disconnected')
		self._log('info', 'Reset complete -- use Connect to re-establish')

	# -- Public: Transfers -----------------------------------------------------

	def Upload(self, local_path=None, remote_path=None, make_public=None):
		"""
		Upload a file to Firebase Storage.

		Args:
			local_path:   File path (relative to Localpath par, or absolute).
			remote_path:  Remote blob path (relative to Remotepath par, or absolute).
			              If None, mirrors the local relative path.
			make_public:  Override Makepublic par for this upload. None = use par.

		Returns:
			transfer_id (str) for tracking in the transfers tableDAT.
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot upload')
			return ''

		resolved_local = self._resolve_local_path(local_path)
		if not resolved_local or not os.path.isfile(resolved_local):
			self._log('error', f'Local file not found: {resolved_local}')
			return ''

		resolved_remote = self._resolve_remote_path(remote_path, local_path)

		if make_public is None:
			make_public = bool(self.my.par.Makepublic.eval())

		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'upload', resolved_remote, resolved_local, 'pending')

		self._submit_or_queue(
			_upload_worker,
			(self._bucket, resolved_local, resolved_remote, make_public,
			 transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def Download(self, remote_path=None, local_path=None):
		"""
		Download a file from Firebase Storage.

		Args:
			remote_path:  Remote blob path (relative to Remotepath par, or absolute).
			local_path:   Local destination (relative to Localpath par, or absolute).
			              If None, mirrors the remote relative path.

		Returns:
			transfer_id (str).
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot download')
			return ''

		resolved_remote = self._resolve_remote_path(remote_path)
		resolved_local = self._resolve_local_path(local_path, remote_path)

		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'download', resolved_remote, resolved_local, 'pending')

		self._submit_or_queue(
			_download_worker,
			(self._bucket, resolved_remote, resolved_local,
			 transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def Delete(self, remote_path):
		"""
		Delete a remote blob.

		Args:
			remote_path: Remote blob path (relative to Remotepath par, or absolute).

		Returns:
			transfer_id (str).
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot delete')
			return ''

		resolved_remote = self._resolve_remote_path(remote_path)
		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'delete', resolved_remote, '', 'pending')

		self._submit_or_queue(
			_delete_worker,
			(self._bucket, resolved_remote, transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def SyncFolder(self, direction='both', delete_remote=None, delete_local=None):
		"""
		Sync between Localpath and Remotepath.

		Args:
			direction:     'both', 'upload', or 'download'.
			delete_remote: Delete remote blobs not present locally (None = use par).
			delete_local:  Delete local files not present remotely (None = use par).

		Fires onSyncComplete callback when done.
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot sync')
			return ''

		local_root = self.my.par.Localpath.eval()
		remote_prefix = self.my.par.Remotepath.eval()

		if not local_root:
			self._log('error', 'Local Path parameter is not set')
			return ''

		if delete_remote is None:
			delete_remote = bool(self.my.par.Deleteremoteorphans.eval())
		if delete_local is None:
			delete_local = bool(self.my.par.Deletelocalorphans.eval())

		make_public = bool(self.my.par.Makepublic.eval())

		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'sync', remote_prefix, local_root, 'pending')
		self._log('info', f'SyncFolder started: direction={direction}, local={local_root}, remote={remote_prefix}')

		# Sync is a single long-running worker -- bypass concurrency limit
		bucket = self._bucket
		tm = op.TDResources.ThreadManager
		task = tm.TDTask(
			target=_sync_worker,
			args=(bucket, local_root, remote_prefix, direction, make_public,
				  delete_remote, delete_local, transfer_id, self._transfer_results),
		)
		tm.EnqueueTask(task)
		return transfer_id

	def ListFiles(self, prefix='', delimiter='/'):
		"""
		List blobs under a prefix.

		Args:
			prefix:    Remote path prefix (relative to Remotepath par).
			delimiter: Delimiter for directory-like listing. Use '' for recursive.

		Returns:
			transfer_id (str). Results delivered via onListComplete callback.
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot list')
			return ''

		remote_prefix = self.my.par.Remotepath.eval()
		full_prefix = (remote_prefix.rstrip('/') + '/' + prefix.lstrip('/')) if remote_prefix else prefix
		if full_prefix and not full_prefix.endswith('/') and delimiter:
			full_prefix += '/'

		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'list', full_prefix, '', 'pending')

		self._submit_or_queue(
			_list_worker,
			(self._bucket, full_prefix, delimiter, transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def GetMetadata(self, remote_path):
		"""
		Get metadata for a remote blob.

		Args:
			remote_path: Remote blob path (relative to Remotepath par).

		Returns:
			transfer_id (str). Results delivered via onTransferComplete callback.
		"""
		if not self._bucket:
			self._log('error', 'Not connected -- cannot get metadata')
			return ''

		resolved_remote = self._resolve_remote_path(remote_path)
		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'metadata', resolved_remote, '', 'pending')

		self._submit_or_queue(
			_metadata_worker,
			(self._bucket, resolved_remote, transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def GetPublicUrl(self, remote_path):
		"""
		Get the public URL for a blob (synchronous, no network call).

		Args:
			remote_path: Remote blob path (relative to Remotepath par).

		Returns:
			str: The public URL.
		"""
		if not self._bucket:
			self._log('error', 'Not connected')
			return ''
		resolved = self._resolve_remote_path(remote_path)
		blob = self._bucket.blob(resolved)
		return blob.public_url

	def GetSignedUrl(self, remote_path, expiration_minutes=60):
		"""
		Generate a temporary signed URL for a blob.

		Args:
			remote_path:        Remote blob path (relative to Remotepath par).
			expiration_minutes: Minutes until the URL expires (default 60).

		Returns:
			transfer_id (str). The signed URL is delivered via onTransferComplete
			callback with transfer_type='signed_url'.
		"""
		if not self._bucket:
			self._log('error', 'Not connected')
			return ''

		resolved = self._resolve_remote_path(remote_path)
		transfer_id = self._next_transfer_id()
		self._record_transfer(transfer_id, 'signed_url', resolved, '', 'pending')

		self._submit_or_queue(
			_signed_url_worker,
			(self._bucket, resolved, expiration_minutes,
			 transfer_id, self._transfer_results),
			transfer_id,
		)
		return transfer_id

	def CancelTransfers(self):
		"""Cancel all pending (not yet started) transfers. Main thread."""
		cancelled = 0
		while self._pending_transfers:
			_target, _args, tid = self._pending_transfers.popleft()
			self._update_transfer(tid, 'cancelled')
			cancelled += 1
		if cancelled:
			self._log('info', f'Cancelled {cancelled} pending transfer(s)')

	# -- Public: Callbacks -----------------------------------------------------

	def CreateCallbacksDat(self):
		"""Create a sibling textDAT with pre-populated callback stubs."""
		parent_comp = self.my.parent()
		dat_name = 'storage_callbacks'
		if parent_comp.op(dat_name):
			self._log('warning', f'{dat_name} already exists -- skipping creation')
			return
		new_dat = parent_comp.create(textDAT, dat_name)
		new_dat.par.language = 'python'
		new_dat.text = _CALLBACKS_TEMPLATE
		self.my.par.Callbacksdat = dat_name
		self._log('info', f'Created callbacks DAT: {parent_comp.path}/{dat_name}')

	# -- Public: Diagnostics ---------------------------------------------------

	def GetStatus(self):
		"""Return current status as a dict."""
		dat = self.my.op('status')
		if not dat or dat.numRows < 2:
			return {}
		return {dat[0, col].val: dat[1, col].val for col in range(dat.numCols)}

	def GetLogs(self, level=None, limit=50):
		"""
		Return recent log entries from the ring buffer.

		Args:
			level: Optional filter -- 'INFO', 'WARNING', 'ERROR', 'DEBUG', or None for all.
			limit: Max entries to return (default 50).
		"""
		entries = list(self._log_buffer)
		if level:
			entries = [e for e in entries if e['level'] == level.upper()]
		return entries[-limit:]

	# -- Internal: ThreadManager hooks (main thread) ---------------------------

	def _on_bootstrap_success(self, returnValue=None):
		"""Main thread. SuccessHook called by ThreadManager after bootstrap."""
		self._log('info', 'Bootstrap complete -- importing Firebase')
		try:
			self._import_firebase()
		except Exception as e:
			self._log('error', f'Firebase import failed: {e}')
			self._update_status('error', error=str(e))
			return
		if self.my.par.Autoconnect.eval():
			self.Connect()

	def _on_bootstrap_error(self, error):
		"""Main thread. ExceptHook called by ThreadManager on bootstrap exception."""
		self._log('error', f'Bootstrap failed: {error}')
		self._update_status('error', error=str(error))

	def _drain_results(self):
		"""
		Main thread. RefreshHook -- called every frame by ThreadManager.
		Drains transfer results populated by worker threads.
		"""
		count = 0
		while count < self.MAX_RESULTS_PER_FRAME:
			try:
				result = self._transfer_results.get_nowait()
				self._process_transfer_result(result)
				count += 1
			except queue.Empty:
				break

	def _on_reconnect_done(self, returnValue=None):
		"""Main thread. SuccessHook called after reconnect sleep completes."""
		if self.my.ext.StorageExt is not self:
			return
		self._log('info', 'Attempting reconnect after backoff')
		self.Connect()

	def _on_reconnect_error(self, error):
		"""Main thread. Should not normally fire."""
		self._log('warning', f'Reconnect sleep interrupted: {error}')

	# -- Internal: Firebase setup ----------------------------------------------

	def _import_firebase(self):
		"""Import firebase_admin after bootstrap. Main thread."""
		import firebase_admin
		from firebase_admin import credentials, storage
		self._firebase_admin = firebase_admin
		self._credentials_cls = credentials
		self._storage_mod = storage

	def _connect_firebase(self):
		"""Initialize Firebase app and Storage bucket. Main thread (blocks)."""
		key_path = self.my.par.Privatekey.eval()
		bucket_name = self.my.par.Bucket.eval().strip()
		app_name = f'td_{self.my.name}'

		cred = self._credentials_cls.Certificate(key_path)
		with open(key_path, 'r') as f:
			project_id = json.loads(f.read()).get('project_id', '')
		if not project_id:
			raise ValueError('Service account JSON is missing project_id')

		# Auto-derive default bucket if not specified
		if not bucket_name:
			bucket_name = f'{project_id}.firebasestorage.app'

		self._firebase_app = self._firebase_admin.initialize_app(
			cred, {'storageBucket': bucket_name}, name=app_name,
		)
		self._bucket = self._storage_mod.bucket(app=self._firebase_app)

	def _cleanup_firebase(self):
		"""Safely delete Firebase app instance. Main thread."""
		if self._firebase_app and self._firebase_admin:
			try:
				self._firebase_admin.delete_app(self._firebase_app)
			except Exception:
				pass
		self._firebase_app = None
		self._bucket = None

	def _schedule_reconnect(self, delay):
		"""Submit a reconnect sleep task to the ThreadManager pool. Main thread."""
		tm = op.TDResources.ThreadManager
		task = tm.TDTask(
			target=_reconnect_sleep,
			args=(delay,),
			SuccessHook=self._on_reconnect_done,
			ExceptHook=self._on_reconnect_error,
		)
		tm.EnqueueTask(task)

	# -- Internal: Path resolution ---------------------------------------------

	def _resolve_local_path(self, path=None, fallback_rel=None):
		"""
		Resolve a local file path.
		- If path is absolute, return as-is.
		- If path is relative, join with Localpath par.
		- If path is None and fallback_rel given, use fallback_rel relative to Localpath.
		"""
		local_root = self.my.par.Localpath.eval()

		if path is None and fallback_rel is not None:
			path = fallback_rel

		if path is None:
			return local_root

		if os.path.isabs(path):
			return os.path.normpath(path)

		if local_root:
			return os.path.normpath(os.path.join(local_root, path))

		return os.path.normpath(path)

	def _resolve_remote_path(self, path=None, fallback_rel=None):
		"""
		Resolve a remote blob path.
		- If path starts with /, treat as absolute (strip leading /).
		- If path is relative, prepend Remotepath par.
		- If path is None and fallback_rel given, use fallback_rel relative to Remotepath.
		"""
		remote_prefix = self.my.par.Remotepath.eval()

		if path is None and fallback_rel is not None:
			path = fallback_rel

		if path is None:
			return remote_prefix

		# Absolute remote path (starts with /)
		if path.startswith('/'):
			return path.lstrip('/')

		if remote_prefix:
			prefix = remote_prefix.rstrip('/')
			return f'{prefix}/{path}'

		return path

	# -- Internal: Transfer concurrency ----------------------------------------

	def _next_transfer_id(self):
		self._transfer_counter += 1
		return str(self._transfer_counter)

	def _submit_or_queue(self, target, args, transfer_id):
		"""Submit a transfer to ThreadManager, or queue if at max concurrency."""
		max_concurrent = int(self.my.par.Maxconcurrent.eval())
		if self._active_count < max_concurrent:
			self._active_count += 1
			self._update_transfer(transfer_id, 'active')
			tm = op.TDResources.ThreadManager
			task = tm.TDTask(target=target, args=args)
			tm.EnqueueTask(task)
		else:
			self._pending_transfers.append((target, args, transfer_id))

	def _submit_next_pending(self):
		"""Submit the next queued transfer if there's capacity. Main thread."""
		max_concurrent = int(self.my.par.Maxconcurrent.eval())
		while self._pending_transfers and self._active_count < max_concurrent:
			target, args, tid = self._pending_transfers.popleft()
			self._active_count += 1
			self._update_transfer(tid, 'active')
			tm = op.TDResources.ThreadManager
			task = tm.TDTask(target=target, args=args)
			tm.EnqueueTask(task)

	# -- Internal: Transfer result processing ----------------------------------

	def _process_transfer_result(self, result):
		"""Process a transfer result dict. Main thread only."""
		transfer_id = result.get('transfer_id', '')
		transfer_type = result.get('type', '')
		success = result.get('success', False)
		error = result.get('error')
		remote_path = result.get('remote_path', '')
		local_path = result.get('local_path', '')
		size = result.get('size', 0)

		# Update transfers DAT
		status = 'complete' if success else 'failed'
		self._update_transfer(transfer_id, status, error=error or '', size=size)

		# Decrement active count and submit next pending
		if transfer_type != 'sync':
			self._active_count = max(0, self._active_count - 1)
			self._submit_next_pending()

		# Update status DAT active transfer count
		self._update_active_count()

		# Fire appropriate callback
		if transfer_type == 'sync':
			self._fire_callback(
				'onSyncComplete',
				transfer_id,
				result.get('direction', 'both'),
				result.get('uploaded', []),
				result.get('downloaded', []),
				result.get('deleted', []),
				result.get('errors', []),
				success,
				error,
			)
			uploaded = result.get('uploaded', [])
			downloaded = result.get('downloaded', [])
			deleted = result.get('deleted', [])
			self._log('info',
				f'Sync complete: {len(uploaded)} uploaded, {len(downloaded)} downloaded, '
				f'{len(deleted)} deleted, {len(result.get("errors", []))} error(s)')
		elif transfer_type == 'list':
			files = result.get('files', [])
			self._fire_callback('onListComplete', transfer_id, remote_path, files, success, error)
			if success:
				self._log('info', f'Listed {len(files)} file(s) under {remote_path}')
		elif transfer_type == 'metadata':
			metadata = result.get('metadata', {})
			self._fire_callback('onTransferComplete', transfer_id, 'metadata', remote_path, '', success, error)
			if success:
				self._log('info', f'Metadata: {remote_path} ({metadata.get("size", 0)} bytes)')
		elif transfer_type == 'signed_url':
			signed_url = result.get('signed_url', '')
			self._fire_callback('onTransferComplete', transfer_id, 'signed_url', remote_path, signed_url, success, error)
			if success:
				self._log('info', f'Signed URL generated for {remote_path}')
		else:
			self._fire_callback('onTransferComplete', transfer_id, transfer_type, remote_path, local_path, success, error)
			if success:
				self._log('info', f'{transfer_type.capitalize()} complete: {remote_path}')
			else:
				self._log('error', f'{transfer_type.capitalize()} failed [{remote_path}]: {error}')
				self.my.ext.ConnectionExt.RecordFailure()

	# -- Internal: Transfers DAT -----------------------------------------------

	def _record_transfer(self, transfer_id, transfer_type, remote_path, local_path, status):
		"""Add a row to the transfers tableDAT. Main thread."""
		dat = self.my.op('transfers')
		if not dat:
			return
		now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		dat.appendRow([transfer_id, transfer_type, remote_path, local_path, status, now, '', '', ''])

	def _update_transfer(self, transfer_id, status, error='', size=''):
		"""Update an existing row in the transfers tableDAT. Main thread."""
		dat = self.my.op('transfers')
		if not dat:
			return
		cell = dat.findCell(str(transfer_id), cols=[0])
		if cell is None:
			return
		row = int(cell.row)
		dat[row, 'status'] = status
		if status in ('complete', 'failed', 'cancelled'):
			dat[row, 'completed_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		if error:
			dat[row, 'error'] = error
		if size:
			dat[row, 'size'] = str(size)

	def _clear_transfers_dat(self):
		"""Clear all rows except header in transfers tableDAT. Main thread."""
		dat = self.my.op('transfers')
		if dat:
			dat.clear(keepFirstRow=True)

	def _update_active_count(self):
		"""Update the active_transfers field in status DAT. Main thread."""
		dat = self.my.op('status')
		if dat and dat.numRows >= 2:
			dat[1, 'active_transfers'] = str(self._active_count)

	# -- Internal: Callbacks ---------------------------------------------------

	def _fire_callback(self, func_name, *args):
		"""Invoke a function in the user's Callbacksdat DAT. Main thread."""
		cb_path = self.my.par.Callbacksdat.eval()
		if not cb_path:
			return
		cb_dat = op(cb_path)
		if not cb_dat:
			return
		try:
			cb_dat.run(func_name, *args)
		except Exception as e:
			self._log('warning', f'Callback {func_name} error: {e}')

	# -- Internal: Status + logging --------------------------------------------

	def _update_status(self, state, error='', bucket=''):
		"""Update status tableDAT. Main thread."""
		dat = self.my.op('status')
		if not dat or dat.numRows < 2:
			return
		dat[1, 'state'] = state
		dat[1, 'circuit'] = self.my.ext.ConnectionExt.GetState()
		dat[1, 'last_error'] = str(error)
		dat[1, 'active_transfers'] = str(self._active_count)
		if bucket:
			dat[1, 'bucket'] = bucket
		if state == 'connected':
			dat[1, 'connected_at'] = datetime.datetime.utcnow().isoformat()
		self._fire_callback('onConnectionStateChange', state, error or None)

	def _log(self, level, msg):
		"""
		Structured logging with ring buffer, FIFO DAT, and optional external log.
		Uses print() for clean textport output (no debug() DAT/line noise).
		"""
		ts = datetime.datetime.now().strftime('%H:%M:%S')
		current_frame = absTime.frame
		level_str = level.upper()

		# Always store in ring buffer (all levels, for programmatic access)
		self._log_counter += 1
		self._log_buffer.append({
			'id': self._log_counter,
			'timestamp': datetime.datetime.now().isoformat(),
			'frame': current_frame,
			'level': level_str,
			'message': msg,
		})

		# Skip DEBUG output to textport/FIFO unless Verbose is enabled
		if level_str == 'DEBUG':
			verbose_par = getattr(self.my.par, 'Verbose', None)
			if not verbose_par or not verbose_par.eval():
				return

		entry = f'{ts} {current_frame:>7} {level_str:<7} Storage: {msg}'

		print(entry)

		log_dat = self.my.op('log')
		if log_dat:
			log_dat.appendRow([entry])
		ext_path = self.my.par.Logop.eval()
		if ext_path:
			ext_log = op(ext_path)
			if ext_log:
				ext_log.appendRow([entry])
