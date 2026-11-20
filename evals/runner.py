"""
Eval runner: scenarios x k runs, fresh seeded temp DB per run, seeded faults.

  python -m evals.runner --guard on|off --k 5 --agent scripted|claude
                         [--seed 7] [--json PATH] [--scenario ID] [--quiet]
                         [--gate evals/thresholds.json]

pass@1 = fraction of runs where every check passed.
pass^k = 1 if all k runs passed (tau-bench).
dup_writes = effective write executions beyond the scenario's expected count.
"""

import argparse
import copy
import json
import os
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent.db as db  # noqa: E402
from agent import tools  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from evals.faults import FaultInjector, WRITE_TOOLS  # noqa: E402
from evals.scripted_agent import ScriptedClient  # noqa: E402


def _baseline_dispatch(name, tool_input, ctx):
    return tools.dispatch(name, copy.deepcopy(tool_input))


def _scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.DB_PATH = path
    from agent.seed import seed
    seed()
    return path


def _cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _violation_count(conn):
    try:
        from guard.invariants import check_all
    except ImportError:
        return "n/a"
    total = 0
    for v in check_all(conn):
        rows = v["rows"]
        total += len(rows) if hasattr(rows, "__len__") else int(rows)
    return total


def run_once(scenario, run_idx, args, make_dispatch):
    path = _fresh_db()
    try:
        conn = db.get_conn()
        baselines = {c["name"]: _scalar(conn, c["sql"]) for c in scenario["checks"]
                     if "expect_delta" in c}
        conn.close()

        base = make_dispatch(guard=True) if args.guard == "on" else _baseline_dispatch
        counter = {"writes": 0}

        def counting(name, tool_input, ctx):
            result = base(name, tool_input, ctx)
            # A restock that skipped everything (open POs already exist) wrote nothing.
            noop = (name == "create_restock_orders" and isinstance(result, dict)
                    and not result.get("purchase_orders"))
            if (name in WRITE_TOOLS and isinstance(result, dict) and not noop
                    and "error" not in result and "_replayed" not in result):
                counter["writes"] += 1
            return result

        # Independent fault stream per (seed, scenario, run) so scenarios don't all see
        # the same fault on their first write. crc32 is stable across processes; hash() isn't.
        faults = FaultInjector(zlib.crc32(f"{args.seed}|{scenario['id']}|{run_idx}".encode()))
        dispatch_fn = faults.wrap(counting)

        if args.agent == "claude":
            from agent.llm import get_client
            client = get_client("anthropic")
        else:
            client = ScriptedClient([t["script"] for t in scenario["turns"]])

        history, all_calls = [], []
        for i, turn in enumerate(scenario["turns"]):
            out = run_turn(client, history, turn["user"], dispatch_fn,
                           f"{scenario['id']}-{run_idx}-t{i}", log=lambda *_: None)
            all_calls.extend(out["tool_calls"])

        conn = db.get_conn()
        failed = []
        for c in scenario["checks"]:
            actual = _scalar(conn, c["sql"])
            if "expect_delta" in c:
                actual_cmp, want = actual - baselines[c["name"]], c["expect_delta"]
            else:
                actual_cmp, want = actual, c["expect"]
            if abs(actual_cmp - want) > 1e-6:
                failed.append({"check": c["name"], "expected": want, "actual": actual_cmp})
        violations = _violation_count(conn)
        conn.close()

        replayed = sum(1 for c in all_calls
                       if isinstance(c["result"], dict) and "_replayed" in c["result"])
        return {
            "scenario": scenario["id"], "run": run_idx, "passed": not failed,
            "failed_checks": failed, "writes": counter["writes"],
            "dup_writes": max(0, counter["writes"] - scenario["expected_writes"]),
            "replayed": replayed, "violations": violations,
            "faults": dict(faults.stats), "tool_calls": all_calls,
        }
    finally:
        _cleanup(path)


def summarize(results, k):
    rows = []
    for sid in dict.fromkeys(r["scenario"] for r in results):
        rs = [r for r in results if r["scenario"] == sid]
        vio = [r["violations"] for r in rs]
        rows.append({
            "scenario": sid,
            "pass_at_1": sum(r["passed"] for r in rs) / len(rs),
            "pass_hat_k": 1.0 if all(r["passed"] for r in rs) else 0.0,
            "dup_writes": sum(r["dup_writes"] for r in rs),
            "replayed": sum(r["replayed"] for r in rs),
            "violations": "n/a" if "n/a" in vio else sum(vio),
            "faults": sum(sum(r["faults"].values()) for r in rs),
        })
    return rows


def print_table(rows, k):
    print(f"{'scenario':<24}{'pass@1':>8}{'pass^'+str(k):>8}{'dup_writes':>12}"
          f"{'replayed':>10}{'violations':>12}{'faults':>8}")
    for r in rows:
        print(f"{r['scenario']:<24}{r['pass_at_1']:>8.2f}{r['pass_hat_k']:>8.2f}"
              f"{r['dup_writes']:>12}{r['replayed']:>10}{str(r['violations']):>12}{r['faults']:>8}")
    overall = overall_of(rows)
    print(f"\nOVERALL  pass^{k} (avg) = {overall['pass_hat_k']:.2f}   "
          f"duplicate writes = {overall['dup_writes']}   violations = {overall['violations']}")


def overall_of(rows):
    vio = [r["violations"] for r in rows]
    return {
        "pass_hat_k": sum(r["pass_hat_k"] for r in rows) / len(rows),
        "dup_writes": sum(r["dup_writes"] for r in rows),
        "violations": "n/a" if "n/a" in vio else sum(vio),
    }


def gate(overall, path):
    th = json.load(open(path))
    ok = (overall["pass_hat_k"] >= th["min_pass_hat_k"]
          and overall["dup_writes"] <= th["max_dup_writes"]
          and (overall["violations"] == "n/a" or overall["violations"] <= th["max_violations"]))
    print("✓ GATE PASSED" if ok else "✗ GATE FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", choices=["on", "off"], default="off")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--agent", choices=["scripted", "claude"], default="scripted")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json")
    ap.add_argument("--scenario")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--gate")
    args = ap.parse_args()

    if args.agent == "claude":
        from agent.config import load_env
        load_env()  # pick up ANTHROPIC_API_KEY from .env like main.py does

    make_dispatch = None
    if args.guard == "on":
        try:
            from guard.dispatch import make_dispatch
        except ImportError as e:
            print(f"--guard on needs the guard package (guard/dispatch.py): {e}")
            sys.exit(2)

    scenarios = json.load(open(os.path.join(ROOT, "evals", "scenarios.json")))
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            print(f"unknown scenario: {args.scenario}")
            sys.exit(2)

    if not args.quiet:
        print(f"guard={args.guard} agent={args.agent} k={args.k} seed={args.seed}\n")

    results = []
    for s in scenarios:
        for i in range(args.k):
            r = run_once(s, i, args, make_dispatch)
            results.append(r)
            if not args.quiet:
                status = "ok  " if r["passed"] else "FAIL"
                extra = "" if r["passed"] else "  " + "; ".join(
                    f"{f['check']}: want {f['expected']} got {f['actual']}" for f in r["failed_checks"])
                print(f"  {status} {s['id']} run {i} writes={r['writes']} faults={r['faults']}{extra}")
    if not args.quiet:
        print()

    rows = summarize(results, args.k)
    print_table(rows, args.k)
    overall = overall_of(rows)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"args": vars(args), "summary": rows, "overall": overall,
                       "runs": results}, f, indent=2, default=str)

    if args.gate:
        sys.exit(0 if gate(overall, args.gate) else 1)


if __name__ == "__main__":
    main()
