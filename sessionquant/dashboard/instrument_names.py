"""Korean card labels; provider names first, local aliases for older snapshots."""
import re

KOREAN_NAMES = {
    'AAPL':'애플', 'MSFT':'마이크로소프트', 'NVDA':'엔비디아', 'AMZN':'아마존',
    'GOOGL':'알파벳 A', 'GOOG':'알파벳 C', 'META':'메타', 'TSLA':'테슬라',
    'AVGO':'브로드컴', 'AMD':'에이엠디', 'MU':'마이크론', 'INTC':'인텔',
    'BRK.B':'버크셔 해서웨이 B', 'BRK-B':'버크셔 해서웨이 B', 'JPM':'JP모건 체이스',
    'LLY':'일라이 릴리', 'V':'비자', 'MA':'마스터카드', 'WMT':'월마트',
    'ORCL':'오라클', 'COST':'코스트코', 'NFLX':'넷플릭스', 'PLTR':'팔란티어',
    'UNH':'유나이티드헬스', 'JNJ':'존슨앤드존슨', 'XOM':'엑슨모빌',
    'BAC':'뱅크오브아메리카', 'HD':'홈디포', 'PG':'프록터앤드갬블',
    'ABBV':'애브비', 'CVX':'셰브론', 'KO':'코카콜라', 'GE':'GE 에어로스페이스',
    'CSCO':'시스코', 'CRM':'세일즈포스', 'WFC':'웰스파고', 'IBM':'아이비엠',
    'PM':'필립모리스', 'ABT':'애보트', 'GS':'골드만삭스', 'MCD':'맥도날드',
    'CAT':'캐터필러', 'AXP':'아메리칸 익스프레스', 'MRK':'머크',
    'RTX':'알티엑스', 'TMO':'써모 피셔', 'PEP':'펩시코', 'DIS':'월트 디즈니',
    'ISRG':'인튜이티브 서지컬', 'ADBE':'어도비', 'QCOM':'퀄컴',
    'TXN':'텍사스 인스트루먼트', 'AMAT':'어플라이드 머티어리얼즈',
    'LRCX':'램리서치', 'KLAC':'케이엘에이', 'INTU':'인튜이트', 'NOW':'서비스나우',
    'UBER':'우버', 'BKNG':'부킹홀딩스', 'LIN':'린데', 'GEV':'GE 버노바',
    'ANET':'아리스타 네트웍스', 'MS':'모건스탠리', 'C':'씨티그룹',
    'T':'에이티앤티', 'VZ':'버라이즌', 'UNP':'유니언 퍼시픽', 'SCHW':'찰스 슈왑',
    'NEE':'넥스트에라 에너지', 'DHR':'다나허', 'AMGN':'암젠',
}


def korean_name(symbol, row):
    for key in ('name_ko', 'name'):
        name = row.get(key)
        if isinstance(name,str) and re.search('[가-힣]',name):
            return ' '.join(name.split())[:100]
    return KOREAN_NAMES.get(symbol,'')
