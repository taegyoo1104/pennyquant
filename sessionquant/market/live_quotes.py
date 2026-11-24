"""Coalesce the feed into bounded engine batches; never rewrite source clocks."""
from ..models import jsonable, utc

VIEW = 'websocket_quotes_v1'


class LiveQuoteDelivery:
    def __init__(self, feed):
        self.feed = feed
        self._delivered = {}
        self._entry_generations = {}
        self._entry_evidence = {}
        self.batches = self.quotes = 0

    def synchronize(self, watchlist, broker, now):
        risk = set(broker.positions) | set(broker.exit_intents)
        orders = {order.intent.symbol for order in broker.get_open_orders()}
        candidates = watchlist.light_symbols(now)
        self.feed.set_symbols(candidates, held=risk, open_orders=orders)
        desired = set(candidates) | risk | orders
        self._delivered = {s: t for s, t in self._delivered.items() if s in desired}
        self._entry_generations = {s: g for s, g in self._entry_generations.items() if s in desired}
        self._entry_evidence = {s: v for s, v in self._entry_evidence.items() if s in desired}
        # All 50 receive data; only focused and risk symbols enter the costly
        # strategy/DB path. A focus change can immediately use a recent snapshot.
        return sorted(set(watchlist.focus(now)) | risk | orders)

    def entry_allowed(self, symbol, now, *, known_quotes=None):
        now = utc(now)
        quote=self.feed.latest(symbol,now)
        known=(known_quotes or {}).get(symbol)
        conflict=self.feed._conflicts.get(symbol)
        def blocked(reason):
            # In-memory only, one snapshot per current subscription. The engine
            # copies it to an order only when a new waiting interval begins.
            raw = self.feed._quotes.get(symbol)
            self._entry_evidence[symbol] = {
                'reason':reason, 'checked_at':now.isoformat(),
                'connection_state':self.feed.state, 'acknowledged':self.feed._acked,
                'generation':self.feed._generation, 'entry_generation':self._entry_generations.get(symbol),
                'quote_at':raw.timestamp.isoformat() if raw else None,
                'quote_received_at':raw.received_at.isoformat() if raw else None,
                'quote_age_seconds':round((now-raw.timestamp).total_seconds(),3) if raw else None,
                'max_age_seconds':self.feed.max_age,
                'engine_quote_at':known.timestamp.isoformat() if known else None,
                'engine_quote_received_at':known.received_at.isoformat() if known else None,
                'conflict_at':conflict.isoformat() if conflict else None,
            }
            return False
        if quote is None:
            raw=self.feed._quotes.get(symbol)
            if not self.feed._acked:reason='feed_not_ready'
            elif raw is None:
                last=self.feed._history.get(symbol)
                reason='quote_conflict' if conflict is not None and last and last.timestamp<=conflict else 'quote_missing'
            elif not raw.timestamp<=raw.received_at<=now:reason='quote_time_invalid'
            else:reason='quote_stale'
            result=blocked(reason)
            self._entry_generations.pop(symbol,None);return result
        reason = ('engine_quote_missing' if known is None else
                  'stream_behind_engine' if quote.timestamp<known.timestamp else
                  'post_conflict_not_delivered' if conflict is not None and known.timestamp<=conflict else None)
        if reason:
            result=blocked(reason)
            self._entry_generations.pop(symbol,None);return result
        if quote.timestamp>known.timestamp:
            # A reconnect must first deliver a book to the engine. Afterwards,
            # normal coalescing may leave its still-fresh book behind the feed.
            if self._entry_generations.get(symbol)!=self.feed._generation:
                return blocked('generation_not_delivered')
            self._entry_evidence.pop(symbol,None)
            return True
        # A restart/new subscription may not remember the previous book. The
        # engine does; a suppressed conflicting frame cannot unlock that book.
        if (quote.bid,quote.ask,quote.bid_size,quote.ask_size)!=(
                known.bid,known.ask,known.bid_size,known.ask_size):
            result=blocked('engine_quote_conflict')
            self._entry_generations.pop(symbol,None);return result
        self._entry_generations[symbol]=self.feed._generation
        self._entry_evidence.pop(symbol,None)
        return True

    def entry_evidence(self, symbol):
        """Evidence from the last guard result, without a new feed/API check."""
        return dict(self._entry_evidence.get(symbol, {}))

    def events(self, symbols, now, *, known_quotes=None):
        now = utc(now)
        events = []
        for symbol in sorted(set(symbols)):
            quote = self.feed.latest(symbol, now)
            if quote is None:
                continue
            previous = self._delivered.get(symbol)
            known = (known_quotes or {}).get(symbol)
            if ((previous is not None and quote.timestamp <= previous)
                    or (known is not None and quote.timestamp <= known.timestamp)):
                continue
            events.append({'kind': 'quote', 'data': jsonable(quote), 'delivered_at': now.isoformat()})
            self._delivered[symbol] = quote.timestamp
        if events:
            self.batches += 1
            self.quotes += len(events)
        return events
