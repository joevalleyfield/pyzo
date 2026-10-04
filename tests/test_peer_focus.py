from types import SimpleNamespace

import pytest
import pyzo
from pyzo.qt import QtWidgets
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


def focus_fixture(monkeypatch, *, peer=True):
    editors = SimpleNamespace(active=None)
    shells = SimpleNamespace(active=None, execution=object())
    editors.setActiveGroup = lambda host: setattr(editors, "active", host)
    shells.setActiveHost = lambda host: setattr(shells, "active", host)
    monkeypatch.setattr(pyzo, "editors", editors, raising=False)
    monkeypatch.setattr(pyzo, "shells", shells, raising=False)
    runtimes = {}
    activations = []
    for owner in ("a", "b"):
        docks = {kind: object() for kind in ("editor", "shell", "tool")}
        hosts = {kind: object() for kind in ("editor", "shell")}
        backend = SimpleNamespace(
            docks=docks,
            _pane_id_from_dock=lambda dock, docks=docks: next(
                kind for kind, value in docks.items() if value is dock
            ),
        )
        runtimes[owner] = SimpleNamespace(
            backend=backend,
            editor_hosts={hosts["editor"]: docks["editor"]},
            shell_hosts={hosts["shell"]: docks["shell"]},
            hosts=hosts,
        )
    host = SimpleNamespace(
        _peerFrameMode=peer,
        _peerRuntimeCatalog=SimpleNamespace(
            identities=tuple(runtimes), runtime=runtimes.__getitem__,
        ),
        _peerWorkspaces=SimpleNamespace(
            adopt_backend_activation=lambda owner, pane: activations.append((owner, pane))
        ),
        _projectPeerFrameMetadata=lambda dock: None,
        _editorHosts=runtimes["a"].editor_hosts,
        _shellHosts=runtimes["a"].shell_hosts,
    )
    return host, runtimes, editors, shells, activations


@pytest.mark.parametrize("owner", ["a", "b"])
@pytest.mark.parametrize("kind", ["editor", "shell", "tool"])
def test_peer_focus_routes_owner_without_changing_execution(monkeypatch, owner, kind):
    host, runtimes, editors, shells, activations = focus_fixture(monkeypatch)
    execution = shells.execution
    editors.active = old_editor = object()
    shells.active = old_shell = object()
    runtime = runtimes[owner]
    for _ in range(2):
        MainWindow._onFocusedDockWidgetChanged(host, runtime.backend.docks[kind])
        assert editors.active is (runtime.hosts["editor"] if kind == "editor" else old_editor)
        assert shells.active is (runtime.hosts["shell"] if kind == "shell" else old_shell)
        assert shells.execution is execution
    assert activations == [(owner, kind)] * 2


@pytest.mark.parametrize("dock", [None, object()])
def test_absent_or_retired_focus_preserves_context(monkeypatch, dock):
    host, _, editors, shells, activations = focus_fixture(monkeypatch)
    editors.active = old_editor = object()
    shells.active = old_shell = object()
    MainWindow._onFocusedDockWidgetChanged(host, dock)
    assert editors.active is old_editor
    assert shells.active is old_shell
    assert activations == []


@pytest.mark.parametrize("kind", ["editor", "shell"])
def test_legacy_focus_keeps_existing_host_route(monkeypatch, kind):
    host, runtimes, editors, shells, activations = focus_fixture(monkeypatch, peer=False)
    runtime = runtimes["a"]
    MainWindow._onFocusedDockWidgetChanged(host, runtime.backend.docks[kind])
    service = editors if kind == "editor" else shells
    assert service.active is runtime.hosts[kind]
    assert activations == []


def test_focus_sequence_tracks_last_owner_and_ignores_late_retired_dock(monkeypatch):
    host, runtimes, editors, shells, activations = focus_fixture(monkeypatch)
    for owner, kind in (("b", "shell"), ("a", "shell"), ("b", "editor")):
        MainWindow._onFocusedDockWidgetChanged(host, runtimes[owner].backend.docks[kind])
    MainWindow._onFocusedDockWidgetChanged(host, object())
    assert editors.active is runtimes["b"].hosts["editor"]
    assert shells.active is runtimes["a"].hosts["shell"]
    assert activations == [("b", "shell"), ("a", "shell"), ("b", "editor")]


@pytest.mark.parametrize("owner", ["a", "b"])
@pytest.mark.parametrize("descendant", [False, True])
def test_widget_focus_uses_owning_runtime(monkeypatch, owner, descendant):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    host, runtimes, _, _, _ = focus_fixture(monkeypatch)
    focused = []
    contents = []
    for identity, runtime in runtimes.items():
        content = QtWidgets.QWidget()
        contents.append(content)
        runtime.backend.docks = {
            "shell": SimpleNamespace(widget=lambda content=content: content)
        }
        runtime.backend._focused_dock_widget_changed = (
            lambda dock, identity=identity: focused.append((identity, dock))
        )
    host._peerBackend = runtimes["a"].backend
    projected = []
    host._onFocusedDockWidgetChanged = projected.append
    target = contents[0 if owner == "a" else 1]
    if descendant:
        target = QtWidgets.QWidget(target)
    try:
        MainWindow._activatePeerPaneForWidget(host, target)
        dock = runtimes[owner].backend.docks["shell"]
        assert focused == [(owner, dock)]
        assert projected == [dock]
        MainWindow._activatePeerPaneForWidget(host, None)
        MainWindow._activatePeerPaneForWidget(host, QtWidgets.QWidget())
        assert len(focused) == len(projected) == 1
    finally:
        for content in contents:
            content.deleteLater()
        app.sendPostedEvents()
