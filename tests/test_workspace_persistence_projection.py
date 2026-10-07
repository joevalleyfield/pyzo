"""A resumed peer's current view membership overrides historical retention."""

from types import SimpleNamespace
from copy import deepcopy

import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


def test_foreign_resume_and_close_replace_membership_in_both_directions():
    main = SimpleNamespace(_peerWorkspaceIdentity="bootstrap")
    entry = dict(workspace_id="peer", document_id="shared")
    previous = dict(schema=1, views={}, retained_views={"view": entry}, documents={})
    current = dict(schema=1, views={"view": entry}, retained_views={}, documents={})
    before = deepcopy((previous, current))
    resumed = MainWindow._mergePeerResourcePersistence(main, previous, current)
    assert resumed["views"] == {"view": entry} and not resumed["retained_views"]
    assert (previous, current) == before
    closed = MainWindow._mergePeerResourcePersistence(main, resumed, previous)
    assert not closed["views"] and closed["retained_views"] == {"view": entry}


def test_mixed_editor_tool_restore_maps_every_local_pane_and_prunes_old_shell():
    from spikes.peer_frames.contract import (
        Direction,
        OperationResult,
        OperationStatus,
        PaneDescriptor,
        PaneKind,
        WorkspaceModel,
    )
    from spikes.peer_frames.fake_backend import FakeBackend

    saved = WorkspaceModel(FakeBackend())
    for pane_id, kind in (
        ("old-editor", PaneKind.EDITOR),
        ("tool-local", PaneKind.TOOL),
        ("old-shell", PaneKind.SHELL),
    ):
        assert saved.register_pane(PaneDescriptor(pane_id, kind, pane_id)).applied
    assert saved.create_frame("old-editor", "saved-frame").applied
    assert saved.create_frame("tool-local", "tool-start-frame").applied
    assert saved.split("tool-local", "old-editor", Direction.BELOW).applied
    assert saved.create_frame("old-shell", "terminated-frame").applied
    current = WorkspaceModel(FakeBackend())
    for pane_id, kind in (
        ("new-editor", PaneKind.EDITOR),
        ("tool-local", PaneKind.TOOL),
    ):
        assert current.register_pane(PaneDescriptor(pane_id, kind, pane_id)).applied

    class Group:
        _tabs = SimpleNamespace(
            currentItem=lambda: SimpleNamespace(
                editor=SimpleNamespace(_peerViewIdentity="view")
            )
        )

    runtime = SimpleNamespace(
        identity="owner",
        editor_hosts={Group(): "dock"},
        backend=SimpleNamespace(_pane_id_from_dock=lambda dock: "new-editor"),
    )
    main = SimpleNamespace(
        _peerWorkspaces=SimpleNamespace(model=lambda owner: current),
        _PeerPaneKind=PaneKind,
        _PeerOperationResult=OperationResult,
        _PeerOperationStatus=OperationStatus,
    )
    result = MainWindow._restorePeerRuntimeLayout(
        main,
        runtime,
        {
            "session": {
                "layout": saved.serialize_layout(),
                "editor_views": {"view": "old-editor"},
            }
        },
    )
    assert result.applied, result
    layout = current.serialize_layout()
    assert [frame["id"] for frame in layout["frames"]] == ["saved-frame"]
    assert {
        pane
        for frame in layout["frames"]
        for group in frame["groups"]
        for pane in group["panes"]
    } == {"new-editor", "tool-local"}
