"""Persistent, bounded references to collected news; paid work only in regular hours."""
from dataclasses import fields
from datetime import timedelta
import re

from ..market.bars import NY, session_bounds
from ..models import NewsEvent, utc
from ..macro.models import MacroArticle, stable_hash
from .intake import company_reason, macro_reason, time_reason

STATE = 'news_analysis_queue_v1'
MAX_PENDING = 240
MAX_DONE = 2000
BATCH_SIZE = 3
BATCH_SECONDS = 300
MAX_ATTEMPTS = 3


def session_phase(now):
    now = utc(now)
    bounds = session_bounds(now)
    if not bounds:
        return 'CLOSED'
    if now < bounds[0]:
        return 'PREMARKET'
    return 'REGULAR' if now < bounds[1] else 'CLOSED'


class NewsAnalysisQueue:
    def __init__(self, repo, *, clock, version, company_max_age=24, macro_max_age=72):
        self.repo, self.clock, self.version = repo, clock, version
        self.company_max_age, self.macro_max_age = company_max_age, macro_max_age

    def _change(self, operation, now):
        now = utc(now)
        def change(state):
            state = state or {}
            state.setdefault('items', {})
            state.setdefault('done', {})
            day = now.astimezone(NY).date().isoformat()
            if state.get('day') != day:
                state['day'] = day
                state['counts'] = {}
            counts = state.setdefault('counts', {})
            for key, item in list(state['items'].items()):
                if utc(item['expires_at']) <= now:
                    del state['items'][key]
                    counts['expired'] = counts.get('expired', 0) + 1
            state['done'] = {k: v for k, v in state['done'].items() if utc(v) > now}
            operation(state)
            # References only: no article bodies or unbounded audit history in controls.
            state['done'] = dict(sorted(state['done'].items(), key=lambda x: x[1], reverse=True)[:MAX_DONE])
            state['updated_at'] = now.isoformat()
            return state
        return self.repo.update_control(STATE, change, {})

    def enqueue(self, receipt, *, kind, ticker='', primary=False, now, fingerprint_raw=None):
        raw = fingerprint_raw or receipt['raw']
        # Conservative syndication dedupe: identical text after whitespace/case
        # normalization, same publication date and same analysis target. Revisions
        # or opposite claims remain separate; no LLM is used for deduplication.
        normalize = lambda value: re.sub(r'\s+', ' ', value).strip().casefold()
        published = utc(raw['published_at'])
        key = stable_hash([kind, ticker, primary, published.date().isoformat(),
                           normalize(raw['title']), normalize(raw['body']), self.version])
        age = self.company_max_age if kind == 'company' else self.macro_max_age
        item = {'receipt_id': receipt['id'], 'kind': kind, 'ticker': ticker,
                'provider': receipt['provider'], 'primary': primary,
                'published_at': published.isoformat(),
                'expires_at': (published + timedelta(hours=age)).isoformat(), 'attempts': 0}
        def add(state):
            counts = state['counts']
            counts['received'] = counts.get('received', 0) + 1
            if key in state['items'] or key in state['done']:
                counts['duplicates'] = counts.get('duplicates', 0) + 1
                return
            if utc(item['expires_at']) <= utc(now):
                counts['expired'] = counts.get('expired', 0) + 1
                return
            state['items'][key] = item
            if len(state['items']) > MAX_PENDING:
                oldest = min(state['items'], key=lambda k: state['items'][k]['published_at'])
                del state['items'][oldest]
                counts['capacity_excluded'] = counts.get('capacity_excluded', 0) + 1
        self._change(add, now)

    def _restore(self, item, now):
        receipt = self.repo.get_record('news_ingestions', item['receipt_id'])
        if not receipt or not isinstance(receipt.get('raw'), dict):
            return None, 'stored_article_missing'
        raw = receipt['raw']
        cls = NewsEvent if item['kind'] == 'company' else MacroArticle
        value = {f.name: raw[f.name] for f in fields(cls) if f.name in raw}
        if cls is MacroArticle and raw.get('discovery_source') == 'gdelt':
            value.setdefault('body', '')  # publisher text is re-fetched, not retained
        for name in ('published_at', 'received_at', 'first_available_at'):
            if value.get(name):
                value[name] = utc(value[name])
        article = cls(**value)
        if item['kind'] == 'company':
            reason = company_reason(article, item['ticker'], item['provider'], item['primary'], now, self.company_max_age)
        elif article.discovery_source == 'gdelt':
            reason = time_reason(article.published_at, article.received_at, now, self.macro_max_age)
        else:
            reason = macro_reason(article, now, self.macro_max_age)
        return article, reason

    async def drain(self, process, *, held=(), focused=(), now=None):
        now = utc(now or self.clock())
        if session_phase(now) != 'REGULAR':
            self._change(lambda state: None, now)
            return
        chosen = []
        def claim(state):
            if state.get('next_batch_at') and utc(state['next_batch_at']) > now:
                return
            def rank(pair):
                item = pair[1]
                priority = 0 if item['ticker'] in held else 1 if item['ticker'] in focused else 2 if item['kind'] == 'macro' else 3
                return priority, -utc(item['published_at']).timestamp()
            ready = [(k, v) for k, v in state['items'].items()
                     if not v.get('retry_at') or utc(v['retry_at']) <= now]
            for key, item in sorted(ready, key=rank)[:BATCH_SIZE]:
                # Persist the retry delay before the request, including across crashes.
                item['retry_at'] = (now + timedelta(minutes=15)).isoformat()
                chosen.append((key, dict(item)))
            if chosen:
                state['next_batch_at'] = (now + timedelta(seconds=BATCH_SECONDS)).isoformat()
        self._change(claim, now)
        for key, item in chosen:
            current = max(now, utc(self.clock()))
            if session_phase(current) != 'REGULAR':
                break
            try:
                article, reason = self._restore(item, current)
                error = reason or await process(article, item, current) or ''
            except (ValueError, TypeError, KeyError):
                error = 'stored_article_invalid'
            except (RuntimeError, OSError):
                error = 'analysis_retry'
            blocked = error in {'llm_budget_limit', 'budget_or_provider_cooldown', 'llm_in_progress'}
            terminal = error in {'stored_article_missing', 'stored_article_invalid', 'stale_article',
                                 'invalid_timestamp', 'unverified_article_identity', 'primary_source_unverified',
                                 'ticker_not_in_article_symbols', 'article_body_missing', 'title_only_article'}
            def finish(state):
                stored = state['items'].get(key)
                if not stored:
                    return
                if not blocked:
                    stored['attempts'] += 1
                stored['last_error'] = error
                if not error or terminal or stored['attempts'] >= MAX_ATTEMPTS:
                    state['items'].pop(key)
                    state['done'][key] = stored['expires_at']
                    name = 'analyzed' if not error else 'excluded'
                    state['counts'][name] = state['counts'].get(name, 0) + 1
                if blocked:
                    state['next_batch_at'] = (current + timedelta(minutes=15)).isoformat()
            self._change(finish, current)
            if blocked:
                break

    def snapshot(self, now=None):
        now = utc(now or self.clock())
        state = self.repo.get_control(STATE, {}) or {}
        return {'phase': session_phase(now), 'updated_at': state.get('updated_at'),
                'day': state.get('day'), 'pending': len(state.get('items', {})),
                'counts': state.get('counts', {}), 'next_batch_at': state.get('next_batch_at'),
                'batch_size': BATCH_SIZE, 'batch_seconds': BATCH_SECONDS,
                'max_pending': MAX_PENDING}
