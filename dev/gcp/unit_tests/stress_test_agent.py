"""
Stress Test Agent - TD-side script for integration stress testing.

Lives as a textDAT in /gcp/unit_tests. Listens on __stress_control
Firestore collection for commands from the pytest stress test suite,
executes TD operations, and writes results to __stress_results.

Reuses the firestore COMP's _db reference - does NOT init its own
Firebase connection.

Threading safety:
  - on_snapshot callback (Firebase thread) puts commands into a queue
  - _drain_commands() runs on main thread via run() polling
  - All TD ops happen on main thread only
"""

import json
import queue
import time
import datetime
import traceback


# ---------------------------------------------------------------------------
# Module-level helpers (no TD refs)
# ---------------------------------------------------------------------------

def _serialize_value(v):
	"""Recursively convert Firestore types to JSON-safe Python."""
	if isinstance(v, datetime.datetime):
		return v.isoformat()
	if hasattr(v, 'path') and hasattr(v, 'id'):
		return v.path
	if isinstance(v, dict):
		return {k: _serialize_value(val) for k, val in v.items()}
	if isinstance(v, (list, tuple)):
		return [_serialize_value(item) for item in v]
	return v


class StressTestAgent:
	"""
	Remote-controlled test agent inside TD.

	Commands arrive via Firestore __stress_control collection.
	Results are written to __stress_results collection.
	"""

	def __init__(self, ownerComp):
		self.my = ownerComp
		self._command_queue = queue.Queue()
		self._listener_unsub = None
		self._polling_run = None
		self._active = False

	# -- Lifecycle -----------------------------------------------------------

	def onInitTD(self):
		pass

	def Start(self):
		"""Begin listening for stress test commands."""
		fs = self._get_firestore_ext()
		if not fs or not fs._db:
			debug('StressTestAgent: Firestore COMP not connected')
			return

		db = fs._db
		self._active = True

		# Listen on __stress_control for command docs
		col_ref = db.collection('__stress_control')
		inbound_q = self._command_queue

		def _on_command(docs, changes, read_time):
			for change in changes:
				if change.type.name != 'ADDED':
					continue
				try:
					data = change.document.to_dict()
					if not data or data.get('status') != 'pending':
						continue
					data['_doc_id'] = change.document.id
					inbound_q.put(data)
				except Exception as e:
					debug(f'StressTestAgent: error parsing command: {e}')

		self._listener_unsub = col_ref.on_snapshot(_on_command)
		self._start_polling()
		debug('StressTestAgent: started, listening on __stress_control')

	def Stop(self):
		"""Stop listening and clean up."""
		self._active = False
		if self._listener_unsub:
			try:
				self._listener_unsub()
			except Exception:
				pass
			self._listener_unsub = None
		if self._polling_run:
			try:
				self._polling_run.kill()
			except Exception:
				pass
			self._polling_run = None
		debug('StressTestAgent: stopped')

	def onDestroyTD(self):
		self.Stop()

	# -- Polling -------------------------------------------------------------

	def _start_polling(self):
		"""Schedule periodic drain of command queue on main thread."""
		if not self._active:
			return
		self._drain_commands()
		self._polling_run = run(
			'args[0]._start_polling()',
			self,
			delayFrames=3,
		)

	def _drain_commands(self):
		"""Process pending commands from the queue. Main thread."""
		processed = 0
		while processed < 5:
			try:
				cmd_data = self._command_queue.get_nowait()
			except queue.Empty:
				break
			self._execute_command(cmd_data)
			processed += 1

	# -- Command execution ---------------------------------------------------

	def _execute_command(self, cmd_data):
		"""Execute a single command and write result. Main thread."""
		request_id = cmd_data.get('request_id', '')
		cmd = cmd_data.get('cmd', '')
		args = cmd_data.get('args', {})
		frame_start = absTime.frame

		try:
			result = self._dispatch(cmd, args)
			self._write_result(request_id, 'done', result, None, frame_start)
		except Exception as e:
			tb = traceback.format_exc()
			debug(f'StressTestAgent: command {cmd} failed: {e}\n{tb}')
			self._write_result(request_id, 'error', {}, str(e), frame_start)

	def _dispatch(self, cmd, args):
		"""Route command to handler. Main thread."""
		handlers = {
			'push_batch': self._cmd_push_batch,
			'push_doc': self._cmd_push_doc,
			'flush_dirty': self._cmd_flush_dirty,
			'read_table': self._cmd_read_table,
			'edit_cells': self._cmd_edit_cells,
			'get_stats': self._cmd_get_stats,
			'delete_docs': self._cmd_delete_docs,
			'ping': self._cmd_ping,
		}
		handler = handlers.get(cmd)
		if not handler:
			raise ValueError(f'Unknown command: {cmd}')
		return handler(args)

	# -- Command handlers ----------------------------------------------------

	def _cmd_ping(self, args):
		"""Health check."""
		return {'pong': True, 'frame': absTime.frame}

	def _cmd_push_batch(self, args):
		"""Push multiple docs via PushBatch."""
		fs = self._get_firestore_ext()
		collection = args['collection']
		docs = args.get('docs', {})
		ops = [
			{
				'collection': collection,
				'doc_id': doc_id,
				'op_type': 'set',
				'payload': payload,
			}
			for doc_id, payload in docs.items()
		]
		fs.PushBatch(ops)
		return {'success': True, 'count': len(ops)}

	def _cmd_push_doc(self, args):
		"""Push a single doc."""
		fs = self._get_firestore_ext()
		fs.PushDoc(args['collection'], args['doc_id'], args['data'])
		return {'success': True}

	def _cmd_flush_dirty(self, args):
		"""Flush all dirty docs."""
		fs = self._get_firestore_ext()
		fs.FlushDirty()
		return {'success': True}

	def _cmd_read_table(self, args):
		"""Read a collection tableDAT and return rows."""
		fs = self._get_firestore_ext()
		collection = args['collection']
		docs = fs.GetCollection(collection)
		return {'rows': docs, 'count': len(docs)}

	def _cmd_edit_cells(self, args):
		"""Edit payload cells in a collection table and mark dirty."""
		fs = self._get_firestore_ext()
		collection = args['collection']
		edits = args.get('edits', {})  # {doc_id: new_payload_dict}
		dat = fs.my.op(f'collections/{collection}')
		if not dat:
			raise ValueError(f'No tableDAT for collection: {collection}')

		edited = 0
		for doc_id, new_payload in edits.items():
			cell = dat.findCell(doc_id, cols=[0])
			if cell is None:
				continue
			row = int(cell.row)
			dat[row, 'payload'] = json.dumps(new_payload)
			dat[row, '_dirty'] = '1'
			edited += 1
		return {'success': True, 'edited': edited}

	def _cmd_get_stats(self, args):
		"""Return frame stats, queue depth, version count."""
		fs = self._get_firestore_ext()
		return {
			'frame': absTime.frame,
			'time': time.time(),
			'cook_rate': project.cookRate,
			'inbound_queue_depth': fs._inbound_queue.qsize(),
			'write_queue_depth': fs._write_results.qsize(),
			'active_collections': list(fs._active_collections),
			'version_count': len(fs._versions),
			'listeners': list(fs._listeners.keys()),
		}

	def _cmd_delete_docs(self, args):
		"""Delete multiple docs."""
		fs = self._get_firestore_ext()
		collection = args['collection']
		doc_ids = args.get('doc_ids', [])
		for doc_id in doc_ids:
			fs.DeleteDoc(collection, doc_id)
		return {'success': True, 'deleted': len(doc_ids)}

	# -- Helpers -------------------------------------------------------------

	def _get_firestore_ext(self):
		"""Get the sibling firestore COMP's extension. Main thread."""
		return op('../firestore').ext.FirestoreExt

	def _write_result(self, request_id, status, result, error, frame_start):
		"""Write command result to __stress_results/{request_id}. Main thread."""
		if not request_id:
			return
		fs = self._get_firestore_ext()
		db = fs._db
		if not db:
			debug('StressTestAgent: cannot write result - no db connection')
			return

		doc_data = {
			'request_id': request_id,
			'status': status,
			'result': _serialize_value(result),
			'error': error,
			'frame_start': frame_start,
			'frame_end': absTime.frame,
			'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
		}
		try:
			db.collection('__stress_results').document(request_id).set(doc_data)
		except Exception as e:
			debug(f'StressTestAgent: failed to write result {request_id}: {e}')