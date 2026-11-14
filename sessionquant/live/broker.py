"""단 한 번 전송하는 소수점 주문과 브로커 누적 체결 원장.

서버의 10분 중복 방지 기간 이후에도 로컬 ID를 재사용하지 않는다.
접수 결과가 불명확하면 새로운 ID로도 재매수하지 않고 운영자를 기다린다.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import re

from ..collectors.common import CapabilityUnavailable, DataQualityError
from ..collectors.toss_market import BASE_URL
from ..models import symbol_valid, utc
from ..portfolio.models import decimal

TERMINAL={'FILLED','CANCELED','REJECTED'}
ACTIVE={'SUBMITTING','PENDING','PARTIAL_FILLED','PENDING_CANCEL'}

@dataclass(frozen=True)
class FractionalRequest:
    client_order_id: str
    symbol: str
    side: str
    amount: Decimal | None = None
    quantity: Decimal | None = None

    def __post_init__(self):
        symbol_valid(self.symbol)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,36}',self.client_order_id):
            raise ValueError('주문 ID는 영문·숫자·밑줄·하이픈 1~36자여야 합니다.')
        if self.side not in {'BUY','SELL'}: raise ValueError('주문 방향 오류')
        name='amount' if self.side=='BUY' else 'quantity'
        other='quantity' if name=='amount' else 'amount'
        if getattr(self,other) is not None or getattr(self,name) is None:
            raise ValueError('매수는 금액, 매도는 수량 하나만 지정합니다.')
        value=decimal(getattr(self,name),name,positive=True)
        if len(format(value,'f'))>30: raise ValueError('주문 숫자는 30자 이하여야 합니다.')
        step=Decimal('0.01') if name=='amount' else Decimal('0.000001')
        if value!=value.quantize(step): raise ValueError('금액은 센트, 수량은 소수점 6자리 이하여야 합니다.')
        object.__setattr__(self,name,value)

    def payload(self):
        return {'clientOrderId':self.client_order_id,'symbol':self.symbol,'side':self.side,
                'orderType':'MARKET','timeInForce':'DAY',
                **({'orderAmount':str(self.amount)} if self.amount is not None else {'quantity':str(self.quantity)})}

class FractionalLiveBroker:
    """실제 체결·수수료만 저장하며 시세로 체결을 생성하지 않는다."""
    def __init__(self,market,repository,*,armed=False,clock=None):
        if type(market.account_seq) is not int or market.account_seq<1: raise ValueError("유효한 계좌 선택이 필요합니다.")
        if type(armed) is not bool: raise ValueError("실주문 활성화 값은 불리언이어야 합니다.")
        self.market,self.repo,self.armed=market,repository,armed
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.min_entry_to_exit_minutes=30
        self.failed=False
        self.lock=asyncio.Lock()
        self.state=self.repo.get_control('fractional_live_v1') or {
            'version':1,'account_seq':market.account_seq,'orders':{},'baseline':None,'halt_reasons':[]}
        if self.state['account_seq']!=market.account_seq:
            raise ValueError('실거래 장부의 계좌가 현재 선택한 계좌와 다릅니다.')
        for item in self.state['orders'].values():
            if item['status']=='SUBMITTING': item['status']='UNKNOWN'
        self.reconciled=False
        self.observed_at=None
        self.positions={}
        self.position_currencies={}
        self.cash=Decimal(0)

    @property
    def orders(self): return self.state['orders']

    @contextmanager
    def _transaction(self):
        try:
            with self.repo.transaction(): yield
        except BaseException:
            self.failed=True; self.armed=False; self.reconciled=False
            raise

    def _save(self):
        if self.failed: raise RuntimeError('DB 저장 실패 후 재시작과 장부 확인이 필요합니다.')
        try:
            with self._transaction():
                self.repo.set_control('fractional_live_v1',self.state)
                self.repo.set_control('broker_state',{'mode':'live','environment':'LIVE_LOCAL',
                    'live_orders_enabled':False,'activation':'invocation_only','reconciled':self.reconciled,
                    'halt_reasons':self.state['halt_reasons'],'observed_at':self.observed_at,
                    'positions':{k:str(v) for k,v in self.positions.items()},'cash_buying_power':str(self.cash)})
                managed={}
                for symbol,qty in self.owned_quantities().items():
                    buys=[i for i in self.orders.values() if i['request']['symbol']==symbol and i['request']['side']=='BUY']
                    bought=sum((decimal(i.get('filled_quantity','0')) for i in buys),Decimal(0))
                    amount=sum((decimal(i.get('filled_amount','0')) for i in buys),Decimal(0))
                    managed[symbol]={'symbol':symbol,'qty':str(qty),'avg_price':str(amount/bought),
                        'execution_mode':'live','opened_at':buys[0]['created_at']}
                self.repo.replace_positions(managed)
                for cid,item in self.orders.items():
                    request=item['request']
                    self.repo.record('orders',{'client_id':cid,'symbol':request['symbol'],'side':request['side'].lower(),
                        'status':item['status'].lower(),'amount':request.get('orderAmount'),'qty':request.get('quantity'),
                        'filled_qty':item.get('filled_quantity','0'),'filled_notional':item.get('filled_amount','0'),
                        'fee':item.get('commission'),'tax':item.get('tax'),'broker_order_id':item.get('order_id'),
                        'created_at':item['created_at'],'execution_mode':'live'})
        except BaseException:
            self.failed=True
            self.armed=False
            raise

    def _block(self,reason):
        if reason not in self.state['halt_reasons']: self.state['halt_reasons'].append(reason)
        self.reconciled=False

    def owned_quantities(self):
        result={}
        for item in self.orders.values():
            if item.get('ownership')=='external': continue
            req=item['request']; sign=1 if req['side']=='BUY' else -1
            result[req['symbol']]=result.get(req['symbol'],Decimal(0))+sign*decimal(item.get('filled_quantity','0'),nonnegative=True)
        if any(v<0 for v in result.values()): raise DataQualityError('실거래 원장의 보유 수량이 음수입니다.')
        return {k:v for k,v in result.items() if v}

    async def get_order(self,cid):
        item=self.orders[cid]
        if not item.get('order_id'): raise CapabilityUnavailable('접수 ID 미확인: 자동 재전송하지 않습니다.')
        raw=await self.market._get('/api/v1/orders/'+item['order_id'],account=True)
        req=item['request']
        if (raw.get('orderId')!=item['order_id'] or raw.get('symbol')!=req['symbol'] or raw.get('side')!=req['side']
                or raw.get('currency')!='USD' or raw.get('orderType')!='MARKET' or raw.get('timeInForce')!='DAY'
                or raw.get('status') not in TERMINAL|ACTIVE-{'SUBMITTING'}):
            self._block('order_detail_mismatch'); self._save()
            raise DataQualityError('브로커 주문 상세와 저장된 의도가 다릅니다.')
        if req['side']=='BUY':
            if decimal(raw.get('orderAmount'),positive=True)!=decimal(req['orderAmount']):
                raise DataQualityError('금액 주문 상세 불일치')
        elif decimal(raw['quantity'],positive=True)!=decimal(req['quantity']):
            raise DataQualityError('수량 주문 상세 불일치')
        if item['status'] in TERMINAL and raw['status']!=item['status']:
            raise DataQualityError('종료 주문 상태가 진행 상태로 되돌아갔습니다.')
        execution=raw['execution']
        qty=decimal(execution['filledQuantity'],nonnegative=True)
        amount=decimal(execution.get('filledAmount') or '0',nonnegative=True)
        old_qty=decimal(item.get('filled_quantity','0'))
        old_amount=decimal(item.get('filled_amount','0'))
        if qty<old_qty or amount<old_amount or (qty==old_qty and amount!=old_amount):
            raise DataQualityError('누적 체결 수량·금액이 이전 기록과 모순됩니다.')
        if req['side']=='SELL' and qty>decimal(req['quantity']): raise DataQualityError('초과 매도 체결')
        if req['side']=='BUY' and amount>decimal(req['orderAmount']): raise DataQualityError('금액 주문 초과 체결')
        if qty:
            avg=decimal(execution['averageFilledPrice'],positive=True)
            if abs(avg*qty-amount)>Decimal('0.02'): raise DataQualityError('체결 가격·금액 불일치')
            filled_at=utc(execution['filledAt'])
            if filled_at>self.clock() or filled_at<utc(item['created_at']): raise DataQualityError('체결 시각 오류')
        elif amount: raise DataQualityError('수량 없는 체결 금액')
        if raw['status']=='FILLED' and (not qty or (req['side']=='SELL' and qty!=decimal(req['quantity']))):
            raise DataQualityError('전량 체결 상태와 실제 수량 불일치')
        commission=execution.get('commission'); tax=execution.get('tax')
        for value in (commission,tax):
            if value is not None: decimal(value,nonnegative=True)
        with self._transaction():
            item.update(status=raw['status'],filled_quantity=str(qty),filled_amount=str(amount),
                commission=commission,tax=tax,filled_at=execution.get('filledAt'))
            if qty:
                # 누적 확인을 upsert한다. 재조회·수수료 확정은 체결을 추가 생성하지 않는다.
                self.repo.record('executions',{'id':'toss:'+item['order_id'],'client_id':cid,
                    'symbol':req['symbol'],'side':req['side'].lower(),'qty':str(qty),'price':str(avg),
                    'amount':str(amount),'fee':commission,'tax':tax,'timestamp':execution['filledAt'],
                    'execution_mode':'live','aggregation':'broker_cumulative','fees_final':commission is not None and tax is not None})
            self._save()
        return dict(item)

    async def get_account(self):
        accounts=await self.market.accounts()
        if not any(x.get('accountSeq')==self.market.account_seq and x.get('accountType')=='BROKERAGE' for x in accounts):
            raise CapabilityUnavailable('선택한 종합매매 계좌를 확인하지 못했습니다.')
        return {'account_seq':self.market.account_seq,'account_verified':True}

    async def get_available_cash(self):
        value=await self.market._get('/api/v1/buying-power',params={'currency':'USD'},account=True)
        if value.get('currency')!='USD': raise DataQualityError('달러 매수 가능 금액 필요')
        self.cash=decimal(value['cashBuyingPower'],nonnegative=True)
        return self.cash

    async def get_positions(self):
        raw=await self.market.holdings()
        positions={}
        for row in raw['items']:
            symbol=row['symbol']; symbol_valid(symbol)
            if (row.get('marketCountry'),row.get('currency')) not in {('US','USD'),('KR','KRW')}:
                raise CapabilityUnavailable('보유 종목의 시장·통화를 확인하지 못했습니다.')
            self.position_currencies[symbol]=row['currency']
            if symbol in positions: raise DataQualityError('중복 보유 종목')
            positions[symbol]=decimal(row['quantity'],nonnegative=True)
        self.positions={s:q for s,q in positions.items() if q}
        return self.positions

    async def get_open_orders(self):
        result=await self.market.orders('OPEN')
        if result.get('hasNext') is not False: raise DataQualityError('미체결 목록의 완전성 미확인')
        return result['orders']

    async def reconcile(self):
        """주문 전송과 대사를 같은 잠금으로 직렬화한다."""
        async with self.lock:
            return await self._reconcile()

    async def _reconcile(self):
        """기존 외부 보유는 고정 기준으로 보존하며 전략 체결분만 더한다."""
        self.reconciled=False
        await self.get_account()
        for cid,item in self.orders.items():
            if item['status']=='UNKNOWN' and not item.get('order_id'):
                self._block('unknown_order'); self._save(); return False
            if item.get('order_id'): await self.get_order(cid)
            if item.get('cancel_requested') and not item.get('cancel_id') and item['status'] not in TERMINAL:
                self._block('unknown_cancel'); self._save(); return False
        positions=await self.get_positions()
        opened=await self.get_open_orders()
        known={i.get('order_id') for i in self.orders.values()}
        if any(o.get('orderId') not in known for o in opened):
            self._block('external_open_order')
        if self.state['baseline'] is None:
            if self.orders: raise DataQualityError('기준 잔고 없는 주문 이력')
            self.state['baseline']={s:str(q) for s,q in positions.items()}
        expected={s:decimal(q) for s,q in self.state['baseline'].items()}
        for item in self.orders.values():
            if item.get('ownership')=='external':
                symbol=item['request']['symbol']
                expected[symbol]=expected.get(symbol,Decimal(0))-decimal(item.get('filled_quantity','0'))
        for s,q in self.owned_quantities().items(): expected[s]=expected.get(s,Decimal(0))+q
        if {s:q for s,q in expected.items() if q}!=positions: self._block('holdings_mismatch')
        await self.get_available_cash()
        self.observed_at=self.clock().isoformat()
        self.reconciled=not self.state['halt_reasons']
        self._save()
        self.repo.record('reconciliations',{'id':self.observed_at,'timestamp':self.observed_at,
            'mode':'live','ok':self.reconciled,'issues':self.state['halt_reasons']})
        return self.reconciled

    def _require_write(self,*,for_exit=False):
        if not self.armed or self.failed: raise CapabilityUnavailable('실제 주문은 이번 실행의 명시적 활성화가 필요합니다.')
        if not self.reconciled or not self.observed_at or (self.clock()-utc(self.observed_at)).total_seconds()>30:
            raise CapabilityUnavailable('최신 계좌 대사가 필요합니다.')
        from ..market.bars import session_bounds
        bounds=session_bounds(self.clock())
        from datetime import timedelta
        if not bounds or not bounds[0]<=self.clock()<bounds[1]-timedelta(hours=1):
            raise CapabilityUnavailable('소수점 주문 접수 시간이 아닙니다.')
        if not for_exit and self.repo.get_control('manual_halt',False) is True:
            raise CapabilityUnavailable('사용자 거래 중단 상태입니다.')

    def _check_dashboard_buy_gate(self,symbol):
        gate=self.repo.get_control('trade_control_state')
        if not gate: return
        if not gate.get('running') or gate.get('draining'):
            raise CapabilityUnavailable('대시보드에서 신규 매수가 중단되어 있습니다.')
        for row in self.repo.recent('order_intents',10000):
            if (row.get('kind')=='dashboard_command' and row.get('status')=='pending'
                    and row.get('run_id')==gate['run_id'] and row.get('account_seq')==gate.get('account_seq')
                    and (row.get('action')=='stop_all' or (row.get('action')=='sell_symbol' and row.get('symbol')==symbol))):
                try: valid=0<=(self.clock()-utc(row['requested_at'])).total_seconds()<=60
                except (ValueError,KeyError,TypeError): valid=False
                if valid: raise CapabilityUnavailable('종료·매도 요청이 먼저 도착하여 신규 매수를 차단했습니다.')

    async def submit_order(self,request,*,buy_cap,eligibility_verified=False,external_position=False):
        async with self.lock:
            previous=self.orders.get(request.client_order_id)
            if previous:
                if previous['request']!=request.payload(): raise ValueError('주문 ID 재사용 충돌')
                return dict(previous)
            self._require_write(for_exit=request.side=='SELL')
            if request.side=='BUY':
                if eligibility_verified is not True: raise CapabilityUnavailable('계좌별 소수점 거래 자격 확인이 필요합니다.')
                from ..session_window import permits_new_entry
                if not permits_new_entry(self.clock(),min_entry_to_exit_minutes=self.min_entry_to_exit_minutes): raise CapabilityUnavailable('청산 전 신규 매수 금지 구간입니다.')
                self._check_dashboard_buy_gate(request.symbol)
                if any(i['request']['side']=='BUY' for i in self.orders.values()): raise CapabilityUnavailable('초기 검증의 추가 매수는 금지됩니다. 새 ID로 재시도하지 마세요.')
                if request.symbol in self.positions or len(self.positions)>=5:
                    raise CapabilityUnavailable('기존 보유 종목 또는 노출 한도')
                if request.amount>decimal(buy_cap,positive=True) or request.amount>self.cash:
                    raise CapabilityUnavailable('소액 검증 한도 또는 달러 현금 부족')
            else:
                if sum(i['request']['side']=='SELL' and i['request']['symbol']==request.symbol for i in self.orders.values())>=3:
                    raise CapabilityUnavailable('매도 시도 3회 한도: 토스 앱에서 잔량을 확인하세요.')
                if self.position_currencies.get(request.symbol)!='USD':
                    raise CapabilityUnavailable('현재 수동 시장가 매도는 미국 주식만 지원합니다.')
                owned=self.owned_quantities().get(request.symbol,Decimal(0))
                if external_position:
                    if owned: raise CapabilityUnavailable('전략 보유와 외부 보유를 혼합 매도할 수 없습니다.')
                    disposed=sum((decimal(i.get('filled_quantity','0')) for i in self.orders.values() if i.get('ownership')=='external' and i['request']['symbol']==request.symbol),Decimal(0))
                    owned=decimal((self.state['baseline'] or {}).get(request.symbol,'0'))-disposed
                reserved=sum((decimal(i['request']['quantity'])-decimal(i.get('filled_quantity','0'))
                    for i in self.orders.values() if i['request']['side']=='SELL' and i['request']['symbol']==request.symbol and i['status'] not in TERMINAL),Decimal(0))
                available=await self.market._get('/api/v1/sellable-quantity',params={'symbol':request.symbol},account=True)
                if request.quantity>min(owned-reserved,decimal(available['sellableQuantity'],nonnegative=True)):
                    raise CapabilityUnavailable('전략 보유 또는 실제 매도 가능 수량 초과')
            token=await self.market.access_token(); await self.market.limiter.wait()
            self._require_write(for_exit=request.side=='SELL')
            if request.side=='BUY':
                if not permits_new_entry(self.clock(),min_entry_to_exit_minutes=self.min_entry_to_exit_minutes): raise CapabilityUnavailable('신규 매수 마감 시각을 지났습니다.')
                self._check_dashboard_buy_gate(request.symbol)
            item={'request':request.payload(),'status':'SUBMITTING','order_id':None,
                'ownership':'external' if external_position else 'managed',
                'created_at':self.clock().isoformat(),'filled_quantity':'0','filled_amount':'0','commission':None,'tax':None}
            self.orders[request.client_order_id]=item
            self._save()
            self.reconciled=False
            if request.side=='BUY':
                try:
                    if not permits_new_entry(self.clock(),min_entry_to_exit_minutes=self.min_entry_to_exit_minutes):
                        raise CapabilityUnavailable('마감 시각 경과')
                    self._check_dashboard_buy_gate(request.symbol)
                except CapabilityUnavailable:
                    item.update(status='CANCELED',local_cancel_before_submit=True)
                    self._save(); return dict(item)
            try:
                response=await self.market.client.post(BASE_URL+'/api/v1/orders',json=request.payload(),
                    headers={'Authorization':'Bearer '+token,'X-Tossinvest-Account':str(self.market.account_seq)})
                # 불명확한 응답을 거절로 추측하지 않는다.
                if response.status_code in {400,401,403,404,422,429}:
                    item.update(status='REJECTED',http_status=response.status_code)
                else:
                    response.raise_for_status(); result=response.json()['result']
                    if (result.get('clientOrderId')!=request.client_order_id or not isinstance(result.get('orderId'),str)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',result['orderId'])): raise ValueError('접수 응답 오류')
                    item.update(status='PENDING',order_id=result['orderId'])
                self._save(); return dict(item)
            except BaseException:
                item['status']='UNKNOWN'; self._block('unknown_order')
                if not self.failed: self._save()
                raise

    async def cancel_order(self,cid):
        async with self.lock:
            item=self.orders[cid]
            if item['status'] in TERMINAL or item.get('cancel_requested'): return dict(item)
            self._require_write(for_exit=True)
            if not item.get('order_id'): raise CapabilityUnavailable('접수 ID 없는 주문은 자동 취소하지 않습니다.')
            token=await self.market.access_token(); await self.market.limiter.wait()
            self._require_write(for_exit=True)
            item['cancel_requested']=True; self._save(); self.reconciled=False
            try:
                response=await self.market.client.post(BASE_URL+'/api/v1/orders/'+item['order_id']+'/cancel',json={},
                    headers={'Authorization':'Bearer '+token,'X-Tossinvest-Account':str(self.market.account_seq)})
                response.raise_for_status(); cancel_id=response.json()['result']['orderId']
                if not isinstance(cancel_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',cancel_id): raise ValueError('취소 응답 오류')
                item.update(status='PENDING_CANCEL',cancel_id=cancel_id); self._save(); return dict(item)
            except BaseException:
                self._block('unknown_cancel')
                if not self.failed: self._save()
                raise
