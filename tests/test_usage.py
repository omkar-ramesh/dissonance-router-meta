"""The usage ledger: every Meta API call is logged as it finishes. No network."""
import httpx
import pytest
from fastapi.testclient import TestClient

from src.agent.core import Agent
from src.agent.llm_client import LLMClient, LLMResult
from src.agent.usage import Ledger
from src.api.dependencies import get_agent
from src.main import app
from tests.test_phase2 import MODELS, completion

PRICES = {"nano": {"in": 0.06, "out": 0.24, "cached": 0.006}}


def result(tier="nano", inp=100, out=50, cached=0, cost=0.001, effort="minimal"):
    return LLMResult(text="x", tier=tier, model=f"muse-{tier}", latency_ms=1000, input_tokens=inp, output_tokens=out,
                     cached_tokens=cached, est_cost_usd=cost, reasoning_effort=effort, reasoning_token_count=10)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "u.db"))


class TestLedger:
    def test_empty_ledger_summarises_to_zero(self, ledger):
        s = ledger.summary()
        assert s["total"]["calls"] == 0 and s["by_day"] == [] and s["recent"] == []

    def test_totals_and_breakdowns(self, ledger):
        ledger.record(result("nano", 100, 50, cached=40, cost=0.001), "app")
        ledger.record(result("ultra", 300, 700, cost=0.01, effort="xhigh"), "eval")
        s = ledger.summary()
        assert s["total"]["calls"] == 2 and s["total"]["input_tokens"] == 400 and s["total"]["output_tokens"] == 750
        assert s["total"]["cached_tokens"] == 40 and s["total"]["cache_hit_rate"] == 10.0
        assert s["total"]["reasoning_tokens"] == 20 and s["total"]["cost_usd"] == 0.011
        assert [r["name"] for r in s["by_tier"]] == ["ultra", "nano"]          # biggest spender first
        assert {r["name"] for r in s["by_source"]} == {"app", "eval"}
        assert {r["name"] for r in s["by_effort"]} == {"minimal", "xhigh"}
        assert s["recent"][0]["tier"] == "ultra"                                # newest first

    def test_no_message_text_is_stored(self, ledger):
        ledger.record(result(), "app")
        assert "text" not in ledger.summary()["recent"][0]

    def test_csv_has_a_header_and_a_row_per_call(self, ledger):
        ledger.record(result(), "app")
        ledger.record(result(), "app")
        lines = ledger.csv().strip().splitlines()
        assert lines[0].startswith("ts,source,tier") and len(lines) == 3

    def test_a_broken_database_never_breaks_a_call(self, ledger):
        ledger.db_path = "/nonexistent-folder-xyz/\0/bad.db"
        ledger.record(result(), "app")            # must not raise


class TestClientLogging:
    def test_every_call_is_logged_with_its_source(self, ledger):
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}
        client = LLMClient("http://test/v1", MODELS, transport=httpx.MockTransport(lambda r: completion("hi", usage=usage)),
                           prices=PRICES, ledger=ledger, source="eval")
        client.chat("nano", [{"role": "user", "content": "hello"}])
        row = ledger.summary()["recent"][0]
        assert row["source"] == "eval" and row["tier"] == "nano" and row["model"] == "small"
        assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (10, 4, 3)
        assert row["cost_usd"] > 0

    def test_a_client_without_a_ledger_logs_nothing(self, ledger):
        client = LLMClient("http://test/v1", MODELS, transport=httpx.MockTransport(lambda r: completion("hi")))
        client.chat("nano", [{"role": "user", "content": "hello"}])
        assert ledger.summary()["total"]["calls"] == 0


class TestApi:
    @pytest.fixture
    def client(self, tmp_path):
        a = Agent(str(tmp_path / "api.db"), None)
        app.dependency_overrides[get_agent] = lambda: a
        yield TestClient(app), Ledger(a.db_path)
        app.dependency_overrides.clear()

    def test_usage_endpoint_reflects_new_calls_at_once(self, client):
        http, ledger_ = client
        assert http.get("/usage").json()["total"]["calls"] == 0
        ledger_.record(result(), "app")
        body = http.get("/usage").json()
        assert body["total"]["calls"] == 1 and body["total"]["cost_usd"] == 0.001
        assert "saved_reports" in body and body["saved_reports"]["count"] >= 0

    def test_range_filters_by_days(self, client):
        http, ledger_ = client
        ledger_.record(result(), "app")
        assert http.get("/usage?days=1").json()["total"]["calls"] == 1
        assert http.get("/usage?days=400").status_code == 422

    def test_csv_download(self, client):
        http, ledger_ = client
        ledger_.record(result(), "app")
        r = http.get("/usage.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert len(r.text.strip().splitlines()) == 2
