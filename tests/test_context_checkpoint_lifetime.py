"""Checkpoint callbacks obey exit ownership, including queued delivery."""

from types import SimpleNamespace as NS

import pytest
import pyzo

pyzo.translate = lambda _context, text, _disambiguation=None: text
from pyzo.core.main import MainWindow
from pyzo.qt import QtCore, QtWidgets
from spikes.peer_frames.application_lifecycle import (
    ApplicationExitDisposition as Disposition,
    ApplicationShutdownCoordinator,
    CleanupStep,
)

_app = None


def checkpoint_world(monkeypatch):
    calls = []
    main = NS(
        _peerApplicationExitRequested=False,
        _peerClosingWorkspace=False,
        _peerWorkspaces=NS(workspace_ids=("A",)),
        _peerContextSnapshot=None,
        _peerRetentionCoordinator=NS(edit=lambda: calls.append("edit")),
        _refreshWorkspaceAffiliation=lambda: calls.append("affiliation"),
        _peerStatusProjector=NS(refresh=lambda: calls.append("status")),
    )
    contexts = NS(
        snapshot=lambda owner: {"config": {"x": 1}},
        apply_preferences=lambda owner: calls.append("preferences"),
    )
    monkeypatch.setattr(pyzo, "workspaceContext", contexts, raising=False)
    return main, contexts, calls


@pytest.mark.parametrize("workspace_closing", [False, True])
@pytest.mark.parametrize("exit_requested", [False, True])
def test_checkpoint_suspends_owner_work(monkeypatch, workspace_closing, exit_requested):
    main, contexts, calls = checkpoint_world(monkeypatch)
    main._peerClosingWorkspace = workspace_closing
    main._peerApplicationExitRequested = exit_requested
    if exit_requested:
        def retired(owner):
            raise AssertionError("accessed disposed owner")
        contexts.snapshot = retired
    MainWindow._checkpointPeerContext(main)
    assert calls == ([] if workspace_closing or exit_requested else
                     ["preferences", "edit", "affiliation", "status"])


@pytest.mark.parametrize("transition", ["exit", "workspace-close", "owner-removed"])
def test_checkpoint_rechecks_after_snapshot_callbacks(monkeypatch, transition):
    main, contexts, calls = checkpoint_world(monkeypatch)
    def snapshot(owner):
        if transition == "exit":
            main._peerApplicationExitRequested = True
        elif transition == "workspace-close":
            main._peerClosingWorkspace = True
        else:
            main._peerWorkspaces.workspace_ids = ()
        return {"config": {"x": 1}}
    contexts.snapshot = snapshot
    MainWindow._checkpointPeerContext(main)
    assert calls == []


def test_checkpoint_rechecks_after_preferences_callback(monkeypatch):
    main, contexts, calls = checkpoint_world(monkeypatch)
    def apply(owner):
        calls.append("preferences")
        main._peerApplicationExitRequested = True
    contexts.apply_preferences = apply
    MainWindow._checkpointPeerContext(main)
    assert calls == ["preferences"]
    assert main._peerContextSnapshot is None


def test_checkpoint_stops_capture_before_next_retired_owner(monkeypatch):
    main, contexts, calls = checkpoint_world(monkeypatch)
    main._peerWorkspaces.workspace_ids = ("A", "B")
    def snapshot(owner):
        calls.append(owner)
        assert owner == "A", "captured B after A's callback began teardown"
        main._peerApplicationExitRequested = True
        return {"config": {"x": 1}}
    contexts.snapshot = snapshot
    MainWindow._checkpointPeerContext(main)
    assert calls == ["A"]


def test_checkpoint_unrelated_failure_remains_visible(monkeypatch):
    main, contexts, calls = checkpoint_world(monkeypatch)
    def fail(owner):
        raise ValueError("unrelated snapshot failure")
    contexts.snapshot = fail
    with pytest.raises(ValueError, match="unrelated snapshot failure"):
        MainWindow._checkpointPeerContext(main)
    assert calls == []


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("disposition", list(Disposition))
@pytest.mark.parametrize("scenario", ["cancel", "persistence-error", "workspace-cancel",
                                    "negotiation-error", "completed", "cleanup-error",
                                    "replace-error", "reentrant"])
def test_exit_timer_ownership(monkeypatch, active, disposition, scenario):
    global _app
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    main, contexts, calls = checkpoint_world(monkeypatch)
    timer = main._peerContextTimer = QtCore.QTimer()
    if active:
        timer.start(1000)
    observed = []
    def fail():
        raise RuntimeError("collaborator failure")
    def prepare():
        observed.append(timer.isActive())
        if scenario == "persistence-error":
            fail()
    def shutdown():
        if scenario == "negotiation-error":
            fail()
        return scenario != "workspace-cancel"
    def cleanup():
        observed.append(timer.isActive())
        # A callback queued before stop must still respect exit ownership.
        MainWindow._checkpointPeerContext(main)
        if scenario == "cleanup-error":
            fail()
        if scenario == "reentrant":
            assert MainWindow._requestPeerApplicationExit(main, disposition).value == "coalesced"
    def replace():
        if scenario == "replace-error":
            fail()
    coordinator = main._peerShutdownCoordinator = ApplicationShutdownCoordinator(
        confirm_exit=lambda _: scenario != "cancel",
        prepare_cleanup=prepare,
        request_workspace_shutdown=shutdown,
        cleanup_steps=(CleanupStep("checkpoint", cleanup),),
        finish_quit=lambda code: None,
        replace_process=replace,
        log_error=lambda message: None,
    )
    MainWindow._requestPeerApplicationExit(main, disposition)
    resumed = coordinator.phase.value == "idle"
    assert main._peerApplicationExitRequested is (not resumed)
    assert timer.isActive() is (active and resumed)
    assert not any(observed)
    assert calls == []
    if resumed:
        MainWindow._checkpointPeerContext(main)
        assert calls == ["preferences", "edit", "affiliation", "status"]
    timer.stop()
