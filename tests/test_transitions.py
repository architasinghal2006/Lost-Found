import itertools

import pytest

import transitions as t

ALL_ACTORS = ["C", "F", "A", "S", "V"]
LEGAL = [(state, trigger, new, actor)
         for (state, trigger), (new, actors) in t.TRANSITIONS.items()
         for actor in sorted(actors)]
ILLEGAL = [(state, trigger)
           for state, trigger in itertools.product([None] + t.STATES, t.TRIGGERS)
           if (state, trigger) not in t.TRANSITIONS]
UNAUTHORISED = [(state, trigger, actor)
                for (state, trigger), (_, actors) in t.TRANSITIONS.items()
                for actor in ALL_ACTORS if actor not in actors]


def test_table_matches_srs_rows():
    # T10 covers three source states, T11 two, and T13 is split into approve/reject.
    assert len(t.TRANSITIONS) == 17


@pytest.mark.parametrize("state, trigger, new, actor", LEGAL)
def test_every_legal_transition_succeeds(state, trigger, new, actor):
    assert t.next_state(state, trigger, actor) == new


@pytest.mark.parametrize("state, trigger", ILLEGAL)
def test_every_undeclared_transition_raises(state, trigger):
    with pytest.raises(t.IllegalTransition):
        t.next_state(state, trigger, "A")


@pytest.mark.parametrize("state, trigger, actor", UNAUTHORISED)
def test_every_unauthorised_actor_is_rejected(state, trigger, actor):
    with pytest.raises(t.NotAuthorised):
        t.next_state(state, trigger, actor)


def test_terminal_states_have_no_exit():
    for state in t.TERMINAL:
        assert not [k for k in t.TRANSITIONS if k[0] == state]


def test_no_path_from_pending_straight_to_verified_or_closed():
    targets = {new for (state, _), (new, _) in t.TRANSITIONS.items() if state == "PENDING"}
    assert "VERIFIED" not in targets and "CLOSED" not in targets
