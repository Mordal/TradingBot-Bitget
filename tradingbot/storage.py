"""SQLite-opslag voor adviezen, outcomes, beslissingen, orders, fills, posities en rate-events."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS advices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, agent_id TEXT, model_version TEXT, instrument TEXT,
  advice TEXT, ratio REAL, horizon_s INTEGER, edge_bps REAL, entry_mid REAL, meta TEXT);
CREATE INDEX IF NOT EXISTS ix_adv_ts ON advices(ts_ms);
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, advice_id INTEGER, agent_id TEXT, instrument TEXT, horizon_s INTEGER,
  advice TEXT, ratio REAL, net_bps REAL, correct INTEGER, max_adverse_bps REAL, evaluated_ms INTEGER,
  UNIQUE(advice_id, horizon_s));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, instrument TEXT, action TEXT, notional_usdt REAL,
  ev_bps REAL, p_win REAL, risk_result TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS orders(
  client_oid TEXT PRIMARY KEY, order_id TEXT, instrument TEXT, side TEXT, purpose TEXT, order_type TEXT, force TEXT,
  size REAL, price REAL, status TEXT, filled_base REAL, avg_price REAL, fee REAL, fee_coin TEXT,
  created_ms INTEGER, acked_ms INTEGER, done_ms INTEGER, ack_latency_ms REAL, fill_latency_ms REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS fills(
  fill_id TEXT, client_oid TEXT, instrument TEXT, side TEXT, price REAL, base REAL, fee REAL, fee_coin TEXT,
  ts_ms INTEGER, PRIMARY KEY(client_oid, fill_id));
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, instrument TEXT, qty REAL, avg_price REAL,
  realized_usdt REAL, unrealized_usdt REAL);
CREATE TABLE IF NOT EXISTS trades_closed(
  id INTEGER PRIMARY KEY AUTOINCREMENT, instrument TEXT, opened_ms INTEGER, closed_ms INTEGER, qty REAL,
  entry_price REAL, exit_price REAL, pnl_usdt REAL, pnl_bps REAL, reason TEXT, agent_versions TEXT);
CREATE TABLE IF NOT EXISTS agent_stats(
  agent_id TEXT PRIMARY KEY, model_version TEXT, status TEXT, n INTEGER, hits INTEGER, mean_net_bps REAL,
  updated_ms INTEGER);
CREATE TABLE IF NOT EXISTS rate_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def insert(self, table: str, row: dict[str, Any], replace: bool = False) -> int:
        cols = list(row)
        vals = [json.dumps(v) if isinstance(v, (dict, list))
                else v for v in row.values()]
        sql = f"INSERT {'OR REPLACE ' if replace else ''}INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        with self._lock:
            cur = self._db.execute(sql, vals)
            self._db.commit()
            return cur.lastrowid or 0

    def insert_ignore(self, table: str, row: dict[str, Any]) -> bool:
        cols = list(row)
        sql = f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        with self._lock:
            cur = self._db.execute(sql, list(row.values()))
            self._db.commit()
            return cur.rowcount > 0

    def update(self, table: str, key_col: str, key: Any, values: dict[str, Any]) -> None:
        sets = ",".join(f"{c}=?" for c in values)
        with self._lock:
            self._db.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", [
                             *values.values(), key])
            self._db.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, list(params)).fetchall()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self.insert("kv", {"key": key, "value": value}, replace=True)
