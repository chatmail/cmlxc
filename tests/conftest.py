"""Pytest hooks for fullrun test formatting and defaults.


This conftest.py implementation requires somewhat3 intricate pytest hook ordering knowledge.
But it only modifies the output -- if you remove the "conftest.py" file the test should still run,
just provide less or not enough output.

"""

import sys
from contextlib import contextmanager

import pytest
from _pytest.terminal import TerminalReporter


class FullrunReporter(TerminalReporter):
    """Terminal reporter with header/footer separators for fullrun tests."""

    def pytest_runtest_logstart(self, nodeid, location):
        if "fullrun" in nodeid:
            w = self._tw.fullwidth
            name = nodeid.split("::")[-1]
            msg = f" {name} "
            sep = "=" * w
            self._tw.line(sep)
            self._tw.line(f"{msg:{'='}^{w}}")
            self._tw.line(sep)
        else:
            super().pytest_runtest_logstart(nodeid, location)

    def pytest_runtest_logreport(self, report):
        if "fullrun" not in report.nodeid:
            return super().pytest_runtest_logreport(report)

        # Track result for final summary
        res = self.config.hook.pytest_report_teststatus(
            report=report, config=self.config
        )
        category = res[0]
        self.stats.setdefault(category, []).append(report)
        self._progress_nodeids_reported.add(report.nodeid)

        # Footer after the main phase
        if report.when == "call" or (report.when == "setup" and report.skipped):
            w = self._tw.fullwidth
            name = report.nodeid.split("::")[-1]
            msg = f"'{name}' {report.outcome}"
            self._tw.line(f"{msg:>{w}}")


def _is_fullrun(config):
    return any("fullrun" in str(a) for a in config.args)


def pytest_configure(config):
    if _is_fullrun(config):
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
        config.option.exitfirst = True
        config.option.verbose = max(config.option.verbose, 1)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtestloop(session):
    """Swap in our custom reporter right before tests run.

    By this point ``pytest_sessionstart`` has already set
    ``_session_start`` on the standard reporter, so we can
    safely copy it to the replacement.
    """
    config = session.config
    if _is_fullrun(config):
        standard = config.pluginmanager.getplugin("terminalreporter")
        if standard:
            custom = FullrunReporter(config, sys.stdout)
            custom._session_start = standard._session_start
            config.pluginmanager.unregister(standard)
            config.pluginmanager.register(custom, "terminalreporter")
    yield


@contextmanager
def _suspend_capture(item):
    is_fullrun = "fullrun" in item.nodeid
    if is_fullrun:
        capman = item.config.pluginmanager.getplugin("capturemanager")
        if capman:
            capman.suspend_global_capture(in_=True)
    yield
    if is_fullrun and capman:
        capman.resume_global_capture()


@pytest.hookimpl(trylast=True, hookwrapper=True)
def pytest_runtest_setup(item):
    with _suspend_capture(item):
        yield


@pytest.hookimpl(trylast=True, hookwrapper=True)
def pytest_runtest_call(item):
    with _suspend_capture(item):
        yield
