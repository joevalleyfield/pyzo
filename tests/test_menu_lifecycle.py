"""Retired menus stop observing shared application state before Qt deletion."""

from types import SimpleNamespace as NS

import pyzo
from pyzo.qt import QtCore, QtGui, QtWidgets
from pyzo.util._locale import translate
from shiboken6 import delete

pyzo.translate = translate
_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from pyzo.core.menu import Menu
from pyzo.core.shellCommands import ShellCommandProjector


class Mapper(QtCore.QObject):
    keyMappingChanged = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.calls = []

    def setShortcut(self, action):
        self.calls.append(action)


def test_retired_menu_disconnects_keymap_and_unregisters_shell_actions(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    mapper = Mapper()
    service = NS(stateChanged=mapper.keyMappingChanged, is_enabled=lambda *args: True)
    projector = ShellCommandProjector(service)
    monkeypatch.setattr(pyzo, "keyMapper", mapper, raising=False)
    monkeypatch.setattr(pyzo, "shellCommandProjector", projector, raising=False)
    menu = Menu(None, "Retired")
    action = menu._addAction("Action", None)
    provider_calls = []
    projector._register(action, "test", lambda: provider_calls.append("provider"))
    menu.dispose()
    menu.dispose()  # Retirement is idempotent while Qt deletion is deferred.
    assert not projector._projections
    before = (len(mapper.calls), len(provider_calls))
    mapper.keyMappingChanged.emit()
    assert (len(mapper.calls), len(provider_calls)) == before
    delete(menu)
    mapper.keyMappingChanged.emit()
    assert (len(mapper.calls), len(provider_calls)) == before


def test_deleted_action_never_resolves_its_retired_shell_provider():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    mapper = Mapper()
    service = NS(stateChanged=mapper.keyMappingChanged, is_enabled=lambda *args: True)
    projector = ShellCommandProjector(service)
    action = QtGui.QAction()

    def retired_provider():
        raise AssertionError("resolved a retired workspace")

    projector._projections.append((action, "test", retired_provider))
    delete(action)
    projector.refresh()
    assert not projector._projections


def test_native_parent_deletion_disconnects_menu_without_explicit_retirement(monkeypatch):
    mapper = Mapper()
    monkeypatch.setattr(pyzo, "keyMapper", mapper, raising=False)
    frame = QtWidgets.QWidget()
    bar = QtWidgets.QMenuBar(frame)
    menu = Menu(bar, "Native")
    menu._addAction("Action", None)
    before = len(mapper.calls)
    delete(frame)
    assert not menu._externalConnections
    mapper.keyMappingChanged.emit()
    assert len(mapper.calls) == before


def test_retirement_rejects_callback_already_queued_by_qt():
    mapper = Mapper()
    menu = Menu(None, "Queued")
    calls = []
    menu._connectExternal(
        mapper.keyMappingChanged, lambda: calls.append("called"),
        QtCore.Qt.ConnectionType.QueuedConnection,
    )
    mapper.keyMappingChanged.emit()
    menu.dispose()
    _app.processEvents()
    assert not calls
    delete(menu)
