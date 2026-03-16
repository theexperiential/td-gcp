import sys
import os
import shutil
import subprocess
import threading
from pathlib import Path


class BootstrapExt:
	"""
	Handles uv-based venv creation and package installation for Firebase Storage.
	No TD object access -- project_folder must be passed from main thread.
	"""

	PACKAGES = [
		'firebase-admin',
		'google-cloud-storage',
	]

	def __init__(self, ownerComp):
		self.my = ownerComp
		self._ready = False
		self._error = None
		self._lock = threading.Lock()

	def IsReady(self):
		return self._ready

	def GetError(self):
		return self._error

	def NeedsBootstrap(self, project_folder=''):
		"""Check if bootstrap is needed. Safe to call from main thread."""
		try:
			import firebase_admin  # noqa
			import google.cloud.storage  # noqa
			return False
		except ImportError:
			pass

		# Try injecting existing venv before giving up
		if project_folder:
			venv_path = Path(project_folder) / 'venv' / 'gcp'
			site_packages = self._get_site_packages(venv_path)
			if site_packages and site_packages.exists():
				self._inject_path(str(site_packages))
				try:
					import firebase_admin  # noqa
					import google.cloud.storage  # noqa
					return False
				except ImportError:
					pass

		return True

	def ensure_venv(self, project_folder, python_version=''):
		"""
		Synchronous bootstrap -- call from background thread.
		project_folder: string captured from project.folder on main thread.
		python_version: e.g. '3.11' -- passed to uv venv --python to match TD's interpreter.
		Returns True on success, False on failure.
		"""
		with self._lock:
			# Fast path: already importable
			try:
				import firebase_admin  # noqa
				import google.cloud.storage  # noqa
				self._ready = True
				return True
			except ImportError:
				pass

			venv_path = Path(project_folder) / 'venv' / 'gcp'
			site_packages = self._get_site_packages(venv_path)

			# Inject existing venv if present
			if site_packages and site_packages.exists():
				self._inject_path(str(site_packages))
				try:
					import firebase_admin  # noqa
					import google.cloud.storage  # noqa
					self._ready = True
					return True
				except ImportError:
					pass  # venv exists but packages missing -- re-install

			# Find uv -- auto-install if missing
			uv = self._find_uv(project_folder)
			if not uv:
				uv = self._install_uv()
			if not uv:
				self._error = (
					'uv installation failed. '
					'Install manually from: https://docs.astral.sh/uv/getting-started/installation/'
				)
				return False

			# Create venv pinned to TD's Python version
			venv_cmd = [uv, 'venv', str(venv_path)]
			if python_version:
				venv_cmd += ['--python', python_version]
			try:
				subprocess.run(
					venv_cmd,
					check=True, capture_output=True, text=True,
				)
			except subprocess.CalledProcessError as e:
				self._error = f'uv venv failed: {e.stderr.strip()}'
				return False

			# Install packages
			venv_python = self._get_venv_python(venv_path)
			try:
				subprocess.run(
					[uv, 'pip', 'install', '--python', str(venv_python)] + self.PACKAGES,
					check=True, capture_output=True, text=True,
				)
			except subprocess.CalledProcessError as e:
				self._error = f'uv pip install failed: {e.stderr.strip()}'
				return False

			# Inject new venv
			site_packages = self._get_site_packages(venv_path)
			if site_packages:
				self._inject_path(str(site_packages))

			# Confirm import
			try:
				import firebase_admin  # noqa
				import google.cloud.storage  # noqa
				self._ready = True
				return True
			except ImportError as e:
				self._error = f'Import failed after install: {e}'
				return False

	def _get_venv_python(self, venv_path):
		if sys.platform == 'win32':
			return venv_path / 'Scripts' / 'python.exe'
		return venv_path / 'bin' / 'python'

	def _get_site_packages(self, venv_path):
		"""Find site-packages directory inside the venv."""
		if sys.platform == 'win32':
			candidate = venv_path / 'Lib' / 'site-packages'
			return candidate if candidate.exists() else None
		lib_path = venv_path / 'lib'
		if lib_path.exists():
			for d in lib_path.iterdir():
				if d.name.startswith('python'):
					sp = d / 'site-packages'
					if sp.exists():
						return sp
		return None

	def _inject_path(self, path_str):
		if path_str not in sys.path:
			sys.path.insert(0, path_str)

	def _install_uv(self):
		"""
		Download and install uv using the official installer.
		Returns the path to the uv binary on success, or None on failure.
		"""
		try:
			if sys.platform == 'win32':
				subprocess.run(
					[
						'powershell', '-ExecutionPolicy', 'ByPass', '-c',
						'irm https://astral.sh/uv/install.ps1 | iex',
					],
					check=True, capture_output=True, text=True,
					timeout=120,
				)
				# Default Windows install location
				candidate = os.path.join(
					os.environ.get('LOCALAPPDATA', ''), 'uv', 'bin', 'uv.exe'
				)
			else:
				subprocess.run(
					['sh', '-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh'],
					check=True, capture_output=True, text=True,
					timeout=120,
				)
				# Default unix install location
				candidate = os.path.expanduser('~/.local/bin/uv')

			if os.path.isfile(candidate):
				return candidate

			# Fallback: check PATH in case installer put it somewhere else
			from_path = shutil.which('uv')
			return from_path

		except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
			return None

	def _find_uv(self, project_folder):
		"""Locate uv binary. Returns path string or None."""
		candidates = []

		# Bundled with project
		pf = Path(project_folder)
		candidates.append(str(pf / 'tools' / 'uv'))
		candidates.append(str(pf / 'tools' / 'uv.exe'))

		# PATH
		from_path = shutil.which('uv')
		if from_path:
			candidates.append(from_path)

		# Platform defaults
		if sys.platform == 'win32':
			appdata = os.environ.get('LOCALAPPDATA', '')
			candidates.append(os.path.join(appdata, 'uv', 'bin', 'uv.exe'))
		else:
			candidates += [
				os.path.expanduser('~/.local/bin/uv'),
				'/opt/homebrew/bin/uv',
				'/usr/local/bin/uv',
			]

		return next((c for c in candidates if os.path.isfile(c)), None)
