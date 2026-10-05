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

    def set_selection_execution(self, service):
        """Install the peer Selection route independently of File/Main Run."""
        self._selectionExecution = service

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
        self.run_cell(advance=True)

    def run_cell(self, advance=False):
        shell, editor = self.get_shell_and_editor("cell")
        if not shell or not editor:
            return

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
        shell.executeCode(code, editor.id(), line_number, cell_name or " ")
        if advance:
            self._advance(run_cursor)

    def run_file(self, run_mode, given_editor=None):
        as_script, main_file = run_mode
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

    def _run_script(self, editor, shell):
        save_ok = self._editors.saveFile(editor)
        if save_ok and not editor.document().isModified():
            self._show_what_to_execute(editor)
            if shell._startup_info.get("ipython", "") == "yes":
                directory = os.path.normpath(
                    os.path.normcase(os.path.dirname(editor._filename))
                )
                shell._ctrl_command.send(f'%cd "{directory}"\n')
                shell._ctrl_command.send(f'%run -i "{editor._filename}"\n')
            else:
                shell.restart(editor._filename)
        else:
            dialog = QtWidgets.QMessageBox(self._dialogParent())
            dialog.setWindowTitle(
                self._translate("menu dialog", "Could not run script.")
            )
            dialog.setText(self._translate("menu", "Could not save the file."))
            dialog.setIcon(dialog.Icon.Warning)
            dialog.exec()
