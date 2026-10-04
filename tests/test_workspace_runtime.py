from __future__ import annotations

import unittest

from pyzo.core.workspaceRuntime import WorkspaceRuntime, WorkspaceRuntimeCatalog
from spikes.peer_frames.contract import WorkspaceModel
from spikes.peer_frames.fake_backend import FakeBackend as CoordinatorBackend
from spikes.peer_frames.identity import IdentityKind, new_identity
from spikes.peer_frames.workspace_coordinator import WorkspaceCoordinator


class FakeBackend:
    def __init__(self, *, fail_bind=False):
        self.fail_bind = fail_bind
        self.frame_windows = {}
        self.bound = None
        self.close_count = 0

    def bind_model(self, model, frame_close_request, pane_close_request):
        if self.fail_bind:
            raise RuntimeError("bind failed")
        self.bound = (model, frame_close_request, pane_close_request)

    def close(self):
        self.close_count += 1


class FakeModel:
    def __init__(self, backend, identity):
        self.backend = backend
        self.identity = identity


class WorkspaceRuntimeTests(unittest.TestCase):
    def test_initial_runtime_isolates_preferred_failure_and_uses_a_fallback(self):
        for preferred, failed, expected in (
            (("a", "b"), set(), "a"),
            (("a", "b"), {"a"}, "b"),
            (("a", "b"), {"a", "b"}, "bootstrap"),
            ((), set(), "bootstrap"),
        ):
            with self.subTest(preferred=preferred, failed=failed):
                catalog = WorkspaceRuntimeCatalog()
                backends = []

                def backend_factory(backends=backends):
                    backend = FakeBackend()
                    backends.append(backend)
                    return backend

                def model_factory(backend, identity, failed=failed):
                    if identity in failed:
                        raise RuntimeError("failed " + identity)
                    return FakeModel(backend, identity)

                runtime, failures = catalog.create_preferred(
                    preferred,
                    fallback_identity="bootstrap",
                    backend_factory=backend_factory,
                    model_factory=model_factory,
                )
                self.assertEqual(runtime.identity, expected)
                self.assertEqual(
                    failures,
                    {
                        identity: "failed " + identity
                        for identity in preferred
                        if identity in failed
                    },
                )
                self.assertEqual(catalog.identities, (expected,))
                self.assertTrue(
                    all(backend.close_count == 1 for backend in backends[:-1])
                )
                catalog.dispose_all()
                self.assertEqual(backends[-1].close_count, 1)

    def test_runtime_owns_hosts_and_disposes_backend_once(self):
        backend = FakeBackend()
        runtime = WorkspaceRuntime.create(
            identity="workspace-a",
            backend_factory=lambda: backend,
            model_factory=FakeModel,
        )
        runtime.editor_hosts["editor"] = object()
        runtime.shell_hosts["shell"] = object()

        runtime.dispose()
        runtime.dispose()

        self.assertEqual(runtime.identity, "workspace-a")
        self.assertEqual(runtime.model.backend, backend)
        self.assertEqual(runtime.editor_hosts, {})
        self.assertEqual(runtime.shell_hosts, {})
        self.assertEqual(backend.close_count, 1)

    def test_partial_bind_failure_closes_backend_and_does_not_return_runtime(self):
        backend = FakeBackend(fail_bind=True)

        with self.assertRaisesRegex(RuntimeError, "bind failed"):
            WorkspaceRuntime.create(
                identity="workspace-a",
                backend_factory=lambda: backend,
                model_factory=FakeModel,
            )

        self.assertEqual(backend.close_count, 1)

    def test_two_runtimes_keep_native_ownership_distinct_and_share_services(self):
        coordinator = WorkspaceCoordinator()
        documents = object()
        undo = object()
        backends = [FakeBackend(), FakeBackend()]
        identities = [new_identity(IdentityKind.WORKSPACE) for _ in backends]

        runtimes = [
            WorkspaceRuntime.create(
                identity=identity,
                backend_factory=lambda backend=backend: backend,
                model_factory=lambda _backend, identity: WorkspaceModel(
                    CoordinatorBackend(), workspace_id=identity
                ),
                shared_services={"documents": documents, "undo": undo},
            )
            for identity, backend in zip(identities, backends)
        ]

        try:
            ids = [
                coordinator.add_workspace(
                    runtime.model,
                    name=str(runtime.identity),
                    identity=runtime.identity,
                )
                for runtime in runtimes
            ]
            self.assertEqual(ids, identities)
            self.assertIsNot(runtimes[0].backend, runtimes[1].backend)
            self.assertIs(runtimes[0].shared_services["documents"], documents)
            self.assertIs(runtimes[1].shared_services["documents"], documents)
            self.assertIs(runtimes[0].shared_services["undo"], undo)
            self.assertIs(runtimes[1].shared_services["undo"], undo)
            self.assertEqual(coordinator.workspace_ids, tuple(identities))
        finally:
            for runtime in runtimes:
                self.assertTrue(coordinator.remove_workspace(runtime.identity).applied)
                runtime.dispose()

    def test_restore_loaded_isolates_failure_and_retry_reuses_success(self):
        catalog = WorkspaceRuntimeCatalog()
        backends = {identity: FakeBackend() for identity in ("a", "b")}
        attempts = {"b": 0}

        def construct(identity, _record):
            if identity == "b" and attempts[identity] == 0:
                attempts[identity] += 1
                raise RuntimeError("layout restore failed")
            return WorkspaceRuntime.create(
                identity=identity,
                backend_factory=lambda identity=identity: backends[identity],
                model_factory=FakeModel,
            )

        try:
            restored, failures = catalog.restore_loaded(
                {"a": {"identity": "a"}, "b": {"identity": "b"}},
                ("a", "b"),
                construct=construct,
            )
            self.assertEqual(tuple(restored), ("a",))
            self.assertEqual(failures, {"b": "layout restore failed"})
            self.assertEqual(catalog.identities, ("a",))

            retried, failures = catalog.restore_loaded(
                {"a": {"identity": "a"}, "b": {"identity": "b"}},
                ("a", "b"),
                construct=construct,
            )
            self.assertIs(retried["a"], restored["a"])
            self.assertIsNotNone(retried["b"])
            self.assertEqual(failures, {})
            self.assertEqual(catalog.identities, ("a", "b"))
            self.assertEqual(backends["b"].close_count, 0)
        finally:
            catalog.dispose_all()


if __name__ == "__main__":
    unittest.main()
