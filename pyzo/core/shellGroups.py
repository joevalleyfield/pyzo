"""Shell-level routing for the QtADS integration."""

from __future__ import annotations

import pyzo
from pyzo.qt import QtCore


class ShellGroupManager(QtCore.QObject):
    """Keep Pyzo's shell service while hosting one shell per ADS dock."""

    currentShellChanged = QtCore.Signal()
    currentShellStateChanged = QtCore.Signal()
    executionShellChanged = QtCore.Signal()

    def __init__(self, host_factory, parent=None):
        super().__init__(parent)
        self._hostFactory = host_factory
        self._hosts = []
        self._activeHost = None
        self._executionHost = None
        self._contextMenusReady = False

    @property
    def hosts(self):
        return tuple(self._hosts)

    @property
    def _debugmode(self):
        return (
            self._executionHost._debugmode
            if self._executionHost is not None
            else 0
        )

    def setActiveHost(self, host):
        if host not in self._hosts:
            raise ValueError("unknown shell host")
        if self._activeHost is host:
            return
        self._activeHost = host
        shell = host.getCurrentShell()
        host.onShellStateChange(shell)
        host.onShellDebugStateChange(shell)
        self.currentShellChanged.emit()
        self.currentShellStateChanged.emit()

    def _register_host(self, host):
        self._hosts.append(host)
        host.currentShellChanged.connect(
            lambda: self._forward_current_shell_from(host)
        )
        host.currentShellStateChanged.connect(
            lambda: self._forward_state_from(host)
        )
        if self._contextMenusReady:
            host.addContextMenu()

    def _forward_current_shell_from(self, host):
        if host is self._activeHost:
            self.currentShellChanged.emit()
        else:
            self.setActiveHost(host)

    def _forward_state_from(self, host):
        if host is self._activeHost:
            self.currentShellStateChanged.emit()
        if host is self._executionHost:
            self.executionShellChanged.emit()

    def addShell(self, shellInfo=None):
        host = self._hostFactory()
        self._register_host(host)
        self.setActiveHost(host)
        shell = host.addShell(shellInfo)
        if self._executionHost is None:
            self.setExecutionShell(shell)
        return shell

    def setExecutionShell(self, shell):
        host = next(
            (host for host in self._hosts if shell in host.getShells()), None
        )
        if host is None:
            raise ValueError("unknown execution shell")
        if self._executionHost is host:
            return
        self._executionHost = host
        host.onShellDebugStateChange(shell)
        self.executionShellChanged.emit()

    def getExecutionShell(self):
        if self._executionHost is None:
            return None
        return self._executionHost.getCurrentShell()

    def removeShell(self, shell):
        for host in tuple(self._hosts):
            if shell in host.getShells():
                host.removeShell(shell)
                self._hosts.remove(host)
                if pyzo.application is not None:
                    pyzo.application.panes.remove_shell_host(host)
                else:
                    pyzo.main.removeShellHost(host)
                if self._activeHost is host:
                    self._activeHost = None
                    if self._hosts:
                        self.setActiveHost(self._hosts[-1])
                    else:
                        self.currentShellChanged.emit()
                        self.currentShellStateChanged.emit()
                if self._executionHost is host:
                    self._executionHost = None
                    if self._hosts:
                        fallback = self._activeHost or self._hosts[-1]
                        self.setExecutionShell(fallback.getCurrentShell())
                    else:
                        self.executionShellChanged.emit()
                return

    def getCurrentShell(self):
        if self._activeHost is None:
            return None
        return self._activeHost.getCurrentShell()

    def getShells(self):
        return [shell for host in self._hosts for shell in host.getShells()]

    def getShellAt(self, index):
        return self.getShells()[index]

    def __iter__(self):
        return iter(self.getShells())

    def addContextMenu(self):
        self._contextMenusReady = True
        for host in self._hosts:
            host.addContextMenu()

    def __getattr__(self, name):
        if name.startswith("__") or self._activeHost is None:
            raise AttributeError(name)
        return getattr(self._activeHost, name)


def install_shell_group_manager(host_factory, parent=None):
    manager = ShellGroupManager(host_factory, parent)
    pyzo.shells = manager
    return manager
