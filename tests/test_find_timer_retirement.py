"""Retire find callbacks before native deletion is deferred."""
import os
import subprocess
import sys


def test_runtime_disposal_retires_find_timers_and_rejects_queued_callback():
    code = r"""
from types import SimpleNamespace as NS
import pyzo
from pyzo.qt import QtWidgets
from pyzo.util._locale import translate
pyzo.translate = translate
pyzo.config = NS(settings=NS(defaultLineEndings="LF", defaultEncoding="utf-8"),view=NS(showStatusbar=False))
app = QtWidgets.QApplication([])
from pyzo.core.editorTabs import FindReplaceWidget
from pyzo.core.workspaceRuntime import WorkspaceRuntime
import pyzo.core.editorTabs as tabs
calls = []
find = NS(_timerAutoHide=NS(stop=lambda: calls.append("auto")),
          _timerBeginEnd=NS(stop=lambda: calls.append("appearance")))
find.retireTimers = lambda: FindReplaceWidget.retireTimers(find)
class Host:
    _findReplace = find
runtime = WorkspaceRuntime(identity="retiring", backend=NS(close=lambda: calls.append("close")), model=None)
runtime.editor_hosts[Host()] = object()
runtime.dispose()
assert calls == ["auto", "appearance", "close"]
def disposed_config(_):
    raise AssertionError("queued callback accessed disposed runtime")
tabs.config_for = disposed_config
FindReplaceWidget.autoHideTimerCallback(find)
runtime.dispose()
assert calls == ["auto", "appearance", "close"]
print("FIND TIMER RETIREMENT VERIFIED")
"""
    result = subprocess.run([sys.executable, "-c", code],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FIND TIMER RETIREMENT VERIFIED" in result.stdout
