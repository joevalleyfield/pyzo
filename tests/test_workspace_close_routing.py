from types import SimpleNamespace

import pytest
import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


@pytest.mark.parametrize("owner", ["a", "b"])
@pytest.mark.parametrize("entry", ["editor", "pane", "frame"])
def test_close_routes_the_addressed_owner(monkeypatch, owner, entry):
    calls = []
    runtimes = {}
    success = SimpleNamespace(applied=True)
    for identity in ("a", "b"):
        dock = object()
        group = object()
        runtimes[identity] = SimpleNamespace(
            identity=identity,
            editor_hosts={group: dock},
            backend=SimpleNamespace(
                _pane_id_from_dock=lambda _dock, identity=identity: identity + "-pane"
            ),
            model=SimpleNamespace(
                frames={identity + "-frame": object()},
                request_close_pane=lambda pane, resolver, identity=identity: (
                    calls.append((identity, pane)) or success
                ),
                request_close_frame=lambda frame, resolver, identity=identity: (
                    calls.append((identity, frame)) or success
                ),
            ),
        )
    runtime = runtimes[owner]
    group = next(iter(runtime.editor_hosts))
    monkeypatch.setattr(
        pyzo,
        "editors",
        SimpleNamespace(_group_for_editor=lambda editor: group),
        raising=False,
    )
    host = SimpleNamespace(
        _editorHosts=runtimes["a"].editor_hosts,
        _peerBackend=runtimes["a"].backend,
        _peerWorkspace=runtimes["a"].model,
        _peerRuntimeCatalog=SimpleNamespace(
            identities=tuple(runtimes), runtime=runtimes.__getitem__
        ),
        _peerWorkspaces=SimpleNamespace(
            pane_owner=lambda pane: pane[0],
            model=lambda identity: runtimes[identity].model,
        ),
        _peerRuntimeForEditorHost=lambda group: runtime,
        _resolvePeerEditorClose=object(),
        _resolvePeerClose=object(),
        _afterPeerTopologyClose=lambda result: None,
    )
    if entry == "editor":
        assert MainWindow._requestPeerEditorClose(host, object()) == 1
    elif entry == "pane":
        assert MainWindow._requestPeerPaneClose(host, owner + "-pane").applied
    else:
        assert MainWindow._requestPeerFrameClose(host, owner + "-frame").applied
    assert calls == [(owner, owner + ("-frame" if entry == "frame" else "-pane"))]
