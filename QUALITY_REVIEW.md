# X-Summit Bikes — Code Quality & Security Review

**Scope:** `supply_chain_strategy_game_v2` (Django app) — models, services, views, forms, urls, templates, migrations, seed command, and project configuration. Reviewed by static/manual analysis (no automated Django test run was possible in this environment — see *Environment limitation* at the end).

**Bottom line:** The codebase is solid on the things that are easy to get wrong in a multi-tenant Django app — CSRF coverage, template escaping, ORM-only queries (no SQL injection), authorization scoping in nearly every view, decimal (not float) money fields, and defensive division-by-zero handling. The real risks are (1) a few authorization/business-logic gaps that only bite in unusual paths, (2) two scoring-logic bugs that could silently under-grade or over-reward students, and (3) the complete absence of a validated `.clean()`/transaction safety net for a couple of state-mutating flows. There is no automated test suite, so none of this has ever been caught by CI — this review is effectively the first one this code has had.

---

## Critical / High severity

**1. Cross-game data integrity is enforced only in `Model.clean()`, which Django never calls automatically.**
`models.py` — `YearAttempt.clean()` (695-729), `AnnualPlan.clean()` (1187-1328), `MonthlyDecision.clean()` (1416-1463), `ProductDecision.clean()` (1615-1751), `PlayerYearScore.clean()` (1848-1895) all guard the core invariant that `membership.game_id == scenario_year.game_id` (i.e., a player's decision row can't point at a different instructor's game/year), plus price floors and "fixed-cost-share sums to 100%" rules. `.save()` never calls `full_clean()`, and nothing in `services.py` calls it either for these models. Nothing at the DB or ORM-call level currently stops a bug elsewhere in the codebase from writing a `MonthlyDecision` that mixes a Game‑A membership with a Game‑B scenario year. Recommend calling `full_clean()` (or the specific validators) before every `.save()` of these models, or adding DB-level `CheckConstraint`s where feasible.

**2. `GameMembership.user` cascades on user deletion — deleting an account wipes graded history.**
`models.py:247-251` — `user = models.ForeignKey(..., on_delete=models.CASCADE)`. Deleting a `User` (account cleanup, duplicate-account merge, GDPR-style request) cascades through `GameMembership → YearAttempt → AnnualPlan/MonthlyDecision → ProductDecision → PlayerYearScore`, silently erasing a student's entire simulation and grade history with no confirmation step. `Game.created_by` correctly uses `PROTECT` for the same reason (131-135); `GameMembership.user` should too, or use a soft-delete flag.

**3. `total_operating_cost` / `contribution` model properties disagree with the official score calculation.**
`models.py:1599-1612` includes `holding_cost` in per-product operating cost/contribution. `services.py` (`refresh_year_score()` ~3696-3705, `year_end_review_analysis()` ~4309-4313) deliberately *excludes* holding cost from the same calculation, with a comment explaining it's tracked separately. Any screen that renders the model property will show a different (lower) contribution number than the one actually used for grading — two independently-maintained formulas for the same concept that have drifted apart. Pick one source of truth (ideally have the model property call the same helper `services.py` uses) and delete the other.

**4. `run_month` finalize is not race-safe against double submission.**
`services.py:3023-3026` reads `is_finalized` as a plain attribute before doing the expensive month-close computation; the `select_for_update()` lock only comes later (~line 3143) on `product_decisions`. Two near-simultaneous finalize requests (double-click on a slow connection, a retried POST) can both pass the initial guard, and the second re-runs fixed-cost absorption from a stale `remaining_fixed_cost_budget`, silently overwriting correct COGS/absorption numbers with wrong ones — no error is raised, no duplicate row is created (the unique constraint prevents that), just quietly wrong data. Move the `is_finalized` check inside the same `select_for_update()`-protected block, or lock the `MonthlyDecision` row itself at the top of the function.

**5. Any user in the system can be force-enrolled into any instructor's game.**
`forms.py:1328` — `AddPlayerForm.user` uses an unrestricted `get_user_model().objects.all()` queryset. Because the `add_player` view (`views.py:3696`) is behind `_admin_or_403` only (any instructor with a game), an instructor can pick *any* username in the system — including another instructor's login — from the add-player dropdown and add them as a player to their own game, with no invite/consent step. Compare with `player_invite_signup`, which correctly excludes instructor accounts. Scope the queryset (e.g., to users without an `InstructorProfile`, or require the invite-link flow exclusively).

---

## Medium severity

**6. Deleting a `Game` or `ScenarioYear` cascades away all player data with no safety net.** `ScenarioYear.game`, `YearAttempt.scenario_year`, `AnnualPlan.scenario_year`, `MonthlyDecision.scenario_year`, `PlayerYearScore.scenario_year` are all `CASCADE`. One accidental deletion (e.g., via `/admin/`) permanently wipes every player's grades for that game. If "delete a game" is a real feature, add a confirmation/export step in the view layer since the model offers none.

**7. `YearAttempt.is_current` isn't coupled to `status`.** (`models.py:620-693`) Nothing stops an attempt marked `COMPLETE`/`ABANDONED` from staying `is_current=True`, or all attempts for a year being `is_current=False`. `GameMembership.current_attempt_for_year()` trusts this flag blindly — a bug in the "redo year" flow that forgets to flip the flag would silently point live gameplay at a stale attempt.

**8. Stockout penalty is effectively negligible.** `services.py:3747-3752` — `service_penalty = lost_sales_units * Decimal("0.02")`. On the 0–100+ point scoring scale, losing 500 units of stockout costs only ~10 points. Given "service performance" is called out as a core learning objective in the README, this hard-coded, unnamed constant appears to undercut that intent rather than enforce it.

**9. Unnamed magic-number inventory penalty.** `services.py:3754-3757` — `ending_inventory_value / Decimal("100000")`, an unexplained scaling constant (contrast with the named `YEAR_END_REVENUE_TOLERANCE` elsewhere in the same file). Both #8 and #9 should become named, documented, instructor-configurable constants (ideally on `GameSettings`) so the grading weights can be audited and defended to students.

**10. `sales_credit_per_unit` has no upper clamp despite a name implying one.** `services.py:585-632` — credit is `max(0, price - minimum_price)`, uncapped, and the code comment confirms this is intentional. At the 1.5× price ceiling, credit-per-unit is ~3.5× what "maximum_credit" suggests, letting a player hit sales-credit targets simply by pricing high — the opposite of the mechanic's stated purpose as a discount incentive. Confirm this is intended; if not, add the missing cap.

**11. Year‑2 budget options are formula-derived, not tied to actual Year‑2 forecast data.** `services.py:2118-2316` applies a hardcoded `1.85` "market expansion multiplier" to Year‑1 numbers for the budget choices shown to players, while the cost engine and guardrails (`annual_cost_parameters`) use the real Year‑2 `ForecastSnapshot` rows. If an instructor's actual Year‑2 demand data isn't exactly 1.85× Year‑1, players are graded against a budget commitment that doesn't match the simulation's own cost math. Worth a sanity check against the seeded scenario data.

**12. Game-code existence is an enumeration oracle.** Membership-gated views return 403 for "no access" vs 404 for genuinely-missing games/years inconsistently in a few places (`views.py:984` and siblings), letting someone probe whether a guessable course code exists before they're a member. Low-impact given invite tokens are UUIDs, but easy to normalize to always-404.

---

## Low severity / hygiene

- **`add_player` (views.py:3696) lacks `@require_POST`** — every other state-mutating view in the file has it; this one is inconsistent (not currently exploitable since it's `@login_required` + `_admin_or_403`, but worth matching the pattern).
- **Year 3 is validated as a possible `year_number` throughout `models.py` but `annual_revenue_budget_options` (services.py:2160-2167) raises on anything but years 1–2** — dead/vestigial support that would break the moment a game is advanced to a third year.
- **Django admin (`/admin/`) is registered for every core model with no per-tenant scoping** (`simulation/admin.py`) — standard Django behavior, but any account that ever gets `is_staff=True` sees and can edit *every* instructor's games, decisions, and scores unfiltered. The seed command correctly keeps regular instructor/player accounts as `is_staff=False`; just flagging this as a blast-radius reminder if staff access is ever broadened.
- **Hardcoded seed passwords** (`ChangeMe123!`, `Play12345!` in `seed_strategy_game.py:34,38`) are fine for local dev but the README should explicitly warn not to run the seed command's default credentials in a real production seed.

---

## Configuration / deployment gaps

- **`config/settings.py` has no environment-variable mechanism at all.** `SECRET_KEY = "dev-only-change-me"` and `DEBUG = True` are hardcoded literals, not read from the environment. The setup README's production checklist says "production SECRET_KEY from environment variables" and "DEBUG=False", but the code as it stands has no code path to satisfy that — someone would have to hand-edit `settings.py` for every deploy, which is exactly the mistake environment variables are meant to prevent (and risks `DEBUG=True` leaking full stack traces/source in production if the edit is missed).
- **`AUTH_PASSWORD_VALIDATORS = []`** — Django's password-strength validation is fully disabled, so `player_invite_signup`/`instructor_signup` will accept any password, including trivially weak ones.
- **`requirements.txt` referenced throughout `readme_setup.txt` does not exist in the project folder.** Only `manage.py`, `config/`, `simulation/`, `db.sqlite3`, and the two readme files are present at the project root — no `requirements.txt`, no `.gitignore`, and the folder is not under version control (`git status` reports "not a git repository"). Anyone following the README's own setup steps (`pip install -r requirements.txt`) will fail at step 4. Recommend `pip freeze > requirements.txt` from the working venv (Django 5.2.17, asgiref 3.12.1, sqlparse 0.6.0) and initializing git with a `.gitignore` that excludes `venv/`, `db.sqlite3`, and `__pycache__/`.
- **No automated tests exist anywhere in the project** (no `tests.py`, no test app). For a tool whose output is student grades, even a small smoke-test suite (seed → sign up → create game → submit a month → check score) would catch regressions like finding #3 and #4 above automatically instead of relying on manual play-testing or reviews like this one.

---

## What's clean (verified, not just assumed)

- **Migrations match the models exactly** — every field, default, and constraint in `models.py` was traced against `0001_initial.py` + `0002_...py`; no drift found.
- **No `|safe`, no `mark_safe`, no `autoescape off`, no `csrf_exempt`** anywhere in the codebase; every `<form method="post">` across all 15 templates has a matching `{% csrf_token %}` (counts matched 1:1, template by template).
- **No raw SQL, no `eval`/`exec`, no `os.system`/`subprocess` calls, no bare `except:` blocks, no mutable default arguments** anywhere in the Python source.
- **All money fields use `DecimalField`**, not float — no rounding-drift risk found.
- **Authorization scoping is consistent almost everywhere**: every view taking a game `code`/`year`/`month`/membership id runs it through `_membership_or_403` / `_player_or_403` / `_admin_or_403` / `_scenario_year_or_404` before touching data, and querysets are additionally filtered by the resolved `membership=...` rather than trusting a bare id from the request — this is the pattern that closes IDOR, and it's applied consistently except for the one gap noted in #5.
- **`clone_game_scenario` (instructor "create game" flow) is properly idempotency-guarded** — pre-check, wrapped in a transaction, and followed by a post-clone integrity re-validation.
- **Division-by-zero is consistently guarded** across every KPI/ratio calculation checked (`annual_cost_parameters`, `newsvendor_inventory_target`, `calculate_revenue_attainment`/`calculate_budget_attainment`, `fixed_cost_absorption_status`).

---

## Environment limitation (please read)

This review session runs in a sandboxed cloud environment with no PyPI/apt network access, and the bridge to your local machine's shell (which has your working `venv` with Django 5.2.17 already installed) was unable to execute commands during this session — the two attempts to reach it either hung or the local `venv` interpreter itself pointed at a path (`/opt/anaconda3/...`) not visible from that sandboxed shell. As a result, everything above comes from full manual/static reading of the source (every `.py` file was read in full, plus all 15 templates and both migrations) rather than from actually running `manage.py check`, running the app, or exercising a real play-through with Django's test client.

Recommend running these yourself before your next deployment or class:

```
python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py showmigrations
python manage.py <seed_command>
python manage.py runserver
# then manually play through: instructor signup -> create game -> invite link -> player signup -> annual planning -> a few months -> year-end -> results
```

If you'd like, I can also implement fixes for any of the findings above, or write the smoke-test suite mentioned in the hygiene section — just say which ones.
