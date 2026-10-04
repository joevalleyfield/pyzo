from types import SimpleNamespace

import pytest
import pyzo
from pyzo.core.editorGroups import EditorGroupManager


@pytest.mark.parametrize("allowed", [False, True])
def test_file_open_checks_recovery_before_creating_an_editable_view(
    monkeypatch, allowed
):
    events = []
    monkeypatch.setattr(
        pyzo,
        "application",
        SimpleNamespace(
            ensure_recovery_selection=lambda path: (
                events.append(("choice", path)) or allowed
            ),
        ),
        raising=False,
    )
    group = SimpleNamespace(loadFile=lambda *args: events.append(("load", args[0])))
    manager = SimpleNamespace(
        _group_for_file=lambda path: (None, None),
        _empty_or_new_group=lambda: events.append(("create",)) or group,
    )
    assert EditorGroupManager.loadFile(manager, "saved.py") is None
    expected = [("choice", "saved.py")]
    if allowed:
        expected += [("create",), ("load", "saved.py")]
    assert events == expected


def test_legacy_file_open_does_not_require_a_peer_recovery_service(monkeypatch):
    monkeypatch.setattr(pyzo, "application", None, raising=False)
    loaded = []
    manager = SimpleNamespace(
        _group_for_file=lambda path: (None, None),
        _empty_or_new_group=lambda: SimpleNamespace(
            loadFile=lambda *args: loaded.append(args[0])
        ),
    )
    assert EditorGroupManager.loadFile(manager, "healthy.py") is None
    assert loaded == ["healthy.py"]
