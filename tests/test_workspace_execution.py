"""Controls for the live Selection boundary, using the real routing model."""

from types import SimpleNamespace

import pytest

from pyzo.core.workspaceExecution import LiveWorkspaceExecution
from spikes.peer_frames.document_resources import DocumentResourceRegistry
from spikes.peer_frames.identity import IdentityKind, new_identity
from spikes.peer_frames.execution_routing import AssignmentMode, RoutingStatus


class Shell:
    def __init__(self):
        self._version = "3.12"
        self._state = "Ready"
        self._context = SimpleNamespace(connection_count=1)


@pytest.fixture
def world():
    a, b, c = [new_identity(IdentityKind.WORKSPACE) for _ in range(3)]
    documents = DocumentResourceRegistry()
    editors = []
    for owner in (a, b, c):
        document = documents.create_document("value = 42")
        view = documents.open_view(document, owner)
        editors.append(
            SimpleNamespace(_peerViewIdentity=view, _peerDocumentIdentity=document)
        )
    owners = {"a1": a, "a2": a, "b": b}
    workspaces = SimpleNamespace(
        workspace_ids=[a, b, c],
        pane_owner=owners.get,
        transfer_pane=lambda *_: SimpleNamespace(
            applied=False, detail="cross-workspace shell transfer is not supported"
        ),
    )
    messages = []
    retired = []
    service = LiveWorkspaceExecution(
        documents,
        workspaces,
        messages.append,
        editor_is_live=lambda editor: editor not in retired,
    )
    shells = [Shell() for _ in range(3)]
    for shell, owner, pane in zip(shells, (a, a, b), ("a1", "a2", "b")):
        service.attach_shell(shell, owner, pane)
    return SimpleNamespace(
        a=a,
        b=b,
        c=c,
        docs=documents,
        editors=editors,
        shells=shells,
        service=service,
        messages=messages,
        workspaces=workspaces,
        retired=retired,
    )


def token(world, editor=0):
    result = world.service.resolve_selection(world.editors[editor])
    assert result.resolved, result.detail
    return result.provenance


def test_foreign_focus_never_redirects_selection_and_missing_is_visible(world):
    service = world.service
    service.foreground_shell(world.shells[0])
    service.foreground_shell(world.shells[2])
    sends = []
    assert service.dispatch(token(world), sends.append)
    assert service.dispatch(token(world, 1), sends.append)
    assert sends == [world.shells[0], world.shells[2]]
    assert not service.resolve_selection(world.editors[2]).resolved
    assert world.messages and "workspace" in world.messages[-1]


@pytest.mark.parametrize("loss", ["starting", "dead", "disconnected"])
def test_lost_foreground_does_not_fall_back_to_older_local_shell(world, loss):
    service = world.service
    older, current, other = world.shells
    service.foreground_shell(older)
    service.foreground_shell(current)
    old = token(world)
    if loss == "starting":
        current._version = ""
    elif loss == "dead":
        current._state = "Dead"
    else:
        current._context.connection_count = 0
    sends = []
    assert not service.dispatch(old, sends.append)
    assert not service.resolve_selection(world.editors[0]).resolved
    assert sends == []
    service.foreground_shell(other)
    assert token(world, 1).workspace_id == world.b


def test_restart_retains_shell_id_and_invalidates_generation_and_results(world):
    service = world.service
    shell = world.shells[0]
    service.foreground_shell(shell)
    old = token(world)
    shell._version = ""  # Native restart reset: same broker context.
    service.sync_shell(shell)
    assert not service.resolve_selection(world.editors[0]).resolved
    assert not service.provenance_is_current(old)
    shell._version = "3.12"
    service.sync_shell(shell)
    fresh = token(world)
    assert fresh.shell_id == old.shell_id
    assert fresh.kernel_generation_id != old.kernel_generation_id
    results = []
    assert not service.accept_result(old, 41, results.append)
    assert service.accept_result(fresh, 42, results.append)
    assert results == [42]


def test_confirmed_close_releases_target_without_older_shell_fallback(world):
    service = world.service
    older, current, _ = world.shells
    service.foreground_shell(older)
    service.foreground_shell(current)
    old = token(world)
    service.detach_shell(current)
    assert not service.provenance_is_current(old)
    assert not service.resolve_selection(world.editors[0]).resolved
    service.foreground_shell(older)  # Only an explicit foreground selects it.
    assert token(world).shell_id != old.shell_id


@pytest.mark.parametrize(
    "change",
    ["focus", "close", "context", "view-close", "view-move", "workspace-close"],
)
def test_capture_is_revalidated_immediately_before_send(world, change):
    service = world.service
    shell = world.shells[0]
    editor = world.editors[0]
    service.foreground_shell(shell)
    old = token(world)
    if change == "focus":
        service.foreground_shell(world.shells[1])
    elif change == "close":
        service.detach_shell(shell)
    elif change == "context":
        shell._context = SimpleNamespace(connection_count=1)
    elif change == "view-close":
        world.docs.close_view(editor._peerViewIdentity)
        world.docs.reopen_view(
            editor._peerDocumentIdentity,
            workspace_id=world.a,
            view_id=editor._peerViewIdentity,
        )
        world.retired.append(editor)
    elif change == "view-move":
        world.docs.move_view(editor._peerViewIdentity, world.b)
    else:
        world.workspaces.workspace_ids.remove(world.a)
    sends = []
    assert not service.dispatch(old, sends.append)
    assert not service.accept_result(old, "stale", sends.append)
    assert sends == []


def test_owner_mismatch_and_transfer_are_refused_without_mutation(world):
    service = world.service
    shell = world.shells[0]
    service.foreground_shell(shell)
    before = token(world)
    with pytest.raises(ValueError):
        service.attach_shell(shell, world.b, "a1")
    result = service.transfer_shell(shell, world.b)
    assert not result.applied
    assert "not supported" in world.messages[-1]
    assert service.provenance_is_current(before)
    assert token(world).shell_id == before.shell_id


def assign_via_router(world, editor=0, shell=0):
    world.service._synchronize()
    binding = world.service._shells[world.shells[shell]]
    assert world.service.router.assign_view(
        world.editors[editor]._peerViewIdentity, binding.identity
    ).applied


def test_explicit_delivery_ignores_local_and_foreign_foreground(world):
    assign_via_router(world)
    world.service.foreground_shell(world.shells[1])
    world.service.foreground_shell(world.shells[2])
    captured = token(world)
    sends = []
    assert captured.assignment_mode is AssignmentMode.EXPLICIT
    assert world.service.dispatch(captured, sends.append)
    assert sends == [world.shells[0]]


def test_explicit_target_does_not_require_an_eligible_foreground(world):
    assign_via_router(world)
    world.service.foreground_shell(world.shells[1])
    world.shells[1]._version = ""
    captured = token(world)
    assert captured.shell_id == world.service._shells[world.shells[0]].identity


def test_broken_assignment_stays_visible_until_reconnect_or_correction(world):
    assign_via_router(world)
    service, assigned = world.service, world.shells[0]
    old = token(world)
    assigned._version = ""
    service.sync_shell(assigned)
    service.foreground_shell(world.shells[1])
    result = service.resolve_selection(world.editors[0])
    assert result.status is RoutingStatus.BROKEN_TARGET
    assert "Selection target" in world.messages[-1]
    assert not service.dispatch(old, lambda _: pytest.fail("stale send"))
    assigned._version = "3.12"
    fresh = token(world)
    assert fresh.assignment_mode is AssignmentMode.EXPLICIT
    assert fresh.shell_id == old.shell_id
    assert fresh.kernel_generation_id != old.kernel_generation_id
    assert service.dispatch(
        fresh, lambda shell: assigned is shell or pytest.fail("wrong shell")
    )


@pytest.mark.parametrize("roundtrip", [False, True])
def test_view_transfer_keeps_assignment_broken_instead_of_dropping_it(world, roundtrip):
    assign_via_router(world)
    old = token(world)
    world.docs.move_view(world.editors[0]._peerViewIdentity, world.b)
    if roundtrip:
        world.docs.move_view(world.editors[0]._peerViewIdentity, world.a)
    result = world.service.resolve_selection(world.editors[0])
    assert result.status is RoutingStatus.BROKEN_TARGET
    assert not world.service.provenance_is_current(old)


def test_closed_reopened_same_view_between_sync_calls_is_implicit(world):
    assign_via_router(world)
    editor = world.editors[0]
    old = token(world)
    world.docs.close_view(editor._peerViewIdentity)
    world.docs.reopen_view(
        editor._peerDocumentIdentity, view_id=editor._peerViewIdentity
    )
    fresh = token(world)
    assert fresh.assignment_mode is AssignmentMode.IMPLICIT
    assert not world.service.provenance_is_current(old)


def test_assignment_api_is_view_local_live_only_and_clear_invalidates_token(world):
    service, editor = world.service, world.editors[0]
    assert service.assign_selection(editor, world.shells[0]).applied
    context = service.selection_targets(editor).context
    old = token(world)
    service.foreground_shell(world.shells[1])
    second_view = world.docs.open_view(editor._peerDocumentIdentity, world.a)
    second = SimpleNamespace(
        _peerViewIdentity=second_view,
        _peerDocumentIdentity=editor._peerDocumentIdentity,
    )
    assert service.selection_targets(second).assignment.mode is AssignmentMode.IMPLICIT
    assert service.selection_targets(editor).assignment.mode is AssignmentMode.EXPLICIT
    world.docs.close_view(second_view)
    assert world.docs.resource(editor._peerDocumentIdentity).text == "value = 42"
    assert service.clear_selection_assignment(editor, context=context).applied
    assert token(world).assignment_mode is AssignmentMode.IMPLICIT
    assert not service.provenance_is_current(old)
    fresh = LiveWorkspaceExecution(
        world.docs,
        world.workspaces,
        world.messages.append,
        editor_is_live=lambda _: True,
    )
    fresh.attach_shell(world.shells[0], world.a, "a1")
    assert fresh.selection_targets(editor).assignment.mode is AssignmentMode.IMPLICIT


def test_assignment_does_not_modify_serialized_documents(world):
    before = world.docs.persistence_snapshot()
    assert world.service.assign_selection(world.editors[0], world.shells[0]).applied
    assert before == world.docs.persistence_snapshot()


@pytest.mark.parametrize(
    "invalid",
    ["foreign", "starting", "dead", "unknown", "retired", "document", "owner"],
)
def test_assignment_refuses_invalid_choice_without_changing_existing_target(
    world, invalid
):
    service, editor, assigned = world.service, world.editors[0], world.shells[0]
    assert service.assign_selection(editor, assigned).applied
    target = world.shells[1]
    if invalid == "foreign":
        target = world.shells[2]
    elif invalid == "starting":
        target._version = ""
    elif invalid == "dead":
        target._state = "Dead"
    elif invalid == "unknown":
        target = Shell()
    elif invalid == "retired":
        world.retired.append(editor)
    elif invalid == "document":
        editor._peerDocumentIdentity = world.editors[1]._peerDocumentIdentity
    else:
        world.workspaces.workspace_ids.remove(world.a)
    assert not service.assign_selection(editor, target).applied
    if invalid != "owner":
        assert (
            service.router.view_state(editor._peerViewIdentity).assignment.shell_id
            == service._shells[assigned].identity
        )
    assert "Could not change Selection target" in world.messages[-1]


@pytest.mark.parametrize(
    "change", ["move", "roundtrip", "close-reopen", "retire", "shell-close"]
)
def test_popup_context_and_native_shell_are_revalidated_on_activation(world, change):
    service, editor = world.service, world.editors[0]
    context = service.selection_targets(editor).context
    target = world.shells[0]
    if change in ("move", "roundtrip"):
        world.docs.move_view(editor._peerViewIdentity, world.b)
        if change == "roundtrip":
            world.docs.move_view(editor._peerViewIdentity, world.a)
    elif change == "close-reopen":
        world.docs.close_view(editor._peerViewIdentity)
        world.docs.reopen_view(
            editor._peerDocumentIdentity, view_id=editor._peerViewIdentity
        )
    elif change == "retire":
        world.retired.append(editor)
    else:
        service.detach_shell(target)
    assert not service.assign_selection(editor, target, context=context).applied
    if change != "shell-close":
        assert not service.clear_selection_assignment(editor, context=context).applied


def test_confirmed_explicit_close_resets_to_existing_foreground(world):
    service, editor = world.service, world.editors[0]
    assert service.assign_selection(editor, world.shells[0]).applied
    service.foreground_shell(world.shells[1])
    old = token(world)
    service.detach_shell(world.shells[0])
    fresh = token(world)
    assert fresh.assignment_mode is AssignmentMode.IMPLICIT
    assert fresh.shell_id == service._shells[world.shells[1]].identity
    assert not service.provenance_is_current(old)


def test_assignment_admission_checks_native_owner_even_if_router_state_drifts(world):
    service = world.service
    service._synchronize()
    shell = world.shells[0]
    identity = service._shells[shell].identity
    assert service.router.move_shell(identity, world.b).applied
    assert not service.assign_selection(world.editors[1], shell).applied


def test_dispatch_checks_native_owner_even_if_router_state_drifts(world):
    from spikes.peer_frames.execution_routing import ExecutionCommand

    service = world.service
    service._synchronize()
    shell = world.shells[0]
    identity = service._shells[shell].identity
    assert service.router.move_shell(identity, world.b).applied
    assert service.router.assign_view(
        world.editors[1]._peerViewIdentity, identity
    ).applied
    wrong = service.router.resolve(
        ExecutionCommand.EXECUTE_SELECTION, view_id=world.editors[1]._peerViewIdentity
    ).provenance
    sends = []
    assert not service.dispatch(wrong, sends.append)
    assert sends == []
