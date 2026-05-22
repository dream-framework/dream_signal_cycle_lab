#!/usr/bin/env python3
"""Build S2 signal-cycle bundle from public GitHub Pages artifacts only.

Strict source policy:
- Fetch only configured public Pages JSON/CSV artifact URLs.
- No rendered-page scraping.
- No dummy rows.
- No zero-filled coupling rows.
- Live predictions are live-state only; they are never used to compute hit/PnL.
- Market horizon lift is computed only from scored aggregate artifacts or aggregateable realized state.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "feeds.yml"
OUT_DIR = ROOT / "data" / "derived"
BUNDLE_PATH = OUT_DIR / "signal_cycle_bundle.json"
HEALTH_PATH = OUT_DIR / "source_health.json"
USER_AGENT = "Mozilla/5.0 (compatible; s2-signal-cycle-lab/2.0; +https://github.com/dream-framework/)"

TOPIC_ALIASES = {
    "markets": "Markets / Economy",
    "market": "Markets / Economy",
    "markets_economy": "Markets / Economy",
    "markets_/_economy": "Markets / Economy",
    "economy": "Markets / Economy",
    "ai": "AI / Tech",
    "ai_tech": "AI / Tech",
    "ai_/_tech": "AI / Tech",
    "tech": "AI / Tech",
    "public_health": "Public Health",
    "health": "Public Health",
    "space_science": "Space / Science",
    "space_/_science": "Space / Science",
    "space": "Space / Science",
    "science": "Space / Science",
    "culture_media": "Culture / Media",
    "culture_/_media": "Culture / Media",
    "culture": "Culture / Media",
    "media": "Culture / Media",
    "climate": "Climate / Weather",
    "climate_weather": "Climate / Weather",
    "climate_/_weather": "Climate / Weather",
    "weather": "Climate / Weather",
    "politics": "Politics / Elections",
    "elections": "Politics / Elections",
    "politics_elections": "Politics / Elections",
    "politics_/_elections": "Politics / Elections",
    "geopolitics": "Geopolitics",
    "cybersecurity": "Cybersecurity",
    "cyber": "Cybersecurity",
    "energy": "Energy",
    "general": "General",
    "quantum": "Quantum tech",
    "quantum_tech": "Quantum tech",
}

AGG_SCORE_REQUIRED = {"direction_hit", "pnl_proxy", "mae"}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_key(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())).strip("_")


def get_any(d: dict[str, Any], *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    by_norm = {normalize_key(k): k for k in d.keys()}
    for key in keys:
        nk = normalize_key(key)
        if nk in by_norm:
            return d[by_norm[nk]]
    return None


def first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "—", "-"}:
        return None
    text = text.replace(",", "")
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1]
    is_pct = text.endswith("%")
    if is_pct:
        text = text[:-1].strip()
    try:
        v = float(text)
    except ValueError:
        return None
    if neg:
        v = -v
    if not math.isfinite(v):
        return None
    return v / 100.0 if is_pct else v


def duration_to_hours(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip().lower().replace(" ", "")
    if not text or text in {"—", "-"}:
        return None
    try:
        if text.endswith(("hours", "hour")):
            return float(re.sub(r"hours?$", "", text))
        if text.endswith(("hrs", "hr")):
            return float(re.sub(r"hrs?$", "", text))
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith(("days", "day")):
            return float(re.sub(r"days?$", "", text)) * 24.0
        if text.endswith("d"):
            return float(text[:-1]) * 24.0
        return float(text)
    except ValueError:
        return None


def fmt_topic(topic: Any) -> str | None:
    if topic is None or isinstance(topic, (dict, list)):
        return None
    raw = str(topic).strip()
    if not raw:
        return None
    key = normalize_key(raw.replace("/", " / "))
    if key in TOPIC_ALIASES:
        return TOPIC_ALIASES[key]
    key2 = normalize_key(raw)
    if key2 in TOPIC_ALIASES:
        return TOPIC_ALIASES[key2]
    # Title-case snake names but preserve slash labels.
    if "_" in raw and "/" not in raw:
        return raw.replace("_", " ").title()
    return raw


def median(vals: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    return float(statistics.median(clean)) if clean else None


def mean(vals: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    return float(statistics.fmean(clean)) if clean else None


def mode(vals: Iterable[float | None], ndigits: int = 4) -> float | None:
    clean = [round(float(v), ndigits) for v in vals if v is not None and math.isfinite(float(v))]
    return Counter(clean).most_common(1)[0][0] if clean else None


def build_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_url(url: str, timeout: int = 18, max_bytes: int | None = None) -> tuple[bool, str, str | None]:
    """Fetch a public artifact with network timeouts and visible progress logs.

    Required artifacts are intentionally uncapped because the upstream market
    scorecard can be large. We still avoid hangs through connect/read timeouts
    and the workflow-level timeout. Optional/debug artifacts may pass max_bytes.
    """
    started = dt.datetime.now(dt.timezone.utc)
    cap_label = "unlimited" if not max_bytes or max_bytes <= 0 else str(max_bytes)
    log(f"[FETCH] start {url} timeout={timeout}s max_bytes={cap_label}")
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(8, timeout), stream=True) as response:
            if response.status_code >= 400:
                return False, "", f"HTTP {response.status_code}"
            length_header = response.headers.get("content-length")
            if length_header:
                try:
                    length = int(length_header)
                    if max_bytes and max_bytes > 0 and length > max_bytes:
                        return False, "", f"artifact too large: {length} bytes > {max_bytes}"
                    log(f"[FETCH] length {url} bytes={length}")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            next_progress = 25 * 1024 * 1024
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes and max_bytes > 0 and total > max_bytes:
                    return False, "", f"artifact exceeded size cap: {total} bytes > {max_bytes}"
                if total >= next_progress:
                    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                    log(f"[FETCH] progress {url} bytes={total} elapsed={elapsed:.1f}s")
                    next_progress += 25 * 1024 * 1024
                chunks.append(chunk)
            text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            if not text.strip():
                return False, "", "empty response"
            elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            log(f"[FETCH] ok {url} bytes={total} elapsed={elapsed:.1f}s")
            return True, text, None
    except Exception as exc:
        elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        log(f"[FETCH] fail {url} elapsed={elapsed:.1f}s error={exc}")
        return False, "", str(exc)


def parse_json_artifact(text: str, url: str) -> tuple[Any | None, str | None]:
    try:
        return json.loads(text), None
    except Exception as exc:
        return None, f"JSON parse failed for {url}: {exc}"


def parse_csv_artifact(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return [], []
    return [dict(row) for row in reader], list(reader.fieldnames)


def iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)


def parse_cycle_row(d: dict[str, Any], source: str) -> dict[str, Any] | None:
    fit = get_any(d, "fit", "s2_fit", "retention_fit")
    fit = fit if isinstance(fit, dict) else {}
    metrics = get_any(d, "metrics", "summary")
    metrics = metrics if isinstance(metrics, dict) else {}
    topic = fmt_topic(first(
        get_any(d, "topic"), get_any(d, "Topic"), get_any(d, "name"), get_any(d, "label"),
        get_any(d, "category"), get_any(d, "sector"), get_any(d, "channel"), get_any(fit, "topic"),
    ))
    phase = first(get_any(d, "phase"), get_any(d, "Phase"), get_any(d, "verdict"), get_any(d, "status"))
    n_value = first(get_any(d, "N"), get_any(d, "n"), get_any(d, "count"), get_any(d, "rows"), get_any(d, "sample_count"), get_any(metrics, "N"))
    lambda_value = first(
        get_any(d, "lambda_q"), get_any(d, "lambda"), get_any(d, "lambda_hours"), get_any(d, "lambda_q_hours"),
        get_any(d, "tau_hours"), get_any(d, "coherence_hours"), get_any(fit, "lambda_q"), get_any(fit, "tau_hours"),
        get_any(fit, "lambda_hours"), get_any(metrics, "lambda_q"), get_any(metrics, "tau_hours"),
    )
    beta_value = first(get_any(d, "beta"), get_any(d, "Beta"), get_any(fit, "beta"), get_any(metrics, "beta"))
    half_value = first(
        get_any(d, "half"), get_any(d, "Half"), get_any(d, "half_life"), get_any(d, "half_life_hours"),
        get_any(fit, "half_life"), get_any(fit, "half_life_hours"), get_any(metrics, "half_life"),
    )
    # Keep dust strict: do not default missing dust to zero.
    dust_value = first(
        get_any(d, "dust"), get_any(d, "Dust"), get_any(d, "dust_score"), get_any(d, "residual_dust"),
        get_any(fit, "dust"), get_any(fit, "dust_score"), get_any(fit, "residual_dust"), get_any(metrics, "dust"),
    )
    delta_value = first(
        get_any(d, "delta_aic"), get_any(d, "Delta AIC"), get_any(d, "deltaAIC"), get_any(d, "delta_aic_vs_exp"),
        get_any(d, "delta_bic"), get_any(fit, "delta_aic"), get_any(fit, "delta_bic"),
        get_any(fit, "delta_aic_vs_exp"), get_any(metrics, "delta_aic"),
    )
    newest = first(
        get_any(d, "newest"), get_any(d, "peak"), get_any(d, "newest_peak"), get_any(d, "newest / peak"),
        get_any(d, "latest"), get_any(d, "timestamp"), get_any(d, "date"), get_any(d, "as_of"),
    )
    beta = finite_float(beta_value)
    lam_h = duration_to_hours(lambda_value)
    half_h = duration_to_hours(half_value)
    dust = finite_float(dust_value)
    delta = finite_float(delta_value)
    n = finite_float(n_value)
    # A usable cycle row must contain a topic and at least two fit fields, including beta or lambda.
    fit_count = sum(x is not None for x in [beta, lam_h, half_h, dust, delta])
    if not topic or fit_count < 2 or (beta is None and lam_h is None):
        return None
    return {
        "topic": topic,
        "phase": str(phase) if phase is not None else "",
        "n": int(n) if n is not None and n >= 0 else None,
        "newest_peak": str(newest) if newest is not None else "",
        "lambda_hours": lam_h,
        "beta": beta,
        "half_life_hours": half_h,
        "dust": dust,
        "delta_aic": delta,
        "source": source,
    }


def extract_cycle_rows(obj: Any, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for d in iter_dicts(obj):
        row = parse_cycle_row(d, source)
        if not row:
            continue
        sig = (
            row.get("topic"), row.get("phase"), row.get("newest_peak"),
            round(row.get("lambda_hours") or -1, 4), round(row.get("beta") or -1, 4),
            round(row.get("delta_aic") or -9999, 4), row.get("source"),
        )
        if sig in seen:
            continue
        seen.add(sig)
        rows.append(row)
    return rows


def norm_horizon(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return ""
    text = text.replace("horizon", "")
    if text.startswith("h") and text[1:].isdigit():
        return text
    if text.endswith("d") and text[:-1].isdigit():
        return "h" + text[:-1]
    if text.isdigit():
        return "h" + text
    match = re.search(r"(?:^|[_-])h?(\d+)(?:d)?$", text)
    if match:
        return "h" + match.group(1)
    return text


def normalize_model(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    text = re.sub(r"_?h\d+$", "", text).strip("_")
    if "baseline" in text or text in {"base", "price_baseline"}:
        return "baseline"
    if "s2" in text:
        return "s2"
    return text or "unknown"


def parse_model_horizon(row: dict[str, Any]) -> tuple[str, str]:
    mh = first(get_any(row, "model/horizon"), get_any(row, "model_horizon"), get_any(row, "name"))
    model = first(get_any(row, "model"), get_any(row, "track"))
    horizon = first(get_any(row, "horizon"), get_any(row, "target_horizon"), get_any(row, "days"))
    if mh and not model:
        raw = str(mh).strip()
        parts = raw.replace("-", "_").split("_")
        for p in reversed(parts):
            h = norm_horizon(p)
            if h.startswith("h") and h[1:].isdigit():
                horizon = horizon or h
                model = raw[: max(0, raw.lower().rfind(p.lower()))].strip("_- ") or raw
                break
        model = model or raw
    return normalize_model(model), norm_horizon(horizon)


def parse_aggregate_score_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    model, horizon = parse_model_horizon(row)
    if model not in {"baseline", "s2"} or not horizon:
        return None
    realized = finite_float(first(get_any(row, "realized rows"), get_any(row, "realized_rows"), get_any(row, "held-out rows"), get_any(row, "held_out_rows"), get_any(row, "rows"), get_any(row, "n"), get_any(row, "count")))
    hit = finite_float(first(get_any(row, "direction hit"), get_any(row, "direction_hit"), get_any(row, "hit_rate"), get_any(row, "accuracy"), get_any(row, "direction_accuracy"), get_any(row, "hit")))
    coverage = finite_float(first(get_any(row, "coverage"), get_any(row, "signal_coverage")))
    pnl = finite_float(first(get_any(row, "PnL proxy"), get_any(row, "pnl_proxy"), get_any(row, "pnl"), get_any(row, "mean_return"), get_any(row, "avg_return"), get_any(row, "paper_return")))
    mae = finite_float(first(get_any(row, "MAE"), get_any(row, "mae"), get_any(row, "mean_abs_error"), get_any(row, "mean_absolute_error")))
    rmse = finite_float(first(get_any(row, "RMSE"), get_any(row, "rmse")))
    # Aggregate rows must contain at least a hit/pnl/mae metric. Do not accept live rows as aggregate rows.
    metric_count = sum(x is not None for x in [hit, pnl, mae, rmse])
    if metric_count < 2:
        return None
    if hit is not None and not (0 <= hit <= 1):
        return None
    return {
        "model": model,
        "horizon": horizon,
        "realized_rows": int(realized) if realized is not None and realized >= 0 else None,
        "direction_hit": hit,
        "coverage": coverage,
        "pnl_proxy": pnl,
        "mae": mae,
        "rmse": rmse,
        "source": source,
        "source_type": "aggregate",
    }


def direction_from_text(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"up", "buy", "long", "positive", "+", "1", "true", "hit"}:
        return 1
    if text in {"down", "sell", "short", "negative", "-", "-1", "false", "miss"}:
        return -1
    return None


def aggregate_realized_state(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Aggregate individual realized prediction rows if the schema supports it.

    This is intentionally strict: rows need model, horizon, a real/predicted return or correctness field.
    Live/pending rows are ignored.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        model, horizon = parse_model_horizon(row)
        if model not in {"baseline", "s2"} or not horizon:
            continue
        status = str(first(get_any(row, "status"), get_any(row, "state"), "") or "").lower()
        if "pending" in status or "live" in status:
            continue
        actual_ret = finite_float(first(get_any(row, "actual_return"), get_any(row, "realized_return"), get_any(row, "future_return"), get_any(row, "return_actual")))
        pred_ret = finite_float(first(get_any(row, "predicted_return"), get_any(row, "pred_return"), get_any(row, "model_return"), get_any(row, "s2_pred_return"), get_any(row, "baseline_pred_return")))
        pnl = finite_float(first(get_any(row, "pnl_proxy"), get_any(row, "paper_return"), get_any(row, "realized_pnl")))
        correct_raw = first(get_any(row, "correct"), get_any(row, "direction_correct"), get_any(row, "hit"), get_any(row, "baseline_correct"), get_any(row, "s2_correct"))
        correct: bool | None = None
        if isinstance(correct_raw, bool):
            correct = correct_raw
        elif correct_raw is not None:
            txt = str(correct_raw).strip().lower()
            if txt in {"true", "1", "yes", "y", "hit"}:
                correct = True
            elif txt in {"false", "0", "no", "n", "miss"}:
                correct = False
        pred_dir = direction_from_text(first(get_any(row, "predicted_direction"), get_any(row, "direction"), get_any(row, "trade_signal"), get_any(row, "prediction")))
        actual_dir = direction_from_text(first(get_any(row, "actual_direction"), get_any(row, "realized_direction")))
        if actual_dir is None and actual_ret is not None:
            actual_dir = 1 if actual_ret >= 0 else -1
        if pred_dir is None and pred_ret is not None:
            pred_dir = 1 if pred_ret >= 0 else -1
        if correct is None and pred_dir is not None and actual_dir is not None:
            correct = pred_dir == actual_dir
        if pnl is None and actual_ret is not None and pred_dir is not None:
            pnl = actual_ret * pred_dir
        if correct is None and pnl is None and actual_ret is None:
            continue
        groups[(model, horizon)].append({"correct": correct, "pnl": pnl, "actual_ret": actual_ret, "pred_ret": pred_ret})
    out: list[dict[str, Any]] = []
    for (model, horizon), rs in groups.items():
        if len(rs) < 20:
            continue
        corrects = [1.0 if r["correct"] else 0.0 for r in rs if r.get("correct") is not None]
        pnls = [r["pnl"] for r in rs if r.get("pnl") is not None and math.isfinite(float(r["pnl"]))]
        errors = [abs(r["actual_ret"] - r["pred_ret"]) for r in rs if r.get("actual_ret") is not None and r.get("pred_ret") is not None]
        if not corrects and not pnls:
            continue
        out.append({
            "model": model,
            "horizon": horizon,
            "realized_rows": len(rs),
            "direction_hit": mean(corrects),
            "coverage": None,
            "pnl_proxy": mean(pnls),
            "mae": mean(errors),
            "rmse": math.sqrt(mean([e * e for e in errors])) if errors else None,
            "source": source,
            "source_type": "aggregated_realized_state",
        })
    return out


def extract_scorecard_rows(rows: list[dict[str, Any]], source: str, fieldnames: list[str]) -> tuple[list[dict[str, Any]], str]:
    norm_fields = {normalize_key(f) for f in fieldnames}
    aggregate_hint = bool({"model_horizon", "model/horizon", "direction_hit", "direction hit", "pnl_proxy", "pnl proxy"} & norm_fields)
    # First try explicit aggregate rows. If many rows are accepted, this is probably not an aggregate scorecard.
    aggregate_rows = [r for r in (parse_aggregate_score_row(row, source) for row in rows) if r]
    if aggregate_rows and len(aggregate_rows) <= 100:
        return aggregate_rows, "aggregate_scorecard"
    # If the file is not a compact aggregate, only aggregate realized state if sufficient fields exist.
    state_rows = aggregate_realized_state(rows, source)
    if state_rows:
        return state_rows, "aggregated_realized_state"
    if aggregate_rows:
        return [], "rejected_rowwise_score_like_file"
    return [], "schema_not_recognized"




class FastAggregator:
    def __init__(self):
        self.data: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {
            "rows": 0.0, "hit_n": 0.0, "hit_sum": 0.0, "pnl_n": 0.0, "pnl_sum": 0.0,
            "mae_n": 0.0, "mae_sum": 0.0, "rmse_n": 0.0, "rmse_sq_sum": 0.0,
            "coverage_n": 0.0, "coverage_sum": 0.0,
        })

    def add(self, model: str, horizon: str, weight: float, hit: float | None = None,
            pnl: float | None = None, mae: float | None = None, rmse: float | None = None,
            coverage: float | None = None) -> None:
        if model not in {"baseline", "s2"} or not horizon:
            return
        if not weight or weight <= 0 or not math.isfinite(weight):
            weight = 1.0
        d = self.data[(model, horizon)]
        d["rows"] += weight
        if hit is not None and math.isfinite(hit):
            if 1.0 < hit <= 100.0:
                hit = hit / 100.0
            if 0.0 <= hit <= 1.0:
                d["hit_n"] += weight
                d["hit_sum"] += hit * weight
        if pnl is not None and math.isfinite(pnl):
            d["pnl_n"] += weight
            d["pnl_sum"] += pnl * weight
        if mae is not None and math.isfinite(mae):
            if 1.0 < mae <= 100.0:
                mae = mae / 100.0
            d["mae_n"] += weight
            d["mae_sum"] += mae * weight
        if rmse is not None and math.isfinite(rmse):
            if 1.0 < rmse <= 100.0:
                rmse = rmse / 100.0
            d["rmse_n"] += weight
            d["rmse_sq_sum"] += (rmse * rmse) * weight
        if coverage is not None and math.isfinite(coverage):
            if 1.0 < coverage <= 100.0:
                coverage = coverage / 100.0
            if 0.0 <= coverage <= 1.0:
                d["coverage_n"] += weight
                d["coverage_sum"] += coverage * weight

    def rows(self, source: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (model, horizon), d in sorted(self.data.items(), key=lambda kv: (int(kv[0][1][1:]) if kv[0][1].startswith('h') and kv[0][1][1:].isdigit() else 999, kv[0][0])):
            if d["rows"] <= 0:
                continue
            hit = d["hit_sum"] / d["hit_n"] if d["hit_n"] else None
            pnl = d["pnl_sum"] / d["pnl_n"] if d["pnl_n"] else None
            mae = d["mae_sum"] / d["mae_n"] if d["mae_n"] else None
            rmse = math.sqrt(d["rmse_sq_sum"] / d["rmse_n"]) if d["rmse_n"] else None
            coverage = d["coverage_sum"] / d["coverage_n"] if d["coverage_n"] else None
            if sum(x is not None for x in [hit, pnl, mae, rmse]) < 2:
                continue
            out.append({
                "model": model,
                "horizon": horizon,
                "realized_rows": int(round(d["rows"])),
                "direction_hit": hit,
                "coverage": coverage,
                "pnl_proxy": pnl,
                "mae": mae,
                "rmse": rmse,
                "source": source,
                "source_type": "fast_streamed_scorecard",
            })
        return out


def header_lookup(fieldnames: list[str]) -> dict[str, str]:
    return {normalize_key(name): name for name in fieldnames or []}


def get_field(row: dict[str, Any], fmap: dict[str, str], aliases: list[str]) -> Any:
    for alias in aliases:
        key = normalize_key(alias)
        real = fmap.get(key)
        if real is not None:
            return row.get(real)
    return None


def parse_model_horizon_fast(row: dict[str, Any], fmap: dict[str, str]) -> tuple[str, str]:
    mh = get_field(row, fmap, ["model/horizon", "model_horizon", "model horizon", "name", "track_horizon"])
    model = get_field(row, fmap, ["model", "track", "model_name"])
    horizon = get_field(row, fmap, ["horizon", "target_horizon", "days", "h"])
    if mh and (not model or not horizon):
        m2, h2 = parse_model_horizon({"model/horizon": mh})
        model = model or m2
        horizon = horizon or h2
    return normalize_model(model), norm_horizon(horizon)


def truthy_hit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "hit", "correct", "1"}:
        return 1.0
    if text in {"false", "no", "n", "miss", "wrong", "0"}:
        return 0.0
    v = finite_float(value)
    if v is None:
        return None
    if 1.0 < v <= 100.0:
        v = v / 100.0
    if 0.0 <= v <= 1.0:
        return v
    return None


def parse_scorecard_csv_fast(text: str, source: str) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Fast one-pass parser for large scorecard CSVs.

    Avoids building a full list of 80MB+ rows and avoids the slow generic schema
    probe on every row. Live/pending rows are ignored. Returned rows are
    aggregate model/horizon scores only.
    """
    try:
        dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    f = io.StringIO(text)
    reader = csv.DictReader(f, dialect=dialect)
    fields = list(reader.fieldnames or [])
    fmap = header_lookup(fields)
    agg = FastAggregator()
    raw = 0
    parsed_like = 0
    has_realized_weight = any(normalize_key(x) in fmap for x in ["realized_rows", "realized rows", "held_out_rows", "held-out rows", "n", "count"])
    for row in reader:
        raw += 1
        if raw % 100000 == 0:
            log(f"[PARSE] {source} rows={raw} aggregates={len(agg.data)}")
        model, horizon = parse_model_horizon_fast(row, fmap)
        if model not in {"baseline", "s2"} or not horizon:
            continue
        status = str(get_field(row, fmap, ["status", "state", "prediction_status", "realization_status"]) or "").lower()
        if "pending" in status or "live" in status:
            continue
        weight = finite_float(get_field(row, fmap, ["realized rows", "realized_rows", "held-out rows", "held_out_rows", "rows", "n", "count"])) if has_realized_weight else None
        if weight is None or weight <= 0:
            weight = 1.0
        hit = truthy_hit(get_field(row, fmap, ["direction hit", "direction_hit", "hit_rate", "accuracy", "direction_accuracy", "hit", "correct", "direction_correct"]))
        pnl = finite_float(get_field(row, fmap, ["PnL proxy", "pnl_proxy", "pnl", "mean_return", "avg_return", "paper_return", "realized_pnl"]))
        mae = finite_float(get_field(row, fmap, ["MAE", "mae", "mean_abs_error", "mean_absolute_error", "abs_error"]))
        rmse = finite_float(get_field(row, fmap, ["RMSE", "rmse", "root_mean_square_error"]))
        coverage = finite_float(get_field(row, fmap, ["coverage", "signal_coverage"]))
        if hit is None:
            pred_dir = direction_from_text(get_field(row, fmap, ["predicted_direction", "direction", "trade_signal", "prediction", "signal"]))
            actual_dir = direction_from_text(get_field(row, fmap, ["actual_direction", "realized_direction"]))
            actual_ret = finite_float(get_field(row, fmap, ["actual_return", "realized_return", "future_return", "return_actual"]))
            pred_ret = finite_float(get_field(row, fmap, ["predicted_return", "pred_return", "model_return", "s2_pred_return", "baseline_pred_return"]))
            if actual_dir is None and actual_ret is not None:
                actual_dir = 1 if actual_ret >= 0 else -1
            if pred_dir is None and pred_ret is not None:
                pred_dir = 1 if pred_ret >= 0 else -1
            if pred_dir is not None and actual_dir is not None:
                hit = 1.0 if pred_dir == actual_dir else 0.0
            if pnl is None and actual_ret is not None and pred_dir is not None:
                pnl = actual_ret * pred_dir
            if mae is None and actual_ret is not None and pred_ret is not None:
                mae = abs(actual_ret - pred_ret)
        if sum(x is not None for x in [hit, pnl, mae, rmse]) < 1:
            continue
        parsed_like += 1
        # If explicit aggregate rows exist, weight by realized rows. If not, each scored row is one sample.
        agg.add(model, horizon, weight, hit=hit, pnl=pnl, mae=mae, rmse=rmse, coverage=coverage)
    rows = agg.rows(source)
    mode_name = "fast_streamed_scorecard" if rows else "schema_not_recognized_fast"
    log(f"[PARSE] done {source} raw_rows={raw} parsed_metric_rows={parsed_like} aggregate_rows={len(rows)}")
    return rows, mode_name, raw, fields


def context_from_key(key: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """Infer model/horizon context from nested JSON keys such as baseline_h5 or h1."""
    next_ctx = dict(ctx)
    text = str(key or "").strip()
    if not text:
        return next_ctx
    model, horizon = parse_model_horizon({"model/horizon": text})
    if model in {"baseline", "s2"}:
        next_ctx.setdefault("model", model)
    if horizon:
        next_ctx.setdefault("horizon", horizon)
    key_model = normalize_model(text)
    if key_model in {"baseline", "s2"}:
        next_ctx.setdefault("model", key_model)
    key_h = norm_horizon(text)
    if key_h.startswith("h") and key_h[1:].isdigit():
        next_ctx.setdefault("horizon", key_h)
    return next_ctx


def iter_dicts_with_context(obj: Any, ctx: dict[str, Any] | None = None):
    """Yield dictionaries with inherited model/horizon context for nested model_comparison JSON."""
    ctx = ctx or {}
    if isinstance(obj, dict):
        merged = dict(obj)
        for k, v in ctx.items():
            merged.setdefault(k, v)
        yield merged
        for key, value in obj.items():
            yield from iter_dicts_with_context(value, context_from_key(key, ctx))
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts_with_context(item, ctx)


def extract_model_comparison(obj: Any, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    # First pass: ordinary rows and context-inherited nested metrics.
    for d in iter_dicts_with_context(obj):
        row = parse_aggregate_score_row(d, source)
        if not row:
            continue
        sig = (row["model"], row["horizon"], row.get("direction_hit"), row.get("pnl_proxy"), row.get("mae"), row.get("source"))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(row)
    return out


def extract_live_predictions(rows: list[dict[str, Any]], source: str, max_rows: int = 250) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        ticker = first(get_any(row, "ticker"), get_any(row, "symbol"))
        horizon = norm_horizon(first(get_any(row, "horizon"), get_any(row, "target_horizon"), get_any(row, "days")))
        if not ticker:
            continue
        pred = first(get_any(row, "prediction"), get_any(row, "direction"), get_any(row, "trade_signal"), get_any(row, "action"), get_any(row, "side"))
        prob = finite_float(first(get_any(row, "probability"), get_any(row, "probability_up"), get_any(row, "confidence"), get_any(row, "p_up")))
        # Live prediction confidence sometimes arrives as 50.12 meaning 50.12%,
        # while finite_float("50.12%") returns 0.5012. Normalize display values to 0..1.
        if prob is not None:
            if 1.0 < prob <= 100.0:
                prob = prob / 100.0
            elif 100.0 < prob <= 10000.0:
                prob = prob / 10000.0
        exp_ret = finite_float(first(get_any(row, "expected_return"), get_any(row, "predicted_return"), get_any(row, "return"), get_any(row, "pnl_proxy")))
        close = finite_float(first(get_any(row, "asof_close"), get_any(row, "last_close"), get_any(row, "close")))
        asof = first(get_any(row, "asof_date"), get_any(row, "date"), get_any(row, "last_date"))
        out.append({
            "ticker": str(ticker),
            "horizon": horizon,
            "prediction": str(pred or ""),
            "probability": prob,
            "expected_return": exp_ret,
            "asof_date": str(asof or ""),
            "asof_close": close,
            "source": source,
        })
    return out


def summarize_topics(rows: list[dict[str, Any]], beta_floor: float) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["topic"]].append(row)
    summaries: list[dict[str, Any]] = []
    for topic, rs in grouped.items():
        betas = [r.get("beta") for r in rs if r.get("beta") is not None]
        lambdas = [r.get("lambda_hours") for r in rs if r.get("lambda_hours") is not None]
        halfs = [r.get("half_life_hours") for r in rs if r.get("half_life_hours") is not None]
        dusts_all = [r.get("dust") for r in rs if r.get("dust") is not None]
        # If the source supplies both zero and non-zero dust for same topic, prefer non-zero fitted dust values.
        dusts_pos = [d for d in dusts_all if d is not None and d > 1e-9]
        dusts = dusts_pos if dusts_pos else dusts_all
        deltas = [r.get("delta_aic") for r in rs if r.get("delta_aic") is not None]
        phases = [str(r.get("phase") or "").lower() for r in rs]
        s2_likely = sum("s2 likely" in p for p in phases)
        s2_any = sum("s2" in p for p in phases)
        beta_floor_count = sum(abs(float(b) - beta_floor) <= 1e-9 for b in betas)
        beta_floor_share = beta_floor_count / len(betas) if betas else None
        delta_m = median(deltas)
        dust_m = median(dusts)
        likely_share = s2_likely / len(rs) if rs else 0.0
        support = max(0.0, min(1.0, (delta_m or 0.0) / 25.0))
        dust_quality = None if dust_m is None else 1.0 - max(0.0, min(1.0, dust_m / 0.50))
        sample_quality = max(0.0, min(1.0, math.log10(len(rs) + 1) / 2.0))
        retained_pressure = 100.0 * (
            0.42 * support
            + 0.22 * (dust_quality if dust_quality is not None else 0.50)
            + 0.24 * likely_share
            + 0.12 * sample_quality
        )
        beta_verdict = "floor-locked" if beta_floor_share is not None and beta_floor_share >= 0.75 else "varied"
        dust_audit = "ok"
        if dusts_all and not dusts_pos:
            dust_audit = "all-zero-or-missing"
        elif dusts_all and len(dusts_pos) / len(dusts_all) < 0.25:
            dust_audit = "mostly-zero"
        summaries.append({
            "topic": topic,
            "cycle_rows": len(rs),
            "s2_likely_rows": s2_likely,
            "s2_any_rows": s2_any,
            "s2_likely_share": likely_share,
            "lambda_median_hours": median(lambdas),
            "half_life_median_hours": median(halfs),
            "beta_mode": mode(betas),
            "beta_median": median(betas),
            "beta_floor_share": beta_floor_share,
            "beta_verdict": beta_verdict,
            "dust_median": dust_m,
            "dust_nonzero_share": len(dusts_pos) / len(dusts_all) if dusts_all else None,
            "dust_audit": dust_audit,
            "delta_aic_median": delta_m,
            "retained_pressure_score": retained_pressure,
            "sources": sorted({r.get("source") for r in rs if r.get("source")}),
        })
    summaries.sort(key=lambda x: (x.get("retained_pressure_score") or 0.0), reverse=True)
    return summaries


def group_scores(rows: list[dict[str, Any]], preferred_sources: list[str]) -> list[dict[str, Any]]:
    """Deduplicate aggregate score rows by source priority + model/horizon."""
    priority = {src: idx for idx, src in enumerate(preferred_sources)}
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["model"], r["horizon"])
        prev = chosen.get(key)
        if prev is None or priority.get(r.get("source", ""), 99) < priority.get(prev.get("source", ""), 99):
            chosen[key] = r
    return list(chosen.values())


def summarize_market(score_rows: list[dict[str, Any]], source_label: str) -> list[dict[str, Any]]:
    by_h: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        by_h[row["horizon"]].append(row)
    out: list[dict[str, Any]] = []
    for horizon, rows in by_h.items():
        base = next((r for r in rows if r["model"] == "baseline"), None)
        s2 = next((r for r in rows if r["model"] == "s2"), None)
        best = max(rows, key=lambda r: ((r.get("direction_hit") if r.get("direction_hit") is not None else -999), (r.get("pnl_proxy") if r.get("pnl_proxy") is not None else -999)))
        row = {
            "horizon": horizon,
            "models": rows,
            "score_source": source_label,
            "best_model": best.get("model"),
            "best_hit": best.get("direction_hit"),
            "best_pnl": best.get("pnl_proxy"),
            "best_mae": best.get("mae"),
            "realized_rows": max([r.get("realized_rows") or 0 for r in rows]) or None,
        }
        if base and s2:
            row.update({
                "baseline_hit": base.get("direction_hit"),
                "s2_hit": s2.get("direction_hit"),
                "delta_hit": (s2.get("direction_hit") - base.get("direction_hit")) if s2.get("direction_hit") is not None and base.get("direction_hit") is not None else None,
                "baseline_pnl": base.get("pnl_proxy"),
                "s2_pnl": s2.get("pnl_proxy"),
                "delta_pnl": (s2.get("pnl_proxy") - base.get("pnl_proxy")) if s2.get("pnl_proxy") is not None and base.get("pnl_proxy") is not None else None,
                "baseline_mae": base.get("mae"),
                "s2_mae": s2.get("mae"),
                "delta_mae": (s2.get("mae") - base.get("mae")) if s2.get("mae") is not None and base.get("mae") is not None else None,
            })
        out.append(row)
    def hsort(x: dict[str, Any]) -> int:
        h = x.get("horizon", "")
        return int(h[1:]) if isinstance(h, str) and h.startswith("h") and h[1:].isdigit() else 999
    out.sort(key=hsort)
    return out


def build_coupling(topic_rows: list[dict[str, Any]], horizon_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostic = {norm_horizon(h) for h in config["analysis"].get("diagnostic_horizons", [])}
    primary = {norm_horizon(h) for h in config["analysis"].get("primary_horizons", [])}
    min_pressure = float(config["analysis"].get("min_pressure_for_signal", 10.0))
    out: list[dict[str, Any]] = []
    for t in topic_rows:
        pressure = t.get("retained_pressure_score")
        if pressure is None:
            continue
        for h in horizon_rows:
            horizon = h.get("horizon")
            dh = h.get("delta_hit")
            dp = h.get("delta_pnl")
            if dh is None and dp is None:
                continue
            is_diag = horizon in diagnostic
            if is_diag:
                status = "dust diagnostic"
            elif horizon in primary and pressure >= min_pressure and (dh or 0) > 0 and (dp or 0) > 0:
                status = "candidate coupling"
            elif horizon in primary and ((dh or 0) > 0 or (dp or 0) > 0):
                status = "mixed coupling"
            elif horizon in primary:
                status = "not confirmed"
            else:
                status = "secondary horizon"
            # score is a research ranking, not a trading claim.
            edge = 0.0
            if dh is not None:
                edge += max(-0.20, min(0.20, dh)) * 3.0
            if dp is not None:
                edge += max(-0.05, min(0.05, dp)) * 10.0
            if is_diag:
                edge *= 0.0
            score = pressure * edge
            out.append({
                "topic": t.get("topic"),
                "horizon": horizon,
                "retained_pressure_score": pressure,
                "topic_lambda_hours": t.get("lambda_median_hours"),
                "topic_beta_mode": t.get("beta_mode"),
                "topic_beta_floor_share": t.get("beta_floor_share"),
                "topic_dust_median": t.get("dust_median"),
                "topic_dust_audit": t.get("dust_audit"),
                "topic_delta_aic_median": t.get("delta_aic_median"),
                "delta_hit": dh,
                "delta_pnl": dp,
                "realized_rows": h.get("realized_rows"),
                "score_source": h.get("score_source"),
                "coupling_score": score,
                "status": status,
            })
    out.sort(key=lambda r: (r.get("status") != "candidate coupling", -(r.get("coupling_score") or -9999)))
    return out


def load_config() -> dict[str, Any]:
    with CONFIG.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    fetch_cfg = cfg.get("fetch", {}) or {}
    fetch_timeout = int(fetch_cfg.get("timeout_seconds", 18))
    raw_required_cap = fetch_cfg.get("max_bytes_required", 0)
    max_bytes_required = None if raw_required_cap in (None, "", 0, "0", "none", "unlimited") else int(raw_required_cap)
    max_bytes_optional = int(fetch_cfg.get("max_bytes_optional", 5_000_000))
    log("[BUILD] strict public-artifact bundle starting")
    log(f"[BUILD] timeout={fetch_timeout}s max_required={max_bytes_required if max_bytes_required else 'unlimited'} max_optional={max_bytes_optional}")
    health: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []
    scorecard_rows: list[dict[str, Any]] = []
    model_comparison_rows: list[dict[str, Any]] = []
    live_predictions: list[dict[str, Any]] = []

    for artifact in cfg["cycle"].get("required_artifacts", []):
        url = build_url(cfg["cycle"]["base_url"], artifact["path"])
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_required)
        record = {"group": "cycle", "kind": artifact["kind"], "url": url, "ok": ok, "rows": 0, "error": err}
        if ok:
            obj, perr = parse_json_artifact(text, url)
            if perr:
                record.update({"ok": False, "error": perr})
            else:
                rows = extract_cycle_rows(obj, artifact["kind"])
                cycle_rows.extend(rows)
                record["rows"] = len(rows)
                if not rows:
                    record["warning"] = "JSON loaded but no usable cycle rows recognized"
        health.append(record)

    for artifact in cfg["market"].get("required_artifacts", []):
        url = build_url(cfg["market"]["base_url"], artifact["path"])
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_required)
        kind = artifact["kind"]
        record = {"group": "market", "kind": kind, "url": url, "ok": ok, "rows": 0, "raw_rows": 0, "error": err}
        if ok:
            if artifact["path"].lower().endswith(".csv"):
                if kind in {"prediction_scorecard", "prediction_state"}:
                    parsed, mode_name, raw_rows, fields = parse_scorecard_csv_fast(text, kind)
                    record["raw_rows"] = raw_rows
                    record["fields"] = fields[:20]
                    scorecard_rows.extend(parsed)
                    record["rows"] = len(parsed)
                    record["schema_mode"] = mode_name
                    if not parsed:
                        record["warning"] = f"{kind} loaded but no scored aggregate rows recognized"
                else:
                    rows, fields = parse_csv_artifact(text)
                    record["raw_rows"] = len(rows)
                    record["fields"] = fields[:20]
                    if kind == "live_predictions":
                        parsed = extract_live_predictions(rows, kind)
                        live_predictions.extend(parsed)
                        record["rows"] = len(parsed)
                        record["schema_mode"] = "live_state_only"
                    else:
                        record["schema_mode"] = "loaded_not_used_for_score"
            elif artifact["path"].lower().endswith(".json"):
                obj, perr = parse_json_artifact(text, url)
                if perr:
                    record.update({"ok": False, "error": perr})
                else:
                    parsed = extract_model_comparison(obj, kind)
                    model_comparison_rows.extend(parsed)
                    record["rows"] = len(parsed)
                    record["schema_mode"] = "model_comparison"
                    if not parsed:
                        record["warning"] = "JSON loaded but no aggregate model comparison rows recognized"
        health.append(record)

    for artifact in cfg["market"].get("optional_artifacts", []):
        url = build_url(cfg["market"]["base_url"], artifact["path"])
        if artifact.get("disabled_by_default"):
            health.append({
                "group": "market_optional",
                "kind": artifact["kind"],
                "url": url,
                "ok": False,
                "rows": 0,
                "error": "disabled_by_default",
                "note": artifact.get("reason", "optional large artifact skipped"),
            })
            log(f"[SKIP] optional disabled {url}")
            continue
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_optional)
        record = {"group": "market_optional", "kind": artifact["kind"], "url": url, "ok": ok, "rows": 0, "error": err}
        if ok and artifact["path"].lower().endswith(".json"):
            obj, perr = parse_json_artifact(text, url)
            if perr:
                record.update({"ok": False, "error": perr})
            elif isinstance(obj, dict):
                record["metadata"] = {k: obj.get(k) for k in ["generated_at_utc", "latest_market_date", "requested_tickers", "successful_tickers", "quote_rows", "live_predictions", "prior_predictions_scored", "total_realized_scores"] if k in obj}
        health.append(record)

    beta_floor = float(cfg["analysis"].get("beta_floor_watch", 0.35))
    topic_summaries = summarize_topics(cycle_rows, beta_floor)
    # Prefer realized live scorecard/state over backtest comparison. Use model_comparison only as a separate reference.
    scorecard_dedup = group_scores(scorecard_rows, ["prediction_scorecard", "prediction_state"])
    comparison_dedup = group_scores(model_comparison_rows, ["model_comparison"])
    selected_scores = scorecard_dedup if scorecard_dedup else comparison_dedup
    selected_source = "live_scorecard" if scorecard_dedup else ("backtest_model_comparison" if comparison_dedup else "none")
    market_horizons = summarize_market(selected_scores, selected_source)
    backtest_horizons = summarize_market(comparison_dedup, "backtest_model_comparison") if comparison_dedup else []
    coupling = build_coupling(topic_summaries, market_horizons, cfg) if market_horizons else []
    betas = [r.get("beta") for r in cycle_rows if r.get("beta") is not None]
    beta_floor_share = sum(abs(float(b) - beta_floor) <= 1e-9 for b in betas) / len(betas) if betas else None
    dust_values = [r.get("dust") for r in cycle_rows if r.get("dust") is not None]
    dust_nonzero_values = [d for d in dust_values if d and d > 1e-9]

    candidate_count = sum(1 for r in coupling if r.get("status") == "candidate coupling")
    primary_nonh1 = [h for h in market_horizons if h.get("horizon") != "h1"]
    verdict = "waiting for scored market artifacts"
    if market_horizons and candidate_count:
        verdict = "candidate coupling found"
    elif market_horizons:
        verdict = "sources loaded; no confirmed advanced coupling"

    live_horizon_counts = []
    if live_predictions:
        hc = Counter((r.get("horizon") or "unknown") for r in live_predictions)
        live_horizon_counts = [{"horizon": h, "rows": n} for h, n in sorted(hc.items(), key=lambda kv: int(kv[0][1:]) if isinstance(kv[0], str) and kv[0].startswith("h") and kv[0][1:].isdigit() else 999)]

    chart_status = {
        "coupling_chart": "scored_coupling" if any(r.get("status") != "dust diagnostic" for r in coupling) else ("cycle_pressure_only" if topic_summaries else "empty"),
        "horizon_chart": "scored_horizons" if market_horizons else ("backtest_horizons" if backtest_horizons else ("live_horizon_counts" if live_horizon_counts else "empty")),
        "score_source": selected_source,
    }

    bundle = {
        "generated_at": now_utc(),
        "strict_source_policy": True,
        "source_policy": "public GitHub Pages JSON/CSV artifacts only; no dummy rows; no page scraping; no zero-fill coupling; live predictions are not used for hit/PnL",
        "source_health": health,
        "summary": {
            "cycle_rows": len(cycle_rows),
            "topics": len(topic_summaries),
            "score_rows": len(scorecard_dedup),
            "backtest_rows": len(comparison_dedup),
            "market_horizons": len(market_horizons),
            "backtest_horizons": len(backtest_horizons),
            "live_prediction_rows": len(live_predictions),
            "coupling_rows": len(coupling),
            "candidate_coupling_rows": candidate_count,
            "score_source": selected_source,
            "beta_floor_watch": beta_floor,
            "beta_floor_share": beta_floor_share,
            "beta_mode": mode(betas),
            "beta_median": median(betas),
            "dust_rows": len(dust_values),
            "dust_nonzero_share": len(dust_nonzero_values) / len(dust_values) if dust_values else None,
            "verdict": verdict,
            "primary_horizons_loaded": [h.get("horizon") for h in primary_nonh1],
            "chart_status": chart_status,
        },
        "chart_status": chart_status,
        "live_horizon_counts": live_horizon_counts,
        "topic_summaries": topic_summaries,
        "market_horizons": market_horizons,
        "backtest_horizons": backtest_horizons,
        "coupling_rows": coupling,
        "live_predictions": live_predictions[:250],
        "raw_cycle_rows_preview": cycle_rows[:60],
        "raw_score_rows_preview": selected_scores[:60],
    }
    BUNDLE_PATH.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    HEALTH_PATH.write_text(json.dumps({"generated_at": bundle["generated_at"], "source_health": health}, indent=2), encoding="utf-8")
    print(f"[OK] wrote {BUNDLE_PATH}")
    print(f"[INFO] cycle_rows={len(cycle_rows)} score_rows={len(scorecard_dedup)} backtest_rows={len(comparison_dedup)} live_predictions={len(live_predictions)} coupling_rows={len(coupling)}")
    print(f"[INFO] score_source={selected_source} verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
