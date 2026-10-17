"""Explicit user focus handoff between native peer windows."""

import pyzo
from pyzo.qt import QtCore, QtWidgets
from shiboken6 import isValid


def focus_widget(widget):
    """Foreground a live target in the current workspace, then focus it.

    Background debugger/file updates may prepare views, but must not activate
    another workspace. Native activation stays in the input event's turn so
    Wayland can associate the request with its current activation token.
    """
    if not isinstance(widget, QtWidgets.QWidget) or not isValid(widget):
        return False
    service = getattr(pyzo, "workspaceContext", None)
    if service is not None:
        from .workspaceContext import owner_for

        main = service.main
        if not main._externalFileOpenAllowed():
            return False
        try:
            owner = owner_for(widget)
        except RuntimeError:  # Its retained view may already have retired.
            return False
        if owner is None or owner != main._peerWorkspaces.active_workspace_id:
            return False
        window = widget.window()
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
    widget.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
    return True
