# Running the tests yourself

Everything here can be run by hand. Each saved run appears in the app under **Settings → Reports**.

## 1. Setup (once)

```bash
pip install -r requirements.txt
copy .env.example .env      # cp on macOS/Linux
```

**Meta Model API (spends credits).** Get a key at [dev.meta.ai](https://dev.meta.ai) and put it in `.env`, never in code:

```bash
MODEL_API_KEY=...
```

The models, reasoning efforts and prices for each tier are in `models.toml`. The key is never shown in the app or saved in reports.

## 2. Start the app

```bash
python -m src.main
```

Open http://localhost:8000. Settings → Profile lists the personas. Pick one, read its docs, press **Activate** and chat.

## 3. The test runs

| What | Command | Cost |
|---|---|---|
| Unit tests (no models) | `python -m pytest tests -q` | free |
| Scenarios, simulated answers | `python -m src.eval --save` | free |
| Scenarios, real models | `python -m src.eval --live --save` | credits |
| Scenarios with the Nano signal reader | `python -m src.eval --live --reader nano --save` | credits |
| Persona demos (Hope, Jordan, Casey, Sam, Kai) | `python -m src.eval.demos --save` | credits |
| Blind "is Ultra worth it?" comparison | `python -m src.eval compare --save` | credits |

A run counts as a **Meta** run when its tiers use the Meta Model API; a run with no model calls is **simulated**.
Contributor mode costs far less per token than Standard, and is the mode to use for the synthetic test batches.

## 4. Reading a report

Each card shows the models per tier, the personas involved, and the numbers that matter:
routing accuracy (under / over-routed), tokens in and out, latency per answer, cost, and false claims caught.
**More details** opens the full record: every turn, the routing reason, the answer and the tier.

Reports are saved compressed as `docs/reports/<id>.json.gz`. To read one outside the app:

```bash
python -c "import gzip,json,sys; print(json.dumps(json.loads(gzip.open(sys.argv[1]).read()), indent=1))" docs/reports/<id>.json.gz
```

Persona write-ups are in `docs/personas/`.
