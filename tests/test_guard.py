"""
Deterministic tests for the reliability guard: ledger replay, intent dedupe,
DB-level fixes (returns trigger, open-PO rule, ID race) and invariants.
No LLM, no API key.

Run:  python tests/test_guard.py
"""

import os
import sys
import tempfile
import sqlite3
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.db as db
from guard.dispatch import make_dispatch
from guard.invariants import check_all


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.DB_PATH = path
    from agent.seed import seed
    seed()


def _count(sql, *params):
    conn = db.get_conn()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


RET = {"order_id": "O-1008", "sku": "TEE-BLU-M", "quantity": 1, "condition": "good"}


def test_invariants_clean_on_seed():
    fresh_db()
    conn = db.get_conn()
    assert check_all(conn) == [], check_all(conn)


def test_same_call_id_replays_without_second_write():
    fresh_db()
    d = make_dispatch(True)
    ctx = {"call_id": "c1", "turn_id": "t1"}
    a = d("create_return", dict(RET), ctx)
    b = d("create_return", dict(RET), ctx)
    assert "error" not in a, a
    assert b["_replayed"] is True and b["_replay_reason"] == "same call_id", b
    assert b["return_id"] == a["return_id"]
    assert _count("SELECT COUNT(*) FROM returns WHERE order_id='O-1008'") == 1


def test_new_call_id_same_args_same_turn_replayed():
    fresh_db()
    d = make_dispatch(True)
    a = d("create_return", dict(RET), {"call_id": "c1", "turn_id": "t1"})
    b = d("create_return", dict(RET, sku=" tee-blu-m "), {"call_id": "c2", "turn_id": "t1"})
    assert b["_replayed"] is True and b["_replay_reason"] == "same intent in this turn", b
    assert b["return_id"] == a["return_id"]
    assert _count("SELECT COUNT(*) FROM returns WHERE order_id='O-1008'") == 1


def test_same_args_different_turn_executes():
    fresh_db()
    d = make_dispatch(True)
    a = d("create_return", dict(RET), {"call_id": "c1", "turn_id": "t1"})
    b = d("create_return", dict(RET), {"call_id": "c2", "turn_id": "t2"})
    assert "error" not in a and "error" not in b, (a, b)
    assert "_replayed" not in b
    assert _count("SELECT COUNT(*) FROM returns WHERE order_id='O-1008'") == 2


def test_reused_call_id_different_args_rejected():
    # Gemini uses the function name as the call id, so ids repeat within a turn
    fresh_db()
    d = make_dispatch(True)
    ctx = {"call_id": "create_order", "turn_id": "t1"}
    a = d("create_order", {"items": [{"sku": "MUG", "quantity": 1}], "payment_method": "cash"}, ctx)
    b = d("create_order", {"items": [{"sku": "MUG", "quantity": 3}], "payment_method": "card"}, ctx)
    assert "error" not in a, a
    assert "reused with different arguments" in b.get("error", ""), b
    assert "_replayed" not in b


def test_reused_call_id_across_turns_executes():
    # Same provider id in a later turn is a new request, never a stale replay
    fresh_db()
    d = make_dispatch(True)
    order = {"items": [{"sku": "MUG", "quantity": 1}], "payment_method": "cash"}
    a = d("create_order", dict(order), {"call_id": "create_order", "turn_id": "t1"})
    b = d("create_order", dict(order), {"call_id": "create_order", "turn_id": "t2"})
    assert "_replayed" not in b and b["order_id"] != a["order_id"], (a, b)


def test_case_only_matters_for_case_insensitive_fields():
    # 'Good' and 'good' are the same return; a different order id is not
    fresh_db()
    d = make_dispatch(True)
    a = d("create_return", dict(RET, condition="Good"), {"call_id": "c1", "turn_id": "t1"})
    assert a["inventory_restored"] is True, a
    b = d("create_return", dict(RET, condition="good"), {"call_id": "c2", "turn_id": "t1"})
    assert b.get("_replayed") is True, b
    c = d("create_return", dict(RET, order_id="o-1008"), {"call_id": "c3", "turn_id": "t1"})
    assert "_replayed" not in c, c


def test_failed_write_releases_claim():
    fresh_db()
    d = make_dispatch(True)
    bad = d("create_return", dict(RET, order_id="O-9999"), {"call_id": "c1", "turn_id": "t1"})
    assert "error" in bad
    assert _count("SELECT COUNT(*) FROM tool_ledger") == 0
    # the same call id can now be retried with a corrected request
    ok = d("create_return", dict(RET), {"call_id": "c1", "turn_id": "t1"})
    assert "error" not in ok, ok


def test_read_tools_bypass_ledger():
    fresh_db()
    d = make_dispatch(True)
    d("check_inventory", {"sku": "TOTE"}, {"call_id": "c1", "turn_id": "t1"})
    assert _count("SELECT COUNT(*) FROM tool_ledger") == 0


def test_guard_off_is_baseline():
    fresh_db()
    d = make_dispatch(False)
    d("create_return", dict(RET), {"call_id": "c1", "turn_id": "t1"})
    d("create_return", dict(RET), {"call_id": "c1", "turn_id": "t1"})
    assert _count("SELECT COUNT(*) FROM returns WHERE order_id='O-1008'") == 2


def test_over_return_rejected_by_tool():
    """O-1006 HOOD-NVY-L: sold 2 (assumed), 1 already returned in seed."""
    fresh_db()
    from agent.tools import create_return
    r = create_return("O-1006", "HOOD-NVY-L", 2, "good")
    assert "error" in r and "already returned" in r["error"], r
    assert _count("SELECT COUNT(*) FROM returns WHERE order_id='O-1006'") == 1


def test_trigger_blocks_direct_insert():
    fresh_db()
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO returns VALUES ('R-9999','2026-06-19','O-1006','HOOD-NVY-L',2,'good',1.0)")
        conn.commit()
        raised = False
    except sqlite3.DatabaseError as e:
        raised = "return exceeds quantity sold" in str(e)
    assert raised, "trigger did not block the insert"
    assert _count("SELECT COUNT(*) FROM returns WHERE return_id='R-9999'") == 0


def test_restock_twice_no_duplicate_open_pos():
    fresh_db()
    from agent.tools import create_restock_orders
    first = create_restock_orders()
    second = create_restock_orders()
    made = [p for p in first["purchase_orders"] if "po_id" in p]
    assert made, first
    assert not [p for p in second["purchase_orders"] if "po_id" in p], second
    assert {s["product_id"] for s in second["skipped"]} >= {p["product_id"] for p in made}, second
    assert check_all(db.get_conn()) == []


def test_concurrent_create_order_unique_ids():
    fresh_db()
    from agent.tools import create_order
    results, errors = [], []

    def worker():
        try:
            results.append(create_order([{"sku": "SOCK", "quantity": 1}], "cash"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert all("error" not in r for r in results), results
    ids = {r["order_id"] for r in results}
    assert len(ids) == 8, ids


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
