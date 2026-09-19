"""Ownership boundary for one live peer workspace.

The application owns documents, commands, retention, and the workspace
coordinator.  This object owns the replaceable Qt/backend side of one
workspace, including its host lookup tables and construction cleanup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping


class WorkspaceRuntime:
    """A single workspace's backend, model, and native host ownership."""

    def __init__(
        self,
        *,
        identity: Any,
        backend: Any,
        model: Any,
        shared_services: Mapping[str, Any] | None = None,
    ):
        self.identity = identity
        self.backend = backend
        self.model = model
        # Documents, undo authority, and persistence are application-owned.
        # A runtime keeps explicit references to those shared services but
        # never disposes them with its native workspace projection.
        self.shared_services = dict(shared_services or {})
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
        shared_services: Mapping[str, Any] | None = None,
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
        return cls(
            identity=identity,
            backend=backend,
            model=model,
            shared_services=shared_services,
        )

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


class WorkspaceRuntimeCatalog:
    """Construct and dispose several runtime-owned native projections."""

    def __init__(self, *, shared_services: Mapping[str, Any] | None = None):
        self.shared_services = dict(shared_services or {})
        self._runtimes: dict[Any, WorkspaceRuntime] = {}

    @property
    def identities(self) -> tuple[Any, ...]:
        return tuple(self._runtimes)

    def runtime(self, identity: Any) -> WorkspaceRuntime:
        try:
            return self._runtimes[identity]
        except KeyError:
            raise KeyError(f"unknown workspace runtime: {identity}") from None

    def create(self, **kwargs: Any) -> WorkspaceRuntime:
        identity = kwargs["identity"]
        if identity in self._runtimes:
            raise ValueError(f"workspace runtime already exists: {identity}")
        kwargs.setdefault("shared_services", self.shared_services)
        runtime = WorkspaceRuntime.create(**kwargs)
        self._runtimes[identity] = runtime
        return runtime

    def restore_loaded(
        self,
        records: Mapping[Any, Mapping[str, Any]],
        loaded_identities: tuple[Any, ...],
        *,
        construct: Callable[[Any, Mapping[str, Any]], WorkspaceRuntime],
    ) -> tuple[dict[Any, WorkspaceRuntime], dict[Any, str]]:
        """Restore each requested identity, isolating failures for retry.

        ``construct`` owns record-specific model/layout restoration and must
        return a fully bound runtime. Existing identities are reused, so a
        retry cannot duplicate a successfully restored runtime. A failed
        identity is omitted from the catalog and returned with its diagnostic.
        """

        restored: dict[Any, WorkspaceRuntime] = {}
        failures: dict[Any, str] = {}
        for identity in loaded_identities:
            if identity in self._runtimes:
                restored[identity] = self._runtimes[identity]
                continue
            record = records.get(identity)
            if not isinstance(record, Mapping):
                failures[identity] = "workspace record is unavailable"
                continue
            try:
                runtime = construct(identity, record)
            except Exception as error:
                failures[identity] = str(error) or type(error).__name__
                continue
            if runtime.identity != identity:
                runtime.dispose()
                failures[identity] = "constructed runtime identity mismatch"
                continue
            self._runtimes[identity] = runtime
            restored[identity] = runtime
        return restored, failures

    def dispose(self, identity: Any) -> None:
        runtime = self._runtimes.pop(identity, None)
        if runtime is not None:
            runtime.dispose()

    def dispose_all(self) -> None:
        for identity in tuple(reversed(self.identities)):
            self.dispose(identity)
