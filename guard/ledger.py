"""
Idempotency ledger for write tools.

Each write call is claimed by INSERTing a row into tool_ledger. The table has two
uniqueness constraints: idem_key (the provider's call_id, catches transport
retries) and intent_key (hash of turn + tool + normalized args, catches the model
re-issuing the same write under a new call id). The INSERT either wins or raises
sqlite3.IntegrityError, so the claim is atomic. This is the SQLite analogue of a
DynamoDB conditional put (attribute_not_exists(idem_key)).

Known gap: if the process crashes between claim and complete, the row stays
'in_progress' and later duplicates are refused as "still in progress". At scale
you add a lease (expires_at / TTL) so a stale claim can be taken over or
reconciled against the downstream system.
"""

import json
import sqlite3
from datetime import datetime, timezone

from agent.db import get_conn


class Ledger:
    def __init__(self, conn_factory=get_conn):
        # Called per operation (never cached) so tests can swap agent.db.DB_PATH.
        self._conn_factory = conn_factory

    def _find(self, conn, idem_key, intent_key):
        return conn.execute(
            "SELECT * FROM tool_ledger WHERE idem_key=? OR intent_key=?",
            (idem_key, intent_key),
        ).fetchone()

    def claim(self, idem_key, intent_key, tool, args_json):
        """
        Try to claim a call. Returns (status, row):
          ("claimed", None)          we own it; go run the tool
          ("completed", row)         a prior call finished; row has result_json
          ("in_progress", row)       a prior call claimed it and hasn't finished
        row is a dict and includes "matched_on": "idem_key" or "intent_key".
        """
        conn = self._conn_factory()
        try:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO tool_ledger VALUES (?,?,?,?,'in_progress',NULL,?)",
                        (idem_key, intent_key, tool, args_json,
                         datetime.now(timezone.utc).isoformat()),
                    )
                return "claimed", None
            except sqlite3.IntegrityError:
                row = self._find(conn, idem_key, intent_key)
                if row is None:  # released between our failed insert and lookup
                    return self.claim(idem_key, intent_key, tool, args_json)
                row = dict(row)
                row["matched_on"] = "idem_key" if row["idem_key"] == idem_key else "intent_key"
                return row["status"], row
        finally:
            conn.close()

    def complete(self, idem_key, result):
        conn = self._conn_factory()
        try:
            with conn:
                conn.execute(
                    "UPDATE tool_ledger SET status='completed', result_json=? WHERE idem_key=?",
                    (json.dumps(result, default=str), idem_key),
                )
        finally:
            conn.close()

    def release(self, idem_key):
        """Delete the claim (the tool failed, so a genuine retry may run)."""
        conn = self._conn_factory()
        try:
            with conn:
                conn.execute("DELETE FROM tool_ledger WHERE idem_key=?", (idem_key,))
        finally:
            conn.close()
