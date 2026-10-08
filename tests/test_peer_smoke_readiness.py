"""Smoke seed dispatch waits for actual native readiness, not version receipt."""

from types import SimpleNamespace

import pytest
from pyzo.core.main import MainWindow


@pytest.mark.parametrize('position', ('first_shell', 'second_shell'))
@pytest.mark.parametrize('version', ('', '3.12'))
@pytest.mark.parametrize('status', ('', 'Starting', 'Ready', 'More', 'Busy', 'Debug', 'Dead'))
@pytest.mark.parametrize('connected', (0, 1))
def test_affinity_seed_waits_for_both_ready_shells(position, version, status, connected):
    sends = []
    def shell(label):
        return SimpleNamespace(
            _version='3.12', _state='Ready',
            _context=SimpleNamespace(connection_count=1),
            executeCommand=lambda command: sends.append((label, command)),
        )
    first, second = shell('first'), shell('second')
    state = dict(phase='wait-affinity-shell', shell=first,
                 first_shell=first, second_shell=second,
                 cwd_directory=SimpleNamespace(name='/test-owned-cwd'))
    selected = state[position]
    selected._version, selected._state = version, status
    selected._context.connection_count = connected
    host = SimpleNamespace(_peerRunDebugSmoke=state)
    assert MainWindow._testPeerRunAndLiveDebugTick(host) is False
    ready = bool(version and status == 'Ready' and connected)
    assert state['phase'] == ('wait-affinity-seed' if ready else 'wait-affinity-shell')
    assert len(sends) == (2 if ready else 0)
    if ready:
        assert [label for label, _ in sends] == ['first', 'second']
        assert "peer_kernel_identity = 'first'" in sends[0][1]
        assert '/test-owned-cwd' in sends[1][1]
