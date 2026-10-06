"""Native asynchronous receipts: reject stale results before calling consumers."""

from dataclasses import dataclass

import pyzo


def execution_service():
    return getattr(getattr(pyzo, "application", None), "workspace_execution", None)


def retire_consumer(widget):
    """Reject queued callbacks immediately when a native tool is retired."""
    from pyzo.qt import QtCore

    if widget is not None:
        widget._nativeRequestAlive = False
        for child in widget.findChildren(QtCore.QObject):
            child._nativeRequestAlive = False


def wire_receipt(provenance):
    return {
        name: str(getattr(provenance, name))
        for name in (
            "operation_id",
            "workspace_id",
            "view_id",
            "view_epoch",
            "shell_id",
            "shell_epoch",
            "kernel_generation_id",
            "process_workspace_id",
        )
    } | {"command": provenance.command.value}


@dataclass
class NativeRequest:
    service: object
    provenance: object
    shell: object
    consumer: object
    slot: str
    source: object = None
    source_token: object = None
    cursor: object = None
    guard: object = None

    def current(self, *, latest=True):
        if not self.service.provenance_is_current(self.provenance):
            return False
        if not getattr(self.consumer, "_nativeRequestAlive", True):
            return False
        if latest and getattr(self.consumer, self.slot, None) is not self:
            return False
        if self.source is not None:
            if not self.service.request_source_is_current(
                self.source, self.source_token
            ):
                return False
            documents = getattr(pyzo.application, "documents", None)
            if documents is not None:
                documents.sync(self.source)
                if not self.service.request_source_is_current(
                    self.source, self.source_token
                ):
                    return False
            if self.cursor is not None:
                cursor = self.source.textCursor()
                if (cursor.position(), cursor.anchor()) != self.cursor:
                    return False
        return self.guard is None or self.guard()


def capture(shell, command, consumer, *, source=None, guard=None, slot_suffix=""):
    service = execution_service()
    if service is None:
        return None
    from spikes.peer_frames.execution_routing import ExecutionCommand

    command = ExecutionCommand(command)
    if source is not None:
        result = service.resolve_editor(source, command)
    else:
        result = service.resolve_shell(shell, command)
    if not result.resolved:
        return False
    shell = service._binding_for_identity(result.provenance.shell_id).shell
    source_token = cursor = None
    if source is not None:
        documents = getattr(pyzo.application, "documents", None)
        if documents is not None:
            documents.sync(source)
        source_token = service.request_source_token(source)
        if source_token is None:
            return False
        if hasattr(source, "textCursor"):
            c = source.textCursor()
            cursor = (c.position(), c.anchor())
    slot = "_nativeRequest_" + command.value.replace("-", "_") + slot_suffix
    ticket = NativeRequest(
        service,
        result.provenance,
        shell,
        consumer,
        slot,
        source,
        source_token,
        cursor,
        guard,
    )
    if not hasattr(consumer, "_nativeRequestAlive"):
        consumer._nativeRequestAlive = True
        destroyed = getattr(consumer, "destroyed", None)
        if destroyed is not None:
            destroyed.connect(
                lambda *_: setattr(consumer, "_nativeRequestAlive", False)
            )
    setattr(consumer, slot, ticket)
    return ticket


def prepare_introspection(obj):
    service = execution_service()
    if service is None:
        return True
    from spikes.peer_frames.execution_routing import ExecutionCommand

    source = obj.textCtrl
    shell = source if any(source is shell for shell in service._shells) else None
    guard = None
    if shell is not None and hasattr(shell, "textCursor"):
        cursor = shell.textCursor()
        position = (cursor.position(), cursor.anchor(), shell.document().revision())

        def guard():
            c = shell.textCursor()
            return (c.position(), c.anchor(), shell.document().revision()) == position

    ticket = capture(
        shell,
        ExecutionCommand.COMPLETION,
        source,
        source=None if shell is not None else source,
        guard=guard,
        slot_suffix=type(obj).__name__,
    )
    obj._nativeTicket = ticket
    return ticket is not False


def buffer_is_current(obj, attribute):
    if execution_service() is None:
        return True
    ticket = getattr(obj.textCtrl, attribute, None)
    return bool(ticket and ticket.current(latest=False))


class Reply:
    """Expose the native Future API with the verified envelope removed."""

    def __init__(self, future, ticket):
        self.future, self.ticket = future, ticket

    def cancelled(self):
        return self.future.cancelled()

    def exception(self):
        return self.future.exception()

    def result(self):
        value = self.future.result()
        return value["value"] if self.ticket is not None else value


def request(
    shell,
    command,
    consumer,
    method,
    args,
    callback,
    *,
    source=None,
    guard=None,
    ticket=None,
    attributes=None,
):
    if ticket is None:
        ticket = capture(shell, command, consumer, source=source, guard=guard)
    if ticket is False:
        return None
    if ticket is not None:
        if not ticket.current():
            return None
        shell = ticket.shell
        epoch = shell._nativeTransportEpoch
        receipt = wire_receipt(ticket.provenance)
        future = shell._request.requestWithProvenance(receipt, epoch, method, args)
    else:
        future = getattr(shell._request, method)(*args)
    reply = Reply(future, ticket)
    for name, value in (attributes or {}).items():
        setattr(reply, name, value)

    def done(_):
        if ticket is None and execution_service() is not None:
            return  # a pre-peer compatibility request has no admissible receipt
        if ticket is not None:
            if not ticket.current() or shell._nativeTransportEpoch != epoch:
                return
            if not future.cancelled() and not future.exception():
                value = future.result()
                if (
                    not isinstance(value, dict)
                    or value.get("receipt") != receipt
                    or value.get("epoch") != epoch
                    or "value" not in value
                ):
                    return
        callback(reply)

    future.add_done_callback(done)
    return future


def tool_command(shell, text, *, line=False, code=False, filename=None):
    """Tag tool-triggered execution while preserving current tool selection."""

    def send(target):
        if code:
            target.executeCode(text, filename)
        elif line:
            target.processLine(text)
        else:
            target.executeCommand(text)

    service = execution_service()
    if service is None:
        send(shell)
        return True
    from spikes.peer_frames.execution_routing import ExecutionCommand

    result = service.resolve_shell(shell, ExecutionCommand.TOOL)
    return result.resolved and service.dispatch(result.provenance, send)
