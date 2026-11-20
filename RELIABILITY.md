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

All numbers are real runner output: 8 scenarios, k=5, faults injected (lost acks, duplicate delivery, malformed responses).

Original code, guard off. `violations` is n/a because the guard module did not exist yet:
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            0.20    0.00           5         0         n/a       5
return_partial              0.20    0.00           5         0         n/a       5
walk_in_order               0.20    0.00           5         0         n/a       5
promo_then_order            0.00    0.00           8         0         n/a       9
restock                     0.20    0.00           5         0         n/a       5
over_return_seeded          0.00    0.00          10         0         n/a       5
cross_turn_legit            0.00    0.00           8         0         n/a       9
cross_turn_over_return      0.00    0.00          13         0         n/a       9

OVERALL  pass^5 (avg) = 0.00   duplicate writes = 59   violations = n/a
```

DB fixes only, guard off (violations still n/a, no guard yet):
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            1.00    1.00           0         0         n/a       5
return_partial              0.20    0.00           5         0         n/a       5
walk_in_order               0.20    0.00           5         0         n/a       5
promo_then_order            0.00    0.00           8         0         n/a       9
restock                     1.00    1.00           0         0         n/a       5
over_return_seeded          1.00    1.00           0         0         n/a       5
cross_turn_legit            0.00    0.00           5         0         n/a       9
cross_turn_over_return      1.00    1.00           0         0         n/a       9

OVERALL  pass^5 (avg) = 0.50   duplicate writes = 23   violations = n/a
```

DB fixes + guard on:
```
scenario                  pass@1  pass^5  dup_writes  replayed  violations  faults
return_full_item            1.00    1.00           0         4           0       5
return_partial              1.00    1.00           0         4           0       5
walk_in_order               1.00    1.00           0         4           0       5
promo_then_order            1.00    1.00           0         7           0       9
restock                     1.00    1.00           0         4           0       5
over_return_seeded          1.00    1.00           0         0           0       5
cross_turn_legit            1.00    1.00           0         7           0       9
cross_turn_over_return      1.00    1.00           0         4           0       9

OVERALL  pass^5 (avg) = 1.00   duplicate writes = 0   violations = 0
✓ GATE PASSED
```

| Configuration | pass^5 (avg) | Duplicate writes |
|---|---|---|
| Original code | 0.00 | 59 |
| DB fixes only | 0.50 | 23 |
| DB fixes + guard | 1.00 | 0 |

The guard also held at k=20 across seeds 1, 2, 3, 42 and 99.

Why DB fixes alone stop at 0.50: a duplicate order or promotion looks exactly like a legitimate one at the DB level. Same items, same dates, valid quantities. Only the request's identity (call id, or intent within a turn) tells them apart.

## 5. Trade-offs I chose

- **Intent key scope is per user turn.** Two identical orders in one turn get deduped. The same order in a later turn is allowed. The `cross_turn_legit` scenario covers that. The cost: a user who really wants two identical orders in one message has to be told to split them.
- **Invariants detect and alarm. They do not roll back.** Enforcement belongs in the DB transaction (the trigger and preconditions). The invariants catch what the enforcement missed, and they page someone.
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
python3 -m evals.runner --guard off --k 5                       # DB fixes only: pass^5 0.50 on HEAD
python3 -m evals.runner --guard on  --k 5                       # gate must pass
python3 -m evals.runner --guard on  --k 3 --agent claude        # live; needs ANTHROPIC_API_KEY + pip install anthropic
```

To see the original numbers, run the same harness at commit c1cbafd, before the DB fixes. The scripted agent is deterministic on purpose. It retries the way real LLMs do (same name and args, new call id), so every result is reproducible and CI can gate on it. `--agent claude` runs the same scenarios against a live model.
