-- MySQL 8 SELECT 전용. 모든 REPLACE_WITH_PAPER_NAMESPACE를 db-stats의
-- 정확한 namespace로 바꾼다. 본문 암호문·인증정보는 조회하지 않는다.

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status')) AS status,
       JSON_UNQUOTE(JSON_EXTRACT(payload,'$.validation_error_code')) AS validation_error_code,
       JSON_UNQUOTE(JSON_EXTRACT(payload,'$.failed_field')) AS failed_field,
       COUNT(*) AS records,
       SUM(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.network_attempted'))='true') AS network_attempts
FROM pq_llm_calls WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
GROUP BY status,validation_error_code,failed_field ORDER BY records DESC;

SELECT c.payload->>'$.llm_call_id' AS llm_call_id,c.payload->>'$.logical_analysis_id' AS analysis_id,
       c.payload->>'$.article_id' AS article_id,c.payload->>'$.content_hash' AS content_hash,
       c.payload->>'$.model' AS model,c.payload->>'$.stage' AS stage,c.payload->>'$.status' AS status,
       c.payload->>'$.request_started_at' AS request_started_at,c.payload->>'$.provider_request_id' AS provider_request_id,
       c.payload->>'$.input_hash' AS input_hash,c.payload->>'$.response_hash' AS response_hash,
       u.payload->>'$.input_tokens' AS input_tokens,u.payload->>'$.output_tokens' AS output_tokens,
       u.payload->>'$.cached_input_tokens' AS cached_input_tokens,
       u.payload->>'$.cost_usd' AS program_estimated_cost_usd,u.payload->>'$.cost_status' AS cost_status,
       u.payload->>'$.provider_billed_cost_usd' AS provider_billed_cost_usd
FROM pq_llm_calls c LEFT JOIN pq_llm_usage u
 ON u.namespace=c.namespace AND u.payload->>'$.llm_call_id'=c.payload->>'$.llm_call_id'
WHERE c.namespace='REPLACE_WITH_PAPER_NAMESPACE'
ORDER BY c.seq DESC LIMIT 200;

SELECT 'company' AS analysis_kind,a.payload->>'$.analysis_id' AS analysis_id,
       a.payload->>'$.ticker' AS ticker,a.payload->>'$.news_id' AS article_id,ids.llm_call_id,
       c.payload->>'$.stage' AS stage,c.payload->>'$.status' AS call_status,
       a.payload->>'$.adopted_llm_call_id' AS adopted_llm_call_id,a.payload->>'$.error' AS final_error
FROM pq_news_analysis a
JOIN JSON_TABLE(COALESCE(JSON_EXTRACT(a.payload,'$.llm_call_ids'),JSON_ARRAY()),'$[*]'
 COLUMNS(llm_call_id VARCHAR(64) PATH '$')) ids
LEFT JOIN pq_llm_calls c ON c.namespace=a.namespace AND c.payload->>'$.llm_call_id'=ids.llm_call_id
WHERE a.namespace='REPLACE_WITH_PAPER_NAMESPACE'
UNION ALL
SELECT 'macro',a.payload->>'$.id',NULL,a.payload->>'$.article_id',ids.llm_call_id,
       c.payload->>'$.stage',c.payload->>'$.status',a.payload->>'$.adopted_llm_call_id',a.payload->>'$.error'
FROM pq_macro_analysis a
JOIN JSON_TABLE(COALESCE(JSON_EXTRACT(a.payload,'$.llm_call_ids'),JSON_ARRAY()),'$[*]'
 COLUMNS(llm_call_id VARCHAR(64) PATH '$')) ids
LEFT JOIN pq_llm_calls c ON c.namespace=a.namespace AND c.payload->>'$.llm_call_id'=ids.llm_call_id
WHERE a.namespace='REPLACE_WITH_PAPER_NAMESPACE';

SELECT payload->>'$.id' AS usage_id,payload->>'$.model' AS model,
       payload->>'$.timestamp' AS timestamp,payload->>'$.cost_usd' AS estimated_cost_usd,
       'LEGACY_UNMAPPED' AS mapping_status
FROM pq_llm_usage WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
AND (JSON_EXTRACT(payload,'$.llm_call_id') IS NULL OR payload->>'$.llm_call_id'='null')
ORDER BY seq DESC LIMIT 100;

SELECT u.payload->>'$.llm_call_id' AS llm_call_id,u.payload->>'$.article_id' AS article_id,
       u.payload->>'$.cost_status' AS cost_status,'USAGE_WITHOUT_CALL' AS discrepancy
FROM pq_llm_usage u LEFT JOIN pq_llm_calls c
 ON c.namespace=u.namespace AND c.payload->>'$.llm_call_id'=u.payload->>'$.llm_call_id'
WHERE u.namespace='REPLACE_WITH_PAPER_NAMESPACE' AND u.payload->>'$.llm_call_id' IS NOT NULL
AND c.seq IS NULL;

SELECT c.payload->>'$.llm_call_id' AS llm_call_id,c.payload->>'$.article_id' AS article_id,
       c.payload->>'$.status' AS status,'REQUEST_WITHOUT_SETTLEMENT' AS discrepancy
FROM pq_llm_calls c LEFT JOIN pq_llm_usage u
 ON u.namespace=c.namespace AND u.payload->>'$.llm_call_id'=c.payload->>'$.llm_call_id'
WHERE c.namespace='REPLACE_WITH_PAPER_NAMESPACE' AND c.payload->>'$.network_attempted'='true' AND u.seq IS NULL;

SELECT payload->>'$.llm_call_id' AS llm_call_id,COUNT(*) AS records,
       COUNT(DISTINCT payload->>'$.article_id') AS distinct_articles
FROM pq_llm_usage WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE' AND payload->>'$.llm_call_id' IS NOT NULL
GROUP BY llm_call_id HAVING COUNT(*)>1 OR COUNT(DISTINCT payload->>'$.article_id')<>1;

SELECT payload->>'$.llm_call_id' AS llm_call_id,payload->>'$.logical_analysis_id' AS logical_analysis_id,
       payload->>'$.status' AS status,'INPUT_ID_MISSING' AS discrepancy
FROM pq_llm_calls WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
AND payload->>'$.network_attempted'='true'
AND (NULLIF(payload->>'$.article_id','') IS NULL OR payload->>'$.article_id'='null'
     OR NULLIF(payload->>'$.content_hash','') IS NULL OR payload->>'$.content_hash'='null');

-- 기록된 원문 보강 후보의 버전별 funnel. 전체 GDELT 검색 결과 수와는 다르다.
-- 서로 다른 원문 버전은 별개 표본이며 캐시/재시도 호출은 표본 수를 늘리지 않는다.
WITH enriched AS (
 SELECT payload->>'$.article_id' AS article_id,
        COALESCE(NULLIF(payload->>'$.document_version','null'),payload->>'$.content_hash') AS document_version,
        COALESCE(NULLIF(payload->>'$.publisher_name','null'),payload->>'$.publisher_domain','UNKNOWN') AS publisher,
        MAX(COALESCE(CAST(payload->>'$.body_char_count' AS UNSIGNED),0)>0) AS has_body,
        MAX(payload->>'$.eligible_for_llm'='true') AS eligible
 FROM pq_news_enrichments WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
 GROUP BY article_id,document_version,publisher
), calls AS (
 SELECT payload->>'$.article_id' AS article_id,payload->>'$.document_version' AS document_version,
        MAX(payload->>'$.network_attempted'='true') AS requested,
        MAX(payload->>'$.status'='SUCCEEDED') AS succeeded,
        MAX(payload->>'$.status'='SUCCEEDED' AND payload->>'$.primary_official_verified'='true') AS official_succeeded
 FROM pq_llm_calls WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
 GROUP BY article_id,document_version
)
SELECT e.publisher,COUNT(*) AS recorded_versions,SUM(e.has_body) AS body_versions,
       SUM(e.eligible) AS eligible_versions,SUM(COALESCE(c.requested,0)) AS requested_versions,
       SUM(COALESCE(c.succeeded,0)) AS classified_versions,
       SUM(COALESCE(c.official_succeeded,0)) AS official_classified_versions,
       SUM(e.has_body)/NULLIF(COUNT(*),0) AS body_rate,
       SUM(e.eligible)/NULLIF(SUM(e.has_body),0) AS eligible_given_body_rate,
       SUM(COALESCE(c.requested,0))/NULLIF(SUM(e.eligible),0) AS requested_given_eligible_rate,
       SUM(COALESCE(c.succeeded,0))/NULLIF(SUM(COALESCE(c.requested,0)),0) AS classified_given_request_rate
FROM enriched e LEFT JOIN calls c ON c.article_id=e.article_id AND c.document_version=e.document_version
GROUP BY e.publisher ORDER BY recorded_versions DESC;

SELECT payload->>'$.reference_event_id' AS reference_event_id,payload->>'$.status' AS status,
       payload->>'$.skipped_precheck_reason' AS skip_reason,payload->>'$.error' AS error,
       JSON_EXTRACT(payload,'$.llm_call_ids') AS llm_call_ids,payload->>'$.causal_inference_verified' AS causal_inference_verified
FROM pq_macro_context_analysis WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
ORDER BY seq DESC LIMIT 100;
