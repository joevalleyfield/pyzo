"""Controls for the live Selection boundary, using the real routing model."""

from types import SimpleNamespace

import pytest

from pyzo.core.workspaceExecution import LiveWorkspaceExecution
from spikes.peer_frames.document_resources import DocumentResourceRegistry
from spikes.peer_frames.identity import IdentityKind, new_identity


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
