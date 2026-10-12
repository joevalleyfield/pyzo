"""Context-only changes must participate in immutable checkpoint generations."""

from copy import deepcopy
from types import SimpleNamespace as NS

import pyzo
from pyzo.util._locale import translate
from spikes.peer_frames.retention import RetentionCoordinator

pyzo.translate = translate
from pyzo.core.main import MainWindow


def test_context_only_checkpoint_captures_both_owners_without_document_edits(monkeypatch):
    documents = {"documents": {"shared": {"text": "unchanged"}}}
    profiles = {"a": {"config": {"zoom": 0}, "history": []},
                "b": {"config": {"zoom": 0}, "history": []}}
    monkeypatch.setattr(pyzo, "application", NS(documents=NS(
        persistence_snapshot=lambda: deepcopy(documents))), raising=False)
    monkeypatch.setattr(pyzo, "workspaceContext", NS(
        snapshot=lambda owner: deepcopy(profiles[owner])), raising=False)
    writes = []
    host = NS(_peerWorkspaces=NS(workspace_ids=("a", "b")))
    host._savePeerWorkspacePersistence = lambda docs, *, context_snapshot: writes.append(
        (deepcopy(docs), deepcopy(context_snapshot)))
    coordinator = RetentionCoordinator(
        clock=lambda: 0,
        capture=lambda: MainWindow._capturePeerRetentionSnapshot(host),
        writer=lambda snapshot, complete: MainWindow._writePeerRetentionSnapshot(
            host, snapshot, complete),
        schedule=lambda *_: None,
    )
    host._peerRetentionCoordinator = coordinator
    coordinator.edit()
    assert coordinator.flush()
    profiles["a"]["config"]["zoom"] = 2
    profiles["a"]["history"].append("owned_A")
    coordinator.edit()
    # Later mutations must not alter an already captured generation.
    profiles["b"]["config"]["zoom"] = -1
    assert coordinator.flush()
    assert len(writes) == 2
    assert writes[0][0] == writes[1][0] == documents
    assert writes[1][1]["a"] == profiles["a"]
    assert writes[1][1]["b"]["config"]["zoom"] == 0
    coordinator.edit()
    assert coordinator.flush()
    assert writes[2][1] == profiles
    coordinator.edit()
    assert coordinator.flush()
    assert len(writes) == 3
