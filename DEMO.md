# Live demo script (about 10 minutes)

Every outcome below was rehearsed against a freshly seeded database. Run the steps in this order, because each
step depends on the state the previous one left behind.

## Before you present (15 minutes earlier)

1. `python -m pytest -q`: expect **357 passed**. Leave this terminal open as evidence.
2. `python seed.py`, then `uvicorn main:app --host 0.0.0.0 --port 8000`. Note the "On your phone" address.
3. Open that address on your phone. If it doesn't load, go to the backup plan at the bottom **now**, not
   during the talk.
4. Logins are per browser, so use a separate browser or profile per person:

   | Where | Logged in as |
   |---|---|
   | Phone | `finder` / finder123 |
   | Laptop, Chrome | `owner` / owner123 |
   | Laptop, Chrome incognito | `mallory` / mallory123, later `trudy` / trudy123 |
   | Laptop, Edge (or Firefox) | `admin` / admin123 |

5. Optional fallback copies: `python evaluate.py rq2 > results\rq2.txt` and `python evaluate.py rq1`.
6. Find a small object to photograph live, such as a pen or a water bottle.

## Answer sheet (keep this off-screen)

| Item | Questions asked, in this order | Answer to type |
|---|---|---|
| 4: keys (owner) | What animal is the keyring charm? / What is written on the car key fob? / How many keys are there? | `bird` / `Honda` / `3` |
| 1: dark drinks bottle (mallory) | sticker on the bottom? / inside of the lid? / how many litres? | `my name` / `black` / `2` (all wrong) |
| 1: (trudy) | same three questions | anything wrong, e.g. `no idea` |
| 6: red bag (optional) | What is in the front pocket? / What brand is the bag? | `blue pen` / `Nike` (half right) |

---

## Step 1: Start and log in as each user (0:00–1:30)

**Do:**
- Show the uvicorn terminal with the printed LAN address.
- On the phone, show `finder` is logged in. On the laptop, show `owner`, `mallory` and `admin` in their browsers.
- In the owner's browser, type `/admin` into the address bar to show the 403.

**Say:**
- "This is a server-rendered FastAPI app with SQLite. It runs on my laptop, and my phone reaches it over the
  local network."
- "There are two roles, user and admin. Finder and claimant are not roles; they depend on what you did."
- "The admin link is hidden for normal users, but hiding isn't security. The server checks the role on every
  admin route, and here is the 403."

**SRS:** NFR-SEC-002 (authorisation enforced at the request layer), §3.4 (contextual roles).

## Step 2: Report an item from the phone (1:30–3:00)

**Do:**
- On the phone (as finder), tap **Report item**, then the photo field. The camera opens; photograph your object.
- Fill in a description, category, zone (e.g. `lecture_hall`) and today's date.
- Scroll to the yellow **Private details** box, add one or two questions and answers, and submit.

**Say:**
- "Public fields are deliberately vague: enough to recognise the item, not enough to prove you own it."
- "The private box is visually separate and explains why we collect it. Once submitted, nobody can read it back:
  not the finder, not the admin."
- "Registration fails without a photo, category, zone and date. The photo is embedded at once, and the
  embedding is stored with the item. That store, D1, is what makes it findable. The private answers go into a
  different table, D2, and only one module can read it."
- Point at the timeline: "Every state change is an append-only event, written in the same transaction."

**SRS:** UI-001, NFR-USE-002, FR-011, FR-014, FR-016, FR-120 / FR-133. Architecture rule 1 (two disjoint stores).

> With the default stub embedder, a real phone photo gets a meaningless vector, so don't search for it. Real
> photo understanding needs `LF_USE_CLIP=1` (README).

## Step 3: Search with re-ranking off, then on (3:00–4:30)

**Do:**
1. On the laptop home page, search `silver keyring with a little enamel bird on it`. The keys come first.
2. Search `black water bottle` with category `bottle`, where lost `gym`, date lost `2026-10-11` and
   **Re-ranking: Off**. The gym bottle ("Black flask") is **#3**.
3. Change only **Re-ranking** to **On** and search again. "Black flask" jumps to **#1**.

**Say:**
- "This is the description gap from the SRS. The finder wrote 'metal keys, 3, with blue and orange charm'; the owner
  writes 'silver keyring with a little enamel bird'. We match the owner's words against the photo, not against
  the finder's words."
- "But embeddings know category, not identity: three black bottles look the same. Where and when it was found
  isn't in the photo at all, so we add small bonuses for category, zone and a 14-day date window. It's a soft
  prior, never a filter, because owners misremember."
- "Off is the semantic-only baseline that RQ1 compares against. Results show public fields and rank only, never
  scores."
- Be honest: "Right now this runs a deterministic stub embedder so the demo needs no model download; real CLIP
  switches on with an environment variable."

**SRS:** FR-030–FR-032, FR-041, FR-042, FR-044, FR-045, UI-004, OI-08 (soft prior), RQ1.

## Step 4: A genuine owner passes verification (4:30–5:45)

**Do:**
- In the owner's browser, open the keys (item 4), click **Claim this item**, then **Start verification**.
- Answer `bird`, then `Honda` (capital H on purpose), then `3`.
- The page shows **Ownership verified**.

**Say:**
- "Each question comes from a private detail, highest weight first. The weight is also the price: this one
  cost 5 points from my 10-point claim budget and from the item's 20-point budget, charged when it's asked,
  not when I answer. Asking leaks information."
- "Notice there's no progress bar, no 'question 2 of 3', no budget shown. All of that would leak too."
- "Answers are compared with difflib after lowercasing, so 'Honda' matches 'honda'. Similarity 0.8 or more
  counts. The score is the matched weight over the asked weight: 10 out of 10 here, and 0.75 or more is
  verified."
- "The log keeps the similarity score, never the correct answer."

**SRS:** FR-070, FR-073, FR-074, FR-077–FR-079, FR-082, FR-083, UI-010, FR-089. Rule 3 (metered read).

## Step 5: A fraudster is rejected and the budget runs out (5:45–7:00)

**Do:**
1. In the incognito window as `mallory`, open **Dark drinks bottle** (item 1). Claim it and start verification,
   then answer `my name`, `black`, `2`. The page says **Claim not accepted**.
2. Log out and log in as `trudy`. Claim the same bottle and answer anything. Also **Claim not accepted**.
3. In the owner's browser, open the same bottle and click **Claim this item**: *"Claims on this item are paused
   while an administrator reviews it."*

**Say:**
- "Mallory only knows the public listing. She is told the outcome, but not which answers were wrong and not
  the score, because that would teach her the answers."
- "Her budget is spent and isn't refunded. She can't retry, because terminal states have no way out."
- "Trudy is the sequential-claimant attack: a new identity with a fresh 10-point claim budget. But the item's
  budget is cumulative and never resets. Two claims used its 20 points, so automated claiming has stopped for
  everyone. Total disclosure for this item is capped for its whole lifetime."

**SRS:** NFR-USE-005, FR-085, FR-086, FR-087, NFR-SEC-005, NFR-PRIV-004, INV-2, §3.7 terminal states.

## Step 6: Two-sided handover closes the item (7:00–8:00)

**Do:**
1. In the owner's browser, on the verified keys claim, tick **I have received my item** and **Confirm handover**.
   The page shows *"Waiting for the finder to confirm."*
2. On the phone (as finder), open **My items**, then the keys. Under *Claims on this item*, tick the box and
   **Confirm handover**. The item shows **Closed: Returned to owner**.
3. Optionally press back and submit the confirm again; nothing changes.
4. On the laptop, search `silver keyring with a little enamel bird on it` again. The keys are gone.

**Say:**
- "Closing by one party is the main fraud route: an impostor marks it received and the real owner never sees
  it. So both sides must confirm. An admin may stand in for the finder, but never for the claimant."
- "Closing is one guarded update, `UPDATE items ... WHERE id=? AND status='active'`, and we check the row
  count. A second close changes nothing and logs nothing."
- "In the same transaction, other open claims are closed, both budgets are frozen, and the event is written.
  Closed items are filtered out before scoring, so they cost nothing."

**SRS:** FR-104, UI-015, FR-090, FR-094, FR-097, FR-098, FR-101, FR-062, FR-028. Rule 6 (guarded updates). Rule 5:
the row is kept with `status='resolved'`, not deleted.

## Step 7: The timeline (8:00–8:45)

**Do:**
- On the phone (finder) or in the admin browser, scroll down item 4 to the **Timeline**. It shows registered,
  claim opened, each question asked with its cost and similarity, verified, both confirmations, closed.
- Open the same item in a logged-out window. The timeline shows only *Item registered* and *Item closed*.
- Open the yellow umbrella (`/items/13`): **Closed: Expired**, by `scheduler`.

**Say:**
- "The event table is append-only, enforced by database triggers as well as code. You can rebuild an item's
  whole history, including every disclosure, from it."
- "What you see depends on who you are. A stranger never learns that a claim existed."
- "The umbrella was seeded as 40 days old. The startup sweep expired it: that's the third closure path. An item
  with a claim under verification is never expired."

**SRS:** FR-120, FR-123, NFR-AUD-002, NFR-AUD-004, UI-016, NFR-SEC-004, FR-112. Rule 7 (append-only events).

## Step 8: The admin view shows verdicts, not answers (8:45–9:30)

**Do:**
- In the admin browser, open **Admin**. Point at *All claims*: states, scores, each question with cost,
  similarity and ✔/✘.
- Under *Items and budgets*: Dark drinks bottle at **20 / 20**, keys at 10 / 20 and **frozen at 10**.
- *Optional, if ahead of time:* as owner, claim the **Red bag** (item 6) and answer `blue pen`, then `Nike`. It
  says *Under review*. In Admin it appears under **Waiting for a decision** as DISPUTED (score 0.56). Enter a
  justification and **Approve**. Without a justification it's refused.

**Say:**
- "The admin sees everything needed to adjudicate, but never a ground-truth value. The verification module only
  lets verdicts out."
- "Borderline scores, 0.50 to 0.75, are never auto-approved or auto-rejected. A human decides, and must say why."
- "Reopening is admin-only too, and it restores the frozen budget value, never a fresh allowance."

**SRS:** Rule 4 (one-way crossing), NFR-SEC-003, FR-064, FR-065, FR-113, FR-116.

## Step 9: Run the evaluation (9:30–10:30)

**Do:** In the terminal, run `python evaluate.py rq2` (about 10 seconds), then `python evaluate.py rq1`.

**Say (RQ2):**
- "Simulated claimants who know 100% down to 0% of the details go through the same budget and scoring code.
  Owners who know everything pass every time; opportunists almost never do."
- "Here's the ROC table and the threshold it suggests. Here's the false-rejection rate for real owners, which is
  the cost side, and I report it as prominently as the discrimination."
- "And the sequential-claimant simulation: ten identities against one item, disclosure stops at 20 of 20, and
  INV-2 holds."

**Say (RQ1):**
- "Three conditions, lexical, semantic and semantic plus re-rank, with Recall at 1, 5 and 10, and MRR. Every
  query's rank is saved, including any query that re-ranking made worse."
- "These numbers come from the stub on 19 example queries. They prove the pipeline works, not the research
  claim. The real figures need my 60-object, 180-description set with real CLIP."

**SRS:** Appendix B (RQ1, RQ2), FR-046, A-1, INV-2. Close by pointing at the pytest terminal: 357 tests,
including one per architecture rule.

---

## Backup plans

**The venue Wi-Fi blocks phone-to-laptop traffic** (very common: "client isolation").
1. On the phone, turn on its **mobile hotspot**.
2. Connect the laptop to that hotspot.
3. In the uvicorn terminal press `Ctrl+C`, then run `uvicorn main:app --host 0.0.0.0 --port 8000` again. It prints the
   new address on the hotspot network.
4. Open that address on the phone. The phone's own browser can reach the laptop over its hotspot.
5. If it still doesn't load, Windows has probably marked the hotspot network as **Public**. Either switch it to
   Private (Settings → Network & internet → Wi-Fi → the hotspot → Private), or in an admin PowerShell run:

   ```powershell
   New-NetFirewallRule -DisplayName "Lost and Found demo" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Public
   ```

   Remove the rule after the demo: `Remove-NetFirewallRule -DisplayName "Lost and Found demo"`.

Rehearse this at home once; it takes about a minute.

**The phone can't connect at all.**
- Do step 2 on the laptop: press F12, then Ctrl+Shift+M (device mode), and upload a photo file instead.
- Say that on a phone the same field opens the camera (`capture="environment"`).

**Something goes wrong mid-demo.**
- `Ctrl+C`, then `python seed.py` (the old DB is kept as `.bak`), then restart uvicorn. That takes about 5 seconds.
- Because steps depend on each other, restart from step 3.

**The evaluation won't run.** Show the saved `results\rq2.txt` and `results\rq1_per_query.csv`.
