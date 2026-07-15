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
        pyzo.editors.restoreEditorState()

        # Instantiate view manager
        pyzo.viewManager = ViewManager()

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
            from spikes.peer_frames.commands import (
                CommandDescriptor,
                CommandRegistry,
                VIEW_STATUS_COMMAND,
            )
            from spikes.peer_frames.contract import (
                Direction,
                PaneDescriptor,
                PaneKind,
                WorkspaceModel,
            )
            from spikes.peer_frames.qtads_backend import QtAdsPeerFrameBackend
            from spikes.peer_frames.qtads_commands import QtAdsCommandProjector
            from spikes.peer_frames.qtads_status import QtAdsStatusProjector
            from spikes.peer_frames.status import StatusState

            self._PeerDirection = Direction
            self._PeerPaneDescriptor = PaneDescriptor
            self._PeerPaneKind = PaneKind
            self._peerBackend = QtAdsPeerFrameBackend()
            self._peerWorkspace = WorkspaceModel(self._peerBackend)
            self._peerBackend.bind_model(self._peerWorkspace)
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
                    lambda _context: pyzo.main.close(),
                    shortcuts=("Ctrl+Q",),
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
                (VIEW_STATUS_COMMAND, "application.quit"),
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
        else:
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
            lambda host=host: self._updateEditorDockTitle(host)
        )
        if self._menusReady:
            host.addContextMenu()
        return host

    def _updateEditorDockTitle(self, host):
        dock = self._editorHosts.get(host)
        if dock is None:
            return
        item = host._tabs.currentItem()
        title = item.name if item is not None else "Untitled"
        if item is not None and item.dirty:
            title += " *"
        dock.setWindowTitle(title)

    def _removeEditorHost(self, host):
        dock = self._editorHosts.pop(host, None)
        if dock is not None:
            if self._peerFrameMode:
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
        return host

    def _updateShellDockTitle(self, host):
        from pyzo.core.shellStack import shellTitle

        shell = host.getCurrentShell()
        dock = self._shellHosts.get(host)
        if shell is not None and dock is not None:
            dock.setWindowTitle(shellTitle(shell))

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
        second_shell.closeShell()
        assert len(pyzo.shells.hosts) == 1
        assert pyzo.shells.getCurrentShell() is first_shell
        print("Pyzo independent shell-dock smoke test passed")

    def removeShellHost(self, host):
        dock = self._shellHosts.pop(host, None)
        if dock is not None:
            host.dispose()
            if self._peerFrameMode:
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

    def _testPeerFrameNativeOverlays(self):
        assert QtWidgets.qApp.applicationName() == "Pyzo"
        assert QtWidgets.qApp.applicationDisplayName() == "Pyzo"
        assert self._dockManager.parent() is None
        assert self._dockManager.dockAreaCount() == 0
        assert not hasattr(self, "_adsDropGuideController")
        assert self._editorDock.isInFloatingContainer()
        floating = self._editorDock.floatingDockContainer()
        assert floating is not None
        assert all(
            type(window).__name__ == "CFloatingDockContainer"
            for window in self._peerBackend.frame_windows.values()
        )
        assert pyzo.commands.descriptors[0].id == "application.quit"
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
        if sys.platform == "darwin":
            application_menu = self._peerCommandProjector.application_menu_bar
            assert application_menu is not None
            assert application_menu.parent() is None
            assert len({id(action) for action in peer_quit_actions}) == 1
        else:
            assert self._peerCommandProjector.application_menu_bar is None
            assert all(
                self._peerCommandProjector.menu_bar_for(window).parent() is window
                for window in peer_windows
            )
            assert len({id(action) for action in peer_quit_actions}) == len(peer_windows)
        assert all(
            action.property("applicationCommandId") == "application.quit"
            for action in peer_quit_actions
        )
        quit_action = peer_quit_actions[0]
        original_close = pyzo.main.close
        quit_calls = []
        try:
            pyzo.main.close = lambda: quit_calls.append(True) or False
            quit_action.trigger()
        finally:
            pyzo.main.close = original_close
        assert quit_calls == [True]

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
        if self._peerFrameMode:
            self._peerBackend.close()
            QtWidgets.qApp.quit()
        super().closeEvent(event)

    def restart(self):
        """Restart Pyzo."""

        self._closeflag = time.time()

        # Close
        self.close()

        if self._closeflag:
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
