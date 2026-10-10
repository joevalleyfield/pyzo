"""No modal or user-file write may occur after peer close has committed."""

from types import SimpleNamespace

import pytest

import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
_app = None


@pytest.fixture
def editor_tabs():
    global _app
    from pyzo.qt import QtWidgets

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Menu's QKeySequence constants require an application before import.
    from pyzo.core import editorTabs

    return editorTabs


def host_and_editor(dirty=False):
    calls = []
    editor = SimpleNamespace(
        _name="saved.py",
        document=lambda: SimpleNamespace(isModified=lambda: dirty),
        close=lambda: calls.append("closed"),
    )
    item = SimpleNamespace(editor=editor, pinned=True)
    host = SimpleNamespace(
        _tabs=SimpleNamespace(
            items=lambda: [item], removeTab=lambda value: calls.append(value)
        ),
        _get_action_texts=lambda: ("Close", "Discard", "Cancel", "Save"),
        askToSaveFileIfDirty=lambda value: calls.append("dirty-prompt") or 1,
        updateBreakPoints=lambda: None,
    )
    return host, editor, calls


def test_committed_pinned_close_never_prompts(monkeypatch, editor_tabs):
    host, editor, calls = host_and_editor()

    def unexpected_prompt(*args):
        pytest.fail("a committed close opened a modal")

    monkeypatch.setattr(editor_tabs, "simpleDialog", unexpected_prompt)
    assert editor_tabs.EditorTabs.closeFile(host, editor, _peer_committed=True) == 1
    assert calls == [editor, "closed"]


def test_dirty_committed_close_refuses_before_mutation(editor_tabs):
    host, editor, calls = host_and_editor(dirty=True)
    with pytest.raises(AssertionError):
        editor_tabs.EditorTabs.closeFile(host, editor, _peer_committed=True)
    assert not calls


def test_uncommitted_pinned_cancel_keeps_legacy_prompt(monkeypatch, editor_tabs):
    host, editor, calls = host_and_editor()
    monkeypatch.setattr(editor_tabs, "simpleDialog", lambda *args: "Cancel")
    assert not editor_tabs.EditorTabs.closeFile(host, editor)
    assert calls == ["dirty-prompt"]
