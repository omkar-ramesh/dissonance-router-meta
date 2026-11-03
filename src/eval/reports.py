"""Saved reports: one gzipped JSON file per run in docs/reports/, so the app and the repo show the same evidence.

`python -m src.eval.reports --compress` rewrites every report file with the current card fields.
Where a run happened is read from the tier providers: any tier on the Meta Model API makes it a Meta run
("api"); a run with no model calls is simulated.
"""
import gzip
import os
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import config

REPORTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "reports"
ARCHIVE_DIR = REPORTS_DIR / "archive"        # older runs: kept as evidence, hidden from the default lists
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,80}$")


def where(providers: dict | None) -> str:
    return "api" if any(p == "meta" for p in (providers or {}).values()) else "local"


def place(providers: dict | None, simulated: bool = False) -> str:
    """The run's place for a file name: simulated, or the Meta mode it ran in (api-standard, api-contributor)."""
    if simulated:
        return "simulated"
    w = where(providers)
    return f"api-{config.MODE}" if w == "api" else w


def _ms(v) -> str:
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


RANK = {"nano": 0, "super": 1, "ultra": 2}


def turn_totals(report: dict) -> dict:
    """Totals over every turn that really happened: answers plus the signal reader's own calls.

    The strategy table only prices the scenarios it re-runs, so a card built from it showed 0 tokens
    for the rest. The turns are the record of what was actually sent and billed.
    """
    t = dict(turns=0, graded=0, matched=0, under=0, over=0, tok_in=0, tok_out=0, reasoning=0, cached=0, ms=0, ttft=[],
             cost=0.0, reader_cost=0.0, reader_in=0, reader_out=0, reader_reasoning=0)
    for s in report.get("scenarios", []):
        for x in s.get("turns", []):
            t["turns"] += 1
            if x.get("ideal"):
                t["graded"] += 1
                chose = x.get("router_chose")
                t["matched"] += chose == x["ideal"]
                if chose in RANK and x["ideal"] in RANK and chose != x["ideal"]:
                    t["under" if RANK[chose] < RANK[x["ideal"]] else "over"] += 1
            t["tok_in"] += x.get("input_tokens", 0) + x.get("reader_input_tokens", 0)
            t["tok_out"] += x.get("output_tokens", 0) + x.get("reader_output_tokens", 0)
            t["reasoning"] += x.get("reasoning_tokens", 0) + x.get("reader_reasoning_tokens", 0)
            t["cached"] += x.get("cached_tokens", 0)
            t["ms"] += x.get("latency_ms", 0)
            if x.get("ttft_ms") is not None:
                t["ttft"].append(x["ttft_ms"])
            t["cost"] += x.get("cost_usd", 0) + x.get("reader_cost_usd", 0)
            t["reader_cost"] += x.get("reader_cost_usd", 0)
            t["reader_in"] += x.get("reader_input_tokens", 0)
            t["reader_out"] += x.get("reader_output_tokens", 0)
            t["reader_reasoning"] += x.get("reader_reasoning_tokens", 0)
    return t


def _scenario_metrics(report: dict) -> list:
    st = report["self_test"]
    t = turn_totals(report)
    out = []
    if t["graded"]:
        out += [["Routing accuracy", f"{t['matched']}/{t['graded']}"]]
    if t["turns"]:
        out += [["Tokens in / out (all calls)", f"{t['tok_in']:,} / {t['tok_out']:,}"],
                ["Cost (answers + reader)", f"${t['cost']:.5f}"]]
    if t["graded"]:
        out += [["Under / over-routed", f"{t['under']} / {t['over']}"]]
    if t["turns"]:
        out += [["Reasoning tokens", f"{t['reasoning']:,}"]]
        if t["cached"]:
            out += [["Cached input tokens", f"{t['cached']:,}"]]
        out += [["Reader (signal) cost", f"${t['reader_cost']:.5f} · {t['reader_in']:,} in / {t['reader_out']:,} out"],
                ["Latency / answer", _ms(t["ms"] / t["turns"])]]
        if t["ttft"]:
            out += [["Time to first token", _ms(sum(t["ttft"]) / len(t["ttft"]))]]
    return out + [["False claims caught", f"{st['caught']}/{st['total']}"]]


def _scenario_card(report: dict, providers) -> dict:
    sc = report["scenarios"]
    ok = sum(1 for s in sc if s["as_expected"])
    st = report["self_test"]
    cost = turn_totals(report)["cost"]
    reader = report.get("reader", "keywords")
    sim = report["simulated"]
    return {
        "kind": "scenarios",
        "title": (f"{sc[0]['id'].replace('_', ' ')} · " if len(sc) == 1 else f"{len(sc)} scenarios · ") + f"{'Muse Spark' if reader == 'nano' else 'keyword'} reader",
        "description": ("Routing and grading are real; model answers and tokens are simulated." if sim else
                        "Real model answers on every scenario, graded by the same checks as the simulated run."),
        "headline": f"{ok}/{len(sc)} as expected",
        "chips": [f"{st['caught']}/{st['total']} false claims caught", "simulated" if sim else "real answers"],
        "flag": ("fail" if any(not x["as_expected"] or x["label"] in ("FAIL", "KNOWN GAP") for x in sc)
                 else "fake" if len(sc) == 1 and sc[0]["kind"] == "false_claim" else ""),
        "cost_usd": cost, "simulated": sim, "reader": reader, "metrics": _scenario_metrics(report),
        "models": report.get("models", {}), "source": where(providers),
    }


def _compare_card(report: dict, providers) -> dict:
    a, b = report["contestants"]
    return {
        "kind": "compare",
        "title": f"Is Ultra worth it? · {a} vs {b}" + (f" · {report['results'][0]['id']}" if report.get("cases") == 1 and report.get("results") else ""),
        "description": "Blind answer comparison: a judge model sees the answers without names, in both orders.",
        "headline": f"{report['wins'][a]}–{report['wins'][b]} · {report['ties']} tied",
        "chips": [f"{report['cases']} cases", f"cost ×{report['cost_ratio']}"],
        "cost_usd": sum(v["cost_usd"] for v in report["avg"].values()) * report["cases"],
        "simulated": report["simulated"], "reader": "", "models": report.get("models", {}),
        "source": where(providers),
        "metrics": [["Blind wins", f"{a} {report['wins'][a]} · {b} {report['wins'][b]} · tie {report['ties']}"]]
        + [[f"{k} latency / tokens out", f"{_ms(v['latency_ms'])} / {v['output_tokens']:.0f}"] for k, v in report["avg"].items()]
        + [["Cost ratio", f"x{report['cost_ratio']}"]],
    }


def _demo_cost(res: list) -> float:
    """Cost of the answers in a demo run, from the token counts the API reported."""
    from src.eval.runner import _cost
    total = 0.0
    for r in res:
        for t in r["turns"]:
            u = t.get("tokens") or {}
            total += _cost(t["tier"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                           (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    return total


def _demos_card(report: dict, providers) -> dict:
    from src.eval.demos import BY_ID
    res = [{"name": BY_ID.get(r["id"], {}).get("name", r["id"])} | r for r in report["demos"]]
    good = sum(1 for r in res if r["label"] in ("PASS", "CAUGHT", "KNOWN GAP", "FIXED"))
    return {
        "kind": "demos",
        "title": f"{res[0]['name']} · scripted persona run" if len(res) == 1 else f"{len(res)} demo profiles · scripted",
        "description": (f"Fixed script, real model answers: {res[0]['title']}." if len(res) == 1 else
                        "Fixed scripts, real model answers: 2 success, 2 known failures, 1 planted false claim."),
        "headline": " · ".join(f"{r['label']}" for r in res),
        "chips": [f"{good}/{len(res)} behaved as documented"], "cost_usd": _demo_cost(res), "simulated": False, "reader": "",
        "personas": [r.get("name", r["id"]) for r in res],
        "flag": "fail" if any(r["label"] == "KNOWN GAP" for r in res) else "fake" if len(res) == 1 and res[0]["label"] == "CAUGHT" else "",
        "metrics": [[r.get("name", r["id"]), f"{r['label']} · {' > '.join(t['tier'] for t in r['turns'])}"] for r in res]
        + [["Latency / answer", _ms(sum(t["latency_ms"] for r in res for t in r["turns"]) / max(1, sum(len(r["turns"]) for r in res)))],
           ["Tokens (total)", f"{sum(t['tokens'].get('total_tokens', 0) for r in res for t in r['turns']):,}"],
           ["Reasoning tokens", f"{sum((t['tokens'].get('completion_tokens_details') or {}).get('reasoning_tokens', 0) for r in res for t in r['turns']):,}"],
           ["Cost (answers)", f"${_demo_cost(res):.5f}"]],
        "models": report.get("models", {}), "source": where(providers),
    }


def build(report: dict, name: str, providers: dict | None = None, created: str | None = None) -> dict:
    card = (_compare_card if "contestants" in report else _demos_card if "demos" in report else _scenario_card)(report, providers)
    src = card["source"]
    run_n = os.environ.get("RUN_N")                    # repeat runs of the same test get "· run N" in the title
    if run_n:
        card["title"] += f" · run {run_n}"
    live = src == "api" and not card["simulated"]
    if live:                                           # which mode and how hard each tier thought are part of the record
        efforts = " · ".join(f"{tier} {effort}" for tier, effort in config.REASONING.items())
        card["mode"], card["efforts"] = config.MODE, dict(config.REASONING)
        card["chips"] = [*card.get("chips", []), config.MODE.title()]
        card["metrics"] = [*card.get("metrics", []), ["Mode", config.MODE.title()], ["Reasoning effort", efforts]]
    card["run_on"] = ("Simulator · no model calls" if card["simulated"] else
                      f"Meta Model API · {config.META_BASE_URL} · {config.MODE} mode" if src == "api" else "Not run on a model API")
    return {"id": name, "created": created or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **card, "report": report}


def save(report: dict, name: str, providers: dict | None = None, out: Path = REPORTS_DIR, created=None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("RUN_N"):
        name += f"-r{os.environ['RUN_N']}"
    path = out / f"{name}.json.gz"
    path.write_bytes(gzip.compress(json.dumps(build(report, name, providers or config.TIER_PROVIDERS, created)).encode("utf-8")))
    return path


def _read(path: Path) -> dict:
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def cards(out: Path = REPORTS_DIR) -> list[dict]:
    items = []
    for folder, archived in ((out, False), (out / "archive", True)):
        for path in sorted([*folder.glob("*.json.gz"), *folder.glob("*.json")]) if folder.is_dir() else []:
            try:
                data = _read(path)
            except (OSError, ValueError):
                continue
            items.append({k: v for k, v in data.items() if k != "report"} | {"archived": archived})
    return sorted(items, key=lambda c: c["created"], reverse=True)


def get(report_id: str, out: Path = REPORTS_DIR) -> dict | None:
    if not ID_RE.match(report_id):
        return None
    for path in (out / f"{report_id}.json.gz", out / f"{report_id}.json",
                 out / "archive" / f"{report_id}.json.gz", out / "archive" / f"{report_id}.json"):
        if path.is_file():
            return _read(path)
    return None


_SAVINGS_CACHE: dict = {}


def savings(out: Path = REPORTS_DIR) -> dict:
    """The router against always-Nano and always-Ultra, added up over every saved live scenario run.

    `matched` = the router served the tier the scenario labels as ideal; `under` = a cheaper tier than
    ideal (could cost quality); `over` = a dearer one (wastes money). Simulated runs are left out."""
    files = [p for folder in (out, out / "archive") if folder.is_dir() for p in [*folder.glob("*.json.gz"), *folder.glob("*.json")]]
    sig = (str(out), len(files), max((p.stat().st_mtime for p in files), default=0))
    if _SAVINGS_CACHE.get("sig") == sig:
        return _SAVINGS_CACHE["value"]
    total: dict = {}
    runs = 0
    for path in files:
        try:
            data = _read(path)
        except (OSError, ValueError):
            continue
        rep = data.get("report") or {}
        if data.get("kind") != "scenarios" or rep.get("simulated") or "strategies" not in rep:
            continue
        runs += 1
        for name, s in rep["strategies"].items():
            t = total.setdefault(name, dict.fromkeys(("cost_usd", "matched", "under", "over", "graded", "latency_ms", "output_tokens"), 0))
            for k in t:
                t[k] += s.get(k) or 0
    value: dict = {"runs": runs, "strategies": total}
    r, u = total.get("router"), total.get("always-ultra")
    if r and u and u["cost_usd"] and u["latency_ms"] and r["graded"]:
        value |= {"saved_vs_ultra_pct": round(100 * (1 - r["cost_usd"] / u["cost_usd"]), 1),
                  "faster_vs_ultra_pct": round(100 * (1 - r["latency_ms"] / u["latency_ms"]), 1),
                  "router_match_pct": round(100 * r["matched"] / r["graded"], 1)}
    _SAVINGS_CACHE.update(sig=sig, value=value)
    return value


def compress_all(out: Path = REPORTS_DIR) -> list[Path]:
    """Rewrite every report as .json.gz, re-deriving the card so new fields appear."""
    made = []
    for path in sorted([*out.glob("*.json"), *out.glob("*.json.gz")]):
        data = _read(path)
        prov = {"nano": "meta"} if data.get("source") == "api" else {"nano": "none"}
        made.append(save(data["report"], data["id"], prov, out, data.get("created")))
        if path.suffix == ".json":
            path.unlink()
    return made


if __name__ == "__main__":
    if "--compress" in sys.argv:
        for path in compress_all():
            print("compressed", path)
        sys.exit(0)
    print("usage: python -m src.eval.reports --compress")
    sys.exit(1)
