"""A resumed peer's current view membership overrides historical retention."""

from types import SimpleNamespace
from copy import deepcopy

import pyzo
from pyzo.util._locale import translate

pyzo.translate = translate
from pyzo.core.main import MainWindow


def test_foreign_resume_and_close_replace_membership_in_both_directions():
    main = SimpleNamespace(_peerWorkspaceIdentity="bootstrap")
    entry = dict(workspace_id="peer", document_id="shared")
    previous = dict(schema=1, views={}, retained_views={"view": entry}, documents={})
    current = dict(schema=1, views={"view": entry}, retained_views={}, documents={})
    before = deepcopy((previous, current))
    resumed = MainWindow._mergePeerResourcePersistence(main, previous, current)
    assert resumed["views"] == {"view": entry} and not resumed["retained_views"]
    assert (previous, current) == before
    closed = MainWindow._mergePeerResourcePersistence(main, resumed, previous)
    assert not closed["views"] and closed["retained_views"] == {"view": entry}
