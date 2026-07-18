"""Application-owned service boundary for the peer-frame workspace."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyzo.qt import QtCore, QtWidgets


class WorkspaceAccess:
    """Expose peer topology without exposing its window host."""

    def __init__(self, workspace):
        self._workspace = workspace

    @property
    def model(self):
        return self._workspace

    def snapshot(self):
        return self._workspace.serialize_layout()


class ActivePeerLookup:
    """Resolve application context and its ordinary peer window at call time."""

    def __init__(self, workspace, frame_window: Callable[[str], Any]):
        self._workspace = workspace
        self._frameWindow = frame_window

    def context(self):
        return self._workspace.resolve_active_context()

    def frame_id(self):
        return self.context().frame_id

    def window(self):
        frame_id = self.frame_id()
        return self._frameWindow(frame_id) if frame_id is not None else None


class DialogParentAccess:
    """Resolve a visible dialog parent without relying on a hidden host."""

    def __init__(self, active_peer: ActivePeerLookup):
        self._activePeer = active_peer

    def parent(self):
        return self._activePeer.window() or QtWidgets.QApplication.activeWindow()


class ToolWorkspaceAccess:
    """Expose the operations needed by tools without exposing a main window."""

    def __init__(
        self,
        *,
        dock_manager,
        add_dock: Callable[[Any, Any | None], Any],
        close_dock: Callable[[Any], Any],
        peer_mode: bool,
        legacy_parent=None,
    ):
        self.dock_manager = dock_manager
        self.peer_mode = peer_mode
        self.legacy_parent = legacy_parent
        self._addDock = add_dock
        self._closeDock = close_dock

    def widget_parent(self, dock):
        """Parent tool contents to their owning dock for correct lifetime."""

        return dock

    def add_dock(self, dock, target_dock=None):
        return self._addDock(dock, target_dock)

    def close_dock(self, dock):
        return self._closeDock(dock)


class StatusAccess:
    """Locate the application-owned status service without a status bar."""

    def __init__(self, service: Callable[[], Any]):
        self._service = service

    @property
    def service(self):
        return self._service()


class ApplicationMetadataAccess:
    """Project document, shell, and status metadata without a main window."""

    def __init__(
        self,
        *,
        update_document: Callable[[Any | None], Any],
        update_cursor: Callable[[Any | None], Any],
        update_shell: Callable[[Any | None], Any],
        show_status: Callable[[str, int], Any],
    ):
        self._updateDocument = update_document
        self._updateCursor = update_cursor
        self._updateShell = update_shell
        self._showStatus = show_status

    def document(self, editor=None):
        return self._updateDocument(editor)

    def cursor(self, editor=None):
        return self._updateCursor(editor)

    def shell(self, shell=None):
        return self._updateShell(shell)

    def show_status(self, text, timeout=0):
        return self._showStatus(text, timeout)


class ApplicationLifecycleAccess:
    """Route application lifecycle independently from a window close event."""

    def __init__(
        self,
        request_quit: Callable[[], Any],
        request_restart: Callable[[], Any],
    ):
        self._requestQuit = request_quit
        self._requestRestart = request_restart

    def quit(self):
        return self._requestQuit()

    def restart(self):
        return self._requestRestart()


class AppearanceAccess:
    """Own process-wide appearance operations outside workspace chrome."""

    def __init__(
        self,
        set_style: Callable[[str | None], Any],
        request_restart: Callable[[], Any],
    ):
        self._setStyle = set_style
        self._requestRestart = request_restart

    def set_style(self, style_name=None):
        return self._setStyle(style_name)

    def restart(self):
        return self._requestRestart()


class ExecutionTargetAccess:
    """Resolve explicit Run/Debug affinity independently from shell focus."""

    def __init__(
        self,
        get_shell: Callable[[], Any],
        set_shell: Callable[[Any], Any],
    ):
        self._getShell = get_shell
        self._setShell = set_shell

    def shell(self):
        return self._getShell()

    def select_shell(self, shell):
        return self._setShell(shell)


class PeerApplicationController(QtCore.QObject):
    """Non-window root for peer-mode application services.

    ``legacy_host`` is intentionally explicit migration scaffolding. The
    controller does not forward arbitrary attributes to it, so new peer code
    cannot accidentally acquire ``QMainWindow`` APIs through this boundary.
    """

    def __init__(
        self,
        *,
        parent,
        legacy_host,
        workspace,
        frame_window,
        status_service,
        shell_commands,
        run_commands,
        request_quit,
        request_restart,
        set_style,
        get_execution_shell,
        set_execution_shell,
        tools,
        metadata,
    ):
        super().__init__(parent)
        self._legacyHost = legacy_host
        self.workspace = WorkspaceAccess(workspace)
        self.active_peer = ActivePeerLookup(workspace, frame_window)
        self.dialogs = DialogParentAccess(self.active_peer)
        self.status = StatusAccess(status_service)
        self.shell_commands = shell_commands
        self.run_commands = run_commands
        self.lifecycle = ApplicationLifecycleAccess(request_quit, request_restart)
        self.appearance = AppearanceAccess(set_style, request_restart)
        self.execution_target = ExecutionTargetAccess(
            get_execution_shell, set_execution_shell
        )
        self.tools = tools
        self.metadata = metadata

    @property
    def legacy_host(self):
        """Return the temporary host only for named migration sites."""

        return self._legacyHost
