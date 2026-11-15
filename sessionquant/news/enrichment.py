"""허용된 발행사의 원문만 검증한다. 탐색 메타데이터는 사실·게시 시각의 증거가 아니다."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from hashlib import sha256
import asyncio
import ipaddress
import json
import re
import socket
import zlib
from urllib.parse import urlsplit,urljoin
from urllib.robotparser import RobotFileParser

import httpx

from ..models import utc
from ..macro.models import canonical_url,stable_hash
from .schedule import NewsSchedule

VERSION='article-enrichment-v1'
AGENT='SessionQuantNews/1.0'
PERMANENT={'FETCH_401','FETCH_403','FETCH_404','PAYWALL','ROBOTS_DISALLOWED','REDIRECT_INVALID',
    'URL_INVALID','SSRF_BLOCKED','WRONG_ARTICLE','UNSUPPORTED_CONTENT_TYPE','UNSUPPORTED_MEDIA','PUBLISHED_AT_UNKNOWN',
    'DATE_CONFLICT','LANGUAGE_UNKNOWN','CONTENT_TOO_SHORT','BOILERPLATE_ONLY','RIGHTS_UNKNOWN'}


class FetchBlocked(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def safe_url(url,allowed):
    try:
        p=urlsplit(url);host=(p.hostname or '').lower()
        if (p.scheme!='https' or not host or p.username or p.password or p.port not in {None,443}
                or host not in allowed or not re.fullmatch(r'[a-z0-9.-]+',host)):
            raise FetchBlocked('URL_INVALID')
        try:ipaddress.ip_address(host)
        except ValueError:pass
        else:raise FetchBlocked('SSRF_BLOCKED')
        if host=='localhost' or host.endswith(('.local','.internal','.localhost')):raise FetchBlocked('SSRF_BLOCKED')
        return p,host
    except (ValueError,TypeError):raise FetchBlocked('URL_INVALID') from None


async def public_addresses(host):
    values=await asyncio.to_thread(socket.getaddrinfo,host,443,type=socket.SOCK_STREAM)
    addresses=sorted({v[4][0] for v in values})
    if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):raise FetchBlocked('SSRF_BLOCKED')
    return addresses


class SafeArticleFetcher:
    """검증한 IP에 직접 연결하며 Host·TLS SNI는 원래 발행사로 검증한다."""
    def __init__(self,allowed,*,client=None,resolver=None,max_bytes=2_000_000,max_redirects=3):
        self.allowed=set(allowed);self.client=client or httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False)
        self.owns=client is None;self.resolve=resolver or public_addresses
        self.max_bytes=max_bytes;self.max_redirects=max_redirects
        self.limiters={}

    def pacing(self,host,seconds):
        from ..collectors.common import RateLimiter
        limiter=self.limiters.setdefault(host,RateLimiter(.5))
        limiter.interval=max(limiter.interval,seconds)

    async def get(self,url,*,headers=None):
        chain=[]
        for hop in range(self.max_redirects+1):
            _,host=safe_url(url,self.allowed)
            addresses=await asyncio.wait_for(self.resolve(host),timeout=5)
            if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):raise FetchBlocked('SSRF_BLOCKED')
            self.pacing(host,2)
            await self.limiters[host].wait()
            # 접속 시 DNS를 다시 조회하지 않는다. IPv6도 httpx가 대괄호 URL로 처리한다.
            pinned=httpx.URL(url).copy_with(host=addresses[0])
            request_headers={'Host':host,'User-Agent':AGENT,'Accept-Encoding':'identity',**(headers or {})}
            async with self.client.stream('GET',pinned,headers=request_headers,
                    extensions={'sni_hostname':host},follow_redirects=False) as response:
                chain.append(url)
                if response.status_code in {301,302,303,307,308}:
                    if hop>=self.max_redirects:raise FetchBlocked('REDIRECT_INVALID')
                    destination=urljoin(url,response.headers.get('Location',''))
                    _,new_host=safe_url(destination,self.allowed)
                    if new_host!=host:raise FetchBlocked('REDIRECT_INVALID')
                    url=destination;continue
                if response.status_code>=400:
                    response.raise_for_status()
                if response.status_code not in {200,304}:raise FetchBlocked('UNSUPPORTED_CONTENT')
                length=response.headers.get('Content-Length')
                if length and (not length.isdigit() or int(length)>self.max_bytes):raise FetchBlocked('RESPONSE_TOO_LARGE')
                encoding=response.headers.get('Content-Encoding','identity').lower()
                if encoding not in {'identity','gzip','deflate'}:raise FetchBlocked('UNSUPPORTED_CONTENT')
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS if encoding=='gzip' else zlib.MAX_WBITS) if encoding!='identity' else None
                wire=0;body=bytearray()
                # 일부 모의 전송은 이미 읽힌 응답을 제공한다. 실제 HTTP는 항상 raw 스트림이다.
                chunks=[response.content] if response.is_stream_consumed else None
                async def pieces():
                    if chunks is not None:
                        for chunk in chunks:yield chunk
                    else:
                        async for chunk in response.aiter_raw():yield chunk
                async for chunk in pieces():
                    wire+=len(chunk)
                    if wire>self.max_bytes:raise FetchBlocked('RESPONSE_TOO_LARGE')
                    body.extend(decoder.decompress(chunk,self.max_bytes+1-len(body)) if decoder else chunk)
                    if len(body)>self.max_bytes or (decoder and decoder.unconsumed_tail):raise FetchBlocked('RESPONSE_TOO_LARGE')
                if decoder and not decoder.eof:raise FetchBlocked('INVALID_ENCODING')
                return {'status':response.status_code,'headers':dict(response.headers),'body':bytes(body),
                    'final_url':url,'redirect_chain':chain,'pinned_ip':addresses[0]}
        raise FetchBlocked('REDIRECT_INVALID')

    async def aclose(self):
        if self.owns:await self.client.aclose()


def extract_article(document,url,title,language,*,min_chars=300,min_sentences=3):
    """본문 추출기는 날짜 추측·웹 조회를 수행하지 않는다."""
    from lxml import html
    import trafilatura
    tree=html.fromstring(document,parser=html.HTMLParser(no_network=True,recover=True))
    metas={str(n.get('property') or n.get('name') or '').lower():n.get('content','') for n in tree.xpath('//meta')}
    titles=tree.xpath('//h1//text()') or tree.xpath('//title//text()')
    actual=metas.get('og:title') or ' '.join(titles)
    words=lambda s:set(re.findall(r'\w+',s.lower()))
    expected=words(title);observed=words(actual)
    if not expected or len(expected&observed)/max(1,len(expected))<.6:raise FetchBlocked('WRONG_ARTICLE')
    canonical=tree.xpath('//link[@rel="canonical"]/@href')
    canonical=canonical_url(urljoin(url,canonical[0])) if canonical else canonical_url(url)
    if urlsplit(canonical).hostname!=urlsplit(url).hostname or urlsplit(canonical).path.rstrip('/')!=urlsplit(url).path.rstrip('/'):
        raise FetchBlocked('WRONG_ARTICLE')
    dates=[];modified=[];paywall=False
    for node in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:values=json.loads(node)
        except (ValueError,TypeError):continue
        def visit(value):
            nonlocal paywall
            if isinstance(value,list):
                for item in value:visit(item)
            elif isinstance(value,dict):
                kind=value.get('@type',[]);kind=[kind] if isinstance(kind,str) else kind
                if any(k in {'NewsArticle','Article','ReportageNewsArticle'} for k in kind):
                    if value.get('datePublished'):dates.append(('jsonld.datePublished',value['datePublished']))
                    if value.get('dateModified'):modified.append(value['dateModified'])
                    if value.get('isAccessibleForFree') in {False,'false'}:paywall=True
                if '@graph' in value:visit(value['@graph'])
        visit(values)
    if metas.get('article:published_time'):dates.append(('og.article:published_time',metas['article:published_time']))
    if paywall or re.search(r'(subscribe to (continue|read)|sign in to (continue|read)|captcha|enable javascript to)', ' '.join(tree.itertext())[:20000],re.I):
        raise FetchBlocked('PAYWALL')
    if not dates:raise FetchBlocked('PUBLISHED_AT_UNKNOWN')
    try:parsed=[(source,utc(value)) for source,value in dates]
    except (ValueError,TypeError,AttributeError):raise FetchBlocked('PUBLISHED_AT_UNKNOWN') from None
    if len({value for _,value in parsed})!=1:raise FetchBlocked('DATE_CONFLICT')
    html_language=(tree.get('lang') or metas.get('og:locale') or '').lower().split('-')[0].split('_')[0]
    expected_language={'English':'en','Korean':'ko'}.get(language,language.lower()[:2])
    if html_language!=expected_language:raise FetchBlocked('LANGUAGE_UNKNOWN')
    body=trafilatura.extract(document,url=url,favor_precision=True,include_comments=False,include_tables=False,
        include_links=False,deduplicate=True,no_fallback=True) or ''
    sentences=[s.strip() for s in re.split(r'[.!?。]\s*',body) if len(s.strip())>15]
    if len(body)<min_chars or len(sentences)<min_sentences:raise FetchBlocked('CONTENT_TOO_SHORT')
    if len(set(sentences))/max(1,len(sentences))<.7:raise FetchBlocked('BOILERPLATE_ONLY')
    return {'body':body,'published_at':parsed[0][1],'published_at_source':parsed[0][0],
        'published_at_confidence':'EXPLICIT_STRUCTURED_TIME','modified_at':modified[0] if modified else None,
        'canonical_url':canonical,'extracted_title':actual,'body_char_count':len(body),
        'body_word_count':len(body.split()),'body_sentence_count':len(sentences),'content_hash':sha256(body.encode()).hexdigest()}


class ArticleEnricher:
    def __init__(self,repo,*,policies=None,fetcher=None,clock=None,min_chars=300,min_sentences=3):
        self.repo=repo;self.policies=policies or {};self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.fetcher=fetcher or SafeArticleFetcher(self.policies)
        self.schedule=NewsSchedule(repo,clock=self.clock);self.semaphore=asyncio.Semaphore(2)
        self.min_chars,self.min_sentences=min_chars,min_sentences

    async def enrich(self,article):
        now=utc(self.clock());host=urlsplit(article.url).hostname or ''
        key='news_enrich_cache:'+stable_hash([canonical_url(article.url),self.policies.get(host),VERSION])
        cached=self.repo.get_control(key)
        baseline={'article_id':article.id,'discovery_source':article.discovery_source,'discovered_at':article.received_at.isoformat(),
            'source_url':article.url,'publisher_domain':host,'publisher_name':None,'publisher_verified':False,
            'received_at':article.received_at.isoformat(),'fetched_at':None,'published_at':None,
            'eligible_for_llm':False,'body':None,'body_char_count':0,'content_hash':None,'status':'DISCOVERED_ONLY',
            'provider_seen_at':article.provider_seen_at,'parser_version':VERSION,'primary_source_verified':False,'event_cluster_id':None,'attempt_count':0}
        policy=self.policies.get(host,{})
        if not (policy.get('allow_fetch') is True and policy.get('allow_llm') is True and policy.get('terms_url') and policy.get('publisher_name')):
            baseline.update(status='RIGHTS_UNKNOWN',block_reason='RIGHTS_UNKNOWN')
            return self._store(baseline,key),None
        domain='article:'+host
        if cached and cached['status'] in PERMANENT and now-utc(cached['checked_at'])<timedelta(days=1):return cached,None
        if not self.schedule.due(domain,2,now):return {**(cached or baseline),'status':'COOLDOWN','eligible_for_llm':False},None
        baseline['attempt_count']=(cached or {}).get('attempt_count',0)+1
        if cached and cached.get('consecutive_failures',0)>=3 and now-utc(cached['checked_at'])<timedelta(days=1):
            return self._store({**baseline,'status':'RETRY_EXHAUSTED'},key),None
        self.schedule.begin(domain,now)
        async with self.semaphore:
            try:
                result=await asyncio.wait_for(self._fetch(article,cached,baseline,policy),timeout=25)
                row,enriched=result
                self.schedule.success(domain,1,utc(self.clock()))
            except httpx.HTTPStatusError as exc:
                self.schedule.failure(domain,exc,now=utc(self.clock()))
                row={**baseline,'status':'FETCH_'+str(exc.response.status_code),'http_status':exc.response.status_code};enriched=None
            except FetchBlocked as exc:
                row={**baseline,'status':exc.code,'block_reason':exc.code};enriched=None
            except (httpx.HTTPError,ValueError,TypeError,KeyError,TimeoutError,OSError,zlib.error):
                self.schedule.failure(domain,code='fetch_or_extract_unavailable',now=utc(self.clock()))
                row={**baseline,'status':'FETCH_UNAVAILABLE'};enriched=None
            row['consecutive_failures']=0 if enriched or row['status']=='DUPLICATE' else (cached or {}).get('consecutive_failures',0)+1
            row['next_retry_at']=self.schedule.state.get(domain,{}).get('cooldown_until')
            return self._store(row,key),enriched

    async def _fetch(self,article,cached,baseline,policy):
        host=baseline['publisher_domain'];robots_key='news_robots:'+stable_hash(host)
        robots=self.repo.get_control(robots_key)
        if not robots or utc(self.clock())>=utc(robots['valid_until']):
            response=await self.fetcher.get('https://'+host+'/robots.txt')
            if response['status']!=200:raise FetchBlocked('ROBOTS_UNAVAILABLE')
            text=response['body'].decode('utf-8','strict')
            if len(text)>500_000:raise FetchBlocked('ROBOTS_UNAVAILABLE')
            robots={'text':text,'valid_until':(utc(self.clock())+timedelta(days=1)).isoformat()}
            self.repo.set_control(robots_key,robots)
        parser=RobotFileParser();parser.parse(robots['text'].splitlines())
        if not parser.can_fetch(AGENT,article.url):raise FetchBlocked('ROBOTS_DISALLOWED')
        delay=parser.crawl_delay(AGENT) or parser.crawl_delay('*') or 0
        rate=parser.request_rate(AGENT) or parser.request_rate('*')
        if rate:delay=max(delay,rate.seconds/rate.requests)
        if delay>20:raise FetchBlocked('ROBOTS_RATE_LIMITED')
        if hasattr(self.fetcher,'pacing'):self.fetcher.pacing(host,max(2,delay))
        headers={}
        if cached:
            for name,key in [('If-None-Match','etag'),('If-Modified-Since','last_modified')]:
                if cached.get(key):headers[name]=cached[key]
        response=await self.fetcher.get(article.url,headers=headers)
        if response['status']==304:
            if not cached:raise FetchBlocked('INVALID_CACHE')
            return {**cached,'status':'DUPLICATE','eligible_for_llm':False},None
        mime=response['headers'].get('content-type','').split(';')[0].lower()
        if mime not in {'text/html','application/xhtml+xml'}:raise FetchBlocked('UNSUPPORTED_CONTENT_TYPE')
        extracted=await asyncio.to_thread(extract_article,response['body'],response['final_url'],article.title,article.language,
            min_chars=self.min_chars,min_sentences=self.min_sentences)
        now=utc(self.clock())
        if not extracted['published_at']<=article.received_at<=now:raise FetchBlocked('DATE_CONFLICT')
        if now-extracted['published_at']>timedelta(hours=72):raise FetchBlocked('STALE')
        duplicate=cached and cached.get('content_hash')==extracted['content_hash']
        row={**baseline,**extracted,'published_at':extracted['published_at'].isoformat(),'fetched_at':now.isoformat(),
            'final_url':response['final_url'],'redirect_chain':response['redirect_chain'],'publisher_name':policy['publisher_name'],
            'publisher_verified':True,'terms_url':policy['terms_url'],'etag':response['headers'].get('etag'),
            'last_modified':response['headers'].get('last-modified'),'eligible_for_llm':not duplicate,
            'status':'DUPLICATE' if duplicate else 'ELIGIBLE_FOR_LLM',
            'stage_history':['DISCOVERED_ONLY','FETCH_PENDING','FETCHED','BODY_EXTRACTED','SOURCE_AND_TIME_VERIFIED']}
        enriched=replace(article,url=response['final_url'],body=extracted['body'],source=policy['publisher_name'],
            published_at=extracted['published_at'],publisher_verified=True,llm_use_allowed=True,document_version=extracted['content_hash'])
        row['body']=None
        row['body_retention']='HASH_AND_METADATA_ONLY'
        return row,None if duplicate else enriched

    def _store(self,row,key):
        row={**row,'checked_at':utc(self.clock()).isoformat(),'id':'enrichment:'+stable_hash([row['article_id'],row.get('content_hash'),row['status'],row.get('fetched_at')])}
        self.repo.record('news_enrichments',row);self.repo.set_control(key,row)
        return row

    async def aclose(self):await self.fetcher.aclose()
