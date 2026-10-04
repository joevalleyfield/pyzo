from types import SimpleNamespace

import pytest
import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


@pytest.mark.parametrize(
    "documents, initialized, expected",
    [
        ({"retained": {"text": "unsaved"}}, False, 0),
        ({}, True, 0),
        ({}, False, 1),
    ],
)
def test_modern_authority_prevents_legacy_editor_replay(
    monkeypatch, documents, initialized, expected
):
    restored = []
    monkeypatch.setattr(
        pyzo,
        "editors",
        SimpleNamespace(
            restoreEditorState=lambda: restored.append(True),
            groups=(),
        ),
        raising=False,
    )
    host = SimpleNamespace(
        _peerEditorLifecycleMigration=False,
        _peerWorkspaceIdentity="a",
        _peerPersistedDocumentResources={"schema": 1, "documents": documents},
        _peerWorkspacePersistenceLoad=SimpleNamespace(
            state=SimpleNamespace(
                workspaces={
                    "a": {"extensions": {"pyzo_peer_initialized": initialized}}
                },
            )
        ),
    )
    MainWindow._restoreLegacyEditorStateIntoPeerPanes(host)
    assert len(restored) == expected
    assert not host._peerEditorLifecycleMigration
