"""
Test runner for the Firestore unit tests.

Usage from TD:
    op('/gcp/unit_tests/run_tests').run()

Or from a button/script:
    exec(op('/gcp/unit_tests/run_tests').text)

Runs pytest via subprocess using the project's tests/ directory.
Results are printed to the TD textport.
"""

import subprocess
import sys
import os


def run_tests(verbose=True, test_filter=None):
    """
    Run pytest on the project test suite.

    Args:
        verbose: If True, show individual test results (-v)
        test_filter: Optional filter string (e.g., 'test_burst' to run
                     only burst tests, or 'TestBurstInbound' for a class)
    """
    project_root = project.folder
    test_dir = os.path.join(project_root, 'tests')

    cmd = [sys.executable, '-m', 'pytest', test_dir, '--tb=short', '-q']

    if verbose:
        cmd.append('-v')

    if test_filter:
        cmd.extend(['-k', test_filter])

    # Add color output
    cmd.append('--no-header')

    debug(f'Running: {" ".join(cmd)}')
    debug(f'Test dir: {test_dir}')
    debug('=' * 60)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=120,
        )

        # Print stdout
        if result.stdout:
            for line in result.stdout.strip().split('\n'):
                debug(line)

        # Print stderr (warnings, errors)
        if result.stderr:
            debug('--- stderr ---')
            for line in result.stderr.strip().split('\n'):
                debug(line)

        debug('=' * 60)
        if result.returncode == 0:
            debug('ALL TESTS PASSED')
        else:
            debug(f'TESTS FAILED (exit code {result.returncode})')

        return result.returncode == 0

    except subprocess.TimeoutExpired:
        debug('ERROR: Tests timed out after 120 seconds')
        return False
    except Exception as e:
        debug(f'ERROR running tests: {e}')
        return False


# Auto-run when executed
run_tests()