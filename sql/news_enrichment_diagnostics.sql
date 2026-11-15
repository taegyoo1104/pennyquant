-- MySQL 8, SELECT only. Replace every REPLACE_WITH_PAPER_NAMESPACE with the
-- exact namespace returned by db-stats. No USE, SET, migration or cleanup runs here.
-- JSON timestamps retain their original explicit UTC offset; no local-time inference.

SELECT 'news_ingestions' AS journal, COUNT(*) AS records FROM pq_news_ingestions
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
UNION ALL
SELECT 'news_enrichments', COUNT(*) FROM pq_news_enrichments
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
UNION ALL
SELECT 'macro_context_snapshots', COUNT(*) FROM pq_macro_context_snapshots
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE';

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.provider')) AS provider,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.skip_reason')) AS skip_reason,
       COUNT(*) AS receipts,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.duplicate')) = 'true') AS duplicates,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.eligible_for_llm')) = 'true') AS eligible_receipts
FROM pq_news_ingestions
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
GROUP BY provider, skip_reason ORDER BY receipts DESC;

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.article_id')) AS article_id,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.provider')) AS provider,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.published_at')) AS published_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.received_at')) AS received_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.first_available_at')) AS first_available_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.content_hash')) AS content_hash,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.skip_reason')) AS skip_reason
FROM pq_news_ingestions
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
ORDER BY seq DESC LIMIT 100;

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.publisher_domain')) AS publisher_domain,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.status')) AS status,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.block_reason')) AS block_reason,
       COUNT(*) AS records,
       MAX(JSON_UNQUOTE(JSON_EXTRACT(payload, '$.checked_at'))) AS last_checked_at
FROM pq_news_enrichments
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
GROUP BY publisher_domain, status, block_reason ORDER BY records DESC;

SELECT p.provider,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".status'))) AS status,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".last_attempt_at'))) AS last_attempt_at,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".last_success_at'))) AS last_success_at,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".calls'))) AS calls,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".http_403'))) AS http_403,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".http_429'))) AS http_429,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".cooldown_until'))) AS cooldown_until,
       JSON_UNQUOTE(JSON_EXTRACT(c.payload, CONCAT('$."', p.provider, '".last_error'))) AS last_error
FROM pq_controls AS c
JOIN JSON_TABLE(JSON_KEYS(c.payload), '$[*]' COLUMNS(provider VARCHAR(191) PATH '$')) AS p
WHERE c.namespace = 'REPLACE_WITH_PAPER_NAMESPACE' AND c.name = 'news_provider_schedule_v1'
ORDER BY p.provider;

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.context_at')) AS context_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.quality')) AS quality,
       JSON_EXTRACT(payload, '$.reasons') AS reasons,
       JSON_LENGTH(JSON_EXTRACT(payload, '$.official_daily_indicators')) AS daily_indicators,
       JSON_LENGTH(JSON_EXTRACT(payload, '$.intraday_market_proxies')) AS etf_proxies,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.causal_inference_verified')) AS causal_inference_verified
FROM pq_macro_context_snapshots
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
ORDER BY seq DESC LIMIT 30;

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.id')) AS version_id,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.logical_id')) AS logical_id,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.revision')) AS revision,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.status')) AS status,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.scheduled_at')) AS scheduled_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.known_at')) AS known_at,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.source_url')) AS source_url
FROM pq_economic_events
WHERE namespace = 'REPLACE_WITH_PAPER_NAMESPACE'
ORDER BY seq DESC LIMIT 100;
