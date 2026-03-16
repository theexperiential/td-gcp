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


# -- Component registry --------------------------------------------------------
# Each entry: (name, sensitive_params, child_comps_with_build)
# sensitive_params: list of par names to stash/clear before save
# child_comps_with_build: list of relative paths to child COMPs that have Build pars

_COMPONENTS = [
	{
		'name': 'firestore',
		'sensitive_pars': ['Privatekey', 'Collections'],
		'child_comps': ['./collections'],
	},
	{
		'name': 'storage',
		'sensitive_pars': ['Privatekey'],
		'child_comps': [],
	},
]


def _embody():
	return me.parent().op('Embody')


def onProjectPreSave():
	now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
	build_str = str(project.saveBuild)

	for comp_info in _COMPONENTS:
		comp = me.parent().op(comp_info['name'])
		if not comp:
			continue

		# --- 1. Bump build ---
		old_build = int(comp.par.Build.eval())
		new_build = old_build + 1

		comp.par.Build.val = new_build
		comp.par.Date.val = now
		comp.par.Touchbuild.val = build_str

		# Update child COMPs build if they exist
		for child_path in comp_info['child_comps']:
			child = comp.op(child_path)
			if child:
				child.par.Build.val = new_build
				child.par.Date.val = now
				child.par.Touchbuild.val = build_str

		# --- 2. Stash and clear sensitive params ---
		stash = {}
		for par_name in comp_info['sensitive_pars']:
			par = getattr(comp.par, par_name, None)
			if par:
				stash[par_name] = par.eval()
				par.val = ''
		comp.store('_release_stash', stash)

	return


def onProjectPostSave():
	# Defer export to after ALL post-save callbacks (including Embody's
	# TDN reconstruction) have completed. 10 frames is plenty of margin.
	run(f"op('{me}').module._deferred_export()", delayFrames=10)
	return


def _deferred_export():
	embody = _embody()
	release_dir = Path(project.folder).parent / 'release'
	release_dir.mkdir(exist_ok=True)

	for comp_info in _COMPONENTS:
		comp = me.parent().op(comp_info['name'])
		if not comp:
			continue

		# --- 3. Export portable .tox ---
		new_build = int(comp.par.Build.eval())

		for old_tox in release_dir.glob(f'{comp.name}-b*.tox'):
			try:
				old_tox.unlink()
			except Exception:
				pass

		save_path = release_dir / f'{comp.name}-b{new_build}.tox'
		embody.ext.Embody.ExportPortableTox(
			target=comp,
			save_path=str(save_path),
		)

		# --- 4. Restore sensitive params ---
		stash = comp.fetch('_release_stash', {}, search=False)
		if stash:
			for par_name, val in stash.items():
				par = getattr(comp.par, par_name, None)
				if par:
					par.val = val
			comp.unstore('_release_stash')

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
