"""A resumed peer's current view membership overrides historical retention."""

from types import SimpleNamespace
from copy import deepcopy

import pytest

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


@pytest.mark.parametrize(
    "saved_tool,current_tool,semantic",
    [
        ("tool-local", "tool-local", False),
        ("tool-local", "tool-owner-local", False),
        ("tool-owner-local", "tool-local", False),
        ("historical-native-id", "tool-owner-local", True),
    ],
)
def test_mixed_editor_tool_restore_maps_every_local_pane_and_prunes_old_shell(
    saved_tool, current_tool, semantic
):
    from spikes.peer_frames.contract import (
        Direction,
        OperationResult,
        OperationStatus,
        PaneDescriptor,
        PaneKind,
        WorkspaceModel,
    )
    from spikes.peer_frames.fake_backend import FakeBackend
    from spikes.peer_frames.workspace_session import WorkspaceSessionService

    saved = WorkspaceModel(FakeBackend())
    for pane_id, kind in (
        ("old-editor", PaneKind.EDITOR),
        (saved_tool, PaneKind.TOOL),
        ("old-shell", PaneKind.SHELL),
    ):
        assert saved.register_pane(PaneDescriptor(pane_id, kind, pane_id)).applied
    assert saved.create_frame("old-editor", "saved-frame").applied
    assert saved.create_frame(saved_tool, "tool-start-frame").applied
    assert saved.split(saved_tool, "old-editor", Direction.BELOW).applied
    assert saved.create_frame("old-shell", "terminated-frame").applied
    current = WorkspaceModel(FakeBackend())
    for pane_id, kind in (
        ("new-editor", PaneKind.EDITOR),
        (current_tool, PaneKind.TOOL),
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
        backend=SimpleNamespace(
            _pane_id_from_dock=lambda dock: "new-editor",
            docks={current_tool: SimpleNamespace(_toolId="local")},
        ),
    )
    main = SimpleNamespace(
        _peerWorkspaces=SimpleNamespace(model=lambda owner: current),
        _PeerPaneKind=PaneKind,
        _PeerOperationResult=OperationResult,
        _PeerOperationStatus=OperationStatus,
        _workspaceSessionFor=lambda runtime: WorkspaceSessionService(
            current, lambda: {}, lambda: ["local"], lambda tools: None
        ),
    )
    result = MainWindow._restorePeerRuntimeLayout(
        main,
        runtime,
        {
            "session": {
                "schema": 1,
                "loaded_tools": ["local"],
                "layout": saved.serialize_layout(),
                "editor_views": {"view": "old-editor"},
                **({"tool_panes": {"local": saved_tool}} if semantic else {}),
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
    } == {"new-editor", current_tool}


@pytest.mark.parametrize(
    "session, expected",
    [
        ({}, None),
        ({"loaded_tools": []}, []),
        ({"loaded_tools": ["owner-tool"]}, ["owner-tool"]),
    ],
)
def test_saved_tool_set_is_exact_or_legacy_default(session, expected):
    calls = []
    host = SimpleNamespace(
        _peerWorkspacePersistenceLoad=SimpleNamespace(
            state=SimpleNamespace(workspaces={"owner": {"session": session}})
        ),
        _restoreToolsForWorkspace=lambda owner, values: calls.append((owner, values)),
    )
    MainWindow._restoreWorkspaceTools(host, "owner")
    assert calls == ([] if expected is None else [("owner", expected)])
