"""
Firebase test client for stress tests.

Separate Firebase Admin SDK instance (app name 'stress_test') that
connects to the same Firestore project as TD but operates independently.
Used by pytest to inject test data, send commands, and verify results.
"""

import json
import time
import uuid
import threading
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore


class FirebaseTestClient:
    """Direct Firestore access for the stress test suite."""

    def __init__(self, service_account_path: str, database_id: str = '(default)'):
        with open(service_account_path, 'r') as f:
            project_id = json.loads(f.read()).get('project_id', '')
        if not project_id:
            raise ValueError('Service account JSON missing project_id')

        cred = credentials.Certificate(service_account_path)
        self._app = firebase_admin.initialize_app(
            cred, {'projectId': project_id}, name='stress_test',
        )
        if database_id == '(default)':
            self._db = firestore.client(app=self._app)
        else:
            self._db = firestore.client(app=self._app, database_id=database_id)
        self._listeners = []

    def close(self):
        """Clean up Firebase app."""
        for unsub in self._listeners:
            try:
                unsub()
            except Exception:
                pass
        self._listeners.clear()
        try:
            firebase_admin.delete_app(self._app)
        except Exception:
            pass

    # -- Document operations -------------------------------------------------

    def write_doc(self, collection: str, doc_id: str, data: dict) -> None:
        """Set a single document."""
        self._db.collection(collection).document(doc_id).set(data)

    def write_batch(self, collection: str, docs: dict[str, dict]) -> None:
        """Batch write multiple documents. Max 500 per Firestore batch."""
        batch = self._db.batch()
        count = 0
        for doc_id, data in docs.items():
            ref = self._db.collection(collection).document(doc_id)
            batch.set(ref, data)
            count += 1
            if count >= 500:
                batch.commit()
                batch = self._db.batch()
                count = 0
        if count > 0:
            batch.commit()

    def delete_doc(self, collection: str, doc_id: str) -> None:
        """Delete a single document."""
        self._db.collection(collection).document(doc_id).delete()

    def delete_collection(self, collection: str) -> None:
        """Delete all documents in a collection."""
        col_ref = self._db.collection(collection)
        while True:
            docs = list(col_ref.limit(500).stream())
            if not docs:
                break
            batch = self._db.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()

    def read_doc(self, collection: str, doc_id: str) -> dict | None:
        """Read a single document."""
        doc = self._db.collection(collection).document(doc_id).get()
        return doc.to_dict() if doc.exists else None

    def read_collection(self, collection: str) -> dict[str, dict]:
        """Read all documents in a collection."""
        result = {}
        for doc in self._db.collection(collection).stream():
            result[doc.id] = doc.to_dict()
        return result

    # -- Listener for outbound verification ----------------------------------

    def listen(self, collection: str, expected_count: int, timeout: float = 60.0):
        """
        Attach on_snapshot listener and wait for expected_count documents.

        Returns a dict of {doc_id: data} for all received documents.
        Raises TimeoutError if not enough docs arrive in time.
        """
        received = {}
        lock = threading.Lock()
        event = threading.Event()

        def _on_snapshot(docs, changes, read_time):
            with lock:
                for change in changes:
                    if change.type.name in ('ADDED', 'MODIFIED'):
                        received[change.document.id] = change.document.to_dict()
                    elif change.type.name == 'REMOVED':
                        received.pop(change.document.id, None)
                if len(received) >= expected_count:
                    event.set()

        col_ref = self._db.collection(collection)
        unsub = col_ref.on_snapshot(_on_snapshot)
        self._listeners.append(unsub)

        if not event.wait(timeout=timeout):
            unsub()
            self._listeners.remove(unsub)
            raise TimeoutError(
                f'Expected {expected_count} docs in {collection}, '
                f'got {len(received)} after {timeout}s'
            )

        unsub()
        self._listeners.remove(unsub)
        with lock:
            return dict(received)

    def listen_for_deletions(
        self, collection: str, doc_ids: list[str], timeout: float = 60.0
    ):
        """
        Wait until all specified doc_ids are removed from the collection.

        Returns True if all were removed within timeout.
        Raises TimeoutError otherwise.
        """
        remaining = set(doc_ids)
        lock = threading.Lock()
        event = threading.Event()

        def _on_snapshot(docs, changes, read_time):
            with lock:
                for change in changes:
                    if change.type.name == 'REMOVED':
                        remaining.discard(change.document.id)
                if not remaining:
                    event.set()

        col_ref = self._db.collection(collection)
        unsub = col_ref.on_snapshot(_on_snapshot)
        self._listeners.append(unsub)

        if not event.wait(timeout=timeout):
            unsub()
            self._listeners.remove(unsub)
            raise TimeoutError(
                f'Expected {len(doc_ids)} deletions in {collection}, '
                f'{len(remaining)} remaining after {timeout}s'
            )

        unsub()
        self._listeners.remove(unsub)
        return True

    # -- Stress test command/control -----------------------------------------

    def send_command(
        self, cmd: str, args: dict, request_id: str | None = None
    ) -> str:
        """
        Write a command to __stress_control for the TD agent to pick up.
        Returns the request_id.
        """
        if request_id is None:
            request_id = f'req_{uuid.uuid4().hex[:12]}'
        self._db.collection('__stress_control').document(request_id).set({
            'cmd': cmd,
            'args': args,
            'request_id': request_id,
            'status': 'pending',
        })
        return request_id

    def wait_for_result(self, request_id: str, timeout: float = 60.0) -> dict:
        """
        Poll __stress_results/{request_id} until it appears.
        Returns the result document dict.
        """
        deadline = time.monotonic() + timeout
        interval = 0.2
        while time.monotonic() < deadline:
            doc = self._db.collection('__stress_results').document(request_id).get()
            if doc.exists:
                data = doc.to_dict()
                if data.get('status') in ('done', 'error'):
                    return data
            time.sleep(interval)
            interval = min(interval * 1.2, 0.5)
        raise TimeoutError(
            f'No result for request {request_id} after {timeout}s'
        )

    def send_and_wait(
        self, cmd: str, args: dict, timeout: float = 60.0
    ) -> dict:
        """Send a command and wait for its result. Convenience wrapper."""
        request_id = self.send_command(cmd, args)
        return self.wait_for_result(request_id, timeout=timeout)

    def cleanup_control_collections(self) -> None:
        """Delete all docs in __stress_control and __stress_results."""
        self.delete_collection('__stress_control')
        self.delete_collection('__stress_results')
