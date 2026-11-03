"""Usage ledger: one row per Meta API call, written as it happens.

The app, the eval CLI and the dev dashboard all log to the same SQLite file, so the Budget tab shows
real usage whichever of them made the call. Only numbers are stored, never message text. A ledger
failure never breaks an answer: `record` swallows its own errors.
"""
import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.db.database import get_connection, init_db

COLUMNS = ("ts", "source", "tier", "model", "effort", "input_tokens", "cached_tokens", "output_tokens",
           "reasoning_tokens", "cost_usd", "latency_ms", "ttft_ms", "estimated")


class Ledger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        init_db(db_path)

    def record(self, result, source: str = "app") -> None:
        """Log one finished call (an LLMResult)."""
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), source, result.tier, result.model,
               result.reasoning_effort or "", result.input_tokens, result.cached_tokens, result.output_tokens,
               result.reasoning_tokens, result.est_cost_usd or 0.0, result.latency_ms, result.ttft_ms or 0,
               int(result.tokens_estimated))
        try:
            conn = get_connection(self.db_path)
            try:
                conn.execute(f"INSERT INTO usage_log ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})", row)
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            pass

    def _rows(self, days: int = 0) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM usage_log", ()
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            sql, args = sql + " WHERE ts >= ?", (since,)
        conn = get_connection(self.db_path)
        try:
            return conn.execute(sql + " ORDER BY id", args).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _sum(rows) -> dict:
        n = len(rows)
        inp = sum(r["input_tokens"] for r in rows)
        cached = sum(r["cached_tokens"] for r in rows)
        return {
            "calls": n, "input_tokens": inp, "cached_tokens": cached,
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "reasoning_tokens": sum(r["reasoning_tokens"] for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "cache_hit_rate": round(100 * cached / inp, 1) if inp else 0.0,
            "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n) if n else 0,
            "estimated_calls": sum(r["estimated"] for r in rows),
        }

    def summary(self, days: int = 0, recent: int = 25) -> dict:
        """Totals, breakdowns, a per-day series (UTC days) and the latest calls. `days` 0 = all time."""
        rows = self._rows(days)
        groups = {}
        for key in ("model", "tier", "source", "effort"):
            seen = {}
            for r in rows:
                seen.setdefault(r[key] or "–", []).append(r)
            groups[f"by_{key}"] = sorted(({"name": k, **self._sum(v)} for k, v in seen.items()),
                                         key=lambda d: -d["cost_usd"])
        by_day = {}
        for r in rows:
            by_day.setdefault(r["ts"][:10], []).append(r)
        return {
            "days": days, "total": self._sum(rows), **groups,
            "by_day": [{"day": d, **self._sum(v)} for d, v in sorted(by_day.items())],
            "recent": [dict(r) for r in rows[-recent:]][::-1],
            "first_call": rows[0]["ts"] if rows else None, "last_call": rows[-1]["ts"] if rows else None,
        }

    def csv(self, days: int = 0) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(COLUMNS)
        for r in self._rows(days):
            w.writerow([r[c] for c in COLUMNS])
        return out.getvalue()


def ledger_for(db_path: Optional[str]) -> Optional[Ledger]:
    """A ledger on this database, or None when logging is off (no path)."""
    return Ledger(db_path) if db_path else None
