"""Application-owned editor execution commands."""

from __future__ import annotations

import os

from pyzo.qt import QtCore, QtWidgets

from pyzo.codeeditor.parsers.python_parser import CellCommentToken
import pyzo.codeeditor.parsers.tokens as Tokens


def _skip_nested_tokens(tokens, opening_index):
    stack = []
    closing = {"(": ")", "[": "]", "{": "}"}
    for index, token in enumerate(tokens[opening_index:], start=opening_index):
        text = str(token)
        if text in "([{":
            stack.append(text)
        elif text in ")]}":
            if not stack or closing[stack.pop()] != text:
                return None
            if not stack:
                return index
    return None


def find_left_result_in_tokens(tokens):
    """Return the assignment target whose value should be printed, if any."""

    left_result = None
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if isinstance(token, Tokens.NonIdentifierToken):
            text = str(token)
            if ";" in text:
                break
            equals_index = text.find("=")
            if equals_index >= 0:
                if text[equals_index : equals_index + 2] == "==":
                    break
                prefix = text[: equals_index + 1]
                assignments = (
                    " =",
                    ",=",
                    "+=",
                    "-=",
                    "**=",
                    "*=",
                    "//=",
                    "/=",
                    "%=",
                    "^=",
                    "&=",
                    "|=",
                    "<<=",
                    ">>=",
                    "@=",
                )
                for assignment in assignments:
                    if prefix.endswith(assignment):
                        left_result = (
                            "".join(str(item) for item in tokens[:token_index])
                            + prefix[: -len(assignment)]
                        )
                        break
                break
        elif isinstance(token, Tokens.ParenthesisToken) and str(token) in "([{":
            closing_index = _skip_nested_tokens(tokens, token_index)
            if closing_index is None:
                break
            token_index = closing_index + 1
            continue
        elif isinstance(token, Tokens.ParenthesisToken) and str(token) in ")]}":
            break
        token_index += 1

    if left_result is not None:
        left_result = left_result.strip()
        if left_result.endswith(","):
            left_result = left_result[:-1].rstrip()
    return left_result


class RunCommandService(QtCore.QObject):
    """Own Run-menu behavior without owning a menu or window."""

    def __init__(
        self,
        shells,
        editors,
        config,
        command_history,
        translate,
        dialog_parent=lambda: None,
        parent=None,
    ):
        super().__init__(parent)
        self._shells = shells
        self._editors = editors
        self._config = config
        self._commandHistory = command_history
        self._translate = translate
        self._dialogParent = dialog_parent
        self._selectionExecution = None
        self._mainEditor = None
        self._mainChanged = lambda: None

    def set_selection_execution(self, service):
        """Install workspace routing for all editor execution commands."""
        self._selectionExecution = service

    def set_main_services(self, editor_provider, changed):
        self._mainEditor = editor_provider
        self._mainChanged = changed

    def toggle_main(self, editor):
        execution = self._selectionExecution
        context = execution.selection_targets(editor).context
        if context is None:
            return False
        if not editor._filename:
            if not self._editors.saveFile(editor):
                return False
            if not execution.validate_editor_context(editor, context):
                return False
            if not editor._filename or editor.document().isModified():
                return False
        result = execution.toggle_main(editor, context=context)
        if result.applied:
            self._mainChanged()
        return result.applied

    def get_shell_and_editor(self, what, main_editor=False, given_editor=None):
        message = ""
        shell = self._shells.getExecutionShell()
        if shell is None:
            message += (
                self._translate("menu", "No shell to run code in.").rstrip() + " "
            )
        if given_editor is not None:
            editor = given_editor
        elif main_editor:
            editor = self._editors.getMainEditor()
            if editor is None:
                message += self._translate("menu", "There is no main file selected.")
        else:
            editor = self._editors.getCurrentEditor()
            if editor is None:
                message += self._translate("menu", "No editor selected.")
        if message:
            dialog = QtWidgets.QMessageBox(self._dialogParent())
            dialog.setWindowTitle(self._translate("menu dialog", "Could not run"))
            dialog.setText(
                self._translate("menu", "Could not run " + what + ":\n\n" + message)
            )
            dialog.setIcon(dialog.Icon.Warning)
            dialog.exec()
        return shell, editor

    def run_selected_advance(self):
        self.run_selected(advance=True)

    def run_current_line_and_print_result(self):
        self.run_selected(run_current_line_and_print_result=True)

    def run_selected(self, advance=False, run_current_line_and_print_result=False):
        execution = self._selectionExecution
        if execution is None:
            shell, editor = self.get_shell_and_editor("selection")
            if not shell or not editor:
                return
            provenance = None
        else:
            editor = self._editors.getCurrentEditor()
            resolved = execution.resolve_selection(
                editor, line=run_current_line_and_print_result
            )
            if not resolved.resolved:
                return
            provenance = resolved.provenance

        screen_cursor = editor.textCursor()
        run_cursor = editor.textCursor()
        run_cursor.setPosition(screen_cursor.selectionStart())
        run_cursor.movePosition(run_cursor.MoveOperation.StartOfBlock)
        first_line = run_cursor.blockNumber()
        run_cursor.setPosition(
            screen_cursor.selectionEnd(), run_cursor.MoveMode.KeepAnchor
        )
        if not (screen_cursor.hasSelection() and run_cursor.atBlockStart()):
            run_cursor.movePosition(
                run_cursor.MoveOperation.EndOfBlock, run_cursor.MoveMode.KeepAnchor
            )
        last_line = run_cursor.blockNumber()
        is_statement = first_line == last_line and screen_cursor.hasSelection()

        if is_statement or run_current_line_and_print_result:
            selected_text = screen_cursor.selectedText()
            if run_current_line_and_print_result:
                if first_line != last_line:
                    return
                selected_text = run_cursor.selectedText()
                if editor.parser().name().startswith("python"):
                    tokens = run_cursor.block().userData().tokens
                    if tokens:
                        if isinstance(tokens[-1], Tokens.CommentToken):
                            tokens = tokens[:-1]
                        left_result = find_left_result_in_tokens(tokens)
                        selected_text = "".join(str(token) for token in tokens).strip()
                        if left_result is not None:
                            selected_text += "; " + left_result
            code = selected_text.replace("\u2029", "\n").strip()

            def send(shell):
                self._commandHistory.append(code)
                shell.executeCommand(code + "\n")
        else:
            code = run_cursor.selectedText().replace("\u2029", "\n")
            self._show_what_to_execute(editor, run_cursor)
            filename = editor.id()

            def send(shell):
                shell.executeCode(code, filename, first_line)

        if execution is None:
            send(shell)
        elif not execution.dispatch(provenance, send):
            return

        if advance:
            self._advance(run_cursor, editor=editor)
        return provenance

    def _advance(self, run_cursor, editor=None):
        if editor is None:
            shell, editor = self.get_shell_and_editor("selection")
            if not shell or not editor:
                return
        cursor = editor.textCursor()
        cursor.setPosition(run_cursor.position())
        cursor.movePosition(cursor.MoveOperation.NextBlock)
        editor.setTextCursor(cursor)

    def run_cell_advance(self):
        return self.run_cell(advance=True)

    def run_cell(self, advance=False):
        execution = self._selectionExecution
        if execution is None:
            shell, editor = self.get_shell_and_editor("cell")
            if not shell or not editor:
                return
            provenance = None
        else:
            from spikes.peer_frames.execution_routing import ExecutionCommand

            editor = self._editors.getCurrentEditor()
            resolved = execution.resolve_editor(editor, ExecutionCommand.EXECUTE_CELL)
            if not resolved.resolved:
                return
            provenance = resolved.provenance

        cell_name = ""
        if editor.parser().name().startswith("python"):

            def is_cell_comment(cursor):
                return any(
                    isinstance(token, CellCommentToken)
                    for token in cursor.block().userData().tokens[:2]
                )

        else:

            def is_cell_comment(cursor):
                return cursor.block().text().lstrip().startswith(("##", "#%%", "# %%"))

        run_cursor = editor.textCursor()
        run_cursor.movePosition(run_cursor.MoveOperation.StartOfBlock)
        while True:
            if is_cell_comment(run_cursor):
                if not run_cursor.block().next().isValid():
                    return
                line = run_cursor.block().text().lstrip()
                run_cursor.movePosition(run_cursor.MoveOperation.NextBlock)
                cell_name = line.lstrip("#% ").strip()
                break
            if not run_cursor.block().previous().isValid():
                break
            run_cursor.movePosition(run_cursor.MoveOperation.PreviousBlock)

        line_number = run_cursor.blockNumber()
        if len(cell_name) > 20:
            cell_name = cell_name[:17] + "..."
        while True:
            if is_cell_comment(run_cursor):
                run_cursor.movePosition(
                    run_cursor.MoveOperation.Left, run_cursor.MoveMode.KeepAnchor
                )
                break
            if not run_cursor.block().next().isValid():
                run_cursor.movePosition(
                    run_cursor.MoveOperation.EndOfBlock,
                    run_cursor.MoveMode.KeepAnchor,
                )
                break
            run_cursor.movePosition(
                run_cursor.MoveOperation.NextBlock, run_cursor.MoveMode.KeepAnchor
            )

        code = run_cursor.selectedText().replace("\u2029", "\n")
        self._show_what_to_execute(editor, run_cursor)
        filename = editor.id()

        def send(shell):
            shell.executeCode(code, filename, line_number, cell_name or " ")

        if execution is None:
            send(shell)
        elif not execution.dispatch(provenance, send):
            return
        if advance:
            self._advance(run_cursor, editor=editor)
        return provenance

    def run_file(self, run_mode, given_editor=None):
        as_script, main_file = run_mode
        execution = self._selectionExecution
        if execution is not None:
            from spikes.peer_frames.execution_routing import ExecutionCommand

            invoking_editor = given_editor or self._editors.getCurrentEditor()
            command = (
                (
                    ExecutionCommand.RUN_MAIN_AS_SCRIPT
                    if main_file
                    else ExecutionCommand.RUN_FILE_AS_SCRIPT
                )
                if as_script
                else (
                    ExecutionCommand.EXECUTE_MAIN
                    if main_file
                    else ExecutionCommand.EXECUTE_FILE
                )
            )
            resolved = execution.resolve_editor(invoking_editor, command)
            if not resolved.resolved:
                return
            provenance = resolved.provenance
            source = execution.file_source(provenance, main=main_file)
            if source is None:
                return
            if as_script:
                editor = (
                    self._mainEditor(source.document_id, provenance.workspace_id)
                    if main_file
                    else invoking_editor
                )
                if editor is None or not execution.validate_file(provenance, source):
                    return
                if self._run_script(editor, provenance=provenance, source=source):
                    return provenance
                return
            if not main_file:
                filename, text = self._get_code_of_file(invoking_editor)
            else:
                filename, text = source.filename, source.text
                if self._mainEditor is not None:
                    editor = self._mainEditor(
                        source.document_id, provenance.workspace_id, reopen=False
                    )
                    if editor is not None:
                        self._show_what_to_execute(editor)
            if execution.validate_file(provenance, source) and execution.dispatch(
                provenance,
                lambda shell: shell.executeCode(
                    text, filename, changeDir=self._config.settings.changeDirOnFileExec
                ),
            ):
                return provenance
            return
        description = "main file" if main_file else "file"
        if as_script:
            description += self._translate("menu", " (as script)")
        shell, editor = self.get_shell_and_editor(description, main_file, given_editor)
        if not shell or not editor:
            return
        if as_script:
            self._run_script(editor, shell)
        else:
            filename, text = self._get_code_of_file(editor)
            shell.executeCode(
                text,
                filename,
                changeDir=self._config.settings.changeDirOnFileExec,
            )

    def _show_what_to_execute(self, editor, run_cursor=None):
        if run_cursor is None:
            run_cursor = editor.textCursor()
            run_cursor.movePosition(run_cursor.MoveOperation.Start)
            run_cursor.movePosition(
                run_cursor.MoveOperation.End, run_cursor.MoveMode.KeepAnchor
            )
        editor.showRunCursor(run_cursor)

    def _get_code_of_file(self, editor):
        text = editor.toPlainText()
        self._show_what_to_execute(editor)
        return editor.id(), text

    def _run_script(self, editor, shell=None, *, provenance=None, source=None):
        execution = self._selectionExecution
        if provenance is not None:
            if not execution.validate_file(provenance, source):
                return False
            context = execution.source_editor_context(editor, provenance, source)
            if context is None:
                return False
        save_ok = self._editors.saveFile(editor)
        if provenance is not None:
            if not execution.validate_file(provenance, source):
                return False
            if (
                execution.source_editor_context(
                    editor, provenance, source, context=context
                )
                is None
            ):
                return False
        if save_ok and not editor.document().isModified():
            self._show_what_to_execute(editor)
            filename = editor._filename

            def send(shell):
                if shell._startup_info.get("ipython", "") == "yes":
                    directory = os.path.normpath(
                        os.path.normcase(os.path.dirname(filename))
                    )
                    shell._ctrl_command.send(f'%cd "{directory}"\n')
                    shell._ctrl_command.send(f'%run -i "{filename}"\n')
                else:
                    shell.restart(filename)

            if provenance is None:
                send(shell)
                return True
            if execution.validate_file(provenance, source):
                return execution.dispatch(provenance, send)
            return False
        else:
            dialog = QtWidgets.QMessageBox(self._dialogParent())
            dialog.setWindowTitle(
                self._translate("menu dialog", "Could not run script.")
            )
            dialog.setText(self._translate("menu", "Could not save the file."))
            dialog.setIcon(dialog.Icon.Warning)
            dialog.exec()
