"""Ownership boundary for one live peer workspace.

The application owns documents, commands, retention, and the workspace
coordinator.  This object owns the replaceable Qt/backend side of one
workspace, including its host lookup tables and construction cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class WorkspaceRuntime:
    """A single workspace's backend, model, and native host ownership."""

    def __init__(self, *, identity: Any, backend: Any, model: Any):
        self.identity = identity
        self.backend = backend
        self.model = model
        self.editor_hosts: dict[Any, Any] = {}
        self.shell_hosts: dict[Any, Any] = {}
        self.editor_host_serial = 0
        self.shell_host_serial = 0
        self._disposed = False

    @classmethod
    def create(
        cls,
        *,
        identity: Any,
        backend_factory: Callable[[], Any],
        model_factory: Callable[[Any, Any], Any],
        frame_close_request: Callable[..., Any] | None = None,
        pane_close_request: Callable[..., Any] | None = None,
    ) -> "WorkspaceRuntime":
        """Construct and bind a runtime, cleaning a partial backend on error."""

        backend = backend_factory()
        try:
            model = model_factory(backend, identity)
            backend.bind_model(model, frame_close_request, pane_close_request)
        except Exception:
            close = getattr(backend, "close", None)
            if close is not None:
                close()
            raise
        return cls(identity=identity, backend=backend, model=model)

    @property
    def frame_windows(self):
        """Native frame lookup owned by this runtime."""

        return self.backend.frame_windows

    def dispose(self) -> None:
        """Release runtime-owned native state without touching shared services."""

        if self._disposed:
            return
        self._disposed = True
        self.editor_hosts.clear()
        self.shell_hosts.clear()
        self.backend.close()

