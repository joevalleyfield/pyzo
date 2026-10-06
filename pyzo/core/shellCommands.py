"""Application-owned shell commands and Qt action projections."""

from __future__ import annotations

from dataclasses import dataclass
import pyzo

from pyzo.qt import QtCore, QtGui


@dataclass(frozen=True)
class ShellCommandDescriptor:
    id: str
    text: object
    icon: QtGui.QIcon | None
    target: str
    operation: str


class ShellCommandService(QtCore.QObject):
    """Own shell command behavior and dynamic state without owning widgets."""

    stateChanged = QtCore.Signal()

    def __init__(self, shells, editors, icons, translate, parent=None):
        super().__init__(parent)
        self._shells = shells
        self._editors = editors
        self._descriptors = self._build_descriptors(icons, translate)
        shells.currentShellChanged.connect(self.stateChanged)
        shells.currentShellStateChanged.connect(self.stateChanged)
        shells.executionShellChanged.connect(self.stateChanged)

    @property
    def ordinary_command_ids(self):
        return tuple(
            command_id
            for command_id in self._descriptors
            if command_id.startswith("shell.")
            and not command_id.startswith("shell.debug.")
        )

    @property
    def debug_command_ids(self):
        return tuple(
            command_id
            for command_id in self._descriptors
            if command_id.startswith("shell.debug.control.")
        )

    def descriptor(self, command_id):
        return self._descriptors[command_id]

    def _debug_target(self, shell=None):
        if getattr(getattr(pyzo, "application", None), "workspace_debugging", None):
            return shell or self._shells.getCurrentShell()
        return self._shells.getExecutionShell()

    def is_enabled(self, command_id, shell=None):
        descriptor = self.descriptor(command_id)
        if descriptor.target == "current":
            return shell is not None
        if descriptor.target == "execution":
            target = self._debug_target(shell)
            if target is None:
                return False
            debugmode = self._shells._debugmode
            debugging = getattr(
                getattr(pyzo, "application", None), "workspace_debugging", None
            )
            if debugging:
                execution = getattr(debugging, "execution", None)
                if execution is not None:
                    from spikes.peer_frames.execution_routing import ExecutionCommand

                    if not execution.resolve_shell(
                        target, ExecutionCommand.DEBUGGER
                    ).resolved:
                        return False
                debugmode = (target._debugState or {}).get("debugmode", 0)
            if descriptor.operation == "STOP":
                return debugmode > 0
            if descriptor.operation == "START":
                return debugmode == 0
            return debugmode == 2
        return True

    def invoke(self, command_id, shell=None):
        descriptor = self.descriptor(command_id)
        if not self.is_enabled(command_id, shell):
            return False
        if descriptor.target == "current":
            getattr(shell, descriptor.operation)()
            return True
        if descriptor.target == "breakpoints":
            debugging = getattr(
                getattr(pyzo, "application", None), "workspace_debugging", None
            )
            if debugging is not None:
                debugging.clear(pyzo.application.workspaces.active_identity)
                return True
            for editor in self._editors:
                editor.clearBreakPoints()
            return True

        target = self._debug_target(shell)
        command = descriptor.operation
        if command == "JUMP":
            editor = self._editors.getCurrentEditor()
            debugging = getattr(
                getattr(pyzo, "application", None), "workspace_debugging", None
            )
            if debugging is not None:
                view = debugging.view(editor)
                binding = debugging.execution._shells.get(target)
                if (
                    view is None
                    or binding is None
                    or view.workspace_id != binding.workspace_id
                ):
                    return False
            line_number = (
                editor.textCursor().blockNumber() + 1 if editor is not None else -1
            )
            command = f"JUMP {line_number}"
        from .nativeRequests import execution_service

        service = execution_service()
        if service is None:
            target.executeCommand(f"DB {command}\n")
            return True
        from spikes.peer_frames.execution_routing import ExecutionCommand

        result = service.resolve_shell(target, ExecutionCommand.DEBUGGER)
        return result.resolved and service.dispatch(
            result.provenance, lambda shell: shell.executeCommand(f"DB {command}\n")
        )

    @staticmethod
    def _build_descriptors(icons, translate):
        def descriptor(command_id, text, icon, target, operation):
            return ShellCommandDescriptor(
                command_id,
                translate("menu", text),
                icon,
                target,
                operation,
            )

        items = (
            descriptor(
                "shell.clear",
                "Clear screen ::: Clear the screen.",
                icons.application_eraser,
                "current",
                "clearScreen",
            ),
            descriptor(
                "shell.interrupt",
                "Interrupt ::: Interrupt the current running code "
                "(does not work for extension code).",
                icons.application_lightning,
                "current",
                "interrupt",
            ),
            descriptor(
                "shell.pause",
                "Pause ::: Pause the current running code.",
                icons.application_pause,
                "current",
                "pause",
            ),
            descriptor(
                "shell.restart",
                "Restart ::: Terminate and restart the interpreter.",
                icons.application_refresh,
                "current",
                "restart",
            ),
            descriptor(
                "shell.close",
                "Close ::: Terminate the interpreter and close the shell.",
                icons.cancel,
                "current",
                "closeShell",
            ),
            descriptor(
                "shell.debug.clear_breakpoints",
                "Clear all 0 breakpoints",
                icons.bug_delete,
                "breakpoints",
                "clear",
            ),
            descriptor(
                "shell.debug.start",
                "Postmortem: debug from last traceback",
                icons.bug_delete,
                "execution",
                "START",
            ),
            descriptor(
                "shell.debug.control.next",
                "Debug next: proceed until next line",
                icons.debug_next,
                "execution",
                "NEXT",
            ),
            descriptor(
                "shell.debug.control.step",
                "Debug step into: proceed one step",
                icons.debug_step,
                "execution",
                "STEP",
            ),
            descriptor(
                "shell.debug.control.return",
                "Debug return: proceed until returns",
                icons.debug_return,
                "execution",
                "RETURN",
            ),
            descriptor(
                "shell.debug.control.jump",
                "Debug jump: set the next line to be executed ::: "
                "Sets the next line to be executed to the current selected "
                "line in the editor.",
                icons.arrow_in,
                "execution",
                "JUMP",
            ),
            descriptor(
                "shell.debug.control.continue",
                "Debug continue: proceed to next breakpoint",
                icons.debug_continue,
                "execution",
                "CONTINUE",
            ),
            descriptor(
                "shell.debug.control.stop",
                "Stop debugging",
                icons.debug_quit,
                "execution",
                "STOP",
            ),
        )
        return {item.id: item for item in items}


class ShellCommandProjector(QtCore.QObject):
    """Project shell commands into independent menu and toolbar actions."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service
        self._projections = []
        service.stateChanged.connect(self.refresh)

    def menu_action(self, menu, command_id, shell_provider=lambda: None):
        descriptor = self._service.descriptor(command_id)
        action = menu.addItem(
            descriptor.text,
            descriptor.icon,
            lambda: self._service.invoke(command_id, shell_provider()),
        )
        self._register(action, command_id, shell_provider)
        return action

    def toolbar_action(self, toolbar, command_id, shell_provider=lambda: None):
        descriptor = self._service.descriptor(command_id)
        action = QtGui.QAction(descriptor.icon, str(descriptor.text), toolbar)
        if hasattr(descriptor.text, "tt"):
            action.setStatusTip(descriptor.text.tt)
            action.setToolTip(descriptor.text.tt)
        action.triggered.connect(
            lambda checked=False: self._service.invoke(command_id, shell_provider())
        )
        toolbar.addAction(action)
        self._register(action, command_id, shell_provider)
        return action

    def refresh(self):
        live = []
        for action, command_id, shell_provider in self._projections:
            try:
                action.setEnabled(
                    self._service.is_enabled(command_id, shell_provider())
                )
            except (AttributeError, RuntimeError):
                continue
            live.append((action, command_id, shell_provider))
        self._projections = live

    def _register(self, action, command_id, shell_provider):
        self._projections.append((action, command_id, shell_provider))
        self.refresh()
