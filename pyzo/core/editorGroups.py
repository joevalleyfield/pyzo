"""Document-level editor routing for the QtADS integration."""

from __future__ import annotations

from typing import Callable, Iterable

import pyzo
from pyzo.qt import QtCore


class EditorGroupManager(QtCore.QObject):
    """Present Pyzo's editor service while giving every document a host.

    A host is an ``EditorTabs`` with at most one file. QtADS owns all visible
    tabbing and splitting, so an editor can move independently without a
    second, nested tab layer.
    """

    currentChanged = QtCore.Signal()
    parserDone = QtCore.Signal()
    breakPointsChanged = QtCore.Signal(object)

    def __init__(
        self,
        groups: Iterable[object] = (),
        parent=None,
        group_factory: Callable[[], object] | None = None,
        group_remover: Callable[[object], None] | None = None,
        close_requester: Callable[[object], object] | None = None,
        retain_last_empty_group: bool = True,
    ):
        super().__init__(parent)
        self._groups = []
        self._activeGroup = None
        self._groupFactory = group_factory
        self._groupRemover = group_remover
        self._closeRequester = close_requester
        self._retainLastEmptyGroup = retain_last_empty_group
        for group in groups:
            self.addGroup(group)

    @property
    def groups(self):
        return tuple(self._groups)

    @property
    def activeGroup(self):
        return self._activeGroup

    def createGroup(self):
        if self._groupFactory is None:
            raise RuntimeError("no editor-group factory is installed")
        group = self._groupFactory()
        self.addGroup(group)
        return group

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
        if self._activeGroup is None:
            self._activeGroup = group
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

    def _group_for_editor(self, editor):
        for group in self._groups:
            if editor in group:
                return group
        return None

    def _group_for_file(self, filename):
        for group in self._groups:
            item = group._findEditorOfFile(filename)
            if item is not None:
                return group, item
        return None, None

    def _empty_or_new_group(self):
        if self._activeGroup is not None and not tuple(self._activeGroup):
            return self._activeGroup
        return self.createGroup()

    def getCurrentEditor(self):
        if self._activeGroup is None:
            return None
        return self._activeGroup.getCurrentEditor()

    def getMainEditor(self):
        for group in self._groups:
            editor = group.getMainEditor()
            if editor is not None:
                return editor
        return None

    def newFile(self):
        group = self._empty_or_new_group()
        item = group.newFile()
        self.setActiveGroup(group)
        return item

    def loadFile(self, filename, updateTabs=True, ignoreFail=False):
        if getattr(pyzo, "workspaceContext", None) is not None:
            from .workspaceContext import context_for

            return context_for(self._activeGroup).editors.loadFile(
                filename, updateTabs, ignoreFail
            )
        application = getattr(pyzo, "application", None)
        ensure_selection = getattr(application, "ensure_recovery_selection", None)
        if ensure_selection is not None and not ensure_selection(filename):
            return None
        group, item = self._group_for_file(filename)
        if item is not None:
            group._tabs.setCurrentItem(item)
            self.setActiveGroup(group)
            item.editor.setFocus()
            return item

        group = self._empty_or_new_group()
        item = group.loadFile(filename, updateTabs, ignoreFail)
        if item is not None:
            self.setActiveGroup(group)
        return item

    def closeFile(self, editor=None):
        group = self._activeGroup if editor is None else self._group_for_editor(editor)
        if group is None:
            return None
        editor = editor or group.getCurrentEditor()
        if self._closeRequester is not None and editor is not None:
            return self._closeRequester(editor)
        return self.closeFileCommitted(editor)

    def closeFileCommitted(self, editor=None, *, peer_committed=False):
        """Close after application lifecycle negotiation has committed."""

        group = self._activeGroup if editor is None else self._group_for_editor(editor)
        if group is None:
            return None
        result = (
            group.closeFile(editor, _peer_committed=True)
            if peer_committed
            else group.closeFile(editor)
        )
        self._remove_group_if_empty(group)
        return result

    def _remove_group_if_empty(self, group):
        if tuple(group) or (self._retainLastEmptyGroup and len(self._groups) <= 1):
            return
        index = self._groups.index(group)
        self._groups.remove(group)
        if self._activeGroup is group:
            self._activeGroup = (
                self._groups[min(index, len(self._groups) - 1)]
                if self._groups
                else None
            )
        if self._groupRemover is not None:
            self._groupRemover(group)
        self.currentChanged.emit()

    def __iter__(self):
        for group in self._groups:
            yield from group

    def __getattr__(self, name):
        # Active-document operations (including the find widget) retain the
        # shape expected throughout Pyzo.
        if name.startswith("__") or self._activeGroup is None:
            raise AttributeError(name)
        return getattr(self._activeGroup, name)

    def saveEditorState(self) -> None:
        if self._activeGroup is None:
            return
        from pyzo.core.workspaceContext import config_for

        fr = self._activeGroup._findReplace
        configuration = config_for(self._activeGroup)
        configuration.state.find_matchCase = fr._caseCheck.isChecked()
        configuration.state.find_regExp = fr._regExp.isChecked()
        configuration.state.find_wholeWord = fr._wholeWord.isChecked()
        configuration.state.find_autoHide = fr._autoHide.isChecked()
        configuration.state.find_show = fr.isVisible()
        if getattr(pyzo, "workspaceContext", None) is not None:
            # Peer views persist in the document registry. Keep the imported
            # legacy file list readable when this profile returns to QtADS or
            # upstream Pyzo, including after peer hosts have been torn down.
            return
        state = []
        for group in self._groups:
            state.extend(group._getCurrentOpenFilesAsSsdfList())
        pyzo.config.state.editorState2 = state

    def restoreEditorState(self) -> None:
        group = self._activeGroup or self.createGroup()
        group.restoreEditorState()
        items = list(group._tabs.items())
        for item in items[1:]:
            group._tabs.setCurrentItem(item)
            detached = group.detachCurrentItem()
            target = self.createGroup()
            target.attachItem(detached)
        if items:
            first_group, _ = self._group_for_file(items[0].id)
            if first_group is not None:
                self.setActiveGroup(first_group)

    def saveAllFiles(self) -> None:
        for group in self._groups:
            group.saveAllFiles()

    def closeAllFiles(self) -> None:
        for editor in tuple(self):
            self.closeFile(editor)

    def closeAll(self) -> bool:
        return all(group.closeAll() for group in tuple(self._groups))

    def updateBreakPoints(self, editor=None) -> None:
        if editor is not None:
            group = self._group_for_editor(editor)
            if group is not None:
                group.updateBreakPoints(editor)
            return
        for group in self._groups:
            group.updateBreakPoints()

    def setDebugLineIndicators(self, *filename_linenr) -> None:
        for group in self._groups:
            group.setDebugLineIndicators(*filename_linenr)

    def moveCurrentEditorToGroup(self, target_group):
        """Move the active document, retained for compatibility/testing."""
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
        # Documents now move through ADS directly. Keep the old command a
        # harmless focus-cycle until its menu entry is replaced.
        if len(self._groups) < 2:
            return self.getCurrentEditor()
        index = self._groups.index(self._activeGroup)
        self.setActiveGroup(self._groups[(index + 1) % len(self._groups)])
        return self.getCurrentEditor()


def install_editor_group_manager(
    groups: Iterable[object] = (),
    parent=None,
    group_factory=None,
    group_remover=None,
    close_requester=None,
    retain_last_empty_group=True,
):
    manager = EditorGroupManager(
        groups,
        parent,
        group_factory,
        group_remover,
        close_requester,
        retain_last_empty_group,
    )
    pyzo.editors = manager
    return manager
