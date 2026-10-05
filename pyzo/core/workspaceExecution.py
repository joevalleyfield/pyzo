"""Live workspace boundary for Execute Selection.

Native shell identity survives restart; kernel generation does not. This service
does not change the compatibility File/Main execution target or migrate shells.
"""

from dataclasses import dataclass

from spikes.peer_frames.execution_routing import (
    AssignmentMode,
    ExecutionAssignment,
    ExecutionCommand,
    ExecutionResolution,
    ExecutionRouter,
    RoutingStatus,
    RoutingResult,
)
from spikes.peer_frames.identity import IdentityKind, new_identity


@dataclass
class _ShellBinding:
    shell: object
    workspace_id: object
    pane_id: str
    identity: object
    context: object


@dataclass(frozen=True)
class SelectionTargetContext:
    view_id: object
    workspace_id: object
    view_epoch: int


@dataclass(frozen=True)
class SelectionShellChoice:
    shell: object
    identity: object
    label: str


@dataclass(frozen=True)
class SelectionTargetSnapshot:
    context: SelectionTargetContext | None = None
    assignment: ExecutionAssignment = ExecutionAssignment()
    choices: tuple = ()
    assigned_label: str = ""
    detail: str = ""


class LiveWorkspaceExecution:
    def __init__(self, documents, workspaces, show_message, *, editor_is_live):
        self.router = ExecutionRouter()
        self._documents = documents
        self._workspaces = workspaces
        self._show_message = show_message
        self._editor_is_live = editor_is_live
        self._registered_workspaces = set()
        self._views = {}
        self._shells = {}
        self._foreground = {}

    def _register_workspaces(self):
        for owner in self._workspaces.workspace_ids:
            if owner not in self._registered_workspaces:
                self.router.register_workspace(owner)
                self._registered_workspaces.add(owner)

    def _owner_is_current(self, binding):
        return (
            binding.workspace_id in self._workspaces.workspace_ids
            and self._workspaces.pane_owner(binding.pane_id) == binding.workspace_id
        )

    def attach_shell(self, shell, workspace_id, pane_id):
        if (
            workspace_id not in self._workspaces.workspace_ids
            or self._workspaces.pane_owner(pane_id) != workspace_id
        ):
            raise ValueError("shell pane does not belong to the workspace")
        if shell in self._shells:
            binding = self._shells[shell]
            if (binding.workspace_id, binding.pane_id) != (workspace_id, pane_id):
                raise ValueError("live shells cannot change workspace owner")
            self.sync_shell(shell)
            return binding.identity
        self._register_workspaces()
        identity = new_identity(IdentityKind.SHELL)
        binding = _ShellBinding(shell, workspace_id, pane_id, identity, shell._context)
        self._shells[shell] = binding
        self.router.register_shell(
            identity, workspace_id, new_identity(IdentityKind.KERNEL_GENERATION)
        )
        self.foreground_shell(shell)
        return identity

    def sync_shell(self, shell):
        binding = self._shells.get(shell)
        if binding is None:
            return
        state = self.router.shell_state(binding.identity)
        context = getattr(shell, "_context", None)
        eligible = bool(
            self._owner_is_current(binding)
            and getattr(context, "connection_count", 0)
            and getattr(shell, "_version", "")
            and getattr(shell, "_state", "")
            in ("Ready", "More", "Busy", "Very busy", "Debug")
        )
        if not eligible and state.eligible:
            self.router.unexpected_shell_loss(binding.identity)
        elif eligible and not state.eligible:
            self.router.reconnect_shell(
                binding.identity, new_identity(IdentityKind.KERNEL_GENERATION)
            )
        elif eligible and context is not binding.context:
            self.router.recycle_shell(
                binding.identity,
                new_identity(IdentityKind.KERNEL_GENERATION),
                succeeded=True,
            )
        binding.context = context
        if eligible and self._foreground.get(binding.workspace_id) is shell:
            self.router.foreground_shell(binding.workspace_id, binding.identity)

    def foreground_shell(self, shell):
        binding = self._shells.get(shell)
        if binding is None:
            return False
        # Preserve the intended foreground even if starting/lost. An older live
        # shell must never become a fallback merely because it is eligible.
        self._foreground[binding.workspace_id] = shell
        self.sync_shell(shell)
        return True

    def detach_shell(self, shell):
        binding = self._shells.pop(shell, None)
        if binding is not None:
            self.router.confirmed_shell_close(binding.identity)
            if self._foreground.get(binding.workspace_id) is shell:
                del self._foreground[binding.workspace_id]

    def _synchronize(self):
        self._register_workspaces()
        current = {}
        for view_id in self._documents.view_ids:
            view = self._documents.view(view_id)
            if view.workspace_id in self._workspaces.workspace_ids:
                current[view_id] = (view.workspace_id, view.epoch, view)
        for view_id, state in tuple(self._views.items()):
            latest = current.get(view_id)
            if latest is None or latest[2] is not state[2]:
                self.router.close_view(view_id)
                del self._views[view_id]
            elif latest[:2] != state[:2]:
                result = self.router.synchronize_view(
                    view_id, latest[0], epoch=latest[1]
                )
                if not result.applied:
                    raise RuntimeError(result.detail)
                self._views[view_id] = latest
        for view_id, (owner, epoch, record) in current.items():
            if view_id not in self._views:
                self.router.register_view(view_id, owner, epoch=epoch)
                self._views[view_id] = (owner, epoch, record)
        for shell in tuple(self._shells):
            self.sync_shell(shell)

    def _refuse(self, detail, status=RoutingStatus.NO_TARGET):
        self._show_message("Could not execute selection: " + detail)
        return ExecutionResolution(status, detail)

    def _editor_view(self, editor):
        if editor is None or not self._editor_is_live(editor):
            return None, "the invoking editor view is no longer available."
        view_id = getattr(editor, "_peerViewIdentity", None)
        if view_id not in self._views:
            return None, "the invoking editor view is no longer available."
        view = self._documents.view(view_id)
        if view.document_id != getattr(editor, "_peerDocumentIdentity", None):
            return None, "the invoking editor document has changed."
        return view, ""

    @staticmethod
    def _context(view):
        return SelectionTargetContext(view.identity, view.workspace_id, view.epoch)

    def _binding_for_identity(self, identity):
        return next(
            (
                binding
                for binding in self._shells.values()
                if binding.identity == identity
            ),
            None,
        )

    def selection_targets(self, editor):
        """Snapshot only local eligible choices; no persistent/config state."""
        self._synchronize()
        view, detail = self._editor_view(editor)
        if view is None:
            return SelectionTargetSnapshot(detail=detail)
        assignment = self.router.view_state(view.identity).assignment
        choices = []
        for index, binding in enumerate(
            (
                binding
                for binding in self._shells.values()
                if binding.workspace_id == view.workspace_id
            ),
            start=1,
        ):
            if self.router.shell_state(binding.identity).eligible:
                name = getattr(getattr(binding.shell, "_info", None), "name", "Shell")
                choices.append(
                    SelectionShellChoice(
                        binding.shell, binding.identity, f"{index}. {name}"
                    )
                )
        assigned = self._binding_for_identity(assignment.shell_id)
        assigned_label = (
            getattr(getattr(assigned.shell, "_info", None), "name", "Shell")
            if assigned
            else "Shell"
        )
        return SelectionTargetSnapshot(
            self._context(view), assignment, tuple(choices), assigned_label
        )

    def _assignment_refusal(self, detail):
        self._show_message("Could not change Selection target: " + detail)
        return RoutingResult(RoutingStatus.INVALID, detail)

    def _assignment_view(self, editor, context):
        self._synchronize()
        view, detail = self._editor_view(editor)
        if view is None:
            return None, detail
        if context is not None and context != self._context(view):
            return (
                None,
                "the captured editor view has changed; reopen Selection target.",
            )
        return view, ""

    def assign_selection(self, editor, shell, *, context=None):
        view, detail = self._assignment_view(editor, context)
        if view is None:
            return self._assignment_refusal(detail)
        binding = self._shells.get(shell)
        if binding is None or not self._owner_is_current(binding):
            return self._assignment_refusal("this shell is no longer available.")
        if binding.workspace_id != view.workspace_id:
            return self._assignment_refusal(
                "cross-workspace assignment is not supported"
            )
        result = self.router.assign_view(view.identity, binding.identity)
        if not result.applied:
            return self._assignment_refusal(result.detail)
        return result

    def clear_selection_assignment(self, editor, *, context=None):
        view, detail = self._assignment_view(editor, context)
        if view is None:
            return self._assignment_refusal(detail)
        return self.router.clear_assignment(view.identity)

    def resolve_selection(self, editor, *, line=False):
        self._synchronize()
        view, detail = self._editor_view(editor)
        if view is None:
            return self._refuse(detail)
        assignment = self.router.view_state(view.identity).assignment
        if assignment.mode is AssignmentMode.IMPLICIT:
            shell = self._foreground.get(view.workspace_id)
            binding = self._shells.get(shell)
            if (
                binding is None
                or not self.router.shell_state(binding.identity).eligible
            ):
                return self._refuse("this workspace has no eligible foreground shell.")
        result = self.router.resolve(
            ExecutionCommand.EXECUTE_LINE
            if line
            else ExecutionCommand.EXECUTE_SELECTION,
            view_id=view.identity,
        )
        if not result.resolved:
            if result.status is RoutingStatus.BROKEN_TARGET:
                return self._refuse(
                    "the assigned shell is unavailable. Choose a local shell or Workspace foreground under Run > Selection target.",
                    result.status,
                )
            return self._refuse(result.detail, result.status)
        return result

    def provenance_is_current(self, provenance):
        self._synchronize()
        if not self.router.provenance_is_current(provenance):
            return False
        binding = self._binding_for_identity(provenance.shell_id)
        return bool(
            binding
            and self._owner_is_current(binding)
            and binding.workspace_id == provenance.workspace_id
            and binding.workspace_id == provenance.process_workspace_id
            and (
                provenance.assignment_mode is AssignmentMode.EXPLICIT
                or self._foreground.get(provenance.workspace_id) is binding.shell
            )
        )

    def dispatch(self, provenance, send):
        if not self.provenance_is_current(provenance):
            self._refuse(
                "the captured view or shell has changed; invoke Selection again."
            )
            return False
        send(self._binding_for_identity(provenance.shell_id).shell)
        return True

    def accept_result(self, provenance, value, consumer):
        if not self.provenance_is_current(provenance):
            return False
        consumer(value)
        return True

    def transfer_shell(self, shell, target_workspace_id):
        binding = self._shells[shell]
        result = self._workspaces.transfer_pane(binding.pane_id, target_workspace_id)
        if not result.applied:
            self._show_message(result.detail)
        return result
