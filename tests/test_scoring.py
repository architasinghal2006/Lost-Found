import pytest

import scoring


def test_normalise_lowercases_and_collapses_whitespace():
    assert scoring.normalise("  Blue   Bird\n") == "blue bird"


def test_near_match_tolerates_case_and_typos():
    # FR-077
    assert scoring.is_match(scoring.similarity("Blu Bird", "blue bird"))
    assert scoring.is_match(scoring.similarity("BLUE BIRD", "blue bird"))


def test_different_answer_does_not_match():
    assert not scoring.is_match(scoring.similarity("red fish", "blue bird"))


@pytest.mark.parametrize("bad", ["", "   ", None, 12345])
def test_empty_or_odd_input_is_non_matching_not_an_error(bad):
    # FR-081
    assert not scoring.is_match(scoring.similarity(bad, "blue bird"))


def test_score_is_matched_weight_over_issued_weight():
    # FR-078: unanswered (None) counts as wrong
    assert scoring.score([(5, True), (3, False), (2, None)]) == 0.5


@pytest.mark.parametrize("value, trigger", [
    (1.0, "score_pass"), (0.75, "score_pass"),
    (0.74, "contested"), (0.5, "contested"),
    (0.49, "score_fail"), (0.0, "score_fail"),
])
def test_verdict_bands(value, trigger):
    # FR-079, FR-064
    assert scoring.verdict_trigger(value) == trigger
