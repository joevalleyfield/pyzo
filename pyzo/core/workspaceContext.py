"""Explicit workspace configuration and resource access for native consumers.

The legacy profile remains the import/compatibility archive. Peer workspaces
snapshot contextual data once, then publish independent profiles atomically
with their retained documents. No foreground swap mutates the global tree.
"""

from copy import deepcopy
import base64
import sys

import pyzo
from pyzo.qt import QtCore, QtWidgets
from pyzo.util import zon as ssdf


CAPABILITIES = {
    "settings.language",
    "settings.theme",
    "settings.qtscalefactor",
    "view.qtstyle",
}
APPLICATION = {
    "state.newUser",
    "advanced.useNativeFileDialogs",
    "advanced.alwaysAskWhenClosingPyzo",
    "settings.allowFloatingShell",
}
COMPATIBILITY = {
    "state.windowGeometry",
    "state.windowState",
    "state.views",
    "state.editorState2",
    "state.loadedTools",
    "state.peerWorkspaceSession",
    "tools.pyzointeractivehelp.noNewlines",
    "tools.pyzologger",
}


def owner_for(widget):
    """Resolve ancestry or durable view ownership; never consult foreign focus."""
    service = getattr(pyzo, "workspaceContext", None)
    current = widget
    while current is not None:
        view = getattr(current, "_peerViewIdentity", None)
        if view is not None and service is not None:
            try:
                return service.main._peerDocumentResources.view(view).workspace_id
            except KeyError:
                raise RuntimeError(
                    "The invoking document view is unavailable"
                ) from None
        owner = getattr(current, "_workspaceContextOwner", None)
        if owner is not None:
            return owner
        parent = getattr(current, "parent", None)
        current = parent() if callable(parent) else None
    return None


def context_for(widget=None):
    service = getattr(pyzo, "workspaceContext", None)
    if service is None:
        return pyzo
    owner = owner_for(widget)
    if owner is None:
        # Application menus/dialogs capture the invoking owner before dispatch.
        # Bare application entry points may explicitly address the active owner.
        owner = service.main._peerWorkspaces.active_workspace_id
    if owner is None:
        # With no loaded owner, the application already maintains an empty,
        # non-persisted bootstrap runtime for construction/default creation.
        # Its local services cannot expose an unavailable owner's resources.
        owner = service.main._peerRuntime.identity
    return service.context(owner)


def config_for(widget=None):
    return context_for(widget).config


def history_for(widget=None):
    return context_for(widget).command_history


def debugger_capability(shell):
    if shell is None:
        return "unavailable"
    value = getattr(shell, "_startup_info", {}).get("workspaceDebuggerSelection")
    return value if value in {"independent", "shared"} else "unavailable"


def plain(value):
    if isinstance(value, bytes):
        return {"__type__": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "keys"):
        return {str(key): plain(value[key]) for key in value.keys()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return deepcopy(value)


def struct(value):
    if isinstance(value, dict):
        if set(value) == {"__type__", "base64"} and value["__type__"] == "bytes":
            return base64.b64decode(value["base64"], validate=True)
        result = ssdf.new()
        for key, item in value.items():
            result[key] = struct(item)
        return result
    if isinstance(value, list):
        return [struct(item) for item in value]
    return deepcopy(value)


def scope(path):
    if path in CAPABILITIES:
        return "capability"
    if path in APPLICATION or path == "shortcuts2" or path.startswith("shortcuts2."):
        return "application"
    if path in COMPATIBILITY:
        return "compatibility"
    return "workspace"


class ConfigLayer:
    """A merged attribute/subscript view; writes retain workspace requests."""

    def __init__(self, local, application, prefix="", *, requested=False):
        object.__setattr__(self, "_local", local)
        object.__setattr__(self, "_application", application)
        object.__setattr__(self, "_prefix", prefix)
        object.__setattr__(self, "_requested", requested)

    def __getitem__(self, key):
        path = self._prefix + key
        ownership = scope(path)
        source = self._local
        if ownership in {"application", "compatibility"} or (
            ownership == "capability" and not self._requested
        ):
            source = self._application
        if key not in source:
            source = self._application if source is self._local else self._local
            if ownership == "workspace" and source is self._application:
                # Dynamic fields introduced after seeding still get a private
                # contextual snapshot rather than a mutable process alias.
                self._local[key] = deepcopy(source[key])
                source = self._local
        value = source[key]
        if hasattr(value, "keys") and key in self._local and key in self._application:
            return ConfigLayer(
                self._local[key],
                self._application[key],
                path + ".",
                requested=self._requested,
            )
        return value

    def __setitem__(self, key, value):
        source = (
            self._application
            if scope(self._prefix + key) in {"application", "compatibility"}
            else self._local
        )
        source[key] = value

    def __delitem__(self, key):
        source = (
            self._application
            if scope(self._prefix + key) in {"application", "compatibility"}
            else self._local
        )
        del source[key]

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value

    def __contains__(self, key):
        return key in self.keys()

    def keys(self):
        return list(
            dict.fromkeys(list(self._local.keys()) + list(self._application.keys()))
        )

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, AttributeError):
            return default

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


class LocalEditors(QtCore.QObject):
    currentChanged = QtCore.Signal()
    parserDone = QtCore.Signal()
    breakPointsChanged = QtCore.Signal(object)

    def __init__(self, context):
        super().__init__()
        self.context = context
        self._active = None
        pyzo.editors.currentChanged.connect(self._changed)
        pyzo.editors.parserDone.connect(self.parserDone)
        self._changed()

    @property
    def groups(self):
        try:
            return tuple(
                group
                for group in self.context.runtime.editor_hosts
                if group in pyzo.editors.groups
            )
        except KeyError:
            return ()

    @property
    def activeGroup(self):
        return self._active if self._active in self.groups else None

    def _changed(self):
        active = pyzo.editors.activeGroup
        if active in self.groups:
            self._active = active
        self.currentChanged.emit()

    def getCurrentEditor(self):
        return self.activeGroup.getCurrentEditor() if self.activeGroup else None

    def __iter__(self):
        for group in self.groups:
            yield from group

    def _group(self):
        group = self.activeGroup
        if group is None:
            group = self.context.service.main._createEditorHostForRuntime(
                self.context.runtime
            )
            pyzo.editors.addGroup(group)
            self._active = group
        return group

    def _invoke(self, name, editor=None, *args, **kwargs):
        group = (
            next((group for group in self.groups if editor in group), None)
            if editor is not None
            else self.activeGroup
        )
        return (
            getattr(group, name)(editor, *args, **kwargs)
            if group is not None
            else False
        )

    def saveFile(self, editor=None, *args, **kwargs):
        return self._invoke(
            "saveFile", editor or self.getCurrentEditor(), *args, **kwargs
        )

    def saveFileAs(self, editor=None):
        return self._invoke("saveFileAs", editor or self.getCurrentEditor())

    def reloadFile(self, editor=None):
        return self._invoke("reloadFile", editor or self.getCurrentEditor())

    def openFile(self):
        return self._group().openFile()

    @property
    def _findReplace(self):
        editors = self

        class Find:
            def __getattr__(self, name):
                def invoke(*args, **kwargs):
                    group = editors.activeGroup
                    if group is not None:
                        return getattr(group._findReplace, name)(*args, **kwargs)

                return invoke

        return Find()

    def loadFile(self, filename, *args, **kwargs):
        # The global manager deduplicates across owners. A local tool must open
        # a local view of the shared document instead of activating a foreign one.
        for group in self.groups:
            item = group._findEditorOfFile(filename)
            if item is not None:
                self._active = group
                pyzo.editors.setActiveGroup(group)
                item.editor.setFocus()
                return item
        group = self.context.service.main._createEditorHostForRuntime(
            self.context.runtime
        )
        pyzo.editors.addGroup(group)
        self._active = group
        item = group.loadFile(filename, *args, **kwargs)
        pyzo.editors.setActiveGroup(group)
        return item

    def newFile(self):
        group = self.context.service.main._createEditorHostForRuntime(
            self.context.runtime
        )
        pyzo.editors.addGroup(group)
        self._active = group
        item = group.newFile()
        pyzo.editors.setActiveGroup(group)
        return item

    def closeFile(self, editor=None):
        editor = editor or self.getCurrentEditor()
        if editor is not None and editor in tuple(self):
            return pyzo.editors.closeFile(editor)

    def closeAllFiles(self):
        for editor in tuple(self):
            self.closeFile(editor)

    def saveAllFiles(self):
        for editor in tuple(self):
            self.saveFile(editor)

    def moveCurrentEditorToNextGroup(self):
        groups = self.groups
        if groups:
            index = groups.index(self.activeGroup) if self.activeGroup in groups else -1
            self._active = groups[(index + 1) % len(groups)]
            pyzo.editors.setActiveGroup(self._active)

    def setActiveGroup(self, group):
        if group not in self.groups:
            raise ValueError("Editor group belongs to another workspace")
        self._active = group
        pyzo.editors.setActiveGroup(group)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        group = self.activeGroup
        if group is None:
            raise AttributeError(name)
        return getattr(group, name)


class LocalShells(QtCore.QObject):
    currentShellChanged = QtCore.Signal()
    currentShellStateChanged = QtCore.Signal()
    executionShellChanged = QtCore.Signal()

    def __init__(self, context):
        super().__init__()
        self.context = context
        self._current = None
        pyzo.shells.currentShellChanged.connect(self._changed)
        pyzo.shells.currentShellStateChanged.connect(self._state)
        self._changed()

    def getShells(self):
        try:
            return [
                shell
                for host in self.context.runtime.shell_hosts
                for shell in host.getShells()
            ]
        except KeyError:
            return []

    def __iter__(self):
        return iter(self.getShells())

    def getCurrentShell(self):
        # Execution service remembers the selected local target including a
        # broken target. It must not choose an older host when that target dies.
        service = getattr(pyzo.application, "workspace_execution", None)
        if service is not None:
            shell = service._foreground.get(self.context.owner)
            if shell is None:
                return None
            service.sync_shell(shell)
            binding = service._shells.get(shell)
            if (
                binding is None
                or not service._owner_is_current(binding)
                or not service.router.shell_state(binding.identity).eligible
            ):
                return None
            return shell
        return self._current if self._current in self.getShells() else None

    def getSelectedShell(self):
        """The local native view, including a shell still starting/restarting."""
        return self._current if self._current in self.getShells() else None

    def _changed(self):
        shell = pyzo.shells.getCurrentShell()
        if shell in self.getShells():
            self._current = shell
        self.currentShellChanged.emit()

    def _state(self):
        self.currentShellStateChanged.emit()

    def getShellAt(self, index):
        return self.getShells()[index]

    def addShell(self, info=None):
        host = self.context.service.main._createShellHostForRuntime(
            self.context.runtime
        )
        pyzo.shells._register_host(host)
        pyzo.shells.setActiveHost(host)
        return host.addShell(info)

    def getExecutionShell(self):
        return self.getCurrentShell()

    def setExecutionShell(self, shell):
        if shell not in self.getShells():
            raise ValueError("Shell belongs to another workspace")
        pyzo.shells.setExecutionShell(shell)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        shell = self.getCurrentShell()
        host = next(
            (
                host
                for host in self.context.runtime.shell_hosts
                if shell in host.getShells()
            ),
            None,
        )
        if host is None:
            raise AttributeError(name)
        return getattr(host, name)


class WorkspaceContext:
    """Pyzo facade with fixed owner; code/libraries remain application shared."""

    def __init__(self, service, owner):
        self.service, self.owner = service, owner
        self.editors = LocalEditors(self)
        self.shells = LocalShells(self)

    @property
    def runtime(self):
        return self.service.main._peerRuntimeCatalog.runtime(self.owner)

    @property
    def config(self):
        self.runtime  # reject removed owners before returning mutable state
        return ConfigLayer(self.service.profiles[self.owner], pyzo.config)

    @property
    def requested(self):
        return ConfigLayer(
            self.service.profiles[self.owner], pyzo.config, requested=True
        )

    @property
    def command_history(self):
        return self.service.histories[self.owner]

    @property
    def toolManager(self):
        return pyzo.toolManager.for_workspace(self.owner)

    def __getattr__(self, name):
        return getattr(pyzo, name)


class WorkspaceContexts:
    def __init__(self, main, legacy_history):
        self.main = main
        self.profiles, self.histories, self.contexts = {}, {}, {}
        self.legacy_history = legacy_history
        self._seed = plain(pyzo.config)

    def context(self, owner):
        self.main._peerRuntimeCatalog.runtime(owner)
        if owner not in self.contexts:
            self.ensure(owner)
            self.contexts[owner] = WorkspaceContext(self, owner)
        return self.contexts[owner]

    def ensure(self, owner):
        if owner in self.profiles:
            return
        from .history import CommandHistory

        record = self.main._peerWorkspacePersistenceLoad.state.workspaces.get(
            str(owner), {}
        )
        profile = record.get("profile", {})
        data = profile.get("contextual", {})
        # Preserve unknown and old data in the parent profile. Defaults/site/user
        # overlays seed contextual values once; subsequent edits never propagate.
        seed = deepcopy(self._seed)
        self._merge(seed, data.get("config", {}))
        self.profiles[owner] = struct(seed)
        history = CommandHistory(None)
        history._commands = list(
            data.get(
                "history",
                self.legacy_history.get_commands()
                if owner == self.main._peerWorkspaceIdentity
                else [],
            )
        )
        self.histories[owner] = history

    @classmethod
    def _merge(cls, target, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    def snapshot(self, owner):
        self.ensure(owner)
        context = self.contexts.get(owner)
        group = context.editors.activeGroup if context is not None else None
        if group is not None:
            find = group._findReplace
            state = self.profiles[owner].state
            state.find_matchCase = find._caseCheck.isChecked()
            state.find_regExp = find._regExp.isChecked()
            state.find_wholeWord = find._wholeWord.isChecked()
            state.find_autoHide = find._autoHide.isChecked()
            state.find_show = find.isVisible()
        managers = getattr(pyzo.toolManager, "managers", {})
        manager = managers.get(owner)
        record = self.main._peerWorkspacePersistenceLoad.state.workspaces.get(
            str(owner), {}
        )
        states = deepcopy(
            record.get("profile", {}).get("contextual", {}).get("tool_states", {})
        )
        if manager is not None:
            states.update(manager.capture_states())
        loaded_tools = (
            manager.getLoadedTools()
            if manager is not None
            else record.get("session", {}).get("loaded_tools", [])
        )
        snapshot = deepcopy(record.get("profile", {}).get("contextual", {}))
        snapshot.update(
            schema=1,
            config=plain(self.profiles[owner]),
            history=self.histories[owner].get_commands(),
            tool_states=states,
            loaded_tools=list(loaded_tools),
        )
        return snapshot

    def copy(self, source, destination):
        self.ensure(source)
        self.ensure(destination)
        self.profiles[destination] = struct(plain(self.profiles[source]))
        self.histories[destination]._commands = self.histories[source].get_commands()
        source_manager = pyzo.toolManager.for_workspace(source)
        destination_manager = pyzo.toolManager.for_workspace(destination)
        destination_manager.states = source_manager.capture_states()
        destination_manager.reloadTools()
        self.histories[destination].commands_reset.emit()
        if destination in self.contexts:
            self.apply_preferences(destination)

    def capabilities(self, owner):
        context = self.context(owner)
        result = {}
        for path in sorted(CAPABILITIES):
            section, key = path.split(".")
            result[path] = {
                "requested": getattr(getattr(context.requested, section), key),
                "effective": getattr(getattr(context.config, section), key),
                "scope": "application",
            }
        result["settings.debuggerSelection"] = {
            "requested": context.requested.settings.debuggerSelection,
            "effective": debugger_capability(context.shells.getCurrentShell()),
            "scope": "interpreter",
        }
        return result

    def apply_preferences(self, owner):
        context = self.context(owner)
        configuration = context.config
        editors = list(context.editors)
        controls = editors + list(context.shells)
        logger = context.toolManager.getTool("pyzologger")
        if logger is not None and hasattr(logger, "_logger_shell"):
            controls.append(logger._logger_shell)
        view_setters = {
            "fontname": "setFont",
            "zoom": "setZoom",
            "showWhitespace": "setShowWhitespace",
            "highlightMatchingBracket": "setHighlightMatchingBracket",
        }
        setting_setters = {
            "autoIndent": "setAutoIndent",
            "autoComplete_acceptKeys": "setAutoCompletionAcceptKeysFromStr",
            "autoComplete_caseSensitive": "setAutocompleteCaseSensitive",
            "autoComplete_minChars": "setAutocompleteMinChars",
            "autoClose_Quotes": "setAutoClose_Quotes",
            "autoClose_Brackets": "setAutoClose_Brackets",
        }
        for control in controls:
            for key, setter in view_setters.items():
                getattr(control, setter)(getattr(configuration.view, key))
            for key, setter in setting_setters.items():
                getattr(control, setter)(getattr(configuration.settings, key))
            control.setAutocompletPopupSize(*configuration.view.autoComplete_popupSize)
            control._calmedIntrospectNow.set_delay(configuration.advanced.autoCompDelay)
            if control in context.shells:
                control.setMaximumBlockCount(configuration.advanced.shellMaxLines)
        for editor in editors:
            for key, setter in {
                "wrap": "setWrap",
                "showLineEndings": "setShowLineEndings",
                "showIndentationGuides": "setShowIndentationGuides",
                "highlightCurrentLine": "setHighlightCurrentLine",
                "edgeColumn": "setLongLineIndicatorPosition",
            }.items():
                getattr(editor, setter)(getattr(configuration.view, key))
        for dock in context.runtime.backend.docks.values():
            layout = (
                QtWidgets.QWidget.layout(dock.widget())
                if dock.widget() is not None
                else None
            )
            if layout is not None:
                margin = configuration.view.widgetMargin
                layout.setContentsMargins(margin, margin, margin, margin)

    def retire(self, owner):
        context = self.contexts.pop(owner, None)
        if context is not None:
            for signal, callback in (
                (pyzo.editors.currentChanged, context.editors._changed),
                (pyzo.editors.parserDone, context.editors.parserDone),
                (pyzo.shells.currentShellChanged, context.shells._changed),
                (pyzo.shells.currentShellStateChanged, context.shells._state),
            ):
                signal.disconnect(callback)
        pyzo.toolManager.managers.pop(owner, None)
        prefix = "pyzo.tools._workspace_" + str(owner).replace("-", "_")
        for key in tuple(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                del sys.modules[key]
