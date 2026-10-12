"""DOS aliases must not stall the real smoke's cwd-release phase."""
import ctypes
import os
import tempfile
from types import SimpleNamespace as NS
import pytest
import pyzo
from pyzo.util._locale import translate
pyzo.translate = translate
from pyzo.core.main import MainWindow

@pytest.mark.skipif(os.name != "nt", reason="DOS short-path reproduction")
def test_short_cwd_release_completes_smoke_and_cleans_retained_directory(monkeypatch):
    long = tempfile.gettempdir()
    buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetShortPathNameW(long, buffer, len(buffer))
    short = buffer.value
    if short == long:
        # The repository itself has a real DOS alias on Windows installations
        # where the user's temporary directory needs no abbreviation.
        long = str(__import__("pathlib").Path(__file__).resolve().parents[1])
        assert ctypes.windll.kernel32.GetShortPathNameW(long, buffer, len(buffer))
        short = buffer.value
        monkeypatch.setattr(tempfile, "gettempdir", lambda: long)
    if short == long:
        pytest.skip("this volume exposes no distinct DOS alias")
    assert os.path.samefile(long, short)
    calls=[];first=object()
    second=NS(get_kernel_cd=lambda:short,closeShell=lambda:calls.append("close") or 1)
    state=dict(phase="wait-affinity-release",shell=first,first_shell=first,
               second_shell=second,cwd_directory=NS(cleanup=lambda:calls.append("cleanup")))
    host=NS(_peerRunDebugSmoke=state,_peerShellCloseChooser=object())
    monkeypatch.setattr(pyzo,"shells",NS(getExecutionShell=lambda:first),raising=False)
    assert MainWindow._testPeerRunAndLiveDebugTick(host) is True
    assert state["phase"] == "complete" and calls == ["close","cleanup"]
