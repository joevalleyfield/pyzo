from pyzo.core.applicationController import (
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


def test_peer_application_controller_is_a_non_window_service_boundary():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    workspace = Workspace()
    legacy_host = object()
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

    controller = PeerApplicationController(
        parent=app,
        legacy_host=legacy_host,
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
    )

    assert isinstance(controller, QtCore.QObject)
    assert not isinstance(controller, QtWidgets.QWidget)
    assert controller.parent() is app
    assert controller.legacy_host is legacy_host
    assert controller.workspace.model is workspace
    assert controller.workspace.snapshot() == {"schema": 2, "frames": []}
    assert controller.active_peer.context().frame_id == "frame-a"
    assert controller.active_peer.frame_id() == "frame-a"
    assert controller.active_peer.window() is frame
    assert controller.dialogs.parent() is frame
    assert controller.status.service is status
    assert controller.shell_commands == "shell-commands"
    assert controller.run_commands == "run-commands"
    assert controller.execution_target.shell() is execution_shell
    assert controller.tools is tools
    assert not hasattr(controller, "menuBar")
    assert not hasattr(controller, "statusBar")

    controller.lifecycle.quit()
    controller.lifecycle.restart()
    controller.appearance.set_style("Fusion")
    controller.appearance.restart()
    controller.execution_target.select_shell(execution_shell)
    controller.tools.add_dock("dock", "target")
    controller.tools.close_dock("dock")

    assert calls == [
        "quit",
        "restart",
        ("style", "Fusion"),
        "restart",
        ("shell", execution_shell),
    ]
    assert tool_calls == [("add", "dock", "target"), ("close", "dock")]
