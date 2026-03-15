import json
import queue
import threading
import datetime
import time
from collections import deque


# ── Module-level worker functions -- NO TD object access ──────────────────────

def _bootstrap_worker(ensure_venv_fn, get_error_fn, project_folder, python_version):
	"""Pool thread: run uv venv bootstrap. Raises on failure."""
	ok = ensure_venv_fn(project_folder, python_version=python_version)
	if not ok:
		raise RuntimeError(get_error_fn() or 'Unknown bootstrap error')

def _keepalive_worker(shutdown_event):
	"""Standalone thread: blocks until shutdown_event is set."""
	shutdown_event.wait()

def _execute_write(db, item):
	"""
	Pool thread: execute one Firestore write.
	Returns a result dict -- never raises.
	"""
	collection = item['collection']
	doc_id = item['doc_id']
	op_type = item['op_type']
	payload = item.get('payload', {})
	queue_id = item.get('queue_id', '')

	# Reconstruct Firestore SDK types from type markers before writing
	if payload and db is not None:
		payload = _deserialize_value(payload, db)

	if db is None:
		return {
			'queue_id': queue_id, 'doc_id': doc_id, 'collection': collection,
			'success': False, 'update_time': '', 'error': 'Not connected',
		}

	try:
		doc_ref = db.collection(collection).document(doc_id)
		if op_type == 'set':
			result = doc_ref.set(payload)
		elif op_type == 'set_merge':
			result = doc_ref.set(payload, merge=True)
		elif op_type == 'update':
			result = doc_ref.update(payload)
		elif op_type == 'delete':
			doc_ref.delete()
			return {
				'queue_id': queue_id, 'doc_id': doc_id, 'collection': collection,
				'success': True, 'update_time': '', 'error': None,
			}
		else:
			raise ValueError(f'Unknown op_type: {op_type}')

		update_time = ''
		if hasattr(result, 'update_time') and result.update_time:
			update_time = result.update_time.isoformat()
		return {
			'queue_id': queue_id, 'doc_id': doc_id, 'collection': collection,
			'success': True, 'update_time': update_time, 'error': None,
		}

	except Exception as e:
		return {
			'queue_id': queue_id, 'doc_id': doc_id, 'collection': collection,
			'success': False, 'update_time': '', 'error': str(e),
		}

def _discover_collections_worker(db, result_queue):
	"""Pool thread: list Firestore collection IDs, put result in queue. No TD access."""
	try:
		colls = [c.id for c in db.collections()]
		result_queue.put(('ok', colls))
	except Exception as e:
		result_queue.put(('error', str(e)))

def _execute_write_queued(db, item, result_queue):
	"""Pool thread: execute one Firestore write, put result in queue. No TD access."""
	result_queue.put(_execute_write(db, item))

def _reconnect_sleep(delay):
	"""Pool thread: sleep before reconnect attempt."""
	import time
	time.sleep(delay)

def _serialize_value(v):
	"""Recursively serialize a Firestore value to a JSON-safe Python type.

	Special Firestore types are wrapped with ``{"__type": ...}`` markers
	so they can be losslessly round-tripped back to SDK objects by
	``_deserialize_value``.
	"""
	try:
		if v is None or isinstance(v, (bool, int, float, str)):
			return v
		if isinstance(v, bytes):
			import base64
			return {'__type': 'bytes', 'value': base64.b64encode(v).decode('ascii')}
		if hasattr(v, 'latitude') and hasattr(v, 'longitude'):  # GeoPoint
			return {'__type': 'geopoint', 'latitude': v.latitude, 'longitude': v.longitude}
		if hasattr(v, 'isoformat'):   # datetime, DatetimeWithNanoseconds
			return {'__type': 'timestamp', 'value': v.isoformat()}
		if hasattr(v, 'path') and hasattr(v, 'id'):  # DocumentReference
			return {'__type': 'reference', 'value': v.path}
		if isinstance(v, dict):
			return {k: _serialize_value(val) for k, val in v.items()}
		if isinstance(v, list):
			return [_serialize_value(item) for item in v]
		return str(v)
	except Exception:
		return str(v)


def _deserialize_value(v, db):
	"""Recursively deserialize type-marked JSON back to Firestore SDK types.

	Handles both new (marked) and old (plain) formats gracefully.
	``db`` is the Firestore client instance, needed to construct
	DocumentReference objects.
	"""
	if v is None or isinstance(v, (bool, int, float, str)):
		return v
	if isinstance(v, dict):
		t = v.get('__type')
		if t == 'timestamp':
			import datetime as _dt
			raw = v['value']
			if raw.endswith('Z'):
				raw = raw[:-1] + '+00:00'
			# Truncate fractional seconds beyond microseconds (6 digits)
			import re
			raw = re.sub(r'(\.\d{6})\d+', r'\1', raw)
			return _dt.datetime.fromisoformat(raw)
		if t == 'reference':
			return db.document(v['value'])
		if t == 'geopoint':
			from google.cloud.firestore_v1._helpers import GeoPoint
			return GeoPoint(v['latitude'], v['longitude'])
		if t == 'bytes':
			import base64
			return base64.b64decode(v['value'])
		# Regular dict (no marker) - recurse
		return {k: _deserialize_value(val, db) for k, val in v.items()}
	if isinstance(v, list):
		return [_deserialize_value(item, db) for item in v]
	return v


_CELL_WATCH_SCRIPT = '''# Auto-generated by FirestoreExt -- do not edit
def onTableChange(dat, prevDAT, info):
	parent.firestore.ext.FirestoreExt._on_cell_edit(dat, info)
'''

_CALLBACKS_TEMPLATE = '''\
# Firestore Callbacks
# Called by the firestore COMP when events occur.
# Uncomment and modify the examples in each function.


def onFirestoreChange(collection, doc_id, change_type, payload):
	"""
	Called when a remote document is added, modified, or removed.

	Args:
		collection (str):  Collection name (e.g. 'users').
		doc_id (str):      Document ID.
		change_type (str): 'added', 'modified', or 'removed'.
		payload (dict):    Document data (empty dict on removal).
	"""
	# Example: route changes to a table
	# table = op('data_table')
	# if change_type == 'removed':
	# 	row = table.row(doc_id, caseSensitive=True)
	# 	if row is not None:
	# 		table.deleteRow(row)
	# else:
	# 	table.replaceRow(doc_id, [doc_id] + list(payload.values()))
	return


def onWriteComplete(collection, doc_id, success, error):
	"""
	Called after a write operation completes.

	Args:
		collection (str): Collection name.
		doc_id (str):     Document ID.
		success (bool):   True if the write succeeded.
		error (str|None): Error message on failure, None on success.
	"""
	# Example: log failures
	# if not success:
	# 	debug(f'Write failed: {collection}/{doc_id} -- {error}')
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
	# if error:
	# 	debug(f'Connection error: {error}')
	return
'''


# ── Extension ─────────────────────────────────────────────────────────────────

class FirestoreExt:
	"""
	Main Firestore sync engine for TouchDesigner.

	Threading model (no asyncio, no TDAsyncIO):
	  Main thread (TD cook):
	    - _drain_inbound()       RefreshHook -- drains inbound queue every frame
	    - Connect/Disconnect     manage Firebase lifecycle (synchronous -- gRPC
	                             requires main thread for app init)

	  ThreadManager pool tasks (workers pass results via queue.Queue,
	  processed by _drain_inbound on the main thread every frame):
	    - Bootstrap              uv venv setup --> SuccessHook/ExceptHook
	    - Discovery              db.collections() --> _discovery_results queue
	    - Write tasks            per-write --> _write_results queue
	    - Reconnect              sleep(delay) --> SuccessHook

	  ThreadManager standalone task:
	    - Keepalive              blocks on shutdown_event; RefreshHook=_drain_inbound

	  Firebase SDK threads (unmanaged by us):
	    - on_snapshot callbacks  --> _inbound_queue.put() ONLY. No TD access.

	Data format: JSON-per-row in collection tableDATs.
	  Columns: doc_id | payload (JSON) | _version | _synced_at | _dirty
	"""

	MAX_INBOUND_PER_FRAME = 10
	_DISCOVERY_INTERVAL = 30   # seconds between auto-discovery polls

	def __init__(self, ownerComp):
		self.my = ownerComp

		# Thread-safe bridge: Firebase SDK threads → main thread via RefreshHook
		self._inbound_queue = queue.Queue()

		# Keepalive task lifecycle
		self._keepalive_shutdown = threading.Event()

		# Firebase state -- main thread only
		self._firebase_app = None
		self._db = None
		self._listeners = {}           # collection_path → unsub callable
		self._active_collections = set()
		self._versions = {}            # (collection, doc_id) → update_time_str

		# Lazy-imported Firebase modules (populated after bootstrap)
		self._firebase_admin = None
		self._credentials_cls = None
		self._firestore_mod = None

		self._initialized = False

		# Periodic collection discovery
		self._last_discovery = 0.0
		self._last_discovered_colls = set()
		self._discovery_in_progress = False
		self._discovery_results = queue.Queue()

		# Write result queue: workers → main thread via _drain_inbound
		self._write_results = queue.Queue()

		# Structured log ring buffer (all levels, for programmatic access)
		self._log_buffer = deque(maxlen=500)
		self._log_counter = 0

		# Write-back: auto-push cell edits to Firestore
		self._applying_remote = False   # guard: suppress cell-change callback during remote writes
		self._pending_edits = {}        # (collection, doc_id) → Run object for debounce
		self._prompt_run = None         # delayed run for bootstrap prompt

	# ── Lifecycle ──────────────────────────────────────────────────────────────

	def onInitTD(self):
		"""Called automatically by TD when extensions initialize."""
		self.Init()

	def Init(self):
		"""Called from onInitTD. Main thread."""
		if self._initialized:
			return
		self._initialized = True
		self._log('info', 'FirestoreExt initializing')

		# Fast path: deps already available -- skip prompt and bootstrap
		if not self.my.ext.BootstrapExt.NeedsBootstrap(project.folder):
			self._log('info', 'Dependencies already installed -- skipping bootstrap')
			self._import_firebase()
			self._start_keepalive()
			import sys as _sys
			self.my.ext.WriteQueueExt.Init(project.folder)
			if self.my.par.Autoconnect.eval():
				self.Connect()
			return

		# Deps not available -- prompt user before installing
		self._update_status('connecting', error='Waiting for dependency install...')
		self._prompt_run = run(f"op('{self.my.path}').ext.FirestoreExt._prompt_bootstrap()", delayFrames=5)

	def _prompt_bootstrap(self):
		"""Show messageBox asking user to approve dependency install. Main thread."""
		# Guard: if this instance was replaced, bail out
		if self.my.ext.FirestoreExt is not self:
			return

		choice = ui.messageBox(
			'Firestore -- Install Dependencies',
			'This component requires Python packages:\n'
			'  - firebase-admin\n'
			'  - google-cloud-firestore\n\n'
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
		self.my.ext.WriteQueueExt.Init(project_folder)

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
			RefreshHook=self._drain_inbound,
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
		for run_obj in self._pending_edits.values():
			try:
				run_obj.kill()
			except Exception:
				pass
		self._pending_edits.clear()
		self._keepalive_shutdown.set()
		self._stop_listeners()
		self._cleanup_firebase()
		self._initialized = False

	# ── Public: Connection ─────────────────────────────────────────────────────

	def Connect(self):
		"""Connect to Firestore. Main thread (blocks during Firebase init)."""
		# Guard: if this instance was replaced by a newer extension init, bail out
		if self.my.ext.FirestoreExt is not self:
			return
		conn = self.my.ext.ConnectionExt
		if not conn.CanAttempt():
			self._log('warning', f'Circuit {conn.GetState()} -- skipping connect')
			return

		# Guard: missing key is a config issue, not a transient failure
		if not self.my.par.Privatekey.eval():
			self._log('warning', 'Privatekey parameter is not set -- skipping connect')
			self._update_status('disconnected')
			return

		# Tear down stale listeners so WatchCollection can re-register fresh
		self._stop_listeners()
		self._active_collections.clear()
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
		self._update_status('connected')
		self._log('info', 'Connected to Firestore')

		if self.my.par.Cachehydrate.eval():
			self._hydrate_from_cache()

		if self.my.par.Enablelistener.eval():
			self._discover_and_watch()

	def Disconnect(self):
		"""Clean disconnect. Main thread."""
		self._stop_listeners()
		self._cleanup_firebase()
		self._update_status('disconnected')
		self._log('info', 'Disconnected')

	def Reset(self):
		"""Full teardown to disconnected state. Use Connect to re-establish. Main thread."""
		choice = ui.messageBox(
			'Firestore -- Reset',
			'This will:\n'
			'  - Disconnect all listeners\n'
			'  - Clear the write queue (pending offline writes)\n'
			'  - Delete all collection tables\n'
			'  - Reset circuit breaker\n\n'
			'Any unsynced local edits will be lost.\n'
			'Use Connect to re-establish when ready.',
			buttons=['Reset', 'Cancel'],
		)
		if choice == 1:  # Cancel
			self._log('info', 'Reset cancelled by user')
			return

		self._keepalive_shutdown.set()
		self._stop_listeners()
		self._cleanup_firebase()
		self.my.ext.ConnectionExt.Reset()
		self.my.ext.WriteQueueExt.Clear()
		self._clear_collection_dats()
		self._versions.clear()
		self._active_collections.clear()
		self._initialized = False
		self._update_status('disconnected')
		self._log('info', 'Reset complete -- use Connect to re-establish')

	# ── Public: Callbacks ──────────────────────────────────────────────────────

	def CreateCallbacksDat(self):
		"""Create a sibling textDAT with pre-populated callback stubs."""
		parent_comp = self.my.parent()
		dat_name = 'callbacks'
		if parent_comp.op(dat_name):
			self._log('warning', f'{dat_name} already exists -- skipping creation')
			return
		new_dat = parent_comp.create(textDAT, dat_name)
		new_dat.par.language = 'python'
		new_dat.text = _CALLBACKS_TEMPLATE
		self.my.par.Callbacksdat = dat_name
		self._log('info', f'Created callbacks DAT: {parent_comp.path}/{dat_name}')

	# ── Public: Outbound writes ────────────────────────────────────────────────

	def PushDoc(self, collection, doc_id, data):
		"""Set document (full replace). Works offline -- queues if disconnected."""
		self._submit_write(collection, doc_id, 'set', data)

	def MergeDoc(self, collection, doc_id, data):
		"""set(merge=True) -- merge fields into existing document."""
		self._submit_write(collection, doc_id, 'set_merge', data)

	def UpdateDoc(self, collection, doc_id, data):
		"""update() -- partial update, document must already exist."""
		self._submit_write(collection, doc_id, 'update', data)

	def DeleteDoc(self, collection, doc_id):
		"""Delete a document."""
		self._submit_write(collection, doc_id, 'delete', {})

	def PushBatch(self, operations):
		"""
		Submit multiple writes. Each op dict must have:
		  collection, doc_id, op_type ('set'|'update'|'delete'|'set_merge'), payload (dict)
		"""
		for item in operations:
			self._submit_write(
				item['collection'], item['doc_id'],
				item.get('op_type', 'set'), item.get('payload', {}),
			)

	# ── Public: Inbound reads (local cache) ────────────────────────────────────

	def GetDoc(self, collection, doc_id):
		"""Return dict from local tableDAT, or None. No network call."""
		dat = self.my.op(f'collections/{collection}')
		if not dat:
			return None
		cell = dat.findCell(doc_id, cols=[0])
		if cell is None:
			return None
		row = int(cell.row)
		try:
			return json.loads(dat[row, self._COL_PAYLOAD].val)
		except (json.JSONDecodeError, AttributeError):
			return None

	def GetCollection(self, collection):
		"""Return list of dicts from local tableDAT. No network call."""
		dat = self.my.op(f'collections/{collection}')
		if not dat or dat.numRows < 2:
			return []
		result = []
		for row in range(1, dat.numRows):
			try:
				d = json.loads(dat[row, self._COL_PAYLOAD].val)
				d['_doc_id'] = dat[row, self._COL_DOC_ID].val
				result.append(d)
			except (json.JSONDecodeError, AttributeError):
				pass
		return result

	# ── Public: Subscriptions ─────────────────────────────────────────────────

	def WatchCollection(self, collection_path):
		"""
		Start real-time listener for a collection. Main thread.
		on_snapshot is non-blocking -- Firebase SDK manages its own listener thread.
		"""
		if collection_path in self._listeners:
			return
		if not self._db:
			self._log('warning', f'Cannot watch {collection_path} -- not connected')
			return

		self._active_collections.add(collection_path)
		self._ensure_collection_dat(collection_path)

		# Capture filter params on main thread -- closure must not access self.my
		raw = self.my.par.Filterfields.eval()
		filter_set = set(raw.split()) if raw else set()
		filter_fields_mode = self.my.par.Filterfieldsmode.eval()

		raw_docs = self.my.par.Filterdocs.eval()
		filter_docs_set = set(raw_docs.split()) if raw_docs else set()
		filter_docs_mode = self.my.par.Filterdocsmode.eval()

		col_ref = self._db.collection(collection_path)
		callback = self._make_snapshot_callback(
			collection_path, filter_set, filter_fields_mode,
			filter_docs_set, filter_docs_mode,
		)
		unsub = col_ref.on_snapshot(callback)
		self._listeners[collection_path] = unsub

		self._update_collections_status()
		self._log('info', f'Watching: {collection_path}')

	def RefreshCollections(self):
		"""Re-discover collections from Firestore and watch any new ones. Main thread."""
		if not self._db:
			self._log('warning', 'Cannot refresh collections -- not connected')
			return
		self._discover_and_watch()

	def UnwatchCollection(self, collection_path):
		"""Stop listener for a collection and remove its operators."""
		unsub = self._listeners.pop(collection_path, None)
		if unsub:
			try:
				unsub()
			except Exception:
				pass
		self._active_collections.discard(collection_path)

		# Destroy collection operators from the collections COMP
		collections_comp = self.my.op('collections')
		if collections_comp:
			for suffix in ('', '_cellwatch'):
				target = collections_comp.op(collection_path + suffix)
				if target:
					target.destroy()

		self._update_collections_status()
		self._log('info', f'Unwatched: {collection_path}')

	# ── Public: Diagnostics ───────────────────────────────────────────────────

	def FlushWriteQueue(self):
		"""Force-retry all queued offline writes. Main thread."""
		pending = self.my.ext.WriteQueueExt.GetPending()
		for item in pending:
			self._submit_write_item(item)
		self._log('info', f'Flushing {len(pending)} queued writes')

	def ClearWriteQueue(self):
		"""Discard all pending offline writes. Main thread."""
		self.my.ext.WriteQueueExt.Clear()
		self._log('info', 'Write queue cleared')

	def GetStatus(self):
		"""Return current status as a dict."""
		dat = self.my.op('status')
		if not dat or dat.numRows < 2:
			return {}
		return {dat[0, col].val: dat[1, col].val for col in range(dat.numCols)}

	def FlushDirty(self):
		"""Push all dirty docs to Firestore. Main thread."""
		count = 0
		for collection in list(self._active_collections):
			dat = self.my.op(f'collections/{collection}')
			if not dat or dat.numRows < 2:
				continue
			for row in range(1, dat.numRows):
				if dat[row, self._COL_DIRTY].val != '1':
					continue
				doc_id = dat[row, self._COL_DOC_ID].val
				payload_str = dat[row, self._COL_PAYLOAD].val
				try:
					payload = json.loads(payload_str)
				except json.JSONDecodeError as e:
					self._log('warning', f'FlushDirty skip {collection}/{doc_id}: invalid JSON: {e}')
					continue
				self._submit_write(collection, doc_id, 'set', payload)
				self._applying_remote = True
				try:
					dat[row, self._COL_DIRTY] = '0'
				finally:
					self._applying_remote = False
				count += 1
		self._log('info', f'FlushDirty: submitted {count} docs')

	# ── Internal: ThreadManager hooks (main thread) ────────────────────────────

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

	def _drain_inbound(self):
		"""
		Main thread. RefreshHook -- called every frame by ThreadManager.
		Drains inbound queue populated by Firebase SDK listener threads.
		"""
		count = 0
		while count < self.MAX_INBOUND_PER_FRAME:
			try:
				item = self._inbound_queue.get_nowait()
				self._process_inbound(item)
				count += 1
			except queue.Empty:
				break

		# Process write results from pool workers
		while True:
			try:
				wr = self._write_results.get_nowait()
				self._process_write_result(wr)
			except queue.Empty:
				break

		# Process discovery results from pool worker
		try:
			status, data = self._discovery_results.get_nowait()
			self._discovery_in_progress = False
			if status == 'ok':
				colls = data
				if colls:
					coll_set = set(colls)
					if coll_set != self._last_discovered_colls:
						self._log('info', f'Auto-discovered {len(colls)} collections: {" ".join(sorted(colls))}')
						self._last_discovered_colls = coll_set
				else:
					self._log('warning', 'No collections found in Firestore database')
				for coll in colls:
					if coll:
						self.WatchCollection(coll)
			else:
				self._log('error', f'Collection discovery failed: {data}')
		except queue.Empty:
			pass

		# Periodic auto-discovery of new collections (only in auto-discover mode)
		if (self._db
				and self.my.par.Collections.eval().strip() in ('', '*')
				and time.time() - self._last_discovery > self._DISCOVERY_INTERVAL):
			self._last_discovery = time.time()
			self._discover_and_watch()

	# Note: write and discovery results are passed via queue.Queue, NOT via
	# SuccessHook returnValue (ThreadManager calls SuccessHook with no args).
	# _drain_inbound processes results on the main thread every frame.

	def _on_reconnect_done(self, returnValue=None):
		"""Main thread. SuccessHook called after reconnect sleep completes."""
		# Guard: if this instance was replaced by a newer extension init, bail out
		if self.my.ext.FirestoreExt is not self:
			return
		self._log('info', 'Attempting reconnect after backoff')
		self.Connect()

	def _on_reconnect_error(self, error):
		"""Main thread. Should not normally fire."""
		self._log('warning', f'Reconnect sleep interrupted: {error}')

	# ── Internal: Firebase setup ──────────────────────────────────────────────

	def _import_firebase(self):
		"""Import firebase_admin after bootstrap. Main thread."""
		import firebase_admin
		from firebase_admin import credentials, firestore
		self._firebase_admin = firebase_admin
		self._credentials_cls = credentials
		self._firestore_mod = firestore

	def _connect_firebase(self):
		"""Initialize Firebase app and Firestore client. Main thread (blocks)."""
		key_path = self.my.par.Privatekey.eval()
		db_id = self.my.par.Databaseid.eval() or '(default)'
		app_name = f'td_{self.my.name}'

		cred = self._credentials_cls.Certificate(key_path)
		with open(key_path, 'r') as f:
			project_id = json.loads(f.read()).get('project_id', '')
		if not project_id:
			raise ValueError('Service account JSON is missing project_id')

		self._firebase_app = self._firebase_admin.initialize_app(
			cred, {'projectId': project_id}, name=app_name,
		)
		if db_id == '(default)':
			self._db = self._firestore_mod.client(app=self._firebase_app)
		else:
			self._db = self._firestore_mod.client(
				app=self._firebase_app, database_id=db_id,
			)

	def _cleanup_firebase(self):
		"""Safely delete Firebase app instance. Main thread."""
		if self._firebase_app and self._firebase_admin:
			try:
				self._firebase_admin.delete_app(self._firebase_app)
			except Exception:
				pass
		self._firebase_app = None
		self._db = None

	def _stop_listeners(self):
		"""Unsubscribe all active on_snapshot listeners. Main thread."""
		for unsub in list(self._listeners.values()):
			try:
				unsub()
			except Exception:
				pass
		self._listeners.clear()

	def _discover_and_watch(self):
		"""Resolve collection list and start watchers. Main thread."""
		colls = self.my.par.Collections.eval().split()
		if any(colls) and colls != ['*']:
			# Explicit collection list -- no network call needed
			for coll in colls:
				if coll:
					self.WatchCollection(coll)
			return

		# Auto-discover mode: offload blocking db.collections() to thread pool
		if self._discovery_in_progress:
			return
		self._discovery_in_progress = True

		tm = op.TDResources.ThreadManager
		task = tm.TDTask(
			target=_discover_collections_worker,
			args=(self._db, self._discovery_results),
		)
		tm.EnqueueTask(task)

	# ── Internal: Snapshot callback factory ──────────────────────────────────

	def _make_snapshot_callback(self, collection_path, filter_set, filter_fields_mode,
								filter_docs_set, filter_docs_mode):
		"""
		Return a closure-safe on_snapshot callback.
		Captures only plain Python objects -- no TD refs. Safe to call from Firebase thread.
		"""
		inbound_q = self._inbound_queue

		def _callback(docs, changes, read_time):
			for change in changes:
				doc = change.document
				doc_id = doc.id
				change_type = change.type.name.lower()   # 'added' | 'modified' | 'removed'

				# Document-level filter (applies to all change types including removed)
				if filter_docs_set:
					if filter_docs_mode == 'include' and doc_id not in filter_docs_set:
						continue
					if filter_docs_mode == 'exclude' and doc_id in filter_docs_set:
						continue

				if change_type == 'removed':
					inbound_q.put((collection_path, doc_id, 'removed', {}, ''))
					continue

				try:
					data = doc.to_dict() or {}
					if filter_set:
						if filter_fields_mode == 'include':
							data = {k: v for k, v in data.items() if k in filter_set}
						else:
							data = {k: v for k, v in data.items() if k not in filter_set}
					payload = {k: _serialize_value(v) for k, v in data.items()}
					update_time = doc.update_time.isoformat() if doc.update_time else ''
					inbound_q.put((collection_path, doc_id, change_type, payload, update_time))
				except Exception as e:
					inbound_q.put(('__error__', collection_path, 'snapshot_error', {}, str(e)))

		return _callback

	# ── Internal: Write routing ───────────────────────────────────────────────

	def _submit_write(self, collection, doc_id, op_type, payload):
		"""Route write: offline queue or direct TDTask. Main thread."""
		conn = self.my.ext.ConnectionExt
		item = {
			'queue_id': '',
			'collection': collection,
			'doc_id': doc_id,
			'op_type': op_type,
			'payload': payload,
		}
		if not conn.CanAttempt():
			if self.my.par.Enablecache.eval():
				qid = self.my.ext.WriteQueueExt.Enqueue(collection, doc_id, op_type, payload)
				item['queue_id'] = qid
			self._log('debug', f'Queued offline: {collection}/{doc_id}')
		else:
			self._submit_write_item(item)

	def _submit_write_item(self, item):
		"""Submit a write item as a ThreadManager pool TDTask. Main thread."""
		db = self._db   # captured on main thread -- passed to worker, no self._db access in worker

		tm = op.TDResources.ThreadManager
		task = tm.TDTask(
			target=_execute_write_queued,
			args=(db, item, self._write_results),
		)
		tm.EnqueueTask(task)

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

	# ── Internal: Queue processing (main thread) ──────────────────────────────

	def _process_inbound(self, item):
		"""Process one item from the inbound queue. Main thread only."""
		collection, doc_id, change_type, payload, update_time = item

		if collection == '__error__':
			self._log('error', f'Listener error [{doc_id}]: {update_time}')
			self.my.ext.ConnectionExt.RecordFailure()
			self._update_status('reconnecting', error=update_time)
			return

		if change_type == 'removed':
			self._delete_from_table(collection, doc_id)
			self.my.ext.WriteQueueExt.DeleteDocument(collection, doc_id)
			self._fire_callback('onFirestoreChange', collection, doc_id, 'removed', {})
			return

		# Version check -- skip self-echoes from our own writes
		stored = self._versions.get((collection, doc_id), '')
		if update_time and stored and update_time <= stored:
			return

		payload_str = json.dumps(payload)
		synced_at = datetime.datetime.utcnow().isoformat()
		self._write_to_table(collection, doc_id, payload_str, update_time, synced_at, dirty=0)

		if update_time:
			self._versions[(collection, doc_id)] = update_time

		if self.my.par.Enablecache.eval():
			self.my.ext.WriteQueueExt.SaveDocument(collection, doc_id, payload_str, update_time, synced_at)

		self._fire_callback('onFirestoreChange', collection, doc_id, change_type, payload)

	def _process_write_result(self, result):
		"""Process a write result dict. Main thread only."""
		queue_id = result.get('queue_id', '')
		doc_id = result.get('doc_id', '')
		collection = result.get('collection', '')
		success = result.get('success', False)
		update_time = result.get('update_time', '')
		error = result.get('error')

		if success:
			if queue_id:
				self.my.ext.WriteQueueExt.Remove(queue_id)
			if update_time and collection and doc_id:
				self._versions[(collection, doc_id)] = update_time
			self._fire_callback('onWriteComplete', collection, doc_id, True, None)
		else:
			if queue_id:
				self.my.ext.WriteQueueExt.RecordRetry(queue_id, error)
			self._log('error', f'Write failed [{collection}/{doc_id}]: {error}')
			self.my.ext.ConnectionExt.RecordFailure()
			self._fire_callback('onWriteComplete', collection, doc_id, False, error)

		dat = self.my.op('status')
		if dat and dat.numRows >= 2:
			dat[1, 'queue_depth'] = self.my.ext.WriteQueueExt.Count()

	# ── Internal: TableDAT management (main thread) ──────────────────────────

	def _clear_collection_dats(self):
		"""Destroy all collection tableDATs and their cellwatch watchers. Main thread."""
		collections_comp = self.my.op('collections')
		if not collections_comp:
			return
		to_destroy = list(collections_comp.children)
		count = sum(1 for c in to_destroy if c.OPType == 'tableDAT')
		for child in to_destroy:
			child.destroy()
		if count:
			self._log('info', f'Destroyed {count} collection table(s) and watchers')

	# Grid layout for collection DATs inside the collections COMP
	_GRID_COLS = 4        # DATs per row before wrapping
	_GRID_X_STEP = 200    # horizontal spacing between DATs
	_GRID_Y_STEP = -400   # vertical spacing (table + cellwatch + padding)
	_CELLWATCH_COLOR = (0.35, 0.35, 0.35)  # darker grey so watchers recede

	def _ensure_collection_dat(self, collection):
		"""Create a collection tableDAT if one doesn't exist, positioned on a grid."""
		existing = self.my.op(f'collections/{collection}')
		if existing:
			return existing
		collections_comp = self.my.op('collections')
		if not collections_comp:
			self._log('error', 'collections COMP missing')
			return None

		# Find first unoccupied grid slot
		occupied = set()
		for c in collections_comp.children:
			if c.OPType == 'tableDAT':
				occupied.add((c.nodeX, c.nodeY))
		idx = 0
		while True:
			col = idx % self._GRID_COLS
			row = idx // self._GRID_COLS
			x = col * self._GRID_X_STEP
			y = row * self._GRID_Y_STEP
			if (x, y) not in occupied:
				break
			idx += 1

		dat = collections_comp.create(tableDAT, collection)
		dat.nodeX = x
		dat.nodeY = y
		dat.clear()
		dat.appendRow(['doc_id', 'payload', '_version', '_synced_at', '_dirty'])

		# Create cell-change watcher for write-back (placed just below its tableDAT)
		watcher_name = f'{collection}_cellwatch'
		watcher = collections_comp.create(datexecuteDAT, watcher_name)
		watcher.nodeX = x
		watcher.nodeY = y - 200
		watcher.color = self._CELLWATCH_COLOR
		watcher.par.dat = dat
		watcher.par.tablechange = True
		watcher.par.active = True
		watcher.text = _CELL_WATCH_SCRIPT

		return dat

	# Column indices: 0=doc_id, 1=payload, 2=_version, 3=_synced_at, 4=_dirty
	_COL_DOC_ID = 0
	_COL_PAYLOAD = 1
	_COL_VERSION = 2
	_COL_SYNCED = 3
	_COL_DIRTY = 4

	def _write_to_table(self, collection, doc_id, payload_str, version, synced_at, dirty=0):
		"""Insert or update a row in the collection tableDAT. Main thread."""
		dat = self._ensure_collection_dat(collection)
		if not dat:
			return
		self._applying_remote = True
		try:
			cell = dat.findCell(doc_id, cols=[self._COL_DOC_ID])
			if cell is not None:
				row = int(cell.row)
				dat[row, self._COL_PAYLOAD] = payload_str
				dat[row, self._COL_VERSION] = version
				dat[row, self._COL_SYNCED] = synced_at
				dat[row, self._COL_DIRTY] = dirty
			else:
				dat.appendRow([doc_id, payload_str, version, synced_at, dirty])
		finally:
			self._applying_remote = False

	def _delete_from_table(self, collection, doc_id):
		"""Delete a document row from the collection tableDAT. Main thread."""
		dat = self.my.op(f'collections/{collection}')
		if not dat:
			return
		cell = dat.findCell(doc_id, cols=[0])
		if cell is not None:
			dat.deleteRow(cell.row)

	def _hydrate_from_cache(self):
		"""Populate collection tableDATs from SQLite on startup. Main thread."""
		docs = self.my.ext.WriteQueueExt.LoadDocuments()
		for collection, doc_id, payload_str, version, synced_at in docs:
			self._write_to_table(collection, doc_id, payload_str, version, synced_at, dirty=0)
		if docs:
			self._log('info', f'Hydrated {len(docs)} docs from cache')

	# ── Internal: Write-back (cell edit → Firestore) ────────────────────────

	def _on_cell_edit(self, dat, info):
		"""
		Called by per-collection datexecDATs via onTableChange.
		Debounces and routes payload edits to Firestore. Main thread.
		"""
		if self._applying_remote:
			return
		if not self.my.par.Enablewriteback.eval():
			return

		changed_cells = info.get('cellsChanged', []) if isinstance(info, dict) else getattr(info, 'cellsChanged', [])
		for cell in changed_cells:
			# Only care about payload column edits on data rows
			if int(cell.col) != self._COL_PAYLOAD or int(cell.row) == 0:
				continue

			doc_id = dat[int(cell.row), self._COL_DOC_ID].val
			if not doc_id:
				continue

			collection = dat.name

			# Mark dirty (under guard to avoid re-triggering)
			self._applying_remote = True
			try:
				dat[int(cell.row), self._COL_DIRTY] = '1'
			finally:
				self._applying_remote = False

			# Debounce: cancel previous pending run for this doc
			key = (collection, doc_id)
			prev_run = self._pending_edits.get(key)
			if prev_run:
				try:
					prev_run.kill()
				except Exception:
					pass

			# Schedule debounced write -- run() accepts a callable + args
			self._pending_edits[key] = run(
				'args[0]._execute_debounced_write(args[1], args[2])',
				self, collection, doc_id,
				delayMilliSeconds=500,
			)

	def _execute_debounced_write(self, collection, doc_id):
		"""Called after debounce delay. Validates JSON and submits write. Main thread."""
		key = (collection, doc_id)
		self._pending_edits.pop(key, None)

		dat = self.my.op(f'collections/{collection}')
		if not dat:
			return

		cell = dat.findCell(doc_id, cols=[0])
		if cell is None:
			return

		row = int(cell.row)

		# If a remote update arrived during debounce, dirty was reset -- skip
		if dat[row, self._COL_DIRTY].val != '1':
			return

		payload_str = dat[row, self._COL_PAYLOAD].val
		try:
			payload = json.loads(payload_str)
		except json.JSONDecodeError as e:
			self._log('warning', f'Write-back skipped -- invalid JSON in {collection}/{doc_id}: {e}')
			return

		self._submit_write(collection, doc_id, 'set', payload)

		# Clear dirty flag under guard
		self._applying_remote = True
		try:
			dat[row, self._COL_DIRTY] = '0'
		finally:
			self._applying_remote = False

		self._log('debug', f'Write-back: {collection}/{doc_id}')

	# ── Internal: Callbacks ───────────────────────────────────────────────────

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

	# ── Internal: Status + logging ────────────────────────────────────────────

	def _update_status(self, state, error=''):
		"""Update status tableDAT. Main thread."""
		dat = self.my.op('status')
		if not dat or dat.numRows < 2:
			return
		dat[1, 'state'] = state
		dat[1, 'circuit'] = self.my.ext.ConnectionExt.GetState()
		dat[1, 'last_error'] = str(error)
		dat[1, 'queue_depth'] = self.my.ext.WriteQueueExt.Count()
		if state == 'connected':
			dat[1, 'connected_at'] = datetime.datetime.utcnow().isoformat()
		self._fire_callback('onConnectionStateChange', state, error or None)

	def _update_collections_status(self):
		dat = self.my.op('status')
		if dat and dat.numRows >= 2:
			dat[1, 'collections'] = ' '.join(self._active_collections)

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

		entry = f'{ts} {current_frame:>7} {level_str:<7} Firestore: {msg}'

		print(entry)

		log_dat = self.my.op('log')
		if log_dat:
			log_dat.appendRow([entry])
		ext_path = self.my.par.Logop.eval()
		if ext_path:
			ext_log = op(ext_path)
			if ext_log:
				ext_log.appendRow([entry])

	def _debug(self, msg):
		self._log('debug', msg)

	def _info(self, msg):
		self._log('info', msg)

	def _warn(self, msg):
		self._log('warning', msg)

	def _error(self, msg):
		self._log('error', msg)

	def GetLogs(self, level=None, limit=50):
		"""
		Return recent log entries from the ring buffer.
		Useful for MCP/programmatic access.

		Args:
			level: Optional filter -- 'INFO', 'WARNING', 'ERROR', 'DEBUG', or None for all.
			limit: Max entries to return (default 50).
		"""
		entries = list(self._log_buffer)
		if level:
			entries = [e for e in entries if e['level'] == level.upper()]
		return entries[-limit:]
