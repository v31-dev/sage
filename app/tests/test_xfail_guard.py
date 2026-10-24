import os
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")

XFAIL = {
    "a marker on a failing test": "@pytest.mark.xfail(reason='bug')\ndef test_it():\n  assert False",
    "a marker on a passing test": "@pytest.mark.xfail(reason='bug')\ndef test_it():\n  assert True",
    "a strict marker": "@pytest.mark.xfail(strict=True, reason='bug')\ndef test_it():\n  assert False",
    "a call": "def test_it():\n  pytest.xfail('bug')",
}


@pytest.fixture
def run(pytester, monkeypatch):
  """Runs a test file in a nested pytest session that has this suite's conftest."""
  monkeypatch.setattr(os, "environ", os.environ.copy())
  pytester.makeconftest(CONFTEST.read_text())

  def run(source):
    pytester.makepyfile(f"import pytest\n\n\n{source}\n")
    return pytester.runpytest_inprocess("-p", "no:cacheprovider", "-p", "no:asyncio")

  return run


@pytest.mark.parametrize("source", XFAIL.values(), ids=XFAIL.keys())
def test_an_expected_failure_fails_the_run(run, source):
  result = run(source)

  result.assert_outcomes(failed=1)
  assert result.ret == pytest.ExitCode.TESTS_FAILED
  result.stdout.fnmatch_lines(["*xfail is not allowed, fix the bug instead (bug)*"])


def test_a_skip_for_something_missing_is_still_allowed(run):
  result = run("def test_it():\n  pytest.skip('openssl is not installed')")

  result.assert_outcomes(skipped=1)
  assert result.ret == pytest.ExitCode.OK
