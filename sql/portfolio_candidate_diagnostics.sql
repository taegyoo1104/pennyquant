-- MySQL 8 SELECT 전용. REPLACE_WITH_PAPER_NAMESPACE를 실제 PAPER namespace로 바꾼다.
-- 관측된 선정 시각의 이력이다. 현재 유효성은 valid_until과 원본 자료를 함께 확인한다.
SELECT payload->>'$.id' AS selection_id,payload->>'$.evaluated_at' AS evaluated_at,
       payload->>'$.valid_until' AS valid_until,payload->>'$.common_closed_bar_at' AS common_closed_bar_at,
       payload->>'$.evaluated_count' AS evaluated_count,payload->>'$.final_candidate_count' AS final_candidates,
       payload->>'$.approved_new_count' AS new_approvals,payload->>'$.approved_add_count' AS add_approvals,
       payload->>'$.macro_reference_risk.status' AS macro_reference_risk,
       payload->>'$.source_selection_id' AS replay_source_selection_id
FROM pq_pipeline_status
WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE' AND payload->>'$.kind'='portfolio_selection'
ORDER BY seq DESC LIMIT 50;

SELECT s.payload->>'$.id' AS selection_id,s.payload->>'$.evaluated_at' AS evaluated_at,
       r.symbol,r.candidate_rank,r.candidate_score,r.portfolio_status,r.allocation_reason,
       r.news_status,r.sector_status,r.news_analysis_ids,r.sector_reason,r.blockers,
       r.order_intent_id,o.payload->>'$.client_id' AS stored_order_intent_id,
       o.payload->>'$.signal_id' AS signal_id,o.payload->>'$.amount' AS paper_planned_amount
FROM pq_pipeline_status s
JOIN JSON_TABLE(s.payload,'$.rows[*]' COLUMNS(
 symbol VARCHAR(32) PATH '$.symbol',candidate_rank INT PATH '$.rank',
 candidate_score DECIMAL(12,4) PATH '$.candidate_score',portfolio_status VARCHAR(64) PATH '$.portfolio_status',
 allocation_reason VARCHAR(128) PATH '$.allocation_reason',news_status VARCHAR(32) PATH '$.indicators.score_component_status.news',
 sector_status VARCHAR(32) PATH '$.indicators.score_component_status.sector',
 news_analysis_ids JSON PATH '$.news_score_signal.analysis_ids',sector_reason VARCHAR(128) PATH '$.sector_strength.reason',
 blockers JSON PATH '$.blockers',order_intent_id VARCHAR(200) PATH '$.order_intent_id')) r
LEFT JOIN pq_order_intents o ON o.namespace=s.namespace AND o.payload->>'$.client_id'=r.order_intent_id
WHERE s.namespace='REPLACE_WITH_PAPER_NAMESPACE' AND s.payload->>'$.kind'='portfolio_selection'
ORDER BY s.seq DESC,r.candidate_rank LIMIT 200;

SELECT o.payload->>'$.client_id' AS order_intent_id,o.payload->>'$.selection_id' AS selection_id,
       o.payload->>'$.symbol' AS symbol,'SELECTION_LINK_MISSING' AS discrepancy
FROM pq_order_intents o LEFT JOIN pq_pipeline_status s
 ON s.namespace=o.namespace AND s.payload->>'$.id'=o.payload->>'$.selection_id'
 AND s.payload->>'$.kind'='portfolio_selection'
WHERE o.namespace='REPLACE_WITH_PAPER_NAMESPACE' AND o.payload->>'$.selection_id' IS NOT NULL AND s.seq IS NULL;

SELECT payload->>'$.client_id' AS order_intent_id,payload->>'$.symbol' AS symbol,'LEGACY_UNMAPPED' AS mapping_status
FROM pq_order_intents WHERE namespace='REPLACE_WITH_PAPER_NAMESPACE'
AND JSON_EXTRACT(payload,'$.selection_id') IS NULL ORDER BY seq DESC LIMIT 50;
