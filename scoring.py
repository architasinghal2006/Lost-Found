from difflib import SequenceMatcher

# Volatile thresholds (SRS OI-10, OI-11), to be recalibrated from the RQ2 ROC analysis.
MATCH_THRESHOLD = 0.8
PASS_THRESHOLD = 0.75
REVIEW_THRESHOLD = 0.50


def normalise(text):
    if text is None:
        return ""
    return " ".join(str(text).lower().split())


def similarity(response, truth):
    a, b = normalise(response), normalise(truth)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_match(value):
    return value >= MATCH_THRESHOLD


def score(results):
    # results: list of (weight, matched) where matched is True, False or None (unanswered = wrong).
    asked = sum(weight for weight, _ in results)
    matched = sum(weight for weight, ok in results if ok)
    return matched / asked if asked else 0.0


def verdict_trigger(value):
    if value >= PASS_THRESHOLD:
        return "score_pass"   # T3 -> VERIFIED
    if value >= REVIEW_THRESHOLD:
        return "contested"    # T12 -> DISPUTED, an admin decides
    return "score_fail"       # T4 -> REJECTED
