"""Conflict-aware memory: what is kept depends on the dissonance signal. No network, no tokens."""
import gzip
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.agent.core import Agent
from src.agent.llm_client import LLMResult
from src.agent.memory import MemoryStore
from src.api.dependencies import get_agent
from src.db.database import get_connection, init_db
from src.main import app


class Recording:
    """Answers every call and keeps the messages it was sent."""

    def __init__(self):
        self.sent = []

    def chat(self, tier, messages, temperature=None, **kw):
        self.sent.append(messages)
        return LLMResult(text="a fairly long answer " * 20, tier=tier, model=tier, latency_ms=1,
                         input_tokens=100, output_tokens=100)


@pytest.fixture
def agent(tmp_path):
    return Agent(str(tmp_path / "m.db"), Recording(), memory_dir=str(tmp_path / "memory"))


def contradict(agent, user="u"):
    """Say 'short answers', then keep asking for more detail: a length contradiction."""
    agent.set_stated(user, "length", "concise")
    agent.respond(user, "Explain DNS in more detail please")
    agent.respond(user, "Can you elaborate on caching?")


def transcript(agent, item, user="u"):
    return agent.memory.read(user, item["id"])["turns"]


def db_row(agent, interaction_id):
    with get_connection(agent.memory.db_path) as conn:
        return dict(conn.execute("SELECT * FROM interactions WHERE id = ?", (interaction_id,)).fetchone())


class TestArchive:
    def test_calm_turns_keep_only_the_outcome(self, agent):
        agent.respond("u", "What is TCP?")
        item = agent.memory.archive("u")
        turn = transcript(agent, item)[0]
        assert turn["state"] == "calm" and turn["user_message"] == "What is TCP?"
        assert "agent_response" not in turn and turn["outcome"].endswith("80 words")
        assert item["calm_turns"] == 1 and item["unresolved"] == [] and item["bytes"] < item["raw_bytes"]
        assert db_row(agent, turn["id"])["agent_response"] == ""          # dropped from the database too

    def test_open_contradictions_are_kept_word_for_word_and_held_longer(self, agent):
        contradict(agent)
        item = agent.memory.archive("u")
        unresolved = [t for t in transcript(agent, item) if t["state"] == "unresolved"]
        assert unresolved and all(t["agent_response"] and t["dims"] == ["length"] for t in unresolved)
        assert item["unresolved"] == ["length"]
        held = datetime.fromisoformat(item["expires_at"]) - datetime.fromisoformat(item["archived_at"])
        assert held == timedelta(days=30)

    def test_the_users_delete_still_wins(self, agent):
        contradict(agent)
        item = agent.memory.archive("u")
        agent.memory.delete("u", item["id"])
        assert agent.memory.list("u") == []


class TestSettle:
    def test_accepting_folds_archived_contradictions_into_one_note(self, agent):
        contradict(agent)
        item = agent.memory.archive("u")
        argued = [t["id"] for t in transcript(agent, item) if t["state"] == "unresolved"]
        _, settled = agent.accept("u", "length")
        assert settled["archived_turns"] == len(argued) and settled["conversations"] == 1
        assert settled["bytes_after"] < settled["bytes_before"]
        assert "folded into one note: you prefer detailed answers" in settled["summary"]
        notes = [t for t in transcript(agent, item) if t["state"] == "note"]
        assert len(notes) == 1 and notes[0]["turns"] == len(argued) and notes[0]["value"] == "detailed"
        assert not any(t["state"] == "unresolved" for t in transcript(agent, item))
        after = agent.memory.get("u", item["id"])
        assert after["unresolved"] == [] and after["settled_turns"] == len(argued)
        assert after["days_left"] <= 10                                  # back to the normal date
        assert all(db_row(agent, i)["user_message"] == "" for i in argued)

    def test_settled_turns_leave_the_models_context(self, agent):
        contradict(agent)
        _, settled = agent.accept("u", "length")
        # the first "more detail" only taught the habit; the second contradicted the stated preference
        assert settled["live_turns"] == 1 and settled["context_tokens_saved"] > 0
        agent.respond("u", "What is a CDN?")
        context = " ".join(m["content"] for m in agent.llm.sent[-1])
        assert "elaborate on caching" not in context and "Explain DNS in more detail please" in context

    def test_unrelated_open_contradictions_stay_unresolved(self, agent):
        contradict(agent)
        item = agent.memory.archive("u")
        _, settled = agent.accept("u", "tone")                           # nothing argued about tone
        assert settled["turns"] == 0 and agent.memory.get("u", item["id"])["unresolved"] == ["length"]

    def test_the_note_bot_tells_the_story(self, agent):
        contradict(agent)
        agent.accept("u", "length")
        notes = [n["text"] for n in agent.notes.render("u", agent.inspect("u"))["notes"]]
        assert any("folded into one note" in n for n in notes)


def test_an_old_database_gets_the_new_columns(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
                 "user_message TEXT NOT NULL, agent_response TEXT NOT NULL DEFAULT '', model_used TEXT NOT NULL, "
                 "conflict_detected BOOLEAN NOT NULL DEFAULT 0, conflict_score FLOAT NOT NULL DEFAULT 0.0, "
                 "routing_reason TEXT NOT NULL DEFAULT '', timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    init_db(path)
    init_db(path)                                                        # twice: still fine
    with get_connection(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(interactions)")}
    assert {"conflict_dims", "memory_state"} <= cols


def test_accept_endpoint_reports_what_memory_did(tmp_path):
    a = Agent(str(tmp_path / "api.db"), Recording(), memory_dir=str(tmp_path / "memory"))
    app.dependency_overrides[get_agent] = lambda: a
    try:
        contradict(a, "u1")
        body = TestClient(app).post("/accept", json={"user_id": "u1", "dimension": "length"}).json()
        assert body["memory"]["turns"] == 1 and body["memory"]["live_turns"] == 1
        assert body["stated_profile"]["length"] == "detailed"
    finally:
        app.dependency_overrides.clear()
