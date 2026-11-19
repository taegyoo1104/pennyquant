"""Coalesce the feed into bounded engine batches; never rewrite source clocks."""
from ..models import jsonable, utc

VIEW = 'websocket_quotes_v1'


class LiveQuoteDelivery:
    def __init__(self, feed):
        self.feed = feed
        self._delivered = {}
        self.batches = self.quotes = 0

    def synchronize(self, watchlist, broker, now):
        risk = set(broker.positions) | set(broker.exit_intents)
        orders = {order.intent.symbol for order in broker.get_open_orders()}
        candidates = watchlist.light_symbols(now)
        self.feed.set_symbols(candidates, held=risk, open_orders=orders)
        desired = set(candidates) | risk | orders
        self._delivered = {s: t for s, t in self._delivered.items() if s in desired}
        # All 50 receive data; only focused and risk symbols enter the costly
        # strategy/DB path. A focus change can immediately use a recent snapshot.
        return sorted(set(watchlist.focus(now)) | risk | orders)

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
