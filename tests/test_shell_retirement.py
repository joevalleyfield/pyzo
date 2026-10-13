"""Committed/native shell retirement rejects late broker events."""

from types import SimpleNamespace as NS

import pytest
import pyzo
from pyzo.qt import QtCore, QtWidgets
from shiboken6 import delete, isValid

_app = None


@pytest.fixture
def shell_world(monkeypatch):
    global _app
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(pyzo, "translate", lambda _, text, *args: text, raising=False)
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(pyzo, "config", NS(advanced=NS(shellMaxLines=1000)), raising=False)
    from pyzo.core.shell import PythonShell
    import yoton

    class Shell(QtWidgets.QPlainTextEdit):
        _onConnectionClose = PythonShell._onConnectionClose
        closeShellCommitted = PythonShell.closeShellCommitted
        def write(self, text):
            self.insertPlainText(text)

    shell = Shell()
    calls = []
    shell._cursor1 = shell.textCursor()
    shell._cursor2 = shell.textCursor()
    shell._timer = QtCore.QTimer(shell)
    shell._timer.timeout.connect(lambda: None)
    shell.customContextMenuRequested.connect(lambda point: None)
    shell.sendBreakPoints = lambda: None
    shell.terminate = lambda: calls.append("terminate")
    shell._context = NS(connection_count=1, flush=lambda: calls.append("flush"),
                        close=lambda: calls.append("context-close"))
    broker = shell._brokerConnection = NS(closed=yoton.Signal())
    broker.closed.bind(shell._onConnectionClose)
    monkeypatch.setattr(pyzo, "editors", NS(breakPointsChanged=NS(disconnect=lambda handler: None)), raising=False)
    monkeypatch.setattr(pyzo, "shells", NS(removeShell=lambda value: calls.append("remove")), raising=False)
    yield shell, broker, calls, yoton
    yoton.app.process_events()
    if isValid(shell):
        delete(shell)


@pytest.mark.parametrize("native_deleted", [False, True])
def test_event_queued_before_committed_close_is_rejected(shell_world, capsys, native_deleted):
    shell, broker, calls, yoton = shell_world
    broker.closed.emit(broker, "queued before committed close")
    assert shell.closeShellCommitted() == 1
    before = list(calls)
    if native_deleted:
        delete(shell)
    yoton.app.process_events()
    assert calls == before
    assert not broker.closed._handlers
    assert "Exception while handling event" not in capsys.readouterr().out
    if not native_deleted:
        assert shell.toPlainText() == ""


def test_native_deletion_without_explicit_close_rejects_queued_event(shell_world, capsys):
    shell, broker, calls, yoton = shell_world
    broker.closed.emit(broker, "native owner destruction")
    delete(shell)
    yoton.app.process_events()
    assert calls == []
    assert "Exception while handling event" not in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["terminate", "flush", "context-close"])
def test_committed_close_retires_callback_before_collaborator_failure(shell_world, capsys, stage):
    shell, broker, calls, yoton = shell_world
    def fail():
        broker.closed.emit(broker, "during failing committed cleanup")
        yoton.app.process_events()
        raise ValueError("cleanup failure")
    if stage == "terminate":
        shell.terminate = fail
    elif stage == "flush":
        shell._context.flush = fail
    else:
        shell._context.close = fail
    with pytest.raises(ValueError, match="cleanup failure"):
        shell.closeShellCommitted()
    assert shell._brokerConnection is None
    assert not broker.closed._handlers
    assert shell.toPlainText() == ""
    assert "Exception while handling event" not in capsys.readouterr().out


def test_live_unexpected_disconnect_still_reports_error(shell_world):
    shell, broker, calls, yoton = shell_world
    broker.closed.emit(broker, "unexpected disconnect")
    yoton.app.process_events()
    assert calls == ["context-close"]
    assert "Lost connection with broker:" in shell.toPlainText()
    assert shell.isReadOnly()


def test_live_context_failure_is_not_hidden(shell_world):
    shell, broker, calls, yoton = shell_world
    def fail():
        raise ValueError("live context failure")
    shell._context.close = fail
    with pytest.raises(ValueError, match="live context failure"):
        shell._onConnectionClose(broker, "unexpected disconnect")
    assert shell.toPlainText() == ""


def test_no_broker_during_preparation_is_not_a_disconnect(shell_world):
    shell, broker, calls, yoton = shell_world
    shell._brokerConnection = None
    shell._onConnectionClose(None, "no connection")
    assert calls == [] and shell.toPlainText() == ""
