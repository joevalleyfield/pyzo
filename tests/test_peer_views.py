from types import SimpleNamespace

import pyzo
from pyzo.core.views import ViewManager


MISSING = object()


class Session:
    def __init__(self):
        self.snapshot = {"schema": 1, "value": "normal"}
        self.restored = []

    def capture(self):
        return dict(self.snapshot)

    def restore(self, snapshot):
        self.restored.append(snapshot)
        return SimpleNamespace(applied=True, detail="")


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


def test_peer_named_views_store_and_apply_complete_workspace_sessions():
    old_config = getattr(pyzo, "config", MISSING)
    old_session = getattr(pyzo, "workspaceSession", MISSING)
    try:
        session = Session()
        pyzo.workspaceSession = session
        pyzo.config = SimpleNamespace(
            state=State(
                views=[],
                peerWorkspaceSession={"schema": 1, "value": "startup"},
            )
        )
        manager = ViewManager()

        session.snapshot = {"schema": 1, "value": "named"}
        manager.saveView("Focus")
        session.snapshot = {"schema": 1, "value": "normal-before-switch"}
        assert manager.selectView("Focus")

        assert session.restored == [{"schema": 1, "value": "named"}]
        assert pyzo.config.state.peerWorkspaceSession == {
            "schema": 1,
            "value": "normal-before-switch",
        }
        assert manager.getCurrentViewName() == "Focus"
    finally:
        _restore_global("config", old_config)
        _restore_global("workspaceSession", old_session)


def test_legacy_named_view_is_rejected_without_changing_active_view():
    old_config = getattr(pyzo, "config", MISSING)
    old_session = getattr(pyzo, "workspaceSession", MISSING)
    try:
        session = Session()
        pyzo.workspaceSession = session
        pyzo.config = SimpleNamespace(
            state=State(
                views=[
                    {
                        "name": "Legacy",
                        "windowGeometry": "old",
                        "windowState": "old",
                        "loadedTools": [],
                    }
                ],
                peerWorkspaceSession={"schema": 1, "value": "startup"},
            )
        )
        manager = ViewManager()

        assert not manager.selectView("Legacy")
        assert manager.getCurrentViewName() == ViewManager.NORMAL_VIEW_NAME
        assert session.restored == []
    finally:
        _restore_global("config", old_config)
        _restore_global("workspaceSession", old_session)


def _restore_global(name, value):
    if value is MISSING:
        delattr(pyzo, name)
    else:
        setattr(pyzo, name, value)
