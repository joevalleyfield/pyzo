"""Module statusbar

Functionality for status bar in pyzo.

"""

from pyzo.qt import QtCore, QtWidgets


def cursorPositionText(editor):
    """Return Pyzo's stable editor-position status text."""

    nrow = 0
    ncol = 0
    sel = 0
    if editor:
        cur = editor.textCursor()
        nrow = cur.blockNumber() + 1
        ncol = cur.positionInBlock() + 1
        sel = cur.selectionEnd() - cur.selectionStart()
    return "Line: {}, Column: {}, Sel.: {} ".format(nrow, ncol, sel)


class StatusBar(QtWidgets.QStatusBar):
    """
    Add a statusbar to main window

    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # Cursor position
        self.cursor_pos = QtWidgets.QLabel(self)
        self.addPermanentWidget(self.cursor_pos)

    def updateCursorInfo(self, editor):
        self.cursor_pos.setText(cursorPositionText(editor))


class PeerStatusService(QtCore.QObject):
    """Compatibility facade over application-owned peer status state."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._clearTimer = QtCore.QTimer(self)
        self._clearTimer.setSingleShot(True)
        self._clearTimer.timeout.connect(self.clearMessage)
        self.visibility_getter = self.visibility_setter = None

    def showMessage(self, message, timeout=0):
        self._clearTimer.stop()
        self._state.set_message(message)
        if timeout > 0:
            self._clearTimer.start(timeout)

    def clearMessage(self):
        self._clearTimer.stop()
        self._state.set_message("")

    def currentMessage(self):
        return self._state.snapshot.message

    def updateCursorInfo(self, editor):
        self._state.set_cursor_text(cursorPositionText(editor))

    def setVisible(self, visible):
        if self.visibility_setter is not None:
            self.visibility_setter(visible)
        else:
            self._state.set_visible(visible)

    def isVisible(self):
        return (
            self.visibility_getter()
            if self.visibility_getter is not None
            else self._state.snapshot.visible
        )
