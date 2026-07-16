"""
serving/web_dashboard.py

Advanced interactive web dashboard for the Social Listening pipeline.

Everything is powered by the unified record-level table `fct_comment_drillthrough`
(record x entity x theme grain) so that EVERY view can be filtered by brand entity,
platform, record type, language and an arbitrary date range — with day / week / month
time bucketing. Engagement metrics join back to `social_raw.records`; topic modelling
comes from `social_raw.topics`; period-over-period trend alerts from `social_raw.trends`.

Usage:
    python -m serving.web_dashboard            # http://localhost:8000
    python -m serving.web_dashboard --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from utils.logging_config import get_logger

log = get_logger(__name__)

# ── ClickHouse ────────────────────────────────────────────────────────────────
CH_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CH_PORT = os.getenv("CLICKHOUSE_PORT", "8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "labelvie2024")
CH_URL  = f"http://{CH_HOST}:{CH_PORT}/"
GOLD    = "social_gold"
RAW     = "social_raw"
DT      = f"{GOLD}.fct_comment_drillthrough"   # the unified record-level table

app = FastAPI(title="Social Listening — LabelVie", docs_url=None, redoc_url=None)


def _ch(sql: str) -> list[dict]:
    try:
        resp = requests.get(
            CH_URL,
            params={"query": sql + " FORMAT JSONEachRow",
                    "user": CH_USER, "password": CH_PASS},
            timeout=30,
        )
        resp.raise_for_status()
        return [json.loads(ln) for ln in resp.text.strip().splitlines() if ln.strip()]
    except Exception as e:
        log.warning("ch_query.failed", extra={"error": str(e), "sql": sql[:160]})
        return []


# ── safe filter handling ──────────────────────────────────────────────────────
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _esc(v: str) -> str:
    return v.replace("'", "''")


def _csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip() and v.strip().lower() != "all"]


def _in(col: str, values: list[str]) -> Optional[str]:
    if not values:
        return None
    quoted = ", ".join(f"'{_esc(v)}'" for v in values)
    return f"{col} IN ({quoted})"


def _bucket(gran: str) -> str:
    g = (gran or "day").lower()
    if g == "month":
        return "toStartOfMonth(record_date)"
    if g == "week":
        return "toStartOfWeek(record_date, 1)"   # Monday
    return "record_date"


class F:
    """Parsed + validated filter set, builds a WHERE clause for the DT table."""
    def __init__(self, start, end, platforms, entities, record_types, langs,
                 source_brands=None):
        self.start = start if start and _DATE_RE.match(start) else None
        self.end   = end   if end   and _DATE_RE.match(end)   else None
        self.platforms    = _csv(platforms)
        self.entities     = _csv(entities)
        self.record_types = _csv(record_types)
        self.langs        = _csv(langs)
        # Page source ("terrain") d'où le record a été collecté — axe indépendant
        # de la marque mentionnée (entities).
        self.source_brands = _csv(source_brands)

    def where(self, extra: str = "") -> str:
        parts = ["record_date IS NOT NULL"]
        if self.start:
            parts.append(f"record_date >= '{self.start}'")
        if self.end:
            parts.append(f"record_date <= '{self.end}'")
        for clause in (
            _in("platform", self.platforms),
            _in("entity", self.entities),
            _in("source_brand", self.source_brands),
            _in("record_type", self.record_types),
            _in("language", self.langs),
        ):
            if clause:
                parts.append(clause)
        if extra:
            parts.append(extra)
        return " AND ".join(parts)

    def prev_window(self):
        """Equal-length window immediately preceding [start, end]."""
        if not (self.start and self.end):
            return None, None
        s = datetime.strptime(self.start, "%Y-%m-%d").date()
        e = datetime.strptime(self.end, "%Y-%m-%d").date()
        span = (e - s).days + 1
        return (s - timedelta(days=span)).isoformat(), (s - timedelta(days=1)).isoformat()


def _filters(start, end, platforms, entities, record_types, langs,
             source_brands=None) -> F:
    return F(start, end, platforms, entities, record_types, langs, source_brands)


# Reusable WHERE fragments (kept as constants to avoid nested-quote f-string issues)
THEME_NZ = "theme != ''"
LANG_NN  = "language IS NOT NULL"

# Record-level aggregate expressions (avoid theme-row inflation via uniqExact on record_id)
REC      = "uniqExact(record_id)"
POS      = "uniqExactIf(record_id, overall_sentiment = 'Positif')"
NEG      = "uniqExactIf(record_id, overall_sentiment = 'Négatif')"
NEU      = "uniqExactIf(record_id, overall_sentiment = 'Neutre')"
BOY      = "uniqExactIf(record_id, boycott_signal = 1)"

# Deduplicated engagement source. social_raw.records is a ReplacingMergeTree whose
# duplicate rows only collapse on background merge; aggregating one row per record_id
# here guarantees likes/comments/shares are never multiplied by duplicate copies when
# we join (which is exactly what was inflating every engagement figure ~4x).
RAW_ENG = (f"(SELECT record_id, any(likes) AS likes, "
           f"any(comments_count) AS comments_count, any(shares) AS shares "
           f"FROM {RAW}.records GROUP BY record_id)")


# ── HTML shell ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))


# ── META: everything the UI needs to build its filters ────────────────────────
@app.get("/api/meta")
def meta():
    rng = _ch(f"SELECT min(record_date) mn, max(record_date) mx FROM {DT} WHERE record_date IS NOT NULL")
    rng = rng[0] if rng else {"mn": None, "mx": None}
    brands = _ch(f"""
        SELECT d.entity entity, d.display_name display_name,
               d.brand_group brand_group, d.is_own is_own
        FROM {GOLD}.dim_brand d ORDER BY d.is_own DESC, d.entity""")
    # entities present in the data but maybe missing from dim_brand
    present = [r["entity"] for r in _ch(f"SELECT DISTINCT entity FROM {DT} ORDER BY entity")]
    known = {b["entity"] for b in brands}
    for e in present:
        if e not in known:
            brands.append({"entity": e, "display_name": e, "brand_group": e, "is_own": 0})
    return {
        "date_min": rng.get("mn"),
        "date_max": rng.get("mx"),
        "brands": brands,
        # Pages sources présentes dans les données (le "terrain" de collecte).
        "source_brands": [r["source_brand"] for r in _ch(
            f"SELECT DISTINCT source_brand FROM {DT} WHERE source_brand != '' ORDER BY source_brand")],
        "platforms":    [r["platform"]    for r in _ch(f"SELECT DISTINCT platform FROM {DT} ORDER BY platform")],
        "record_types": [r["record_type"] for r in _ch(f"SELECT DISTINCT record_type FROM {DT} ORDER BY record_type")],
        "languages":    [r["language"]    for r in _ch(f"SELECT DISTINCT language FROM {DT} WHERE language IS NOT NULL ORDER BY language")],
        "themes":       [r["theme"]       for r in _ch(f"SELECT DISTINCT theme FROM {DT} WHERE theme != '' ORDER BY theme")],
    }


def _common(start, end, platforms, entities, record_types, langs, source_brands=None):
    return _filters(start, end, platforms, entities, record_types, langs, source_brands)


# ── KPIs (with period-over-period deltas) ─────────────────────────────────────
@app.get("/api/kpis")
def kpis(start: str = None, end: str = None, platforms: str = None,
         entities: str = None, record_types: str = None, langs: str = None,
         source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)

    def snap(where: str) -> dict:
        rows = _ch(f"""
            SELECT {REC} records, {POS} pos, {NEG} neg, {NEU} neu, {BOY} boy,
                   round(avg(overall_score), 3) avg_score
            FROM {DT} WHERE {where}""")
        r = rows[0] if rows else {}
        recs = float(r.get("records") or 0) or 1
        return {
            "records":      int(r.get("records") or 0),
            "pos":          int(r.get("pos") or 0),
            "neg":          int(r.get("neg") or 0),
            "neu":          int(r.get("neu") or 0),
            "boy":          int(r.get("boy") or 0),
            "pos_rate":     round(float(r.get("pos") or 0) / recs * 100, 1),
            "neg_rate":     round(float(r.get("neg") or 0) / recs * 100, 1),
            "neu_rate":     round(float(r.get("neu") or 0) / recs * 100, 1),
            "boycott_rate": round(float(r.get("boy") or 0) / recs * 100, 1),
            "avg_score":    round(float(r.get("avg_score") or 0), 3),
        }

    cur = snap(f.where())
    # engagement for current window
    eng = _ch(f"""
        SELECT sum(r.likes + r.comments_count + r.shares) total
        FROM (SELECT DISTINCT record_id FROM {DT} WHERE {f.where()}) d
        INNER JOIN {RAW_ENG} r USING (record_id)""")
    cur["engagement"] = int((eng[0].get("total") if eng else 0) or 0)

    ps, pe = f.prev_window()
    if ps and pe:
        prev = snap(f"record_date >= '{ps}' AND record_date <= '{pe}'" +
                    (f" AND {_in('platform', f.platforms)}" if f.platforms else "") +
                    (f" AND {_in('entity', f.entities)}" if f.entities else "") +
                    (f" AND {_in('source_brand', f.source_brands)}" if f.source_brands else ""))
    else:
        prev = None

    def delta(key):
        if not prev or prev.get(key) in (None, 0):
            return None
        return round(cur[key] - prev[key], 1)

    return {"current": cur, "delta": {k: delta(k) for k in
            ["records", "pos_rate", "neg_rate", "boycott_rate", "avg_score"]}}


# ── Time series (day / week / month) ──────────────────────────────────────────
@app.get("/api/timeseries")
def timeseries(start: str = None, end: str = None, platforms: str = None,
               entities: str = None, record_types: str = None, langs: str = None,
               gran: str = "day", source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    b = _bucket(gran)
    return _ch(f"""
        SELECT {b} bucket,
               {REC} records, {POS} pos, {NEG} neg, {NEU} neu, {BOY} boycott
        FROM {DT} WHERE {f.where()}
        GROUP BY bucket ORDER BY bucket""")


# ── COMPETITION: own brands vs competitors ────────────────────────────────────
@app.get("/api/competition")
def competition(start: str = None, end: str = None, platforms: str = None,
                record_types: str = None, langs: str = None, gran: str = "month",
                source_brands: str = None):
    # note: entity filter intentionally ignored — competition compares all brands.
    # The source-page ("terrain") filter IS applied: "sur la page X, quelle marque
    # est citée et avec quelle part de voix ?".
    f = _filters(start, end, platforms, None, record_types, langs, source_brands)
    per_brand = _ch(f"""
        SELECT entity,
               {REC} records, {POS} pos, {NEG} neg, {BOY} boy,
               round(avg(overall_score), 3) avg_score
        FROM {DT} WHERE {f.where()}
        GROUP BY entity ORDER BY records DESC""")
    # attach dim_brand metadata + rates
    brands = {b["entity"]: b for b in _ch(
        f"SELECT entity, display_name, brand_group, is_own FROM {GOLD}.dim_brand")}
    total = sum(int(r["records"]) for r in per_brand) or 1
    for r in per_brand:
        meta = brands.get(r["entity"], {})
        recs = float(r["records"]) or 1
        r["display_name"] = meta.get("display_name", r["entity"])
        r["brand_group"]  = meta.get("brand_group", r["entity"])
        r["is_own"]       = int(meta.get("is_own", 0))
        r["sov_pct"]      = round(int(r["records"]) / total * 100, 1)
        r["pos_rate"]     = round(float(r["pos"]) / recs * 100, 1)
        r["neg_rate"]     = round(float(r["neg"]) / recs * 100, 1)
        r["boycott_rate"] = round(float(r["boy"]) / recs * 100, 1)

    # engagement per entity (join distinct record per entity → records)
    eng = _ch(f"""
        SELECT d.entity entity, sum(r.likes + r.comments_count + r.shares) eng
        FROM (SELECT DISTINCT record_id, entity FROM {DT} WHERE {f.where()}) d
        INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY d.entity""")
    engmap = {e["entity"]: int(e["eng"] or 0) for e in eng}
    for r in per_brand:
        r["engagement"] = engmap.get(r["entity"], 0)

    # group-level rollup (own vs competitors)
    # NB: 'Nonidentifié' = no brand detected (ambient VoC, mostly on LabelVie's own
    # pages). It is neither own nor a competitor, so it is excluded from this
    # comparison — otherwise it inflates the "Concurrents" side artificially.
    groups = {}
    for r in per_brand:
        if r["entity"] == "Nonidentifié":
            continue
        side = "Groupe LabelVie" if r["is_own"] else "Concurrents"
        g = groups.setdefault(side, {"records": 0, "pos": 0, "neg": 0, "boy": 0, "engagement": 0})
        g["records"] += int(r["records"]); g["pos"] += int(r["pos"])
        g["neg"] += int(r["neg"]); g["boy"] += int(r["boy"]); g["engagement"] += r["engagement"]
    group_rows = []
    for side, g in groups.items():
        recs = g["records"] or 1
        group_rows.append({
            "side": side, "records": g["records"], "engagement": g["engagement"],
            "pos": g["pos"], "neg": g["neg"], "boy": g["boy"],
            "pos_rate": round(g["pos"]/recs*100, 1),
            "neg_rate": round(g["neg"]/recs*100, 1),
            "boycott_rate": round(g["boy"]/recs*100, 1),
            "sov_pct": round(g["records"]/total*100, 1),
        })
    # ── Propagation du boycott dans le temps, par enseigne ────────────────────
    # Série temporelle : nombre de signaux de boycott par bucket (jour/semaine/mois)
    # et par marque. Seules les enseignes ayant au moins un signal sont renvoyées.
    bkt = _bucket(gran)
    bt_rows = _ch(f"""
        SELECT {bkt} bucket, entity, {BOY} boycott
        FROM {DT} WHERE {f.where()}
        GROUP BY bucket, entity ORDER BY bucket""")
    boy_by_entity: dict = {}
    buckets_set: set = set()
    for r in bt_rows:
        cnt = int(r.get("boycott") or 0)
        if cnt <= 0 or not r.get("bucket"):
            continue
        bk = str(r["bucket"])
        buckets_set.add(bk)
        boy_by_entity.setdefault(r["entity"], {})[bk] = cnt
    buckets = sorted(buckets_set)
    boycott_trend = {
        "buckets": buckets,
        "series": [
            {"entity": ent, "data": [vals.get(bk, 0) for bk in buckets],
             "total": sum(vals.values())}
            for ent, vals in sorted(boy_by_entity.items(),
                                    key=lambda kv: -sum(kv[1].values()))
        ],
    }

    return {"brands": per_brand, "groups": group_rows, "boycott_trend": boycott_trend}


# ── Sentiment by entity + entity×theme heatmap ────────────────────────────────
@app.get("/api/sentiment")
def sentiment(start: str = None, end: str = None, platforms: str = None,
              entities: str = None, record_types: str = None, langs: str = None,
              source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    by_entity = _ch(f"""
        SELECT entity, {POS} positive, {NEG} negative, {NEU} neutral,
               round(avg(overall_score), 3) avg_score
        FROM {DT} WHERE {f.where()}
        GROUP BY entity ORDER BY (positive+negative+neutral) DESC""")
    # theme-level sentiment (row grain is correct here)
    heatmap = _ch(f"""
        SELECT entity, theme,
               countIf(theme_sentiment = 'Positif') pos,
               countIf(theme_sentiment = 'Négatif') neg,
               countIf(theme_sentiment = 'Neutre')  neu,
               count() total,
               round(avg(theme_score), 3) avg_score
        FROM {DT} WHERE {f.where(THEME_NZ)}
        GROUP BY entity, theme ORDER BY entity, theme""")
    return {"by_entity": by_entity, "heatmap": heatmap}


# ── Themes (respects entity filter) + theme trend ─────────────────────────────
@app.get("/api/themes")
def themes(start: str = None, end: str = None, platforms: str = None,
           entities: str = None, record_types: str = None, langs: str = None,
           gran: str = "month", source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    freq = _ch(f"""
        SELECT theme,
               count() mentions,
               countIf(theme_sentiment = 'Positif') positive,
               countIf(theme_sentiment = 'Négatif') negative,
               countIf(theme_sentiment = 'Neutre')  neutral,
               round(avg(theme_score), 3) avg_score
        FROM {DT} WHERE {f.where(THEME_NZ)}
        GROUP BY theme ORDER BY mentions DESC""")
    b = _bucket(gran)
    trend = _ch(f"""
        SELECT {b} bucket, theme, count() mentions
        FROM {DT} WHERE {f.where(THEME_NZ)}
        GROUP BY bucket, theme ORDER BY bucket""")
    return {"frequency": freq, "trend": trend}


# ── Boycott deep dive ─────────────────────────────────────────────────────────
@app.get("/api/boycott")
def boycott(start: str = None, end: str = None, platforms: str = None,
            entities: str = None, record_types: str = None, langs: str = None,
            gran: str = "month", source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    b = _bucket(gran)
    trend = _ch(f"""
        SELECT {b} bucket, {REC} total, {BOY} boycott,
               round({BOY} / {REC} * 100, 1) rate
        FROM {DT} WHERE {f.where()}
        GROUP BY bucket ORDER BY bucket""")
    by_entity = _ch(f"""
        SELECT entity, {BOY} boycott, {REC} total,
               round({BOY} / {REC} * 100, 1) rate
        FROM {DT} WHERE {f.where()}
        GROUP BY entity ORDER BY boycott DESC""")
    by_platform = _ch(f"""
        SELECT platform, {BOY} boycott, {REC} total,
               round({BOY} / {REC} * 100, 1) rate
        FROM {DT} WHERE {f.where()}
        GROUP BY platform ORDER BY boycott DESC""")
    by_lang = _ch(f"""
        SELECT language, {BOY} boycott, {REC} total
        FROM {DT} WHERE {f.where(LANG_NN)}
        GROUP BY language ORDER BY boycott DESC""")
    return {"trend": trend, "by_entity": by_entity,
            "by_platform": by_platform, "by_language": by_lang}


# ── Engagement (joins raw.records for likes/comments/shares) ───────────────────
@app.get("/api/engagement")
def engagement(start: str = None, end: str = None, platforms: str = None,
               entities: str = None, record_types: str = None, langs: str = None,
               gran: str = "month", source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    base = f"(SELECT DISTINCT record_id, platform, record_type, overall_sentiment, record_date FROM {DT} WHERE {f.where()})"
    by_sentiment = _ch(f"""
        SELECT d.overall_sentiment sentiment,
               count() records,
               sum(r.likes + r.comments_count + r.shares) total_eng,
               round(avg(r.likes + r.comments_count + r.shares), 1) avg_eng,
               max(r.likes + r.comments_count + r.shares) max_eng,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY sentiment ORDER BY total_eng DESC""")
    by_platform = _ch(f"""
        SELECT d.platform platform,
               sum(r.likes + r.comments_count + r.shares) total_eng,
               round(avg(r.likes + r.comments_count + r.shares), 1) avg_eng,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY platform ORDER BY total_eng DESC""")
    by_type = _ch(f"""
        SELECT d.record_type record_type,
               sum(r.likes + r.comments_count + r.shares) total_eng,
               round(avg(r.likes + r.comments_count + r.shares), 1) avg_eng,
               count() records,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY record_type""")
    b = _bucket(gran).replace("record_date", "d.record_date")
    trend = _ch(f"""
        SELECT {b} bucket,
               sum(r.likes + r.comments_count + r.shares) engagement,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY bucket ORDER BY bucket""")
    return {"by_sentiment": by_sentiment, "by_platform": by_platform,
            "by_type": by_type, "trend": trend}


# ── Language ──────────────────────────────────────────────────────────────────
@app.get("/api/language")
def language(start: str = None, end: str = None, platforms: str = None,
             entities: str = None, record_types: str = None, langs: str = None,
             source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    dist = _ch(f"""
        SELECT language, {REC} records, {POS} positives, {NEG} negatives, {NEU} neutrals
        FROM {DT} WHERE {f.where(LANG_NN)}
        GROUP BY language ORDER BY records DESC""")
    by_platform = _ch(f"""
        SELECT platform, language, {REC} records
        FROM {DT} WHERE {f.where(LANG_NN)}
        GROUP BY platform, language ORDER BY platform""")
    return {"distribution": dist, "by_platform": by_platform}


# ── Topics (NMF — social_raw.topics, latest run) ──────────────────────────────
@app.get("/api/topics")
def topics():
    latest = _ch(f"SELECT max(run_id) r FROM {RAW}.topics")
    run = latest[0].get("r") if latest else None
    if not run:
        return {"run_id": None, "topics": []}
    rows = _ch(f"""
        SELECT topic_id, label, top_words, record_count
        FROM {RAW}.topics WHERE run_id = '{_esc(run)}'
        ORDER BY record_count DESC""")
    for r in rows:
        try:
            r["words"] = json.loads(r.get("top_words") or "[]")
        except Exception:
            r["words"] = []
    return {"run_id": run, "topics": rows}


# ── Trends / alerts (social_raw.trends, latest run) ───────────────────────────
@app.get("/api/trends")
def trends():
    latest = _ch(f"SELECT max(run_id) r FROM {RAW}.trends")
    run = latest[0].get("r") if latest else None
    if not run:
        return {"run_id": None, "trends": []}
    rows = _ch(f"""
        SELECT entity, curr_count, curr_avg_score, curr_boycott_rate, curr_neg_rate,
               prev_count, prev_avg_score, prev_boycott_rate, prev_neg_rate, alerts
        FROM {RAW}.trends WHERE run_id = '{_esc(run)}'""")
    for r in rows:
        try:
            r["alerts"] = json.loads(r.get("alerts") or "[]")
        except Exception:
            r["alerts"] = []
    return {"run_id": run, "trends": rows}


# ── Authors ───────────────────────────────────────────────────────────────────
@app.get("/api/authors")
def authors(platforms: str = None, limit: int = 25):
    plats = _csv(platforms)
    where = _in("platform", plats) or "1=1"
    lim = max(1, min(limit, 100))
    return _ch(f"""
        SELECT author, platform, records, total_engagement,
               round(avg_sentiment_score, 3) avg_sentiment_score,
               negative_pct, positive_records, negative_records, boycott_records,
               first_seen, last_seen
        FROM {GOLD}.fct_author_influence
        WHERE {where}
        ORDER BY total_engagement DESC LIMIT {lim}""")


# ── Comments explorer (actual text, paginated) ────────────────────────────────
@app.get("/api/comments")
def comments(start: str = None, end: str = None, platforms: str = None,
             entities: str = None, record_types: str = None, langs: str = None,
             theme: str = None, sentiment: str = None,
             boycott: int = 0, limit: int = 100, source_brands: str = None):
    f = _common(start, end, platforms, entities, record_types, langs, source_brands)
    extra = []
    if theme and theme.lower() != "all":
        extra.append(f"theme = '{_esc(theme)}'")
    if sentiment and sentiment.lower() != "all":
        extra.append(f"overall_sentiment = '{_esc(sentiment)}'")
    if boycott:
        extra.append("boycott_signal = 1")
    where = f.where(" AND ".join(extra)) if extra else f.where()
    lim = max(1, min(limit, 500))
    return _ch(f"""
        SELECT record_id, record_date, platform, record_type, language,
               source_brand, entity, theme, theme_sentiment, overall_sentiment,
               round(overall_score, 3) overall_score, boycott_signal,
               author, url, text
        FROM {DT} WHERE {where}
        ORDER BY record_date DESC LIMIT {lim}""")


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"\nDashboard: http://localhost:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
