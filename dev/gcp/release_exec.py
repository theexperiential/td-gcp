# Release Execute DAT -- pre/post save hooks for portable .tox export
#
# Timing:
#   Pre-save:  This DAT bumps build and clears sensitive params.
#              Embody exports TDN then strips children. .toe saves.
#   Post-save: Embody reimports TDN (children restored). This DAT
#              defers the .tox export by 10 frames to guarantee all
#              post-save callbacks have finished. The deferred function
#              exports the portable .tox then restores sensitive params.
#
# TD does NOT guarantee execute DAT callback ordering, so we cannot
# assume Embody's post-save runs before ours. The delayFrames approach
# sidesteps the ordering problem entirely.

from datetime import datetime, timezone
from pathlib import Path


def _firestore():
	return me.parent().op('firestore')


def _embody():
	return me.parent().op('Embody')


def onProjectPreSave():
	fs = _firestore()

	# --- 1. Bump build ---
	old_build = int(fs.par.Build.eval())
	new_build = old_build + 1
	now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
	build_str = str(project.saveBuild)

	fs.par.Build.val = new_build
	fs.par.Date.val = now
	fs.par.Touchbuild.val = build_str

	# Update collections COMP build if it still exists
	coll = fs.op('./collections')
	if coll:
		coll.par.Build.val = new_build
		coll.par.Date.val = now
		coll.par.Touchbuild.val = build_str

	# --- 2. Stash and clear sensitive params ---
	# Cleared before the .toe is written so they don't leak into the file.
	# They stay cleared until the deferred export completes in post-save.
	stash = {
		'Privatekey': fs.par.Privatekey.eval(),
		'Collections': fs.par.Collections.eval(),
	}
	fs.store('_release_stash', stash)
	fs.par.Privatekey.val = ''
	fs.par.Collections.val = ''

	return


def onProjectPostSave():
	# Defer export to after ALL post-save callbacks (including Embody's
	# TDN reconstruction) have completed. 10 frames is plenty of margin.
	run(f"op('{me}').module._deferred_export()", delayFrames=10)
	return


def _deferred_export():
	fs = _firestore()
	embody = _embody()

	# --- 3. Export portable .tox ---
	# By now Embody has fully reimported TDN children. Sensitive params
	# are still cleared from pre-save, so the .tox has no secrets.
	new_build = int(fs.par.Build.eval())
	release_dir = Path(project.folder).parent / 'release'
	release_dir.mkdir(exist_ok=True)

	for old_tox in release_dir.glob(f'{fs.name}-b*.tox'):
		try:
			old_tox.unlink()
		except Exception:
			pass

	save_path = release_dir / f'{fs.name}-b{new_build}.tox'
	embody.ext.Embody.ExportPortableTox(
		target=fs,
		save_path=str(save_path),
	)

	# --- 4. Restore sensitive params ---
	stash = fs.fetch('_release_stash', {}, search=False)
	if stash:
		fs.par.Privatekey.val = stash.get('Privatekey', '')
		fs.par.Collections.val = stash.get('Collections', '')
		fs.unstore('_release_stash')

	return


# --- Unused callbacks (required stubs) ---

def onStart():
	return

def onCreate():
	return

def onExit():
	return

def onFrameStart(frame):
	return

def onFrameEnd(frame):
	return

def onPlayStateChange(state):
	return

def onDeviceChange():
	return
