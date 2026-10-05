"""Real QAction controls for the captured-view Selection chooser."""

from types import SimpleNamespace

import pytest

from pyzo.qt import QtWidgets
from spikes.peer_frames.execution_routing import AssignmentMode
from test_workspace_execution import world as execution_world  # noqa: F401  # shared routing fixture


@pytest.fixture
def menu_world(request):
    world = request.getfixturevalue("execution_world")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from pyzo.core.selectionTargetMenu import SelectionTargetMenu

    current = SimpleNamespace(editor=world.editors[0])
    menu = SelectionTargetMenu(
        service=lambda: world.service, current_editor=lambda: current.editor
    )
    menu.refresh()
    yield world, current, menu
    menu.deleteLater()
    app.sendPostedEvents()


def test_menu_lists_only_local_eligible_choices_with_unambiguous_checks(menu_world):
    world, _, menu = menu_world
    actions = menu.actions()
    assert actions[0].text() == "Workspace foreground"
    assert actions[0].isChecked()
    assert [action.text() for action in actions[2:]] == ["1. Shell", "2. Shell"]
    actions[2].trigger()
    menu.refresh()
    assert not menu.actions()[0].isChecked()
    assert menu.actions()[2].isChecked()
    assert sum(action.isChecked() for action in menu.actions()) == 1
    assert "assigned" in menu.title()
    assert (
        world.service.selection_targets(world.editors[0]).assignment.mode
        is AssignmentMode.EXPLICIT
    )


def test_action_addresses_editor_captured_before_focus_changes(menu_world):
    world, current, menu = menu_world
    action = menu.actions()[2]
    current.editor = world.editors[1]
    action.trigger()
    assert (
        world.service.selection_targets(world.editors[0]).assignment.mode
        is AssignmentMode.EXPLICIT
    )
    assert (
        world.service.selection_targets(world.editors[1]).assignment.mode
        is AssignmentMode.IMPLICIT
    )


def test_lost_assignment_is_checked_visible_and_can_be_cleared(menu_world):
    world, _, menu = menu_world
    menu.actions()[2].trigger()
    world.shells[0]._version = ""
    world.service.foreground_shell(world.shells[1])
    menu.refresh()
    assert "unavailable" in menu.title()
    broken = menu.actions()[-1]
    assert broken.isChecked() and not broken.isEnabled()
    assert "unavailable" in broken.text()
    assert not menu.actions()[0].isChecked()
    menu.actions()[0].trigger()
    menu.refresh()
    assert menu.actions()[0].isChecked()
    assert world.service.resolve_selection(world.editors[0]).resolved


def test_stale_popup_action_cannot_assign_a_transferred_view(menu_world):
    world, _, menu = menu_world
    action = menu.actions()[2]
    world.docs.move_view(world.editors[0]._peerViewIdentity, world.b)
    action.trigger()
    assert (
        world.service.selection_targets(world.editors[0]).assignment.mode
        is AssignmentMode.IMPLICIT
    )
    assert "captured editor view has changed" in world.messages[-1]


def test_no_editor_and_empty_owner_have_no_selectable_shells(menu_world):
    world, current, menu = menu_world
    current.editor = None
    menu.refresh()
    assert len(menu.actions()) == 1 and not menu.actions()[0].isEnabled()
    current.editor = world.editors[2]
    menu.refresh()
    assert menu.actions()[0].isEnabled() and menu.actions()[0].isChecked()
    assert not menu.actions()[2].isEnabled()


def test_legacy_service_has_no_selection_chooser():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from pyzo.core.selectionTargetMenu import SelectionTargetMenu

    menu = SelectionTargetMenu(service=lambda: None, current_editor=lambda: None)
    menu.refresh()
    assert not menu.menuAction().isVisible()
    menu.deleteLater()
    app.sendPostedEvents()


def test_native_menu_wrapper_hides_live_choices_from_shortcut_model(
    menu_world, monkeypatch
):
    import pyzo
    from pyzo.util._locale import translate

    pyzo.translate = translate
    from pyzo.core.menu import KeyMapModel, Menu
    from pyzo.qt import QtCore

    _, _, chooser = menu_world
    parent = Menu(name="Run")
    Menu.addMenu(parent, chooser)
    assert chooser.menuPath == "run__selection_target"
    model = KeyMapModel()
    model.setRootMenu(parent)
    index = model.index(0, 0, QtCore.QModelIndex())
    assert not model.hasChildren(index)
    monkeypatch.setattr(pyzo, "config", SimpleNamespace(shortcuts2={}), raising=False)
    shortcut = model.index(0, 1, QtCore.QModelIndex())
    assert model.data(shortcut, 0) == ""
    parent.deleteLater()
