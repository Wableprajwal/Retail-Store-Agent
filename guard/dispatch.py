"""
Guarded dispatch: wraps agent.tools.dispatch so write tools are idempotent.

  dispatch_fn(tool_name, tool_input, ctx) -> dict      ctx = {"call_id", "turn_id"}

Two keys, two kinds of duplicate:
  idem_key   = turn_id:call_id  transport retry (same call delivered twice). Scoped to the
                                turn because some providers reuse ids (Gemini uses the
                                function name). A reused key with different args is
                                rejected, never replayed (Stripe returns 422 for this).
  intent_key = sha256(turn_id | tool | normalized args)
                                model retry (new call id, same write, same user turn)
Scope trade-off: two genuinely identical writes in one turn are treated as one.
"""

import copy
import hashlib
import json
import logging

from agent import tools
from agent.db import get_conn
from guard.invariants import check_all
from guard.ledger import Ledger

WRITE_TOOLS = {"create_order", "create_return", "create_restock_orders",
               "receive_purchase_order", "create_promotion"}

log = logging.getLogger("guard")


# Only fields the tools themselves treat case-insensitively are lowercased; anything
# else (ids, descriptions) could change behaviour, so merging them would be wrong.
CASE_INSENSITIVE = {"sku", "condition", "supplier_name", "product_name"}


def _normalize(value, key=None):
    """Strip strings, lowercase case-insensitive fields, drop None-valued keys, recurse."""
    if isinstance(value, str):
        value = value.strip()
        return value.lower() if key in CASE_INSENSITIVE else value
    if isinstance(value, dict):
        return {k: _normalize(v, k) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, key) for v in value]
    return value


def intent_key(turn_id, tool_name, tool_input):
    canonical = json.dumps(_normalize(tool_input), sort_keys=True, default=str)
    return hashlib.sha256(f"{turn_id}|{tool_name}|{canonical}".encode()).hexdigest()


def _replay(row):
    result = json.loads(row["result_json"])
    result["_replayed"] = True
    result["_replay_reason"] = ("same call_id" if row["matched_on"] == "idem_key"
                                else "same intent in this turn")
    return result


def make_dispatch(guard=True):
    if not guard:
        return lambda name, tool_input, ctx: tools.dispatch(name, copy.deepcopy(tool_input))

    ledger = Ledger()

    def dispatch_fn(name, tool_input, ctx):
        if name not in WRITE_TOOLS:
            return tools.dispatch(name, copy.deepcopy(tool_input))

        idem = f"{ctx['turn_id']}:{ctx['call_id']}"
        intent = intent_key(ctx["turn_id"], name, tool_input)
        args_json = json.dumps(tool_input, sort_keys=True, default=str)

        status, row = ledger.claim(idem, intent, name, args_json)
        if (status != "claimed" and row["matched_on"] == "idem_key"
                and (row["tool"], row["args_json"]) != (name, args_json)):
            return {"error": "idempotency key reused with different arguments; not executed"}
        if status == "completed":
            return _replay(row)
        if status == "in_progress":
            return {"error": "duplicate request still in progress; not executed"}

        try:
            result = tools.dispatch(name, copy.deepcopy(tool_input))
        except BaseException:
            ledger.release(idem)
            raise
        if "error" in result:
            ledger.release(idem)  # nothing was written; a genuine retry may proceed
            return result

        ledger.complete(idem, result)
        conn = get_conn()
        try:
            violations = check_all(conn)
        finally:
            conn.close()
        if violations:
            result = dict(result, _alarm=violations)
            log.warning("invariant violation after %s: %s", name, violations)
        return result

    return dispatch_fn
