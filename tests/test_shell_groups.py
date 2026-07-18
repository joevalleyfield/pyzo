from pyzo.core.shellGroups import ShellGroupManager
from pyzo.qt import QtCore


class Host(QtCore.QObject):
    currentShellChanged = QtCore.Signal()
    currentShellStateChanged = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._shell = None
        self._debugmode = 0

    def addShell(self, shell_info=None):
        self._shell = object()
        self.currentShellChanged.emit()
        return self._shell

    def getCurrentShell(self):
        return self._shell

    def getShells(self):
        return [] if self._shell is None else [self._shell]

    def onShellStateChange(self, shell):
        pass

    def onShellDebugStateChange(self, shell):
        pass


def test_first_shell_reemits_current_shell_after_empty_host_activation():
    manager = ShellGroupManager(Host)
    observed = []
    manager.currentShellChanged.connect(
        lambda: observed.append(manager.getCurrentShell())
    )

    shell = manager.addShell()

    assert observed == [None, shell]
    assert manager.getCurrentShell() is shell
