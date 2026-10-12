"""Startup must leave Qt able to choose per-monitor Windows DPI awareness."""
import os
import subprocess
import sys
import pytest

@pytest.mark.skipif(os.name != "nt", reason="native Windows DPI awareness")
def test_startup_allows_qt_per_monitor_awareness(tmp_path):
    code = r"""
import ctypes,sys,types
from pyzo.util import paths
paths.prepare_appdata_appconfig_dirs=lambda:(sys.argv[1],sys.argv[1])
commandline=types.ModuleType('pyzo.core.commandline')
commandline.is_our_server_running=lambda:False
commandline.handle_cmd_args=lambda:None
sys.modules['pyzo.core.commandline']=commandline
import pyzo._start
from pyzo.qt import QtWidgets
app=QtWidgets.QApplication([])
awareness=ctypes.c_int()
assert ctypes.windll.shcore.GetProcessDpiAwareness(None,ctypes.byref(awareness)) == 0
assert awareness.value == 2, awareness.value
print('QT PER-MONITOR DPI VERIFIED')
"""
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],
        env=dict(os.environ,QT_QPA_PLATFORM='windows'),text=True,capture_output=True,timeout=15)
    assert result.returncode == 0,result.stdout+result.stderr
    assert 'QT PER-MONITOR DPI VERIFIED' in result.stdout
