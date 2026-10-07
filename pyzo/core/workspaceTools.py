"""Workspace tool instances; undeclared extensions are refused locally."""

import ast
import importlib.util
import os
import sys
from copy import deepcopy
from types import ModuleType

import pyzo
from pyzo.qt import QtWidgets
from pyzo.tools import ToolManager
from .applicationController import ToolWorkspaceAccess


BUILTINS = {
    "pyzohistoryviewer",
    "pyzoworkspace",
    "pyzosnippets",
    "pyzoeditorlist",
    "pyzosourcestructure",
    "pyzologger",
    "pyzowebbrowser",
    "pyzoexpressionviewer",
    "pyzointeractivehelp",
    "pyzofilebrowser",
}


class ScopedToolManager(ToolManager):
    def __init__(self, service, owner):
        self.service, self.owner = service, owner
        self._workspaceContextOwner = owner
        runtime = service.main._peerRuntimeCatalog.runtime(owner)
        workspace = ToolWorkspaceAccess(
            dock_manager=runtime.backend.manager,
            add_dock=lambda dock, target=None: service.main._addWorkspaceToolDock(
                owner, dock, target
            ),
            close_dock=service.main.removePeerToolDock,
            peer_mode=True,
        )
        workspace.context_owner = owner
        super().__init__(workspace)
        self.failures = {}
        record = service.main._peerWorkspacePersistenceLoad.state.workspaces.get(
            str(owner), {}
        )
        self.states = deepcopy(
            record.get("profile", {}).get("contextual", {}).get("tool_states", {})
        )

    def capture_states(self):
        for tool_id, dock in self._activeTools.items():
            tool = dock.widget()
            state = deepcopy(self.states.get(tool_id, {}))
            if tool_id == "pyzoexpressionviewer" and hasattr(tool, "_em"):
                state["expressions"] = {
                    key: value[0] for key, value in tool._em.getExpressions().items()
                }
            if (
                tool_id == "pyzoworkspace"
                and hasattr(tool, "_tree")
                and hasattr(tool._tree, "_proxy")
            ):
                state["namespace"] = tool._tree._proxy._name
            if tool_id == "pyzointeractivehelp" and hasattr(tool, "_history"):
                state.update(
                    history=list(tool._history),
                    index=tool._histindex,
                    text=tool._text.text(),
                    paused=tool._pauseBut.isChecked(),
                )
            if (
                tool_id == "pyzowebbrowser"
                and hasattr(tool, "_view")
                and hasattr(tool._view, "_url")
            ):
                state.update(
                    url=tool._view._url,
                    back=list(tool._view._history),
                    forward=list(tool._view._history2),
                )
            if tool_id == "pyzosnippets" and hasattr(tool, "_searchText"):
                state["search"] = tool._searchText.text()
            self.states[tool_id] = state
        return deepcopy(self.states)

    def loadTool(self, toolId, splitWith=None):
        self.capture_states()
        super().loadTool(toolId, splitWith)
        tool = self.getTool(toolId)
        if tool is None:
            return
        state = self.states.get(toolId, {})
        if toolId == "pyzoexpressionviewer" and hasattr(tool, "_em"):
            for text, enabled in state.get("expressions", {}).items():
                tool._em.addExpression(text, enabled)
            tool._updateTree()
            tool._sendExpressionsToProxy()
        if (
            toolId == "pyzoworkspace"
            and hasattr(tool, "_tree")
            and hasattr(tool._tree, "_proxy")
        ):
            tool._tree._proxy.setName(state.get("namespace", ""))
        if toolId == "pyzointeractivehelp" and hasattr(tool, "_history"):
            tool._history = list(state.get("history", []))
            tool._histindex = state.get("index", 0)
            tool._text.setText(state.get("text", ""))
            tool._pauseBut.setChecked(state.get("paused", False))
        if (
            toolId == "pyzowebbrowser"
            and hasattr(tool, "_view")
            and hasattr(tool._view, "_url")
        ):
            tool._view._url = state.get("url", "")
            tool._view._history = list(state.get("back", []))
            tool._view._history2 = list(state.get("forward", []))
            tool._address.setEditText(tool._view._url)
        if toolId == "pyzosnippets" and hasattr(tool, "_searchText"):
            tool._searchText.setText(state.get("search", ""))

    def onToolClose(self, toolId):
        self.capture_states()
        super().onToolClose(toolId)

    def getToolClass(self, toolId):
        if self._toolInfo is None:
            self.loadToolInfo()
        description = next((tool for tool in self._toolInfo if tool.id == toolId), None)
        if description is None:
            return self._failure(toolId, "Tool installation is unavailable")
        path = description.modulePath
        filename = path if path.endswith(".py") else os.path.join(path, "__init__.py")
        builtin = toolId in BUILTINS and os.path.commonpath(
            (
                os.path.realpath(filename),
                os.path.realpath(os.path.join(pyzo.pyzoDir, "tools")),
            )
        ) == os.path.realpath(os.path.join(pyzo.pyzoDir, "tools"))
        if not builtin:
            try:
                declarations = ast.parse(open(filename, encoding="utf-8").read()).body
                declared = any(
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "workspace_api"
                        for target in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == 1
                    for node in declarations
                )
            except Exception as error:
                return self._failure(toolId, str(error))
            if not declared:
                return self._failure(
                    toolId,
                    "This tool has not declared workspace_api = 1. Its configuration is retained. Use legacy mode or update the tool to accept an explicit context.",
                )
        # Each owner has a private module tree. Relative imports in package
        # tools remain relative to that owner; reloading A cannot rebind B.
        prefix = "pyzo.tools._workspace_" + str(self.owner).replace("-", "_")
        if prefix not in sys.modules:
            package = ModuleType(prefix)
            package.__path__ = []
            sys.modules[prefix] = package
        module_name = prefix + "." + description.moduleName
        for key in tuple(sys.modules):
            if key == module_name or key.startswith(module_name + "."):
                del sys.modules[key]
        try:
            spec = importlib.util.spec_from_file_location(module_name, filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            context = self.service.context(self.owner)
            for key, dependency in tuple(sys.modules.items()):
                if key == module_name or key.startswith(module_name + "."):
                    if hasattr(dependency, "pyzo"):
                        dependency.pyzo = context
            cls = next(
                value for name, value in vars(module).items() if name.lower() == toolId
            )
            if not isinstance(cls, type) or not issubclass(cls, QtWidgets.QWidget):
                raise TypeError("Tool class must inherit QWidget")
            if not builtin:

                class ContextTool(cls):
                    def __init__(self, parent):
                        super().__init__(parent, context=context)

                return ContextTool
            return cls
        except Exception as error:
            return self._failure(toolId, str(error))

    def _failure(self, toolId, detail):
        self.failures[toolId] = detail

        class UnavailableTool(QtWidgets.QLabel):
            def __init__(self, parent):
                super().__init__(f"{toolId} unavailable\n{detail}", parent)
                self.setWordWrap(True)
                self.setAccessibleDescription(detail)

        return UnavailableTool


class WorkspaceToolCatalog:
    """Compatibility surface explicitly resolves application menu context."""

    def __init__(self, service):
        self.service = service
        self.managers = {}

    def for_workspace(self, owner):
        self.service.main._peerRuntimeCatalog.runtime(owner)
        if owner not in self.managers:
            self.managers[owner] = ScopedToolManager(self.service, owner)
        return self.managers[owner]

    def _active(self):
        owner = self.service.main._peerWorkspaces.active_workspace_id
        if owner is None:
            raise RuntimeError("No available workspace for tools")
        return self.for_workspace(owner)

    def __getattr__(self, name):
        return getattr(self._active(), name)

    def getLoadedTools(self):
        return (
            self._active().getLoadedTools()
            if self.service.main._peerWorkspaces.active_workspace_id is not None
            else []
        )

    def getTool(self, toolId):
        return (
            self._active().getTool(toolId)
            if self.service.main._peerWorkspaces.active_workspace_id is not None
            else None
        )
