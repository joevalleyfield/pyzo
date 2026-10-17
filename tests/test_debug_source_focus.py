from types import SimpleNamespace as NS

import pytest
import pyzo
from pyzo.qt import QtGui, QtWidgets
from pyzo.util._locale import translate
from shiboken6 import delete

pyzo.translate = translate
_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from pyzo.core import focus


@pytest.fixture
def debug_stack(monkeypatch):
    if not hasattr(pyzo, 'config'):
        monkeypatch.setattr(pyzo, 'config', NS(advanced=NS(shellMaxLines=10000)), raising=False)
    from pyzo.core.shellStack import DebugStack
    return DebugStack


def test_debug_source_focus_follows_final_line_selection(monkeypatch, debug_stack):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class Editor(QtWidgets.QPlainTextEdit):
        def gotoLine(self, line):
            cursor = self.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.NextBlock,
                                QtGui.QTextCursor.MoveMode.MoveAnchor, line - 1)
            self.setTextCursor(cursor)

    editor = Editor()
    editor.setPlainText('first\nselected source\nlast\n')
    calls = []
    debugging = NS(navigate=lambda *args: editor)
    monkeypatch.setattr(pyzo, 'application', NS(workspace_debugging=debugging), raising=False)
    monkeypatch.setattr(focus, 'focus_widget',
                        lambda target: calls.append((target, target.textCursor().selectedText())))
    source = NS(_contextShell=lambda: object(), _sessionToken='session')
    try:
        assert debug_stack.debugFocus(source, 'File "example.py", line 2, in function') is None
        assert calls == [(editor, 'selected source')]
    finally:
        delete(editor)


def test_stale_debug_navigation_never_requests_focus(monkeypatch, debug_stack):
    debugging = NS(navigate=lambda *args: None)
    monkeypatch.setattr(pyzo, 'application', NS(workspace_debugging=debugging), raising=False)
    calls = []
    monkeypatch.setattr(focus, 'focus_widget', lambda target: calls.append(target))
    source = NS(_contextShell=lambda: NS(setFocus=lambda: None), _sessionToken='retired')
    assert debug_stack.debugFocus(source, 'File "example.py", line 2, in function')
    assert calls == []
