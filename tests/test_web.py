import os
import re

import pytest
from fastapi.testclient import TestClient

import db
import main
from helpers import make_user, png_bytes

SECRET_ANSWERS = ["zebra7731", "quokka4402", "narwhal9915"]
FORM = {
    "description": "Grey backpack with a broken zip", "category": "bag", "zone": "gym", "date_found": "2026-10-01",
    "label": ["Name on the tag?", "What is in the front pocket?", "Keyring shape?"],
    "answer": SECRET_ANSWERS, "weight": ["5", "4", "1"],
}


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LF_DB", str(tmp_path / "test.db"))
    conn = db.connect()
    db.init_db(conn)
    for name in ("finder", "owner", "fraud"):
        make_user(conn, name)
    make_user(conn, "admin", role="admin")
    conn.close()
    main.claim_limiter.calls.clear()
    main.answer_limiter.calls.clear()


def client_for(name):
    client = TestClient(main.app)
    if name:
        response = client.post("/login", data={"username": name, "password": "secret123"})
        assert response.status_code == 200
    return client


def report_item(client):
    response = client.post("/items", data=FORM, files={"photo": ("bag.png", png_bytes("grey backpack"), "image/png")})
    assert response.status_code == 200
    return int(re.search(r"/items/(\d+)$", str(response.url)).group(1))


def run_claim(client, item_id, answers):
    response = client.post(f"/items/{item_id}/claim")
    claim_id = int(re.search(r"/claims/(\d+)$", str(response.url)).group(1))
    page = client.post(f"/claims/{claim_id}/start").text
    while 'name="challenge_id"' in page:
        challenge_id = re.search(r'name="challenge_id" value="(\d+)"', page).group(1)
        question = re.search(r'<p class="question">(.*?)</p>', page).group(1)
        page = client.post(f"/claims/{claim_id}/answer",
                           data={"challenge_id": challenge_id, "answer": answers.get(question, "no idea")}).text
    return claim_id, page


def truth():
    return dict(zip(FORM["label"], FORM["answer"]))


def test_home_page_loads_for_anonymous_visitor(app_db):
    assert client_for(None).get("/").status_code == 200


def test_reporting_requires_login(app_db):
    response = client_for(None).get("/items/new", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_admin_page_is_enforced_on_the_server(app_db):
    # NFR-SEC-002
    assert client_for("owner").get("/admin").status_code == 403
    assert client_for("admin").get("/admin").status_code == 200


def test_search_finds_reported_item(app_db):
    item_id = report_item(client_for("finder"))
    page = client_for(None).get("/", params={"q": "grey backpack", "rerank": "on"}).text
    assert f"/items/{item_id}" in page


def test_missing_photo_is_rejected(app_db):
    response = client_for("finder").post("/items", data=FORM)
    assert response.status_code == 400


def test_answers_never_appear_on_any_page_for_any_role(app_db):
    # Rule 4, FR-014, NFR-SEC-003
    finder, owner, fraud, admin = (client_for(n) for n in ("finder", "owner", "fraud", "admin"))
    item_id = report_item(finder)
    fraud_claim, fraud_page = run_claim(fraud, item_id, {})
    owner_claim, owner_page = run_claim(owner, item_id, truth())
    assert "Ownership verified" in owner_page
    assert "Claim not accepted" in fraud_page
    pages = [fraud_page, owner_page]
    for client in (finder, owner, fraud, admin):
        for url in ("/", f"/items/{item_id}", "/my/claims", "/my/items", "/admin",
                    f"/claims/{owner_claim}", f"/claims/{fraud_claim}"):
            pages.append(client.get(url).text)
    pages.append(client_for(None).get(f"/items/{item_id}").text)
    for page in pages:
        for secret in SECRET_ANSWERS:
            assert secret not in page


def test_admin_sees_verdicts_and_scores(app_db):
    finder, owner, admin = client_for("finder"), client_for("owner"), client_for("admin")
    item_id = report_item(finder)
    run_claim(owner, item_id, truth())
    page = admin.get("/admin").text
    assert "VERIFIED" in page and "1.00" in page


def test_claimant_page_shows_no_budget_or_progress(app_db):
    # UI-010, FR-089
    item_id = report_item(client_for("finder"))
    owner = client_for("owner")
    response = owner.post(f"/items/{item_id}/claim")
    page = owner.post(response.url.path + "/start").text.lower()
    assert 'name="challenge_id"' in page
    for word in ("budget", "remaining", "of 3", "question 1", "score", "weight"):
        assert word not in page


def test_other_claimants_claims_are_invisible(app_db):
    # NFR-SEC-004
    item_id = report_item(client_for("finder"))
    owner_claim, _ = run_claim(client_for("owner"), item_id, truth())
    fraud = client_for("fraud")
    assert fraud.get(f"/claims/{owner_claim}").status_code == 404
    page = fraud.get(f"/items/{item_id}").text
    assert 'class="actor">owner<' not in page and "Claims on this item" not in page


def test_two_sided_handover_closes_item_and_shows_timeline(app_db):
    finder, owner = client_for("finder"), client_for("owner")
    item_id = report_item(finder)
    claim_id, _ = run_claim(owner, item_id, truth())
    page = owner.post(f"/claims/{claim_id}/confirm", data={"confirm": "yes"}).text
    assert "Waiting for the finder" in page
    finder.post(f"/claims/{claim_id}/confirm", data={"confirm": "yes"})
    page = client_for(None).get(f"/items/{item_id}").text
    assert "Returned to owner" in page or "HANDED_OVER" in page
    assert "Item registered" in page


def test_close_needs_reason_and_confirmation(app_db):
    # UI-013, FR-093, section 3.1.4
    finder = client_for("finder")
    item_id = report_item(finder)
    assert finder.post(f"/items/{item_id}/close", data={"reason": "WITHDRAWN"}).status_code == 400
    assert finder.post(f"/items/{item_id}/close", data={"confirm": "yes"}).status_code == 400
    response = finder.post(f"/items/{item_id}/close", data={"reason": "WITHDRAWN", "confirm": "yes"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/items/{item_id}"


def test_claimant_cannot_close_and_finder_cannot_reopen(app_db):
    finder, owner = client_for("finder"), client_for("owner")
    item_id = report_item(finder)
    assert owner.post(f"/items/{item_id}/close", data={"reason": "WITHDRAWN", "confirm": "yes"}).status_code == 400
    finder.post(f"/items/{item_id}/close", data={"reason": "WITHDRAWN", "confirm": "yes"})
    assert finder.post(f"/items/{item_id}/reopen", data={"justification": "oops"}).status_code == 403


def test_claim_endpoint_is_rate_limited(app_db):
    finder, fraud = client_for("finder"), client_for("fraud")
    ids = [report_item(finder) for _ in range(main.claim_limiter.max_calls + 1)]
    codes = [fraud.post(f"/items/{i}/claim").status_code for i in ids]
    assert codes[-1] == 429 and all(c == 200 for c in codes[:-1])


def test_stylesheet_link_carries_file_mtime_version(app_db, tmp_path, monkeypatch):
    # Cache-busting: the ?v= value is the CSS file's modification time, so editing the file changes the URL.
    css = tmp_path / "style.css"
    css.write_text("body {}")
    monkeypatch.setattr(main, "STYLESHEET", str(css))
    os.utime(css, (1_700_000_000, 1_700_000_000))
    assert '/static/style.css?v=1700000000"' in client_for(None).get("/login").text
    os.utime(css, (1_700_000_500, 1_700_000_500))
    assert '/static/style.css?v=1700000500"' in client_for(None).get("/login").text
