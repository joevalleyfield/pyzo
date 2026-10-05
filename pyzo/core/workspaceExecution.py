"""Live workspace boundary for Execute Selection.

Native shell identity survives restart; kernel generation does not. This service
does not change the compatibility File/Main execution target or migrate shells.
"""

from dataclasses import dataclass

from spikes.peer_frames.execution_routing import (
    ExecutionCommand,
    ExecutionResolution,
    ExecutionRouter,
    RoutingStatus,
)
from spikes.peer_frames.identity import IdentityKind, new_identity


@dataclass
class _ShellBinding:
    shell: object
    workspace_id: object
    pane_id: str
    identity: object
    context: object


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
                current[view_id] = (view.workspace_id, view.epoch)
        for view_id, state in tuple(self._views.items()):
            if current.get(view_id) != state:
                self.router.close_view(view_id)
                del self._views[view_id]
        for view_id, (owner, epoch) in current.items():
            if view_id not in self._views:
                self.router.register_view(view_id, owner, epoch=epoch)
                self._views[view_id] = (owner, epoch)
        for shell in tuple(self._shells):
            self.sync_shell(shell)

    def _refuse(self, detail, status=RoutingStatus.NO_TARGET):
        self._show_message("Could not execute selection: " + detail)
        return ExecutionResolution(status, detail)

    def resolve_selection(self, editor, *, line=False):
        self._synchronize()
        view_id = getattr(editor, "_peerViewIdentity", None)
        if (
            editor is None
            or not self._editor_is_live(editor)
            or view_id not in self._views
        ):
            return self._refuse("the invoking editor view is no longer available.")
        view = self._documents.view(view_id)
        if view.document_id != getattr(editor, "_peerDocumentIdentity", None):
            return self._refuse("the invoking editor document has changed.")
        shell = self._foreground.get(view.workspace_id)
        binding = self._shells.get(shell)
        if binding is None or not self.router.shell_state(binding.identity).eligible:
            return self._refuse("this workspace has no eligible foreground shell.")
        result = self.router.resolve(
            ExecutionCommand.EXECUTE_LINE
            if line
            else ExecutionCommand.EXECUTE_SELECTION,
            view_id=view_id,
        )
        if not result.resolved:
            return self._refuse(result.detail, result.status)
        return result

    def provenance_is_current(self, provenance):
        self._synchronize()
        if not self.router.provenance_is_current(provenance):
            return False
        shell = self._foreground.get(provenance.workspace_id)
        binding = self._shells.get(shell)
        return bool(
            binding
            and binding.identity == provenance.shell_id
            and self._owner_is_current(binding)
        )

    def dispatch(self, provenance, send):
        if not self.provenance_is_current(provenance):
            self._refuse(
                "the captured view or shell has changed; invoke Selection again."
            )
            return False
        send(self._foreground[provenance.workspace_id])
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
