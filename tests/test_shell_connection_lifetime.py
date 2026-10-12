"""Retired transport events must not close or decorate a replacement shell."""

from types import SimpleNamespace

import pytest
from pyzo.qt import QtCore, QtWidgets


class ShellProbe(QtCore.QObject):
    def __init__(self, **attributes):
        super().__init__()
        self.__dict__.update(attributes)


@pytest.mark.parametrize(
    "state", ["current", "retired", "preparing", "current-no-context"]
)
def test_close_callback_uses_current_connection_identity(state, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import pyzo
    from pyzo.util._locale import translate

    monkeypatch.setattr(pyzo, "translate", translate, raising=False)
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(
            pyzo,
            "config",
            SimpleNamespace(advanced=SimpleNamespace(shellMaxLines=1000)),
            raising=False,
        )
    from pyzo.core.shell import PythonShell

    calls = []
    current, retired = object(), object()
    cursor = SimpleNamespace(
        MoveOperation=SimpleNamespace(End="end"),
        movePosition=lambda *args: calls.append("cursor"),
    )
    shell = ShellProbe(
        _brokerConnection=None if state == "preparing" else current,
        _context=None
        if state == "current-no-context"
        else SimpleNamespace(close=lambda: calls.append("close")),
        _cursor1=cursor,
        _cursor2=cursor,
        write=lambda text: calls.append(text),
        setReadOnly=lambda value: calls.append(("readonly", value)),
        textCursor=lambda: cursor,
        setTextCursor=lambda value: calls.append("setcursor"),
        ensureCursorVisible=lambda: calls.append("visible"),
    )
    callback = current if state in ("current", "current-no-context") else retired
    PythonShell._onConnectionClose(shell, callback, "gone")
    if state in ("retired", "preparing"):
        assert calls == []
    else:
        assert calls.count("close") == (state == "current")
        assert "Lost connection with broker:\n" in calls
        assert ("readonly", True) in calls
        assert calls[-1] == "visible"
    app.sendPostedEvents()


def test_connection_is_retired_before_replacement_preparation_can_fail(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import pyzo
    from pyzo.util._locale import translate

    monkeypatch.setattr(pyzo, "translate", translate, raising=False)
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(
            pyzo,
            "config",
            SimpleNamespace(advanced=SimpleNamespace(shellMaxLines=1000)),
            raising=False,
        )
    from pyzo.core.shell import PythonShell
    import yoton

    calls, old = [], object()
    shell = SimpleNamespace(
        _brokerConnection=old,
        _context=SimpleNamespace(close=lambda: calls.append("closed")),
    )

    def fail_preparation():
        assert shell._brokerConnection is None
        PythonShell._onConnectionClose(shell, old, "old callback during preparation")
        raise RuntimeError("preparation failed")

    monkeypatch.setattr(yoton, "Context", fail_preparation)
    with pytest.raises(RuntimeError, match="preparation failed"):
        PythonShell.connectToKernel(shell, object())
    assert calls == []
    app.sendPostedEvents()
