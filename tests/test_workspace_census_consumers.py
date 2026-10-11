"""Owner effects for native consumers found by the final direct-global census."""

from types import SimpleNamespace as NS
import pytest
import pyzo
from pyzo.qt import QtWidgets


@pytest.fixture(autouse=True)
def native_modules(monkeypatch):
    global baseTextCtrl, editor, editorTabs, menu, workspaceContext
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(
            pyzo, "config", NS(settings=NS(defaultLineEndings="LF")), raising=False
        )
    from pyzo.core import baseTextCtrl, editor, editorTabs, menu, workspaceContext

    yield app


def test_replace_all_snapshots_only_invoking_workspaces_views(monkeypatch):
    selected = [object(), object()]
    calls = []

    def replace(view):
        calls.append(view)
        selected.clear()  # Reentrant focus/membership change cannot redirect loop.

    host = NS(_replaceAll=replace)
    monkeypatch.setattr(editorTabs, "context_for", lambda widget: NS(editors=selected))
    monkeypatch.setattr(pyzo, "editors", [object()], raising=False)
    original = tuple(selected)
    editorTabs.FindReplaceWidget._replaceInAllFiles(host)
    assert tuple(calls) == original


@pytest.mark.parametrize("source", ("calltip", "autocomplete"))
@pytest.mark.parametrize("availability", ("local", "missing", "retired", "legacy"))
def test_help_completion_uses_its_source_context(monkeypatch, source, availability):
    calls = []
    tool = NS(helpFromExtension=lambda *args: calls.append(args))
    manager = NS(getTool=lambda name: None if availability == "missing" else tool)

    def context(widget):
        if availability == "retired":
            raise KeyError("retired source")
        return NS(toolManager=manager)

    monkeypatch.setattr(baseTextCtrl, "context_for", context)
    monkeypatch.setattr(
        pyzo,
        "toolManager",
        NS(getTool=lambda _: pytest.fail("foreign help lookup")),
        raising=False,
    )
    baseTextCtrl.BaseTextCtrl.restoreHelp(object(), source)
    assert calls == ([] if availability in ("missing", "retired") else [(None, source)])


def test_native_drop_cursor_and_group_notifications_use_invoking_widgets(monkeypatch):
    source, event = NS(), NS(mimeData=lambda: NS(hasUrls=lambda: True))
    drops, cursors = [], []
    monkeypatch.setattr(
        editor, "context_for", lambda widget: NS(editors=NS(dropEvent=drops.append))
    )
    monkeypatch.setattr(pyzo, "metadata", NS(cursor=cursors.append), raising=False)
    editor.PyzoEditor.dropEvent(source, event)
    editor.PyzoEditor._updateStatusBar(source)
    group = NS(
        currentChanged=NS(emit=lambda: None),
        getCurrentEditor=lambda: source,
        _findReplace=NS(resetSearchResults=lambda: None),
    )
    editorTabs.EditorTabs.onCurrentChanged(group)
    assert drops == [event]
    assert cursors == [source, source]


@pytest.mark.parametrize("has_editor", (False, True))
def test_pdf_export_captures_invoking_editor_and_parent(monkeypatch, has_editor):
    selected = object() if has_editor else None
    calls = []
    monkeypatch.setattr(
        menu,
        "context_for",
        lambda widget: NS(editors=NS(getCurrentEditor=lambda: selected)),
    )

    def dialog(**kwargs):
        calls.append(kwargs)
        return NS(exec=lambda: calls.append("exec"))

    monkeypatch.setattr(menu, "PdfExport", dialog)
    menu.FileMenu._pdfExport(object())
    assert calls == (
        [dict(editor=selected, parent=selected), "exec"] if has_editor else []
    )


@pytest.mark.parametrize(
    "condition", ("live", "cancel", "retired", "epoch", "exit", "closing")
)
@pytest.mark.parametrize("known", (False, True))
def test_local_open_retains_shared_identity_and_modal_owner(
    monkeypatch, tmp_path, condition, known
):
    from spikes.peer_frames.document_resources import DocumentResourceRegistry
    from spikes.peer_frames.identity import new_identity, IdentityKind

    owner = new_identity(IdentityKind.WORKSPACE)
    registry = DocumentResourceRegistry()
    path = tmp_path / "shared.py"
    path.write_text("disk")
    document = (
        registry.create_document("retained unsaved", path=str(path)) if known else None
    )
    calls, loaded, items = [], [], {}
    group = NS(_findEditorOfFile=lambda filename: items.get(filename))
    valid = {"owner": condition != "closing", "epoch": True, "exit": True}

    def choose(filename, *, workspace_id):
        assert workspace_id == owner
        calls.append(("choose", filename))
        if condition == "retired":
            valid["owner"] = False
        if condition == "epoch":
            valid["epoch"] = False
        if condition == "exit":
            valid["exit"] = False
        return condition != "cancel"

    def reopen(identity, workspace_id):
        assert identity == document and workspace_id == owner
        calls.append(("reopen", identity))
        items[str(path)] = NS(editor=NS(setFocus=lambda: None), identity=identity)

    new_group = NS(
        loadFile=lambda filename, *args, **kwargs: (
            loaded.append(filename) or NS(editor=object())
        )
    )
    main = NS(
        _externalFileOpenAllowed=lambda: valid["exit"],
        _peerIngressWorkspaceEligible=lambda _: valid["owner"],
        _peerWorkspaces=NS(
            resolve_workspace_context=lambda _: "epoch",
            context_is_current=lambda _: valid["epoch"],
        ),
        _ensurePeerRecoverySelection=choose,
        _peerDocumentResources=registry,
        _peerMainEditor=reopen,
        _createEditorHostForRuntime=lambda _: new_group,
    )
    context = NS(service=NS(main=main), owner=owner, runtime=object())
    facade = NS(context=context, groups=(group,), _active=None)
    monkeypatch.setattr(
        pyzo,
        "editors",
        NS(setActiveGroup=lambda _: None, addGroup=lambda _: None),
        raising=False,
    )
    alias = tmp_path / "alias.py"
    from spikes.peer_frames.filesystem_capabilities import create_optional_symlink

    if not create_optional_symlink(alias, path):
        pytest.skip("optional symlink affordance unavailable to this Windows account")
    result = workspaceContext.LocalEditors.loadFile(facade, str(alias))
    if condition != "live":
        assert (
            result is None and loaded == [] and not any(c[0] == "reopen" for c in calls)
        )
    elif known:
        assert result.identity == document and loaded == []
    else:
        assert loaded == [str(alias)] and result is not None
    assert registry.resource(document).text == "retained unsaved" if known else True
    assert path.read_text() == "disk"


def test_direct_peer_group_load_delegates_once_and_empty_filename_refuses(monkeypatch):
    calls = []
    context = NS(editors=NS(loadFile=lambda *args: calls.append(args) or "item"))
    monkeypatch.setattr(pyzo, "application", object(), raising=False)
    monkeypatch.setattr(editorTabs, "context_for", lambda widget: context)
    assert editorTabs.EditorTabs.loadFile(object(), "shared.py", False, True) == "item"
    assert calls == [("shared.py", False, True)]
    assert editorTabs.EditorTabs.loadFile(object(), "") is None
    assert workspaceContext.LocalEditors.loadFile(object(), "") is None


def test_runtime_geometry_targets_refresh_native_registry_and_skip_disposed():
    from pyzo.core.workspaceRuntime import WorkspaceRuntime

    calls = []
    backend = NS(frame_windows={"retired": object()}, close=lambda: None)
    live = object()

    def refresh():
        calls.append("refresh")
        backend.frame_windows = {"live": live}

    backend._refresh_live_frame_windows = refresh
    runtime = WorkspaceRuntime(identity="owner", backend=backend, model=object())
    assert runtime.frame_windows == {"live": live} and calls == ["refresh"]
    runtime.dispose()
    assert runtime.frame_windows == {} and calls == ["refresh"]


def test_reload_notification_and_help_guard_are_local(monkeypatch):
    calls = []
    monkeypatch.setattr(pyzo, "editors", object(), raising=False)
    monkeypatch.setattr(pyzo, "shells", object(), raising=False)
    source = NS(
        _filename="local.py",
        textCursor=lambda: NS(blockNumber=lambda: 3),
        _loadTextFromFile=lambda path: calls.append(path),
        gotoLine=calls.append,
    )
    monkeypatch.setattr(
        editor,
        "context_for",
        lambda widget: NS(editors=NS(onCurrentChanged=lambda: calls.append("local"))),
    )
    editor.PyzoEditor.reload(source)
    assert calls == ["local.py", 4, "local"]
    queries = []
    tool = NS(helpFromExtension=lambda *args, **kwargs: queries.append((args, kwargs)))
    monkeypatch.setattr(
        baseTextCtrl,
        "context_for",
        lambda widget: NS(
            toolManager=NS(getTool=lambda name: tool),
            shells=NS(getCurrentShell=lambda: object()),
        ),
    )
    baseTextCtrl.BaseTextCtrl.processHelp(source, "owned", "calltip")
    assert queries == [(("owned", "calltip", False), {"requestSource": source})]


def test_failed_debug_navigation_focuses_captured_session(monkeypatch):
    from pyzo.core.shellStack import DebugStack

    focused = []
    monkeypatch.setattr(pyzo, "shells", object(), raising=False)
    shell = NS(setFocus=lambda: focused.append("session"))
    source = NS(_contextShell=lambda: shell)
    assert DebugStack.debugFocus(source, "invalid stack entry") == "Could not focus!"
    assert focused == ["session"]
    source._contextShell = lambda: None
    DebugStack.debugFocus(source, "invalid stack entry")
    assert focused == ["session"]


def test_application_load_delegates_before_dedup_or_empty_host(monkeypatch):
    from pyzo.core.editorGroups import EditorGroupManager

    source = NS(_activeGroup=object())
    calls = []
    monkeypatch.setattr(pyzo, "workspaceContext", object(), raising=False)
    monkeypatch.setattr(
        workspaceContext,
        "context_for",
        lambda group: NS(
            editors=NS(
                loadFile=lambda *args: calls.append((group, args)) or "local-item"
            )
        ),
    )
    assert EditorGroupManager.loadFile(source, "shared.py", False, True) == "local-item"
    assert calls == [(source._activeGroup, ("shared.py", False, True))]
