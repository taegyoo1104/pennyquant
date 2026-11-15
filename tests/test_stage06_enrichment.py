"""발행사 원문 검증과 HTTP 안전 경계를 모의 전송·정적 HTML로 확인한다."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
import json

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.models import MacroArticle
from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.news.enrichment import SafeArticleFetcher,ArticleEnricher,FetchBlocked,extract_article

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)
TITLE='Federal Reserve announces an interest rate decision'
URL='https://publisher.test/news/rates'
POLICY={'publisher.test':{'publisher_name':'Fixture Publisher','terms_url':'https://publisher.test/terms',
    'allow_fetch':True,'allow_llm':True}}


def document(**changes):
    metadata={'@type':'NewsArticle','datePublished':'2026-11-13T15:00:00Z','dateModified':'2026-11-13T15:30:00Z',
        'isAccessibleForFree':True,**changes}
    paragraphs=[
        'The Federal Reserve announced an interest rate decision after the committee completed its meeting in Washington.',
        'The statement explained that employment conditions remain a consideration when evaluating future monetary policy.',
        'Officials reviewed economic activity and discussed the latest consumer price information before reaching a decision.',
        'Market participants are considering the implications for corporate financing and household borrowing over the coming months.',
        'The committee provided a written statement describing the factors that informed its assessment of the current economy.',
        'The next scheduled publication will provide additional information, while today’s release contains the confirmed policy decision.']
    return ('<html lang="en"><head><title>'+TITLE+'</title><link rel="canonical" href="'+URL+'">'
        '<script type="application/ld+json">'+json.dumps(metadata)+'</script></head><body><nav>Menu</nav><article><h1>'+TITLE+'</h1>'
        +''.join('<p>'+p+'</p>' for p in paragraphs)+'</article><footer>Copyright</footer></body></html>').encode()


@pytest.fixture
def repo(tmp_path):
    r=Repository(tmp_path/'enrich.db',backend='sqlite');yield r;r.close()


def article(**kwargs):return replace(MacroArticle(URL,TITLE,'','publisher.test',None,NOW,discovery_source='gdelt'),**kwargs)


def test_real_extractor_preserves_published_not_modified_and_drops_navigation():
    value=extract_article(document(),URL,TITLE,'English')
    assert value['published_at']==NOW-timedelta(hours=1)
    assert value['published_at_source']=='jsonld.datePublished'
    assert value['body_sentence_count']>=3 and 'Menu' not in value['body']
    assert 'Copyright' not in value['body'] and value['body_char_count']>300


@pytest.mark.parametrize('case,code',[
    ('no_date','PUBLISHED_AT_UNKNOWN'),('naive','PUBLISHED_AT_UNKNOWN'),('conflict','DATE_CONFLICT'),
    ('wrong_title','WRONG_ARTICLE'),('paywall','PAYWALL'),('wrong_language','LANGUAGE_UNKNOWN'),
    ('wrong_canonical','WRONG_ARTICLE'),('short','CONTENT_TOO_SHORT')])
def test_bad_article_is_excluded_before_any_llm(case,code):
    data=document()
    if case=='no_date':data=document(datePublished=None)
    if case=='naive':data=document(datePublished='2026-11-13T15:00:00')
    if case=='conflict':data=data.replace(b'</head>',b'<meta property="article:published_time" content="2026-11-13T14:00:00Z"></head>')
    if case=='wrong_title':data=data.replace(TITLE.encode(),b'Unrelated sports event')
    if case=='paywall':data=document(isAccessibleForFree=False)
    if case=='wrong_language':data=data.replace(b'lang="en"',b'lang="de"')
    if case=='wrong_canonical':data=data.replace(URL.encode(),b'https://publisher.test/')
    if case=='short':data=data.replace(b'<article>',b'<article><p>Only a short summary.</p><!--').replace(b'</article>',b'--></article>')
    with pytest.raises(FetchBlocked) as error:extract_article(data,URL,TITLE,'English')
    assert error.value.code==code


async def test_safe_transport_pins_public_ip_and_tls_host_for_each_hop():
    calls=[];dns=[]
    async def resolve(host):dns.append(host);return ['93.184.216.34']
    def handler(request):
        calls.append(request)
        if request.url.path=='/start':return httpx.Response(302,headers={'Location':'/end'})
        return httpx.Response(200,content=b'page',headers={'Content-Type':'text/html'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fetcher=SafeArticleFetcher({'publisher.test'},client=http,resolver=resolve)
        fetcher.pacing=lambda *args:fetcher.limiters.setdefault('publisher.test',type('Limiter',(),{'wait':immediate})())
        result=await fetcher.get('https://publisher.test/start')
    assert result['redirect_chain']==['https://publisher.test/start','https://publisher.test/end']
    assert len(dns)==2 and all(r.url.host=='93.184.216.34' for r in calls)
    assert all(r.headers['host']=='publisher.test' and r.extensions['sni_hostname']=='publisher.test' for r in calls)

async def immediate(*_):pass

@pytest.mark.parametrize('addresses', [['127.0.0.1'],['10.0.0.1'],['169.254.169.254'],['::1'],['::ffff:127.0.0.1'],['93.184.216.34','192.168.0.1']])
async def test_private_dns_never_reaches_transport(addresses):
    calls=[]
    async def resolve(_):return addresses
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        fetcher=SafeArticleFetcher({'publisher.test'},client=http,resolver=resolve)
        with pytest.raises(FetchBlocked):await fetcher.get(URL)
    assert calls==[]


async def test_dns_rebinding_on_redirect_is_checked_again():
    attempts=0;calls=[]
    async def resolve(_):
        nonlocal attempts
        attempts+=1;return ['93.184.216.34'] if attempts==1 else ['127.0.0.1']
    def handler(request):calls.append(request);return httpx.Response(302,headers={'Location':'/next'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fetcher=SafeArticleFetcher({'publisher.test'},client=http,resolver=resolve)
        fetcher.pacing=lambda *args:fetcher.limiters.setdefault('publisher.test',type('Limiter',(),{'wait':immediate})())
        with pytest.raises(FetchBlocked):await fetcher.get(URL)
    assert len(calls)==1 and attempts==2


@pytest.mark.parametrize('target',['http://publisher.test/a','https://127.0.0.1/a','https://publisher.test:444/a','https://user:pass@publisher.test/a','file:///etc/passwd','https://[::1]/'])
async def test_unsafe_redirect_never_gets_a_second_request(target):
    calls=[]
    async def resolve(_):return ['93.184.216.34']
    def handler(request):calls.append(request);return httpx.Response(302,headers={'Location':target})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fetcher=SafeArticleFetcher({'publisher.test'},client=http,resolver=resolve)
        fetcher.pacing=lambda *args:fetcher.limiters.setdefault('publisher.test',type('Limiter',(),{'wait':immediate})())
        with pytest.raises(FetchBlocked):await fetcher.get(URL)
    assert len(calls)==1


async def test_compressed_and_uncompressed_limits():
    import gzip
    async def resolve(_):return ['93.184.216.34']
    def handler(request):return httpx.Response(200,stream=httpx.ByteStream(gzip.compress(b'a'*100000)),headers={'Content-Encoding':'gzip'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fetcher=SafeArticleFetcher({'publisher.test'},client=http,resolver=resolve,max_bytes=1000)
        fetcher.pacing=lambda *args:fetcher.limiters.setdefault('publisher.test',type('Limiter',(),{'wait':immediate})())
        with pytest.raises(FetchBlocked) as error:await fetcher.get(URL)
    assert error.value.code=='RESPONSE_TOO_LARGE'


class FakeFetcher:
    def __init__(self,*,robots='User-agent: *\nAllow: /',mime='text/html',data=None):
        self.calls=[];self.robots=robots;self.mime=mime;self.data=data if data is not None else document()
    async def get(self,url,**kwargs):
        self.calls.append(url)
        return {'status':200,'body':self.robots.encode() if url.endswith('/robots.txt') else self.data,
            'headers':{'content-type':self.mime,'etag':'v1'},'final_url':url,'redirect_chain':[url]}
    async def aclose(self):pass


async def test_enrichment_no_rights_no_network_and_robots_failure(repo):
    fake=FakeFetcher()
    result,value=await ArticleEnricher(repo,fetcher=fake,clock=lambda:NOW).enrich(article())
    assert result['status']=='RIGHTS_UNKNOWN' and not fake.calls and value is None
    fake=FakeFetcher(robots='User-agent: *\nDisallow: /')
    result,value=await ArticleEnricher(repo,policies=POLICY,fetcher=fake,clock=lambda:NOW).enrich(article())
    assert result['status']=='ROBOTS_DISALLOWED' and len(fake.calls)==1 and value is None


async def test_enrichment_success_hash_only_restart_cache_and_exact_identity(repo):
    fake=FakeFetcher();clock=[NOW]
    enricher=ArticleEnricher(repo,policies=POLICY,fetcher=fake,clock=lambda:clock[0])
    result,value=await enricher.enrich(article())
    assert result['status']=='ELIGIBLE_FOR_LLM' and result['body'] is None and value.body
    assert value.id==article().id and value.published_at==NOW-timedelta(hours=1)
    assert not value.official and value.publisher_verified and value.llm_use_allowed
    clock[0]+=timedelta(minutes=16)
    restored=ArticleEnricher(repo,policies=POLICY,fetcher=fake,clock=lambda:clock[0])
    duplicate,value=await restored.enrich(article(topic='other'))
    assert duplicate['status']=='DUPLICATE' and value is None
    assert len(fake.calls)==3 and all(row['body'] is None for row in repo.all('news_enrichments'))


async def test_title_only_is_never_sent_to_paid_model(repo):
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        result=await analyzer.analyze_macro(article(),now=NOW)
        assert result['error']=='published_at_unknown' and calls==[] and repo.all('llm_usage')==[]
        await analyzer.aclose()
