import threading
import time


class ConnectionExt:
	"""
	Circuit breaker + exponential backoff for Firestore connection management.
	Thread-safe via threading.Lock. No asyncio.
	"""

	CLOSED = 'closed'
	OPEN = 'open'
	HALF_OPEN = 'half_open'

	def __init__(self, ownerComp):
		self.my = ownerComp
		self._state = self.CLOSED
		self._failure_count = 0
		self._open_since = None
		self._lock = threading.Lock()

	def CanAttempt(self):
		"""Return True if a connection attempt is allowed."""
		with self._lock:
			if self._state == self.CLOSED:
				return True
			if self._state == self.OPEN:
				timeout = self.my.par.Circuittimeout.eval()
				if time.monotonic() - self._open_since >= timeout:
					self._state = self.HALF_OPEN
					return True
				return False
			# HALF_OPEN: allow one probe
			return True

	def RecordSuccess(self):
		"""Reset circuit to CLOSED on successful connection."""
		with self._lock:
			self._state = self.CLOSED
			self._failure_count = 0
			self._open_since = None

	def RecordFailure(self):
		"""Record a failure; open circuit if threshold exceeded."""
		with self._lock:
			if self._state == self.HALF_OPEN:
				# Probe failed -- back to OPEN
				self._state = self.OPEN
				self._open_since = time.monotonic()
				return
			self._failure_count += 1
			threshold = self.my.par.Circuitfailurethreshold.eval()
			if self._failure_count >= threshold:
				self._state = self.OPEN
				self._open_since = time.monotonic()

	def GetBackoffSeconds(self):
		"""Return current backoff delay in seconds (exponential, capped)."""
		with self._lock:
			base = self.my.par.Backoffbase.eval()
			max_backoff = self.my.par.Backoffmax.eval()
			delay = base ** min(self._failure_count, 10)
			return min(delay, max_backoff)

	def GetState(self):
		with self._lock:
			return self._state

	def Reset(self):
		with self._lock:
			self._state = self.CLOSED
			self._failure_count = 0
			self._open_since = None
