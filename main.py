import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import claim_views
import claims
import db
import embedder
import events
import items
import search
from ratelimit import RateLimiter
from transitions import IllegalTransition, NotAuthorised

templates = Jinja2Templates(directory="templates")
claim_limiter = RateLimiter(max_calls=5, per_seconds=60)
answer_limiter = RateLimiter(max_calls=20, per_seconds=60)

EVENT_TEXT = {
    "item_registered": "Item registered", "claim_filed": "Claim opened",
    "challenge_sent": "Verification started", "challenge_issued": "Question asked",
    "challenge_answered": "Answer scored", "verification_scored": "Verification scored",
    "score_pass": "Claim verified", "score_fail": "Claim rejected", "timeout": "Claim timed out",
    "contested": "Borderline result, sent to an administrator",
    "budget_exhausted": "Item budget exhausted, sent to an administrator",
    "admin_approve": "Administrator approved the claim", "admin_reject": "Administrator rejected the claim",
    "admin_resolve_approve": "Administrator approved the claim",
    "admin_resolve_reject": "Administrator rejected the claim",
    "admin_justification": "Administrator's justification",
    "claimant_withdraws": "Claimant withdrew", "handover_confirmed": "Handover confirmed",
    "handoff_done": "Claim completed by handover", "item_resolved_elsewhere": "Claim closed (item resolved)",
    "item_closed": "Item closed", "item_reopened": "Item reopened", "attribute_replaced": "Private detail replaced",
}
REASON_TEXT = {"HANDED_OVER": "Returned to owner", "WITHDRAWN": "Withdrawn by finder", "EXPIRED": "Expired"}
templates.env.globals.update(EVENT_TEXT=EVENT_TEXT, REASON_TEXT=REASON_TEXT, CATEGORIES=items.CATEGORIES,
                             ZONES=items.ZONES)


def lan_address():
    # Connecting a UDP socket sends no packet; it only makes the OS pick the outgoing network interface.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    db.init_db(conn)
    expired = items.expire_items(conn)  # scheduler check at startup
    conn.close()
    model = embedder.get_embedder()  # load the model now, not on the first request
    port = os.environ.get("LF_PORT", "8000")
    print(f"\n  Lost & Found is running ({model.name} embedder). Expired at startup: {expired}", flush=True)
    print(f"  On this laptop:  http://localhost:{port}", flush=True)
    print(f"  On your phone:   http://{lan_address()}:{port}  (same Wi-Fi or hotspot)\n", flush=True)
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
os.makedirs(items.upload_dir(), exist_ok=True)
app.mount("/uploads", StaticFiles(directory=items.upload_dir()), name="uploads")


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

class LoginRequired(Exception):
    pass


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)):
    return auth.get_user(conn, request.cookies.get("session"))


def require_user(user=Depends(current_user)):
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user=Depends(require_user)):
    # NFR-SEC-002: checked on the server for every admin route, not just hidden in the menu.
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrators only.")
    return user


def render(request, name, user, status_code=200, **context):
    return templates.TemplateResponse(request, name, {"user": user, **context}, status_code=status_code)


def redirect(url):
    return RedirectResponse(url, status_code=303)


@app.exception_handler(LoginRequired)
def login_required(request, exc):
    return redirect(f"/login?next={request.url.path}")


@app.exception_handler(claims.ClaimError)
@app.exception_handler(items.ItemError)
@app.exception_handler(IllegalTransition)
def domain_error(request, exc):
    return render(request, "error.html", None, 400, message=str(exc))


@app.exception_handler(NotAuthorised)
def not_authorised(request, exc):
    return render(request, "error.html", None, 403, message=str(exc))


@app.exception_handler(HTTPException)
def http_error(request, exc):
    return render(request, "error.html", None, exc.status_code, message=exc.detail)


def check_rate(limiter, user):
    if not limiter.allow(user["id"]):
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait a minute and try again.")


# ---------------------------------------------------------------------------
# Public pages and accounts
# ---------------------------------------------------------------------------

@app.get("/")
def home(request: Request, q: str = "", category: str = "", zone: str = "", date_lost: str = "",
         rerank: str = "", user=Depends(current_user), conn=Depends(get_conn)):
    results, low_confidence = [], False
    if q.strip():
        ranked, low_confidence = search.search(conn, embedder.get_embedder(), q, rerank=rerank == "on",
                                               category=category or None, zone=zone or None,
                                               date_lost=date_lost or None)
        results = [items.get_item(conn, r["item_id"]) for r in ranked[:10]]
    recent = conn.execute("SELECT * FROM items WHERE status='active' ORDER BY id DESC LIMIT 12").fetchall()
    return render(request, "home.html", user, q=q, category=category, zone=zone, date_lost=date_lost,
                  rerank=rerank, results=results, low_confidence=low_confidence, recent=recent)


@app.get("/login")
def login_page(request: Request, next: str = "/", user=Depends(current_user)):
    return render(request, "login.html", user, next=next, error=None)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/"),
          conn=Depends(get_conn)):
    try:
        token = auth.login(conn, username, password)
    except auth.AuthError as error:
        return render(request, "login.html", None, 400, next=next, error=str(error))
    response = redirect(next if next.startswith("/") else "/")
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.get("/register")
def register_page(request: Request, user=Depends(current_user)):
    return render(request, "register.html", user, error=None)


@app.post("/register")
def register(request: Request, username: str = Form(""), password: str = Form(""), conn=Depends(get_conn)):
    try:
        auth.register(conn, username, password)
        token = auth.login(conn, username, password)
    except auth.AuthError as error:
        return render(request, "register.html", None, 400, error=str(error))
    response = redirect("/")
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request, conn=Depends(get_conn)):
    auth.logout(conn, request.cookies.get("session", ""))
    response = redirect("/")
    response.delete_cookie("session")
    return response


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@app.get("/items/new")
def new_item_page(request: Request, user=Depends(require_user)):
    return render(request, "item_new.html", user, error=None, form={})


@app.post("/items")
def create_item(request: Request, photo: UploadFile = File(None), description: str = Form(""),
                category: str = Form(""), zone: str = Form(""), date_found: str = Form(""),
                label: list[str] = Form([]), answer: list[str] = Form([]), weight: list[str] = Form([]),
                user=Depends(require_user), conn=Depends(get_conn)):
    photo_bytes = photo.file.read() if photo is not None else b""
    try:
        item_id = items.register_item(conn, embedder.get_embedder(), user, photo_bytes, description, category,
                                      zone, date_found, list(zip(label, answer, weight)))
    except items.ItemError as error:
        form = {"description": description, "category": category, "zone": zone, "date_found": date_found}
        return render(request, "item_new.html", user, 400, error=str(error), form=form)
    return redirect(f"/items/{item_id}")


@app.get("/items/{item_id}")
def item_page(request: Request, item_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    item = items.get_item(conn, item_id)
    is_finder = user is not None and user["id"] == item["finder_id"]
    is_staff = user is not None and (is_finder or user["role"] == "admin")
    context = {
        "item": item, "is_finder": is_finder, "is_staff": is_staff,
        "timeline": events.list_events(conn, item_id, user),
        "staff_claims": claim_views.claims_for_staff(conn, item_id) if is_staff else [],
        "attributes": claims.attribute_lock_status(conn, item_id) if is_finder else [],
        "budget": claim_views.item_budget(conn, item_id) if is_staff else None,
        "my_claim": claim_views.my_open_claim(conn, item_id, user) if user and not is_staff else None,
    }
    return render(request, "item.html", user, **context)


@app.post("/items/{item_id}/claim")
def open_claim(item_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    check_rate(claim_limiter, user)
    claim_id = claims.open_claim(conn, item_id, user)
    return redirect(f"/claims/{claim_id}")


@app.post("/items/{item_id}/close")
def close_item(item_id: int, reason: str = Form(""), confirm: str = Form(""),
               user=Depends(require_user), conn=Depends(get_conn)):
    # SRS 3.1.4: finder or admin, reason code required, redirect to the item view.
    if confirm != "yes":
        raise items.ItemError("Tick the confirmation box to close this item.")
    items.withdraw_item(conn, item_id, user, reason)
    return redirect(f"/items/{item_id}")


@app.post("/items/{item_id}/attributes/{attribute_id}")
def replace_attribute(item_id: int, attribute_id: int, label: str = Form(""), answer: str = Form(""),
                      weight: str = Form(""), user=Depends(require_user), conn=Depends(get_conn)):
    label, answer, weight = items.clean_attributes([(label, answer, weight)])[0]
    claims.replace_attribute(conn, item_id, attribute_id, user, label, answer, weight)
    return redirect(f"/items/{item_id}")


@app.post("/items/{item_id}/reopen")
def reopen_item(item_id: int, justification: str = Form(""), user=Depends(require_admin), conn=Depends(get_conn)):
    claims.reopen_item(conn, item_id, user, justification)
    return redirect(f"/items/{item_id}")


@app.get("/my/items")
def my_items(request: Request, user=Depends(require_user), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM items WHERE finder_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    return render(request, "my_items.html", user, items=rows)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

@app.get("/claims/{claim_id}")
def claim_page(request: Request, claim_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if claim is None:
        raise HTTPException(status_code=404, detail="No such claim.")
    if claim["claimant_id"] == user["id"]:
        view = claim_views.claimant_view(conn, claim_id, user)
        return render(request, "claim.html", user, claim=view, item=items.get_item(conn, claim["item_id"]))
    finder_id = items.get_item(conn, claim["item_id"])["finder_id"]
    if user["role"] == "admin" or user["id"] == finder_id:
        return redirect(f"/items/{claim['item_id']}#claims")
    raise HTTPException(status_code=404, detail="No such claim.")  # NFR-SEC-004: don't reveal it exists


@app.post("/claims/{claim_id}/start")
def start_claim(claim_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    claims.start_verification(conn, claim_id, user)
    return redirect(f"/claims/{claim_id}")


@app.post("/claims/{claim_id}/answer")
def answer(claim_id: int, challenge_id: int = Form(...), answer: str = Form(""),
           user=Depends(require_user), conn=Depends(get_conn)):
    check_rate(answer_limiter, user)
    claims.answer_question(conn, claim_id, challenge_id, user, answer)
    return redirect(f"/claims/{claim_id}")


@app.post("/claims/{claim_id}/withdraw")
def withdraw_claim(claim_id: int, confirm: str = Form(""), user=Depends(require_user), conn=Depends(get_conn)):
    if confirm != "yes":
        raise claims.ClaimError("Tick the confirmation box to withdraw your claim.")
    claims.withdraw_claim(conn, claim_id, user)
    return redirect(f"/claims/{claim_id}")


@app.post("/claims/{claim_id}/confirm")
def confirm_handover(claim_id: int, confirm: str = Form(""), user=Depends(require_user), conn=Depends(get_conn)):
    if confirm != "yes":
        raise claims.ClaimError("Tick the box to confirm the item has changed hands.")
    claims.confirm_handover(conn, claim_id, user)
    claim = conn.execute("SELECT claimant_id, item_id FROM claims WHERE id=?", (claim_id,)).fetchone()
    return redirect(f"/claims/{claim_id}" if claim["claimant_id"] == user["id"] else f"/items/{claim['item_id']}")


@app.get("/my/claims")
def my_claims(request: Request, user=Depends(require_user), conn=Depends(get_conn)):
    return render(request, "my_claims.html", user, claims=claim_views.my_claims(conn, user))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_page(request: Request, swept: str = "", user=Depends(require_admin), conn=Depends(get_conn)):
    all_claims = claim_views.claims_for_staff(conn)
    queue = [c for c in all_claims if c["state"] in claims.ADMIN_REVIEW_STATES]
    item_rows = conn.execute(
        "SELECT items.*, budgets.consumed_total, budgets.allowance, budgets.frozen, budgets.frozen_value"
        " FROM items JOIN budgets ON budgets.item_id = items.id ORDER BY items.id DESC").fetchall()
    return render(request, "admin.html", user, queue=queue, claims=all_claims, items=item_rows, swept=swept)


@app.post("/admin/claims/{claim_id}/decide")
def admin_decide(claim_id: int, decision: str = Form(""), justification: str = Form(""),
                 user=Depends(require_admin), conn=Depends(get_conn)):
    claims.admin_decide(conn, claim_id, user, decision == "approve", justification)
    return redirect("/admin")


@app.post("/admin/sweep")
def admin_sweep(user=Depends(require_admin), conn=Depends(get_conn)):
    expired = items.expire_items(conn)
    return redirect(f"/admin?swept={len(expired)}")
