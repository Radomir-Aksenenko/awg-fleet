"""Time-series metrics for the fleet, backed by a local SQLite file.

The controller samples every node's `awg show dump` each pass and records
per-peer counters here; the web panel reads aggregates from the same file.
Traffic totals are delta-accumulated so they survive interface restarts, which
zero the kernel counters. A tiny `meta` table lets the controller publish the
live rotation and smoothed per-node load for the panel to read without
recomputing it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

ONLINE_WINDOW = 180  # a peer counts as online if it handshook within this many seconds
RETENTION_DAYS = 7
DEFAULT_DB = os.environ.get("AWGFLEET_STATS", "stats.db")


class StatsDB:
    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS totals(
                    node TEXT, client TEXT,
                    rx_total INTEGER DEFAULT 0, tx_total INTEGER DEFAULT 0,
                    last_rx INTEGER DEFAULT 0, last_tx INTEGER DEFAULT 0,
                    session_secs INTEGER DEFAULT 0, last_handshake INTEGER DEFAULT 0,
                    PRIMARY KEY(node, client))"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS samples(
                    ts INTEGER, node TEXT, online INTEGER, rx INTEGER, tx INTEGER)"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
            c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    # ---- writes (controller) ----

    def collect(self, node: str, peers: dict, interval: float, now: int | None = None) -> None:
        """peers: {client_pubkey: {'rx':int,'tx':int,'handshake':int}}."""
        now = now or int(time.time())
        online_count = 0
        node_rx = node_tx = 0
        with self._conn() as c:
            for client, p in peers.items():
                rx, tx, hs = int(p["rx"]), int(p["tx"]), int(p["handshake"])
                row = c.execute(
                    "SELECT rx_total,tx_total,last_rx,last_tx,session_secs FROM totals WHERE node=? AND client=?",
                    (node, client),
                ).fetchone()
                rx_total, tx_total, last_rx, last_tx, sess = row or (0, 0, 0, 0, 0)
                rx_total += (rx - last_rx) if rx >= last_rx else rx  # counter reset -> take current
                tx_total += (tx - last_tx) if tx >= last_tx else tx
                online = 1 if (hs and now - hs < ONLINE_WINDOW) else 0
                if online:
                    sess += int(interval)
                    online_count += 1
                node_rx += rx
                node_tx += tx
                c.execute(
                    """INSERT INTO totals(node,client,rx_total,tx_total,last_rx,last_tx,session_secs,last_handshake)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(node,client) DO UPDATE SET
                         rx_total=excluded.rx_total, tx_total=excluded.tx_total,
                         last_rx=excluded.last_rx, last_tx=excluded.last_tx,
                         session_secs=excluded.session_secs, last_handshake=excluded.last_handshake""",
                    (node, client, rx_total, tx_total, rx, tx, sess, hs),
                )
            c.execute(
                "INSERT INTO samples(ts,node,online,rx,tx) VALUES(?,?,?,?,?)",
                (now, node, online_count, node_rx, node_tx),
            )
            c.execute("DELETE FROM samples WHERE ts < ?", (now - RETENTION_DAYS * 86400,))

    def set_meta(self, **kv) -> None:
        with self._conn() as c:
            for k, v in kv.items():
                c.execute(
                    "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (k, json.dumps(v)),
                )

    # ---- reads (web) ----

    def get_meta(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def client_detail(self, pub: str) -> dict:
        """Per-node traffic + session time for one client (by public key)."""
        now = int(time.time())
        with self._conn() as c:
            rows = c.execute(
                "SELECT node,rx_total,tx_total,session_secs,last_handshake FROM totals WHERE client=?",
                (pub,),
            ).fetchall()
        per_node = []
        total_rx = total_tx = 0
        last_seen = 0
        for node, rx, tx, secs, hs in rows:
            per_node.append({"node": node, "rx": rx, "tx": tx, "session_secs": secs, "handshake": hs})
            total_rx += rx
            total_tx += tx
            last_seen = max(last_seen, hs)
        favorite = max(per_node, key=lambda r: r["session_secs"])["node"] if per_node else None
        online = any(r["handshake"] and now - r["handshake"] < ONLINE_WINDOW for r in per_node)
        per_node.sort(key=lambda r: r["session_secs"], reverse=True)
        return {
            "rx": total_rx,
            "tx": total_tx,
            "last_seen": last_seen,
            "favorite": favorite,
            "online": online,
            "per_node": per_node,
        }

    def node_series(self, node: str, since_secs: int = 6 * 3600, buckets: int = 60) -> list[dict]:
        """Bucketed online-count and traffic-rate series for one node."""
        now = int(time.time())
        start = now - since_secs
        step = max(since_secs // buckets, 1)
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts,online,rx,tx FROM samples WHERE node=? AND ts>=? ORDER BY ts",
                (node, start),
            ).fetchall()
        out = []
        for b in range(buckets):
            b0, b1 = start + b * step, start + (b + 1) * step
            pts = [r for r in rows if b0 <= r[0] < b1]
            online = max((r[1] for r in pts), default=0)
            out.append({"t": b1, "online": online})
        return out

    def overview_series(self, since_secs: int = 6 * 3600, buckets: int = 60) -> list[dict]:
        """Total online clients across the fleet over time."""
        now = int(time.time())
        start = now - since_secs
        step = max(since_secs // buckets, 1)
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts,online FROM samples WHERE ts>=? ORDER BY ts", (start,)
            ).fetchall()
        out = []
        for b in range(buckets):
            b0, b1 = start + b * step, start + (b + 1) * step
            pts = [r[1] for r in rows if b0 <= r[0] < b1]
            out.append({"t": b1, "online": max(pts) if pts else 0})
        return out

    def node_users(self, node: str) -> int:
        """Clients currently online on a node."""
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM totals WHERE node=? AND last_handshake>?",
                (node, now - ONLINE_WINDOW),
            ).fetchone()
        return row[0] if row else 0
