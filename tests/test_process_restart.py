"""Restart argument integrity, platform handoff and launch failure behavior."""

from types import SimpleNamespace as NS

import pytest
import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core import main


@pytest.mark.parametrize("frozen", [False, True])
def test_windows_restart_preserves_arguments_and_exits_only_after_launch(monkeypatch, frozen):
    import subprocess
    arguments = [r"C:\Program Files\Pyzo\pyzo.exe" if frozen else r"C:\Work space\launcher.py",
                 "argument with spaces", 'literal"quote', ""]
    executable = r"C:\Program Files\Pyzo\python.exe"
    events = []
    successor = NS(pid=123)
    monkeypatch.setattr(main, "sys", NS(platform="win32", argv=arguments, executable=executable,
        __stdin__="input", __stdout__="output", __stderr__="error"))
    monkeypatch.setattr(main.paths, "is_frozen", lambda: frozen)
    monkeypatch.setattr(main, "QtWidgets", NS(qApp=NS(exit=lambda code: events.append(("exit", code)))))
    def launch(args, **kwargs):
        events.append(("launch", args, kwargs))
        return successor
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    host = NS()
    main.MainWindow._replacePyzoProcess(host)
    expected = [executable, *arguments[1:]] if frozen else [executable, *arguments]
    assert events == [("launch", expected, {"creationflags": 0x08000000, "stdin": "input", "stdout": "output", "stderr": "error"}), ("exit", 0)]
    assert host._restartProcess is successor
    assert arguments[0] != executable


def test_windows_restart_launch_failure_does_not_report_clean_exit(monkeypatch):
    import subprocess
    exits = []
    monkeypatch.setattr(main, "sys", NS(platform="win32", argv=["launch.py"], executable="python.exe",
        __stdin__=None, __stdout__=None, __stderr__=None))
    monkeypatch.setattr(main.paths, "is_frozen", lambda: False)
    monkeypatch.setattr(main, "QtWidgets", NS(qApp=NS(exit=exits.append)))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    def fail(*args, **kwargs):
        raise OSError("successor launch failed")
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError, match="successor launch failed"):
        main.MainWindow._replacePyzoProcess(NS())
    assert exits == []


def test_posix_restart_keeps_exec_replacement(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "sys", NS(platform="linux", argv=["launch.py", "two words"], executable="/usr/bin/python"))
    monkeypatch.setattr(main.paths, "is_frozen", lambda: False)
    monkeypatch.setattr(main.os, "execv", lambda *args: calls.append(args))
    main.MainWindow._replacePyzoProcess(NS())
    assert calls == [("/usr/bin/python", ["python", "launch.py", "two words"])]
