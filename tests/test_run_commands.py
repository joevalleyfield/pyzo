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

    assert shell.calls == [
        (("answer = 42", "current.py"), {"changeDir": True})
    ]
    assert editor.run_cursor is not None


def test_editor_context_target_does_not_depend_on_current_editor():
    shell = Shell()
    selected_tab_editor = Editor("selected.py", "selected = True")
    service = make_service(shell, Editors(current=None))

    service.run_file((False, False), selected_tab_editor)

    assert shell.calls == [
        (("selected = True", "selected.py"), {"changeDir": False})
    ]
