"""Census-wide config ownership, copy and compatibility assertions."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pyzo
import pytest
from pyzo.util import zon as ssdf
from pyzo.core.workspaceContext import (
    APPLICATION,
    CAPABILITIES,
    COMPATIBILITY,
    ConfigLayer,
    WorkspaceContexts,
    config_for,
    context_for,
    debugger_capability,
    owner_for,
    plain,
    struct,
)


def leaves(value, prefix=""):
    if hasattr(value, "keys") and value.keys():
        for key in value.keys():
            yield from leaves(value[key], prefix + key + ".")
    else:
        yield prefix.rstrip("."), plain(value)


@pytest.fixture
def config_world(monkeypatch):
    configuration = ssdf.load(
        str(Path(pyzo.__file__).parent / "resources/defaultConfig.ssdf")
    )
    configuration.tools.thirdparty = struct(
        {"nested": {"list": [1, {"future": "preserved"}]}, "blob": b"opaque"}
    )
    configuration.future = struct({"unknown": [3]})
    monkeypatch.setattr(pyzo, "config", configuration, raising=False)
    monkeypatch.setattr(pyzo, "workspaceContext", None, raising=False)
    owners = {"A": object(), "B": object(), "copy": object()}
    main = SimpleNamespace(
        _peerWorkspaceIdentity="A",
        _peerWorkspacePersistenceLoad=SimpleNamespace(
            state=SimpleNamespace(workspaces={})
        ),
        _peerRuntimeCatalog=SimpleNamespace(runtime=owners.__getitem__),
    )
    service = WorkspaceContexts(
        main, SimpleNamespace(get_commands=lambda: ["legacy_command"])
    )
    monkeypatch.setattr(
        pyzo, "toolManager", SimpleNamespace(managers={}), raising=False
    )
    service.ensure("A")
    service.ensure("B")
    return configuration, service, monkeypatch


def test_every_current_default_key_has_independent_or_explicit_application_owner(
    config_world,
):
    application, service, _ = config_world
    local_a, local_b = service.profiles["A"], service.profiles["B"]
    layer_a, layer_b = (
        ConfigLayer(local_a, application),
        ConfigLayer(local_b, application),
    )
    baseline = plain(local_b)
    paths = dict(leaves(application))
    assert len(paths) >= 100  # complete defaults, including every shortcut
    for path in paths:
        parts = path.split(".")
        a, b, app = layer_a, layer_b, application
        for key in parts[:-1]:
            a, b, app = a[key], b[key], app[key]
        key = parts[-1]
        before_b = plain(b[key])
        original = deepcopy(app[key])
        marker = ["ownership-probe", path]
        a[key] = marker
        if path in APPLICATION | COMPATIBILITY or path.startswith("shortcuts2."):
            assert b[key] == marker
            assert app[key] == marker
            app[key] = original
        elif path in CAPABILITIES:
            assert plain(a[key]) == plain(original)
            assert plain(b[key]) == before_b
        else:
            assert a[key] == marker
            assert plain(b[key]) == before_b
            assert plain(app[key]) == plain(original)
    assert plain(local_b) == baseline


def test_requested_capabilities_are_retained_without_changing_effective_values(
    config_world,
):
    application, service, _ = config_world
    for owner in ("A", "B"):
        local = ConfigLayer(service.profiles[owner], application)
        for path in CAPABILITIES:
            section, key = path.split(".")
            getattr(local, section)[key] = owner + " request"
            assert (
                getattr(
                    ConfigLayer(service.profiles[owner], application, requested=True),
                    section,
                )[key]
                == owner + " request"
            )
            assert getattr(local, section)[key] == application[section][key]
    assert (
        service.profiles["A"].settings.language
        != service.profiles["B"].settings.language
    )


def test_unknown_config_history_and_projects_roundtrip_and_snapshot_without_aliases(
    config_world,
):
    application, service, _ = config_world
    a = service.profiles["A"]
    a.tools.pyzofilebrowser2 = struct(
        {"starredDirs": [{"path": "/project", "name": "A", "addToPythonpath": True}]}
    )
    a.shellConfigs2.append(
        struct({"name": "A Python", "exe": "/python", "pythonPath": "A"})
    )
    service.histories["A"].append("owner_A()")
    service.histories["B"].append("owner_B()")
    snapshot = service.snapshot("A")
    b_before = service.snapshot("B")
    archive_before = plain(application)
    service.ensure("copy")
    managers = {
        owner: SimpleNamespace(
            capture_states=lambda: {"tool": {"nested": [1, 2]}},
            reloadTools=lambda: None,
        )
        for owner in ("A", "copy")
    }
    pyzo.toolManager.for_workspace = managers.__getitem__
    service.copy("A", "copy")
    managers["copy"].states["tool"]["nested"].append(3)
    assert managers["A"].capture_states()["tool"]["nested"] == [1, 2]
    service.profiles["copy"].tools.thirdparty.nested.list[1].future = "copy only"
    service.profiles["copy"].tools.pyzofilebrowser2.starredDirs[0].name = "copy only"
    service.profiles["copy"].shellConfigs2[0].name = "copy only"
    service.histories["copy"].append("copy_only()")
    assert service.snapshot("A") == snapshot
    assert service.snapshot("B") == b_before
    assert plain(application) == archive_before
    assert struct(snapshot["config"]).tools.thirdparty.blob == b"opaque"
    assert "legacy_command" not in service.histories["B"].get_commands()


def test_widget_ancestry_and_durable_view_owner_override_foreground(config_world):
    application, service, monkeypatch = config_world
    service.main._peerDocumentResources = SimpleNamespace(
        view=lambda identity: (
            SimpleNamespace(workspace_id="B")
            if identity == "current"
            else (_ for _ in ()).throw(KeyError(identity))
        )
    )
    monkeypatch.setattr(pyzo, "workspaceContext", service)
    parent = SimpleNamespace(_workspaceContextOwner="A", parent=lambda: None)
    child = SimpleNamespace(parent=lambda: parent)
    assert owner_for(child) == "A"
    moved = SimpleNamespace(
        _peerViewIdentity="current", _workspaceContextOwner="A", parent=lambda: parent
    )
    assert owner_for(moved) == "B"
    retired = SimpleNamespace(_peerViewIdentity="retired", parent=lambda: parent)
    with pytest.raises(RuntimeError, match="unavailable"):
        owner_for(retired)
    monkeypatch.setattr(pyzo, "workspaceContext", None)
    assert config_for(child) is application


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "unavailable"),
        ("unknown", "unavailable"),
        ("shared", "shared"),
        ("independent", "independent"),
    ],
)
def test_debugger_capability_requires_interpreter_advertisement(value, expected):
    shell = SimpleNamespace(_startup_info={"workspaceDebuggerSelection": value})
    assert debugger_capability(shell) == expected
    assert debugger_capability(None) == "unavailable"


def test_late_dynamic_fields_take_owned_snapshots(config_world):
    application, service, _ = config_world
    application.tools.late_extension = struct({"nested": [1]})
    application.shortcuts20 = struct({"nested": [2]})
    a = ConfigLayer(service.profiles["A"], application)
    b = ConfigLayer(service.profiles["B"], application)
    a.tools.late_extension.nested.append("A")
    a.shortcuts20.nested.append("A")
    b.tools.late_extension.nested.append("B")
    assert application.tools.late_extension.nested == [1]
    assert application.shortcuts20.nested == [2]
    assert a.tools.late_extension.nested == [1, "A"]
    assert b.tools.late_extension.nested == [1, "B"]
    assert b.shortcuts20.nested == [2]


def test_zero_loaded_owner_uses_only_existing_empty_bootstrap(config_world):
    application, service, monkeypatch = config_world
    service.main._peerWorkspaces = SimpleNamespace(active_workspace_id=None)
    service.main._peerRuntime = SimpleNamespace(identity="A")
    bootstrap = SimpleNamespace(owner="A", editors=(), shells=())
    service.contexts["A"] = bootstrap
    monkeypatch.setattr(pyzo, "workspaceContext", service)
    assert context_for() is bootstrap
    assert not context_for().editors and not context_for().shells


@pytest.mark.parametrize(
    "source, expected",
    [
        ("'Literal name'", "Literal name"),
        ("translate('tool', 'Translated')", "Translated"),
        ("pyzo.translate('tool', 'Translated')", "Translated"),
        ("translate()", ""),
        ("translate('tool', missing)", ""),
        ("broken syntax !", ""),
        ("[1, 2]", ""),
        ("__import__('builtins').exec('raise AssertionError')", ""),
    ],
)
def test_tool_metadata_discovery_is_literal_and_failure_local(
    monkeypatch, source, expected
):
    import pyzo.tools as tools

    monkeypatch.setattr(tools, "translate", lambda context, text: text)
    assert tools._metadata_name(source) == expected
