from types import SimpleNamespace

from pyzo.core.applicationController import (
    ApplicationMetadataAccess,
    PaneLifecycleAccess,
    PeerApplicationController,
    ToolWorkspaceAccess,
)
from pyzo.qt import QtCore, QtWidgets


class Context:
    frame_id = "frame-a"


class Workspace:
    def resolve_active_context(self):
        return Context()

    def serialize_layout(self):
        return {"schema": 2, "frames": []}


class WorkspaceCoordinator:
    workspace_ids = ("workspace-a", "workspace-b")
    active_workspace_id = "workspace-a"

    def __init__(self, workspace):
        self._workspace = workspace

    def resolve_active_context(self):
        return self._workspace.resolve_active_context()

    def model(self, workspace_id):
        assert workspace_id == "workspace-a"
        return self._workspace

    def activate_workspace(self, workspace_id):
        return ("activate", workspace_id)

    def activate_pane(self, workspace_id, pane_id):
        return ("activate-pane", workspace_id, pane_id)

    def resolve_workspace_context(self, workspace_id, pane_id=None):
        return ("context", workspace_id, pane_id)


class DocumentRegistry:
    def __init__(self):
        self.calls = []
        self._resource = SimpleNamespace(text="before", revision=4)
        self._view = SimpleNamespace(document_id="document-a")

    def resource(self, document_id):
        assert document_id == "document-a"
        return self._resource

    def view(self, view_id):
        assert view_id == "view-a"
        if self._view is None:
            raise KeyError(view_id)
        return self._view

    def replace_text(self, view_id, text):
        self.calls.append(("sync", view_id, text))
        self._resource.text = text
        self._resource.revision += 1
        return self._resource.revision

    def can_assign_path(self, document_id, path):
        self.calls.append(("can-save", document_id, path))
        return path != "collision.py"

    def adopt_saved(self, document_id, path, text):
        self.calls.append(("saved", document_id, path, text))
        return True


class Editor:
    _peerDocumentIdentity = "document-a"
    _peerViewIdentity = "view-a"

    def toPlainText(self):
        return "after"


def test_peer_application_controller_is_a_non_window_service_boundary():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    workspace = Workspace()
    workspaces = WorkspaceCoordinator(workspace)
    documents = DocumentRegistry()
    status = object()
    frame = object()
    execution_shell = object()
    calls = []
    tool_calls = []
    tools = ToolWorkspaceAccess(
        dock_manager="dock-manager",
        add_dock=lambda dock, target: tool_calls.append(("add", dock, target)),
        close_dock=lambda dock: tool_calls.append(("close", dock)),
        peer_mode=True,
    )
    metadata_calls = []
    metadata = ApplicationMetadataAccess(
        update_document=lambda editor: metadata_calls.append(("document", editor)),
        update_cursor=lambda editor: metadata_calls.append(("cursor", editor)),
        update_shell=lambda shell: metadata_calls.append(("shell", shell)),
        show_status=lambda text, timeout: metadata_calls.append(
            ("status", text, timeout)
        ),
    )
    pane_calls = []
    panes = PaneLifecycleAccess(
        remove_shell_host=lambda host: pane_calls.append(("remove", host)),
        request_shell_close=lambda shell: pane_calls.append(("close", shell)),
        retire_editor_recovery=lambda editor, **options: pane_calls.append(
            ("retire", editor, options)
        ),
    )

    controller = PeerApplicationController(
        parent=app,
        workspace=workspace,
        frame_window=lambda frame_id: frame if frame_id == "frame-a" else None,
        status_service=lambda: status,
        shell_commands="shell-commands",
        run_commands="run-commands",
        request_quit=lambda: calls.append("quit"),
        request_restart=lambda: calls.append("restart"),
        set_style=lambda name: calls.append(("style", name)),
        get_execution_shell=lambda: execution_shell,
        set_execution_shell=lambda shell: calls.append(("shell", shell)),
        tools=tools,
        metadata=metadata,
        panes=panes,
        workspace_coordinator=workspaces,
        document_resources=documents,
    )

    assert isinstance(controller, QtCore.QObject)
    assert not isinstance(controller, QtWidgets.QWidget)
    assert controller.parent() is app
    assert controller.workspace.model is workspace
    assert controller.workspace.snapshot() == {"schema": 2, "frames": []}
    assert controller.workspaces.identities == ("workspace-a", "workspace-b")
    assert controller.workspaces.active_identity == "workspace-a"
    assert controller.workspaces.context().frame_id == "frame-a"
    assert controller.workspaces.model("workspace-a") is workspace
    assert controller.workspaces.activate("workspace-b") == (
        "activate",
        "workspace-b",
    )
    assert controller.workspaces.activate_pane("workspace-b", "pane-b") == (
        "activate-pane",
        "workspace-b",
        "pane-b",
    )
    assert controller.workspaces.context_for("workspace-b", "pane-b") == (
        "context",
        "workspace-b",
        "pane-b",
    )
    assert controller.documents.registry is documents
    editor = Editor()
    assert controller.documents.identity(editor) == "document-a"
    assert controller.documents.view_identity(editor) == "view-a"
    assert controller.documents.resource(editor).text == "before"
    assert controller.documents.sync(editor) == 5
    documents._view = None
    assert controller.documents.sync(editor) is None
    documents._view = SimpleNamespace(document_id="document-a")
    assert controller.documents.can_save_as(editor, "saved.py")
    assert not controller.documents.can_save_as(editor, "collision.py")
    assert controller.documents.adopt_saved(editor, "saved.py") is True
    assert controller.active_peer.context().frame_id == "frame-a"
    assert controller.active_peer.frame_id() == "frame-a"
    assert controller.active_peer.window() is frame
    assert controller.dialogs.parent() is frame
    assert controller.status.service is status
    assert controller.shell_commands == "shell-commands"
    assert controller.run_commands == "run-commands"
    assert controller.execution_target.shell() is execution_shell
    assert controller.tools is tools
    assert controller.metadata is metadata
    assert controller.panes is panes
    assert not hasattr(controller, "menuBar")
    assert not hasattr(controller, "statusBar")

    controller.lifecycle.quit()
    controller.lifecycle.restart()
    controller.appearance.set_style("Fusion")
    controller.appearance.restart()
    controller.execution_target.select_shell(execution_shell)
    controller.tools.add_dock("dock", "target")
    controller.tools.close_dock("dock")
    controller.metadata.document("editor")
    controller.metadata.cursor("editor")
    controller.metadata.shell("shell")
    controller.metadata.show_status("message", 5000)
    controller.panes.remove_shell_host("host")
    controller.panes.request_shell_close("shell")
    controller.panes.retire_editor_recovery(
        "editor", destructive_fallback=True
    )

    assert calls == [
        "quit",
        "restart",
        ("style", "Fusion"),
        "restart",
        ("shell", execution_shell),
    ]
    assert tool_calls == [("add", "dock", "target"), ("close", "dock")]
    assert metadata_calls == [
        ("document", "editor"),
        ("cursor", "editor"),
        ("shell", "shell"),
        ("status", "message", 5000),
    ]
    assert pane_calls == [
        ("remove", "host"),
        ("close", "shell"),
        ("retire", "editor", {"destructive_fallback": True}),
    ]
    assert documents.calls == [
        ("sync", "view-a", "after"),
        ("can-save", "document-a", "saved.py"),
        ("can-save", "document-a", "collision.py"),
        ("saved", "document-a", "saved.py", "after"),
    ]
