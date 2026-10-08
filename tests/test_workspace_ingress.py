"""GUI dispatch, transaction exclusion and modal drift for external opens."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pyzo
import pytest

pyzo.translate = lambda _context, text, _disambiguation=None: text

from pyzo.core.main import MainWindow
from spikes.peer_frames.contract import WorkspaceModel
from spikes.peer_frames.fake_backend import FakeBackend
from spikes.peer_frames.identity import ApplicationIdentity, IdentityKind
from spikes.peer_frames.workspace_coordinator import WorkspaceCoordinator


@pytest.fixture
def ingress_world(monkeypatch):
    coordinator = WorkspaceCoordinator()
    runtimes, opened, created, statuses = {}, [], [], []

    def create(name="Workspace"):
        owner = coordinator.add_workspace(WorkspaceModel(FakeBackend()), name=name)
        runtimes[owner] = SimpleNamespace(
            identity=owner, model=coordinator.model(owner), _disposed=False
        )
        created.append(owner)
        return owner

    owners = [create(name) for name in ("A", "B", "C")]
    created.clear()
    main = SimpleNamespace(
        _peerWorkspaces=coordinator,
        _peerRuntimeCatalog=SimpleNamespace(runtime=runtimes.__getitem__),
        _peerWorkspaceRestoreFailures={},
        _peerDocumentResources=SimpleNamespace(document_ids=()),
        _peerClosingWorkspaceIds=set(),
        _peerShutdownCoordinator=SimpleNamespace(phase="idle"),
        _peerApplicationExitRequested=False,
        _createPeerWorkspace=create,
        _activatePeerWorkspace=coordinator.activate_workspace,
        _ensurePeerRecoverySelection=lambda filename, **kwargs: True,
    )
    for name in (
        "_externalFileOpenAllowed",
        "_peerIngressWorkspaceEligible",
        "_openExternalFile",
    ):
        setattr(main, name, getattr(MainWindow, name).__get__(main))
    application = SimpleNamespace(
        open_external_file=main._openExternalFile,
        metadata=SimpleNamespace(show_status=lambda *args: statuses.append(args)),
    )
    monkeypatch.setattr(pyzo, "application", application, raising=False)
    monkeypatch.setattr(
        pyzo,
        "workspaceContext",
        SimpleNamespace(
            context=lambda owner: SimpleNamespace(
                editors=SimpleNamespace(
                    loadFile=lambda filename: opened.append((owner, filename))
                )
            )
        ),
        raising=False,
    )
    return SimpleNamespace(
        main=main,
        coordinator=coordinator,
        owners=owners,
        runtimes=runtimes,
        opened=opened,
        created=created,
        statuses=statuses,
    )


@pytest.mark.parametrize(
    "excluded", ["closing", "failed", "missing", "disposed", "shutdown"]
)
def test_mru_exclusion_falls_back_to_previous_owner(ingress_world, excluded):
    world = ingress_world
    a, b, c = world.owners
    for owner in (b, a, c):
        world.coordinator.activate_workspace(owner)
    if excluded == "closing":
        world.main._peerClosingWorkspaceIds.add(c)
    elif excluded == "failed":
        world.main._peerWorkspaceRestoreFailures[str(c)] = "failed"
    elif excluded == "missing":
        del world.runtimes[c]
    elif excluded == "disposed":
        world.runtimes[c]._disposed = True
    else:
        world.runtimes[c].model.shutdown_requested = True
    world.main._openExternalFile("/project-b/shared.py")
    assert world.opened == [(a, "/project-b/shared.py")]
    assert not world.created


@pytest.mark.parametrize(
    "phase", ["negotiating", "cleaning", "finished", "exit-requested"]
)
def test_application_exit_blocks_ingress_until_cancelled(ingress_world, phase):
    world = ingress_world
    world.main._peerShutdownCoordinator.phase = phase
    world.main._peerApplicationExitRequested = phase == "exit-requested"
    world.main._openExternalFile("file.py")
    assert not world.opened and not world.created and world.statuses
    world.main._peerShutdownCoordinator.phase = "idle"
    world.main._peerApplicationExitRequested = False
    world.main._openExternalFile("file.py")
    assert world.opened == [(world.owners[0], "file.py")]


def test_all_loaded_ineligible_refuses_without_creating(ingress_world):
    world = ingress_world
    world.main._peerClosingWorkspaceIds.update(world.owners)
    world.main._openExternalFile("file.py")
    assert not world.opened and not world.created and world.statuses


@pytest.mark.parametrize("phase", ["idle", "negotiating", "cleaning", "finished"])
def test_exit_guard_follows_real_shutdown_phase(ingress_world, phase):
    world = ingress_world

    def shutdown(disposition):
        assert world.main._peerApplicationExitRequested
        assert world.main._openExternalFile("during.py") is None
        world.main._peerShutdownCoordinator.phase = phase
        return "result"

    world.main._peerShutdownCoordinator.request = shutdown
    assert MainWindow._requestPeerApplicationExit(world.main, "quit") == "result"
    assert world.main._peerApplicationExitRequested == (phase != "idle")
    world.main._openExternalFile("after.py")
    assert world.opened == ([(world.owners[0], "after.py")] if phase == "idle" else [])


def test_empty_loaded_set_creates_one_default_preserving_failures(ingress_world):
    world = ingress_world
    for owner in world.owners:
        world.coordinator.remove_workspace(owner)
    world.main._peerWorkspaceRestoreFailures["unavailable"] = "opaque failure"
    world.main._openExternalFile("file.py")
    assert len(world.created) == 1
    assert world.opened == [(world.created[0], "file.py")]
    assert world.main._peerWorkspaceRestoreFailures == {"unavailable": "opaque failure"}
    world.main._openExternalFile("next.py")
    assert len(world.created) == 1


@pytest.mark.parametrize(
    "drift", ["focus", "cancel", "closing", "retired", "reload", "quit"]
)
def test_recovery_modal_keeps_selected_owner_or_refuses(ingress_world, drift):
    world = ingress_world
    a, b, _ = world.owners

    def selection(filename, *, workspace_id):
        assert filename == "shared.py"
        assert workspace_id == a
        world.coordinator.activate_workspace(b)
        if drift == "cancel":
            return False
        if drift == "closing":
            world.main._peerClosingWorkspaceIds.add(a)
        if drift in {"retired", "reload"}:
            world.coordinator.remove_workspace(a)
            if drift == "reload":
                world.coordinator.add_workspace(
                    WorkspaceModel(FakeBackend()), name="A", identity=a, restore=True
                )
        if drift == "quit":
            world.main._peerShutdownCoordinator.phase = "negotiating"
        return True

    world.main._ensurePeerRecoverySelection = selection
    world.main._openExternalFile("shared.py")
    assert world.opened == ([(a, "shared.py")] if drift == "focus" else [])
    assert not world.created


@pytest.mark.parametrize(
    "drift", ["focus", "cancel", "closing", "retired", "reload", "quit"]
)
def test_legacy_recovery_publication_keeps_captured_owner(ingress_world, drift):
    world = ingress_world
    a, b, _ = world.owners
    publications, reopened = [], []
    main = world.main
    main._PeerApplicationIdentity = ApplicationIdentity
    main._PeerIdentityKind = IdentityKind
    main._peerStatusService = SimpleNamespace(showMessage=lambda message: None)
    main.legacyPeerRecoveryChoices = lambda: {"doc": [{}]}
    main._choosePeerLegacyRecovery = lambda *args: None
    main._peerRetentionCoordinator = SimpleNamespace(
        edit=lambda: None, flush=lambda: True
    )
    main._peerDocumentResources.adopt_retained_document = lambda *args: None
    main._reopenPeerRetainedView = lambda view, *, workspace_id: reopened.append(
        workspace_id
    )

    def select(document, index, *, workspace_id):
        publications.append((document, index, workspace_id))
        return SimpleNamespace(
            state=SimpleNamespace(
                shared_resources={},
                application_state={
                    "legacy_recovery": {
                        "selections": {"doc": {"view_id": "selected-view"}}
                    }
                },
            )
        )

    main._peerWorkspaceStore = SimpleNamespace(select_legacy_recovery=select)

    def choose(choices, document):
        world.coordinator.activate_workspace(b)
        if drift == "cancel":
            return None
        if drift == "closing":
            main._peerClosingWorkspaceIds.add(a)
        if drift in {"retired", "reload"}:
            world.coordinator.remove_workspace(a)
            if drift == "reload":
                world.coordinator.add_workspace(
                    WorkspaceModel(FakeBackend()), name="A", identity=a, restore=True
                )
        if drift == "quit":
            main._peerShutdownCoordinator.phase = "negotiating"
        return "doc", 0

    main._peerLegacyRecoveryChooser = choose
    result = MainWindow.resolvePeerLegacyRecovery(main, "doc", workspace_id=a)
    assert result == (drift == "focus")
    assert publications == ([("doc", 0, str(a))] if drift == "focus" else [])
    assert reopened == ([a] if drift == "focus" else [])


@pytest.fixture
def command_module(monkeypatch):
    # Import the real command module with a test-owned non-listening server.
    import yoton.clientserver

    class Server:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(yoton.clientserver, "RequestServer", Server)
    path = Path(pyzo.__file__).parent / "core/commandline.py"
    spec = importlib.util.spec_from_file_location("ingress_test_commandline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("syntax", ["open", "bare"])
def test_server_queues_current_gui_routing_after_focus_drift(
    command_module, ingress_world, monkeypatch, syntax
):
    world = ingress_world
    queue = []
    monkeypatch.setattr(
        pyzo,
        "callLater",
        lambda callback, *args: queue.append((callback, args)),
        raising=False,
    )
    monkeypatch.setattr(pyzo, "editors", object(), raising=False)
    request = (
        "open /project-a/path with spaces.py"
        if syntax == "open"
        else "/project-a/path with spaces.py"
    )
    answer = command_module.server.handle_request(request)
    assert "file" in answer.lower() and not world.opened
    world.coordinator.activate_workspace(world.owners[1])
    for callback, args in queue:
        if callback is not print:
            callback(*args)
    assert world.opened == [(world.owners[1], "/project-a/path with spaces.py")]


def test_legacy_dispatch_resolves_current_manager(command_module, monkeypatch):
    queue, opened = [], []
    monkeypatch.setattr(
        pyzo,
        "callLater",
        lambda callback, *args: queue.append((callback, args)),
        raising=False,
    )
    monkeypatch.setattr(pyzo, "application", None, raising=False)
    monkeypatch.setattr(pyzo, "editors", object(), raising=False)
    command_module.handle_command("open", "legacy.py")
    monkeypatch.setattr(
        pyzo,
        "editors",
        SimpleNamespace(loadFile=lambda filename: opened.append(filename)),
        raising=False,
    )
    callback, args = queue.pop()
    callback(*args)
    assert opened == ["legacy.py"]
    assert (
        command_module.handle_command("open", "")
        == "The open command requires a filename."
    )
    assert not queue


@pytest.mark.parametrize("state", ["retired", "live", "legacy"])
def test_queued_file_check_rejects_retired_view_before_any_access(monkeypatch, state):
    from pyzo.util import zon

    monkeypatch.setattr(
        pyzo,
        "config",
        zon.load(str(Path(pyzo.__file__).parent / "resources/defaultConfig.ssdf")),
        raising=False,
    )
    from pyzo.core.editor import PyzoEditor

    checked = []
    editor = SimpleNamespace(
        _checkFileModified=lambda: checked.append("checked") and False
    )
    if state != "legacy":
        editor._peerViewIdentity = "view"
    monkeypatch.setattr(
        pyzo,
        "application",
        SimpleNamespace(
            documents=SimpleNamespace(is_bound=lambda candidate: state == "live")
        ),
        raising=False,
    )
    PyzoEditor._checkAndHandleExternalFileModifications(editor)
    assert checked == ([] if state == "retired" else ["checked"])


@pytest.mark.parametrize("dirty", [False, True])
def test_file_check_stops_after_modal_view_retirement(monkeypatch, dirty):
    from pyzo.util import zon
    from pyzo.qt import QtWidgets

    monkeypatch.setattr(
        pyzo,
        "config",
        zon.load(str(Path(pyzo.__file__).parent / "resources/defaultConfig.ssdf")),
        raising=False,
    )
    monkeypatch.setattr(pyzo, "workspaceContext", None, raising=False)
    from pyzo.core.editor import PyzoEditor

    state = {"live": True}
    editor = SimpleNamespace(
        _peerViewIdentity="view",
        _filename="file.py",
        _checkFileModified=lambda: True,
        document=lambda: SimpleNamespace(isModified=lambda: dirty),
    )

    class Dialog:
        StandardButton = QtWidgets.QMessageBox.StandardButton
        ButtonRole = QtWidgets.QMessageBox.ButtonRole

        def __init__(self, parent):
            assert parent is editor

        def __getattr__(self, name):
            assert name != "clickedButton", "accessed dialog after view retirement"
            return lambda *args: None

        def exec(self):
            state["live"] = False

    monkeypatch.setattr(QtWidgets, "QMessageBox", Dialog)
    monkeypatch.setattr(
        pyzo,
        "application",
        SimpleNamespace(
            documents=SimpleNamespace(is_bound=lambda candidate: state["live"])
        ),
        raising=False,
    )
    PyzoEditor._checkAndHandleExternalFileModifications(editor)
    assert not state["live"]
