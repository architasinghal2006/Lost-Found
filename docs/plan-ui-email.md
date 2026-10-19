# Plan: UI redesign and email alerts

Source: the "Lost & Found: UI redesign and email alerts" brief. Order: **Part 3 (SRS amendments) → Part 2
(email, test-first) → Part 1 (UI)**. Email goes before the UI because it changes the users table and the login
and register forms, which the new pages are built around.

Decisions taken with the user:
- Fonts are self-hosted in `static/fonts/` (both OFL) and loaded with `@font-face`, so they work on a hotspot
  with no internet. There is no Google Fonts link.
- The register field is labelled **Username** (`autocomplete="username"`) and stored in the existing unique
  `users.username` column. Login is by **email** + password.

## Step 0. Clear the superseded work

- The uncommitted lime / dark-mode / tab-bar redesign is replaced by this brief.
- Delete `static/style.css`, `static/icons/sprite.svg` (Lucide), `templates/_ui.html` and `screenshots/after/`.
- Keep the `.gitignore` rule. Keep `manifest.json` and the app icons, redrawn in the new palette (dark
  square, yellow tag).
- Templates are rewritten in Part 1.

## Step 1. SRS amendments (Part 3), no code

The SRS is a PDF, so I can't edit it in place. Instead:

1. **`docs/srs-amendments.md`** holds the exact replacement text for you to paste into the SRS source:
   - §1.2, §3.1.4, §3.4 (ten entities: EmailOutbox; `email` on User) and §2.5
   - new clause **3.2.6 Notification**, FR-140 to FR-146
   - **NFR-PRIV-006** in §3.6, plus the threat-note paragraph after NFR-USE-005
   - **OI-33** and **OI-34** in Appendix A
2. **ID check.**
   - The SRS uses NFR-PRIV-001, -004 and -005, and FR-133 is its highest FR.
   - Appendix A says the companion specification (R-5) holds OI-01 to OI-32, so OI-33 and OI-34 are safe.
   - **The companion spec is not in this folder.** I cannot confirm that FR-140 to FR-146 and NFR-PRIV-006 are
     free in it. The amendments file marks this as a check you must do.
3. **`docs/spec.md`**: add §11 "Notification" (outbox, templates, content rules) and a §9 decision entry.
   Update §3 (data model) and §10 (out of scope: SMS, opt-out OI-33, password reset OI-34).

## Step 2. Email alerts (Part 2), test-first

### 2a. Data
- `users`: add `email TEXT NOT NULL UNIQUE`. It is normalised to lower case.
- New table `email_outbox`:
  - columns: `id, user_id, template, item_id, claim_id NULL, event_id, status
    CHECK(pending|sent|failed), attempts, created_at, sent_at`
  - `UNIQUE(event_id, user_id, template)`
  - a `BEFORE DELETE` trigger that aborts, so rows are never deleted (FR-146), the same as `item_events`
- `events.log_event` returns the new event id. Callers that ignore it are unaffected.
- **An existing `lostfound.db` has no email column, so run `python seed.py` again.** The README will say so.

### 2b. `auth.py`
- `register(conn, username, email, password)` checks:
  - the email has a simple `x@y.z` shape;
  - the email is unique: "An account with that email already exists.";
  - the username is unique.
- `login(conn, email, password)`. Error: "Wrong email or password."
- Every call site is updated: `tests/helpers.py`, `seed.py`, `evaluate.py` and `main.py`.
- Seed emails: `admin@campus.test`, `finder@campus.test` and so on. Passwords are unchanged.

### 2c. `notify.py` (queueing, inside the caller's transaction)
- `queue(conn, user_id, template, item_id, claim_id, event_id)` uses `INSERT OR IGNORE`. The unique key makes a
  repeated request a no-op (FR-143).
- `on_claim_move(conn, claim, trigger, new_state, event_id)` is called from `claims._move()` after
  `log_event`. Triggers map to emails like this:

  | Transition | Email | Recipient |
  |---|---|---|
  | T2 `challenge_sent` | 1 `questions_waiting` | claimant |
  | T4, T5, T9, T13 to REJECTED | 2 `claim_update` | claimant |
  | T3, T8, T13 to VERIFIED | 3 `confirm_handover` | finder + claimant |
  | T10 `item_resolved_elsewhere` | 4 `item_unavailable` | that claimant |
  | T7, T12 | 6 `needs_review` | every admin |

  - T11 (withdraw) and T6 (handover done) send nothing to the claimant.
  - T1 sends nothing.
- `on_item_closed(conn, item_id, event_id)` is called from `close_item_in_tx()` after the `item_closed` event.
  It sends **email 5** to the finder. This covers all three paths: returned, withdrawn and expired.
- Because both hooks run inside the existing `db.transaction`, a rollback drops the outbox row too (FR-141).

### 2d. `mailer.py` (delivery, always outside a transaction)
- `render(template, item, login_url)` returns `(subject, text, html)`.
  - It takes **only** the public item fields `public_description`, `zone` and `date_found`. It is never given a
    claim, a score or a user list, so it cannot leak them.
  - The HTML version is the same sentences wrapped in `<p>` tags.
  - The one link is `{BASE_URL}/login`.
- `MAIL_BACKEND` chooses the backend: `console` (default), `file` (`outbox/*.eml`) or `smtp` (`smtplib` +
  STARTTLS). The SMTP settings come from `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` and `MAIL_FROM`.
  `BASE_URL` defaults to the printed LAN address.
- `send_pending(conn, backend=None, limit=50)`:
  - loads pending rows and sends each one;
  - on success, sets `status='sent'` and `sent_at`;
  - on an exception, does `attempts += 1`, and sets `status='failed'` once attempts reach 5.
  - Each row update is its own short transaction.
- Called from:
  - `BackgroundTasks` in `main.py`, after any POST route that can queue mail (claim, start, answer, decide,
    confirm, close, sweep), so it runs after the response is sent;
  - the expiry sweep, at startup and from `/admin/sweep`.

### 2e. Tests, written first (`tests/test_notify.py`, `tests/test_mailer.py`)
- **Triggers:**
  - each row of the table above queues exactly one row per recipient, with the right template;
  - withdraw queues nothing;
  - a verified claimant gets email 3 and not email 2.
- **Same transaction:** monkeypatch to raise after `_move` or `close_item_in_tx`, then assert there are no
  outbox rows and the state is unchanged.
- **Delivery failure:** a fake backend that raises. The state change stays committed, the row stays `pending`
  with `attempts=1`, and after 5 failures it is `failed`.
- **Idempotency:** repeating a closure request, or calling `queue` twice, gives one row.
- **Never deleted:** `DELETE FROM email_outbox` raises.
- **Content scan:** render every template with fixtures containing private answers, question text and a
  score (`0.83`). Assert none appear in the subject, text or HTML.
- **No budget figures:** with the item fields fixed, the only digits in an email come from the date and the
  item title.
- **Email 4:** it contains no other claimant's username and no closure-reason text ("Returned", "Withdrawn",
  "Expired", "HANDED_OVER" and so on).
- **Links:** every `http` URL in every email equals the login URL.
- **Plain text and HTML match:** stripping the tags from the HTML gives the same words as the text version.
- **Web:** registering without an email fails, and logging in by email works.

## Step 3. UI (Part 1)

### 3a. Foundations
- `static/css/site.css` holds the brief's tokens verbatim, light theme only.
  - No `prefers-color-scheme` rules.
  - Nothing smaller than 16px.
  - Touch targets at least 44px; main buttons and inputs 52 to 56px.
- `static/fonts/` holds Bricolage Grotesque 700 and Instrument Sans 400, 500 and 600 (woff2, OFL licence
  included).
- `templates/_icons.html` holds inline stroke SVG macros with `currentColor` and stroke width 2: search, tag,
  camera, map-pin, calendar, eye, eye-off, lock, check, x, chevron, log-out and so on. There is no emoji
  anywhere: the current 🔒 is removed.
- `base.html`:
  - the header has the logo mark (a 36px dark square with a yellow tag) and the nav:
    - logged out: Browse found items, How claiming works, Log in, Register;
    - logged in: Browse found items, My claims, My reports, Report a found item, the user's name and Log out;
  - there is no bell;
  - the footer has the two 16px lines;
  - at phone width the nav wraps into a stacked menu, using `<details>` with no JavaScript.

### 3b. Pages from the mockup
- **Home:**
  - the hero h1 and the search box;
  - the filter row as three 48px pill buttons, Where, When and What. Each is a `<details>` that opens a
    native select or date input, so no JavaScript is needed. The note reads "Optional. Used to rank results.";
  - three "Try:" chips, which are plain links to `/?q=...`;
  - the dark "How claiming works" panel with `id="how"` and four yellow numbered steps;
  - "Handed in recently": a 260px-minimum card grid with a 190px photo, a yellow category tag, the title in
    sentence case and "5 Oct 2026" dates;
  - the "Found something?" band.
- **"When (Last 30 days)" needs a small backend change.** The server takes a single `date_lost` for re-ranking.
  The When pill offers Any time, Today, Last 7 days and Last 30 days (the default), plus a "pick a date"
  input:
  - a preset maps to a `date_lost` value: the midpoint of the window, done in `main.py`;
  - an exact date is passed straight through;
  - re-ranking stays a soft prior, and OI-08 is left open as the brief asks.
- **New `GET /items` "Browse found items" page:** every active item in the same card grid. "See all found
  items" links here.
- **Log in and Register:** two columns, the form (520px maximum) next to a dark panel, stacking on a phone.
  - Log in has an Email field and a Password field with a show/hide `<button aria-label>`. That button is the
    only JavaScript, about 5 lines.
  - The header button switches to "Create an account" on Log in and "Log in" on Register.
  - There is no "Forgot password" link (OI-34) and no role picker.
  - The password hint under the field says "At least 6 characters", the current rule. It is marked in the
    template to revisit once OI-19 is settled.

### 3c. Remaining pages, same tokens
- **Search results:** the card grid ranked 1 to 10, plus the low-confidence notice.
- **Item view:**
  - the photo and the public fields;
  - the claim button;
  - the timeline, with an icon per event;
  - for staff only: the claims list, the budget figure and the private-detail lock status.
  - There is no gauge, because the brief doesn't ask for one.
- **Claim and challenge screen:**
  - the plain-language state, the next step and one question card;
  - **no stepper, progress bar or counts** (UI-010). The 4-step bar from the previous round is removed.
- **Report form:** the public card is white. The private section is clearly different: a dark-bordered,
  tinted panel with a lock icon headed "Private details: never shown to anyone". It also has a "Why we ask"
  paragraph (UI-001, NFR-USE-002).
- **Admin:** a queue of cards and readable tables, which stack at phone width.
- **My claims and My reports:** card lists with friendly empty states.
- **Error page:** a friendly message with Go back and Home buttons.

### 3d. Checks
- The existing web tests stay green: no private answers on any page, and no budget, progress or counts on the
  claimant page.
- New web tests:
  - the CSS file contains no `font-size` below 16px;
  - no page contains `prefers-color-scheme`, emoji or a Google Fonts URL;
  - icon-only buttons have an `aria-label`;
  - `/items` lists only active items.
- Playwright (Edge) screenshots at 375px and 1280px go into `screenshots/after/`. I compare them with the brief
  and fix what's off.

## Step 4. Wrap-up
- Update README (email env vars, re-seed note, login by email) and DEMO.md (logins become emails; show
  `MAIL_BACKEND=file` and the `outbox/` folder in the demo).
- Add `outbox/` to `.gitignore`.
- Run the full suite and commit each part separately: amendments + spec, email, UI.

## Not covered by this plan
- **The canvas mockup and the "Alert emails" artboard:** I don't have access to them. I'm working from the
  brief's text. If you export them to `design-ref/`, I'll match them.
- **The superpowers skills (writing-plans, test-driven-development) aren't installed in this session.** I'm
  following the same method by hand: this written plan first, then tests that fail before each piece of code.
- **Real SMTP delivery:** I can test it against a fake backend only, unless you give me relay details.
