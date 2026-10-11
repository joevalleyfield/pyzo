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
