"""Native payloads and save-dialog ordering for all workspace Run variants."""

from types import SimpleNamespace

import pytest

from pyzo.qt import QtWidgets
from pyzo.core.runCommands import RunCommandService
from spikes.peer_frames.execution_routing import AssignmentMode
from test_workspace_execution import world as execution_world  # noqa: F401


@pytest.fixture
def run_world(request, tmp_path, monkeypatch):
    w = request.getfixturevalue("execution_world")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    native = []
    for index, original in enumerate(w.editors):
        editor = QtWidgets.QPlainTextEdit()
        editor._peerViewIdentity = original._peerViewIdentity
        editor._peerDocumentIdentity = original._peerDocumentIdentity
        editor._filename = str(tmp_path / f"source-{index}.py")
        editor.id = lambda editor=editor: editor._filename or "<tmp>"
        editor.showRunCursor = lambda cursor: None
        editor.parser = lambda: SimpleNamespace(name=lambda: "text")
        editor.setPlainText("## first\ncell_value = 41\n## next\nnever_run = 99\n")
        editor.document().setModified(True)
        w.docs.adopt_saved(
            editor._peerDocumentIdentity, editor._filename, "# disk baseline\n"
        )
        w.docs.replace_text(editor._peerViewIdentity, editor.toPlainText())
        native.append(editor)
    w.editors = native
    w.current = native[0]
    w.saves, w.dialogs = [], []
    for shell in w.shells:
        shell.calls, shell.restarts, shell.commands = [], [], []
        shell._startup_info = {}
        shell.executeCode = lambda *args, shell=shell, **kwargs: shell.calls.append(
            (args, kwargs)
        )
        shell.restart = lambda path, shell=shell: shell.restarts.append(path)
        shell._ctrl_command = SimpleNamespace(send=shell.commands.append)
        shell.executeCommand = shell.commands.append

    def save(editor):
        w.saves.append(editor)
        w.docs.adopt_saved(
            editor._peerDocumentIdentity, editor._filename, editor.toPlainText()
        )
        editor.document().setModified(False)
        return True

    w.save = save
    editors = SimpleNamespace(
        getCurrentEditor=lambda: w.current, saveFile=lambda editor: w.save(editor)
    )
    w.run = RunCommandService(
        SimpleNamespace(getExecutionShell=lambda: w.shells[2]),
        editors,
        SimpleNamespace(settings=SimpleNamespace(changeDirOnFileExec=True)),
        [],
        lambda domain, text: text,
    )
    w.run.set_selection_execution(w.service)
    w.run.set_main_services(
        lambda doc, owner, **kwargs: next(
            (
                e
                for e in native
                if e._peerDocumentIdentity == doc
                and w.docs.view(e._peerViewIdentity).workspace_id == owner
            ),
            None,
        ),
        lambda: None,
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "exec", lambda dialog: w.dialogs.append(dialog.text())
    )
    w.service.foreground_shell(w.shells[0])
    yield w
    for editor in native:
        editor.deleteLater()
    app.sendPostedEvents()


@pytest.mark.parametrize("main", [False, True])
def test_execute_file_and_main_use_dirty_buffer_without_save(run_world, main):
    w = run_world
    source = w.editors[1] if main else w.editors[0]
    if main:
        # Main is a resource reference; the invoking view's explicit assignment owns execution.
        w.docs.set_main(w.a, source._peerDocumentIdentity)
    assert w.service.assign_selection(w.editors[0], w.shells[1]).applied
    receipt = w.run.run_file((False, main))
    assert receipt.assignment_mode is AssignmentMode.EXPLICIT
    assert receipt.command.value == ("execute-main" if main else "execute-file")
    assert w.shells[1].calls == [
        ((source.toPlainText(), source._filename), {"changeDir": True})
    ]
    assert not w.saves and not w.shells[1].restarts and not w.shells[2].calls


def test_context_file_uses_given_editor_owner_with_foreign_current(run_world):
    w = run_world
    w.current = w.editors[1]
    receipt = w.run.run_file((False, False), w.editors[0])
    assert receipt.workspace_id == w.a
    assert len(w.shells[0].calls) == 1 and not w.shells[2].calls


@pytest.mark.parametrize("advance", [False, True])
def test_cell_payload_and_advance_stay_with_invoking_editor(run_world, advance):
    w = run_world
    editor = w.current
    original = w.shells[0].executeCode

    def send(*args, **kwargs):
        w.current = w.editors[1]
        original(*args, **kwargs)

    w.shells[0].executeCode = send
    receipt = w.run.run_cell_advance() if advance else w.run.run_cell()
    assert receipt.command.value == "execute-cell"
    assert w.shells[0].calls == [
        (("cell_value = 41", editor._filename, 1, "first"), {})
    ]
    assert editor.textCursor().blockNumber() == (2 if advance else 0)
    assert w.editors[1].textCursor().position() == 0
    assert not w.saves


@pytest.mark.parametrize("main", [False, True])
@pytest.mark.parametrize("ipython", [False, True])
def test_script_saves_then_uses_exact_selected_native_strategy(
    run_world, main, ipython
):
    w = run_world
    source = w.editors[0]
    if main:
        w.docs.set_main(w.a, source._peerDocumentIdentity)
        w.docs.move_view(w.editors[2]._peerViewIdentity, w.a)
        w.current = w.editors[2]  # Same-owner invoking view distinct from main.
    assert w.service.assign_selection(w.current, w.shells[0]).applied
    if ipython:
        w.shells[0]._startup_info = {"ipython": "yes"}
    receipt = w.run.run_file((True, main))
    assert receipt.command.value == (
        "run-main-as-script" if main else "run-file-as-script"
    )
    assert receipt.policy.autosave and receipt.policy.recycle is (not ipython)
    assert w.saves == [source]
    if ipython:
        import os
        from pathlib import Path

        assert w.shells[0].commands == [
            f'%cd "{os.path.normcase(str(Path(source._filename).parent))}"\n',
            f'%run -i "{source._filename}"\n',
        ]
        assert not w.shells[0].restarts
    else:
        assert w.shells[0].restarts == [source._filename]
        assert not w.shells[0].commands
    assert not w.shells[2].restarts and not w.shells[2].commands


@pytest.mark.parametrize("failure", ["cancel", "dirty"])
def test_failed_script_save_never_starts(run_world, failure):
    w = run_world
    w.save = lambda editor: failure == "dirty"
    assert w.run.run_file((True, False)) is None
    assert not w.shells[0].restarts and not w.shells[0].commands
    assert w.dialogs == ["Could not save the file."]


@pytest.mark.parametrize(
    "drift", ["view", "owner", "kernel", "target", "main", "source-view"]
)
def test_save_dialog_drift_cannot_restart_captured_or_new_target(run_world, drift):
    w = run_world
    source = w.editors[0]
    w.docs.set_main(w.a, source._peerDocumentIdentity)
    # Third native editor must invoke A's main; source and invoker have distinct lifetimes.
    w.docs.move_view(w.editors[2]._peerViewIdentity, w.a)
    w.current = w.editors[2]

    def save(editor):
        editor.document().setModified(False)
        if drift == "view":
            w.retired.append(w.current)
            w.docs.close_view(w.current._peerViewIdentity)
        elif drift == "owner":
            w.docs.move_view(w.current._peerViewIdentity, w.b)
        elif drift == "kernel":
            w.shells[0]._context = SimpleNamespace(connection_count=1)
        elif drift == "target":
            w.service.foreground_shell(w.shells[1])
        elif drift == "main":
            w.docs.set_main(w.a, w.editors[1]._peerDocumentIdentity)
        else:
            w.retired.append(editor)
            w.docs.close_view(editor._peerViewIdentity)
        return True

    w.save = save
    assert w.run.run_file((True, True)) is None
    assert not any(s.restarts or s.commands for s in w.shells)
    assert w.messages


def test_save_as_and_focus_only_change_keep_captured_explicit_route(run_world):
    w = run_world
    source = w.current
    assert w.service.assign_selection(source, w.shells[0]).applied

    def save(editor):
        editor._filename += ".renamed.py"
        w.docs.adopt_saved(
            editor._peerDocumentIdentity, editor._filename, editor.toPlainText()
        )
        editor.document().setModified(False)
        w.current = w.editors[1]
        w.service.foreground_shell(w.shells[1])
        return True

    w.save = save
    assert w.run.run_file((True, False)) is not None
    assert w.shells[0].restarts == [source._filename]
    assert not w.shells[1].restarts


@pytest.mark.parametrize("state", ["saved", "pathless", "cancel", "moved", "retired"])
def test_main_designation_uses_captured_owner_and_native_pathless_save(
    run_world, state
):
    w = run_world
    editor = w.current
    if state != "saved":
        editor._filename = ""
    original_save = w.save

    def save(editor):
        if state == "cancel":
            return False
        editor._filename = "/chosen-main.py"
        result = original_save(editor)
        if state == "moved":
            w.docs.move_view(editor._peerViewIdentity, w.b)
        elif state == "retired":
            w.retired.append(editor)
            w.docs.close_view(editor._peerViewIdentity)
            editor.document = lambda: (_ for _ in ()).throw(
                AssertionError("retired Qt access")
            )
        return result

    w.save = save
    assert w.run.toggle_main(editor) is (state in {"saved", "pathless"})
    reference = w.docs.main(w.a)
    if state in {"saved", "pathless"}:
        assert reference.document_id == editor._peerDocumentIdentity
        assert w.run.toggle_main(editor)
        assert w.docs.main(w.a) is None
    else:
        assert reference is None and w.docs.main(w.b) is None


def test_untitled_file_executes_buffer_without_saving(run_world):
    w = run_world
    editor = w.current
    doc = w.docs.create_document(editor.toPlainText(), dirty=True)
    editor._peerDocumentIdentity = doc
    editor._peerViewIdentity = w.docs.open_view(doc, w.a)
    editor._filename = ""
    receipt = w.run.run_file((False, False))
    assert receipt.policy.uses_buffer and not receipt.policy.autosave
    assert w.shells[0].calls == [((editor.toPlainText(), "<tmp>"), {"changeDir": True})]
    assert not w.saves and not w.shells[0].restarts


def test_refused_cell_does_not_send_or_advance(run_world):
    w = run_world
    w.service.dispatch = lambda provenance, send: False
    assert w.run.run_cell_advance() is None
    assert w.current.textCursor().position() == 0
    assert not any(s.calls for s in w.shells) and not w.saves
