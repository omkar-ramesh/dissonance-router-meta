"""Past conversations: archived compressed, kept 10 days unless the user keeps them.

"New chat" archives the current conversation to a gzip'd JSON file (one per conversation,
in a folder per user) and starts a fresh thread. The profile is not touched: a new chat is
not a new person.

An archived conversation is deleted automatically after RETENTION_DAYS unless the user
pressed Keep. Deletion removes the file AND the message text in the database (routing
numbers such as tier and score stay, for statistics), so "deleted" means deleted.
Reminders are computed from dates: no model call, no tokens.

Conflict-aware compression: what is kept depends on the dissonance signal, not just on age.
  calm turns (no contradiction)        -> outcome only: the question, the tier, the answer's size
  turns with an open contradiction     -> kept word for word, marked unresolved; the conversation
                                          is held HOLD_FACTOR times longer
  turns whose contradiction was closed -> folded into one preference note per dimension
`settle` runs when the user accepts an update: the turns that argued about it leave the model's
context and collapse into that note, in archived conversations too. All of it is fixed rules:
no model call, no tokens.
"""
from __future__ import annotations    # the method named `list` would otherwise shadow list[...] hints

import gzip
import hashlib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.agent.notes import label
from src.agent.profiles import DIMENSIONS
from src.db.database import get_connection

REMIND_DAYS = 3
HOLD_FACTOR = 3          # a conversation with an open contradiction is kept 3x longer


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self, db_path: str, folder, retention_days: float = 10, now: Callable[[], datetime] = _utc_now):
        self.db_path, self.folder, self.retention, self.now = db_path, Path(folder), retention_days, now

    # ------------------------------------------------------------ archive
    def boundary(self, user_id: str) -> int:
        """Id of the last archived interaction: the current conversation starts after it."""
        with closing(get_connection(self.db_path)) as conn:
            row = conn.execute("SELECT MAX(last_id) AS b FROM conversations WHERE user_id = ?", (user_id,)).fetchone()
        return row["b"] or 0

    def archive(self, user_id: str) -> Optional[dict]:
        """Compress the current conversation to a file and start a new one. None if it was empty."""
        start = self.boundary(user_id)
        with closing(get_connection(self.db_path)) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, user_message, agent_response, model_used, conflict_score, routing_reason, timestamp, "
                "conflict_dims, memory_state FROM interactions WHERE user_id = ? AND id > ? ORDER BY id",
                (user_id, start)).fetchall()]
            open_dims = self._open_dims(conn, user_id)
            stated = self._stated(conn, user_id)
        if not rows:
            return None
        now = self.now()
        title = next((r["user_message"] for r in rows if r["user_message"].strip()), "Conversation")[:80]
        turns, unresolved, calm_ids, folded_ids = self._shape(rows, open_dims, stated)
        days = self.retention * (HOLD_FACTOR if unresolved else 1)
        with closing(get_connection(self.db_path)) as conn:
            with conn:
                cid = conn.execute(
                    "INSERT INTO conversations (user_id, title, first_id, last_id, turns, bytes, file, archived_at, expires_at, "
                    "unresolved, settled_turns, calm_turns) VALUES (?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?, ?)",
                    (user_id, title, rows[0]["id"], rows[-1]["id"], len(rows), now.isoformat(),
                     (now + timedelta(days=days)).isoformat(), ",".join(unresolved), len(folded_ids), len(calm_ids))).lastrowid
                head = {"id": cid, "title": title, "archived_at": now.isoformat()}
                raw = len(gzip.compress(json.dumps({**head, "turns": rows}, ensure_ascii=False).encode("utf-8")))
                path = self._write(user_id, cid, {**head, "turns": turns})
                conn.execute("UPDATE conversations SET file = ?, bytes = ?, raw_bytes = ? WHERE id = ?",
                             (path.name, path.stat().st_size, raw, cid))
                self._drop_words(conn, user_id, calm_ids, folded_ids)
        return self.get(user_id, cid)

    # ------------------------------------------------------------ conflict-aware compression
    def settle(self, user_id: str, dimension: str) -> dict:
        """The user accepted an update on `dimension`: every turn that argued about it (and about
        nothing still open) is folded into one preference note. In the live conversation those turns
        leave the model's context; in archived ones their words are dropped and the note takes their
        place. Returns what changed, for the UI."""
        match = "instr(',' || {} || ',', ',' || ? || ',') > 0"
        with closing(get_connection(self.db_path)) as conn:
            open_dims = self._open_dims(conn, user_id)
            stated = self._stated(conn, user_id)
            live = conn.execute(
                "SELECT id, user_message, agent_response, conflict_dims FROM interactions WHERE user_id = ? AND id > ? "
                f"AND memory_state != 'settled' AND {match.format('conflict_dims')}",
                (user_id, self.boundary(user_id), dimension)).fetchall()
            convs = conn.execute(f"SELECT * FROM conversations WHERE user_id = ? AND {match.format('unresolved')}",
                                 (user_id, dimension)).fetchall()
        live = [r for r in live if not set(_dims(r["conflict_dims"])) & open_dims]
        context_tokens = sum(len(r["user_message"]) + len(r["agent_response"]) for r in live) // 4
        if live:
            with closing(get_connection(self.db_path)) as conn:
                with conn:
                    conn.execute(f"UPDATE interactions SET memory_state = 'settled' WHERE id IN ({','.join('?' * len(live))})",
                                 [r["id"] for r in live])
        before = after = archived = 0
        for row in convs:
            b, a, n = self._settle_conversation(row, open_dims, stated)
            before, after, archived = before + b, after + a, archived + n
        turns = len(live) + archived
        value = stated.get(dimension, "balanced")
        summary = (f"{turns} turn{'s' if turns != 1 else ''} that argued about {dimension} folded into one note: "
                   f"you prefer {label(dimension, value)}.") if turns else ""
        return {"dimension": dimension, "value": value, "turns": turns, "live_turns": len(live),
                "archived_turns": archived, "conversations": len(convs), "context_tokens_saved": context_tokens,
                "bytes_before": before, "bytes_after": after, "summary": summary}

    def _settle_conversation(self, row, open_dims: set, stated: dict) -> tuple[int, int, int]:
        """Re-fold one archived conversation now that fewer contradictions are open."""
        path = self._path(row["user_id"], row["id"])
        try:
            before = path.stat().st_size
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return 0, 0, 0
        turns, unresolved, _, folded = self._shape(payload["turns"], open_dims, stated)
        payload["turns"] = turns
        self._write(row["user_id"], row["id"], payload)
        after = path.stat().st_size
        expires = row["expires_at"]
        if not unresolved:          # nothing open any more: back to the normal date (never sooner than a day)
            base = datetime.fromisoformat(row["archived_at"]) + timedelta(days=self.retention)
            expires = max(base, self.now() + timedelta(days=1)).isoformat()
        with closing(get_connection(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE conversations SET bytes = ?, unresolved = ?, settled_turns = settled_turns + ?, "
                             "expires_at = ? WHERE id = ?", (after, ",".join(unresolved), len(folded), expires, row["id"]))
                self._drop_words(conn, row["user_id"], [], folded)
        return before, after, len(folded)

    @staticmethod
    def _shape(rows: list[dict], open_dims: set, stated: dict) -> tuple[list, list, list, list]:
        """Apply the three rules to a list of turns (raw rows, or turns already shaped once).
        Returns (turns, open dimensions left in them, ids kept as outcome only, ids folded into notes)."""
        out, unresolved, calm, folded, notes = [], [], [], [], {}
        for t in rows:
            state = t.get("state")
            if state in ("calm", "note"):
                if state == "note":
                    notes[t["dimension"]] = t
                out.append(t)
                continue
            dims = t.get("dims") or _dims(t.get("conflict_dims", ""))
            still_open = [d for d in dims if d in open_dims]
            if still_open:
                out.append({**{k: v for k, v in t.items() if k not in ("conflict_dims", "memory_state")},
                            "state": "unresolved", "dims": still_open})
                unresolved += [d for d in still_open if d not in unresolved]
            elif dims or t.get("memory_state") == "settled":
                folded.append(t["id"])
                for d in dims or ["preference"]:
                    note = notes.get(d)
                    if note is None:
                        note = notes[d] = {"state": "note", "dimension": d, "value": stated.get(d, "balanced"),
                                           "model_used": t.get("model_used", ""), "turns": 0, "user_message": ""}
                        out.append(note)
                    note["turns"] += 1
                    note["value"] = stated.get(d, note["value"])
                    note["note"] = (f"Settled: you prefer {label(d, note['value'])}. {note['turns']} turn"
                                    f"{'s' if note['turns'] != 1 else ''} that argued about it were folded into this note.")
            else:
                calm.append(t["id"])
                words = len((t.get("agent_response") or "").split())
                out.append({"id": t["id"], "state": "calm", "user_message": t.get("user_message", ""),
                            "model_used": t.get("model_used", ""), "timestamp": t.get("timestamp"),
                            "outcome": f"{(t.get('model_used') or 'model').title()} answered · {words} words"})
        return out, unresolved, calm, folded

    def _write(self, user_id: str, cid: int, payload: dict) -> Path:
        path = self._path(user_id, cid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    @staticmethod
    def _drop_words(conn, user_id: str, calm_ids: list, folded_ids: list) -> None:
        """Compression is real: the words it dropped from the file go from the database too."""
        if calm_ids:
            conn.execute(f"UPDATE interactions SET agent_response = '' WHERE user_id = ? AND id IN ({','.join('?' * len(calm_ids))})",
                         [user_id, *calm_ids])
        if folded_ids:
            conn.execute("UPDATE interactions SET user_message = '', agent_response = '', memory_state = 'settled' "
                         f"WHERE user_id = ? AND id IN ({','.join('?' * len(folded_ids))})", [user_id, *folded_ids])

    @staticmethod
    def _open_dims(conn, user_id: str) -> set:
        return {r["conflict_type"] for r in conn.execute(
            "SELECT DISTINCT conflict_type FROM conflicts WHERE user_id = ? AND resolved = 0", (user_id,))}

    @staticmethod
    def _stated(conn, user_id: str) -> dict:
        row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return {d: row[f"stated_{d}"] for d in DIMENSIONS} if row else {}

    # ------------------------------------------------------------ read
    def list(self, user_id: str) -> list[dict]:
        with closing(get_connection(self.db_path)) as conn:
            rows = conn.execute("SELECT * FROM conversations WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        return [self._item(r) for r in rows]

    def get(self, user_id: str, cid: int) -> Optional[dict]:
        row = self._row(user_id, cid)
        return self._item(row) if row else None

    def read(self, user_id: str, cid: int) -> dict:
        """The archived transcript (decompressed)."""
        row = self._row(user_id, cid)
        if not row:
            raise LookupError(f"No conversation {cid} for this user")
        with gzip.open(self._path(user_id, cid), "rt", encoding="utf-8") as f:
            return json.load(f)

    def file(self, user_id: str, cid: int) -> Path:
        if not self._row(user_id, cid):
            raise LookupError(f"No conversation {cid} for this user")
        return self._path(user_id, cid)

    def reminders(self, user_id: str) -> list[dict]:
        """Unkept conversations that will be deleted within REMIND_DAYS."""
        return [i for i in self.list(user_id) if not i["kept"] and i["days_left"] <= REMIND_DAYS]

    # ------------------------------------------------------------ change
    def keep(self, user_id: str, cid: int, kept: bool = True) -> dict:
        """Keep forever, or undo that (the 10 days start again from now)."""
        if not self._row(user_id, cid):
            raise LookupError(f"No conversation {cid} for this user")
        expires = (self.now() + timedelta(days=self.retention)).isoformat()
        with closing(get_connection(self.db_path)) as conn:
            with conn:
                if kept:
                    conn.execute("UPDATE conversations SET saved = 1 WHERE id = ?", (cid,))
                else:
                    conn.execute("UPDATE conversations SET saved = 0, expires_at = ? WHERE id = ?", (expires, cid))
        return self.get(user_id, cid)

    def delete(self, user_id: str, cid: int) -> None:
        row = self._row(user_id, cid)
        if not row:
            raise LookupError(f"No conversation {cid} for this user")
        self._erase(row)

    def sweep(self) -> int:
        """Delete every unkept conversation past its date (all users). Returns how many."""
        now = self.now().isoformat()
        with closing(get_connection(self.db_path)) as conn:
            due = conn.execute("SELECT * FROM conversations WHERE saved = 0 AND expires_at <= ?", (now,)).fetchall()
        for row in due:
            self._erase(row)
        return len(due)

    # ------------------------------------------------------------ internals
    def _erase(self, row) -> None:
        self._path(row["user_id"], row["id"]).unlink(missing_ok=True)
        with closing(get_connection(self.db_path)) as conn:
            with conn:
                # the words go; tier, score and reason stay, for statistics
                conn.execute("UPDATE interactions SET user_message = '', agent_response = '' "
                             "WHERE user_id = ? AND id BETWEEN ? AND ?", (row["user_id"], row["first_id"], row["last_id"]))
                # quotes the note bot kept from this conversation go too
                conn.execute("UPDATE events SET detail = '' WHERE user_id = ? AND interaction_id BETWEEN ? AND ?",
                             (row["user_id"], row["first_id"], row["last_id"]))
                conn.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))

    def _row(self, user_id: str, cid: int):
        with closing(get_connection(self.db_path)) as conn:
            return conn.execute("SELECT * FROM conversations WHERE id = ? AND user_id = ?", (cid, user_id)).fetchone()

    def _path(self, user_id: str, cid: int) -> Path:
        # Folder named by a hash of the id: user ids may contain dots, so never trust them in a path.
        folder = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        return self.folder / folder / f"conversation-{int(cid)}.json.gz"

    def _item(self, row) -> dict:
        expires = datetime.fromisoformat(row["expires_at"])
        left = (expires - self.now()).total_seconds() / 86400
        return {"id": row["id"], "title": row["title"], "turns": row["turns"], "bytes": row["bytes"],
                "raw_bytes": row["raw_bytes"] or row["bytes"], "unresolved": _dims(row["unresolved"]),
                "settled_turns": row["settled_turns"], "calm_turns": row["calm_turns"],
                "archived_at": row["archived_at"], "kept": bool(row["saved"]),
                "expires_at": None if row["saved"] else row["expires_at"],
                "days_left": None if row["saved"] else max(0.0, round(left, 2))}


def _dims(text: str) -> list[str]:
    return [d for d in (text or "").split(",") if d]
