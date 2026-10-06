"""Native reply delivery, source lifetime and wire-identity assertions."""

from types import SimpleNamespace

import pytest
import pyzo
from pyzo.core.nativeRequests import capture, request, wire_receipt, buffer_is_current
from spikes.peer_frames.execution_routing import ExecutionCommand
from test_workspace_execution import world as execution_world  # noqa: F401


class Consumer:
    pass


class Future:
    def __init__(self, value, immediate=False, failure=None):
        self.value, self.immediate, self.failure = value, immediate, failure
        self.callbacks = []

    def cancelled(self):
        return self.failure == "cancelled"

    def exception(self):
        return RuntimeError("failed") if self.failure == "exception" else None

    def result(self):
        return self.value

    def add_done_callback(self, callback):
        self.callbacks.append(callback)
        if self.immediate:
            callback(self)

    def deliver(self):
        for callback in self.callbacks:
            callback(self)


class Channel:
    def __init__(self):
        self.futures, self.calls = [], []
        self.immediate = False
        self.failure = None

    def requestWithProvenance(self, receipt, epoch, method, args):
        self.calls.append((receipt, epoch, method, args))
        future = Future(
            dict(receipt=receipt, epoch=epoch, value=args), self.immediate, self.failure
        )
        self.futures.append(future)
        return future


@pytest.fixture
def native(execution_world, monkeypatch):  # noqa: F811 - pytest fixture injection
    w = execution_world
    monkeypatch.setattr(
        pyzo,
        "application",
        SimpleNamespace(workspace_execution=w.service),
        raising=False,
    )
    for shell in w.shells:
        shell._nativeTransportEpoch = "transport-first"
        shell._request = Channel()
    w.service.foreground_shell(w.shells[0])
    w.consumer, w.values = Consumer(), []
    return w


def issue(w, *, source=True, command=ExecutionCommand.COMPLETION):
    return request(
        w.shells[0],
        command,
        w.consumer,
        "dir",
        ["object"],
        lambda reply: w.values.append(reply.result()),
        source=w.editors[0] if source else None,
    )


def test_wire_echo_and_immediate_callback_metadata_precedes_delivery(native):
    w = native
    w.shells[0]._request.immediate = True
    seen = []
    request(
        w.shells[0],
        ExecutionCommand.HELP,
        w.consumer,
        "doc",
        ["len"],
        lambda reply: seen.append((reply.cto, reply.result())),
        source=w.editors[0],
        attributes={"cto": "captured"},
    )
    assert seen == [("captured", ["len"])]
    receipt, epoch, method, args = w.shells[0]._request.calls[0]
    assert set(receipt) == {
        "operation_id",
        "command",
        "workspace_id",
        "view_id",
        "view_epoch",
        "shell_id",
        "shell_epoch",
        "kernel_generation_id",
        "process_workspace_id",
    }
    assert receipt["workspace_id"] == receipt["process_workspace_id"] == str(w.a)
    assert receipt["view_id"] == str(w.editors[0]._peerViewIdentity)
    assert (
        receipt["command"] == "help" and epoch == "transport-first" and method == "doc"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "view-close",
        "view-reopen",
        "view-move",
        "doc-edit",
        "retired",
        "assign",
        "foreground",
        "restart",
        "loss",
        "context",
        "owner",
        "transport",
        "consumer",
        "superseded",
    ],
)
def test_delayed_native_result_has_zero_consumer_writes_after_drift(native, mutation):
    w = native
    f = issue(w)
    view = w.editors[0]._peerViewIdentity
    if mutation.startswith("view-"):
        if mutation == "view-move":
            w.docs.move_view(view, w.b)
        else:
            w.docs.close_view(view)
            if mutation == "view-reopen":
                w.docs.reopen_view(w.editors[0]._peerDocumentIdentity, view_id=view)
    elif mutation == "doc-edit":
        w.docs.replace_text(view, "changed")
    elif mutation == "retired":
        w.retired.append(w.editors[0])
    elif mutation == "assign":
        w.service.assign_selection(w.editors[0], w.shells[1])
    elif mutation == "foreground":
        w.service.foreground_shell(w.shells[1])
    elif mutation == "restart":
        w.shells[0]._version = ""
        assert not w.service.provenance_is_current(
            getattr(w.consumer, "_nativeRequest_completion").provenance
        )
        w.shells[0]._version = "3.12"
    elif mutation == "loss":
        w.shells[0]._context.connection_count = 0
    elif mutation == "context":
        w.shells[0]._context = SimpleNamespace(connection_count=1)
    elif mutation == "owner":
        w.workspaces.pane_owner = lambda _: w.b
    elif mutation == "transport":
        w.shells[0]._nativeTransportEpoch = "transport-second"
    elif mutation == "consumer":
        w.consumer._nativeRequestAlive = False
    elif mutation == "superseded":
        issue(w)
    f.deliver()
    assert w.values == []


@pytest.mark.parametrize("field", ["receipt", "epoch", "value", "shape"])
def test_malformed_or_mismatched_native_envelope_is_not_consumed(native, field):
    w = native
    f = issue(w)
    if field == "shape":
        f.value = ["wrong"]
    elif field == "value":
        del f.value[field]
    else:
        f.value[field] = "wrong"
    f.deliver()
    assert w.values == []


@pytest.mark.parametrize("failure", ["cancelled", "exception"])
def test_native_failures_keep_future_api_and_do_not_read_result(native, failure):
    w = native
    w.shells[0]._request.failure = failure
    seen = []
    request(
        w.shells[0],
        ExecutionCommand.TOOL,
        w.consumer,
        "dir2",
        [""],
        lambda reply: seen.append((reply.cancelled(), bool(reply.exception()))),
    )
    w.shells[0]._request.futures[0].deliver()
    assert seen == [(failure == "cancelled", failure == "exception")]


def test_latest_query_wins_reverse_order_and_foreign_focus_does_not_redirect(native):
    w = native
    old = issue(w)
    current = issue(w)
    w.service.foreground_shell(w.shells[2])
    current.deliver()
    old.deliver()
    assert w.values == [["object"]]
    assert not w.shells[2]._request.calls


def test_explicit_and_direct_requests_do_not_depend_on_foreground(native):
    w = native
    w.service.assign_selection(w.editors[0], w.shells[0])
    explicit = issue(w)
    direct = issue(w, source=False, command=ExecutionCommand.TOOL)
    w.service.foreground_shell(w.shells[1])
    explicit.deliver()
    direct.deliver()
    assert w.values == [["object"], ["object"]]


def test_cache_cannot_survive_target_or_document_revision_change(native):
    w = native
    ticket = capture(
        w.shells[0], ExecutionCommand.COMPLETION, w.consumer, source=w.editors[0]
    )
    w.consumer._cached = ticket
    obj = SimpleNamespace(textCtrl=w.consumer)
    assert buffer_is_current(obj, "_cached")
    w.docs.replace_text(w.editors[0]._peerViewIdentity, "changed")
    assert not buffer_is_current(obj, "_cached")


def test_dispatch_carries_exact_operation_and_restores_context_even_on_failure(native):
    w = native
    receipt = w.service.resolve_selection(w.editors[0]).provenance
    previous = object()
    w.shells[0]._workspaceOperation = previous

    def send(shell):
        assert shell._workspaceOperation is receipt
        assert wire_receipt(receipt)["operation_id"] == str(receipt.operation_id)
        raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        w.service.dispatch(receipt, send)
    assert w.shells[0]._workspaceOperation is previous


def test_restart_completed_between_synchronizations_still_changes_model_generation(
    native,
):
    w = native
    old = w.service.resolve_selection(w.editors[0]).provenance
    w.shells[0]._nativeTransportEpoch = "complete-restart-between-checks"
    fresh = w.service.resolve_selection(w.editors[0]).provenance
    assert fresh.shell_id == old.shell_id
    assert fresh.kernel_generation_id != old.kernel_generation_id
    assert not w.service.provenance_is_current(old)


def test_source_cursor_and_query_guard_invalidate_before_reply_or_send(native):
    w = native
    position = [10]
    w.editors[0].textCursor = lambda: SimpleNamespace(
        position=lambda: position[0], anchor=lambda: position[0]
    )
    ticket = capture(w.shells[0], "help", w.consumer, source=w.editors[0])
    position[0] = 11
    assert (
        request(
            w.shells[0],
            "help",
            w.consumer,
            "doc",
            ["object"],
            lambda _: w.values.append("bad"),
            ticket=ticket,
        )
        is None
    )
    assert not w.shells[0]._request.calls
    f = request(
        w.shells[0],
        "tool",
        w.consumer,
        "dir2",
        [""],
        lambda _: w.values.append("bad"),
        guard=lambda: position[0] == 11,
    )
    position[0] = 12
    f.deliver()
    assert not w.values


def test_tool_retirement_rejects_before_deferred_qt_deletion(native):
    from pyzo.qt import QtWidgets
    from pyzo.core.nativeRequests import retire_consumer

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent, child = QtWidgets.QWidget(), QtWidgets.QWidget()
    child.setParent(parent)
    w = native
    f = request(
        w.shells[0], "tool", child, "dir2", [""], lambda _: w.values.append("bad")
    )
    retire_consumer(parent)
    f.deliver()
    assert not w.values
    parent.deleteLater()
    app.sendPostedEvents()


def test_legacy_immediate_delivery_preserves_raw_future_result(monkeypatch):
    monkeypatch.setattr(pyzo, "application", None, raising=False)
    future = Future("raw legacy", immediate=True)
    shell = SimpleNamespace(_request=SimpleNamespace(doc=lambda name: future))
    seen = []
    request(
        shell,
        "help",
        Consumer(),
        "doc",
        ["len"],
        lambda reply: seen.append((reply.name, reply.result())),
        attributes={"name": "len"},
    )
    assert seen == [("len", "raw legacy")]


def test_retired_startup_channel_and_epoch_do_not_touch_native_widget(monkeypatch):
    from pyzo.qt import QtWidgets
    from pyzo.util._locale import translate

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(pyzo, "translate", translate, raising=False)
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(
            pyzo,
            "config",
            SimpleNamespace(advanced=SimpleNamespace(shellMaxLines=1000)),
            raising=False,
        )
    from pyzo.core.shell import PythonShell

    current = SimpleNamespace(
        recv=lambda: {"version": (3, 12), "nativeTransportEpoch": "old"}
    )
    shell = SimpleNamespace(_stat_startup=current, _nativeTransportEpoch="new")
    PythonShell._onReceivedStartupInfo(shell, object())
    PythonShell._onReceivedStartupInfo(shell, current)
    assert not hasattr(shell, "_startup_info")
    app.sendPostedEvents()


def test_native_run_transport_echo_recycle_and_malformed_receipts(native, monkeypatch):
    from types import MethodType
    from pyzo.qt import QtWidgets
    from pyzo.util._locale import translate

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(pyzo, "translate", translate, raising=False)
    if not hasattr(pyzo, "config"):
        monkeypatch.setattr(
            pyzo,
            "config",
            SimpleNamespace(advanced=SimpleNamespace(shellMaxLines=1000)),
            raising=False,
        )
    from pyzo.core.shell import PythonShell

    w, sends = native, []
    shell = w.shells[0]
    shell._pendingWorkspaceOperations, shell._workspaceResults = {}, []
    shell._ctrl_operation = SimpleNamespace(send=sends.append)
    for name in (
        "_rememberWorkspaceOperation",
        "_sendWorkspaceOperation",
        "_acceptWorkspaceMessage",
        "_consumeWorkspaceResult",
    ):
        setattr(shell, name, MethodType(getattr(PythonShell, name), shell))
    token = w.service.resolve_selection(w.editors[0]).provenance
    assert w.service.dispatch(
        token,
        lambda target: target._sendWorkspaceOperation("code", {"source": "value = 42"}),
    )
    assert sends[0]["receipt"] == wire_receipt(token)
    echo = dict(receipt=sends[0]["receipt"], epoch=sends[0]["epoch"], phase="complete")
    assert shell._consumeWorkspaceResult(echo)
    for bad in (
        {},
        {**echo, "receipt": None},
        {**echo, "receipt": "bad"},
        {**echo, "phase": "bad"},
        {**echo, "epoch": "retired"},
        {**echo, "receipt": {**echo["receipt"], "operation_id": "unknown"}},
    ):
        assert not shell._consumeWorkspaceResult(bad)
    w.service.foreground_shell(w.shells[1])
    assert not shell._consumeWorkspaceResult(echo)
    w.service.foreground_shell(shell)
    shell._nativeTransportEpoch = "script-successor"
    receipt = shell._rememberWorkspaceOperation(token, recycle=True)
    startup = dict(receipt=receipt, epoch="script-successor", phase="startup")
    assert shell._consumeWorkspaceResult(startup)
    successor = shell._workspaceResults[-1]["successor_generation_id"]
    assert successor != str(token.kernel_generation_id)
    shell._nativeTransportEpoch = "later-unrelated-restart"
    assert not shell._consumeWorkspaceResult(startup)
    app.sendPostedEvents()
