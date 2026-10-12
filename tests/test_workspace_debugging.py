"""Workspace overlay projection, captured sessions and origin rejection."""

from types import SimpleNamespace

import pytest
from pyzo.core.workspaceDebugging import WorkspaceDebugging, debug_token
from pyzo.core.nativeRequests import wire_receipt
from spikes.peer_frames.execution_routing import ExecutionCommand
from test_workspace_execution import world as execution_world  # noqa: F401


class Editor:
    def __init__(self, view, document):
        self._peerViewIdentity, self._peerDocumentIdentity = view, document
        self.filename = "/shared.py"
        self.entries, self.callbacks, self.indicator = {}, [], None
        self.breakPointsChanged = SimpleNamespace(connect=self.callbacks.append)

    def breakPoints(self, triState=False):
        return (
            list(self.entries.items())
            if triState
            else [line for line, enabled in self.entries.items() if enabled]
        )

    def toggleBreakpoint(self, line, triState=True):
        if line not in self.entries:
            self.entries[line] = True
        elif triState and self.entries[line]:
            self.entries[line] = False
        else:
            del self.entries[line]
        for callback in self.callbacks:
            callback(self)

    def setDebugLineIndicator(self, line):
        self.indicator = line


@pytest.fixture
def debugging(execution_world):  # noqa: F811
    w = execution_world
    document = w.docs.create_document("one\ntwo\nthree\n", path="/shared.py")
    w.views = [
        Editor(w.docs.open_view(document, owner), document) for owner in (w.a, w.a, w.b)
    ]
    documents = SimpleNamespace(
        registry=w.docs,
        _editors=w.views,
        is_bound=lambda editor: editor in w.views,
        view_identity=lambda editor: editor._peerViewIdentity,
    )
    w.writes, w.navigation = [], []
    w.debugging = WorkspaceDebugging(
        documents,
        w.service,
        w.views,
        lambda: w.writes.append("checkpoint"),
        lambda *args: w.navigation.append(args),
        w.messages.append,
    )
    for shell in w.shells:
        shell._debugState = {}
        shell._nativeTransportEpoch = "transport"
        shell.breakpoints = []
        shell._stat_breakpoints = SimpleNamespace(send=shell.breakpoints.append)
    for editor in w.views:
        w.debugging.bind(editor)
    return w


def test_projection_same_owner_disabled_state_and_foreign_delivery(debugging):
    w = debugging
    a, other_a, b = w.views
    a.toggleBreakpoint(2)
    a.toggleBreakpoint(3)
    a.toggleBreakpoint(3)
    b.toggleBreakpoint(1)
    assert a.breakPoints(True) == other_a.breakPoints(True) == [(2, True), (3, False)]
    assert b.breakPoints(True) == [(1, True)]
    assert w.shells[0].breakpoints[-1] == {"/shared.py": [2]}
    assert w.shells[2].breakpoints[-1] == {"/shared.py": [1]}
    w.debugging.clear(w.a)
    assert not a.breakPoints(True) and not other_a.breakPoints(True)
    assert b.breakPoints(True) == [(1, True)]


def test_active_snapshot_and_next_operation_use_distinct_overlay_generations(debugging):
    w = debugging
    shell, editor = w.shells[0], w.views[0]
    w.service.foreground_shell(shell)
    editor.toggleBreakpoint(2)
    receipt = wire_receipt(
        w.service.resolve_shell(shell, ExecutionCommand.DEBUGGER).provenance
    )
    w.debugging.before_operation(shell, receipt, "code", {})
    editor.toggleBreakpoint(3)
    assert shell.breakpoints[-1] == {"/shared.py": [2]}
    w.debugging.finished(shell, dict(phase="complete", receipt=receipt))
    assert shell.breakpoints[-1] == {"/shared.py": [2, 3]}
    w.debugging.before_operation(shell, {**receipt, "operation_id": "next"}, "code", {})
    assert shell._workspaceBreakpointSnapshot == {"/shared.py": [2, 3]}


@pytest.mark.parametrize(
    "drift",
    [
        "workspace_id",
        "shell_id",
        "kernel_generation_id",
        "shell_epoch",
        "process_workspace_id",
        "sequence",
        "epoch",
    ],
)
def test_debug_status_rejects_foreign_or_retired_origin(debugging, drift):
    w = debugging
    shell = w.shells[0]
    w.service.foreground_shell(shell)
    receipt = wire_receipt(
        w.service.resolve_shell(shell, ExecutionCommand.DEBUGGER).provenance
    )
    state = dict(debugmode=2, session_id="session", sequence=3, origin=receipt)
    assert w.debugging.accept_debug(shell, dict(epoch="transport", state=state))
    shell._debugState = state
    token = debug_token(shell)
    if drift == "sequence":
        state[drift] = 2
    elif drift == "epoch":
        shell._nativeTransportEpoch = "new"
    else:
        receipt[drift] = "foreign"
    assert not w.debugging.accept_debug(shell, dict(epoch="transport", state=state))
    assert token[1:] == ("session", 3, 2)


def test_source_identity_and_indicators_do_not_fall_through_to_other_owner(debugging):
    w = debugging
    a, _other_a, b = w.views
    shell = w.shells[0]
    w.service.foreground_shell(shell)
    shell._workspaceSessionSources = {"/shared.py": a._peerDocumentIdentity}
    w.debugging.indicators(shell, [("/shared.py", 2)])
    assert a.indicator == 2 and b.indicator is None
    w.debugging.navigate(shell, "/shared.py", 2)
    assert w.navigation == [(w.a, a._peerDocumentIdentity, "/shared.py", 2)]
    w.docs.mark_unavailable(a._peerDocumentIdentity)
    w.debugging.navigate(shell, "/shared.py", 2)
    assert len(w.navigation) == 1 and w.messages
    assert not w.debugging.overlays.project(w.a)
    w.service.detach_shell(shell)
    w.debugging.navigate(shell, "/shared.py", 2)
    assert len(w.navigation) == 1


def test_pathless_document_projects_every_local_native_alias(debugging):
    w = debugging
    document = w.docs.create_document("one\ntwo\nthree\n")
    for index, (editor, owner) in enumerate(zip(w.views, (w.a, w.a, w.b))):
        editor._peerDocumentIdentity = document
        editor._peerViewIdentity = w.docs.open_view(document, owner)
        editor.filename = None
        editor.id = lambda index=index: f"<tmp-{index}>"
    w.views[0].toggleBreakpoint(2)
    w.views[2].toggleBreakpoint(3)
    assert w.shells[0].breakpoints[-1] == {"<tmp-0>": [2], "<tmp-1>": [2]}
    assert w.shells[2].breakpoints[-1] == {"<tmp-2>": [3]}


def test_traceback_canonical_alias_uses_registered_document_identity(
    debugging, tmp_path
):
    w = debugging
    physical = tmp_path / "physical.py"
    physical.write_text("source")
    alias = tmp_path / "alias.py"
    from spikes.peer_frames.filesystem_capabilities import create_optional_symlink

    if not create_optional_symlink(alias, physical):
        pytest.skip("optional symlink affordance unavailable to this Windows account")
    identity = w.views[0]._peerDocumentIdentity
    assert w.docs.rename_document(identity, str(alias)).applied
    w.debugging.navigate(w.shells[0], str(physical), 2)
    assert w.navigation == [(w.a, identity, str(physical), 2)]


def test_lost_foreground_debugger_disables_controls_without_fallback(
    debugging, monkeypatch
):
    import pyzo
    from test_shell_commands import make_service

    w = debugging
    commands, panel, _assigned, _editor = make_service()
    selected, other, _foreign = w.shells
    selected._debugState = other._debugState = dict(debugmode=2)
    w.service.foreground_shell(selected)
    panel.getCurrentShell = lambda: selected
    monkeypatch.setattr(
        pyzo,
        "application",
        SimpleNamespace(workspace_debugging=w.debugging, workspace_execution=w.service),
        raising=False,
    )
    assert commands.is_enabled("shell.debug.control.next")
    selected._context.connection_count = 0
    assert not commands.is_enabled("shell.debug.control.next")
    assert not commands.invoke("shell.debug.control.next")


def test_debug_indicators_match_platform_path_casing(debugging, monkeypatch):
    import ntpath
    from pyzo.core import workspaceDebugging

    w = debugging
    shell = w.shells[0]
    w.service.foreground_shell(shell)
    shell._workspaceSessionSources = {r"C:\Work\Shared.py": w.views[0]._peerDocumentIdentity}
    monkeypatch.setattr(workspaceDebugging.os.path, "normcase", ntpath.normcase)
    w.debugging.indicators(shell, [(r"c:\work\shared.py", 2)])
    assert [editor.indicator for editor in w.views] == [2, 2, None]
    w.debugging.navigate(shell, r"c:\work\shared.py", 2)
    assert w.navigation[-1] == (w.a, w.views[0]._peerDocumentIdentity, r"c:\work\shared.py", 2)
