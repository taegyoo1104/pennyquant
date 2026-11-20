"""Coalesce the feed into bounded engine batches; never rewrite source clocks."""
from ..models import jsonable, utc

VIEW = 'websocket_quotes_v1'


class LiveQuoteDelivery:
    def __init__(self, feed):
        self.feed = feed
        self._delivered = {}
        self._entry_generations = {}
        self.batches = self.quotes = 0

    def synchronize(self, watchlist, broker, now):
        risk = set(broker.positions) | set(broker.exit_intents)
        orders = {order.intent.symbol for order in broker.get_open_orders()}
        candidates = watchlist.light_symbols(now)
        self.feed.set_symbols(candidates, held=risk, open_orders=orders)
        desired = set(candidates) | risk | orders
        self._delivered = {s: t for s, t in self._delivered.items() if s in desired}
        self._entry_generations = {s: g for s, g in self._entry_generations.items() if s in desired}
        # All 50 receive data; only focused and risk symbols enter the costly
        # strategy/DB path. A focus change can immediately use a recent snapshot.
        return sorted(set(watchlist.focus(now)) | risk | orders)

    def entry_allowed(self, symbol, now, *, known_quotes=None):
        quote=self.feed.latest(symbol,now)
        if quote is None:
            self._entry_generations.pop(symbol,None);return False
        known=(known_quotes or {}).get(symbol)
        conflict=self.feed._conflicts.get(symbol)
        if (known is None or quote.timestamp<known.timestamp
                or (conflict is not None and known.timestamp<=conflict)):
            self._entry_generations.pop(symbol,None);return False
        if quote.timestamp>known.timestamp:
            # A reconnect must first deliver a book to the engine. Afterwards,
            # normal coalescing may leave its still-fresh book behind the feed.
            return self._entry_generations.get(symbol)==self.feed._generation
        # A restart/new subscription may not remember the previous book. The
        # engine does; a suppressed conflicting frame cannot unlock that book.
        if (quote.bid,quote.ask,quote.bid_size,quote.ask_size)!=(
                known.bid,known.ask,known.bid_size,known.ask_size):
            self._entry_generations.pop(symbol,None);return False
        self._entry_generations[symbol]=self.feed._generation
        return True

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
