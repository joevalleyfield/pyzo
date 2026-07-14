"""Editor-group routing for the multi-area editor experiment."""

from __future__ import annotations

from typing import Iterable

import pyzo
from pyzo.qt import QtCore


class EditorGroupManager(QtCore.QObject):
    """Keep Pyzo's single-editor API while routing it to one active group.

    Existing Pyzo code talks to ``pyzo.editors``. This facade keeps that public
    shape while allowing the main window to host several ``EditorTabs``
    instances. Methods that operate on one editor delegate to the active group;
    persistence and bulk operations cover every group.
    """

    currentChanged = QtCore.Signal()
    parserDone = QtCore.Signal()
    breakPointsChanged = QtCore.Signal(object)

    def __init__(self, groups: Iterable[object], parent=None):
        super().__init__(parent)
        self._groups = list(groups)
        if not self._groups:
            raise ValueError("at least one editor group is required")
        self._activeGroup = self._groups[0]

        for group in self._groups:
            group.currentChanged.connect(
                lambda group=group: self._activate_from_group(group)
            )
            group.parserDone.connect(self.parserDone)
            group.breakPointsChanged.connect(self._on_break_points_changed)

    @property
    def groups(self):
        return tuple(self._groups)

    @property
    def activeGroup(self):
        return self._activeGroup

    def setActiveGroup(self, group) -> None:
        if group not in self._groups:
            raise ValueError("unknown editor group")
        if self._activeGroup is group:
            return
        self._activeGroup = group
        self.currentChanged.emit()

    def addGroup(self, group) -> None:
        if group in self._groups:
            return
        self._groups.append(group)
        group.currentChanged.connect(
            lambda group=group: self._activate_from_group(group)
        )
        group.parserDone.connect(self.parserDone)
        group.breakPointsChanged.connect(self._on_break_points_changed)

    def _activate_from_group(self, group) -> None:
        self.setActiveGroup(group)
        self.currentChanged.emit()

    def _on_break_points_changed(self, _break_points) -> None:
        self.breakPointsChanged.emit(self._collect_break_points())

    def _collect_break_points(self):
        result = {}
        for group in self._groups:
            result.update(group._breakPoints)
        return result

    def getCurrentEditor(self):
        return self._activeGroup.getCurrentEditor()

    def getMainEditor(self):
        return self._activeGroup.getMainEditor()

    def __iter__(self):
        for group in self._groups:
            yield from group

    def __getattr__(self, name):
        # Keep existing Pyzo callers working for active-group operations and
        # private compatibility attributes such as _tabs and _findReplace.
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._activeGroup, name)

    def saveEditorState(self) -> None:
        for group in self._groups:
            group.saveEditorState()

    def restoreEditorState(self) -> None:
        for group in self._groups:
            group.restoreEditorState()

    def saveAllFiles(self) -> None:
        for group in self._groups:
            group.saveAllFiles()

    def closeAllFiles(self) -> None:
        for group in self._groups:
            group.closeAllFiles()

    def closeAll(self) -> bool:
        return all(group.closeAll() for group in self._groups)

    def updateBreakPoints(self, editor=None) -> None:
        if editor is not None:
            for group in self._groups:
                if editor in group:
                    group.updateBreakPoints(editor)
                    return
            return
        for group in self._groups:
            group.updateBreakPoints()

    def setDebugLineIndicators(self, *filename_linenr) -> None:
        for group in self._groups:
            group.setDebugLineIndicators(*filename_linenr)

    def moveCurrentEditorToGroup(self, target_group):
        """Move the active document, preserving its editor and FileItem."""

        if target_group not in self._groups:
            raise ValueError("unknown editor group")
        source_group = self._activeGroup
        if source_group is target_group:
            return source_group.getCurrentEditor()

        item = source_group.detachCurrentItem()
        if item is None:
            return None
        target_group.attachItem(item)
        self.setActiveGroup(target_group)
        return item.editor

    def moveCurrentEditorToNextGroup(self):
        """Move the active document to the next editor area, cyclically."""

        if len(self._groups) < 2:
            return self.getCurrentEditor()
        index = self._groups.index(self._activeGroup)
        target_group = self._groups[(index + 1) % len(self._groups)]
        return self.moveCurrentEditorToGroup(target_group)


def install_editor_group_manager(groups: Iterable[object], parent=None):
    manager = EditorGroupManager(groups, parent)
    pyzo.editors = manager
    return manager
