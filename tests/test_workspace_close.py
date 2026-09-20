from types import SimpleNamespace

import pyzo

pyzo.translate = lambda _context, text, _disambiguation=None: text

from pyzo.core.main import MainWindow
from spikes.peer_frames.contract import OperationResult, OperationStatus
from spikes.peer_frames.identity import ApplicationIdentity, IdentityKind


def test_close_workspace_persistence_failure_preserves_runtime_before_commit():
    identity = ApplicationIdentity(IdentityKind.WORKSPACE, "workspace-a")

    class Model:
        panes = {"pane-a": object()}
        preflight_calls = 0
        discard_calls = 0
        commit_calls = 0

        def preflight_shutdown(self, _confirm):
            self.preflight_calls += 1
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
        _resolvePeerClose=lambda _votes: True,
        _savePeerWorkspacePersistence=fail_save,
    )

    result = MainWindow._closePeerWorkspace(window, "workspace-a")

    assert not result.applied
    assert "injected persistence failure" in result.detail
    assert model.preflight_calls == 1
    assert model.discard_calls == 1
    assert model.commit_calls == 0
    assert Workspaces.workspace_ids == (identity,)
