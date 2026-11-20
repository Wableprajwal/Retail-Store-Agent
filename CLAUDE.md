# CLAUDE.md: Agent Action Guard (reliability-guard branch)

This file is the handoff document. Any Claude session (or human) picking this up should read it fully first,
then check **Progress log** at the bottom to see what's done and what's next.

## Context

- **Owner:** Prajwal Wable (github.com/Wableprajwal).
- **Why this exists:** demo project for a 30-min 1:1 with an AWS Senior AI PM whose team builds agents, evals and
  "trust infra", focused on *making agents reliable at scale*. Also maps to the AWS Data Services SDE role
  (transactions, DB constraints, distributed systems).
- **Pitch:** "Stripe-style idempotency for AI agents." Agents that take real actions (refunds, orders, restocks)
  retry, repeat themselves and act on stale context. One double refund repeated across a million conversations is
  a real loss, and normal evals (one clean run) never catch it.
- **Story arc for the demo:** the harness finds real bugs in this repo's own agent (double refunds, duplicate POs),
  the guard + DB-level fixes eliminate them, and a CI gate keeps them from coming back.
  The commit order on this branch deliberately shows *harness first, fix second*.

## Bugs found in the original agent (before this branch)

1. `create_return` (`agent/tools.py`) only checks `quantity <= sold`; it never subtracts prior returns, so the same
   unit can be refunded twice and inventory restored twice.
2. `create_restock_orders` never checks for existing open POs; a retry creates duplicate purchase orders.
3. `_next_id` reads MAX(id) then inserts inside a *deferred* SQLite transaction. Two concurrent writers can both
   read the same max; one then fails (`database is locked` / PK conflict).

## Design

There are three kinds of duplicates, and they need different fixes:

| Kind | Example | Fix |
|---|---|---|
| Transport retry | Tool ran, response lost (timeout) or network re-delivers the same call | **Idempotency key = `call_id`** (provider's tool_call id). Ledger replays the stored original result |
| Model retry | LLM sees the timeout and issues a *new* call (new id) with the same args | **Intent key = sha256(turn_id, tool, normalized args)**. Same write + same args within one user turn = duplicate |
| Bad-state action | User asks again in a later turn; quantities don't add up | **DB-level enforcement**: SQLite trigger + precondition inside the write transaction (returns ≤ sold; one open PO per product) |

Plus **detection**: `guard/invariants.py` holds declarative SQL invariants checked after every write (and by the eval
runner after every run). This mirrors the fintech fix on Prajwal's resume: enforce at the DB layer, monitor for
immediate detection.

Intent-key scope (per turn) is a product trade-off: two identical orders in the *same* turn get deduped.
That's documented, not hidden.

### Write tools (state-changing)
`create_order`, `create_return`, `create_restock_orders`, `receive_purchase_order`, `create_promotion`.
Everything else is read-only and never goes through the ledger.

## Layout and interface contract

Stdlib only for everything new (no PyYAML; scenarios are JSON). Python 3.11+.

```
agent/loop.py            run_turn(client, history, user_text, dispatch_fn, turn_id, max_steps=12, log=print)
                         -> {"text": str, "tool_calls": [{"name","input","result","call_id"}]}
                         run_agent_loop() (CLI) uses run_turn with the guarded dispatch.
agent/tools.py           fixes for bugs 1-3 (BEGIN IMMEDIATE helper; prior-returns check; open-PO check)
agent/db.py              SCHEMA gains: trigger returns_not_exceed_sold, tool_ledger table
guard/ledger.py          Ledger(conn_factory): claim/complete/release; tool_ledger table
guard/invariants.py      INVARIANTS list; check_all(conn) -> [{"name","rows"}]
guard/dispatch.py        make_dispatch(guard: bool) -> dispatch_fn
                         WRITE_TOOLS set lives here
evals/faults.py          FaultInjector(seed, p_lost_ack, p_duplicate_delivery, p_malformed).wrap(dispatch_fn)
evals/scripted_agent.py  ScriptedClient(script) with the same interface as agent.llm.AnthropicClient
evals/scenarios.json     scenarios: user turns, scripted tool calls, SQL checks on final DB state
evals/runner.py          python -m evals.runner --guard on|off --k 5 --agent scripted|claude [--seed N] [--json out]
evals/thresholds.json    CI gate (applies to --guard on)
tests/test_guard.py      ledger replay, intent dedupe, cross-turn allowed, trigger, ID race, restock dedupe
.github/workflows/eval.yml  tests + eval gate on push/PR
RELIABILITY.md           problem, design, before/after results, "at AWS scale"
```

**`dispatch_fn` signature (everything shares this):**
```python
dispatch_fn(tool_name: str, tool_input: dict, ctx: dict) -> dict
# ctx = {"call_id": str, "turn_id": str}
```
- Baseline (guard off): `lambda name, inp, ctx: tools.dispatch(name, copy.deepcopy(inp))`.
  Always deepcopy: `create_order` mutates its `items` in place.
- Guarded: write tools go through the ledger; replayed results come back as the original dict plus
  `"_replayed": True`. Read tools pass straight through. After a successful write, invariants are checked;
  violations are attached as `"_alarm": [...]` and logged (detection, not rollback; enforcement is at the DB layer).
- A failed write (result has `"error"`) releases its ledger claim so a genuine retry can proceed.

**Fault model** (`FaultInjector.wrap(fn)` returns a `dispatch_fn`; seeded RNG, deterministic per seed):
- `lost_ack`: run the tool, then return `{"error": "timeout: tool did not respond in 10s; outcome unknown"}`.
- `duplicate_delivery`: run the tool twice with the same ctx (network re-delivery); return the second result.
- `malformed`: don't run the tool; return `{"error": "malformed response from tool backend"}` (safe to retry).
- Faults apply only to write tools.

**Scripted agent behaviour** (mimics what real LLMs do; that's the point):
issues the scenario's planned calls in order; on a result whose error contains `timeout` or `malformed` it retries
the *same name+args with a new call id* (max 2 retries); on any other error it moves on; ends the turn with a short
text summary.

**Metrics** (per scenario, k runs, fresh seeded temp DB per run, fault seed = base seed + run index):
- `pass@1` = fraction of runs where every check passed; `pass^k` = 1 if all k runs passed (τ-bench, Sierra 2024).
- `dup_writes` = effective write executions (result without `error`) beyond the scenario's expected count.
- `replayed` = calls the guard answered from the ledger.
- `violations` = invariant rows after the run.

## Commands

```bash
python tests/test_tools.py && python tests/test_guard.py      # all deterministic, no API key
python -m evals.runner --guard off --k 5                      # shows the bugs
python -m evals.runner --guard on  --k 5                      # gate must pass
python -m evals.runner --guard on  --k 3 --agent claude       # live; needs ANTHROPIC_API_KEY + pip install anthropic
python main.py                                                # interactive CLI (guard on)
```

## Working rules for this branch

- After each feature: run all tests and the eval, commit, push. The draft PR updates automatically.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Don't break the existing 16 tests in `tests/test_tools.py`; they encode the store's business rules.
- Keep new code in the same style as `agent/tools.py`: plain functions, docstrings stating business rules, no frameworks.
- Model routing preference (from the owner): Sonnet for writing code/docs and reading; Opus for planning and code review.

## Demo script (2 minutes, for the call)

1. "I built a retail agent a few months ago. I wanted to know if it was *reliable*, not just whether it works once."
2. `python -m evals.runner --guard off --k 5`: pass@1 looks okay, pass^5 collapses, double refunds and duplicate POs.
3. Explain the three duplicate kinds (table above). The intent key is the non-obvious one.
4. `python -m evals.runner --guard on --k 5`: pass^5 = 1.00, 0 duplicates, N replayed; gate passes.
5. At AWS scale: ledger → DynamoDB conditional put + TTL (same pattern as Powertools for AWS Lambda idempotency);
   in-progress lease for crash-between-claim-and-complete; evals on sampled prod traces; fault injection in canaries.
6. Ask him: how does his team scope "same intent"? Per turn, per session, per time window?

## Progress log

- [x] Branch `reliability-guard` created; baseline 16/16 tests pass.
- [x] CLAUDE.md handoff written; draft PR opened.
- [x] Eval harness (faults, scripted agent, scenarios, runner, `run_turn` refactor): guard off shows bugs.
      Original code: pass^5 avg 0.00, 59 duplicate writes. (commit c1cbafd)
- [x] DB-level fixes for bugs 1-3. Guard off: pass^5 avg 0.50, 23 duplicate writes. (commit 6612fcb)
      Note: these fixes apply with the guard off too, so `--guard off` on HEAD shows the "DB fixes only" picture.
      To reproduce the original numbers, run the harness at commit c1cbafd.
- [x] Guard (ledger, intent key, invariants, guarded dispatch) + `tests/test_guard.py` (11 tests).
      Guard on: pass^5 1.00, 0 duplicate writes; also holds at k=20 for seeds 1,2,3,42,99. (commit da9098a)
- [x] CI gate (`evals/thresholds.json` + `.github/workflows/eval.yml`).
- Shell note: the dev machine has `python3`, not `python`.
- [ ] RELIABILITY.md with real before/after numbers; README section.
- [ ] Opus code review of the full diff; fixes applied.
- [ ] Live Claude run (optional; no API key on the dev machine at time of writing).
