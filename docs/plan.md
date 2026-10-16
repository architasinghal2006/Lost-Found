# Implementation Plan

Each step is test-first: write the tests, watch them fail, implement, watch them pass, commit.

1. **Scaffold**: `.gitignore`, `requirements.txt`, `pytest.ini`, `tests/conftest.py`.
2. **db.py**: schema, `connect`, `transaction`; triggers. Tests: tables exist, item delete blocked,
   events append-only, rollback on error.
3. **auth.py**: hash/verify, register, login, sessions. Tests: hash not plaintext, wrong password fails,
   duplicate username rejected.
4. **events.py**: `log_event`, `list_events` visibility. Tests: required fields non-null (FR-123), scoping.
5. **transitions.py + scoring.py**: pure logic. Tests: parameterised legal / illegal / unauthorised
   (Appendix B), normalisation, threshold, verdict bands.
6. **embedder.py + search.py**: stub embedder, ranking, re-ranking. Tests: FR-028/031/032/039/041/044/045.
7. **claims.py (part 1)**: private attributes, item budget, open claim, issue/answer, verdict, T7, T11, admin
   decisions. Tests: FR-024/050/052/070-089, FR-064/065.
8. **items.py**: register, withdraw, expiry. Tests: FR-010/011/015/016/021/112.
9. **claims.py (part 2)**: handover, closure core, reopen, expiry of claims. Tests: FR-062/090-116, D-9.
10. **Architecture tests**: rules 1-7 (grep tests, metered-read rollback, INV-2).
11. **main.py + templates + ratelimit.py**: routes. Tests via TestClient: login, admin 403, no answers in any
    page for any role, no budget in claimant view, rate limit 429, full flow.
12. **seed.py**: users, items, PNG placeholders, an aged item.
13. **evaluate.py**: RQ1, RQ2, scale. Tests: metric functions, RQ2 INV-2 assertion. Example CSV.
14. **Docker, README.md, DEMO.md.** Final full test run + manual smoke run of the server.
