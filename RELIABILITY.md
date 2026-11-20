# Reliability: idempotency guard and fault-injection evals

## 1. The problem

Agents that take real actions retry and repeat themselves. A tool call times out, the model sees the error, and it issues the same call again. The first call may have already run. One clean eval run hides this, because most single runs get lucky. So I report pass^k, not pass@1: the fraction of scenarios where all k runs pass (from tau-bench, Sierra Research, 2024). If an agent double-refunds one run in five, pass@1 looks fine and pass^5 does not.

## 2. What the harness found in this agent

Three bugs in the original tools, found by running the eval against them:

1. `create_return` only checks `quantity <= sold`. It never subtracts prior returns, so one unit can be refunded twice and inventory restored twice.
2. `create_restock_orders` never checks for an open purchase order, so a retry creates duplicate POs.
3. `_next_id` reads MAX(id) and then inserts in a deferred SQLite transaction. Two writers can read the same max, and one fails.

## 3. Three kinds of duplicates, three fixes

| Kind | Example | Fix |
|---|---|---|
| Transport retry | Tool ran, response lost (timeout), or the network re-delivers the same call | Idempotency key = `call_id`. A ledger replays the stored original result |
| Model retry | The LLM sees the timeout and issues a new call (new id) with the same args | Intent key = sha256(turn_id, tool, normalized args). Same write + same args in one user turn = duplicate |
| Bad-state action | The user asks again in a later turn; the quantities no longer add up | DB trigger + precondition inside the write transaction (returns <= sold; one open PO per product) |

For detection, `guard/invariants.py` holds declarative SQL invariants. They run after every write, and the eval runner runs them after every run.

The intent key is the non-obvious one. A model retry has a new call id, so the call_id key misses it. The args are legal, so the DB accepts it. Take a customer who bought 3 units and returns 1. The call times out, the model retries, and the DB sees two returns of 1 against 3 sold. Every quantity check passes, and the customer is refunded twice. Only the identity of the request tells the two apart.

## 4. Results

All numbers are real runner output: 9 scenarios, k=5, seed 7, faults injected (lost acks, duplicate delivery, malformed responses). Each (seed, scenario, run) gets its own fault stream.

Original code, guard off. `violations` is n/a because the guard module did not exist yet. Note pass@1 of 0.40-0.60 on most scenarios: a single run often looks fine, pass^5 is 0.00 everywhere:
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            0.60    0.00           3         0         n/a       4
return_partial              0.40    0.00           4         0         n/a       9
walk_in_order               0.60    0.00           2         0         n/a       4
promo_then_order            0.00    0.00          14         0         n/a      16
restock                     0.60    0.00           2         0         n/a       2
receive_partial_po          0.40    0.00           3         0         n/a       4
over_return_seeded          0.00    0.00           7         0         n/a       4
cross_turn_legit            0.60    0.00           2         0         n/a       2
cross_turn_over_return      0.00    0.00           8         0         n/a       5

OVERALL  pass^5 (avg) = 0.00   duplicate writes = 45   violations = n/a
```

DB fixes only, guard off (invariants are still measured, the ledger is just not in the path):
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            1.00    1.00           0         0           0       4
return_partial              0.40    0.00           4         0           0       9
walk_in_order               0.60    0.00           2         0           0       4
promo_then_order            0.00    0.00          14         0           0      16
restock                     1.00    1.00           0         0           0       2
receive_partial_po          0.40    0.00           3         0           0       4
over_return_seeded          1.00    1.00           0         0           0       4
cross_turn_legit            0.60    0.00           2         0           0       2
cross_turn_over_return      1.00    1.00           0         0           0       5

OVERALL  pass^5 (avg) = 0.44   duplicate writes = 25   violations = 0
```

DB fixes + guard on:
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            1.00    1.00           0         2           0       4
return_partial              1.00    1.00           0         3           0       9
walk_in_order               1.00    1.00           0         1           0       4
promo_then_order            1.00    1.00           0         8           0      16
restock                     1.00    1.00           0         2           0       2
receive_partial_po          1.00    1.00           0         3           0       4
over_return_seeded          1.00    1.00           0         0           0       4
cross_turn_legit            1.00    1.00           0         2           0       2
cross_turn_over_return      1.00    1.00           0         1           0       5

OVERALL  pass^5 (avg) = 1.00   duplicate writes = 0   violations = 0
✓ GATE PASSED
```

| Configuration | pass^5 (avg) | Duplicate writes |
|---|---|---|
| Original code | 0.00 | 45 |
| DB fixes only | 0.44 | 25 |
| DB fixes + guard | 1.00 | 0 |

At k=20 the gate holds for seeds 1, 2, 3 and 99. Seed 42 scores 0.89: in one `return_partial` run the backend returned three malformed responses in a row, the agent used up its two retries and gave up. Zero duplicates and zero violations, so correctness held, but the customer's return never happened. That is a liveness failure, not a safety one, and the guard is not meant to fix it. The fix belongs in the agent: a retry budget with backoff, and telling the user plainly that the action did not go through.

**Honest caveat:** the scripted agent retries with byte-identical arguments, so the intent key always matches. A real model may change arguments on retry (add a default date, send `"1"` instead of `1`). Then the intent key misses and only the DB rules are left, which cannot catch a duplicate order. Treat the scripted numbers as an upper bound; `--agent claude` is the real test.

Why DB fixes alone stop at 0.44: a duplicate order or promotion looks exactly like a legitimate one at the DB level. Same items, same dates, valid quantities. Only the request's identity (call id, or intent within a turn) tells them apart.

## 5. Trade-offs I chose

- **Intent key scope is per user turn.** Two identical orders in one turn get deduped. The same order in a later turn is allowed. The `cross_turn_legit` scenario covers that. The cost: a user who really wants two identical orders in one message has to be told to split them.
- **Invariants detect and alarm. They do not roll back.** Enforcement belongs in the DB transaction (the trigger and preconditions). The invariants catch what the enforcement missed, and they page someone.
- **The transport key is `turn_id:call_id`, and a reused key with different arguments is rejected, not replayed.** Some providers reuse call ids (Gemini uses the function name), and replaying an old result for a different request would silently drop it. Stripe returns 422 for the same case.
- **Only case-insensitive fields are lowercased in the intent key** (sku, condition, supplier and product names). Lowercasing an order id or a description could merge two different requests.
- **The ledger claim and the tool transaction are separate.** If the process crashes between them, an `in_progress` row is left behind and that call stays blocked. This is the known gap. See the lease in section 6.

## 6. At AWS scale

- **Ledger:** DynamoDB conditional put (`attribute_not_exists`) plus TTL. This is the same pattern as the idempotency utility in Powertools for AWS Lambda.
- **Crashed claims:** an `in_progress` lease with an expiry, so a claim from a dead worker can be taken over safely.
- **Intent scope:** a per-tool policy (per turn, per session, per time window), not one global rule. A refund and a purchase order need different answers. This is the question I would most like to ask the team.
- **Evals:** run on sampled production traces, and use fault injection in canary deploys, so retry behavior is measured on real traffic shapes.
- **Invariants:** emit them as CloudWatch alarms.

## 7. Reproduce

```bash
python3 tests/test_tools.py && python3 tests/test_guard.py      # deterministic, no API key
python3 -m evals.runner --guard off --k 5                       # DB fixes only: pass^5 0.44 on HEAD
python3 -m evals.runner --guard on  --k 5                       # gate must pass
python3 -m evals.runner --guard on  --k 3 --agent claude        # live; needs ANTHROPIC_API_KEY + pip install anthropic
```

To see the original-code numbers, copy the repo and restore `agent/tools.py` and `agent/db.py` from commit aba6d92, then run the guard-off command there. The scripted agent is deterministic on purpose. It retries the way real LLMs do (same name and args, new call id), so every result is reproducible and CI can gate on it. `--agent claude` runs the same scenarios against a live model.
