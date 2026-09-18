from __future__ import annotations

import unittest

from pyzo.core.workspaceRuntime import WorkspaceRuntime


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


if __name__ == "__main__":
    unittest.main()
