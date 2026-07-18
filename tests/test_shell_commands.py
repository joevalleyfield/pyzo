from types import SimpleNamespace

from pyzo.core.shellCommands import ShellCommandService
from pyzo.qt import QtCore


class Shells(QtCore.QObject):
    currentShellChanged = QtCore.Signal()
    currentShellStateChanged = QtCore.Signal()
    executionShellChanged = QtCore.Signal()

    def __init__(self, shell):
        super().__init__()
        self.shell = shell
        self._debugmode = 0

    def getExecutionShell(self):
        return self.shell


class Shell:
    def __init__(self):
        self.calls = []

    def clearScreen(self):
        self.calls.append("clear")

    def interrupt(self):
        self.calls.append("interrupt")

    def executeCommand(self, command):
        self.calls.append(command)


class Cursor:
    def blockNumber(self):
        return 6


class Editor:
    def __init__(self):
        self.cleared = False

    def textCursor(self):
        return Cursor()

    def clearBreakPoints(self):
        self.cleared = True


class Editors:
    def __init__(self, editor):
        self.editor = editor

    def getCurrentEditor(self):
        return self.editor

    def __iter__(self):
        return iter((self.editor,))


def make_service():
    shell = Shell()
    editor = Editor()
    icons = SimpleNamespace(
        application_eraser=None,
        application_lightning=None,
        application_pause=None,
        application_refresh=None,
        cancel=None,
        bug_delete=None,
        debug_next=None,
        debug_step=None,
        debug_return=None,
        arrow_in=None,
        debug_continue=None,
        debug_quit=None,
    )
    shells = Shells(shell)
    service = ShellCommandService(
        shells, Editors(editor), icons, lambda _domain, text: text
    )
    return service, shells, shell, editor


def test_shell_commands_own_targeting_invocation_and_debug_state():
    service, shells, shell, editor = make_service()

    assert service.is_enabled("shell.clear", shell)
    assert not service.is_enabled("shell.clear", None)
    assert service.invoke("shell.clear", shell)
    assert service.invoke("shell.interrupt", shell)
    assert shell.calls == ["clear", "interrupt"]

    assert service.is_enabled("shell.debug.start")
    assert service.invoke("shell.debug.start")
    assert shell.calls[-1] == "DB START\n"
    assert not service.is_enabled("shell.debug.control.next")

    shells._debugmode = 2
    assert service.is_enabled("shell.debug.control.next")
    assert service.invoke("shell.debug.control.jump")
    assert shell.calls[-1] == "DB JUMP 7\n"
    assert service.is_enabled("shell.debug.control.stop")

    assert service.invoke("shell.debug.clear_breakpoints")
    assert editor.cleared
