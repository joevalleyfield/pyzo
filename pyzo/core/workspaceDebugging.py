"""Native projections of durable overlays and workspace-owned debug sessions."""

from copy import deepcopy
import os

from spikes.peer_frames.breakpoint_overlays import BreakpointOverlays


def debug_token(shell):
    state = getattr(shell, "_debugState", {}) or {}
    return (
        getattr(shell, "_nativeTransportEpoch", None),
        state.get("session_id"),
        state.get("sequence"),
        state.get("debugmode", 0),
    )


class WorkspaceDebugging:
    def __init__(
        self, documents, execution, editors, changed, open_source, show_message
    ):
        self.documents, self.execution, self.editors = documents, execution, editors
        self.overlays = BreakpointOverlays(documents.registry)
        self.changed, self.open_source, self.show_message = (
            changed,
            open_source,
            show_message,
        )
        self._projecting = False

    def view(self, editor):
        if not self.documents.is_bound(editor):
            return None
        try:
            return self.documents.registry.view(self.documents.view_identity(editor))
        except KeyError:
            return None

    def bind(self, editor):
        if not getattr(editor, "_workspaceBreakpointsBound", False):
            editor._workspaceBreakpointsBound = True
            editor.breakPointsChanged.connect(lambda *_: self.editor_changed(editor))
        self.project_editor(editor)

    def project_editor(self, editor):
        view = self.view(editor)
        if view is None:
            return
        desired = self.overlays.entries(view.workspace_id, view.document_id)
        if tuple(sorted(editor.breakPoints(triState=True))) == desired:
            return
        self._projecting = True
        try:
            for line, _ in tuple(editor.breakPoints(triState=True)):
                editor.toggleBreakpoint(line, triState=False)
            for line, enabled in desired:
                editor.toggleBreakpoint(line)
                if not enabled:
                    editor.toggleBreakpoint(line)
        finally:
            self._projecting = False

    def editor_changed(self, editor):
        if self._projecting:
            return
        view = self.view(editor)
        if view is None:
            return
        self.overlays.replace(
            view.workspace_id, view.document_id, editor.breakPoints(triState=True)
        )
        for peer in tuple(self.documents._editors):
            peer_view = self.view(peer)
            if peer_view and (peer_view.workspace_id, peer_view.document_id) == (
                view.workspace_id,
                view.document_id,
            ):
                self.project_editor(peer)
        self.changed()
        for shell, binding in tuple(self.execution._shells.items()):
            if binding.workspace_id == view.workspace_id:
                self.send_breakpoints(shell)

    def sources(self, owner):
        return {
            editor.filename or editor.id(): view.document_id
            for editor in tuple(self.documents._editors)
            if (view := self.view(editor)) and view.workspace_id == owner
        }

    def send_breakpoints(self, shell):
        binding = self.execution._shells.get(shell)
        if binding is None:
            return
        breaks = getattr(shell, "_workspaceBreakpointSnapshot", None)
        if breaks is None:
            breaks = self.project(binding.workspace_id)
        shell._stat_breakpoints.send(deepcopy(breaks))

    def project(self, owner):
        breaks = self.overlays.project(owner)
        for name, identity in self.sources(owner).items():
            resource = self.documents.registry.resource(identity)
            if resource.path is None and resource.available:
                lines = [
                    line
                    for line, enabled in self.overlays.entries(owner, identity)
                    if enabled
                ]
                if lines:
                    breaks[name] = lines
        return breaks

    def before_operation(self, shell, receipt, kind, payload):
        binding = self.execution._shells.get(shell)
        if binding is None:
            return {}
        state = shell._debugState or {}
        mode = state.get("debugmode", 0)
        fields = {
            "debug_session_id": state.get("session_id"),
            "debug_sequence": state.get("sequence"),
        }
        if not mode:
            fields = {}
            shell._workspaceSessionSources = (
                deepcopy(getattr(shell, "_workspaceTracebackSources", {}))
                if kind == "command" and payload.upper().startswith("DB START")
                else self.sources(binding.workspace_id)
            )
            if not (
                kind == "command"
                and payload.upper().startswith("DB ")
                and not payload.upper().startswith("DB START")
            ):
                shell._workspaceBreakpointSnapshot = self.project(binding.workspace_id)
                shell._workspaceBreakpointOperation = receipt["operation_id"]
                self.send_breakpoints(shell)
        return fields

    def finished(self, shell, message):
        if message["phase"] == "traceback":
            shell._workspaceTracebackReceipt = deepcopy(message["receipt"])
            shell._workspaceTracebackSources = deepcopy(
                getattr(shell, "_workspaceSessionSources", {})
            )
        elif message["phase"] == "complete" and message["receipt"][
            "operation_id"
        ] == getattr(shell, "_workspaceBreakpointOperation", None):
            shell._workspaceBreakpointSnapshot = None
            self.send_breakpoints(shell)

    def accept_debug(self, shell, message):
        self.execution._synchronize()
        if message.get("epoch") != shell._nativeTransportEpoch:
            return False
        state = message.get("state")
        if not isinstance(state, dict):
            return False
        origin = state.get("origin")
        if origin is None:
            return not state.get("debugmode") and not getattr(
                shell, "_workspaceDebugSequence", 0
            )
        binding = self.execution._shells.get(shell)
        if binding is None or not self.execution._owner_is_current(binding):
            return False
        current = self.execution.router.shell_state(binding.identity)
        expected = {
            "workspace_id": str(binding.workspace_id),
            "process_workspace_id": str(binding.workspace_id),
            "shell_id": str(binding.identity),
            "kernel_generation_id": str(current.kernel_generation_id),
            "shell_epoch": str(current.epoch),
        }
        if any(origin.get(key) != value for key, value in expected.items()):
            return False
        sequence = state.get("sequence")
        if (
            not state.get("session_id")
            or type(sequence) is not int
            or sequence < getattr(shell, "_workspaceDebugSequence", 0)
        ):
            return False
        shell._workspaceDebugSequence = sequence
        return True

    def context_current(self, shell, token):
        from spikes.peer_frames.execution_routing import ExecutionCommand

        return (
            debug_token(shell) == token
            and self.execution.resolve_shell(shell, ExecutionCommand.DEBUGGER).resolved
        )

    def navigate(self, shell, filename, line, token=None):
        binding = self.execution._shells.get(shell)
        if binding is None or (
            token is not None and not self.context_current(shell, token)
        ):
            return None
        sources = getattr(shell, "_workspaceSessionSources", {})
        identity = next(
            (identity for path, identity in sources.items()
             if os.path.normcase(path) == os.path.normcase(filename)),
            None,
        )
        if identity is None:
            identity = self.documents.registry._paths.get(filename)
        if identity is None:
            canonical = os.path.realpath(filename)
            identity = next(
                (
                    identity
                    for path, identity in self.documents.registry._paths.items()
                    if os.path.normcase(os.path.realpath(path)) == os.path.normcase(canonical)
                ),
                None,
            )
        if identity is not None:
            try:
                resource = self.documents.registry.resource(identity)
            except KeyError:
                return None
            if not resource.available:
                self.show_message(
                    "Debugger source is unavailable; retained breakpoint entries remain inactive."
                )
                return None
        return self.open_source(binding.workspace_id, identity, filename, line)

    def indicators(self, shell, lines):
        binding = self.execution._shells.get(shell)
        if binding is None:
            return
        if self.execution._foreground.get(binding.workspace_id) is not shell:
            return
        sources = getattr(shell, "_workspaceSessionSources", {})
        # Native debugger filenames use platform-normalized casing. Preserve
        # captured document identity while matching that spelling on Windows.
        normalized_sources = {
            os.path.normcase(path): identity for path, identity in sources.items()
        }
        by_document = {
            normalized_sources.get(os.path.normcase(filename)): line
            for filename, line in lines
        }
        for editor in tuple(self.documents._editors):
            view = self.view(editor)
            if view and view.workspace_id == binding.workspace_id:
                editor.setDebugLineIndicator(by_document.get(view.document_id))

    def clear(self, owner):
        self.overlays.clear(owner)
        for editor in tuple(self.documents._editors):
            self.project_editor(editor)
        self.changed()
        for shell in tuple(self.execution._shells):
            self.send_breakpoints(shell)

    def discard_broken(self, owner, identity):
        if self.overlays.discard_broken(owner, identity):
            self.changed()
            for shell in tuple(self.execution._shells):
                self.send_breakpoints(shell)
