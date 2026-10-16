# The claim transition table from SRS section 3.7, as one dictionary.
# Key: (current state, trigger). Value: (new state, actors allowed to fire it).
# Actors: C claimant, F finder, A admin, S scheduler/system, V verification subsystem.
# Any pair not in this table is forbidden.

TRANSITIONS = {
    (None, "claim_filed"): ("PENDING", {"C"}),                                   # T1
    ("PENDING", "challenge_sent"): ("VERIFYING", {"V"}),                         # T2
    ("VERIFYING", "score_pass"): ("VERIFIED", {"V"}),                            # T3
    ("VERIFYING", "score_fail"): ("REJECTED", {"V"}),                            # T4
    ("VERIFYING", "timeout"): ("EXPIRED", {"S"}),                                # T5
    ("VERIFIED", "handoff_done"): ("CLOSED", {"F", "C"}),                        # T6
    ("VERIFYING", "budget_exhausted"): ("MANUAL_REVIEW", {"V"}),                 # T7
    ("MANUAL_REVIEW", "admin_approve"): ("VERIFIED", {"A"}),                     # T8
    ("MANUAL_REVIEW", "admin_reject"): ("REJECTED", {"A"}),                      # T9
    ("PENDING", "item_resolved_elsewhere"): ("CLOSED_ITEM_RESOLVED", {"S"}),     # T10
    ("VERIFYING", "item_resolved_elsewhere"): ("CLOSED_ITEM_RESOLVED", {"S"}),   # T10
    ("VERIFIED", "item_resolved_elsewhere"): ("CLOSED_ITEM_RESOLVED", {"S"}),    # T10
    ("PENDING", "claimant_withdraws"): ("REJECTED", {"C"}),                      # T11 (OI-03)
    ("VERIFYING", "claimant_withdraws"): ("REJECTED", {"C"}),                    # T11 (OI-03)
    ("VERIFYING", "contested"): ("DISPUTED", {"V", "A"}),                        # T12
    ("DISPUTED", "admin_resolve_approve"): ("VERIFIED", {"A"}),                  # T13
    ("DISPUTED", "admin_resolve_reject"): ("REJECTED", {"A"}),                   # T13
}

STATES = ["PENDING", "VERIFYING", "VERIFIED", "REJECTED", "EXPIRED", "CLOSED",
          "MANUAL_REVIEW", "CLOSED_ITEM_RESOLVED", "DISPUTED"]
TERMINAL = {"REJECTED", "EXPIRED", "CLOSED", "CLOSED_ITEM_RESOLVED"}
TRIGGERS = sorted({trigger for _, trigger in TRANSITIONS})


class IllegalTransition(Exception):
    pass


class NotAuthorised(Exception):
    pass


def next_state(state, trigger, actor):
    if (state, trigger) not in TRANSITIONS:
        raise IllegalTransition(f"'{trigger}' is not allowed from state {state}.")
    new_state, actors = TRANSITIONS[(state, trigger)]
    if actor not in actors:
        raise NotAuthorised(f"Actor {actor} may not fire '{trigger}'.")
    return new_state
