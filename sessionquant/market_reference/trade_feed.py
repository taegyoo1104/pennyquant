"""Display-only ETF trades. One receiver, bounded one-second samples, no orders.

Toss AsyncAPI 1.2.3 trade:us is lossy. These are received price samples, not
complete trades, volume, candles or strategy inputs. A second connection keeps
display reconnects separate from the existing trading orderbook connection.
"""
import asyncio
from collections import deque
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic

from ..collectors.common import now_utc
from ..collectors.toss_websocket import TossWebSocket
from ..market.bars import session_bounds

VIEW = 'benchmark_trade_view_v1'
SYMBOLS = ('SPY', 'QQQ', 'SOXX')
MAX_POINTS = 3601
MAX_AGE = 30
GAP_SECONDS = 15


def timestamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return result.astimezone(timezone.utc)


def positive(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('invalid price')
    result = Decimal(str(value))
    if not result.is_finite() or not 0 < result < Decimal('1e12'):
        raise ValueError('invalid price')
    return result


def samples(rows, now):
    """Validate a bounded persisted display history; never renew source timestamps."""
    now = timestamp(now); bounds = session_bounds(now)
    result = []
    if not bounds or not isinstance(rows, list):
        return result
    for row in rows[-MAX_POINTS:]:
        try:
            millis, price, segment = row
            if type(millis) is not int or type(segment) is not int or segment < 0:
                continue
            at = datetime.fromtimestamp(millis/1000, timezone.utc)
            value = positive(price)
            if (not bounds[0] <= at <= min(bounds[1],now) or at < now-timedelta(hours=1)
                    or (result and millis <= result[-1][0])):
                continue
            result.append([millis,str(value),segment])
        except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
            continue
    return result


def price_sample(row, now):
    """A display price may be preopen; regular-session chart samples may not."""
    try:
        now = timestamp(now)
        millis, price, segment = row
        if type(millis) is not int or type(segment) is not int or segment < 0:
            return None
        at = datetime.fromtimestamp(millis/1000, timezone.utc)
        value = positive(price)
        bounds = session_bounds(now)
        if (not bounds or session_bounds(at) != bounds or not now-timedelta(hours=1) <= at <= now
                or at > bounds[1] or (at < bounds[0]) != (now < bounds[0])):
            return None
        return [millis, str(value), segment]
    except (ValueError, TypeError, OverflowError, OSError, InvalidOperation):
        return None


class BenchmarkTradeFeed:
    def __init__(self, rest, *, symbols=SYMBOLS, restored=None, stream=None, clock=now_utc,
                 retry_initial=1., retry_max=30.):
        if not symbols or not set(symbols) <= set(SYMBOLS) or len(set(symbols)) != len(symbols):
            raise ValueError('display ETF symbols only')
        if not 0 < retry_initial <= retry_max:
            raise ValueError('invalid retry interval')
        self.symbols = tuple(symbols)
        self.stream, self.clock = stream or TossWebSocket(rest), clock
        self.retry_initial, self.retry_max = retry_initial, retry_max
        self._stop = asyncio.Event(); self._running = False
        now = timestamp(clock()); saved = restored if isinstance(restored,dict) else {}
        try:
            if timestamp(saved['updated_at']) > now:
                saved = {}
        except (KeyError, TypeError, ValueError):
            saved = {}
        saved_points = saved.get('points') if isinstance(saved.get('points'),dict) else {}
        self.points = {s:deque(samples(saved_points.get(s,[]),now),maxlen=MAX_POINTS) for s in symbols}
        saved_prices = saved.get('price_samples') if isinstance(saved.get('price_samples'),dict) else {}
        self.price_samples = {s:row for s in symbols if (row := price_sample(saved_prices.get(s),now))}
        self.segment = max([r[2] for rows in self.points.values() for r in rows]
                           + [r[2] for r in self.price_samples.values()],default=0)+1
        self.latest = {}; self.state = 'STARTING'; self.acknowledged = False
        self.retry_seconds = 0.; self.last_error = None
        self.counts = {'connections':0,'reconnects':0,'received':0,'invalid':0,'ignored':0}

    def invalidate(self, state='DISCONNECTED'):
        self.state, self.acknowledged = state, False
        self.latest.clear()

    def accept(self, frame, generation):
        if self._stop.is_set() or not self.acknowledged or generation != self.segment:
            return
        try:
            topic = frame.get('topic')
            if topic not in {f'trade:us:{s}' for s in self.symbols}:
                return
            symbol = topic.split(':',2)[2]; data = frame['data']
            if data['currency'] != 'USD':
                raise ValueError('USD required')
            price = positive(data['price']); positive(data['volume'])
            at, received, now = timestamp(data['timestamp']), timestamp(frame['received_at']), timestamp(self.clock())
            bounds = session_bounds(now)
            if not at <= received <= now or (now-at).total_seconds() > MAX_AGE:
                raise ValueError('future or delayed trade')
            if (not bounds or session_bounds(at) != bounds or at > bounds[1]
                    or (at < bounds[0]) != (now < bounds[0])):
                self.counts['ignored'] += 1
                return
            millis = int(at.timestamp()*1000)
            rows = self.points[symbol]
            prior = self.latest.get(symbol)
            last = self.price_samples.get(symbol) or (rows[-1] if rows else None)
            # Across restart/reconnect the old source event cannot refresh the chart.
            if last and (millis < last[0] or (millis == last[0] and
                    (last[2] != self.segment or str(price) == last[1]))):
                self.counts['ignored'] += 1
                return
            row = [millis,str(price),self.segment]
            self.price_samples[symbol] = row
            if at >= bounds[0]:
                if rows and rows[-1][0]//1000 == millis//1000 and rows[-1][2] == self.segment:
                    rows[-1] = row
                else:
                    rows.append(row)
            while rows and rows[0][0] < int((now-timedelta(hours=1)).timestamp()*1000):
                rows.popleft()
            # Same-time trades can legitimately have different prices. Do not
            # pretend that a repeated event timestamp is a new market instant.
            receipt = prior['received_at'] if prior and prior['at'] == millis else received.isoformat()
            self.latest[symbol] = {'at':millis,'price':str(price),'received_at':receipt,'segment':self.segment}
            self.counts['received'] += 1
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
            self.counts['invalid'] += 1

    async def _receive(self):
        generation = self.segment
        async with aclosing(self.stream.stream(list(self.symbols),trades=True,orderbook=False,max_candidates=3,
                on_disconnect=self.invalidate)) as frames:
            async for frame in frames:
                if self._stop.is_set() or generation != self.segment:
                    return
                if frame.get('type') == 'subscriptions':
                    expected = {f'trade:us:{s}' for s in self.symbols}
                    if frame.get('rejected') or set(frame.get('subscribed',[])) != expected:
                        raise ValueError('ETF trade subscription incomplete')
                    self.acknowledged = True; self.state = 'CONNECTED'; self.last_error = None
                elif frame.get('type') == 'message':
                    if not self.acknowledged:
                        raise ValueError('trade before acknowledgement')
                    self.accept(frame,generation)
        raise ConnectionError('ETF trade stream closed')

    async def run(self):
        if self._running or self._stop.is_set():
            raise RuntimeError('receiver already running or closed')
        self._running = True; failures = 0
        try:
            while not self._stop.is_set():
                self.segment += 1; self.invalidate('CONNECTING'); self.retry_seconds = 0.
                self.counts['connections'] += 1; began = monotonic()
                receiver = asyncio.create_task(self._receive())
                stopped = asyncio.create_task(self._stop.wait())
                try:
                    done,_ = await asyncio.wait((receiver,stopped),return_when=asyncio.FIRST_COMPLETED)
                    if receiver in done:
                        try:receiver.result()
                        except Exception as exc:self.last_error = type(exc).__name__
                finally:
                    self.invalidate()
                    receiver.cancel(); stopped.cancel()
                    await asyncio.gather(receiver,stopped,return_exceptions=True)
                if self._stop.is_set():break
                failures = 0 if monotonic()-began >= 60 else failures
                self.retry_seconds = min(self.retry_max,self.retry_initial*2**min(failures,16))
                failures += 1; self.counts['reconnects'] += 1; self.state = 'BACKOFF'
                try:await asyncio.wait_for(self._stop.wait(),self.retry_seconds)
                except TimeoutError:pass
        finally:
            self.invalidate('STOPPED'); self._running = False

    def snapshot(self):
        now = timestamp(self.clock())
        return {'enabled':True,'purpose':'DISPLAY_ONLY','transport':'trade:us','updated_at':now.isoformat(),
                'active_symbols':list(self.symbols),'state':self.state,'acknowledged':self.acknowledged,
                'retry_seconds':self.retry_seconds,'last_error_type':self.last_error,'counts':dict(self.counts),
                'sample_seconds':1,'gap_seconds':GAP_SECONDS,
                'latest':{s:dict(r) for s,r in self.latest.items()},
                'price_samples':{s:row for s,r in self.price_samples.items() if (row := price_sample(r,now))},
                'points':{s:samples(list(rows),now) for s,rows in self.points.items()}}

    def stop(self):
        self.invalidate('STOPPING'); self._stop.set()
