"""Module main

This module contains the main frame. Implements the main window.
Also adds some variables to the pyzo namespace, such as the callLater
function which is also defined here.

"""

import os
import sys
import time
import base64
import gc
import tempfile
import traceback
import weakref
from queue import Queue, Empty

import pyzo
from pyzo.core.icons import IconArtist
from pyzo.core import commandline
from pyzo.core.statusbar import PeerStatusService, StatusBar
from pyzo.qt import QtCore, QtGui, QtWidgets
from pyzo.core.splash import SplashWidget
from pyzo.util import paths
from pyzo.util import zon as ssdf  # zon is ssdf-light
from pyzo import translate
from pyzo.core.views import ViewManager


class _AdsWindowOrderTracker(QtCore.QObject):
    """Freeze the real dock-window activation order for each ADS drag.

    QtADS gives its main dock manager a permanent z-order of zero, which
    incorrectly lets a background floating container beat a foreground main
    window on macOS. Floating-to-floating order remains owned by QtADS; this
    tracker only determines whether the main window must outrank all of them.
    """

    def __init__(self, main_window):
        super().__init__(main_window)
        self._mainWindow = main_window
        self._dockManager = None
        self._windowOrder = []
        self._trackedWindowIds = set()
        self._dragSnapshot = None
        self._dragActive = False
        self._tracePath = None
        self._lastTraceState = None
        self._recordWindow(main_window)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._syncDragState)
        self._timer.start()

    def setDockManager(self, dock_manager):
        self._dockManager = dock_manager

    def setTracePath(self, trace_path):
        self._tracePath = trace_path

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type == QtCore.QEvent.Type.WindowActivate:
            window = self._dockWindow(watched)
            self._syncDragState()
            if window is not None:
                self._handleWindowActivation(window)
        return False

    def _dockWindow(self, watched):
        if not isinstance(watched, QtWidgets.QWidget):
            return None
        window = watched.window()
        if window is self._mainWindow:
            return window
        if type(window).__name__ == "CFloatingDockContainer":
            return window
        return None

    def _recordWindow(self, window):
        live = [item for item in self._liveWindowOrder() if item is not window]
        live.append(window)
        self._windowOrder = live
        window_id = id(window)
        if window_id not in self._trackedWindowIds and hasattr(window, "destroyed"):
            self._trackedWindowIds.add(window_id)
            window.destroyed.connect(
                lambda _object=None, window_id=window_id: self._forgetWindowId(
                    window_id
                )
            )

    def _forgetWindowId(self, window_id):
        self._trackedWindowIds.discard(window_id)

        def without_window(order):
            if order is None:
                return None
            return tuple(window for window in order if id(window) != window_id)

        self._windowOrder = [
            window for window in self._windowOrder if id(window) != window_id
        ]
        self._dragSnapshot = without_window(self._dragSnapshot)

    def _handleWindowActivation(self, window):
        if not self._dragActive:
            self._recordWindow(window)

    def _liveWindowOrder(self):
        return list(self._windowOrder)

    def _activeSourceWindows(self):
        if self._dockManager is None:
            return set()
        return {
            floating
            for floating in self._dockManager.floatingWidgets()
            if floating is not None and floating.isDraggingActive()
        }

    def _previewDragActive(self):
        return any(
            type(window).__name__ == "CFloatingDragPreview" and window.isVisible()
            for window in QtWidgets.QApplication.topLevelWidgets()
        )

    def _syncDragState(self):
        active = bool(self._activeSourceWindows()) or self._previewDragActive()
        self._setDragActive(active)

    def _setDragActive(self, active):
        if active and not self._dragActive:
            self._dragSnapshot = tuple(self._liveWindowOrder())
        elif not active and self._dragActive:
            self._dragSnapshot = None
        self._dragActive = active

    @staticmethod
    def _foregroundEligibleWindow(window_order, source_windows):
        for window in reversed(window_order):
            if window in source_windows:
                continue
            try:
                visible = window.isVisible()
            except (AttributeError, RuntimeError):
                visible = True
            if visible:
                return window
        return None

    def mainWindowLeads(self):
        self._syncDragState()
        order = self._dragSnapshot or tuple(self._liveWindowOrder())
        sources = self._activeSourceWindows()
        foreground = self._foregroundEligibleWindow(order, sources)
        main_leads = foreground is self._mainWindow
        trace_state = (
            self._dragActive,
            tuple(id(window) for window in order),
            tuple(sorted(id(window) for window in sources)),
            id(foreground) if foreground is not None else None,
            main_leads,
        )
        if self._tracePath and trace_state != self._lastTraceState:
            self._lastTraceState = trace_state
            describe = lambda window: (
                f"{type(window).__name__}:{window.windowTitle()}"
            )
            with open(self._tracePath, "a", encoding="utf-8") as trace:
                trace.write(
                    "{:.6f} order drag={} cursor=({}, {}) order={} "
                    "sources={} foreground={} main_leads={}\n".format(
                        time.monotonic(),
                        self._dragActive,
                        QtGui.QCursor.pos().x(),
                        QtGui.QCursor.pos().y(),
                        [describe(window) for window in order],
                        [describe(window) for window in sources],
                        describe(foreground) if foreground is not None else None,
                        main_leads,
                    )
                )
        return main_leads


class _AdsTraceFilter(QtCore.QObject):
    """Opt-in event trace for diagnosing QtADS drag feedback."""

    _interesting = {
        QtCore.QEvent.Type.DragEnter,
        QtCore.QEvent.Type.DragMove,
        QtCore.QEvent.Type.DragLeave,
        QtCore.QEvent.Type.Drop,
        QtCore.QEvent.Type.Show,
        QtCore.QEvent.Type.Hide,
        QtCore.QEvent.Type.Paint,
        QtCore.QEvent.Type.WindowActivate,
        QtCore.QEvent.Type.WindowDeactivate,
    }

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._file = open(path, "a", encoding="utf-8")

    def eventFilter(self, watched, event):
        watched_name = type(watched).__name__
        event_type = event.type()
        is_drag_event = event_type in {
            QtCore.QEvent.Type.DragEnter,
            QtCore.QEvent.Type.DragMove,
            QtCore.QEvent.Type.DragLeave,
            QtCore.QEvent.Type.Drop,
        }
        is_ads_widget = "Dock" in watched_name or "Overlay" in watched_name
        if event_type in self._interesting and (is_drag_event or is_ads_widget):
            widget = watched if isinstance(watched, QtWidgets.QWidget) else None
            name = widget.objectName() if widget else ""
            geometry = widget.geometry().getRect() if widget else None
            self._file.write(
                "{:.6f} {} class={} name={} visible={} geometry={} event={}\n".format(
                    time.monotonic(),
                    "widget",
                    watched_name,
                    name,
                    widget.isVisible() if widget else None,
                    geometry,
                    event_type.name,
                )
            )
            self._file.flush()
            if (
                watched_name == "CDockOverlay"
                and event_type == QtCore.QEvent.Type.Show
            ):
                for top_level in QtWidgets.QApplication.topLevelWidgets():
                    self._file.write(
                        (
                            "{:.6f} top-level class={} title={} visible={} "
                            "geometry={}\n"
                        ).format(
                            time.monotonic(),
                            type(top_level).__name__,
                            top_level.windowTitle(),
                            top_level.isVisible(),
                            top_level.geometry().getRect(),
                        )
                    )
                self._file.flush()
        return False

    def close(self):
        self._file.close()


class _AdsDropGuide(QtWidgets.QWidget):
    """Application-owned five-point guide over QtADS's active drop overlay."""

    def __init__(self, overlays, parent=None):
        flags = (
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self._overlays = tuple(overlays)
        self._visibleOverlays = []
        self.setWindowTitle("PyzoDockDropGuide")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._refresh)

    def sync(self, shown_overlay=None):
        if not self._timer.isActive():
            self._timer.start()
        self._refresh(shown_overlay)

    def hideGuide(self):
        self._timer.stop()
        self._visibleOverlays = []
        self.hide()

    def _refresh(self, shown_overlay=None):
        visible = [overlay for overlay in self._overlays if overlay.isVisible()]
        # A Show event reaches the event filter immediately before Qt marks
        # the overlay visible. Include that known-current target so the guide
        # can move to an overlapping floating window in the same frame.
        if shown_overlay is not None and shown_overlay not in visible:
            visible.append(shown_overlay)
        if not visible:
            self.hideGuide()
            return
        self._visibleOverlays = visible
        geometry = visible[0].geometry()
        for overlay in visible[1:]:
            geometry = geometry.united(overlay.geometry())
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        import PySide6QtAds as QtAds

        if not self._visibleOverlays:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # QtADS owns a container overlay followed by a dock-area overlay. Draw
        # both at once: amber outer targets split the whole container, while
        # blue inner targets split/tab within the hovered dock area.
        container_overlay, dock_area_overlay = self._overlays
        if container_overlay in self._visibleOverlays:
            self._paintTargets(
                painter,
                container_overlay,
                stride=0,
                accent=QtGui.QColor(255, 174, 66),
                outer=True,
                QtAds=QtAds,
            )
        if dock_area_overlay in self._visibleOverlays:
            self._paintTargets(
                painter,
                dock_area_overlay,
                stride=45,
                accent=QtGui.QColor(68, 177, 255),
                outer=False,
                QtAds=QtAds,
            )

    def _paintTargets(self, painter, overlay, stride, accent, outer, QtAds):
        size = 45
        overlay_rect = QtCore.QRect(overlay.geometry())
        overlay_rect.translate(-self.geometry().topLeft())
        center = overlay_rect.center()
        if outer:
            margin = 4
            centers = {
                QtAds.CenterDockWidgetArea: center,
                QtAds.LeftDockWidgetArea: QtCore.QPoint(
                    overlay_rect.left() + margin + size // 2,
                    center.y(),
                ),
                QtAds.RightDockWidgetArea: QtCore.QPoint(
                    overlay_rect.right() - margin - size // 2,
                    center.y(),
                ),
                QtAds.TopDockWidgetArea: QtCore.QPoint(
                    center.x(),
                    overlay_rect.top() + margin + size // 2,
                ),
                QtAds.BottomDockWidgetArea: QtCore.QPoint(
                    center.x(),
                    overlay_rect.bottom() - margin - size // 2,
                ),
            }
        else:
            centers = {
                QtAds.CenterDockWidgetArea: center,
                QtAds.LeftDockWidgetArea: center + QtCore.QPoint(-stride, 0),
                QtAds.RightDockWidgetArea: center + QtCore.QPoint(stride, 0),
                QtAds.TopDockWidgetArea: center + QtCore.QPoint(0, -stride),
                QtAds.BottomDockWidgetArea: center + QtCore.QPoint(0, stride),
            }
        allowed = overlay.allowedAreas()
        hovered = overlay.dropAreaUnderCursor()

        for area, target_center in centers.items():
            if not bool(allowed & area):
                continue
            rect = QtCore.QRect(0, 0, size, size)
            rect.moveCenter(target_center)
            active = area == hovered
            fill = QtGui.QColor(42, 54, 68, 245)
            border = accent if active or outer else QtGui.QColor(190, 205, 220)
            painter.setPen(QtGui.QPen(border, 3 if active else 2))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 7, 7)
            self._paintDirection(painter, rect, area, border, outer, QtAds)

    def _paintDirection(self, painter, rect, area, color, outer, QtAds):
        painter.setPen(
            QtGui.QPen(
                color,
                3,
                QtCore.Qt.PenStyle.SolidLine,
                QtCore.Qt.PenCapStyle.RoundCap,
                QtCore.Qt.PenJoinStyle.RoundJoin,
            )
        )
        c = rect.center()
        if area == QtAds.CenterDockWidgetArea:
            inner = rect.adjusted(11, 12, -11, -10)
            painter.drawRoundedRect(inner, 2, 2)
            painter.drawLine(
                inner.topLeft() + QtCore.QPoint(0, 6),
                inner.topRight() + QtCore.QPoint(0, 6),
            )
            return
        delta = {
            QtAds.LeftDockWidgetArea: QtCore.QPoint(-9, 0),
            QtAds.RightDockWidgetArea: QtCore.QPoint(9, 0),
            QtAds.TopDockWidgetArea: QtCore.QPoint(0, -9),
            QtAds.BottomDockWidgetArea: QtCore.QPoint(0, 9),
        }[area]
        tip = c + delta
        if delta.x():
            wing1 = tip - delta + QtCore.QPoint(0, -7)
            wing2 = tip - delta + QtCore.QPoint(0, 7)
        else:
            wing1 = tip - delta + QtCore.QPoint(-7, 0)
            wing2 = tip - delta + QtCore.QPoint(7, 0)
        painter.drawLine(wing1, tip)
        painter.drawLine(tip, wing2)
        if outer:
            second_tip = tip - delta
            painter.drawLine(wing1 - delta, second_tip)
            painter.drawLine(second_tip, wing2 - delta)


class _AdsDropGuideController(QtCore.QObject):
    def __init__(self, overlays, parent=None):
        super().__init__(parent)
        self._overlays = tuple(overlays)
        self._guide = _AdsDropGuide(self._overlays)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched in self._overlays and event_type in {
            QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.Hide,
        }:
            if event_type == QtCore.QEvent.Type.Show:
                self._guide.sync(watched)
            # Resolve current visibility after QtADS completes this event.
            # The callback does not retain the event's target, so stale show
            # and hide events cannot move the guide to an obsolete geometry.
            QtCore.QTimer.singleShot(0, self._guide.sync)
        return False


class _PeerPaneFocusFilter(QtCore.QObject):
    """Route content focus to application pane identity in peer mode."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self._mainWindow = main_window

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.FocusIn:
            self._mainWindow._activatePeerPaneForWidget(watched)
        return False


class _PyzoEditorDocumentLifecycle:
    """Adapt one current Pyzo document to application lifecycle ownership."""

    def __init__(self, main_window, pane_id, item):
        self._mainWindow = main_window
        self._paneId = pane_id
        self._item = item

    def is_dirty(self):
        return self._item.dirty

    def save_document(self):
        host = pyzo.editors._group_for_editor(self._item.editor)
        return bool(host is not None and host.saveFile(self._item.editor))

    def choose_dirty_close(self):
        host = pyzo.editors._group_for_editor(self._item.editor)
        return "cancel" if host is None else host.askDirtyCloseChoice(self._item.editor)

    def recovery_snapshot(self):
        return self._mainWindow._snapshotPeerDocument(self._item)

    def discard_recovery_for_exit(self):
        self._mainWindow._retirePeerRecoveryForEditor(
            self._item.editor, destructive_fallback=True
        )
        self._item.editor.document().setModified(False)

    def close_document(self, disposition):
        if disposition.value == "discarded":
            self._mainWindow._retirePeerRecoveryForEditor(
                self._item.editor, destructive_fallback=True
            )
            self._item.editor.document().setModified(False)
        elif disposition.value == "retained":
            self._item.editor.document().setModified(False)
        self._mainWindow._closeCommittedPeerEditor(
            self._paneId, self._item.editor
        )


class _PyzoShellLifecycle:
    """Adapt one Pyzo interpreter shell to application lifecycle ownership."""

    def __init__(self, main_window, pane_id, shell):
        self._mainWindow = main_window
        self._paneId = pane_id
        self._shell = shell

    def shell_state(self):
        states = self._mainWindow._PeerShellState
        context = self._shell._context
        if self._shell._state == "Dead":
            return states.DEAD
        if context is None or not context.connection_count:
            return states.DEAD
        debug_state = self._shell._debugState or {}
        if self._shell._state == "Debug" or debug_state.get("debugmode", 0) > 0:
            return states.DEBUGGING
        if self._shell._state in ("Busy", "Very busy"):
            return states.RUNNING
        if self._shell._state in ("Ready", "More"):
            return states.IDLE
        return states.STARTING_OR_UNKNOWN

    def choose_live_close(self):
        return self._mainWindow._askPeerShellClose(self)

    def close_shell(self, disposition):
        self._mainWindow._closeCommittedPeerShell(
            self._paneId,
            self._shell,
            terminate=disposition.value == "terminated",
        )


class _PyzoToolClosePolicy:
    """Commit tool cleanup only after application topology closes its pane."""

    def __init__(self, main_window, pane_id, dock):
        self._mainWindow = main_window
        self._paneId = pane_id
        self._dock = dock

    def prepare_close(self, pane, request):
        return self._mainWindow._PeerCloseVote(
            pane.id, self._mainWindow._PeerCloseDecision.ALLOW
        )

    def validate_close(self, pane, request):
        return self._mainWindow._PeerCloseVote(
            pane.id, self._mainWindow._PeerCloseDecision.ALLOW
        )

    def did_close(self, pane, request):
        self._mainWindow._closeCommittedPeerTool(
            self._paneId, self._dock
        )


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None, locale=None):
        super().__init__(parent)

        self._peerFrameMode = os.environ.get("PYZO_PEER_FRAMES") == "1"

        self._closeflag = 0  # Used during closing/restarting

        # Init window title and application icon
        # Set title to something nice. On Ubuntu 12.10 this text is what
        # is being shown at the fancy title bar (since it's not properly
        # updated)
        self.setMainTitle()
        loadAppIcons()
        self.setWindowIcon(pyzo.icon)

        # Restore window geometry before drawing for the first time,
        # such that the window is in the right place
        self.resize(800, 600)  # default size
        self.restoreGeometry()

        # Show splash screen (we need to set our color too)
        w = SplashWidget(self, distro="no distro")
        self.setCentralWidget(w)
        self.setStyleSheet("QMainWindow { background-color: #268bd2;}")

        # Show empty window and disable updates for a while
        self.show()
        self.paintNow()
        self.setUpdatesEnabled(False)

        # Determine timeout for showing splash screen
        splash_timeout = time.time() + 1.0

        # Set locale of main widget, so that qt strings are translated
        # in the right way
        if locale:
            self.setLocale(locale)

        # Store myself
        pyzo.main = self

        # Init dockwidget settings
        self.setTabPosition(
            QtCore.Qt.DockWidgetArea.AllDockWidgetAreas,
            QtWidgets.QTabWidget.TabPosition.South,
        )
        self.setDockOptions(
            QtWidgets.QMainWindow.DockOption.AllowTabbedDocks
            | QtWidgets.QMainWindow.DockOption.AllowNestedDocks
            # |  QtWidgets.QMainWindow.DockOption.AnimatedDocks
        )

        # Set window atrributes
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)

        # Load icons and fonts
        loadIcons()
        loadFonts()

        # Set qt style, test success and detect Qt dark mode
        self.setQtStyle(None)  # None means init!

        if pyzo.config.settings.theme == "":
            # set light or dark mode when running Pyzo the first time
            pyzo.config.settings.theme = "default_dark" if pyzo.darkQt else "default"

        # detect dark syntax style (light text on dark background)
        try:
            theme = pyzo.themes[pyzo.config.settings.theme.lower()]["data"]
            s = theme["editor.text"]  # e.g.: "fore:#657b83, back:#fff"
            colors = dict(
                [tuple(s3.strip() for s3 in s2.split(":")) for s2 in s.split(",")]
            )
            pyzo.darkSyntax = (
                QtGui.QColor(colors["fore"]).lightness()
                > QtGui.QColor(colors["back"]).lightness()
            )
        except KeyError:
            pyzo.darkSyntax = False

        # Hold the splash screen if needed
        while time.time() < splash_timeout:
            QtWidgets.qApp.sendPostedEvents()
            QtWidgets.qApp.processEvents()
            time.sleep(0.05)

        # Populate the window (imports more code)
        self._populate()

        # Revert to normal background, and enable updates
        self.setStyleSheet("")
        self.setUpdatesEnabled(True)

        # Restore window state, force updating, and restore again
        self.restoreState()
        self.paintNow()
        self.restoreState()
        if self._peerFrameMode:
            self._restoreLegacyEditorStateIntoPeerPanes()
        else:
            pyzo.editors.restoreEditorState()

        # Instantiate view manager
        pyzo.viewManager = ViewManager()
        if self._peerFrameMode:
            self._restorePeerRecoverySession()
            if os.environ.get("PYZO_EXPECT_RECOVERY_TEXT"):
                self._testPeerRestoredRecovery()

        # Present user with wizard if he/she is new.
        if False:  # pyzo.config.state.newUser:
            from pyzo.util.pyzowizard import PyzoWizard

            w = PyzoWizard(self)
            w.show()  # Use show() instead of exec() so the user can interact with pyzo

        # Create new shell config if there is None
        if not pyzo.config.shellConfigs2:
            from pyzo.core.kernelbroker import KernelInfo

            pyzo.config.shellConfigs2.append(KernelInfo())

        # Focus on editor
        e = pyzo.editors.getCurrentEditor()
        if e is not None:
            e.setFocus()

        # Handle any actions
        commandline.handle_cmd_args()

        if self._peerFrameMode:
            # The QMainWindow remains an application-service host for this
            # experiment. Every user-visible workspace frame is owned by the
            # peer-frame backend.
            self.hide()

    # To force drawing ourselves
    def paintEvent(self, event):
        super().paintEvent(event)
        self._ispainted = True

    def paintNow(self):
        """Enforce a repaint and keep calling processEvents until
        we are repainted.
        """
        self._ispainted = False
        self.update()
        while not self._ispainted:
            QtWidgets.qApp.sendPostedEvents()
            QtWidgets.qApp.processEvents()
            time.sleep(0.01)

    def _populate(self):
        # Delayed imports
        from pyzo.core.editorTabs import EditorTabs
        from pyzo.core.editorGroups import install_editor_group_manager
        from pyzo.core.shellStack import ShellStackWidget
        from pyzo.core.shellGroups import install_shell_group_manager
        from pyzo.core import codeparser
        from pyzo.core.history import CommandHistory
        from pyzo.tools import ToolManager

        try:
            import PySide6QtAds as QtAds
        except ImportError as err:
            raise RuntimeError(
                "The docking integration requires the PySide6QtAds package"
            ) from err

        # Instantiate tool manager
        pyzo.toolManager = ToolManager()

        # Instantiate and start source-code parser
        if pyzo.parser is None:
            pyzo.parser = codeparser.Parser()
            pyzo.parser.start()

        # Keep QtADS drag feedback at its library defaults, matching the
        # standalone spike. The only integration-specific setting here is
        # equal split sizing for newly inserted editor areas.
        config_flag = QtAds.CDockManager.eConfigFlag
        QtAds.CDockManager.setConfigFlag(config_flag.EqualSplitOnInsertion, True)

        if self._peerFrameMode:
            from pyzo.core.applicationController import PeerApplicationController
            from spikes.peer_frames.commands import (
                CommandDescriptor,
                CommandRegistry,
                VIEW_STATUS_COMMAND,
            )
            from spikes.peer_frames.contract import (
                CloseDecision,
                CloseVote,
                Direction,
                OperationStatus,
                PaneDescriptor,
                PaneKind,
                WorkspaceModel,
            )
            from spikes.peer_frames.qtads_backend import QtAdsPeerFrameBackend
            from spikes.peer_frames.qtads_commands import QtAdsCommandProjector
            from spikes.peer_frames.qtads_status import QtAdsStatusProjector
            from spikes.peer_frames.lifecycle import (
                DocumentViewRegistry,
                ShellLifecycleRegistry,
                ShellState,
            )
            from spikes.peer_frames.application_lifecycle import (
                ApplicationExitDisposition,
                ApplicationFrameLifecycle,
                ApplicationShutdownCoordinator,
                CleanupStep,
                WorkspaceShutdownResult,
            )
            from spikes.peer_frames.session_recovery import (
                RecoveryRetentionCoordinator,
                RecoverySnapshot,
                RecoveryStore,
                RecoveryWriteError,
                file_signature,
                named_document_id,
                new_untitled_document_id,
            )
            from spikes.peer_frames.status import StatusState

            self._PeerDirection = Direction
            self._PeerCloseDecision = CloseDecision
            self._PeerCloseVote = CloseVote
            self._PeerOperationStatus = OperationStatus
            self._PeerPaneDescriptor = PaneDescriptor
            self._PeerPaneKind = PaneKind
            self._PeerShellState = ShellState
            self._peerBackend = QtAdsPeerFrameBackend()
            self._peerWorkspace = WorkspaceModel(self._peerBackend)
            self._peerBackend.bind_model(
                self._peerWorkspace,
                self._requestPeerFrameClose,
                self._requestPeerPaneClose,
            )
            self._peerDocumentViews = DocumentViewRegistry()
            self._peerShellLifecycles = ShellLifecycleRegistry()
            self._peerEditorLifecycleMigration = False
            self._peerDirtyCloseChooser = self._choosePeerDirtyClose
            self._peerShellCloseChooser = self._choosePeerShellClose
            self._ApplicationFrameLifecycle = ApplicationFrameLifecycle
            self._ApplicationExitDisposition = ApplicationExitDisposition
            self._ApplicationShutdownCoordinator = ApplicationShutdownCoordinator
            self._PeerCleanupStep = CleanupStep
            self._WorkspaceShutdownResult = WorkspaceShutdownResult
            self._RecoverySnapshot = RecoverySnapshot
            self._RecoveryRetentionCoordinator = RecoveryRetentionCoordinator
            self._RecoveryStore = RecoveryStore
            self._RecoveryWriteError = RecoveryWriteError
            self._peerFileSignature = file_signature
            self._peerNamedDocumentId = named_document_id
            self._peerNewUntitledDocumentId = new_untitled_document_id
            self._peerRecoveryStore = RecoveryStore(
                os.environ.get("PYZO_RECOVERY_DIR")
                or os.path.join(pyzo.appDataDir, "session-recovery")
            )
            self._peerRecoveryFailureChooser = self._choosePeerRecoveryFailure
            self._peerRecoveryRetention = RecoveryRetentionCoordinator(
                self._peerRecoveryStore,
                lambda documents, error: self._peerRecoveryFailureChooser(
                    documents, error
                ),
            )
            QtWidgets.qApp.setQuitOnLastWindowClosed(False)
            self._dockManager = self._peerBackend.manager
            self._peerStatusState = StatusState()
            self._peerStatusProjector = QtAdsStatusProjector(
                self._peerStatusState, self._dockManager
            )
            pyzo.commands = CommandRegistry(
                self._peerWorkspace.resolve_active_context
            )
            pyzo.commands.register(
                CommandDescriptor(
                    "application.quit",
                    translate("menu", "Quit Pyzo"),
                    lambda _context: pyzo.main.quit(),
                    shortcuts=("Ctrl+Q",),
                )
            )
            pyzo.commands.register(
                CommandDescriptor(
                    "application.restart",
                    translate("menu", "Restart Pyzo"),
                    lambda _context: pyzo.main.restart(),
                )
            )
            pyzo.commands.register(
                CommandDescriptor(
                    VIEW_STATUS_COMMAND,
                    translate("menu", "Show status bar"),
                    self._togglePeerStatusBar,
                    checked_when=lambda _context: (
                        self._peerStatusState.snapshot.visible
                    ),
                    checkable=True,
                )
            )
            self._peerCommandProjector = QtAdsCommandProjector(
                pyzo.commands,
                self._dockManager,
                (VIEW_STATUS_COMMAND, "application.restart", "application.quit"),
            )
        else:
            # The previous main-window integration remains as a control. Its
            # custom ordering and guide code is deliberately absent from the
            # peer-frame path, where stock QtADS owns native overlay rendering.
            self._adsWindowOrder = _AdsWindowOrderTracker(self)

            class _PyzoDockManager(QtAds.CDockManager):
                def zOrderIndex(manager_self):
                    if self._adsWindowOrder.mainWindowLeads():
                        return 0xFFFFFFFF
                    return super().zOrderIndex()

            self._dockManager = _PyzoDockManager(self)
            self._adsWindowOrder.setDockManager(self._dockManager)
            overlays = (
                self._dockManager.containerOverlay(),
                self._dockManager.dockAreaOverlay(),
            )
            self._adsDropGuideController = _AdsDropGuideController(overlays, self)
            for overlay in overlays:
                overlay.installEventFilter(self._adsDropGuideController)
        trace_path = os.environ.get("PYZO_ADS_TRACE")
        if trace_path:
            self._adsTrace = _AdsTraceFilter(trace_path, self)
            if not self._peerFrameMode:
                self._adsWindowOrder.setTracePath(trace_path)
            self._traceAdsWidget(self._dockManager)
            self._traceAdsWidget(self._dockManager.containerOverlay())
            self._traceAdsWidget(self._dockManager.dockAreaOverlay())
            self._dockManager.floatingWidgetCreated.connect(self._traceAdsWidget)
            with open(trace_path, "a", encoding="utf-8") as trace:
                trace.write("--- ADS trace installed ---\n")
        self._dockManager.focusedDockWidgetChanged.connect(
            self._onFocusedDockWidgetChanged
        )
        self._QtAds = QtAds
        self._EditorTabs = EditorTabs
        self._ShellStackWidget = ShellStackWidget
        self._editorHosts = {}
        self._shellHosts = {}
        self._editorHostSerial = 0
        self._shellHostSerial = 0
        self._menusReady = False
        if self._peerFrameMode:
            self._peerPaneFocusFilter = _PeerPaneFocusFilter(self)
            QtWidgets.QApplication.instance().installEventFilter(
                self._peerPaneFocusFilter
            )

        # There is one legacy host per document/interpreter. Its internal
        # selector is hidden; QtADS is the sole visible grouping layer.
        pyzo.editors = install_editor_group_manager(
            parent=self,
            group_factory=self._createEditorHost,
            group_remover=self._removeEditorHost,
            close_requester=(
                self._requestPeerEditorClose if self._peerFrameMode else None
            ),
            retain_last_empty_group=not self._peerFrameMode,
        )
        self._primaryEditors = pyzo.editors.createGroup()
        self._editorDock = self._editorHosts[self._primaryEditors]
        install_shell_group_manager(self._createShellHost, self)

        # Initialize command history
        pyzo.command_history = CommandHistory("command_history.py")

        # Create the default shell when returning to the event queue
        callLater(pyzo.shells.addShell)

        # Create the legacy status widget or the application-owned peer service.
        # Kernel/execution affinity is intentionally not part of this globally
        # projected state; focus must not silently select a run target.
        if self._peerFrameMode:
            self._peerStatusService = PeerStatusService(
                self._peerStatusState, self
            )
            self._peerApplicationController = PeerApplicationController(
                parent=QtWidgets.qApp,
                legacy_host=self,
                workspace=self._peerWorkspace,
                frame_window=lambda frame_id: self._peerBackend.frame_windows.get(
                    frame_id
                ),
                status_service=lambda: self._peerStatusService,
                request_quit=self.quit,
                request_restart=self.restart,
                set_style=self.setQtStyle,
                get_execution_shell=lambda: pyzo.shells.getExecutionShell(),
                set_execution_shell=lambda shell: pyzo.shells.setExecutionShell(
                    shell
                ),
            )
            pyzo.application = self._peerApplicationController
        else:
            pyzo.application = None
            self.setStatusBar(StatusBar())
        self.statusBar().setVisible(pyzo.config.view.showStatusbar)

        # Create menu
        from pyzo.core import menu

        pyzo.keyMapper = menu.KeyMapper()
        menu.buildMenus(self.menuBar())
        self._menusReady = True

        # Add context menus now that their actions have been constructed.
        for editor_host in pyzo.editors.groups:
            editor_host.addContextMenu()
        pyzo.shells.addContextMenu()
        if "--test" in sys.argv:
            if not self._peerFrameMode:
                self._testAdsWindowOrderModel()
            first_item = pyzo.editors.newFile()
            second_item = pyzo.editors.newFile()
            assert first_item.editor is not second_item.editor
            assert len(pyzo.editors.groups) == 2
            assert all(group._tabs.count() == 1 for group in pyzo.editors.groups)
            assert all(group._tabs.tabBar().isHidden() for group in pyzo.editors.groups)
            print("Pyzo independent editor-dock smoke test passed")
            if self._peerFrameMode:
                assert isinstance(pyzo.application, QtCore.QObject)
                assert not isinstance(pyzo.application, QtWidgets.QWidget)
                assert pyzo.application.legacy_host is self
                assert pyzo.application.workspace.model is self._peerWorkspace
                assert pyzo.application.workspace.snapshot() == (
                    self._peerWorkspace.serialize_layout()
                )
                assert pyzo.application.active_peer.context() == (
                    self._peerWorkspace.resolve_active_context()
                )
                assert pyzo.application.active_peer.window() is not None
                assert pyzo.application.status.service is self._peerStatusService
                assert pyzo.application.execution_target.shell() is (
                    pyzo.shells.getExecutionShell()
                )
                assert not hasattr(pyzo.application, "menuBar")
                assert not hasattr(pyzo.application, "statusBar")
                print("Pyzo peer application-controller smoke test passed")
            # Exercise the debugger-facing facade: source loading gets its own
            # document dock and indicators are broadcast to every document.
            debug_item = pyzo.editors.loadFile(__file__, ignoreFail=True)
            assert debug_item is not None
            assert len(pyzo.editors.groups) == 3
            assert all(group._tabs.count() == 1 for group in pyzo.editors.groups)
            pyzo.editors.setDebugLineIndicators((__file__, 1))
            assert debug_item.editor._debugLineIndicator == 1
            if self._peerFrameMode:
                self._activatePeerPaneForWidget(first_item.editor)
                first_group = pyzo.editors._group_for_editor(first_item.editor)
                assert pyzo.editors.activeGroup is first_group
                assert pyzo.editors.getCurrentEditor() is first_item.editor
                assert self._peerWorkspace.resolve_active_context().pane.kind is (
                    self._PeerPaneKind.EDITOR
                )
            pyzo.editors.closeFile(second_item.editor)
            assert len(pyzo.editors.groups) == 2
            assert second_item.editor not in tuple(pyzo.editors)
            print("Pyzo debugger document-dock routing smoke test passed")
            pyzo.toolManager.loadTool("pyzofilebrowser")
            file_browser_dock = pyzo.toolManager._activeTools.get("pyzofilebrowser")
            assert file_browser_dock is not None
            assert isinstance(file_browser_dock, QtAds.CDockWidget)
            if self._peerFrameMode:
                active_editor = pyzo.editors.activeGroup
                self._peerBackend._focused_dock_widget_changed(file_browser_dock)
                self._onFocusedDockWidgetChanged(file_browser_dock)
                context = self._peerWorkspace.resolve_active_context()
                assert context.pane.kind is self._PeerPaneKind.TOOL
                assert pyzo.editors.activeGroup is active_editor
            pyzo.toolManager.closeTool("pyzofilebrowser")
            print("Pyzo ADS tool-container smoke test passed")
            if self._peerFrameMode:
                self._testPeerFrameNativeOverlays()
            else:
                container_overlay = self._dockManager.containerOverlay()
                dock_area_overlay = self._dockManager.dockAreaOverlay()
                editor_area = self._editorDock.dockAreaWidget()
                container_overlay.setAllowedAreas(
                    QtAds.TopDockWidgetArea
                    | QtAds.RightDockWidgetArea
                    | QtAds.BottomDockWidgetArea
                    | QtAds.LeftDockWidgetArea
                )
                dock_area_overlay.setAllowedAreas(
                    QtAds.TopDockWidgetArea
                    | QtAds.RightDockWidgetArea
                    | QtAds.BottomDockWidgetArea
                    | QtAds.LeftDockWidgetArea
                    | QtAds.CenterDockWidgetArea
                )
                container_overlay.showOverlay(
                    self._dockManager,
                    self._dockManager.mapToGlobal(self._dockManager.rect().center()),
                )
                dock_area_overlay.showOverlay(
                    editor_area,
                    editor_area.mapToGlobal(editor_area.rect().center()),
                )
                expected_guide_geometry = container_overlay.geometry().united(
                    dock_area_overlay.geometry()
                )
                assert (
                    self._adsDropGuideController._guide.geometry()
                    == expected_guide_geometry
                )
                QtWidgets.QApplication.processEvents()
                guide_image = self._adsDropGuideController._guide.grab().toImage()
                dock_area_center = (
                    dock_area_overlay.geometry().center()
                    - self._adsDropGuideController._guide.geometry().topLeft()
                )
                assert guide_image.pixelColor(dock_area_center).alpha() > 0
                assert container_overlay in (
                    self._adsDropGuideController._guide._visibleOverlays
                )
                has_outer_accent = any(
                    color.alpha() > 0
                    and color.red() > 220
                    and 120 < color.green() < 220
                    and color.blue() < 120
                    for y in range(guide_image.height())
                    for x in range(guide_image.width())
                    for color in (guide_image.pixelColor(x, y),)
                )
                assert has_outer_accent
                container_overlay.hideOverlay()
                dock_area_overlay.hideOverlay()
                self._adsDropGuideController._guide.hideGuide()
                print("Pyzo ADS dual-layer drop-guide paint smoke test passed")
            callLater(self._testIndependentShellDocks)
        # Load tools
        if pyzo.config.state.newUser and not pyzo.config.state.loadedTools:
            pyzo.toolManager.loadTool("pyzosourcestructure")
            pyzo.toolManager.loadTool("pyzofilebrowser", "pyzosourcestructure")
        elif pyzo.config.state.loadedTools:
            self.restoreTools()
        if self._peerFrameMode:
            self._installPeerApplicationLifecycle()

    def _onFocusedDockWidgetChanged(self, dock_widget):
        # Focus selects command/service context. It does not bind an editor to
        # an interpreter; execution affinity needs its own explicit resolver.
        for host, dock in self._editorHosts.items():
            if dock_widget is dock:
                pyzo.editors.setActiveGroup(host)
                return
        for host, dock in self._shellHosts.items():
            if dock_widget is dock:
                pyzo.shells.setActiveHost(host)
                return

    def _activatePeerPaneForWidget(self, widget):
        if not self._peerFrameMode or not isinstance(widget, QtWidgets.QWidget):
            return
        for pane_id, dock in self._peerBackend.docks.items():
            content = dock.widget()
            if content is None:
                continue
            if widget is not content and not content.isAncestorOf(widget):
                continue
            self._peerBackend._focused_dock_widget_changed(dock)
            self._onFocusedDockWidgetChanged(dock)
            if pane_id.startswith("editor-") and hasattr(
                self, "_peerStatusService"
            ):
                self.statusBar().updateCursorInfo(pyzo.editors.getCurrentEditor())
            return

    def _togglePeerStatusBar(self, _context):
        visible = not self.statusBar().isVisible()
        pyzo.config.view.showStatusbar = visible
        self.statusBar().setVisible(visible)

    def statusBar(self):
        if self._peerFrameMode and hasattr(self, "_peerStatusService"):
            return self._peerStatusService
        return super().statusBar()

    def _testAdsWindowOrderModel(self):
        """Assert every ordering state introduced by the macOS fix."""

        class Window:
            def __init__(self, visible=True):
                self._visible = visible

            def isVisible(self):
                return self._visible

        foreground = _AdsWindowOrderTracker._foregroundEligibleWindow
        main = Window()
        floating_back = Window()
        floating_front = Window()
        hidden = Window(False)

        assert foreground([], set()) is None
        assert foreground([main], set()) is main
        assert foreground([floating_front], set()) is floating_front
        assert foreground([floating_back, main], set()) is main
        assert foreground([main, floating_front], set()) is floating_front
        assert (
            foreground([floating_back, main, floating_front], set())
            is floating_front
        )
        assert (
            foreground([floating_back, main, floating_front], {floating_front})
            is main
        )
        assert foreground([main, hidden], set()) is main

        tracker = self._adsWindowOrder
        saved_order = list(tracker._windowOrder)
        saved_snapshot = tracker._dragSnapshot
        saved_active = tracker._dragActive
        saved_tracked_ids = set(tracker._trackedWindowIds)
        try:
            tracker._dragActive = True
            tracker._handleWindowActivation(floating_front)
            assert tracker._windowOrder == saved_order
            tracker._setDragActive(False)
            assert tracker._dragSnapshot is None
            tracker._handleWindowActivation(floating_front)
            assert tracker._liveWindowOrder()[-1] is floating_front
            tracker._setDragActive(True)
            assert tracker._dragSnapshot[-1] is floating_front
            # A pane drag leaves its containing window eligible, while a
            # whole-floating-window drag excludes only that moving window.
            assert foreground(tracker._dragSnapshot, set()) is floating_front
            assert (
                foreground(tracker._dragSnapshot, {floating_front})
                is tracker._mainWindow
            )
            tracker._setDragActive(False)

            retained_window = QtWidgets.QWidget()
            retained_window_id = id(retained_window)
            retained_window_ref = weakref.ref(retained_window)
            tracker._recordWindow(retained_window)
            del retained_window
            gc.collect()
            assert retained_window_ref() is not None
            assert any(
                id(window) == retained_window_id
                for window in tracker._liveWindowOrder()
            )
            retained_window_ref().deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(
                None, QtCore.QEvent.Type.DeferredDelete
            )
            assert all(
                id(window) != retained_window_id
                for window in tracker._liveWindowOrder()
            )

            tracker._handleWindowActivation(floating_front)
            live_at_drag_start = tuple(tracker._liveWindowOrder())
            tracker._setDragActive(True)
            assert tracker._dragSnapshot == live_at_drag_start
            tracker._setDragActive(False)
            assert tracker._dragSnapshot is None
        finally:
            tracker._windowOrder = saved_order
            tracker._dragSnapshot = saved_snapshot
            tracker._dragActive = saved_active
            tracker._trackedWindowIds = saved_tracked_ids
        print("Pyzo ADS foreground-window ordering smoke test passed")

    def _createEditorHost(self):
        self._editorHostSerial += 1
        host = self._EditorTabs(self)
        host.setObjectName(f"pyzo-editor-host-{self._editorHostSerial}")
        dock = self._QtAds.CDockWidget(self._dockManager, "Untitled")
        dock.setObjectName(f"pyzo-editor-dock-{self._editorHostSerial}")
        dock.setWidget(host)
        if self._peerFrameMode:
            self._registerPeerDock(
                f"editor-{self._editorHostSerial}",
                self._PeerPaneKind.EDITOR,
                "Untitled",
                dock,
            )
        else:
            active = getattr(pyzo, "editors", None)
            active_group = getattr(active, "activeGroup", None)
            active_dock = self._editorHosts.get(active_group)
            if active_dock is None:
                self._dockManager.addDockWidget(self._QtAds.CenterDockWidgetArea, dock)
            else:
                self._dockManager.addDockWidget(
                    self._QtAds.CenterDockWidgetArea,
                    dock,
                    active_dock.dockAreaWidget(),
                )
        self._editorHosts[host] = dock
        host._tabs.fileTabsChanged.connect(
            lambda host=host: self._editorHostContentChanged(host)
        )
        if self._menusReady:
            host.addContextMenu()
        return host

    def _editorHostContentChanged(self, host):
        self._updateEditorDockTitle(host)
        if self._peerFrameMode:
            self._bindPeerEditorLifecycle(host)

    def _restoreLegacyEditorStateIntoPeerPanes(self):
        """Convert legacy nested editor state at one explicit migration boundary."""

        assert not self._peerEditorLifecycleMigration
        self._peerEditorLifecycleMigration = True
        try:
            pyzo.editors.restoreEditorState()
        finally:
            self._peerEditorLifecycleMigration = False
        for host in pyzo.editors.groups:
            if host._tabs.count() > 1:
                raise RuntimeError(
                    "legacy editor-state conversion left a multi-document peer pane"
                )
            self._bindPeerEditorLifecycle(host)

    def _bindPeerEditorLifecycle(self, host):
        if self._peerEditorLifecycleMigration:
            return
        dock = self._editorHosts.get(host)
        item = host._tabs.currentItem()
        if dock is None or item is None:
            return
        pane_id = self._peerBackend._pane_id_from_dock(dock)
        if pane_id not in self._peerWorkspace.panes:
            return
        document_id = f"pyzo-document-{item.lifecycleId}"
        bound_document_id = self._peerDocumentViews.document_id_for_view(pane_id)
        if bound_document_id == document_id:
            return
        if bound_document_id is not None:
            raise RuntimeError(
                f"editor pane {pane_id} is already bound to {bound_document_id}"
            )
        lifecycle = _PyzoEditorDocumentLifecycle(self, pane_id, item)
        policy = self._peerDocumentViews.register_view(
            pane_id,
            document_id,
            lifecycle,
            is_pinned=lambda item=item: item.pinned,
        )
        result = self._peerWorkspace.set_close_policy(pane_id, policy)
        if not result.applied:
            self._peerDocumentViews.forget_view(pane_id)
            raise RuntimeError(result.detail)

    def _requestPeerEditorClose(self, editor):
        host = pyzo.editors._group_for_editor(editor)
        dock = self._editorHosts.get(host)
        if dock is None:
            return 0
        pane_id = self._peerBackend._pane_id_from_dock(dock)
        result = self._peerWorkspace.request_close_pane(
            pane_id, self._resolvePeerEditorClose
        )
        return 1 if result.applied else 0

    def _choosePeerDirtyClose(self, _document_id, lifecycle):
        return lifecycle.choose_dirty_close()

    def _resolvePeerEditorClose(self, votes):
        return self._peerDocumentViews.resolve_confirmations(
            votes, self._peerDirtyCloseChooser
        )

    def _closeCommittedPeerEditor(self, pane_id, editor):
        assert pane_id not in self._peerWorkspace.panes
        result = pyzo.editors.closeFileCommitted(editor)
        if not result:
            raise RuntimeError("committed clean editor close did not complete")

    def _updateEditorDockTitle(self, host):
        dock = self._editorHosts.get(host)
        if dock is None:
            return
        item = host._tabs.currentItem()
        title = item.name if item is not None else "Untitled"
        if item is not None and item.dirty:
            title += " *"
        if item is not None and getattr(
            item.editor, "_peerRecoveryConflict", False
        ):
            title += " ⚠"
        dock.setWindowTitle(title)

    def _removeEditorHost(self, host):
        dock = self._editorHosts.pop(host, None)
        if dock is not None:
            if self._peerFrameMode:
                pane_id = self._peerBackend._pane_id_from_dock(dock)
                if pane_id in self._peerWorkspace.panes:
                    self._removePeerDock(dock)
            else:
                self._dockManager.removeDockWidget(dock)
                dock.deleteLater()

    def _createShellHost(self):
        self._shellHostSerial += 1
        host = self._ShellStackWidget(self)
        host.setObjectName(f"pyzo-shell-host-{self._shellHostSerial}")
        dock = self._QtAds.CDockWidget(self._dockManager, "Shell")
        dock.setObjectName(f"pyzo-shell-dock-{self._shellHostSerial}")
        features = dock.DockWidgetFeature
        if self._peerFrameMode or pyzo.config.settings.allowFloatingShell:
            dock.setFeatures(features.DockWidgetMovable | features.DockWidgetFloatable)
        else:
            dock.setFeatures(features.DockWidgetMovable)
        dock.setWidget(host)
        if self._peerFrameMode:
            self._registerPeerDock(
                f"shell-{self._shellHostSerial}",
                self._PeerPaneKind.SHELL,
                "Shell",
                dock,
            )
        else:
            self._dockManager.addDockWidget(self._QtAds.RightDockWidgetArea, dock)
        self._shellHosts[host] = dock
        host.currentShellStateChanged.connect(
            lambda host=host: self._updateShellDockTitle(host)
        )
        if self._peerFrameMode:
            host.currentShellChanged.connect(
                lambda host=host: self._bindPeerShellLifecycle(host)
            )
        return host

    def _bindPeerShellLifecycle(self, host):
        dock = self._shellHosts.get(host)
        shell = host.getCurrentShell()
        if dock is None or shell is None:
            return
        pane_id = self._peerBackend._pane_id_from_dock(dock)
        if pane_id not in self._peerWorkspace.panes:
            return
        if pane_id in self._peerShellLifecycles.pane_ids:
            return
        lifecycle = _PyzoShellLifecycle(self, pane_id, shell)
        policy = self._peerShellLifecycles.register_shell(pane_id, lifecycle)
        result = self._peerWorkspace.set_close_policy(pane_id, policy)
        if not result.applied:
            self._peerShellLifecycles.forget_shell(pane_id)
            raise RuntimeError(result.detail)

    def _requestPeerShellClose(self, shell):
        host = next(
            (host for host in pyzo.shells.hosts if shell in host.getShells()),
            None,
        )
        dock = self._shellHosts.get(host)
        if dock is None:
            return 0
        pane_id = self._peerBackend._pane_id_from_dock(dock)
        result = self._peerWorkspace.request_close_pane(
            pane_id, self._resolvePeerShellClose
        )
        return 1 if result.applied else 0

    def _choosePeerShellClose(self, _pane_id, lifecycle):
        return lifecycle.choose_live_close()

    def _askPeerShellClose(self, lifecycle):
        state = lifecycle.shell_state()
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(translate("shell", "Close shell"))
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        dialog.setText(
            translate("shell", "Terminate this interpreter and close its shell?")
        )
        if state is self._PeerShellState.DEBUGGING:
            detail = translate(
                "shell", "The active debugging session and its stack state will be lost."
            )
        elif state is self._PeerShellState.RUNNING:
            detail = translate("shell", "The interpreter is currently executing code.")
        elif state is self._PeerShellState.IDLE:
            detail = translate("shell", "The interpreter is still running.")
        else:
            detail = translate(
                "shell",
                "The interpreter is still starting or its state is unknown.",
            )
        dialog.setInformativeText(detail)
        terminate = dialog.addButton(
            translate("shell", "Terminate and close"),
            QtWidgets.QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel = dialog.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        return "terminate" if dialog.clickedButton() is terminate else "cancel"

    def _resolvePeerShellClose(self, votes):
        return self._peerShellLifecycles.resolve_confirmations(
            votes, self._peerShellCloseChooser
        )

    def _resolvePeerClose(self, votes):
        document_votes = tuple(
            vote
            for vote in votes
            if self._peerDocumentViews.document_id_for_view(vote.pane_id)
            is not None
        )
        shell_votes = tuple(
            vote
            for vote in votes
            if vote.pane_id in self._peerShellLifecycles.pane_ids
        )
        if len(document_votes) + len(shell_votes) != len(votes):
            return False
        if shell_votes and not self._peerShellLifecycles.resolve_confirmations(
            shell_votes, self._peerShellCloseChooser
        ):
            return False
        if document_votes and not self._peerDocumentViews.resolve_confirmations(
            document_votes, self._peerDirtyCloseChooser
        ):
            self._peerShellLifecycles.cancel_pending(
                tuple(vote.pane_id for vote in shell_votes)
            )
            return False
        return True

    def _resolvePeerApplicationClose(self, votes):
        document_votes = tuple(
            vote
            for vote in votes
            if self._peerDocumentViews.document_id_for_view(vote.pane_id)
            is not None
        )
        shell_votes = tuple(
            vote
            for vote in votes
            if vote.pane_id in self._peerShellLifecycles.pane_ids
        )
        if len(document_votes) + len(shell_votes) != len(votes):
            return False
        if shell_votes and not self._peerShellLifecycles.resolve_confirmations(
            shell_votes, self._peerShellCloseChooser
        ):
            return False
        if document_votes and not self._peerDocumentViews.resolve_retention(
            document_votes, self._retainPeerDocuments
        ):
            self._peerShellLifecycles.cancel_pending(
                tuple(vote.pane_id for vote in shell_votes)
            )
            return False
        return True

    def _requestPeerFrameClose(self, frame_id):
        result = self._peerWorkspace.request_close_frame(
            frame_id, self._resolvePeerClose
        )
        self._afterPeerTopologyClose(result)
        return result

    def _requestPeerPaneClose(self, pane_id):
        result = self._peerWorkspace.request_close_pane(
            pane_id, self._resolvePeerClose
        )
        self._afterPeerTopologyClose(result)
        return result

    def _afterPeerTopologyClose(self, result):
        lifecycle = getattr(self, "_peerApplicationLifecycle", None)
        if (
            result.applied
            and not self._peerWorkspace.frames
            and lifecycle is not None
        ):
            lifecycle.zero_frames_reached()

    def _installPeerApplicationLifecycle(self):
        self._peerDefaultFrameErrors = []
        self._peerApplicationExitErrors = []
        self._peerShutdownCoordinator = self._ApplicationShutdownCoordinator(
            confirm_exit=self._confirmPeerApplicationExit,
            request_workspace_shutdown=self._requestPeerWorkspaceShutdown,
            cleanup_steps=(
                self._PeerCleanupStep("config", pyzo.saveConfig),
                self._PeerCleanupStep("history", pyzo.command_history.save),
                self._PeerCleanupStep("server", commandline.stop_our_server),
                self._PeerCleanupStep(
                    "kernels", pyzo.localKernelManager.terminateAll
                ),
                self._PeerCleanupStep("tools", self._cleanupRemainingPeerTools),
                self._PeerCleanupStep("threads", self._stopPeerApplicationThreads),
                self._PeerCleanupStep(
                    "projectors", self._peerStatusProjector.close
                ),
                self._PeerCleanupStep("backend", self._peerBackend.close),
            ),
            finish_quit=QtWidgets.qApp.exit,
            replace_process=self._replacePyzoProcess,
            log_error=self._logPeerApplicationExitError,
        )
        self._peerApplicationLifecycle = self._ApplicationFrameLifecycle(
            has_independent_handle=lambda: sys.platform == "darwin",
            frame_count=lambda: len(self._peerWorkspace.frames),
            request_quit=lambda: callLater(self.quit),
            create_configured_frame=self._createConfiguredPeerDefaultFrame,
            create_factory_frame=self._createFactoryPeerDefaultFrame,
            log_error=self._logPeerDefaultFrameError,
            fatal_exit=self._fatalPeerDefaultFrameExit,
        )
        if sys.platform == "darwin":
            QtWidgets.qApp.applicationStateChanged.connect(
                self._onPeerApplicationStateChanged
            )

    def _confirmPeerApplicationExit(self, _disposition):
        if not pyzo.config.advanced.alwaysAskWhenClosingPyzo:
            return True
        dialog = QtWidgets.QMessageBox(None)
        dialog.setWindowTitle("Pyzo")
        dialog.setText(translate("main", "Do you really want to close Pyzo?"))
        close_button = dialog.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        cancel_button = dialog.addButton(
            QtWidgets.QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(cancel_button)
        dialog.exec()
        return dialog.clickedButton() is close_button

    def _requestPeerWorkspaceShutdown(self):
        result = self._peerWorkspace.request_shutdown(
            self._resolvePeerApplicationClose
        )
        return self._WorkspaceShutdownResult(
            result.applied, result.cleanup_errors
        )

    def _recoveryIdForItem(self, item):
        editor = item.editor
        recovery_id = getattr(editor, "_peerRecoveryId", None)
        if recovery_id is None:
            recovery_id = (
                self._peerNamedDocumentId(item.filename)
                if item.filename
                else self._peerNewUntitledDocumentId()
            )
            editor._peerRecoveryId = recovery_id
        return recovery_id

    def _snapshotPeerDocument(self, item):
        from pyzo.core.editor import getEncodingFromMagic

        editor = item.editor
        text = editor.toPlainText()
        return self._RecoverySnapshot(
            document_id=self._recoveryIdForItem(item),
            filename=item.filename or None,
            display_name=item.name,
            text=text,
            encoding=getEncodingFromMagic(text[:200]),
            use_bom=bool(editor.useBom),
            line_endings=editor.lineEndingsHumanReadable,
            disk_signature=getattr(
                editor,
                "_peerRecoveryDiskSignature",
                self._peerFileSignature(item.filename or None),
            ),
        )

    def _restorePeerRecoverySession(self):
        loaded = self._peerRecoveryStore.load()
        conflicts = []
        for snapshot in loaded.snapshots:
            item = None
            if snapshot.filename and os.path.isfile(snapshot.filename):
                item = pyzo.editors.loadFile(snapshot.filename, ignoreFail=True)
            if item is None:
                item = pyzo.editors.newFile()
                if snapshot.filename:
                    item.editor._filename = snapshot.filename
                    item.editor._name = os.path.basename(snapshot.filename)
                else:
                    item.editor._name = snapshot.display_name

            editor = item.editor
            current_signature = self._peerFileSignature(snapshot.filename)
            conflict = bool(
                snapshot.filename
                and (
                    current_signature == "missing"
                    or current_signature != snapshot.disk_signature
                )
            )
            editor.setPlainText(snapshot.text)
            editor.lineEndings = snapshot.line_endings
            editor.useBom = snapshot.use_bom
            editor._peerRecoveryId = snapshot.document_id
            editor._peerRecoveryDiskSignature = snapshot.disk_signature
            editor._peerRecoveryEncoding = snapshot.encoding
            editor._peerRecoveryConflict = conflict
            if snapshot.filename and os.path.isfile(snapshot.filename):
                editor._modifyTime = os.path.getmtime(snapshot.filename)
            editor.document().setModified(True)
            host = pyzo.editors._group_for_editor(editor)
            self._updateEditorDockTitle(host)
            if conflict:
                conflicts.append(snapshot.display_name)

        messages = []
        if conflicts:
            messages.append(
                translate(
                    "main",
                    "Recovered dirty buffers conflict with files on disk: {names}",
                ).format(names=", ".join(conflicts))
            )
        if loaded.issues:
            messages.append(
                translate(
                    "main",
                    "Some recovery records were quarantined: {count}",
                ).format(count=len(loaded.issues))
            )
            for issue in loaded.issues:
                print(
                    f"recovery record quarantined ({issue.source}): {issue.detail}",
                    file=sys.stderr,
                )
        if messages:
            self.statusBar().showMessage(" ".join(messages))

    def _testPeerRestoredRecovery(self):
        expected = os.environ["PYZO_EXPECT_RECOVERY_TEXT"]
        matches = [
            editor
            for editor in pyzo.editors
            if editor.toPlainText() == expected
        ]
        assert len(matches) == 1
        editor = matches[0]
        assert editor.document().isModified()
        assert getattr(editor, "_peerRecoveryId", None)
        assert getattr(editor, "_peerRecoveryEncoding", None)
        assert editor.lineEndingsHumanReadable == "CRLF"
        assert editor.useBom
        expected_conflict = os.environ.get("PYZO_EXPECT_RECOVERY_CONFLICT") == "1"
        assert bool(getattr(editor, "_peerRecoveryConflict", False)) is expected_conflict
        host = pyzo.editors._group_for_editor(editor)
        title = self._editorHosts[host].windowTitle()
        assert ("⚠" in title) is expected_conflict
        self._retirePeerRecoveryForEditor(editor, destructive_fallback=True)
        editor.document().setModified(False)
        assert pyzo.editors.closeFile(editor) == 1
        print("Pyzo retained dirty-buffer restoration smoke test passed")

    def _retainPeerDocuments(self, documents):
        return self._peerRecoveryRetention.retain(
            tuple(lifecycle for _document_id, lifecycle in documents)
        )

    def _choosePeerRecoveryFailure(self, lifecycles, error):
        names = [lifecycle.recovery_snapshot().display_name for lifecycle in lifecycles]
        dialog = QtWidgets.QMessageBox(None)
        dialog.setWindowTitle(translate("main", "Could not retain dirty buffers"))
        dialog.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        dialog.setText(
            translate(
                "main",
                "Pyzo could not write recovery snapshots for:\n{names}",
            ).format(names="\n".join(names))
        )
        dialog.setInformativeText(str(error))
        retry = dialog.addButton(
            translate("main", "Retry"), QtWidgets.QMessageBox.ButtonRole.AcceptRole
        )
        save = dialog.addButton(
            translate("main", "Save affected"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        discard = dialog.addButton(
            translate("main", "Discard affected and exit"),
            QtWidgets.QMessageBox.ButtonRole.DestructiveRole,
        )
        dialog.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(retry)
        dialog.exec()
        if dialog.clickedButton() is retry:
            return "retry"
        if dialog.clickedButton() is save:
            return "save"
        if dialog.clickedButton() is discard:
            return "discard"
        return "cancel"

    def _retirePeerRecoveryForEditor(
        self, editor, *, destructive_fallback=False
    ):
        recovery_id = getattr(editor, "_peerRecoveryId", None)
        if recovery_id is None:
            return
        try:
            self._peerRecoveryStore.retire(recovery_id)
        except self._RecoveryWriteError:
            if not destructive_fallback:
                raise
            self._peerRecoveryStore.invalidate(recovery_id)
        editor._peerRecoveryConflict = False
        editor._peerRecoveryDiskSignature = None
        editor._peerRecoveryEncoding = None
        editor._peerRecoveryId = None
        host = pyzo.editors._group_for_editor(editor)
        if host is not None:
            self._updateEditorDockTitle(host)

    def _cleanupRemainingPeerTools(self):
        errors = []
        for toolname in tuple(pyzo.toolManager.getLoadedTools()):
            tool = pyzo.toolManager.getTool(toolname)
            if hasattr(tool, "cleanUp"):
                try:
                    tool.cleanUp()
                except Exception as error:
                    errors.append(f"{toolname}: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _stopPeerApplicationThreads(self):
        import threading

        errors = []
        for thread in threading.enumerate():
            if hasattr(thread, "stop"):
                try:
                    thread.stop(0.1)
                except Exception as error:
                    errors.append(f"{thread.name}: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _logPeerApplicationExitError(self, message):
        self._peerApplicationExitErrors.append(message)
        print(message, file=sys.stderr)

    def _requestPeerApplicationExit(self, disposition):
        return self._peerShutdownCoordinator.request(disposition)

    def _onPeerApplicationStateChanged(self, state):
        if state == QtCore.Qt.ApplicationState.ApplicationActive:
            self._peerApplicationLifecycle.application_activated()

    def _createConfiguredPeerDefaultFrame(self):
        configured_tools = tuple(pyzo.config.state.loadedTools or ())
        if not configured_tools:
            configured_tools = ("pyzosourcestructure", "pyzofilebrowser")
        self._createPeerDefaultFrame(configured_tools)

    def _createFactoryPeerDefaultFrame(self):
        self._createPeerDefaultFrame(
            ("pyzosourcestructure", "pyzofilebrowser")
        )

    def _createPeerDefaultFrame(self, tool_ids):
        pane_ids = []

        try:
            item = pyzo.editors.newFile()
            host = pyzo.editors._group_for_editor(item.editor)
            pane_ids.append(
                self._peerBackend._pane_id_from_dock(self._editorHosts[host])
            )
        except Exception:
            self._logPeerDefaultFrameError(
                "default editor pane failed:\n" + traceback.format_exc()
            )

        try:
            shell = pyzo.shells.getCurrentShell()
            if shell is None:
                shell = pyzo.shells.addShell()
            host = next(
                host for host in pyzo.shells.hosts if shell in host.getShells()
            )
            pane_ids.append(
                self._peerBackend._pane_id_from_dock(self._shellHosts[host])
            )
        except Exception:
            self._logPeerDefaultFrameError(
                "default shell pane failed:\n" + traceback.format_exc()
            )

        for tool_id in tool_ids:
            try:
                pyzo.toolManager.loadTool(tool_id)
                dock = pyzo.toolManager._activeTools.get(tool_id)
                if dock is None:
                    raise RuntimeError(f"tool did not create a pane: {tool_id}")
                pane_ids.append(self._peerBackend._pane_id_from_dock(dock))
            except Exception:
                self._logPeerDefaultFrameError(
                    f"default tool pane failed ({tool_id}):\n"
                    + traceback.format_exc()
                )

        pane_ids = list(dict.fromkeys(pane_ids))
        if not pane_ids:
            return
        target = pane_ids[0]
        if len(pane_ids) > 1:
            result = self._peerWorkspace.split(
                pane_ids[1], target, self._PeerDirection.BELOW
            )
            if not result.applied:
                raise RuntimeError(result.detail)
            lower_target = pane_ids[1]
            for pane_id in pane_ids[2:]:
                result = self._peerWorkspace.tab(pane_id, lower_target)
                if not result.applied:
                    raise RuntimeError(result.detail)

    def _logPeerDefaultFrameError(self, message):
        self._peerDefaultFrameErrors.append(message)
        print(message, file=sys.stderr)

    def _fatalPeerDefaultFrameExit(self, code, message):
        try:
            QtWidgets.QMessageBox.critical(
                None,
                "Pyzo",
                "Pyzo could not create a window.\n\n" + message,
            )
        finally:
            QtWidgets.qApp.exit(code)

    def _closeCommittedPeerShell(self, pane_id, shell, *, terminate):
        assert pane_id not in self._peerWorkspace.panes
        result = shell.closeShellCommitted(terminate_kernel=terminate)
        if not result:
            raise RuntimeError("committed shell close did not complete")

    def _updateShellDockTitle(self, host):
        from pyzo.core.shellStack import shellTitle

        shell = host.getCurrentShell()
        dock = self._shellHosts.get(host)
        if shell is not None and dock is not None:
            dock.setWindowTitle(shellTitle(shell))

    def _testPeerRunAndLiveDebugTick(self):
        """Advance the real-kernel run/debug smoke without blocking Qt."""
        state = getattr(self, "_peerRunDebugSmoke", None)
        if state is None:
            state = self._peerRunDebugSmoke = {"phase": "wait-shell"}

        phase = state["phase"]
        if phase == "wait-shell":
            shell = pyzo.shells.getCurrentShell()
            if shell is None or not shell._version:
                return False
            fd, path = tempfile.mkstemp(prefix="pyzo-peer-debug-", suffix=".py")
            source = (
                "peer_run_value = 6 * 7\n"
                "\n"
                "def peer_live_debug_probe():\n"
                "    peer_frame_value = 41\n"
                "    peer_frame_value += 1\n"
                "    return peer_frame_value\n"
                "\n"
                "peer_live_debug_result = peer_live_debug_probe()\n"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(source)
            item = pyzo.editors.loadFile(path, ignoreFail=True)
            assert item is not None
            editor = item.editor
            self._activatePeerPaneForWidget(editor)
            assert pyzo.editors.getCurrentEditor() is editor
            self.menuBar()._menumap["run"]._runFile((False, False), editor)
            state.update(
                phase="wait-run",
                shell=shell,
                path=path,
                editor=editor,
            )
            return False

        shell = state["shell"]
        editor = state.get("editor")
        if phase == "wait-run":
            value = shell._request.eval("peer_run_value").result_or_cancel(0.2)
            if value != 42:
                return False
            editor.toggleBreakpoint(5, triState=False)
            assert editor.breakPoints() == [5]
            self.menuBar()._menumap["run"]._runFile((False, False), editor)
            state["phase"] = "wait-debug"
            return False

        if phase == "wait-debug":
            debug_state = shell._debugState or {}
            if debug_state.get("debugmode") != 2:
                return False
            frames = debug_state["frames"]
            index = debug_state["index"]
            assert frames[index - 1][0] == state["path"]
            assert frames[index - 1][1] == 5
            assert pyzo.editors.getCurrentEditor() is editor
            assert editor._debugLineIndicator == 5
            host = next(host for host in pyzo.shells.hosts if shell in host.getShells())
            assert host._dbs._lastFrames == frames
            assert host._dbs._lastIndex == index
            value = shell._request.eval("peer_frame_value").result_or_cancel(0.2)
            assert value == 41
            shell.executeCommand("DB CONTINUE\n")
            state["phase"] = "wait-continue"
            return False

        if phase == "wait-continue":
            if (shell._debugState or {}).get("debugmode", 0) != 0:
                return False
            value = shell._request.eval("peer_live_debug_result").result_or_cancel(
                0.2
            )
            if value != 42:
                return False
            editor.toggleBreakpoint(5, triState=False)
            assert editor.breakPoints() == []
            assert pyzo.editors.closeFile(editor) == 1
            os.remove(state["path"])
            print("Pyzo ordinary run and live-debug smoke test passed")

            fd, path = tempfile.mkstemp(
                prefix="pyzo-peer-postmortem-", suffix=".py"
            )
            source = (
                "def peer_postmortem_outer():\n"
                "    peer_outer_value = 21\n"
                "    def peer_postmortem_inner():\n"
                "        peer_captured_value = 84\n"
                "        raise RuntimeError('peer-postmortem')\n"
                "    peer_postmortem_inner()\n"
                "peer_postmortem_outer()\n"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(source)
            item = pyzo.editors.loadFile(path, ignoreFail=True)
            assert item is not None
            editor = item.editor
            self._activatePeerPaneForWidget(editor)
            editor_host = pyzo.editors._group_for_editor(editor)
            editor_dock = self._editorHosts[editor_host]
            editor_id = self._peerBackend._pane_id_from_dock(editor_dock)
            source_frame_id = "pyzo-postmortem-source-frame"
            result = self._peerWorkspace.float(editor_id, source_frame_id)
            assert result.applied, result.detail
            QtWidgets.QApplication.processEvents()
            assert editor_dock in self._peerBackend.frame_windows[
                source_frame_id
            ].dockWidgets()
            self.menuBar()._menumap["run"]._runFile((False, False), editor)
            state.update(
                phase="wait-exception",
                path=path,
                editor=editor,
                source_frame_id=source_frame_id,
            )
            return False

        if phase == "wait-exception":
            if (shell._debugState or {}).get("debugmode", 0) != 0:
                return False
            last_exception = shell._request.eval(
                "repr(__import__('sys').last_value)"
            ).result_or_cancel(0.2)
            if last_exception != "RuntimeError('peer-postmortem')":
                return False
            shell.executeCommand("DB START\n")
            state["phase"] = "wait-postmortem"
            return False

        if phase == "wait-postmortem":
            debug_state = shell._debugState or {}
            if debug_state.get("debugmode") != 1:
                return False
            frames = debug_state["frames"]
            index = debug_state["index"]
            assert frames[index - 1][0] == state["path"]
            assert frames[index - 1][1] == 5
            editor = state["editor"]
            assert pyzo.editors.getCurrentEditor() is editor
            assert editor._debugLineIndicator == 5
            editor_host = pyzo.editors._group_for_editor(editor)
            editor_dock = self._editorHosts[editor_host]
            assert editor_dock in self._peerBackend.frame_windows[
                state["source_frame_id"]
            ].dockWidgets()
            captured = shell._request.eval(
                "peer_captured_value"
            ).result_or_cancel(0.2)
            assert captured == 84
            outer_index = next(
                i
                for i, frame in enumerate(frames, 1)
                if frame[2] == "peer_postmortem_outer"
            )
            shell.executeCommand(f"DB FRAME {outer_index}\n")
            state.update(phase="wait-postmortem-outer", outer_index=outer_index)
            return False

        if phase == "wait-postmortem-outer":
            debug_state = shell._debugState or {}
            if debug_state.get("index") != state["outer_index"]:
                return False
            frame = debug_state["frames"][state["outer_index"] - 1]
            assert frame == (state["path"], 6, "peer_postmortem_outer")
            editor = state["editor"]
            assert pyzo.editors.getCurrentEditor() is editor
            assert editor._debugLineIndicator == 6
            editor_host = pyzo.editors._group_for_editor(editor)
            editor_dock = self._editorHosts[editor_host]
            assert editor_dock in self._peerBackend.frame_windows[
                state["source_frame_id"]
            ].dockWidgets()

            shell_host = next(
                host for host in pyzo.shells.hosts if shell in host.getShells()
            )
            shell_dock = self._shellHosts[shell_host]
            shell_id = self._peerBackend._pane_id_from_dock(shell_dock)
            tool_id = next(
                pane_id
                for pane_id, pane in self._peerWorkspace.panes.items()
                if pane.kind is self._PeerPaneKind.TOOL
            )
            editor_id = self._peerBackend._pane_id_from_dock(editor_dock)
            editor_document = editor.document()
            shell_context = shell._context
            frames = debug_state["frames"]
            editor.toggleBreakpoint(5, triState=False)
            assert editor.breakPoints() == [5]

            def apply_layout(operation):
                result = operation()
                assert result.applied, result.detail
                QtWidgets.QApplication.processEvents()
                assert shell._context is shell_context
                assert shell._debugState["debugmode"] == 1
                assert shell._debugState["index"] == state["outer_index"]
                assert shell._debugState["frames"] == frames
                assert shell_host._dbs._lastFrames == frames
                assert shell_host._dbs._lastIndex == state["outer_index"]
                assert editor.document() is editor_document
                assert editor._debugLineIndicator == 6
                assert editor.breakPoints() == [5]

            apply_layout(
                lambda: self._peerWorkspace.split(
                    tool_id, editor_id, self._PeerDirection.RIGHT
                )
            )
            apply_layout(
                lambda: self._peerWorkspace.split(
                    editor_id, shell_id, self._PeerDirection.BELOW
                )
            )
            apply_layout(lambda: self._peerWorkspace.tab(editor_id, shell_id))
            assert editor_dock.dockAreaWidget() is shell_dock.dockAreaWidget()
            apply_layout(
                lambda: self._requestPeerFrameClose(state["source_frame_id"])
            )
            assert state["source_frame_id"] not in self._peerWorkspace.frames
            editor.toggleBreakpoint(5, triState=False)
            assert editor.breakPoints() == []
            shell.executeCommand(
                "peer_selected_result = peer_outer_value * 2\n"
            )
            state["phase"] = "wait-postmortem-exec"
            return False

        if phase == "wait-postmortem-exec":
            debug_state = shell._debugState or {}
            assert debug_state.get("debugmode") == 1
            assert debug_state.get("index") == state["outer_index"]
            result = shell._request.eval(
                "peer_selected_result"
            ).result_or_cancel(0.2)
            if result != 42:
                return False
            shell.executeCommand("DB STOP\n")
            state["phase"] = "wait-postmortem-stop"
            return False

        if phase == "wait-postmortem-stop":
            if (shell._debugState or {}).get("debugmode", 0) != 0:
                return False
            assert pyzo.editors.closeFile(state["editor"]) == 1
            os.remove(state["path"])
            print("Pyzo post-mortem re-entry smoke test passed")

            second_shell = pyzo.shells.addShell()
            state.update(
                phase="wait-affinity-shell",
                first_shell=shell,
                second_shell=second_shell,
                cwd_directory=tempfile.TemporaryDirectory(
                    prefix="pyzo-peer-kernel-cwd-"
                ),
            )
            return False

        if phase == "wait-affinity-shell":
            first_shell = state["first_shell"]
            second_shell = state["second_shell"]
            state["affinity_observation"] = {
                "first_version": first_shell._version,
                "second_version": second_shell._version,
            }
            if not first_shell._version or not second_shell._version:
                return False
            first_shell.executeCommand("peer_kernel_identity = 'first'\n")
            second_shell.executeCommand(
                "import os; os.chdir({}); peer_kernel_identity = 'second'\n".format(
                    repr(state["cwd_directory"].name)
                )
            )
            state["phase"] = "wait-affinity-seed"
            return False

        if phase == "wait-affinity-seed":
            first_shell = state["first_shell"]
            second_shell = state["second_shell"]
            first_identity = first_shell._request.eval(
                "peer_kernel_identity"
            ).result_or_cancel(0.2)
            second_identity = second_shell._request.eval(
                "peer_kernel_identity"
            ).result_or_cancel(0.2)
            state["affinity_observation"] = {
                "first_identity": first_identity,
                "second_identity": second_identity,
                "second_cwd": second_shell.get_kernel_cd(),
            }
            if first_identity != "first" or second_identity != "second":
                return False
            expected_cwd = os.path.realpath(state["cwd_directory"].name)
            if second_shell.get_kernel_cd() != expected_cwd:
                return False

            first_context = first_shell._context
            second_context = second_shell._context
            assert pyzo.shells.getExecutionShell() is first_shell
            second_host = next(
                host
                for host in pyzo.shells.hosts
                if second_shell in host.getShells()
            )
            second_dock = self._shellHosts[second_host]
            self._peerBackend._focused_dock_widget_changed(second_dock)
            self._onFocusedDockWidgetChanged(second_dock)
            assert pyzo.shells.getCurrentShell() is second_shell
            assert pyzo.shells.getExecutionShell() is first_shell

            pyzo.shells.setExecutionShell(second_shell)
            assert pyzo.shells.getExecutionShell() is second_shell
            shell_menu = self.menuBar()._menumap["shell"]
            run_menu = self.menuBar()._menumap["run"]
            shell_menu._updateExecutionTarget()
            run_menu._updateExecutionTarget()
            assert "Run/Debug target:" in shell_menu._executionTargetAction.text()
            assert second_shell._info.name in shell_menu._executionTargetAction.text()
            assert second_shell._info.name in run_menu._executionTargetAction.text()
            assert shell_menu.getShell() is pyzo.shells.getCurrentShell()
            assert pyzo.shells.getExecutionShell() is second_shell
            debug_commands = []
            first_execute_command = first_shell.executeCommand
            second_execute_command = second_shell.executeCommand
            first_shell.executeCommand = lambda command: debug_commands.append(
                ("first", command)
            )
            second_shell.executeCommand = lambda command: debug_commands.append(
                ("second", command)
            )
            try:
                shell_menu._debugAction("START")
            finally:
                first_shell.executeCommand = first_execute_command
                second_shell.executeCommand = second_execute_command
            assert debug_commands == [("second", "DB START\n")]

            fd, path = tempfile.mkstemp(
                prefix="pyzo-peer-affinity-", suffix=".py"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(
                    "peer_affinity_result = 'ran-' + peer_kernel_identity\n"
                )
            item = pyzo.editors.loadFile(path, ignoreFail=True)
            assert item is not None
            editor = item.editor
            self._activatePeerPaneForWidget(editor)
            assert pyzo.shells.getCurrentShell() is second_shell
            assert pyzo.shells.getExecutionShell() is second_shell

            editor_host = pyzo.editors._group_for_editor(editor)
            editor_dock = self._editorHosts[editor_host]
            editor_id = self._peerBackend._pane_id_from_dock(editor_dock)
            second_id = self._peerBackend._pane_id_from_dock(second_dock)

            def apply_shell_layout(operation):
                result = operation()
                assert result.applied, result.detail
                QtWidgets.QApplication.processEvents()
                assert first_shell._context is first_context
                assert second_shell._context is second_context
                assert second_shell.get_kernel_cd() == expected_cwd
                assert pyzo.shells.getExecutionShell() is second_shell

            affinity_frame_id = "pyzo-affinity-shell-frame"
            apply_shell_layout(
                lambda: self._peerWorkspace.float(second_id, affinity_frame_id)
            )
            apply_shell_layout(
                lambda: self._peerWorkspace.split(
                    second_id, editor_id, self._PeerDirection.BELOW
                )
            )
            assert affinity_frame_id not in self._peerWorkspace.frames
            assert run_menu._getShellAndEditor("affinity", False)[0] is second_shell
            run_menu._runFile((False, False), editor)
            state.update(
                phase="wait-affinity-run",
                editor=editor,
                path=path,
                first_context=first_context,
                second_context=second_context,
                expected_cwd=expected_cwd,
            )
            return False

        if phase == "wait-affinity-run":
            first_shell = state["first_shell"]
            second_shell = state["second_shell"]
            second_result = second_shell._request.eval(
                "peer_affinity_result"
            ).result_or_cancel(0.2)
            if second_result != "ran-second":
                return False
            first_result = first_shell._request.eval(
                "peer_affinity_result"
            ).result_or_cancel(0.2)
            assert first_result != "ran-second"
            assert first_shell._context is state["first_context"]
            assert second_shell._context is state["second_context"]
            assert second_shell.get_kernel_cd() == state["expected_cwd"]
            assert pyzo.editors.closeFile(state["editor"]) == 1
            os.remove(state["path"])
            original_chooser = self._peerShellCloseChooser
            self._peerShellCloseChooser = (
                lambda _pane_id, _lifecycle: "terminate"
            )
            try:
                assert second_shell.closeShell() == 1
            finally:
                self._peerShellCloseChooser = original_chooser
            assert pyzo.shells.getExecutionShell() is first_shell
            state["cwd_directory"].cleanup()
            state["phase"] = "complete"
            print("Pyzo multi-kernel execution-affinity smoke test passed")
            return True

        return phase == "complete"

    def _testIndependentShellDocks(self):
        """Exercise shell ownership after the deferred default shell exists."""
        first_shell = pyzo.shells.getCurrentShell()
        assert first_shell is not None
        second_shell = pyzo.shells.addShell()
        assert second_shell is not first_shell
        assert len(pyzo.shells.hosts) == 2
        assert all(len(host.getShells()) == 1 for host in pyzo.shells.hosts)
        assert pyzo.shells.getCurrentShell() is second_shell
        if self._peerFrameMode:
            editor_group = pyzo.editors.activeGroup
            editor_dock = self._editorHosts[editor_group]
            second_host = next(
                host for host in pyzo.shells.hosts if second_shell in host.getShells()
            )
            first_host = next(
                host for host in pyzo.shells.hosts if first_shell in host.getShells()
            )

            self._peerBackend._focused_dock_widget_changed(editor_dock)
            self._onFocusedDockWidgetChanged(editor_dock)
            assert pyzo.editors.activeGroup is editor_group
            assert pyzo.shells.getCurrentShell() is second_shell
            assert self._peerWorkspace.resolve_active_context().pane.kind is (
                self._PeerPaneKind.EDITOR
            )

            first_shell_dock = self._shellHosts[first_host]
            self._peerBackend._focused_dock_widget_changed(first_shell_dock)
            self._onFocusedDockWidgetChanged(first_shell_dock)
            assert pyzo.shells.getCurrentShell() is first_shell
            assert pyzo.editors.activeGroup is editor_group
            assert self._peerWorkspace.resolve_active_context().pane.kind is (
                self._PeerPaneKind.SHELL
            )

            second_shell_dock = self._shellHosts[second_host]
            self._peerBackend._focused_dock_widget_changed(second_shell_dock)
            self._onFocusedDockWidgetChanged(second_shell_dock)
            assert pyzo.shells.getCurrentShell() is second_shell
            second_shell_id = self._peerBackend._pane_id_from_dock(
                second_shell_dock
            )
            second_frame_id = self._peerWorkspace.resolve_active_context().frame_id
            assert second_frame_id is not None
            second_window = self._peerBackend.frame_windows[second_frame_id]
            original_chooser = self._peerShellCloseChooser
            self._peerShellCloseChooser = lambda _pane_id, _lifecycle: "cancel"
            assert not second_window.close()
            QtWidgets.QApplication.processEvents()
            assert second_shell_id in self._peerWorkspace.panes
            assert second_window.isVisible()
            self._peerShellCloseChooser = original_chooser
            self._testPeerPaneMovementMatrix(second_host, second_shell)
            original_chooser = self._peerShellCloseChooser
            self._peerShellCloseChooser = lambda _pane_id, _lifecycle: "cancel"
            assert second_shell.closeShell() == 0
            assert second_shell in tuple(pyzo.shells)
            self._peerShellCloseChooser = (
                lambda _pane_id, _lifecycle: "terminate"
            )
            assert second_shell.closeShell() == 1
            self._peerShellCloseChooser = original_chooser
        else:
            second_shell.closeShell()
        assert len(pyzo.shells.hosts) == 1
        assert pyzo.shells.getCurrentShell() is first_shell
        print("Pyzo independent shell-dock smoke test passed")
        if self._peerFrameMode and sys.platform == "darwin":
            self._testPeerZeroFrameLifecycle()

    def _testPeerZeroFrameLifecycle(self):
        assert self._peerWorkspace.frames
        assert not QtWidgets.qApp.quitOnLastWindowClosed()
        editor = pyzo.editors.getCurrentEditor()
        assert editor is not None
        editor_host = pyzo.editors._group_for_editor(editor)
        target_id = self._peerBackend._pane_id_from_dock(
            self._editorHosts[editor_host]
        )
        for pane_id in tuple(self._peerWorkspace.panes):
            if pane_id != target_id:
                result = self._peerWorkspace.tab(pane_id, target_id)
                assert result.applied, result.detail

        frame_id = self._peerWorkspace.resolve_active_context().frame_id
        assert frame_id is not None
        window = self._peerBackend.frame_windows[frame_id]
        original_dirty_chooser = self._peerDirtyCloseChooser
        original_shell_chooser = self._peerShellCloseChooser
        self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: "discard"
        self._peerShellCloseChooser = lambda _pane_id, _lifecycle: "terminate"
        try:
            assert not window.close()
            QtWidgets.QApplication.processEvents()
        finally:
            self._peerDirtyCloseChooser = original_dirty_chooser
            self._peerShellCloseChooser = original_shell_chooser

        assert not self._peerWorkspace.frames
        assert not self._peerWorkspace.panes
        assert not self._peerWorkspace.shutdown_requested
        assert not pyzo.editors.groups
        assert not pyzo.shells.hosts
        assert not pyzo.toolManager.getLoadedTools()

        result = self._peerApplicationLifecycle.application_activated()
        assert result.value == "frame-created"
        QtWidgets.QApplication.processEvents()
        assert len(self._peerWorkspace.frames) == 1
        kinds = {pane.kind for pane in self._peerWorkspace.panes.values()}
        assert self._PeerPaneKind.EDITOR in kinds
        assert self._PeerPaneKind.SHELL in kinds
        assert self._PeerPaneKind.TOOL in kinds
        assert not self._peerWorkspace.shutdown_requested
        print("Pyzo zero-frame survival and default recreation smoke test passed")

    def _testPeerPaneMovementMatrix(self, shell_host, shell):
        """Move real Pyzo surfaces while preserving application identity."""

        editor_group = pyzo.editors.activeGroup
        editor = editor_group.getCurrentEditor()
        editor_document = editor.document()
        editor_dirty = editor_document.isModified()
        editor_dock = self._editorHosts[editor_group]
        shell_dock = self._shellHosts[shell_host]
        shell_context = shell._context
        debugger_widget = shell_host._dbs
        debugger_actions = tuple(shell_host._debugActions)

        pyzo.toolManager.loadTool("pyzofilebrowser")
        tool_dock = pyzo.toolManager._activeTools["pyzofilebrowser"]
        tool_widget = tool_dock.widget()

        editor_id = self._peerBackend._pane_id_from_dock(editor_dock)
        shell_id = self._peerBackend._pane_id_from_dock(shell_dock)
        tool_id = self._peerBackend._pane_id_from_dock(tool_dock)
        assert self._peerWorkspace.panes[editor_id].kind is self._PeerPaneKind.EDITOR
        assert self._peerWorkspace.panes[shell_id].kind is self._PeerPaneKind.SHELL
        assert self._peerWorkspace.panes[tool_id].kind is self._PeerPaneKind.TOOL
        document_id = self._peerDocumentViews.document_id_for_view(editor_id)
        assert document_id is not None
        assert shell_id in self._peerShellLifecycles.pane_ids

        def apply(operation):
            result = operation()
            assert result.applied, result.detail
            QtWidgets.QApplication.processEvents()
            assert editor_group.getCurrentEditor() is editor
            assert editor.document() is editor_document
            assert editor_document.isModified() is editor_dirty
            assert shell_host.getCurrentShell() is shell
            assert shell._context is shell_context
            assert shell_host._dbs is debugger_widget
            assert tuple(shell_host._debugActions) == debugger_actions
            assert tool_dock.widget() is tool_widget
            assert self._editorHosts[editor_group] is editor_dock
            assert self._shellHosts[shell_host] is shell_dock
            assert self._peerDocumentViews.document_id_for_view(editor_id) == document_id
            assert shell_id in self._peerShellLifecycles.pane_ids
            assert self._dockManager.dockAreaCount() == 0

        apply(
            lambda: self._peerWorkspace.split(
                tool_id, editor_id, self._PeerDirection.RIGHT
            )
        )
        apply(lambda: self._peerWorkspace.tab(shell_id, editor_id))
        apply(
            lambda: self._peerWorkspace.float(
                editor_id, "pyzo-matrix-editor-frame"
            )
        )
        apply(
            lambda: self._peerWorkspace.split(
                shell_id, editor_id, self._PeerDirection.BELOW
            )
        )
        apply(
            lambda: self._peerWorkspace.float(
                tool_id, "pyzo-matrix-tool-frame"
            )
        )
        apply(
            lambda: self._peerWorkspace.split(
                tool_id,
                editor_id,
                self._PeerDirection.LEFT,
                outer=True,
            )
        )

        for pane_id, content in (
            (editor_id, editor),
            (shell_id, shell),
            (tool_id, tool_widget),
        ):
            self._activatePeerPaneForWidget(content)
            assert self._peerWorkspace.resolve_active_context().pane.id == pane_id

        peer_window_ids = {
            id(window) for window in self._peerBackend.frame_windows.values()
        }
        assert peer_window_ids <= self._peerStatusProjector.projected_window_ids
        assert peer_window_ids <= self._peerCommandProjector.projected_window_ids
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete
        )
        QtWidgets.QApplication.processEvents()
        self._peerStatusProjector.refresh()
        self._peerCommandProjector.refresh()
        assert self._peerStatusProjector.projected_window_ids == peer_window_ids
        assert self._peerCommandProjector.projected_window_ids == peer_window_ids
        pyzo.toolManager.closeTool("pyzofilebrowser")
        print("Pyzo real-pane movement matrix smoke test passed")
        self._testCleanPeerEditorLifecycle()

    def _testCleanPeerEditorLifecycle(self):
        editors = tuple(pyzo.editors)
        assert len(editors) == 2
        closing, survivor = editors
        closing_host = pyzo.editors._group_for_editor(closing)
        survivor_host = pyzo.editors._group_for_editor(survivor)
        closing_dock = self._editorHosts[closing_host]
        survivor_dock = self._editorHosts[survivor_host]
        closing_pane = self._peerBackend._pane_id_from_dock(closing_dock)
        survivor_pane = self._peerBackend._pane_id_from_dock(survivor_dock)
        closing_document = self._peerDocumentViews.document_id_for_view(closing_pane)
        survivor_document = self._peerDocumentViews.document_id_for_view(survivor_pane)
        assert closing_document is not None
        assert survivor_document is not None

        closing_dock.requestCloseDockWidget()
        assert self._peerBackend.last_pane_close_result.applied
        assert closing not in tuple(pyzo.editors)
        assert survivor in tuple(pyzo.editors)
        assert closing_host not in pyzo.editors.groups
        assert survivor_host in pyzo.editors.groups
        assert closing_host not in self._editorHosts
        assert closing_pane not in self._peerWorkspace.panes
        assert closing_document not in self._peerDocumentViews.document_ids
        assert self._peerDocumentViews.document_id_for_view(survivor_pane) == (
            survivor_document
        )
        assert not self._peerWorkspace.shutdown_requested

        assert pyzo.editors.closeFile(survivor) == 1
        assert tuple(pyzo.editors) == ()
        assert pyzo.editors.groups == ()
        assert pyzo.editors.activeGroup is None
        assert survivor_host not in self._editorHosts
        assert survivor_pane not in self._peerWorkspace.panes
        assert self._peerDocumentViews.document_ids == frozenset()
        assert any(
            pane.kind is self._PeerPaneKind.SHELL
            for pane in self._peerWorkspace.panes.values()
        )
        assert not self._peerWorkspace.shutdown_requested

        replacement = pyzo.editors.newFile()
        replacement_host = pyzo.editors._group_for_editor(replacement.editor)
        replacement_dock = self._editorHosts[replacement_host]
        replacement_pane = self._peerBackend._pane_id_from_dock(replacement_dock)
        assert pyzo.editors.groups == (replacement_host,)
        assert pyzo.editors.activeGroup is replacement_host
        assert self._peerWorkspace.panes[replacement_pane].kind is (
            self._PeerPaneKind.EDITOR
        )
        assert self._peerDocumentViews.document_id_for_view(replacement_pane) is not None

        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete
        )
        QtWidgets.QApplication.processEvents()
        self._peerStatusProjector.refresh()
        self._peerCommandProjector.refresh()
        live_window_ids = {
            id(window) for window in self._peerBackend.frame_windows.values()
        }
        assert self._peerStatusProjector.projected_window_ids == live_window_ids
        assert self._peerCommandProjector.projected_window_ids == live_window_ids
        print("Pyzo clean editor lifecycle matrix smoke test passed")
        self._testDirtyPeerEditorLifecycle(replacement)

    def _testDirtyPeerEditorLifecycle(self, replacement):
        editor = replacement.editor
        host = pyzo.editors._group_for_editor(editor)
        dock = self._editorHosts[host]
        editor.setPlainText("unsaved peer buffer\n")
        editor.document().setModified(True)
        before = self._peerWorkspace.serialize_layout()

        original_save = host.saveFile
        original_chooser = self._peerDirtyCloseChooser
        try:
            host.saveFile = lambda _editor: False
            self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: "save"
            dock.requestCloseDockWidget()
            assert not self._peerBackend.last_pane_close_result.applied
            assert self._peerWorkspace.serialize_layout() == before
            assert editor.document().isModified()

            self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: "cancel"
            dock.requestCloseDockWidget()
            assert not self._peerBackend.last_pane_close_result.applied
            assert self._peerWorkspace.serialize_layout() == before
            assert editor.document().isModified()

            self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: "discard"
            dock.requestCloseDockWidget()
            assert self._peerBackend.last_pane_close_result.applied
            assert editor not in tuple(pyzo.editors)
        finally:
            host.saveFile = original_save
            self._peerDirtyCloseChooser = original_chooser

        file_descriptor, filename = tempfile.mkstemp(
            prefix="pyzo-peer-dirty-", suffix=".py"
        )
        os.close(file_descriptor)
        try:
            with open(filename, "w", encoding="utf-8") as stream:
                stream.write("before = True\n")
            saved_item = pyzo.editors.loadFile(filename, ignoreFail=True)
            assert saved_item is not None
            saved_item.editor.setPlainText("after = True\n")
            saved_item.editor.document().setModified(True)
            self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: "save"
            assert pyzo.editors.closeFile(saved_item.editor) == 1
            with open(filename, encoding="utf-8") as stream:
                assert stream.read() == "after = True\n"
        finally:
            self._peerDirtyCloseChooser = original_chooser
            try:
                os.remove(filename)
            except OSError:
                pass

        first = pyzo.editors.newFile()
        second = pyzo.editors.newFile()
        first.editor.setPlainText("first dirty\n")
        second.editor.setPlainText("second dirty\n")
        first.editor.document().setModified(True)
        second.editor.document().setModified(True)
        first_host = pyzo.editors._group_for_editor(first.editor)
        second_host = pyzo.editors._group_for_editor(second.editor)
        first_pane = self._peerBackend._pane_id_from_dock(
            self._editorHosts[first_host]
        )
        second_pane = self._peerBackend._pane_id_from_dock(
            self._editorHosts[second_host]
        )
        assert self._peerWorkspace.tab(second_pane, first_pane).applied
        frame_id = next(
            frame["id"]
            for frame in self._peerWorkspace.serialize_layout()["frames"]
            if any(
                first_pane in group["panes"]
                for group in frame["groups"]
            )
        )
        shell = pyzo.shells.getCurrentShell()
        assert shell is not None
        shell_host = next(
            host for host in pyzo.shells.hosts if shell in host.getShells()
        )
        shell_pane = self._peerBackend._pane_id_from_dock(
            self._shellHosts[shell_host]
        )
        shell_context = shell._context
        assert self._peerWorkspace.tab(shell_pane, first_pane).applied
        before = self._peerWorkspace.serialize_layout()
        choices = iter(("discard", "cancel"))
        self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: next(choices)
        original_shell_chooser = self._peerShellCloseChooser
        self._peerShellCloseChooser = lambda _pane_id, _lifecycle: "terminate"
        result = self._peerWorkspace.request_close_frame(
            frame_id, self._resolvePeerClose
        )
        self._peerShellCloseChooser = original_shell_chooser
        assert result.status is self._PeerOperationStatus.CANCELLED
        assert self._peerWorkspace.serialize_layout() == before
        assert first.editor.document().isModified()
        assert second.editor.document().isModified()
        assert shell in tuple(pyzo.shells)
        assert shell._context is shell_context
        assert shell_pane in self._peerShellLifecycles.pane_ids
        assert self._peerWorkspace.float(
            shell_pane, "pyzo-dirty-mixed-shell-survivor"
        ).applied

        choices = iter(("discard", "discard"))
        self._peerDirtyCloseChooser = lambda _document_id, _lifecycle: next(choices)
        result = self._peerWorkspace.request_close_frame(
            frame_id, self._resolvePeerEditorClose
        )
        self._peerDirtyCloseChooser = original_chooser
        assert result.applied
        assert first.editor not in tuple(pyzo.editors)
        assert second.editor not in tuple(pyzo.editors)
        assert not self._peerWorkspace.shutdown_requested

        final_item = pyzo.editors.newFile()
        assert pyzo.editors.getCurrentEditor() is final_item.editor
        print("Pyzo dirty editor lifecycle matrix smoke test passed")

    def removeShellHost(self, host):
        dock = self._shellHosts.pop(host, None)
        if dock is not None:
            host.dispose()
            if self._peerFrameMode:
                pane_id = self._peerBackend._pane_id_from_dock(dock)
                if pane_id in self._peerWorkspace.panes:
                    self._removePeerDock(dock)
            else:
                self._dockManager.removeDockWidget(dock)
                dock.deleteLater()

    def _registerPeerDock(self, pane_id, pane_kind, title, dock):
        self._peerBackend.register_dock(pane_id, dock)
        result = self._peerWorkspace.register_pane(
            self._PeerPaneDescriptor(pane_id, pane_kind, title)
        )
        if not result.applied:
            raise RuntimeError(result.detail)
        result = self._peerWorkspace.create_frame(
            pane_id, f"pyzo-frame-{pane_id}"
        )
        if not result.applied:
            raise RuntimeError(result.detail)
        dock.setWindowTitle(title)
        self._peerBackend.frame_windows[f"pyzo-frame-{pane_id}"].setWindowTitle(
            title
        )

    def _removePeerDock(self, dock):
        pane_id = self._peerBackend._pane_id_from_dock(dock)
        result = self._peerWorkspace.request_close_pane(pane_id)
        if not result.applied:
            raise RuntimeError(result.detail)

    def addAdsDockWidget(self, area, dock, target_dock=None):
        if not self._peerFrameMode:
            return self._dockManager.addDockWidget(
                area,
                dock,
                target_dock.dockAreaWidget() if target_dock is not None else None,
            )

        pane_id = f"tool-{dock._toolId}"
        self._registerPeerDock(pane_id, self._PeerPaneKind.TOOL, dock.windowTitle(), dock)
        result = self._peerWorkspace.set_close_policy(
            pane_id, _PyzoToolClosePolicy(self, pane_id, dock)
        )
        if not result.applied:
            raise RuntimeError(result.detail)
        if target_dock is not None:
            target_id = self._peerBackend._pane_id_from_dock(target_dock)
            result = self._peerWorkspace.split(
                pane_id, target_id, self._PeerDirection.RIGHT
            )
            if not result.applied:
                raise RuntimeError(result.detail)
        return dock.dockAreaWidget()

    def removePeerToolDock(self, dock):
        if dock._toolManager is not None:
            dock._toolManager.onToolClose(dock._toolId)
            dock._toolManager = None
        self._removePeerDock(dock)

    def _closeCommittedPeerTool(self, pane_id, dock):
        assert pane_id not in self._peerWorkspace.panes
        tool = dock.widget()
        if tool is not None and hasattr(tool, "cleanUp"):
            tool.cleanUp()
        if dock._toolManager is not None:
            dock._toolManager.onToolClose(dock._toolId)
            dock._toolManager = None

    def _testPeerFrameNativeOverlays(self):
        assert QtWidgets.qApp.applicationName() == "Pyzo"
        assert QtWidgets.qApp.applicationDisplayName() == "Pyzo"
        assert self._dockManager.parent() is None
        assert self._dockManager.dockAreaCount() == 0
        assert not hasattr(self, "_adsDropGuideController")
        assert self._editorDock.isInFloatingContainer()
        probe_dock = self._QtAds.CDockWidget(
            self._dockManager, "Native frame close probe"
        )
        probe_dock.setWidget(QtWidgets.QLabel("Native frame close probe"))
        probe_id = "native-frame-close-probe"
        self._registerPeerDock(
            probe_id,
            self._PeerPaneKind.CUSTOM,
            "Native frame close probe",
            probe_dock,
        )
        probe_frame_id = f"pyzo-frame-{probe_id}"
        probe_window = self._peerBackend.frame_windows[probe_frame_id]
        assert not probe_window.close()
        QtWidgets.QApplication.processEvents()
        assert probe_id not in self._peerWorkspace.panes
        assert probe_frame_id not in self._peerWorkspace.frames
        assert not self._peerWorkspace.shutdown_requested
        floating = self._editorDock.floatingDockContainer()
        assert floating is not None
        assert all(
            type(window).__name__ == "CFloatingDockContainer"
            for window in self._peerBackend.frame_windows.values()
        )
        assert pyzo.commands.descriptors[0].id == "application.quit"
        assert pyzo.commands.descriptors[1].id == "application.restart"
        assert self._peerCommandProjector.native_menus == (sys.platform == "darwin")
        peer_windows = tuple(self._peerBackend.frame_windows.values())
        status_was_visible = self.statusBar().isVisible()
        self.statusBar().setVisible(True)
        self.statusBar().showMessage("Peer status smoke")
        self.statusBar().updateCursorInfo(pyzo.editors.getCurrentEditor())
        assert self.statusBar().currentMessage() == "Peer status smoke"
        assert not self.findChildren(QtWidgets.QStatusBar)
        peer_status_bars = tuple(
            self._peerStatusProjector.status_bar_for(window)
            for window in peer_windows
        )
        assert all(bar.isVisible() for bar in peer_status_bars)
        assert all(
            bar.message_label.text() == "Peer status smoke"
            for bar in peer_status_bars
        )
        assert len({bar.cursor_label.text() for bar in peer_status_bars}) == 1
        self.statusBar().clearMessage()
        self.statusBar().setVisible(status_was_visible)
        peer_quit_actions = tuple(
            self._peerCommandProjector.action_for(window, "application.quit")
            for window in peer_windows
        )
        peer_restart_actions = tuple(
            self._peerCommandProjector.action_for(window, "application.restart")
            for window in peer_windows
        )
        if sys.platform == "darwin":
            application_menu = self._peerCommandProjector.application_menu_bar
            assert application_menu is not None
            assert application_menu.parent() is None
            assert len({id(action) for action in peer_quit_actions}) == 1
            assert len({id(action) for action in peer_restart_actions}) == 1
        else:
            assert self._peerCommandProjector.application_menu_bar is None
            assert all(
                self._peerCommandProjector.menu_bar_for(window).parent() is window
                for window in peer_windows
            )
            assert len({id(action) for action in peer_quit_actions}) == len(peer_windows)
            assert len({id(action) for action in peer_restart_actions}) == len(
                peer_windows
            )
        assert all(
            action.property("applicationCommandId") == "application.quit"
            for action in peer_quit_actions
        )
        quit_action = peer_quit_actions[0]
        original_quit = pyzo.main.quit
        quit_calls = []
        try:
            pyzo.main.quit = lambda: quit_calls.append(True) or False
            quit_action.trigger()
        finally:
            pyzo.main.quit = original_quit
        assert quit_calls == [True]
        restart_action = peer_restart_actions[0]
        original_restart = pyzo.main.restart
        restart_calls = []
        try:
            pyzo.main.restart = lambda: restart_calls.append(True) or False
            restart_action.trigger()
        finally:
            pyzo.main.restart = original_restart
        assert restart_calls == [True]

        container_overlay = self._dockManager.containerOverlay()
        dock_area_overlay = self._dockManager.dockAreaOverlay()
        container = floating.dockContainer()
        area = self._editorDock.dockAreaWidget()
        container_overlay.showOverlay(
            container, container.mapToGlobal(container.rect().center())
        )
        dock_area_overlay.showOverlay(area, area.mapToGlobal(area.rect().center()))
        QtWidgets.QApplication.processEvents()
        assert container_overlay.isVisible()
        assert dock_area_overlay.isVisible()
        assert not any(
            type(widget).__name__ == "_AdsDropGuide"
            for widget in QtWidgets.QApplication.topLevelWidgets()
        )
        container_overlay.hideOverlay()
        dock_area_overlay.hideOverlay()
        print("Pyzo peer-frame native ADS overlay ownership smoke test passed")

    def _traceAdsWidget(self, widget):
        if hasattr(self, "_adsTrace") and widget is not None:
            widget.installEventFilter(self._adsTrace)

    def setMainTitle(self, path=None):
        """Set the title of the main window, by giving a file path."""
        if not path:
            # Plain title
            title = "Interactive Editor for Python"
        else:
            # Title with a filename
            name = os.path.basename(path)
            if os.path.isfile(path):
                pass
            elif name == path:
                path = translate("main", "unsaved")
            else:
                pass  # We hope the given path is informative
            # Set title
            tmp = {
                "fileName": name,
                "filename": name,
                "name": name,
                "fullPath": path,
                "fullpath": path,
                "path": path,
            }
            title = pyzo.config.advanced.titleText.format(**tmp)

        # Set
        self.setWindowTitle(title)

    def getWindowState(self):
        return {
            # list of tools which are loaded
            "loadedTools": pyzo.toolManager.getLoadedTools(),
            # geometry of the top level window
            "windowGeometry": base64.encodebytes(self.saveGeometry().data()).decode(
                "ascii"
            ),
            # layout of dockwidgets and toolbars
            "windowState": base64.encodebytes(self.saveState().data()).decode("ascii"),
        }

    def saveWindowState(self):
        """Save:
        * which tools are loaded
        * geometry of the top level windows
        * layout of dockwidgets and toolbars
        """
        pyzo.config.state.update(self.getWindowState())

    def restoreTools(self, value=None):
        """Restore loaded tools"""

        if value is None:
            # No value given, try to get it from the config
            value = pyzo.config.state.loadedTools

        toolsTarget = set(value)
        toolsLoaded = set(pyzo.toolManager.getLoadedTools())
        for t in toolsLoaded - toolsTarget:
            pyzo.toolManager.closeTool(t)
        for t in toolsTarget - toolsLoaded:
            pyzo.toolManager.loadTool(t)

    def restoreGeometry(self, value=None):
        """Restore window position and whether it is maximized"""

        if value is not None:
            return super().restoreGeometry(value)

        # No value given, try to get it from the config
        if pyzo.config.state.windowGeometry:
            try:
                geometry = pyzo.config.state.windowGeometry
                geometry = base64.decodebytes(geometry.encode("ascii"))
                self.restoreGeometry(geometry)
            except Exception as err:
                print("Could not restore window geometry: " + str(err))

    def restoreState(self, value=None):
        """Restore layout of dock widgets and toolbars"""

        if value is not None:
            return super().restoreState(value)

        # No value given, try to get it from the config
        if pyzo.config.state.windowState:
            try:
                state = pyzo.config.state.windowState
                state = base64.decodebytes(state.encode("ascii"))
                self.restoreState(state)
            except Exception as err:
                print("Could not restore window state: " + str(err))

    def setQtStyle(self, stylename=None):
        """Set the style and the palette, based on the given style name.
        If stylename is None or not given will do some initialization.
        If bool(stylename) evaluates to False will use the default style
        for this system. Returns the QStyle instance.
        """

        if stylename is None:
            # Initialize

            # Get native palette (used below)
            QtWidgets.qApp.nativePalette = QtWidgets.qApp.palette()

            # Obtain default style name
            pyzo.defaultQtStyleName = str(QtWidgets.qApp.style().objectName())

            # Other than gtk+ and mac, Fusion/Cleanlooks looks best (in my opinion)
            if "gtk" in pyzo.defaultQtStyleName.lower():
                pass  # Use default style
            elif "macintosh" in pyzo.defaultQtStyleName.lower():
                pass  # Use default style
            else:
                pyzo.defaultQtStyleName = "Fusion"

            # Set style if there is no style yet
            if not pyzo.config.view.qtstyle:
                pyzo.config.view.qtstyle = pyzo.defaultQtStyleName

        # Init
        if not stylename:
            stylename = pyzo.config.view.qtstyle

        # Check if this style exist, set to default otherwise
        styleNames = [name.lower() for name in QtWidgets.QStyleFactory.keys()]
        if stylename.lower() not in styleNames:
            stylename = pyzo.defaultQtStyleName

        # Try changing the style
        qstyle = QtWidgets.qApp.setStyle(stylename)

        # Set palette
        if qstyle:
            QtWidgets.qApp.setPalette(QtWidgets.qApp.nativePalette)

        # detect dark mode for widgets (might be different than dark mode for syntax)
        pal = QtWidgets.qApp.palette()
        pyzo.darkQt = (
            pal.window().color().lightness() < pal.windowText().color().lightness()
        )

        # Done
        return qstyle

    def closeEvent(self, event):
        """Override close event handler."""

        if self._peerFrameMode:
            event.ignore()
            self.quit()
            return

        # Are we restaring?
        restarting = time.time() - self._closeflag < 1.0  # noqa: F841

        if pyzo.config.advanced.alwaysAskWhenClosingPyzo:
            dlg = QtWidgets.QMessageBox(pyzo.main)
            dlg.setWindowTitle("Pyzo")
            dlg.setText(translate("main", "Do you really want to close Pyzo?"))

            infoTexts = []
            busyStates = ("Busy", "Very busy", "Debug")
            if any(shell._state in busyStates for shell in pyzo.shells):
                infoTexts.append(
                    translate("main", "At least one shell is still executing code.")
                )
            if any(ed.document().isModified() for ed in pyzo.editors):
                infoTexts.append(
                    translate("main", "At least one editor has unsaved changes.")
                )
            dlg.setInformativeText("\n".join(infoTexts))

            btnClose = dlg.addButton(QtWidgets.QMessageBox.StandardButton.Close)
            btnCancel = dlg.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            dlg.setDefaultButton(btnCancel)
            dlg.exec()
            if dlg.clickedButton() is not btnClose:
                self._closeflag = False
                event.ignore()
                return

        # Proceed with closing...
        result = pyzo.editors.closeAll()
        if not result:
            self._closeflag = False
            event.ignore()
            return
        else:
            self._closeflag = True
            # event.accept()  # Had to comment on Windows+py3.3 to prevent error

        # Save settings
        pyzo.saveConfig()
        pyzo.command_history.save()

        # Stop command server
        commandline.stop_our_server()

        # Proceed with closing shells
        pyzo.localKernelManager.terminateAll()

        for shell in pyzo.shells:
            shell._context.close()

        # The tools need to be explicitly closed to allow them to clean up
        for toolname in pyzo.toolManager.getLoadedTools():
            tool = pyzo.toolManager.getTool(toolname)
            if hasattr(tool, "cleanUp"):
                tool.cleanUp()

        # Stop all threads (this should really only be daemon threads)
        import threading

        for thread in threading.enumerate():
            if hasattr(thread, "stop"):
                try:
                    thread.stop(0.1)
                except Exception:
                    pass

        #         # Wait for threads to die ...
        #         # This should not be necessary, but I used it in the hope that it
        #         # would prevent the segfault on Python3.3. It didn't.
        #         timeout = time.time() + 0.5
        #         while threading.activeCount() > 1 and time.time() < timeout:
        #             time.sleep(0.1)
        #         print('Number of threads alive:', threading.activeCount())

        # Proceed as normal
        super().closeEvent(event)

    def quit(self):
        """Request application Quit independently from frame closure."""

        if self._peerFrameMode:
            return self._requestPeerApplicationExit(
                self._ApplicationExitDisposition.QUIT
            )
        return self.close()

    def restart(self):
        """Restart Pyzo."""

        if self._peerFrameMode:
            return self._requestPeerApplicationExit(
                self._ApplicationExitDisposition.RESTART
            )

        self._closeflag = time.time()

        # Close
        self.close()

        if self._closeflag:
            self._replacePyzoProcess()

    def _replacePyzoProcess(self):
        """Replace this process with a fresh Pyzo process."""

        # Get args
        args = list(sys.argv)

        if not paths.is_frozen():
            # Prepend the executable name (required on Linux)
            lastBit = os.path.basename(sys.executable)
            args.insert(0, lastBit)

        # When running from the pip entry point pyzo.exe ... (issue #641)
        if (
            len(args) == 2
            and args[0] == "python.exe"
            and not os.path.isfile(args[1])
        ):
            args = ["python.exe", "-m", "pyzo"]

        if sys.platform == "win32":
            # workaround for MSVCRT issue with spaces in arguments
            #     https://bugs.python.org/issue436259
            from subprocess import list2cmdline

            args = [list2cmdline([s]) for s in args]

        # Replace the process!
        os.execv(sys.executable, args)

    def createPopupMenu(self):
        # This menu pops up when right clicking on the menu bar
        # or when right clicking on the title bar of a panel.

        # We have to use the initialized menu from the parent class because creating
        # a completely new QMenu would result in a memory error.
        menu = super().createPopupMenu()
        menu.clear()

        # The build_callback function is necessary because without that, a simple lambda
        # would bind the argument to the latest action of the for loop via the closure.
        def build_callback(cb, action):
            return lambda: cb(action.isChecked())

        # Add all tools, with checkmarks for those that are active
        for tool in pyzo.toolManager.getToolInfo():
            a = menu.addAction(tool.name)
            a.setCheckable(True)
            a.setChecked(bool(tool.instance))
            a.triggered.connect(build_callback(tool.menuLauncher, a))

        return menu

    def keyPressEvent(self, event):
        # Here we forward the key press events to the editor widget so that tab switching
        # will also be possible when the focus is outside the editor widget.
        if pyzo.editors:  # check for None to avoid crash during Pyzo startup
            pyzo.editors.processKeyPressFromMainWindow(event)


def loadAppIcons():
    """Load the application icons."""
    # Get directory containing the icons
    appiconDir = os.path.join(pyzo.pyzoDir, "resources", "appicons")

    # Determine template for filename of the application icon-files.
    fnameT = "pyzologo{}.png"

    # Construct application icon. Include a range of resolutions. Note that
    # Qt somehow does not use the highest possible res on Linux/Gnome(?), even
    # the logo of qt-designer when alt-tabbing looks a bit ugly.
    pyzo.icon = QtGui.QIcon()

    # Construct another icon to show when the current shell is busy
    pyzo.iconRunning = QtGui.QIcon(pyzo.icon)

    for sze in [16, 32, 48, 64, 128, 256]:
        fname = os.path.join(appiconDir, fnameT.format(sze))
        if os.path.isfile(fname):
            pyzo.icon.addFile(fname, QtCore.QSize(sze, sze))

            artist = IconArtist(pyzo.icon, size=sze)
            artist.setPenColor("#0D0")
            artist.setBrushColor("#0D0")
            a = 6 * sze // 16
            x2 = sze - 1
            x1 = x2 - a + 1
            y1 = sze - 1
            y2 = y1 - a + 1
            y3 = y2 - a + 1
            artist.addPolygon([(x1, y1), (x2, y2), (x1, y3)])
            pm = artist.finish().pixmap(sze, sze)
            pyzo.iconRunning.addPixmap(pm)

    # Set as application icon. This one is used as the default for all
    # windows of the application.
    QtWidgets.qApp.setWindowIcon(pyzo.icon)


def loadIcons():
    """Load all icons in the icon dir."""
    # Get directory containing the icons
    iconDir = os.path.join(pyzo.pyzoDir, "resources", "icons")

    # Construct other icons
    dummyIcon = IconArtist().finish()
    pyzo.icons = ssdf.new()
    for fname in os.listdir(iconDir):
        if fname.endswith(".png"):
            try:
                # Short and full name
                name = fname.split(".")[0]
                name = name.replace("pyzo_", "")  # discard prefix
                ffname = os.path.join(iconDir, fname)
                # Create icon
                icon = QtGui.QIcon()
                icon.addFile(ffname, QtCore.QSize(16, 16))
                # Store
                pyzo.icons[name] = icon
            except Exception as err:
                pyzo.icons[name] = dummyIcon
                print("Could not load icon {}: {}".format(fname, err))

    artist = IconArtist("folder_page")
    artist.addLayer("arrow_refresh")
    pyzo.icons["reload_file_from_disk"] = artist.finish()

    artist = IconArtist("page_white_copy")
    artist.addLayer("overlay_disk")
    pyzo.icons["save_copy_as"] = artist.finish()


def loadFonts():
    """Load all fonts that come with Pyzo."""
    import pyzo.codeeditor  # we need pyzo and codeeditor namespace here

    # Get directory containing the icons
    fontDir = os.path.join(pyzo.pyzoDir, "resources", "fonts")

    # Get database object
    db = QtGui.QFontDatabase  # static class

    # Set default font
    pyzo.codeeditor.Manager.setDefaultFontFamily("DejaVu Sans Mono")

    # Load fonts that are in the fonts directory
    if os.path.isdir(fontDir):
        for fname in os.listdir(fontDir):
            if "oblique" in fname.lower():  # issue #461
                continue
            if os.path.splitext(fname)[1].lower() in [".otf", ".ttf"]:
                try:
                    db.addApplicationFont(os.path.join(fontDir, fname))
                except Exception as err:
                    print("Could not load font {}: {}".format(fname, err))


class _CallbackEventHandler(QtCore.QObject):
    """Helper class to provide the callLater function."""

    def __init__(self):
        super().__init__()
        self.queue = Queue()

    def customEvent(self, event):
        while True:
            try:
                callback, args = self.queue.get_nowait()
            except Empty:
                break
            try:
                callback(*args)
            except Exception as why:
                print("callback failed: {}:\n{}".format(callback, why))

    def postEventWithCallback(self, callback, *args):
        self.queue.put((callback, args))
        QtWidgets.qApp.postEvent(self, QtCore.QEvent(QtCore.QEvent.Type.User))


def callLater(callback, *args):
    """Post a callback to be called in the main thread."""
    _callbackEventHandler.postEventWithCallback(callback, *args)


# Create callback event handler instance and insert function in pyzo namespace
_callbackEventHandler = _CallbackEventHandler()
pyzo.callLater = callLater


_SCREENSHOT_CODE = """
import random

numerator = 4

def get_number():
    # todo: something appears to be broken here
    val = random.choice(range(10))
    return numerator / val

class Groceries(list):
    \"\"\" Overloaded list class.
    \"\"\"
    def append_defaults(self):
        spam = 'yum'
        pie = 3.14159
        self.extend([spam, pie])

class GroceriesPlus(Groceries):
    \"\"\" Groceries with surprises!
    \"\"\"
    def append_random(self):
        value = get_number()
        self.append(value)

# Create some groceries
g = GroceriesPlus()
g.append_defaults()
g.append_random()

"""


def screenshotExample(width=1244, height=700):
    e = pyzo.editors.newFile()
    e.editor.setPlainText(_SCREENSHOT_CODE)
    pyzo.main.resize(width, height)


def screenshot(countdown=5):
    QtCore.QTimer.singleShot(countdown * 1000, _screenshot)


def _screenshot():
    # Grab
    print("SNAP!")
    screen = QtWidgets.qApp.primaryScreen()
    pix = screen.grabWindow(pyzo.main.winId())
    # Get name
    i = 1
    while i > 0:
        name = "pyzo_screen_{}_{:02d}.png".format(sys.platform, i)
        fname = os.path.join(os.path.expanduser("~"), name)
        if os.path.isfile(fname):
            i += 1
        else:
            i = -1
    # Save screenshot and a thumb
    pix.save(fname)
    thumb = pix.scaledToWidth(500, QtCore.Qt.SmoothTransformation)
    thumb.save(fname.replace("screen", "thumb"))
    print("Screenshot and thumb saved in", os.path.expanduser("~"))


pyzo.screenshot = screenshot
pyzo.screenshotExample = screenshotExample
