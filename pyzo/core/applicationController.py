"""Application-owned service boundary for the peer-frame workspace."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
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


class WorkspaceCollectionAccess:
    """Expose concurrent workspace identity without leaking mutable registries."""

    def __init__(self, coordinator):
        self._coordinator = coordinator

    @property
    def identities(self):
        return self._coordinator.workspace_ids

    @property
    def active_identity(self):
        return self._coordinator.active_workspace_id

    def context(self):
        return self._coordinator.resolve_active_context()

    def model(self, workspace_id):
        return self._coordinator.model(workspace_id)

    def activate(self, workspace_id):
        """Activate an explicitly addressed workspace without focus guessing."""

        return self._coordinator.activate_workspace(workspace_id)

    def activate_pane(self, workspace_id, pane_id):
        """Activate a pane only when it belongs to the addressed workspace."""

        return self._coordinator.activate_pane(workspace_id, pane_id)

    def context_for(self, workspace_id, pane_id=None):
        """Resolve a workspace-addressed context without active fallback."""

        return self._coordinator.resolve_workspace_context(workspace_id, pane_id)


class DocumentCollectionAccess:
    """Bridge peer editors to the application-owned document resource model."""

    def __init__(self, registry, on_edit=None):
        self._registry = registry
        self._on_edit = on_edit
        self._editors = {}
        self._pending = {}
        extensions = getattr(registry, "_persistence_extensions", {})
        retained = extensions.get("peer_document_retention", {})
        pending = retained.get("pending", {}) if isinstance(retained, dict) else {}
        if isinstance(pending, dict) and hasattr(registry, "document_ids"):
            views = getattr(registry, "view_ids", ())
            for document_id in registry.document_ids:
                record = pending.get(str(document_id))
                if not isinstance(record, dict):
                    continue
                view_value = record.get("view_id")
                base_text = record.get("base_text")
                view_id = next(
                    (candidate for candidate in views if str(candidate) == view_value),
                    None,
                )
                if view_id is not None and isinstance(base_text, str):
                    self._pending[document_id] = (view_id, base_text)

    @property
    def registry(self):
        return self._registry

    def identity(self, editor):
        return getattr(editor, "_peerDocumentIdentity", None)

    def view_identity(self, editor):
        return getattr(editor, "_peerViewIdentity", None)

    def resource(self, editor):
        identity = self.identity(editor)
        return None if identity is None else self._registry.resource(identity)

    def sync(self, editor):
        view_id = self.view_identity(editor)
        if view_id is None:
            return None
        try:
            view = self._registry.view(view_id)
        except KeyError:
            return None
        resource = self._registry.resource(view.document_id)
        text = editor.toPlainText()
        if resource.text != text:
            if editor not in self._editors:
                return self._registry.replace_text(view_id, text)
            self._changed(editor)
        return resource.revision

    def persistence_snapshot(self):
        """Capture registry state plus adapter-owned pending transactions."""

        snapshot = deepcopy(dict(self._registry.persistence_snapshot()))
        extensions = snapshot.setdefault("extensions", {})
        extensions.pop("peer_document_retention", None)
        if self._pending:
            extensions["peer_document_retention"] = {
                "pending": {
                    str(document_id): {
                        "view_id": str(view_id),
                        "base_text": base_text,
                    }
                    for document_id, (view_id, base_text) in self._pending.items()
                }
            }
        return snapshot

    def bind(self, editor):
        """Bind one live peer editor to its retained document resource."""

        if editor in self._editors:
            return
        initial_modified = editor.document().isModified()
        initial_text = editor.toPlainText()
        self._editors[editor] = editor
        editor.setUndoRedoEnabled(False)
        editor._peerRetentionChange = None
        editor._peerRetentionPreviousChange = None
        editor._peerRetentionBreak = False
        editor.document().contentsChange.connect(
            lambda position, removed, added, editor=editor: self._contents_changed(
                editor, position, removed, added
            )
        )
        editor.cursorPositionChanged.connect(
            lambda editor=editor: self._cursor_changed(editor)
        )
        editor.textChanged.connect(lambda editor=editor: self._changed(editor))
        editor.undo = lambda editor=editor: self.undo(editor)
        editor.redo = lambda editor=editor: self.redo(editor)
        # Pyzo's editor constructor marks new documents dirty before the tab
        # lifecycle is attached. Preserve the state observed at the boundary;
        # otherwise the native setup signal can reassert that marker after bind.
        editor.document().setModified(initial_modified)
        if not initial_modified:
            def restore_initial_modified_state():
                try:
                    if editor.toPlainText() == initial_text:
                        editor.document().setModified(False)
                except RuntimeError:
                    pass

            QtCore.QTimer.singleShot(0, restore_initial_modified_state)

    def unbind(self, editor):
        """Release editor-scoped retention state before widget destruction."""

        self._editors.pop(editor, None)
        for name in (
            "_calmedIntrospectNow",
            "_calmedUpdateHelpFromAutocomplete",
        ):
            delayed = getattr(editor, name, None)
            if delayed is not None:
                delayed.clear_pending()
        editor.calltipCancel()
        editor._showRunCursorTimer.stop()
        view_id = self.view_identity(editor)
        if view_id is not None:
            try:
                document_id = self._registry.view(view_id).document_id
            except KeyError:
                document_id = self.identity(editor)
            if document_id is not None:
                self._pending.pop(document_id, None)

    def _contents_changed(self, editor, position, removed, added):
        editor._peerRetentionChange = (position, removed, added)

    def _cursor_changed(self, editor):
        document_id = self.identity(editor)
        view_id = self.view_identity(editor)
        if document_id is not None and view_id is not None:
            cursor = editor.textCursor()
            self._registry.update_view_state(
                view_id,
                cursor=int(cursor.position()),
                selection=(int(cursor.selectionStart()), int(cursor.selectionEnd())),
                scroll=int(editor.verticalScrollBar().value()),
            )
        if (
            editor._peerRetentionChange is None
            and document_id is not None
            and document_id in self._pending
            and self._pending[document_id][0] == view_id
        ):
            editor._peerRetentionBreak = True

    def _changed(self, editor):
        change = editor._peerRetentionChange
        editor._peerRetentionChange = None
        document_id = self.identity(editor)
        view_id = self.view_identity(editor)
        if document_id is None or view_id is None:
            return
        resource = self._registry.resource(document_id)
        if resource.text == editor.toPlainText():
            return
        pending = self._pending.get(document_id)
        previous = editor._peerRetentionPreviousChange
        removed = change[1] if change is not None else 0
        deletion_continues = bool(
            change
            and previous
            and removed > 0
            and change[2] == 0
            and previous[1] > 0
            and previous[2] == 0
            and change[0] in (previous[0], previous[0] - previous[1])
        )
        if pending is not None and (
            pending[0] != view_id
            or editor._peerRetentionBreak
            or (removed and not deletion_continues)
        ):
            self._flush(document_id)
        editor._peerRetentionBreak = False
        if document_id not in self._pending:
            self._pending[document_id] = (view_id, resource.text)
        resource.text = editor.toPlainText()
        resource.revision += 1
        editor._peerRetentionPreviousChange = change
        self._project(document_id, editor)
        if self._on_edit is not None:
            self._on_edit()

    def _flush(self, document_id):
        pending = self._pending.pop(document_id, None)
        if pending is None:
            return
        view_id, base_text = pending
        resource = self._registry.resource(document_id)
        if resource.text == base_text:
            return
        resource._undo.append(base_text)
        resource._redo.clear()
        self._registry._notify(resource, view_id)

    def _project(self, document_id, source=None):
        from pyzo.qt import QtCore

        resource = self._registry.resource(document_id)
        text = resource.text
        for editor in tuple(self._editors):
            if self.identity(editor) != document_id or editor is source:
                continue
            cursor = editor.textCursor()
            position, anchor = cursor.position(), cursor.anchor()
            with QtCore.QSignalBlocker(editor), QtCore.QSignalBlocker(editor.document()):
                editor.setUndoRedoEnabled(False)
                editor.setPlainText(text)
                editor.document().setModified(resource.is_dirty())
            cursor = editor.textCursor()
            maximum = max(0, editor.document().characterCount() - 1)
            cursor.setPosition(min(anchor, maximum))
            cursor.setPosition(
                min(position, maximum), cursor.MoveMode.KeepAnchor
            )
            editor.setTextCursor(cursor)

    def flush(self, editor):
        document_id = self.identity(editor)
        if document_id is not None:
            self._flush(document_id)

    def undo(self, editor):
        view_id = self.view_identity(editor)
        if view_id is None:
            return False
        self.flush(editor)
        if not self._registry.undo(view_id):
            return False
        self._project(self.identity(editor))
        import pyzo

        for target in tuple(self._editors):
            if self.identity(target) == self.identity(editor):
                target.document().setModified(
                    self._registry.resource(self.identity(editor)).is_dirty()
                )
                pyzo.parser.parseThis(target)
        if self._on_edit is not None:
            self._on_edit()
        return True

    def redo(self, editor):
        view_id = self.view_identity(editor)
        if view_id is None:
            return False
        self.flush(editor)
        if not self._registry.redo(view_id):
            return False
        self._project(self.identity(editor))
        import pyzo

        for target in tuple(self._editors):
            if self.identity(target) == self.identity(editor):
                target.document().setModified(
                    self._registry.resource(self.identity(editor)).is_dirty()
                )
                pyzo.parser.parseThis(target)
        if self._on_edit is not None:
            self._on_edit()
        return True

    def can_save_as(self, editor, path):
        identity = self.identity(editor)
        return identity is None or self._registry.can_assign_path(identity, path)

    def adopt_saved(self, editor, path):
        self.flush(editor)
        identity = self.identity(editor)
        if identity is None:
            return None
        return self._registry.adopt_saved(identity, path, editor.toPlainText())

    def cache_token(self, editor):
        view_id = self.view_identity(editor)
        if view_id is None:
            return None
        self.sync(editor)
        try:
            return self._registry.view_token(view_id)
        except KeyError:
            return None

    def token_is_current(self, token):
        return self._registry.token_is_current(token)


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


class PaneLifecycleAccess:
    """Route pane cleanup and close negotiation without exposing its host."""

    def __init__(
        self,
        *,
        remove_shell_host: Callable[[Any], Any],
        request_shell_close: Callable[[Any], Any],
        retire_editor_recovery: Callable[[Any, bool], Any],
    ):
        self._removeShellHost = remove_shell_host
        self._requestShellClose = request_shell_close
        self._retireEditorRecovery = retire_editor_recovery

    def remove_shell_host(self, host):
        return self._removeShellHost(host)

    def request_shell_close(self, shell):
        return self._requestShellClose(shell)

    def retire_editor_recovery(self, editor, *, destructive_fallback=False):
        return self._retireEditorRecovery(
            editor, destructive_fallback=destructive_fallback
        )


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

    The controller does not forward arbitrary host attributes, so peer code
    cannot accidentally acquire window APIs through this boundary.
    """

    def __init__(
        self,
        *,
        parent,
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
        panes,
        workspace_coordinator=None,
        document_resources=None,
        retention_edit=None,
        reopen_retained_buffer=None,
        reopen_retained_view=None,
    ):
        super().__init__(parent)
        self.workspace = WorkspaceAccess(workspace)
        self.workspaces = (
            WorkspaceCollectionAccess(workspace_coordinator)
            if workspace_coordinator is not None
            else None
        )
        self.documents = (
            DocumentCollectionAccess(document_resources, on_edit=retention_edit)
            if document_resources is not None
            else None
        )
        self.reopen_retained_buffer = reopen_retained_buffer
        self.reopen_retained_view = reopen_retained_view
        context_owner = workspace_coordinator or workspace
        self.active_peer = ActivePeerLookup(context_owner, frame_window)
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
        self.panes = panes
