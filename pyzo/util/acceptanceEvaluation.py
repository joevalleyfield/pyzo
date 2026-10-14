"""Keep an acceptance evaluation pending instead of confusing timeout with None."""

class EvaluationPending(Exception):
    pass


class EvaluationPoller:
    def __init__(self, timeout_error=TimeoutError):
        self.timeout_error = timeout_error
        self.phase = None
        self.pending = {}
        self.completed = {}

    def value(self, shell, expression, phase, *, label=None):
        if phase != self.phase:
            self.phase = phase
            self.pending.clear()
            self.completed.clear()
        key = (shell, id(shell._context), getattr(shell, "_nativeTransportEpoch", None), expression, label)
        if key in self.completed:
            return self.completed[key]
        future = self.pending.get(key)
        if future is None:
            future = self.pending[key] = shell._request.eval(expression)
        try:
            result = future.result(0.2)
        except self.timeout_error:
            print("PROBE EVALUATION PENDING", phase, expression, flush=True)
            raise EvaluationPending from None
        self.pending.pop(key)
        # A completed None can precede command delivery (e.g. a name not yet set).
        # Retry it, while preserving False/zero as actual assertion values.
        if result is not None and not (isinstance(result, str) and result.startswith("Error evaluating:")):
            self.completed[key] = result
        print("PROBE EVALUATION RESULT", phase, expression, repr(result), flush=True)
        return result

    def finish_tick(self):
        # A completed but not-yet-updated namespace must be queried next tick.
        # Only a pending interruption retains earlier batch results for retry.
        self.completed.clear()
