"""
One-liner stress test runner.

Usage from TD textport:
    op('/gcp/unit_tests/run_stress_tests').run()

Or from anywhere:
    exec(op('/gcp/unit_tests/run_stress_tests').text)

What it does:
  1. Starts the stress test agent (if not already running)
  2. Runs the pytest stress suite via subprocess
  3. Stops the agent and prints results
"""

import subprocess
import sys
import os


def run_stress_tests(scale='all', verbose=True):
	"""
	Run the full stress test suite.

	Args:
		scale: Test filter -- 'all', 'inbound', 'outbound', 'bidir',
			   'health', or a pytest -k expression
		verbose: Show individual test results
	"""
	# 1. Start agent
	agent_dat = op('./stress_test_agent')
	if not agent_dat:
		debug('ERROR: stress_test_agent DAT not found in this network')
		return False

	agent_mod = mod(agent_dat)
	agent = agent_dat.fetch('_stress_agent', search=False)
	if not agent:
		agent = agent_mod.StressTestAgent(me.parent())
		agent_dat.store('_stress_agent', agent)

	fs = op('../firestore')
	if not fs or not fs.ext.FirestoreExt._db:
		debug('ERROR: Firestore COMP not connected - connect first')
		return False

	agent.Start()
	debug('Stress test agent started')

	# 2. Build pytest command
	project_root = project.folder
	test_dir = os.path.join(project_root, 'stress_tests')

	cmd = [sys.executable, '-m', 'pytest', test_dir, '--tb=short', '-q']

	if verbose:
		cmd.append('-v')

	# Scale filter
	filters = {
		'inbound': 'test_inbound',
		'outbound': 'test_outbound',
		'bidir': 'test_bidirectional',
		'health': 'test_frame',
	}
	if scale in filters:
		cmd.extend(['-k', filters[scale]])
	elif scale != 'all':
		cmd.extend(['-k', scale])

	cmd.extend(['--timeout=120', '--no-header'])

	debug(f'Running: {" ".join(cmd)}')
	debug('=' * 60)

	# 3. Run tests
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			cwd=project_root,
			timeout=600,  # 10 min max for full suite
		)

		if result.stdout:
			for line in result.stdout.strip().split('\n'):
				debug(line)
		if result.stderr:
			debug('--- stderr ---')
			for line in result.stderr.strip().split('\n'):
				debug(line)

		debug('=' * 60)
		if result.returncode == 0:
			debug('ALL STRESS TESTS PASSED')
		else:
			debug(f'STRESS TESTS FAILED (exit code {result.returncode})')

		return result.returncode == 0

	except subprocess.TimeoutExpired:
		debug('ERROR: Stress tests timed out after 10 minutes')
		return False
	except Exception as e:
		debug(f'ERROR running stress tests: {e}')
		return False
	finally:
		# 4. Stop agent
		try:
			agent.Stop()
			debug('Stress test agent stopped')
		except Exception:
			pass


# Auto-run when executed
run_stress_tests()