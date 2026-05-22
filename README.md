# S2 Signal Cycle Lab

Strict GitHub Pages app for coupling retained news cycles with scored market horizons.

## Source policy

Only public Pages artifacts are used.

Cycle artifacts:

- `https://dream-framework.github.io/s2_event_horizon_cycle/data/cycles.json`
- `https://dream-framework.github.io/s2_event_horizon_cycle/data/history.json`
- `https://dream-framework.github.io/s2_event_horizon_cycle/data/news_s2.json`

Market artifacts:

- `https://dream-framework.github.io/s2_signal_lab/data/live_predictions.csv`
- `https://dream-framework.github.io/s2_signal_lab/data/prediction_state.csv`
- `https://dream-framework.github.io/s2_signal_lab/data/prediction_scorecard.csv`
- `https://dream-framework.github.io/s2_signal_lab/data/model_comparison.json`

No dummy rows. No page scraping. No zero-filled coupling rows.

## What changed in this hardened build

- Normalizes duplicate cycle topic names before aggregation.
- Separates market artifact types:
  - `live_predictions.csv` is display-only.
  - `prediction_scorecard.csv` and aggregateable realized state are used for live scored horizons.
  - `model_comparison.json` is shown separately as backtest reference.
- Refuses to calculate hit/PnL from live predictions.
- Emits coupling rows only when scored horizon deltas are real.
- Keeps h1 as a dust diagnostic; advanced coupling is based on non-h1 horizons.
- Adds beta and dust audits so β floor-locking is visible.

## Deploy

1. Upload this repo to GitHub.
2. Settings → Pages → Source: GitHub Actions.
3. Actions → **Update and deploy S2 signal cycle lab** → Run workflow.
4. Open the Pages URL.

Generated bundle:

- `data/derived/signal_cycle_bundle.json`
- `data/derived/source_health.json`

These are deployed through GitHub Pages artifacts. They are not required to be committed back to the repo.
