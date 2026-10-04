from __future__ import annotations

import unittest

from PySide6 import QtCore, QtWidgets
import PySide6QtAds as QtAds
import pyzo

from pyzo.core.applicationController import DocumentCollectionAccess
from pyzo.core.workspaceRuntime import WorkspaceRuntimeCatalog
from spikes.peer_frames.contract import PaneDescriptor, PaneKind, WorkspaceModel
from spikes.peer_frames.document_resources import DocumentResourceRegistry
from spikes.peer_frames.identity import IdentityKind, new_identity
from spikes.peer_frames.qtads_backend import QtAdsPeerFrameBackend
from spikes.peer_frames.workspace_coordinator import WorkspaceCoordinator


class WorkspaceRuntimeQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_catalog_renders_two_independent_real_qt_workspaces(self) -> None:
        documents = object()
        undo = object()
        catalog = WorkspaceRuntimeCatalog(
            shared_services={"documents": documents, "undo": undo}
        )
        coordinator = WorkspaceCoordinator()
        identities = [new_identity(IdentityKind.WORKSPACE) for _ in range(2)]
        runtimes = []
        try:
            for index, identity in enumerate(identities):
                runtime = catalog.create(
                    identity=identity,
                    backend_factory=QtAdsPeerFrameBackend,
                    model_factory=lambda backend, workspace_id: WorkspaceModel(
                        backend, workspace_id=workspace_id
                    ),
                )
                runtimes.append(runtime)
                coordinator.add_workspace(
                    runtime.model,
                    name=f"Workspace {index + 1}",
                    identity=identity,
                )
                dock = QtAds.CDockWidget(
                    runtime.backend.manager, f"Workspace {index + 1} editor"
                )
                dock.setWidget(QtWidgets.QPlainTextEdit())
                pane_id = f"{identity}-editor"
                runtime.backend.register_dock(pane_id, dock)
                self.assertTrue(
                    runtime.model.register_pane(
                        PaneDescriptor(pane_id, PaneKind.EDITOR, "Editor")
                    ).applied
                )
                self.assertTrue(
                    runtime.model.create_frame(pane_id, f"{pane_id}-frame").applied
                )

            self.app.processEvents()
            self.assertEqual(catalog.identities, tuple(identities))
            self.assertEqual(
                {len(runtime.frame_windows) for runtime in runtimes}, {1}
            )
            first_window = next(iter(runtimes[0].frame_windows.values()))
            second_window = next(iter(runtimes[1].frame_windows.values()))
            self.assertIsNot(first_window, second_window)
            self.assertIs(runtimes[0].shared_services["documents"], documents)
            self.assertIs(runtimes[1].shared_services["undo"], undo)
        finally:
            for runtime in runtimes:
                for pane_id in tuple(runtime.model.panes):
                    runtime.model.request_close_pane(pane_id)
            for identity in identities:
                if identity in coordinator.workspace_ids:
                    self.assertTrue(coordinator.remove_workspace(identity).applied)
            catalog.dispose_all()
            self.app.processEvents()

    def test_two_runtime_editors_share_document_undo_and_keep_view_cursors(self):
        documents = DocumentResourceRegistry()
        workspaces = [new_identity(IdentityKind.WORKSPACE) for _ in range(2)]
        catalog = WorkspaceRuntimeCatalog(
            shared_services={"documents": documents, "undo": documents}
        )
        coordinator = WorkspaceCoordinator()
        runtimes = []
        docks = []
        document_id = documents.create_document("start")
        view_ids = [
            documents.open_view(document_id, workspace_id) for workspace_id in workspaces
        ]
        editors = []
        for index, (workspace_id, view_id) in enumerate(zip(workspaces, view_ids)):
            runtime = catalog.create(
                identity=workspace_id,
                backend_factory=QtAdsPeerFrameBackend,
                model_factory=lambda backend, identity: WorkspaceModel(
                    backend, workspace_id=identity
                ),
            )
            runtimes.append(runtime)
            coordinator.add_workspace(
                runtime.model,
                name=f"Workspace {index + 1}",
                identity=workspace_id,
            )
            editor = QtWidgets.QPlainTextEdit()
            editor._peerDocumentIdentity = document_id
            editor._peerViewIdentity = view_id
            editor.calltipCancel = lambda: None
            editor._showRunCursorTimer = QtCore.QTimer(editor)
            editor.setPlainText("start")
            editors.append(editor)
            dock = QtAds.CDockWidget(runtime.backend.manager, f"Editor {index + 1}")
            dock.setWidget(editor)
            docks.append(dock)
            pane_id = f"{workspace_id}-editor"
            runtime.backend.register_dock(pane_id, dock)
            self.assertTrue(
                runtime.model.register_pane(
                    PaneDescriptor(pane_id, PaneKind.EDITOR, "Editor")
                ).applied
            )
            self.assertTrue(runtime.model.create_frame(pane_id, f"{pane_id}-frame").applied)

        class Parser:
            def __init__(self):
                self.calls = []

            def parseThis(self, editor):
                self.calls.append(editor)

        pyzo.parser = Parser()
        adapter = DocumentCollectionAccess(documents)
        for editor in editors:
            adapter.bind(editor)

        try:
            cursor = editors[1].textCursor()
            cursor.setPosition(2)
            editors[1].setTextCursor(cursor)
            editors[0].setPlainText("changed")
            self.assertEqual(documents.resource(document_id).text, "changed")
            self.assertEqual(editors[1].toPlainText(), "changed")
            self.assertEqual(editors[1].textCursor().position(), 2)

            self.assertTrue(adapter.undo(editors[1]))
            self.assertEqual(documents.resource(document_id).text, "start")
            self.assertEqual(editors[0].toPlainText(), "start")
            self.assertEqual(editors[1].toPlainText(), "start")
            self.assertTrue(pyzo.parser.calls)

            self.assertTrue(adapter.redo(editors[0]))
            self.assertEqual(documents.resource(document_id).text, "changed")
            self.assertEqual(editors[0].toPlainText(), "changed")
            self.assertEqual(editors[1].toPlainText(), "changed")

            adapter.unbind(editors[0])
            self.assertTrue(documents.close_view(view_ids[0]))
            # Pyzo can deliver callbacks queued before unbind after another
            # live view has edited the resource. The retired widget is stale.
            editors[1].setPlainText("survivor edit")
            adapter._contents_changed(editors[0], 0, 0, 1)
            adapter._cursor_changed(editors[0])
            adapter._changed(editors[0])
            self.assertEqual(documents.resource(document_id).text, "survivor edit")
            self.assertEqual(editors[1].toPlainText(), "survivor edit")
            self.assertTrue(adapter.undo(editors[1]))
            editors[0].deleteLater()
            self.assertEqual(documents.views_for_document(document_id), (view_ids[1],))
            self.assertTrue(adapter.undo(editors[1]))
            self.assertEqual(documents.resource(document_id).text, "start")
            self.assertTrue(adapter.redo(editors[1]))
            self.assertEqual(documents.resource(document_id).text, "changed")

            adapter.unbind(editors[1])
            self.assertTrue(documents.close_view(view_ids[1]))
            self.assertEqual(
                documents.retained_view(view_ids[1]).workspace_id, workspaces[1]
            )
            self.assertFalse(documents.resource(document_id).live)
            reopened = documents.reopen_view(
                document_id,
                view_id=view_ids[1],
                workspace_id=workspaces[1],
            )
            self.assertEqual(reopened, view_ids[1])
            self.assertTrue(documents.resource(document_id).live)
            with self.assertRaises(KeyError):
                documents.reopen_view(
                    document_id,
                    view_id=view_ids[1],
                    workspace_id=workspaces[1],
                )
        finally:
            for editor in editors:
                adapter.unbind(editor)
                editor.deleteLater()
            for runtime in runtimes:
                for pane_id in tuple(runtime.model.panes):
                    runtime.model.request_close_pane(pane_id)
            for workspace_id in workspaces:
                if workspace_id in coordinator.workspace_ids:
                    self.assertTrue(coordinator.remove_workspace(workspace_id).applied)
            catalog.dispose_all()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
