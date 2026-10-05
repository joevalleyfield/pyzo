from types import SimpleNamespace

from pyzo.core.runCommands import RunCommandService


class Shells:
    def __init__(self, execution_shell):
        self.execution_shell = execution_shell

    def getExecutionShell(self):
        return self.execution_shell


class Shell:
    def __init__(self):
        self.calls = []

    def executeCode(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class Cursor:
    MoveOperation = SimpleNamespace(Start="start", End="end")
    MoveMode = SimpleNamespace(KeepAnchor="anchor")

    def movePosition(self, *args):
        pass


class Editor:
    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.run_cursor = None

    def id(self):
        return self.name

    def toPlainText(self):
        return self.text

    def textCursor(self):
        return Cursor()

    def showRunCursor(self, cursor):
        self.run_cursor = cursor


class Editors:
    def __init__(self, current=None, main=None):
        self.current = current
        self.main = main

    def getCurrentEditor(self):
        return self.current

    def getMainEditor(self):
        return self.main


def make_service(shell, editors, *, change_directory=False):
    config = SimpleNamespace(
        settings=SimpleNamespace(changeDirOnFileExec=change_directory)
    )
    return RunCommandService(
        Shells(shell),
        editors,
        config,
        [],
        lambda _domain, text: text,
    )


def test_run_file_uses_explicit_execution_shell_and_current_editor():
    shell = Shell()
    editor = Editor("current.py", "answer = 42")
    service = make_service(shell, Editors(current=editor), change_directory=True)

    service.run_file((False, False))

    assert shell.calls == [(("answer = 42", "current.py"), {"changeDir": True})]
    assert editor.run_cursor is not None


def test_editor_context_target_does_not_depend_on_current_editor():
    shell = Shell()
    selected_tab_editor = Editor("selected.py", "selected = True")
    service = make_service(shell, Editors(current=None))

    service.run_file((False, False), selected_tab_editor)

    assert shell.calls == [(("selected = True", "selected.py"), {"changeDir": False})]


def test_selection_uses_captured_local_shell_instead_of_global_target():
    from pyzo.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    local, other = Shell(), Shell()
    editor = QtWidgets.QPlainTextEdit()
    editor.setPlainText("answer = 42\nsecond = answer + 1\n")
    editor.id = lambda: "selection.py"
    editor.showRunCursor = lambda cursor: None
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    service = make_service(other, Editors(current=editor))
    receipt = SimpleNamespace(resolved=True, provenance=object())
    dispatches = []
    service._selectionExecution = SimpleNamespace(
        resolve_selection=lambda editor, **kwargs: receipt,
        dispatch=lambda provenance, send: (
            dispatches.append(provenance) or send(local) or True
        ),
    )
    try:
        service.run_selected()
        assert local.calls == [
            (("answer = 42\nsecond = answer + 1\n", "selection.py", 0), {})
        ]
        assert other.calls == []
        assert dispatches == [receipt.provenance]
    finally:
        editor.deleteLater()
        app.sendPostedEvents()


def test_selection_statement_history_and_advance_use_captured_editor():
    from pyzo.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    first, later = QtWidgets.QPlainTextEdit(), QtWidgets.QPlainTextEdit()
    first.setPlainText("answer = 42\nnext_line = 1")
    later.setPlainText("unrelated = 0\n")
    cursor = first.textCursor()
    cursor.select(cursor.SelectionType.LineUnderCursor)
    first.setTextCursor(cursor)
    local = SimpleNamespace(
        commands=[], executeCommand=lambda code: local.commands.append(code)
    )
    editors = Editors(current=first)
    service = make_service(Shell(), editors)
    receipt = SimpleNamespace(resolved=True, provenance=object())

    def dispatch(provenance, send):
        editors.current = later  # Focus changes must not advance another editor.
        send(local)
        return True

    service.set_selection_execution(
        SimpleNamespace(
            resolve_selection=lambda editor, **kwargs: receipt, dispatch=dispatch
        )
    )
    try:
        assert service.run_selected(advance=True) is receipt.provenance
        assert local.commands == ["answer = 42\n"]
        assert service._commandHistory == ["answer = 42"]
        assert first.textCursor().blockNumber() == 1
        assert later.textCursor().position() == 0
    finally:
        first.deleteLater()
        later.deleteLater()
        app.sendPostedEvents()


def test_stale_selection_does_not_send_append_history_or_advance():
    from pyzo.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    editor = QtWidgets.QPlainTextEdit()
    editor.setPlainText("answer = 42\nnext_line = 1")
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.LineUnderCursor)
    editor.setTextCursor(cursor)
    before = (cursor.position(), cursor.anchor())
    shell = Shell()
    service = make_service(shell, Editors(current=editor))
    service.set_selection_execution(
        SimpleNamespace(
            resolve_selection=lambda editor, **kwargs: SimpleNamespace(
                resolved=True, provenance=object()
            ),
            dispatch=lambda provenance, send: False,
        )
    )
    try:
        assert service.run_selected(advance=True) is None
        assert shell.calls == []
        assert service._commandHistory == []
        assert (editor.textCursor().position(), editor.textCursor().anchor()) == before
    finally:
        editor.deleteLater()
        app.sendPostedEvents()


def test_print_line_keeps_assignment_result_and_uses_line_route():
    from pyzo.qt import QtGui, QtWidgets
    from pyzo.codeeditor.parsers.python_parser import PythonParser

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    editor = QtWidgets.QPlainTextEdit()
    editor.setPlainText("answer = 42 # comment\n")
    editor.parser = lambda: PythonParser()
    data = QtGui.QTextBlockUserData()
    data.tokens = PythonParser().parseLine("answer = 42 # comment", 0)
    editor.textCursor().block().setUserData(data)
    commands, captures = [], []
    shell = SimpleNamespace(executeCommand=commands.append)
    service = make_service(Shell(), Editors(current=editor))
    service.set_selection_execution(
        SimpleNamespace(
            resolve_selection=lambda editor, **kwargs: (
                captures.append(kwargs)
                or SimpleNamespace(resolved=True, provenance=object())
            ),
            dispatch=lambda provenance, send: send(shell) or True,
        )
    )
    try:
        service.run_current_line_and_print_result()
        assert commands == ["answer = 42; answer\n"]
        assert captures == [{"line": True}]
    finally:
        editor.deleteLater()
        app.sendPostedEvents()
