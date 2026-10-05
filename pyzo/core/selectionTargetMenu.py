"""Execution-target projection; callbacks retain the invoking live view."""

from pyzo.qt import QtGui, QtWidgets
from spikes.peer_frames.execution_routing import AssignmentMode


class SelectionTargetMenu(QtWidgets.QMenu):
    _hide_in_key_map_dialog = True

    def __init__(self, parent=None, *, service, current_editor):
        super().__init__("Execution target", parent)
        self.menuPath = getattr(parent, "menuPath", "run") + "__selection_target"
        self._service = service
        self._current_editor = current_editor
        self._choice_group = None
        self.aboutToShow.connect(self.refresh)

    def refresh(self):
        self.clear()
        if self._choice_group is not None:
            self._choice_group.deleteLater()
        self._choice_group = QtGui.QActionGroup(self)
        self._choice_group.setExclusive(True)
        service = self._service()
        self.menuAction().setVisible(service is not None)
        if service is None:
            return
        editor = self._current_editor()
        snapshot = service.selection_targets(editor)
        self.setTitle("Execution target")
        if snapshot.context is None:
            self.addAction("No live editor selected").setEnabled(False)
            return

        def choice(label, checked):
            action = self.addAction(label)
            action.setCheckable(True)
            action.setChecked(checked)
            self._choice_group.addAction(action)
            return action

        implicit = snapshot.assignment.mode is AssignmentMode.IMPLICIT
        action = choice("Workspace foreground", implicit)
        action.triggered.connect(
            lambda _checked=False: service.clear_selection_assignment(
                editor, context=snapshot.context
            )
        )
        self.addSeparator()
        for target in snapshot.choices:
            assigned = (
                snapshot.assignment.mode is AssignmentMode.EXPLICIT
                and snapshot.assignment.shell_id == target.identity
            )
            action = choice(target.label, assigned)
            action.triggered.connect(
                lambda _checked=False, shell=target.shell: service.assign_selection(
                    editor, shell, context=snapshot.context
                )
            )
            if assigned:
                self.setTitle("Execution target: " + target.label + " (assigned)")
        if snapshot.assignment.mode is AssignmentMode.BROKEN:
            self.setTitle("Execution target: assigned shell unavailable")
            action = choice(
                "Assigned shell unavailable: " + snapshot.assigned_label, True
            )
            action.setEnabled(False)
        elif not snapshot.choices:
            self.addAction("No eligible shells in this workspace").setEnabled(False)
