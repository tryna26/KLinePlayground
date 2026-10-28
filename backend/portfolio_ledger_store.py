"""Aggregate transaction boundary, also usable within repository transactions.

One aggregate includes cursor, orders, lots, fills, cash journal and marks.
Persisting it together with operation results makes even disk failures atomic.
No legacy user tables or data paths are read or modified.
"""

import json
import sqlite3
from threading import RLock

from backend.portfolio_models import serialize
from backend.replay_models import ReplayDataError


class PortfolioLedgerStore:
    def __init__(self, path=":memory:"):
        self._connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._lock = RLock()
        self._connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS engine_state (
                training_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                schema_version INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS engine_operation (
                training_id TEXT NOT NULL REFERENCES engine_state(training_id),
                operation_id TEXT NOT NULL, expected_date TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(training_id, operation_id)
            );
        """)

    def close(self):
        self._connection.close()

    def read(self, training_id):
        with self._lock:
            row = self._connection.execute("SELECT payload FROM engine_state WHERE training_id=?",
                                           (training_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def operation(self, training_id, operation_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT expected_date,result FROM engine_operation WHERE training_id=? AND operation_id=?",
                (training_id, operation_id)).fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def _before_commit(self):
        """Fault-injection seam for verifying rollback after all writes."""

    def save(self, training_id, state, expected_version, *, operation=None):
        payload = json.dumps(serialize(state), ensure_ascii=False, sort_keys=True)
        with self._lock:
            db = self._connection
            owns_transaction = not db.in_transaction
            if owns_transaction:
                db.execute("BEGIN IMMEDIATE")
            try:
                if expected_version is None:
                    db.execute("INSERT INTO engine_state VALUES(?,?,?,?)",
                               (training_id, state.version, 1, payload))
                else:
                    cursor = db.execute(
                        "UPDATE engine_state SET version=?,payload=? WHERE training_id=? AND version=?",
                        (state.version, payload, training_id, expected_version))
                    if cursor.rowcount != 1:
                        raise ReplayDataError("账户版本已过期，请重新读取后操作")
                if operation:
                    operation_id, expected_date, result = operation
                    db.execute("INSERT INTO engine_operation VALUES(?,?,?,?)",
                               (training_id, operation_id, str(expected_date),
                                json.dumps(result, ensure_ascii=False, sort_keys=True)))
                self._before_commit()
                if owns_transaction:
                    db.execute("COMMIT")
            except BaseException:
                if owns_transaction:
                    db.execute("ROLLBACK")
                raise
