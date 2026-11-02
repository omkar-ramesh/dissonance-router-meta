"""Environment-driven configuration."""
import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DB_PATH = os.getenv("DB_PATH", "data/dissonance.db")
if not os.path.isabs(DB_PATH):
    DB_PATH = str(ROOT_DIR / DB_PATH)

# Models per tier live in models.toml, in two profiles of the Meta Model API: "standard" (your content is
# not used for training) and "contributor" (cheaper; your content may be used to train Meta models).
# MODE picks one explicitly.
TIERS = ("nano", "super", "ultra")
MODES = ("standard", "contributor")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
try:
    MODELS_FILE = ROOT_DIR / "models.toml"
    _FILE = tomllib.loads(MODELS_FILE.read_text(encoding="utf-8")) if MODELS_FILE.exists() else {}
except tomllib.TOMLDecodeError as e:
    raise SystemExit(f"models.toml is not valid: {e}")
MODE = os.getenv("MODE", _FILE.get("default_mode", "standard"))
if MODE not in MODES:
    raise SystemExit(f"MODE must be one of {MODES}, not {MODE!r}")
# Muse Spark always reasons: the router's dial is how much (reasoning_effort), not on or off.
_DEFAULT = {"nano": ("muse-spark-1.3", "minimal"), "super": ("muse-spark-1.3", "medium"),
            "ultra": ("muse-wonder-1.0", "high")}
_PRICE_DEFAULT = {"nano": (1.25, 4.25, 0.15), "super": (1.25, 4.25, 0.15), "ultra": (4.00, 20.00, 0.20)}

MODEL_TIERS, TIER_PROVIDERS, REASONING, PRICES = {}, {}, {}, {}


def _entry(mode: str, tier: str) -> dict:
    d, p = _DEFAULT[tier], _PRICE_DEFAULT[tier]
    return {"model": d[0], "effort": d[1], "in": p[0], "out": p[1], "cached": p[2], **_FILE.get(mode, {}).get(tier, {})}


def apply_mode(mode: str, use_env: bool = False) -> None:
    """Point every tier at the models of one profile. Dicts are updated in place so nothing holding them goes stale."""
    for tier in TIERS:
        e, up = _entry(mode, tier), tier.upper()
        MODEL_TIERS[tier] = os.getenv(f"MODEL_{up}", e["model"]) if use_env else e["model"]
        TIER_PROVIDERS[tier] = "meta"
        REASONING[tier] = os.getenv(f"EFFORT_{up}", e["effort"]) if use_env else e["effort"]
        PRICES[tier] = {k: float(os.getenv(f"PRICE_{up}_{k.upper()}", e[k])) if use_env else float(e[k])
                        for k in ("in", "out", "cached")}


def api_blocker() -> str:
    """Why the Meta Model API can't be used right now ('' if it can)."""
    if not META_API_KEY:
        return "Add MODEL_API_KEY to .env."
    return ""


def set_mode(mode: str) -> None:
    global MODE
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if api_blocker():
        raise ValueError(api_blocker())
    apply_mode(mode)
    MODE = mode


# Context window (tokens) per tier: every Muse text model in Model API has 1,048,576.
CONTEXT_WINDOWS = {tier: int(os.getenv(f"CTX_{tier.upper()}", "1048576")) for tier in TIERS}

# Who reads each message for style requests: "keywords" (fast, English-only phrasings)
# or "nano" (a short Muse Spark call reads it, keywords as the fallback if the call fails).
SIGNAL_EXTRACTOR = os.getenv("SIGNAL_EXTRACTOR", "nano")

# Public demo mode: private random session ids, a per-IP rate limit, output caps and a daily
# spend cut-off, so a public link can't read other people's chats or drain the credits.
PUBLIC_DEMO = os.getenv("PUBLIC_DEMO", "0") == "1"
# Hosted demo for judges only: when set, every request needs this code (see src/api/gate.py).
DEMO_ACCESS_CODE = os.getenv("DEMO_ACCESS_CODE", "")
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "6"))
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "1.00"))
# Max output tokens per answer (0 = no cap). Capped by default only in public demo mode.
_CAP_DEFAULTS = {"nano": "1500", "super": "2500", "ultra": "4000"} if PUBLIC_DEMO else {}
MAX_TOKENS = {tier: int(os.getenv(f"MAX_TOKENS_{tier.upper()}", _CAP_DEFAULTS.get(tier, "0"))) for tier in TIERS}

# Past conversations: compressed files here, deleted after this many days unless kept.
MEMORY_DIR = os.getenv("MEMORY_DIR", str(ROOT_DIR / "data" / "memory"))
MEMORY_RETENTION_DAYS = float(os.getenv("MEMORY_RETENTION_DAYS", "10"))

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))

# Meta Model API: an OpenAI-compatible endpoint (https://dev.meta.ai/docs/overview).
META_API_KEY = os.getenv("MODEL_API_KEY", "") or os.getenv("META_API_KEY", "")
META_BASE_URL = os.getenv("META_BASE_URL", "https://api.meta.ai/v1")
# The Models page lists these, with the live price of each tier.
META_MODELS = [
    {"tier": "nano", "name": "Muse Spark 1.3", "role": "Default tier: aligned users, minimal reasoning"},
    {"tier": "super", "name": "Muse Spark 1.3", "role": "Moderate conflict, medium reasoning"},
    {"tier": "ultra", "name": "Muse Wonder 1.0 (preview)", "role": "High conflict, high reasoning"},
]

apply_mode(MODE, use_env=True)
