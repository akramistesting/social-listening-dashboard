"""
dashboard_app.py — Social Listening LabelVie
Réplique exacte des 10 onglets de serving/web_dashboard_light.py en Streamlit.
Même table source (social_gold.fct_comment_drillthrough), mêmes requêtes SQL.
"""
import os
import html
from datetime import date, timedelta, datetime

import clickhouse_connect
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
APP_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "labelvie2026")
CH_HOST      = os.getenv("CLICKHOUSE_HOST", "localhost")
CH_PORT      = int(os.getenv("CLICKHOUSE_PORT", 8123))
CH_USER      = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS      = os.getenv("CLICKHOUSE_PASSWORD", "")
CH_DB        = os.getenv("CLICKHOUSE_DB", "social_raw")

GOLD = "social_gold"
RAW  = "social_raw"
DT   = f"{GOLD}.fct_comment_drillthrough"

REC = "uniqExact(record_id)"
POS = "uniqExactIf(record_id, overall_sentiment = 'Positif')"
NEG = "uniqExactIf(record_id, overall_sentiment = 'Négatif')"
NEU = "uniqExactIf(record_id, overall_sentiment = 'Neutre')"
BOY = "uniqExactIf(record_id, boycott_signal = 1)"
RAW_ENG = (
    f"(SELECT record_id, any(likes) AS likes, "
    f"any(comments_count) AS comments_count, any(shares) AS shares "
    f"FROM {RAW}.records GROUP BY record_id)"
)

C_POS  = "#22c55e"
C_NEG  = "#ef4444"
C_NEU  = "#94a3b8"
C_BOY  = "#dc2626"
C_OWN  = "#3b82f6"
C_COMP = "#f97316"
PAL    = ["#dc2626","#3b82f6","#22c55e","#f97316","#a855f7",
          "#14b8a6","#f59e0b","#ec4899","#64748b","#10b981","#6366f1"]

# Couleur fixe par enseigne (identité stable, indépendante du filtre/tri) —
# familles froides pour le groupe LabelVie, chaudes pour les concurrents, afin
# de garder la lecture "groupe" tout en distinguant chaque marque individuelle.
BRAND_COLORS = {
    "LabelVie":          "#3b82f6",
    "Carrefour":         "#0ea5e9",
    "Carrefour Express": "#06b6d4",
    "Carrefour Gourmet": "#6366f1",
    "Carrefour Market":  "#8b5cf6",
    "Suppeco":           "#14b8a6",
    "Atacadao":          "#10b981",
    "Marjane":           "#f97316",
    "Marjane City":      "#f59e0b",
    "Kazyon":            "#dc2626",
    "BIM":               "#ec4899",
    "HyperU":            "#eab308",
    "Asswak Essalam":    "#a855f7",
}


def _brand_colors(names):
    """Couleur fixe par nom de marque ; repli sur PAL (cyclique) si inconnue."""
    return [BRAND_COLORS.get(n, PAL[i % len(PAL)]) for i, n in enumerate(names)]


def _label_pie(fig):
    """Attache le nom, la valeur absolue (gris) et le % (couleur vive) sur chaque part."""
    values = list(fig.data[0].values)
    total = sum(values) or 1
    texts = [
        f"{lbl}<br><span style='color:#9aa4b0'>{fmt(v)}</span> · "
        f"<span style='color:#fbbf24'>{v/total*100:.1f}%</span>"
        for lbl, v in zip(fig.data[0].labels, values)
    ]
    fig.update_traces(
        text=texts, textinfo="none", textposition="outside",
        texttemplate="%{text}",
    )
    fig.update_layout(uniformtext_minsize=9, uniformtext_mode="hide")
    return fig


# ── Auth ──────────────────────────────────────────────────────────────────────
def check_password() -> bool:
    if st.session_state.get("auth"):
        return True
    st.title("Social Listening — LabelVie")
    pwd = st.text_input("Mot de passe", type="password")
    if st.button("Connexion"):
        if pwd == APP_PASSWORD:
            st.session_state["auth"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    return False


# ── ClickHouse ────────────────────────────────────────────────────────────────
def _make_client():
    # Si l'host contient cfargotunnel.com ou ngrok → connexion HTTPS port 443
    use_https = any(x in CH_HOST for x in ["cfargotunnel.com", "ngrok", "trycloudflare.com"])
    return clickhouse_connect.get_client(
        host=CH_HOST,
        port=443 if use_https else CH_PORT,
        username=CH_USER,
        password=CH_PASS,
        secure=use_https,
        verify=False,
        # Pas de session_id auto : sinon les requêtes des différents onglets,
        # exécutées en parallèle par Streamlit, se bloquent mutuellement
        # ("concurrent queries within the same session").
        autogenerate_session_id=False,
        # Le tunnel ngrok gratuit sert une page d'avertissement HTML aux requêtes
        # sans ce header, au lieu de les transmettre à ClickHouse (ERR_NGROK_6024).
        headers={"ngrok-skip-browser-warning": "true"} if "ngrok" in CH_HOST else None,
    )


@st.cache_data(ttl=3600, show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    # Un client neuf par requête : pas d'état de session partagé entre threads.
    client = _make_client()
    try:
        return client.query_df(sql)
    finally:
        client.close()


# ── Affichage : étiquettes de valeurs sur toutes les barres ───────────────────
_render = st.plotly_chart

def _lbl(v) -> str:
    """Formate une valeur de barre : entiers avec séparateur, décimaux à 1 chiffre."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v == 0:
        return ""
    if v == int(v):
        return f"{int(v):,}".replace(",", " ")
    return f"{v:.1f}"

def show(fig, **kwargs):
    """Rend une figure Plotly en ajoutant les valeurs lisibles sur chaque barre."""
    for tr in fig.data:
        if tr.type == "bar":
            vals = tr.x if tr.orientation == "h" else tr.y
            if vals is not None:
                tr.text = [_lbl(v) for v in vals]
                tr.textposition = "auto"
                tr.cliponaxis = False
                tr.textfont = dict(size=11)
    _render(fig, **kwargs)


# ── Filter helpers ────────────────────────────────────────────────────────────
def _esc(v: str) -> str:
    return v.replace("'", "''")

def _in(col: str, vals: list) -> str:
    if not vals:
        return ""
    quoted = ", ".join(f"'{_esc(v)}'" for v in vals)
    return f"{col} IN ({quoted})"

def _bucket(gran: str) -> str:
    if gran == "month":  return "toStartOfMonth(record_date)"
    if gran == "week":   return "toStartOfWeek(record_date, 1)"
    return "record_date"

def _where(start, end, entities, platforms, types, langs, extra="", source_brands=None,
           scope="voc", voc_override_entity=None) -> str:
    """
    scope pilote le périmètre voix-client / voix-marque via is_brand_voice :
      - "voc"   (défaut) : is_brand_voice=0 → uniquement le contenu écrit par des
                 utilisateurs (commentaires clients + posts de GROUPES par des users).
                 Exclut les posts de pages officielles et les réponses CM.
      - "brand" : is_brand_voice=1 → contenu écrit par une page de marque.
      - "all"   : aucun filtre (activité/portée : volume, engagement, part de voix).
    is_brand_voice est calculé dans fct_comment_drillthrough (authorship + contexte),
    donc le périmètre reste correct le jour où on ajoute les groupes Facebook.

    voc_override_entity : dérogation ciblée pour une seule marque (ex: "CarrefourExpress"),
    utilisée uniquement par les onglets Thèmes/Langue. Cette marque n'a pas de
    commentaires BrightData exploitables (texte vide / non enrichi) — sous scope="voc"
    par défaut elle apparaîtrait donc sans aucun contenu thématisé. Pour cette marque
    précise, on bascule sur ses posts (déjà enrichis avec entité détectée par le NLP)
    au lieu de is_brand_voice=0. Aucune autre marque n'est affectée : elles gardent
    strictement is_brand_voice=0.
    """
    parts = ["record_date IS NOT NULL"]
    if start: parts.append(f"record_date >= '{start}'")
    if end:   parts.append(f"record_date <= '{end}'")
    for clause in [
        _in("entity",       entities),
        _in("source_brand", source_brands or []),
        _in("platform",     platforms),
        _in("record_type",  types),
        _in("language",     langs),
    ]:
        if clause: parts.append(clause)
    if scope == "voc":
        if voc_override_entity:
            ent = _esc(voc_override_entity)
            parts.append(
                f"((entity = '{ent}' AND record_type = 'post') "
                f"OR (entity != '{ent}' AND is_brand_voice = 0))"
            )
        else:
            parts.append("is_brand_voice = 0")
    elif scope == "brand":
        parts.append("is_brand_voice = 1")
    if extra: parts.append(extra)
    return " AND ".join(parts)

def _prev_window(start, end):
    if not (start and end):
        return None, None
    s = datetime.strptime(str(start), "%Y-%m-%d").date()
    e = datetime.strptime(str(end),   "%Y-%m-%d").date()
    span = (e - s).days + 1
    return str(s - timedelta(days=span)), str(s - timedelta(days=1))

def fmt(n) -> str:
    try:    return f"{int(n):,}".replace(",", " ")
    except: return str(n)


# ── Meta ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def load_meta():
    rng    = q(f"SELECT min(record_date) mn, max(record_date) mx FROM {DT} WHERE record_date IS NOT NULL")
    brands = q(f"SELECT entity, display_name, brand_group, is_own FROM {GOLD}.dim_brand ORDER BY is_own DESC, entity")
    plats  = q(f"SELECT DISTINCT platform FROM {DT} ORDER BY platform")
    types  = q(f"SELECT DISTINCT record_type FROM {DT} ORDER BY record_type")
    langs  = q(f"SELECT DISTINCT language FROM {DT} WHERE language IS NOT NULL ORDER BY language")
    themes = q(f"SELECT DISTINCT theme FROM {DT} WHERE theme != '' ORDER BY theme")
    srcb   = q(f"SELECT DISTINCT source_brand FROM {DT} WHERE source_brand != '' ORDER BY source_brand")
    return {
        "date_min": str(rng["mn"].iloc[0]) if not rng.empty else None,
        "date_max": str(rng["mx"].iloc[0]) if not rng.empty else None,
        "brands":   brands,
        "platforms": plats["platform"].tolist(),
        "types":    types["record_type"].tolist(),
        "langs":    langs["language"].tolist(),
        "themes":   themes["theme"].tolist(),
        # Pages sources ("terrain" de collecte) présentes dans les données.
        "source_brands": srcb["source_brand"].tolist(),
    }


# ── Tabs ──────────────────────────────────────────────────────────────────────

def tab_overview(start, end, entities, platforms, types, langs, gran, source_brands=None):
    # w      = voix client (défaut) → sert aux taux de sentiment et à la tendance.
    # w_all  = tout le contenu (posts inclus) → sert au VOLUME et à l'ENGAGEMENT.
    w     = _where(start, end, entities, platforms, types, langs, source_brands=source_brands)
    w_all = _where(start, end, entities, platforms, types, langs, source_brands=source_brands, scope="all")
    bkt   = _bucket(gran)

    # KPIs de perception (voix client) : dénominateur = commentaires clients.
    kpi = q(f"""
        SELECT {REC} records, {POS} pos, {NEG} neg, {NEU} neu, {BOY} boy,
               round(avg(overall_score), 3) avg_score
        FROM {DT} WHERE {w}
    """)
    # Volume total publié (posts de marque inclus) — KPI d'activité.
    vol = q(f"SELECT {REC} records FROM {DT} WHERE {w_all}")
    eng = q(f"""
        SELECT sum(r.likes + r.comments_count + r.shares) total
        FROM (SELECT DISTINCT record_id FROM {DT} WHERE {w_all}) d
        INNER JOIN {RAW_ENG} r USING (record_id)
    """)

    voc_recs = int(kpi["records"].iloc[0]) if not kpi.empty else 0
    pos_n = int(kpi["pos"].iloc[0])     if not kpi.empty else 0
    neg_n = int(kpi["neg"].iloc[0])     if not kpi.empty else 0
    neu_n = int(kpi["neu"].iloc[0])     if not kpi.empty else 0
    boy_n = int(kpi["boy"].iloc[0])     if not kpi.empty else 0
    score = float(kpi["avg_score"].iloc[0]) if not kpi.empty else 0.0
    total_recs = int(vol["records"].iloc[0]) if not vol.empty else 0
    engv  = int(eng["total"].iloc[0])   if not eng.empty and eng["total"].iloc[0] else 0
    den   = voc_recs or 1

    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("Publications",    fmt(total_recs), help="Volume total publié (posts de marque inclus).")
    c2.metric("Taux positif",    f"{round(pos_n/den*100,1)}%", help=f"% sur {fmt(voc_recs)} messages clients (voix client).")
    c3.metric("Taux négatif",    f"{round(neg_n/den*100,1)}%")
    c4.metric("Taux boycott",    f"{round(boy_n/den*100,1)}%")
    c5.metric("Score moyen",     f"{score:+.3f}")
    c6.metric("Engagement",      fmt(engv))

    st.markdown("---")

    # Trend + Pie
    ts = q(f"""
        SELECT {bkt} bucket, {REC} records, {POS} pos, {NEG} neg, {NEU} neu
        FROM {DT} WHERE {w}
        GROUP BY bucket ORDER BY bucket
    """)
    col1, col2 = st.columns([2, 1])
    with col1:
        if not ts.empty:
            ts["bucket"] = ts["bucket"].astype(str).str[:10]
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=ts["bucket"], y=ts["pos"], name="Positif", fill="tozeroy", line_color=C_POS))
            fig.add_trace(go.Scatter(x=ts["bucket"], y=ts["neg"], name="Négatif", fill="tozeroy", line_color=C_NEG))
            fig.add_trace(go.Scatter(x=ts["bucket"], y=ts["neu"], name="Neutre",  line_color=C_NEU))
            fig.update_layout(title=f"Tendance du sentiment · {gran}", height=280, margin=dict(t=40,b=20))
            show(fig, use_container_width=True)
    with col2:
        fig2 = go.Figure(go.Pie(
            labels=["Positif","Négatif","Neutre"],
            values=[pos_n, neg_n, neu_n],
            hole=0.6,
            marker_colors=[C_POS, C_NEG, C_NEU],
        ))
        fig2 = _label_pie(fig2)
        fig2.update_layout(title="Répartition", height=280, margin=dict(t=40,b=60))
        show(fig2, use_container_width=True)

    # Top themes + Volume par plateforme
    th = q(f"""
        SELECT theme, count() mentions FROM {DT}
        WHERE {_where(start, end, entities, platforms, types, langs, "theme != ''", source_brands=source_brands)}
        GROUP BY theme ORDER BY mentions DESC LIMIT 8
    """)
    bp = q(f"SELECT platform, {REC} total FROM {DT} WHERE {w_all} GROUP BY platform ORDER BY total DESC")

    col3, col4 = st.columns(2)
    with col3:
        if not th.empty:
            fig3 = px.bar(th, x="mentions", y="theme", orientation="h",
                          title="Top thèmes (mentions)", color_discrete_sequence=[C_OWN])
            fig3.update_layout(height=280, margin=dict(t=40,b=20), showlegend=False)
            show(fig3, use_container_width=True)
    with col4:
        if not bp.empty:
            fig4 = px.bar(bp, x="platform", y="total", title="Volume par plateforme",
                          color="platform", color_discrete_sequence=PAL)
            fig4.update_layout(height=280, margin=dict(t=40,b=20), showlegend=False)
            show(fig4, use_container_width=True)


def tab_competition(start, end, entities, platforms, types, langs, gran, source_brands=None):
    bkt = _bucket(gran)
    # filtre entity volontairement ignoré (on compare toutes les marques) ; le
    # filtre page-source ("terrain") est appliqué.
    # w = voix client (SOV + taux de sentiment) ; w_all = tout (engagement/portée).
    w     = _where(start, end, [], platforms, types, langs, source_brands=source_brands)
    w_all = _where(start, end, [], platforms, types, langs, source_brands=source_brands, scope="all")

    # entity vient de DT (côté gauche) — jamais vide ; display_name/brand_group
    # retombent sur l'entité si absents de dim_brand. Nonidentifié est exclu :
    # ce n'est pas une marque, et son étiquette vide s'affichait comme « 6 ».
    brands_df = q(f"""
        SELECT entity,
               if(d.display_name = '', entity, d.display_name) AS display_name,
               if(d.brand_group  = '', entity, d.brand_group)  AS brand_group,
               toUInt8(d.is_own) AS is_own,
               {REC} records, {POS} pos, {NEG} neg, {NEU} neu, {BOY} boy,
               round(avg(overall_score),3) avg_score
        FROM {DT} dt LEFT JOIN {GOLD}.dim_brand d USING (entity)
        WHERE {w} AND entity NOT LIKE 'Nonidentif%'
        GROUP BY entity, display_name, brand_group, is_own
        ORDER BY records DESC
    """)
    if brands_df.empty:
        st.info("Aucune donnée.")
        return

    total = int(brands_df["records"].sum()) or 1
    brands_df["sov_pct"]      = (brands_df["records"] / total * 100).round(1)
    brands_df["pos_rate"]     = (brands_df["pos"] / brands_df["records"].clip(lower=1) * 100).round(1)
    brands_df["neg_rate"]     = (brands_df["neg"] / brands_df["records"].clip(lower=1) * 100).round(1)
    brands_df["boycott_rate"] = (brands_df["boy"] / brands_df["records"].clip(lower=1) * 100).round(1)
    brands_df["display_name"] = brands_df["display_name"].fillna(brands_df["entity"])
    brands_df["brand_group"]  = brands_df["brand_group"].fillna(brands_df["entity"])

    # Engagement (portée) → tout le contenu, posts inclus.
    eng = q(f"""
        SELECT d.entity entity, sum(r.likes + r.comments_count + r.shares) eng
        FROM (SELECT DISTINCT record_id, entity FROM {DT} WHERE {w_all}) d
        INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY entity
    """)
    if not eng.empty:
        brands_df = brands_df.merge(eng, on="entity", how="left")
        brands_df["eng"] = brands_df["eng"].fillna(0).astype(int)
    else:
        brands_df["eng"] = 0

    col_sov = _brand_colors(brands_df["display_name"])

    col1, col2 = st.columns(2)
    with col1:
        fig = go.Figure(go.Pie(
            labels=brands_df["display_name"], values=brands_df["records"],
            hole=0.55, marker_colors=col_sov,
        ))
        fig = _label_pie(fig)
        fig.update_layout(title="Part de voix (mentions)", height=380, margin=dict(t=40,b=60))
        show(fig, use_container_width=True)
    with col2:
        groups = brands_df.groupby("is_own")[["pos","neg","boy","records"]].sum().reset_index()
        groups["label"] = groups["is_own"].map({1:"Groupe LabelVie", 0:"Concurrents"})
        groups["pos_rate"]     = (groups["pos"] / groups["records"].clip(lower=1) * 100).round(1)
        groups["neg_rate"]     = (groups["neg"] / groups["records"].clip(lower=1) * 100).round(1)
        groups["boycott_rate"] = (groups["boy"] / groups["records"].clip(lower=1) * 100).round(1)
        fig2 = go.Figure()
        for label, rate_col, cnt_col, color in [("Positif %","pos_rate","pos",C_POS),("Négatif %","neg_rate","neg",C_NEG),("Boycott %","boycott_rate","boy",C_BOY)]:
            texts = [
                f"<span style='color:{color}'>{r}%</span><br><span style='color:#9aa4b0'>({fmt(n)})</span>"
                for r, n in zip(groups[rate_col], groups[cnt_col])
            ]
            fig2.add_trace(go.Bar(name=label, x=groups["label"], y=groups[rate_col], marker_color=color,
                                   text=texts, texttemplate="%{text}", textposition="outside"))
        fig2.update_layout(title="Groupe LabelVie vs Concurrents", height=320, margin=dict(t=40,b=20))
        show(fig2, use_container_width=True)

    # Boycott propagation par enseigne
    bt = q(f"""
        SELECT {bkt} bucket, entity, {BOY} boycott
        FROM {DT} WHERE {w}
        GROUP BY bucket, entity ORDER BY bucket
    """)
    st.subheader(f"Propagation du boycott par enseigne · {gran}")
    if not bt.empty:
        bt["bucket"] = bt["bucket"].astype(str).str[:10]
        bt_filt = bt[bt["boycott"] > 0]
        if not bt_filt.empty:
            fig3 = px.line(bt_filt, x="bucket", y="boycott", color="entity",
                           color_discrete_map=BRAND_COLORS, color_discrete_sequence=PAL, markers=True)
            fig3.update_layout(height=300, margin=dict(t=20,b=20), yaxis_title="Signaux boycott")
            show(fig3, use_container_width=True)
        else:
            st.info("Aucun signal de boycott sur la période.")

    # Engagement par marque
    fig4 = px.bar(brands_df.sort_values("eng"), x="eng", y="display_name",
                  orientation="h", title="Engagement par marque",
                  color="display_name", color_discrete_map=BRAND_COLORS)
    fig4.update_layout(height=350, margin=dict(t=40,b=20), showlegend=False)
    show(fig4, use_container_width=True)

    # Tableau comparatif
    st.subheader("Tableau comparatif")
    table = brands_df[["display_name","brand_group","records","sov_pct","pos_rate","neg_rate","boycott_rate","avg_score","eng"]].copy()
    table.columns = ["Marque","Groupe","Mentions","SOV %","Positif %","Négatif %","Boycott %","Score moy.","Engagement"]
    st.dataframe(table.reset_index(drop=True), use_container_width=True)


def tab_sentiment(start, end, entities, platforms, types, langs, gran, source_brands=None):
    w = _where(start, end, entities, platforms, types, langs, source_brands=source_brands)

    by_entity = q(f"""
        SELECT entity, {POS} positive, {NEG} negative, {NEU} neutral,
               round(avg(overall_score),3) avg_score
        FROM {DT} WHERE {w}
        GROUP BY entity ORDER BY (positive+negative+neutral) DESC
    """)
    if by_entity.empty:
        st.info("Aucune donnée.")
        return

    col1, col2 = st.columns(2)
    with col1:
        fig = go.Figure()
        for label, col, color in [("Positif","positive",C_POS),("Négatif","negative",C_NEG),("Neutre","neutral",C_NEU)]:
            fig.add_trace(go.Bar(name=label, x=by_entity["entity"], y=by_entity[col], marker_color=color))
        fig.update_layout(barmode="stack", title="Sentiment par marque", height=320, margin=dict(t=40,b=20))
        show(fig, use_container_width=True)
    with col2:
        fig2 = px.bar(by_entity.sort_values("avg_score"), x="avg_score", y="entity",
                      orientation="h", title="Score moyen par marque",
                      color="avg_score", color_continuous_scale=["#ef4444","#94a3b8","#22c55e"],
                      color_continuous_midpoint=0)
        fig2.update_layout(height=320, margin=dict(t=40,b=20), showlegend=False)
        show(fig2, use_container_width=True)

    # Heatmap brand × theme
    hm = q(f"""
        SELECT entity, theme, round(avg(theme_score),3) avg_score, count() total
        FROM {DT} WHERE {_where(start, end, entities, platforms, types, langs, "theme != ''", source_brands=source_brands)}
        GROUP BY entity, theme
    """)
    if not hm.empty:
        st.subheader("Matrice marque × thème (score moyen + nombre de mentions, vert = positif)")
        score_pivot = hm.pivot_table(index="entity", columns="theme", values="avg_score")
        cnt_pivot   = hm.pivot_table(index="entity", columns="theme", values="total")
        # Texte par case : score moyen + nombre de mentions (n=...)
        text = score_pivot.copy().astype(object)
        for i in score_pivot.index:
            for j in score_pivot.columns:
                s = score_pivot.loc[i, j]
                n = cnt_pivot.loc[i, j]
                text.loc[i, j] = "" if pd.isna(s) else f"{s:.2f}<br>n={int(n)}"
        fig3 = px.imshow(score_pivot, color_continuous_scale=["#ef4444","#f8fafc","#22c55e"],
                         color_continuous_midpoint=0, aspect="auto")
        fig3.update_traces(
            text=text.values, texttemplate="%{text}", textfont=dict(size=10),
            hovertemplate="%{y} · %{x}<br>Score moyen : %{z:.2f}<extra></extra>",
        )
        fig3.update_layout(height=460, margin=dict(t=20, b=20))
        show(fig3, use_container_width=True)


def tab_themes(start, end, entities, platforms, types, langs, gran, source_brands=None):
    bkt = _bucket(gran)
    wt  = _where(start, end, entities, platforms, types, langs, "theme != ''", source_brands=source_brands,
                 voc_override_entity="CarrefourExpress")

    freq = q(f"""
        SELECT theme, count() mentions,
               countIf(theme_sentiment = 'Positif') positive,
               countIf(theme_sentiment = 'Négatif') negative,
               countIf(theme_sentiment = 'Neutre')  neutral
        FROM {DT} WHERE {wt}
        GROUP BY theme ORDER BY mentions DESC
    """)
    if freq.empty:
        st.info("Aucune donnée.")
        return

    col1, col2 = st.columns(2)
    with col1:
        fig = px.bar(freq, x="mentions", y="theme", orientation="h",
                     title="Fréquence des thèmes", color_discrete_sequence=[C_OWN])
        fig.update_layout(height=360, margin=dict(t=40,b=20), showlegend=False)
        show(fig, use_container_width=True)
    with col2:
        fig2 = go.Figure()
        for label, col, color in [("Positif","positive",C_POS),("Négatif","negative",C_NEG),("Neutre","neutral",C_NEU)]:
            fig2.add_trace(go.Bar(name=label, y=freq["theme"], x=freq[col], orientation="h", marker_color=color))
        fig2.update_layout(barmode="stack", title="Sentiment par thème", height=360, margin=dict(t=40,b=20))
        show(fig2, use_container_width=True)

    trend = q(f"""
        SELECT {bkt} bucket, theme, count() mentions
        FROM {DT} WHERE {wt}
        GROUP BY bucket, theme ORDER BY bucket
    """)
    if not trend.empty:
        trend["bucket"] = trend["bucket"].astype(str).str[:10]
        fig3 = px.line(trend, x="bucket", y="mentions", color="theme",
                       title=f"Évolution des thèmes · {gran}", color_discrete_sequence=PAL)
        fig3.update_layout(height=320, margin=dict(t=40,b=20))
        show(fig3, use_container_width=True)


def tab_boycott(start, end, entities, platforms, types, langs, gran, source_brands=None):
    bkt = _bucket(gran)
    w   = _where(start, end, entities, platforms, types, langs, source_brands=source_brands)

    trend = q(f"""
        SELECT {bkt} bucket, {REC} total, {BOY} boycott,
               round({BOY} / {REC} * 100, 1) rate
        FROM {DT} WHERE {w}
        GROUP BY bucket ORDER BY bucket
    """)
    if not trend.empty:
        trend["bucket"] = trend["bucket"].astype(str).str[:10]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=trend["bucket"], y=trend["rate"], name="Taux boycott %",
                                 fill="tozeroy", line_color=C_BOY, yaxis="y"))
        fig.add_trace(go.Scatter(x=trend["bucket"], y=trend["total"], name="Total mentions",
                                 line=dict(color="#64748b", dash="dot"), yaxis="y2"))
        fig.update_layout(
            title=f"Taux de boycott dans le temps · {gran}", height=320,
            yaxis=dict(title="Taux boycott %", side="left"),
            yaxis2=dict(title="Nb mentions", side="right", overlaying="y", showgrid=False),
            margin=dict(t=40,b=20),
        )
        show(fig, use_container_width=True)

    by_entity   = q(f"SELECT entity, {BOY} boycott, {REC} total FROM {DT} WHERE {w} GROUP BY entity ORDER BY boycott DESC")
    by_platform = q(f"SELECT platform, {BOY} boycott, {REC} total FROM {DT} WHERE {w} GROUP BY platform ORDER BY boycott DESC")
    by_lang     = q(f"SELECT language, {BOY} boycott FROM {DT} WHERE {w} AND language IS NOT NULL GROUP BY language ORDER BY boycott DESC")

    col1, col2, col3 = st.columns(3)
    with col1:
        if not by_entity.empty:
            fig2 = px.bar(by_entity, x="entity", y="boycott", title="Boycott par marque",
                          color_discrete_sequence=[C_BOY])
            fig2.update_layout(height=300, margin=dict(t=40,b=20), showlegend=False)
            show(fig2, use_container_width=True)
    with col2:
        if not by_platform.empty:
            fig3 = go.Figure(go.Pie(labels=by_platform["platform"], values=by_platform["boycott"],
                                    hole=0.52, marker_colors=PAL))
            fig3 = _label_pie(fig3)
            fig3.update_layout(title="Boycott par plateforme", height=340, margin=dict(t=40,b=60))
            show(fig3, use_container_width=True)
    with col3:
        if not by_lang.empty:
            fig4 = px.bar(by_lang, x="boycott", y="language", orientation="h",
                          title="Boycott par langue", color_discrete_sequence=[C_COMP])
            fig4.update_layout(height=300, margin=dict(t=40,b=20), showlegend=False)
            show(fig4, use_container_width=True)


def tab_engagement(start, end, entities, platforms, types, langs, gran, source_brands=None):
    # Onglet d'ACTIVITÉ/portée : on garde tout le contenu (posts de marque inclus,
    # car ils génèrent l'essentiel de l'engagement) → scope="all".
    bkt = _bucket(gran)
    w   = _where(start, end, entities, platforms, types, langs, source_brands=source_brands, scope="all")
    base = f"(SELECT DISTINCT record_id, platform, record_type, overall_sentiment, record_date FROM {DT} WHERE {w})"

    by_sent = q(f"""
        SELECT d.overall_sentiment sentiment,
               count() records,
               sum(r.likes + r.comments_count + r.shares) total_eng,
               round(avg(r.likes + r.comments_count + r.shares), 1) avg_eng,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY sentiment ORDER BY total_eng DESC
    """)
    by_plat = q(f"""
        SELECT d.platform platform,
               sum(r.likes) likes, sum(r.shares) shares, sum(r.comments_count) comments
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY platform ORDER BY (likes+shares+comments) DESC
    """)
    by_type = q(f"""
        SELECT d.record_type record_type,
               sum(r.likes + r.comments_count + r.shares) total_eng
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY record_type
    """)
    trend = q(f"""
        SELECT {bkt.replace('record_date','d.record_date')} bucket,
               sum(r.likes + r.comments_count + r.shares) engagement
        FROM {base} d INNER JOIN {RAW_ENG} r USING (record_id)
        GROUP BY bucket ORDER BY bucket
    """)

    col1, col2 = st.columns(2)
    with col1:
        if not by_sent.empty:
            sc = by_sent["sentiment"].map({"Positif":C_POS,"Négatif":C_NEG,"Neutre":C_NEU})
            fig = go.Figure()
            fig.add_trace(go.Bar(x=by_sent["sentiment"], y=by_sent["total_eng"],
                                 name="Engagement total", marker_color=sc, yaxis="y"))
            fig.add_trace(go.Scatter(x=by_sent["sentiment"], y=by_sent["avg_eng"],
                                     name="Moy.", line_color="#1e293b", yaxis="y2", mode="lines+markers"))
            fig.update_layout(title="Engagement par sentiment", height=320,
                              yaxis=dict(side="left"), yaxis2=dict(side="right", overlaying="y", showgrid=False),
                              margin=dict(t=40,b=20))
            show(fig, use_container_width=True)
    with col2:
        if not by_plat.empty:
            fig2 = go.Figure()
            for label, col, color in [("Likes","likes",C_POS),("Partages","shares",C_OWN),("Commentaires","comments",C_NEU)]:
                fig2.add_trace(go.Bar(name=label, x=by_plat["platform"], y=by_plat[col], marker_color=color))
            fig2.update_layout(title="Détail par plateforme", height=320, margin=dict(t=40,b=20))
            show(fig2, use_container_width=True)

    col3, col4 = st.columns([2, 1])
    with col3:
        if not trend.empty:
            trend["bucket"] = trend["bucket"].astype(str).str[:10]
            fig3 = px.area(trend, x="bucket", y="engagement", title=f"Tendance d'engagement · {gran}",
                           color_discrete_sequence=[C_OWN])
            fig3.update_layout(height=300, margin=dict(t=40,b=20))
            show(fig3, use_container_width=True)
    with col4:
        if not by_type.empty:
            labels = by_type["record_type"].map({"post":"Post","comment":"Commentaire"}).fillna(by_type["record_type"])
            fig4 = go.Figure(go.Pie(labels=labels, values=by_type["total_eng"],
                                    hole=0.55, marker_colors=[C_OWN, C_COMP]))
            fig4 = _label_pie(fig4)
            fig4.update_layout(title="Post vs Commentaire", height=340, margin=dict(t=40,b=60))
            show(fig4, use_container_width=True)


def tab_language(start, end, entities, platforms, types, langs, gran, source_brands=None):
    w = _where(start, end, entities, platforms, types, langs, "language IS NOT NULL", source_brands=source_brands,
               voc_override_entity="CarrefourExpress")

    dist = q(f"""
        SELECT language, {REC} records, {POS} positives, {NEG} negatives, {NEU} neutrals
        FROM {DT} WHERE {w}
        GROUP BY language ORDER BY records DESC
    """)
    by_plat = q(f"""
        SELECT platform, language, {REC} records
        FROM {DT} WHERE {w}
        GROUP BY platform, language ORDER BY platform
    """)

    if dist.empty:
        st.info("Aucune donnée.")
        return

    col1, col2 = st.columns(2)
    with col1:
        fig = px.bar(dist, x="language", y="records", title="Volume par langue",
                     color="language", color_discrete_sequence=PAL)
        fig.update_layout(height=320, margin=dict(t=40,b=20), showlegend=False)
        show(fig, use_container_width=True)
    with col2:
        fig2 = go.Figure()
        for label, col, color in [("Positif","positives",C_POS),("Négatif","negatives",C_NEG),("Neutre","neutrals",C_NEU)]:
            fig2.add_trace(go.Bar(name=label, x=dist["language"], y=dist[col], marker_color=color))
        fig2.update_layout(barmode="stack", title="Sentiment par langue", height=320, margin=dict(t=40,b=20))
        show(fig2, use_container_width=True)

    if not by_plat.empty:
        fig3 = px.bar(by_plat, x="platform", y="records", color="language",
                      title="Langue × Plateforme", color_discrete_sequence=PAL)
        fig3.update_layout(barmode="stack", height=320, margin=dict(t=40,b=20))
        show(fig3, use_container_width=True)


def tab_topics():
    try:
        topics = q(f"SELECT topic_id, label, top_words, record_count FROM {RAW}.topics ORDER BY record_count DESC")
        trends = q(f"""
            SELECT entity, curr_count, curr_avg_score, curr_boycott_rate, curr_neg_rate,
                   prev_count, prev_avg_score, alerts
            FROM {RAW}.trends
        """)
    except Exception as e:
        st.error(f"Table topics/trends introuvable : {e}")
        return

    st.subheader("Modélisation de sujets (NMF)")
    if topics.empty:
        st.info("Aucun topic. Lancez le topic modeling.")
    else:
        cols = st.columns(3)
        for i, row in topics.iterrows():
            with cols[i % 3]:
                try:
                    words = __import__("json").loads(row["top_words"] or "[]")
                except Exception:
                    words = []
                tags = " · ".join(words[:10])
                st.info(f"**Topic {row['topic_id']}** — {row['record_count']} records\n\n{tags}")

    st.subheader("Alertes de tendance (période vs précédente)")
    if trends.empty:
        st.info("Aucune donnée de tendance.")
    else:
        for _, r in trends.iterrows():
            try:
                alerts = __import__("json").loads(r.get("alerts") or "[]")
                alert_str = "  ".join(f"`{a}`" for a in alerts) if alerts else "aucune alerte"
            except Exception:
                alert_str = "aucune alerte"
            st.markdown(
                f"**{r['entity']}** — mentions {r['prev_count']}→{r['curr_count']}, "
                f"score {r['prev_avg_score']}→{r['curr_avg_score']} {alert_str}"
            )


def tab_authors(platforms):
    plat_filter = _in("platform", platforms)
    where = plat_filter if plat_filter else "1=1"
    try:
        d = q(f"""
            SELECT author, platform, records, total_engagement,
                   round(avg_sentiment_score,3) avg_sentiment_score,
                   negative_pct, boycott_records
            FROM {GOLD}.fct_author_influence
            WHERE {where}
            ORDER BY total_engagement DESC LIMIT 25
        """)
    except Exception as e:
        st.error(f"Table fct_author_influence introuvable : {e}")
        return

    if d.empty:
        st.info("Aucune donnée.")
        return

    top10 = d.head(10)

    fig = px.bar(top10, x="total_engagement", y="author", orientation="h",
                 title="Top auteurs — engagement", color_discrete_sequence=[C_OWN])
    fig.update_layout(height=320, margin=dict(t=40,b=20), showlegend=False)
    show(fig, use_container_width=True)

    st.subheader("Détail des auteurs")
    disp = d.rename(columns={
        "author":"Auteur","platform":"Plateforme","records":"Posts",
        "total_engagement":"Engagement","avg_sentiment_score":"Score",
        "negative_pct":"Négatif %","boycott_records":"Boycott"
    })
    st.dataframe(disp.reset_index(drop=True), use_container_width=True)


def tab_explorer(start, end, entities, platforms, types, langs, all_themes, source_brands=None):
    st.subheader("Explorateur de publications")

    col1, col2, col3, col4, col5 = st.columns([2,2,2,1,1])
    theme_sel = col1.selectbox("Thème", ["Tous"] + all_themes)
    sent_sel  = col2.selectbox("Sentiment", ["Tous","Positif","Négatif","Neutre"])
    # Auteur (basé sur is_brand_voice — authorship + contexte) :
    #  - "Voix client"        = is_brand_voice=0 : contenu écrit par des utilisateurs
    #    (commentaires clients + posts de GROUPES par des users). Exclut posts de page
    #    et réponses CM.
    #  - "Contenu de la marque" = is_brand_voice=1 : posts de pages officielles + réponses
    #    CM (le nôtre ET les concurrents) → pour auditer notre communication.
    #  - "Tout".
    author_opts = {
        "Voix client (hors marque)":        "voc",
        "Contenu de la marque":             "brand",
        "Tout":                             "all",
    }
    author_sel = col3.selectbox("Auteur", list(author_opts.keys()))
    boycott   = col4.checkbox("Boycott seulement")
    limit     = col5.number_input("Limite", min_value=10, max_value=500, value=200, step=10)

    extra_parts = []
    if theme_sel != "Tous":    extra_parts.append(f"theme = '{_esc(theme_sel)}'")
    if sent_sel  != "Tous":    extra_parts.append(f"overall_sentiment = '{_esc(sent_sel)}'")
    if boycott:                extra_parts.append("boycott_signal = 1")

    # Le périmètre Auteur est délégué à _where via scope (is_brand_voice).
    w = _where(start, end, entities, platforms, types, langs,
               " AND ".join(extra_parts) if extra_parts else "",
               source_brands=source_brands,
               scope=author_opts[author_sel])

    df = q(f"""
        SELECT record_id, record_date, platform, record_type, language,
               source_brand, entity, theme, theme_sentiment, overall_sentiment,
               round(overall_score,3) overall_score, boycott_signal, is_brand_voice,
               author, url, text
        FROM {DT} WHERE {w}
        ORDER BY record_date DESC LIMIT {int(limit)}
    """)

    st.caption(f"{len(df)} résultats")
    if df.empty:
        return

    # clickhouse-connect renvoie des pd.NA (dtypes nullable) pour les colonnes
    # Nullable(...) — bool(pd.NA) lève "boolean value of NA is ambiguous" dans les
    # `x or default` ci-dessous. df.where(df.notna(), None) est un no-op sur les
    # dtypes nullable (Int64/boolean/string) : réassigner None y retombe sur
    # pd.NA. Il faut d'abord passer en dtype object pour que None reste None.
    df = df.astype(object).where(df.notna(), None)

    # Rendu en tableau HTML : les emojis et le texte RTL (arabe/darija) s'affichent
    # nativement, et la colonne « Source » est un lien cliquable vers la publication.
    sent_color = {"Positif": "#16a34a", "Négatif": "#dc2626", "Neutre": "#64748b"}
    rows_html = []
    for _, r in df.iterrows():
        txt = html.escape(str(r["text"] or "").strip())[:400] or "<span style='color:#94a3b8'>(emoji/réaction sans texte)</span>"
        sc  = sent_color.get(r["overall_sentiment"], "#64748b")
        boy = "🚫" if (pd.notna(r["boycott_signal"]) and r["boycott_signal"]) else ""
        url = str(r["url"] or "")
        src = (f"<a href='{html.escape(url)}' target='_blank' "
               f"style='color:#2563eb;text-decoration:none;white-space:nowrap'>Voir ↗</a>"
               if url.startswith("http") else "")
        # 🏢 = contenu écrit par une page de marque (post officiel ou réponse CM).
        badge = ("<span title='Contenu de la marque' "
                 "style='color:#2563eb'>🏢 </span>") if (pd.notna(r.get("is_brand_voice")) and r.get("is_brand_voice")) else ""
        author = html.escape(str(r["author"] or ""))
        rows_html.append(
            "<tr>"
            f"<td style='white-space:nowrap'>{str(r['record_date'])[:10]}</td>"
            f"<td>{html.escape(str(r['platform'] or ''))}</td>"
            f"<td>{html.escape(str(r['source_brand'] or ''))}</td>"
            f"<td>{html.escape(str(r['entity'] or ''))}</td>"
            f"<td>{html.escape(str(r['theme'] or ''))}</td>"
            f"<td style='color:{sc};font-weight:600'>{html.escape(str(r['overall_sentiment'] or ''))}</td>"
            f"<td style='text-align:center'>{boy}</td>"
            f"<td>{html.escape(str(r['language'] or ''))}</td>"
            f"<td style='white-space:nowrap'>{badge}{author}</td>"
            f"<td style='max-width:480px'>{txt}</td>"
            f"<td>{src}</td>"
            "</tr>"
        )

    table = (
        "<div style='max-height:600px;overflow:auto;border:1px solid #e2e8f0;border-radius:8px'>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        "<thead><tr style='position:sticky;top:0;background:#1e2530;color:#fff'>"
        "<th style='padding:6px 8px;text-align:left'>Date</th>"
        "<th style='padding:6px 8px;text-align:left'>Plateforme</th>"
        "<th style='padding:6px 8px;text-align:left'>Page source</th>"
        "<th style='padding:6px 8px;text-align:left'>Marque citée</th>"
        "<th style='padding:6px 8px;text-align:left'>Thème</th>"
        "<th style='padding:6px 8px;text-align:left'>Sentiment</th>"
        "<th style='padding:6px 8px'>Boy.</th>"
        "<th style='padding:6px 8px;text-align:left'>Langue</th>"
        "<th style='padding:6px 8px;text-align:left'>Auteur</th>"
        "<th style='padding:6px 8px;text-align:left'>Texte</th>"
        "<th style='padding:6px 8px;text-align:left'>Source</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html) +
        "</tbody></table></div>"
    )
    st.markdown(table, unsafe_allow_html=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="Social Listening — LabelVie",
        layout="wide",
        page_icon="📊",
    )

    if not check_password():
        return

    # Load meta
    try:
        meta = load_meta()
    except Exception as e:
        st.error(f"Impossible de joindre ClickHouse : {e}")
        return

    st.title("LabelVie · Social Listening")

    # ── Sidebar filters ──
    with st.sidebar:
        st.header("Filtres")

        # Période
        date_min = str(meta["date_min"] or "2024-01-01")[:10]
        date_max = str(meta["date_max"] or str(date.today()))[:10]
        preset = st.radio("Période", ["Tout","Année","90 jours","30 jours"], horizontal=True)
        d_max = datetime.strptime(date_max, "%Y-%m-%d").date()
        if preset == "30 jours":    d_min = d_max - timedelta(days=30)
        elif preset == "90 jours":  d_min = d_max - timedelta(days=90)
        elif preset == "Année":     d_min = date(d_max.year, 1, 1)
        else:                       d_min = datetime.strptime(date_min, "%Y-%m-%d").date()
        start = st.date_input("Du",  value=d_min)
        end   = st.date_input("Au",  value=d_max)

        # Granularité
        gran = st.radio("Granularité", ["day","week","month"],
                        format_func=lambda x: {"day":"Jour","week":"Semaine","month":"Mois"}[x],
                        horizontal=True, index=2)

        # Filtres multi-select
        brands_df = meta["brands"]
        brand_labels = {r["entity"]: r["display_name"] + (" ★" if r["is_own"] else "")
                        for _, r in brands_df.iterrows()} if not brands_df.empty else {}
        entities  = st.multiselect(
            "Marque mentionnée", list(brand_labels.keys()),
            format_func=lambda x: brand_labels.get(x, x),
            help="Marque CITÉE dans le texte (détectée par le NLP).")
        # Page source / terrain — d'où le record a été collecté (axe indépendant
        # de la marque mentionnée). Réutilise les libellés des marques.
        source_brands = st.multiselect(
            "Page source / terrain", meta.get("source_brands", []),
            format_func=lambda x: brand_labels.get(x, x),
            help="Page officielle D'OÙ le record vient. Croisez-la avec « Marque "
                 "mentionnée » : page = Marjane City + mention = Carrefour Express "
                 "→ sommes-nous cités chez le concurrent.")
        platforms = st.multiselect("Plateformes", meta["platforms"])
        types     = st.multiselect("Type",        meta["types"],
                                   format_func=lambda x: "Post" if x=="post" else "Commentaire")
        langs     = st.multiselect("Langue",      meta["langs"])

        st.markdown("---")
        if st.button("Vider le cache"):
            st.cache_data.clear()
            st.success("Cache vidé.")

    start_s = str(start)
    end_s   = str(end)

    # ── tabs ──
    tabs = st.tabs([
        "Vue d'ensemble","Concurrence","Sentiment","Thèmes",
        "Boycott","Engagement","Langue","Auteurs","Explorer",
    ])

    args = (start_s, end_s, entities, platforms, types, langs, gran, source_brands)

    with tabs[0]:
        try:    tab_overview(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[1]:
        try:    tab_competition(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[2]:
        try:    tab_sentiment(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[3]:
        try:    tab_themes(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[4]:
        try:    tab_boycott(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[5]:
        try:    tab_engagement(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[6]:
        try:    tab_language(*args)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[7]:
        try:    tab_authors(platforms)
        except Exception as e: st.error(f"Erreur : {e}")

    with tabs[8]:
        try:    tab_explorer(start_s, end_s, entities, platforms, types, langs, meta["themes"], source_brands)
        except Exception as e: st.error(f"Erreur : {e}")


if __name__ == "__main__":
    main()
