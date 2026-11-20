"""Stage 1: bounded read-only quote receiver. No DB, orders or engine callbacks.

One asyncio loop owns this object. A subscription change closes the old connection
before opening the replacement; market data is invalidated across generations.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from time import monotonic

from ..models import utc
from .common import now_utc
from .toss_market import symbols_parameter
from .toss_websocket import TossWebSocket, quote_from_frame


class TossQuoteFeed:
    def __init__(self, rest, *, stream=None, clock=now_utc, max_age_seconds=30,
                 retry_initial=1., retry_max=30., rest_timeout=15.):
        if not 0<retry_initial<=retry_max or rest_timeout<=0 or max_age_seconds<=0:
            raise ValueError('positive feed timeouts required')
        self.rest,self.stream,self.clock = rest,stream or TossWebSocket(rest),clock
        self.max_age,self.retry_initial,self.retry_max,self.rest_timeout = max_age_seconds,retry_initial,retry_max,rest_timeout
        self._desired=(); self._priority=(); self._quotes={}; self._conflicts={}; self._history={}
        self._changed=asyncio.Event(); self._stop=False; self._running=False
        self._acked=False; self._generation=0; self._source={}
        self.state='IDLE'; self.last_error=None; self.retry_seconds=0.
        self.counts={k:0 for k in ('connections','reconnects','frames','quotes','rest_failures','invalid_quotes','ignored_quotes')}

    def set_symbols(self, candidates, *, held=(), open_orders=()):
        candidates=set(candidates); priority=set(held)|set(open_orders)
        desired=tuple(sorted(candidates|priority))
        if len(candidates)>50:raise ValueError('at most 50 candidate symbols')
        if len(desired)>100:raise ValueError('at most 100 orderbook topics; no silent risk-symbol truncation')
        if desired:symbols_parameter(desired,maximum=100)
        self._priority=tuple(sorted(priority))
        if desired==self._desired:return
        self._desired=desired
        # Keep ordering/conflict evidence through reconnects, bounded by the
        # current subscription. Engine-known quotes cover re-adds and restarts.
        self._history={s:q for s,q in self._history.items() if s in desired}
        self._conflicts={s:t for s,t in self._conflicts.items() if s in desired}
        self._generation+=1  # Reject in-flight REST/messages from the previous set immediately.
        self._invalidate('CHANGING' if desired else 'IDLE')
        self._changed.set()

    def _invalidate(self, state):
        self._acked=False; self._quotes.clear(); self._source.clear()
        self.state=state

    def latest(self, symbol, now=None):
        """A valid snapshot is data availability, NOT instrument/order eligibility."""
        q=self._quotes.get(symbol); now=utc(now or self.clock())
        if not self._acked or not q:return None
        if not q.timestamp<=q.received_at<=now:return None
        if any((now-t).total_seconds()>self.max_age for t in (q.timestamp,q.received_at)):return None
        return q

    def snapshot(self):
        now=self.clock()
        return {'state':self.state,'generation':self._generation,'desired_count':len(self._desired),
            'acknowledged':self._acked,'cached_count':len(self._quotes),
            'fresh_count':sum(self.latest(s,now) is not None for s in self._desired),
            'counts':dict(self.counts),'last_error_type':self.last_error,'retry_seconds':self.retry_seconds,
            'observed_at':utc(now).isoformat(),'live_orders_enabled':False}

    def _accept(self, quote, source, generation):
        if generation!=self._generation or not self._acked or quote.symbol not in self._desired:return
        now=utc(self.clock())
        if not quote.timestamp<=quote.received_at<=now:
            self.counts['invalid_quotes']+=1;return
        symbol=quote.symbol; old=self._history.get(symbol); conflict=self._conflicts.get(symbol)
        if conflict and quote.timestamp<=conflict:return
        if old and quote.timestamp<old.timestamp:
            self.counts['ignored_quotes']+=1;return
        if old and quote.timestamp==old.timestamp:
            if (quote.bid,quote.ask,quote.bid_size,quote.ask_size)!=(old.bid,old.ask,old.bid_size,old.ask_size):
                self._quotes.pop(symbol,None); self._source.pop(symbol,None)
                self._conflicts[symbol]=quote.timestamp; self.counts['invalid_quotes']+=1
            else:
                # A matching reconnect snapshot can revalidate the old value,
                # but must not renew its original receipt time or liquidity.
                self._quotes[symbol]=old; self._source[symbol]=source
                self.counts['ignored_quotes']+=1
            return
        # Keep the last disputed timestamp even after a newer valid frame. The
        # engine must first receive a post-conflict book before entries resume.
        self._history[symbol]=quote; self._quotes[symbol]=quote; self._source[symbol]=source; self.counts['quotes']+=1

    async def _seed(self, symbols, generation):
        # One worker shares the REST client's rate limiter. WebSocket receipt is
        # independent; a slow bootstrap never queues market messages behind REST.
        ordered=[s for s in self._priority if s in symbols]+[s for s in symbols if s not in self._priority]
        for symbol in ordered:
            if generation!=self._generation or not self._acked:return
            if self.latest(symbol) is not None:continue
            try:
                quote=await asyncio.wait_for(self.rest.quote(symbol),timeout=self.rest_timeout)
                self._accept(quote,'rest',generation)
            except Exception:
                # A single bootstrap failure doesn't tear down healthy streaming.
                # No raw provider exception or authentication material is logged.
                self.counts['rest_failures']+=1

    async def _receive(self, symbols, generation):
        seed=None
        def disconnected():
            if generation==self._generation:self._invalidate('DISCONNECTED')
        try:
            async with aclosing(self.stream.stream(list(symbols),trades=False,max_candidates=100,
                    on_disconnect=disconnected)) as frames:
                async for frame in frames:
                    if generation!=self._generation:return
                    self.counts['frames']+=1
                    if frame.get('type')=='subscriptions':
                        expected={f'orderbook:us:{s}' for s in symbols}
                        if frame.get('rejected') or set(frame.get('subscribed',[]))!=expected:
                            raise ValueError('Incomplete quote subscription')
                        if not self._acked:
                            self._acked=True; self.state='SUBSCRIBED'; self.last_error=None
                            seed=asyncio.create_task(self._seed(symbols,generation))
                    elif frame.get('type')=='message':
                        if not self._acked:raise ValueError('Quote before acknowledgement')
                        try:
                            quote=quote_from_frame(frame,received_at=utc(frame['received_at']))
                            if quote:self._accept(quote,'websocket',generation)
                        except (ValueError,TypeError,KeyError):
                            self.counts['invalid_quotes']+=1
            raise ConnectionError('Quote stream closed')
        finally:
            # Stop granting entries before waiting for REST/socket cleanup.
            # A slow cancellation must not extend a disconnected generation.
            if generation==self._generation:self._invalidate('DISCONNECTED')
            if seed:
                seed.cancel(); await asyncio.gather(seed,return_exceptions=True)

    async def run(self):
        if self._running or self._stop:raise RuntimeError('feed already running or closed')
        self._running=True; failures=0
        try:
            while not self._stop:
                self._changed.clear()
                if not self._desired:
                    self.state='IDLE'; await self._changed.wait(); continue
                self._generation+=1; self._invalidate('CONNECTING')
                self.counts['connections']+=1; self.retry_seconds=0.
                began=monotonic()
                receive=asyncio.create_task(self._receive(self._desired,self._generation))
                changed=asyncio.create_task(self._changed.wait())
                try:
                    done,_=await asyncio.wait([receive,changed],return_when=asyncio.FIRST_COMPLETED)
                    if changed in done:continue
                    try:receive.result()
                    except Exception as exc:self.last_error=type(exc).__name__
                finally:
                    receive.cancel(); changed.cancel()
                    await asyncio.gather(receive,changed,return_exceptions=True)
                    self._invalidate('DISCONNECTED')
                if self._stop:break
                failures=0 if monotonic()-began>=60 else failures
                self.retry_seconds=min(self.retry_max,self.retry_initial*2**min(failures,16))
                failures+=1; self.counts['reconnects']+=1; self.state='BACKOFF'
                try:await asyncio.wait_for(self._changed.wait(),timeout=self.retry_seconds)
                except TimeoutError:pass
        finally:
            self._invalidate('STOPPED'); self._running=False

    def stop(self):
        self._stop=True; self._generation+=1; self._invalidate('STOPPING'); self._changed.set()
