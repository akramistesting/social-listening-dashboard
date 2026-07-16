-- Drill-through table: one flat row per (record × entity × theme).
--
-- Entity attribution priority (recovers brand for records the NLP left blank):
--   1. Record's own detected entities (NLP).
--   2. Parent post's entities — for comments, matched on the numeric content-ID
--      shared by the comment's post_url and the post's url.
--   3. Author = brand page — comment authored by a brand's own page inherits it.
--   4. Source page (deterministic, name-based) — the page the record was scraped
--      from. The page slug is read straight from the URL:
--        • Facebook : facebook.com/<slug>/...        (posts AND comments)
--        • TikTok   : tiktok.com/@<slug>/...         (posts AND comments)
--        • Instagram: /p/<code>/ has no slug, so an IG comment inherits the page
--          of its parent post, joined on the shortcode <code>.
--      The slug is mapped to a brand by name (CarrefourExpressMaroc →
--      CarrefourExpress, marjanecity.maroc → MarjaneCity, …). This recovers the
--      vast majority of the volume the NLP left as "Nonidentifié".
--   5. Fallback 'Nonidentifié' — only when the page is genuinely unknown.
--
-- NOTE: posts are the brand's own voice (caption/description), not customer VoC,
-- so their sentiment is neutralised (Neutre / score 0) both overall (in
-- stg_social_records) and per-theme (here).
{{ config(materialized='table') }}

WITH
-- (2)/(4) parent-post entities/themes AND author, keyed by the numeric content-id
-- in the post url. No length(entities) filter so post_author is available even for
-- posts where the NLP detected no brand. argMax keeps the richest (non-empty)
-- entities/themes array for that post id. post_author lets a comment inherit the
-- page it was posted under — crucial for FB comments whose own url is
-- "facebook.com/video.php?v=<id>" (no page slug).
post_by_id AS (
    SELECT
        extract(coalesce(url, ''), '[0-9]{6,}')            AS pid,
        argMax(entities, length(entities))                 AS post_entities,
        argMax(theme_sentiments, length(theme_sentiments)) AS post_themes,
        any(author)                                        AS post_author
    FROM {{ ref('stg_social_records') }}
    WHERE record_type = 'post'
      AND extract(coalesce(url, ''), '[0-9]{6,}') != ''
    GROUP BY pid
),

-- (4-IG) parent-post author, keyed by the Instagram shortcode (/p/<code>/),
-- so an Instagram comment can inherit the page it was posted under.
post_by_code AS (
    SELECT
        extractGroups(coalesce(url, ''), '/(?:p|reel|reels)/([^/?]+)')[1] AS code,
        any(author) AS post_author
    FROM {{ ref('stg_social_records') }}
    WHERE record_type = 'post'
      AND extractGroups(coalesce(url, ''), '/(?:p|reel|reels)/([^/?]+)')[1] != ''
    GROUP BY code
),

-- (3) author → dominant entity, learned from posts the brand page itself published
author_entity AS (
    SELECT author, argMax(ent, c) AS entity
    FROM (
        SELECT author, arrayJoin(entities) AS ent, count() AS c
        FROM {{ ref('stg_social_records') }}
        WHERE record_type = 'post'
          AND author IS NOT NULL AND author != ''
          AND length(entities) > 0
        GROUP BY author, ent
    )
    GROUP BY author
),

-- Resolve the source-page token from the URL (or parent post for IG comments).
joined AS (
    SELECT
        r.record_id, r.platform, r.record_type, r.record_date, r.language,
        r.text, r.author AS author, r.url,
        r.overall_sentiment, r.overall_sentiment_score, r.boycott_signal,
        r.entities         AS own_entities,
        r.theme_sentiments AS own_themes,
        p.post_entities,
        p.post_themes,
        a.entity           AS author_brand,
        multiIf(
            -- POST : la page est l'auteur du post lui-même.
            r.record_type = 'post', r.author,
            -- COMMENT : priorité à l'auteur du post parent (le plus fiable, couvre
            -- les URLs Facebook "video.php?v=<id>" sans slug de page).
            p.post_author != '', p.post_author,
            -- COMMENT Instagram : auteur du post parent via le shortcode /p/<code>/.
            c.post_author IS NOT NULL AND c.post_author != '', c.post_author,
            -- COMMENT : slug de page directement dans l'URL (FB/TikTok), seulement
            -- si c'est une vraie page (pas video.php, photo, watch, groups…).
            extractGroups(coalesce(r.url, ''), 'facebook.com/([^/?]+)')[1] != ''
              AND extractGroups(coalesce(r.url, ''), 'facebook.com/([^/?]+)')[1] NOT LIKE '%.php'
              AND extractGroups(coalesce(r.url, ''), 'facebook.com/([^/?]+)')[1]
                  NOT IN ('photo', 'watch', 'groups', 'events', 'marketplace', 'reel', 'story'),
                extractGroups(coalesce(r.url, ''), 'facebook.com/([^/?]+)')[1],
            extractGroups(coalesce(r.url, ''), 'tiktok.com/@([^/?]+)')[1] != '',
                extractGroups(coalesce(r.url, ''), 'tiktok.com/@([^/?]+)')[1],
            -- Sinon : nom du commentateur (rarement une marque) → restera Nonidentifié.
            r.author
        ) AS source_token
    FROM {{ ref('stg_social_records') }} r
    LEFT JOIN post_by_id   p ON extract(coalesce(r.url, ''), '[0-9]{6,}') = p.pid
    LEFT JOIN author_entity a ON r.author = a.author
    LEFT JOIN post_by_code  c ON extractGroups(coalesce(r.url, ''), '/(?:p|reel|reels)/([^/?]+)')[1] = c.code
),

-- Map the source-page token → brand entity (deterministic, name-based).
with_brand AS (
    SELECT
        *,
        multiIf(
            positionCaseInsensitive(source_token, 'carrefour') > 0
              AND positionCaseInsensitive(source_token, 'express') > 0, 'CarrefourExpress',
            positionCaseInsensitive(source_token, 'carrefour') > 0
              AND positionCaseInsensitive(source_token, 'gourmet') > 0, 'CarrefourGourmet',
            positionCaseInsensitive(source_token, 'carrefour') > 0
              AND positionCaseInsensitive(source_token, 'market')  > 0, 'CarrefourMarket',
            positionCaseInsensitive(source_token, 'carrefour') > 0,     'Carrefour',
            positionCaseInsensitive(source_token, 'marjane') > 0
              AND positionCaseInsensitive(source_token, 'city')    > 0, 'MarjaneCity',
            positionCaseInsensitive(source_token, 'marjane') > 0,       'Marjane',
            positionCaseInsensitive(source_token, 'labelvie') > 0,      'LabelVie',
            positionCaseInsensitive(source_token, 'atacad') > 0,        'Atacadao',
            positionCaseInsensitive(source_token, 'suppeco') > 0,       'Suppeco',
            positionCaseInsensitive(source_token, 'asswak') > 0,        'AsswakEssalam',
            positionCaseInsensitive(source_token, 'kazyon') > 0,        'Kazyon',
            positionCaseInsensitive(source_token, 'bringo') > 0,        'Bringo',
            positionCaseInsensitive(source_token, 'bim') > 0,           'BIM',
            positionCaseInsensitive(source_token, 'hyperu') > 0,        'HyperU',
            ''
        ) AS source_brand,
        -- Auteur DU COMMENTAIRE mappé à une marque par son nom : sert à détecter
        -- les réponses postées par une page de marque (community management), que
        -- ce soit une page du groupe LabelVie (Carrefour, Express, Market, Gourmet,
        -- Suppeco, Bringo…) ou d'un concurrent. coalesce car author est Nullable.
        multiIf(
            positionCaseInsensitive(coalesce(author, ''), 'carrefour') > 0
              AND positionCaseInsensitive(coalesce(author, ''), 'express') > 0, 'CarrefourExpress',
            positionCaseInsensitive(coalesce(author, ''), 'carrefour') > 0
              AND positionCaseInsensitive(coalesce(author, ''), 'gourmet') > 0, 'CarrefourGourmet',
            positionCaseInsensitive(coalesce(author, ''), 'carrefour') > 0
              AND positionCaseInsensitive(coalesce(author, ''), 'market')  > 0, 'CarrefourMarket',
            positionCaseInsensitive(coalesce(author, ''), 'carrefour') > 0,     'Carrefour',
            positionCaseInsensitive(coalesce(author, ''), 'marjane') > 0
              AND positionCaseInsensitive(coalesce(author, ''), 'city')    > 0, 'MarjaneCity',
            positionCaseInsensitive(coalesce(author, ''), 'marjane') > 0,       'Marjane',
            positionCaseInsensitive(coalesce(author, ''), 'labelvie') > 0,      'LabelVie',
            positionCaseInsensitive(coalesce(author, ''), 'bringo') > 0,        'Bringo',
            positionCaseInsensitive(coalesce(author, ''), 'atacad') > 0,        'Atacadao',
            positionCaseInsensitive(coalesce(author, ''), 'suppeco') > 0,       'Suppeco',
            positionCaseInsensitive(coalesce(author, ''), 'asswak') > 0,        'AsswakEssalam',
            positionCaseInsensitive(coalesce(author, ''), 'kazyon') > 0,        'Kazyon',
            positionCaseInsensitive(coalesce(author, ''), 'bim') > 0,           'BIM',
            positionCaseInsensitive(coalesce(author, ''), 'hyperu') > 0,        'HyperU',
            ''
        ) AS author_page_brand
    FROM joined
),

enriched AS (
    SELECT
        record_id, platform, record_type, record_date, language,
        text, author, url, overall_sentiment, overall_sentiment_score, boycott_signal,
        -- Page d'où le record a été collecté (le "terrain"), indépendante de la
        -- marque citée dans le texte (entity). Permet de croiser les deux axes :
        -- p.ex. source_brand='MarjaneCity' AND entity='CarrefourExpress'.
        if(source_brand != '', source_brand, 'Nonidentifié') AS source_brand,
        multiIf(
            length(own_entities) > 0,                               own_entities,
            record_type = 'comment' AND length(post_entities) > 0,  post_entities,
            record_type = 'comment' AND author_brand != '',         [author_brand],
            source_brand != '',                                     [source_brand],
            ['Nonidentifié']
        ) AS entities,
        -- Un commentaire ne porte QUE le thème détecté par le NLP dans son propre
        -- texte. On n'hérite PLUS du thème du post parent : un simple tag de nom
        -- ("Gohmid Mohamed") ou un compliment générique ("C'est très bon") n'a pas
        -- de thème propre et doit retomber en "Autre" — pas hériter "prix"/"livraison"
        -- du post sous lequel il est publié.
        multiIf(
            length(own_themes) > 0,                                 own_themes,
            [concat('{"theme":"Autre","sentiment":"', coalesce(overall_sentiment, 'Neutre'),
                    '","score":', toString(round(coalesce(overall_sentiment_score, 0), 3)), '}')]
        ) AS theme_sentiments,
        -- Réponse postée par une page de marque (community management) : détectée
        -- par le nom de l'auteur (author_page_brand) ou par l'historique des posts
        -- publiés par cet auteur (author_brand). Conservée avec son vrai sentiment
        -- pour inspection dans l'explorer, mais exclue des agrégats voix-client.
        toUInt8(record_type = 'comment'
                AND (author_page_brand != '' OR author_brand != '')) AS is_brand_reply,
        -- VOIX DE MARQUE (critère pérenne, authorship + contexte) :
        --   1) un POST publié depuis une page officielle (pas un groupe), OU
        --   2) tout contenu écrit par une page de marque (post OU commentaire).
        -- => VoC = is_brand_voice = 0. Un post de GROUPE écrit par un utilisateur a
        --    is_brand_voice=0 et entre donc automatiquement dans la voix client.
        toUInt8(
            (record_type = 'post' AND position(coalesce(url, ''), '/groups/') = 0)
            OR author_page_brand != '' OR author_brand != ''
        ) AS is_brand_voice
    FROM with_brand
),

exploded_entities AS (
    SELECT
        record_id, platform, record_type, record_date, language,
        text, author, url, source_brand, overall_sentiment, overall_sentiment_score,
        theme_sentiments, boycott_signal, is_brand_reply, is_brand_voice,
        arrayJoin(entities) AS entity_raw
    FROM enriched
),

with_entity AS (
    SELECT
        record_id, platform, record_type, record_date, language,
        text, author, url, source_brand, overall_sentiment, overall_sentiment_score,
        theme_sentiments, boycott_signal, is_brand_reply, is_brand_voice,
        replaceAll(replaceAll(entity_raw, '"', ''), ' ', '') AS entity
    FROM exploded_entities
    WHERE entity_raw != ''
),

exploded_themes AS (
    SELECT
        record_id, platform, record_type, record_date, language,
        text, author, url, source_brand, entity, overall_sentiment,
        overall_sentiment_score, boycott_signal, is_brand_reply, is_brand_voice,
        arrayJoin(theme_sentiments) AS ts_raw
    FROM with_entity
)

SELECT
    record_id,
    platform,
    record_type,
    record_date,
    language,
    text,
    author,
    url,
    source_brand,
    entity,
    JSONExtractString(ts_raw, 'theme')                              AS theme,
    -- Post d'une page officielle = voix de marque → thème neutralisé aussi (même
    -- règle que le sentiment global dans stg). Un commentaire de marque (CM) garde
    -- son vrai sentiment de thème pour l'audit ; il est exclu du VoC via is_brand_voice.
    if(record_type = 'post' AND position(coalesce(url, ''), '/groups/') = 0, 'Neutre',
       JSONExtractString(ts_raw, 'sentiment'))                      AS theme_sentiment,
    if(record_type = 'post' AND position(coalesce(url, ''), '/groups/') = 0, toFloat32(0),
       toFloat32OrNull(toString(JSONExtractFloat(ts_raw, 'score')))) AS theme_score,
    overall_sentiment,
    overall_sentiment_score                                         AS overall_score,
    toUInt8(boycott_signal)                                         AS boycott_signal,
    toUInt8(is_brand_reply)                                         AS is_brand_reply,
    toUInt8(is_brand_voice)                                         AS is_brand_voice,
    multiIf(
        overall_sentiment = 'Positif',  toInt8(1),
        overall_sentiment = 'Négatif', toInt8(-1),
        toInt8(0)
    )                                                               AS sentiment_direction
FROM exploded_themes
WHERE JSONExtractString(ts_raw, 'theme') != ''
ORDER BY record_date DESC, record_id, entity, theme
