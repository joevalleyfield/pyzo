"""A local backend update cannot retire another loaded workspace's chrome."""

from types import SimpleNamespace as NS

import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


def test_settled_frame_update_retains_other_loaded_workspace_chrome():
    a, b = object(), object()
    runtimes = {"a": NS(frame_windows={"a-frame": a}), "b": NS(frame_windows={"b-frame": b})}
    status, menus = [], []
    catalog = NS(identities=("a", "b"), runtime=runtimes.__getitem__)
    host = NS(
        _peerFrameMode=True,
        _peerRuntimeCatalog=catalog,
        _peerStatusProjector=NS(reconcile=status.append),
        _peerCommandProjector=NS(reconcile=menus.append),
    )
    host._peerRuntimeFrameWindows = lambda: MainWindow._peerRuntimeFrameWindows(host)
    MainWindow._reconcilePeerChrome(host, (b,))
    assert status == [(a, b)] and menus == [(a, b)]
    catalog.identities = ("b",)
    MainWindow._reconcilePeerChrome(host, (b,))
    assert status[-1] == (b,) and menus[-1] == (b,)


def test_each_runtime_chrome_and_focus_are_watched_once():
    focus, settled, refreshed = [], [], []
    backend = NS(manager=NS(focusedDockWidgetChanged=NS(connect=focus.append)),
                 add_settled_callback=settled.append)
    host = NS(_onFocusedDockWidgetChanged=lambda *_: None,
              _reconcilePeerChrome=lambda *_: None,
              _peerCommandProjector=NS(dock_manager=None, refresh=lambda *_: refreshed.append(True)))
    runtime = NS(backend=backend)
    MainWindow._watchPeerRuntimeChrome(host, runtime)
    MainWindow._watchPeerRuntimeChrome(host, runtime)
    assert focus == [host._onFocusedDockWidgetChanged]
    assert settled == [host._reconcilePeerChrome]


def test_render_focus_does_not_reproject_affiliation_but_user_focus_does(monkeypatch):
    dock, group = object(), object()
    metadata, selection = [], []
    backend = NS(docks={"pane": dock}, _rendering=True, _closing=False,
                 _pane_id_from_dock=lambda _: "pane")
    runtime = NS(backend=backend, editor_hosts={group: dock}, shell_hosts={})
    host = NS(_peerFrameMode=True,
              _peerRuntimeCatalog=NS(identities=("a",), runtime=lambda _: runtime),
              _projectPeerFrameMetadata=metadata.append,
              _peerWorkspaces=NS(adopt_backend_activation=lambda *_: NS(applied=True)))
    monkeypatch.setattr(pyzo, "editors", NS(setActiveGroup=selection.append), raising=False)
    MainWindow._onFocusedDockWidgetChanged(host, dock)
    assert metadata == [] and selection == []
    backend._rendering = False
    MainWindow._onFocusedDockWidgetChanged(host, dock)
    assert metadata == [dock] and selection == [group]
    metadata.clear()
    selection.clear()
    host._peerWorkspaces.adopt_backend_activation = lambda *_: NS(applied=False)
    MainWindow._onFocusedDockWidgetChanged(host, dock)
    assert metadata == [] and selection == []
