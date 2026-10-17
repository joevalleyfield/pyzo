from types import SimpleNamespace as NS

import pytest
import pyzo
from pyzo.qt import QtWidgets
from shiboken6 import delete
from pyzo.core.focus import focus_widget


@pytest.fixture
def target(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    events = []

    class Window(QtWidgets.QWidget):
        def show(self): events.append('show')
        def showNormal(self): events.append('restore')
        def raise_(self): events.append('raise')
        def activateWindow(self): events.append('activate')

    class Content(QtWidgets.QLineEdit):
        def setFocus(self, reason): events.append('focus')

    window = Window()
    widget = Content(window)
    widget._workspaceContextOwner = 'a'
    main = NS(_peerWorkspaces=NS(active_workspace_id='a'),
              _externalFileOpenAllowed=lambda: True)
    monkeypatch.setattr(pyzo, 'workspaceContext', NS(main=main), raising=False)
    yield window, widget, main, events
    delete(window)


def test_peer_focus_activates_the_target_window_before_content(target):
    window, widget, _, events = target
    assert focus_widget(widget)
    assert events == ['show', 'raise', 'activate', 'focus']


def test_peer_focus_restores_a_minimized_target(target, monkeypatch):
    window, widget, _, events = target
    monkeypatch.setattr(window, 'isMinimized', lambda: True)
    assert focus_widget(widget)
    assert events == ['restore', 'raise', 'activate', 'focus']


@pytest.mark.parametrize('condition', ['foreign', 'unowned', 'closing'])
def test_background_or_retired_context_never_activates_or_focuses(target, condition):
    _, widget, main, events = target
    if condition == 'foreign': main._peerWorkspaces.active_workspace_id = 'b'
    elif condition == 'unowned': del widget._workspaceContextOwner
    else: main._externalFileOpenAllowed = lambda: False
    assert not focus_widget(widget)
    assert events == []


def test_legacy_focus_does_not_reorder_windows(target, monkeypatch):
    _, widget, _, events = target
    monkeypatch.setattr(pyzo, 'workspaceContext', None)
    assert focus_widget(widget)
    assert events == ['focus']


def test_deleted_target_is_ignored(target):
    _, widget, _, events = target
    delete(widget)
    assert not focus_widget(widget)
    assert events == []


def test_retired_view_identity_is_ignored(target, monkeypatch):
    from pyzo.core import workspaceContext
    _, widget, _, events = target
    def retired(_widget):
        raise RuntimeError("The invoking document view is unavailable")
    monkeypatch.setattr(workspaceContext, 'owner_for', retired)
    assert not focus_widget(widget)
    assert events == []
