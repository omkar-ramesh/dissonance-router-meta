"""models.toml profiles and the Standard / Contributor switch."""
import pytest
from fastapi.testclient import TestClient

from src import config
from src.main import app


@pytest.fixture(autouse=True)
def back_to_standard():
    yield
    config.apply_mode("standard")
    config.MODE = "standard"


def test_standard_profile_matches_models_toml():
    config.apply_mode("standard")
    assert config.MODEL_TIERS == {"nano": "muse-spark-1.3", "super": "muse-spark-1.3", "ultra": "muse-wonder-1.0"}
    assert set(config.TIER_PROVIDERS.values()) == {"meta"}
    assert config.REASONING == {"nano": "minimal", "super": "medium", "ultra": "high"}


def test_contributor_profile_uses_the_contributor_models_at_their_lower_price():
    config.apply_mode("contributor")
    assert all(m.endswith("-contributor") for m in config.MODEL_TIERS.values())
    assert config.PRICES["nano"]["in"] < 0.2 and config.REASONING["ultra"] == "xhigh"   # Contributor has no "max"
    config.apply_mode("standard")
    assert config.PRICES["ultra"] == {"in": 4.0, "out": 20.0, "cached": 0.2}


def test_the_switch_needs_a_key(monkeypatch):
    monkeypatch.setattr(config, "META_API_KEY", "")
    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        config.set_mode("contributor")
    assert config.MODE == "standard"
    monkeypatch.setattr(config, "META_API_KEY", "k")
    config.set_mode("contributor")
    assert config.MODE == "contributor" and config.MODEL_TIERS["nano"] == "muse-spark-1.3-contributor"


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        config.set_mode("offline")


def test_contributor_needs_confirm_and_no_switch_in_public_demo(monkeypatch):
    client = TestClient(app)
    assert client.post("/mode", json={"mode": "contributor"}).status_code == 400
    monkeypatch.setattr(config, "PUBLIC_DEMO", True)
    assert client.post("/mode", json={"mode": "standard"}).status_code == 403


def test_health_describes_the_meta_setup():
    h = TestClient(app).get("/health").json()
    assert h["mode"] == "standard" and set(h["tiers"]) == {"nano", "super", "ultra"}
    assert all(t["provider"] == "meta" for t in h["tiers"].values())
    assert [m["tier"] for m in h["meta_models"]] == ["nano", "super", "ultra"] and "api_blocker" in h
