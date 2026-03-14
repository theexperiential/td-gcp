import sqlite3
import json
import uuid
import threading
import datetime
from pathlib import Path


class WriteQueueExt:
	"""
	Persistent offline write queue backed by SQLite.
	write_queue_dat tableDAT mirrors pending writes for visual inspection.

	Thread safety:
	  - _lock protects SQLite access from any thread
	  - tableDAT mutations are main-thread only (callers' responsibility)
	"""

	def __init__(self, ownerComp):
		self.my = ownerComp
		self._lock = threading.Lock()
		self._db_conn = None

	def Init(self, project_folder):
		"""Called from main thread during extension init. project_folder from project.folder."""
		self._ensure_db(project_folder)
		self._sync_dat_from_db()

	# ── Public API ────────────────────────────────────────────────────────────

	def Enqueue(self, collection, doc_id, op_type, payload):
		"""
		Add a write to the offline queue.
		op_type: 'set' | 'update' | 'delete' | 'set_merge'
		payload: dict
		Returns queue_id string.
		"""
		queue_id = str(uuid.uuid4())
		queued_at = datetime.datetime.utcnow().isoformat()
		payload_str = json.dumps(payload) if payload else '{}'
		with self._lock:
			self._db_execute(
				'INSERT INTO write_queue VALUES (?,?,?,?,?,?,?,?)',
				(queue_id, collection, doc_id, op_type, payload_str, queued_at, 0, ''),
			)
		self._append_dat_row(queue_id, collection, doc_id, op_type, payload_str, queued_at, 0, '')
		return queue_id

	def GetPending(self):
		"""Return list of pending write op dicts ordered by queued_at."""
		with self._lock:
			rows = self._db_fetch('SELECT * FROM write_queue ORDER BY queued_at ASC')
		return [
			{
				'queue_id': r[0], 'collection': r[1], 'doc_id': r[2],
				'op_type': r[3], 'payload': json.loads(r[4] or '{}'),
				'queued_at': r[5], 'retry_count': r[6], 'last_error': r[7],
			}
			for r in rows
		]

	def Remove(self, queue_id):
		"""Remove a successfully flushed write. Main thread only."""
		with self._lock:
			self._db_execute('DELETE FROM write_queue WHERE queue_id=?', (queue_id,))
		self._remove_dat_row(queue_id)

	def RecordRetry(self, queue_id, error_msg):
		"""Increment retry count and log error. Main thread only."""
		with self._lock:
			self._db_execute(
				'UPDATE write_queue SET retry_count=retry_count+1, last_error=? WHERE queue_id=?',
				(str(error_msg), queue_id),
			)
		self._sync_dat_from_db()

	def Clear(self):
		"""Discard all pending writes. Main thread only."""
		with self._lock:
			self._db_execute('DELETE FROM write_queue')
		dat = self.my.op('write_queue_dat')
		if dat:
			dat.clear(keepFirstRow=True)

	def Count(self):
		"""Return number of pending writes."""
		with self._lock:
			rows = self._db_fetch('SELECT COUNT(*) FROM write_queue')
		return int(rows[0][0]) if rows else 0

	# ── Document cache ────────────────────────────────────────────────────────

	def SaveDocument(self, collection, doc_id, payload_str, version, synced_at):
		"""Upsert a document into the SQLite cache."""
		with self._lock:
			self._db_execute(
				'INSERT OR REPLACE INTO documents (collection, doc_id, payload, version, synced_at) VALUES (?,?,?,?,?)',
				(collection, doc_id, payload_str, version, synced_at),
			)

	def DeleteDocument(self, collection, doc_id):
		"""Remove a document from the SQLite cache."""
		with self._lock:
			self._db_execute(
				'DELETE FROM documents WHERE collection=? AND doc_id=?',
				(collection, doc_id),
			)

	def LoadDocuments(self):
		"""Return all cached documents for hydrating tableDATs on startup."""
		with self._lock:
			return self._db_fetch(
				'SELECT collection, doc_id, payload, version, synced_at FROM documents'
			)

	# ── Internal ──────────────────────────────────────────────────────────────

	def _get_db_path(self, project_folder):
		cache_folder = self.my.par.Cachepath.eval() or 'cache'
		p = Path(project_folder) / cache_folder
		p.mkdir(parents=True, exist_ok=True)
		return str(p / 'gcp.db')

	def _ensure_db(self, project_folder):
		db_path = self._get_db_path(project_folder)
		self._db_conn = sqlite3.connect(db_path, check_same_thread=False)
		self._db_conn.execute('''
			CREATE TABLE IF NOT EXISTS write_queue (
				queue_id   TEXT PRIMARY KEY,
				collection TEXT NOT NULL,
				doc_id     TEXT NOT NULL,
				op_type    TEXT NOT NULL,
				payload    TEXT NOT NULL,
				queued_at  TEXT NOT NULL,
				retry_count INTEGER DEFAULT 0,
				last_error TEXT DEFAULT ""
			)
		''')
		self._db_conn.execute('''
			CREATE TABLE IF NOT EXISTS documents (
				collection TEXT NOT NULL,
				doc_id     TEXT NOT NULL,
				payload    TEXT NOT NULL,
				version    TEXT NOT NULL,
				synced_at  TEXT NOT NULL,
				PRIMARY KEY (collection, doc_id)
			)
		''')
		self._db_conn.commit()

	def _db_execute(self, sql, params=()):
		if self._db_conn:
			self._db_conn.execute(sql, params)
			self._db_conn.commit()

	def _db_fetch(self, sql, params=()):
		if not self._db_conn:
			return []
		cur = self._db_conn.execute(sql, params)
		return cur.fetchall()

	def _sync_dat_from_db(self):
		"""Rebuild write_queue_dat from SQLite. Main thread only."""
		dat = self.my.op('write_queue_dat')
		if not dat:
			return
		rows = self._db_fetch('SELECT * FROM write_queue ORDER BY queued_at ASC')
		dat.clear(keepFirstRow=True)
		for r in rows:
			dat.appendRow(list(r))

	def _append_dat_row(self, *cols):
		dat = self.my.op('write_queue_dat')
		if dat:
			dat.appendRow(list(cols))

	def _remove_dat_row(self, queue_id):
		dat = self.my.op('write_queue_dat')
		if not dat:
			return
		cells = dat.findCell(queue_id, cols=[0])
		if cells:
			dat.deleteRow(cells[0].row)
