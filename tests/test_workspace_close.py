from types import SimpleNamespace
import pytest

import pyzo

pyzo.translate = lambda _context, text, _disambiguation=None: text

from pyzo.core.main import MainWindow
from spikes.peer_frames.contract import OperationResult, OperationStatus
from spikes.peer_frames.identity import ApplicationIdentity, IdentityKind
from spikes.peer_frames.contract import CloseDecision
from spikes.peer_frames.contract import PaneKind
from spikes.peer_frames.lifecycle import ShellLifecycleRegistry, ShellState


def test_close_workspace_persistence_failure_preserves_runtime_before_commit():
    identity = ApplicationIdentity(IdentityKind.WORKSPACE, "workspace-a")
    shells = ShellLifecycleRegistry()
    for pane_id in ("pane-a", "pane-b"):
        shells.register_shell(
            pane_id, SimpleNamespace(shell_state=lambda: ShellState.IDLE)
        )
    # A wider transaction may have separately approved B. A's rollback must
    # clear only A's uncommitted termination authority.
    assert shells.resolve_confirmations(
        (shells.prepare_close("pane-b"),), lambda *_: "terminate"
    )

    class Model:
        panes = {"pane-a": object()}
        preflight_calls = 0
        discard_calls = 0
        commit_calls = 0

        def preflight_shutdown(self, _confirm):
            self.preflight_calls += 1
            assert _confirm((shells.prepare_close("pane-a"),))
            return OperationResult(OperationStatus.APPLIED)

        def discard_preflight(self):
            self.discard_calls += 1

        def request_shutdown(self):
            self.commit_calls += 1
            raise AssertionError("destructive close reached after failed save")

    model = Model()
    runtime = SimpleNamespace(model=model)

    class Catalog:
        identities = (identity,)

        def runtime(self, requested):
            assert requested == identity
            return runtime

    class Workspaces:
        workspace_ids = (identity,)

    def fail_save():
        raise OSError("injected persistence failure")

    window = SimpleNamespace(
        _PeerApplicationIdentity=ApplicationIdentity,
        _PeerIdentityKind=IdentityKind,
        _PeerOperationStatus=OperationStatus,
        _PeerOperationResult=OperationResult,
        _peerWorkspaces=Workspaces(),
        _peerRuntimeCatalog=Catalog(),
        _peerClosingWorkspace=False,
        _resolvePeerClose=lambda votes: shells.resolve_confirmations(
            votes, lambda *_: "terminate"
        ),
        _peerShellLifecycles=shells,
        _savePeerWorkspacePersistence=fail_save,
    )

    result = MainWindow._closePeerWorkspace(window, "workspace-a")

    assert not result.applied
    assert "injected persistence failure" in result.detail
    assert model.preflight_calls == 1
    assert model.discard_calls == 1
    assert model.commit_calls == 0
    assert Workspaces.workspace_ids == (identity,)
    assert shells.validate_close("pane-a").decision is CloseDecision.REFUSE
    assert shells.validate_close("pane-b").decision is CloseDecision.ALLOW
    assert not window._peerClosingWorkspace


@pytest.mark.parametrize(
    "state, expected", [("live", True), ("retired", False), ("legacy", True)]
)
def test_tab_main_projection_during_view_retirement(monkeypatch, state, expected):
    from pyzo.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from pyzo.core.editorTabs import FileTabWidget

    assert app is not None
    editor = SimpleNamespace(_peerViewIdentity="view")
    item = SimpleNamespace(editor=editor, id="old-main")

    def view(identity):
        assert identity == "view"
        if state == "retired":
            raise KeyError("retired view")
        return SimpleNamespace(workspace_id="a", document_id="doc")

    documents = SimpleNamespace(
        is_bound=lambda candidate: candidate is editor and state != "legacy",
        registry=SimpleNamespace(
            view=view, main=lambda owner: SimpleNamespace(document_id="doc")
        ),
    )
    monkeypatch.setattr(
        pyzo, "application", SimpleNamespace(documents=documents), raising=False
    )
    assert (
        FileTabWidget.isMainItem(SimpleNamespace(_mainFile="old-main"), item)
        is expected
    )


def test_resume_refuses_incomplete_editor_placement_mapping_before_layout_commit():
    model = SimpleNamespace(panes={"editor": SimpleNamespace(kind=PaneKind.EDITOR)})
    runtime = SimpleNamespace(identity="a", editor_hosts={})
    host = SimpleNamespace(
        _peerWorkspaces=SimpleNamespace(model=lambda identity: model),
        _PeerPaneKind=PaneKind,
        _PeerOperationResult=OperationResult,
        _PeerOperationStatus=OperationStatus,
    )
    record = {"session": {"layout": {"frames": [{}]}, "editor_views": {}}}
    result = MainWindow._restorePeerRuntimeLayout(host, runtime, record)
    assert not result.applied
    assert "mapping is incomplete" in result.detail
