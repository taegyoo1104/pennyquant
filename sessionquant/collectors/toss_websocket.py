"""Documented Toss full-replace subscriptions; caller owns reconnect reconciliation."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from .common import CapabilityUnavailable, now_utc
from .toss_market import TossMarketClient, symbols_parameter, parse_orderbook

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"


def subscription_declaration(symbols: list[str], *, trades: bool = True, orderbook: bool = True,
                             account_seq: int | None = None, max_candidates: int = 20) -> list[dict]:
    symbols = list(dict.fromkeys(symbols))
    if symbols:
        symbols_parameter(symbols)
    if len(symbols) > max_candidates:
        raise ValueError("candidate limit exceeded")
    declarations = []
    if trades and symbols:
        declarations.append({"type": "trade:us", "codes": symbols})
    if orderbook and symbols:
        declarations.append({"type": "orderbook:us", "codes": symbols})
    if account_seq is not None:
        declarations.append({"type": "personal:order", "codes": [str(account_seq)]})
    if sum(len(item["codes"]) for item in declarations) > 100:
        raise ValueError("connection subscription limit exceeded (100 channel/code pairs)")
    return declarations


def quote_from_frame(frame: dict, *, received_at=None, exchange="UNKNOWN", tradable=False):
    if frame.get("type") != "message" or not frame.get("topic", "").startswith("orderbook:us:"):
        return None
    return parse_orderbook(frame["topic"].split(":", 2)[2], frame["data"], received_at or now_utc(),
                           exchange=exchange, tradable=tradable)


class TossWebSocket:
    def __init__(self, rest: TossMarketClient):
        self.rest = rest

    async def stream(self, symbols: list[str], *, include_orders: bool = False,
                     trades: bool = True, orderbook: bool = True, max_candidates: int = 20,
                     ack_timeout: float = 15, idle_timeout: float = 90, on_disconnect=None) -> AsyncIterator[dict]:
        """One connection. Fail closed on errors/partial subscriptions; no silent reconnect.

        Consumer must halt entries on exception, REST-reconcile orders/holdings,
        then open a new stream and receive a successful ack before resuming.
        """
        from websockets.asyncio.client import connect
        notified=False
        def disconnected():
            nonlocal notified
            if not notified:
                notified=True
                if on_disconnect is not None:on_disconnect()
        declarations = subscription_declaration(symbols, trades=trades, orderbook=orderbook, max_candidates=max_candidates,
            account_seq=self.rest.account_seq if include_orders else None)
        token = await self.rest.access_token()
        async with connect(WS_URL, additional_headers={"Authorization": f"Bearer {token}"},
                           ping_interval=None, max_size=2**20, open_timeout=15) as ws:
            async def keepalive():
                while True:
                    await asyncio.sleep(60)
                    await ws.send("PING")
            heartbeat = asyncio.create_task(keepalive())
            try:
                await ws.send(json.dumps(declarations))
                acknowledged = False
                iterator = aiter(ws)
                ack_deadline = asyncio.get_running_loop().time()+ack_timeout
                while True:
                    timeout = idle_timeout if acknowledged else max(0,ack_deadline-asyncio.get_running_loop().time())
                    receive = asyncio.create_task(anext(iterator))
                    try:
                        done,_ = await asyncio.wait([receive,heartbeat],timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
                        if heartbeat in done:
                            heartbeat.result()
                            raise CapabilityUnavailable('Toss heartbeat stopped')
                        if receive not in done:raise TimeoutError('Toss acknowledgement or connection idle timeout')
                        try:raw = receive.result()
                        except StopAsyncIteration:
                            disconnected();break
                    except BaseException:
                        disconnected();raise
                    finally:
                        receive.cancel()
                        await asyncio.gather(receive,return_exceptions=True)
                    frame = json.loads(raw)
                    if not isinstance(frame,dict):raise CapabilityUnavailable('Invalid Toss frame')
                    frame_type = frame.get("type")
                    if frame_type == "error":
                        raise CapabilityUnavailable("Toss WebSocket error: " + frame.get("error", {}).get("code", "unknown"))
                    if frame_type == "subscriptions":
                        if frame.get("rejected"):
                            raise CapabilityUnavailable("Toss rejected one or more subscriptions; correct declarations before reconnect")
                        expected = {f'{item["type"]}:{code}' for item in declarations for code in item["codes"]}
                        if not expected.issubset(set(frame.get("subscribed", []))):
                            raise CapabilityUnavailable("Toss subscription acknowledgement incomplete")
                        acknowledged = True
                    if frame_type == "message" and not acknowledged:
                        raise CapabilityUnavailable("Received market data before subscription acknowledgement")
                    yield {**frame, "received_at": now_utc().isoformat()}
                raise CapabilityUnavailable("Toss stream closed; REST reconciliation required")
            finally:
                # Revoke consumers before awaited heartbeat/socket cleanup. A
                # slow close handshake must not extend entry permission.
                try:disconnected()
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat,return_exceptions=True)
