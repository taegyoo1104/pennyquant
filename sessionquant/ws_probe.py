"""Read-only, opt-in stage-1 probe. Never starts the engine or writes a DB."""
import argparse
import asyncio
import json
from time import monotonic

from .collectors.toss_market import TossMarketClient
from .collectors.toss_quote_feed import TossQuoteFeed
from .local_env import load_local_env


async def probe(symbols, seconds):
    rest=TossMarketClient(); feed=TossQuoteFeed(rest)
    try:
        feed.set_symbols(symbols)
        task=asyncio.create_task(feed.run())
        try:
            end=monotonic()+seconds
            while monotonic()<end:
                await asyncio.sleep(min(5,end-monotonic()))
                if task.done():task.result()
                print(json.dumps(feed.snapshot(),ensure_ascii=False),flush=True)
        finally:
            feed.stop(); await task
    finally:await rest.aclose()


def main():
    parser=argparse.ArgumentParser(description='호가 웹소켓 수신 점검 · 주문/DB 저장 없음')
    parser.add_argument('--symbols',nargs='+',required=True)
    parser.add_argument('--seconds',type=int,default=60)
    args=parser.parse_args()
    if not 1<=args.seconds<=3600:parser.error('seconds must be 1..3600')
    load_local_env()
    try:asyncio.run(probe(args.symbols,args.seconds))
    except KeyboardInterrupt:pass


if __name__=='__main__':main()
