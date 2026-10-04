"""
collect_active_etf.py
─────────────────────
액티브 ETF 구성종목(PDF) 일별 → active_etf_holdings 저장 (화면: 프론트 js/active-etf.js '액티브 ETF' 페이지)

[원본] 태린이아빠 「액티브ETF를 관찰하자」(2026-09-18 자료)
  DataGuide 'ETP Components per CU'로 ETF 30개의 CU당 구성종목(주식수·금액·금액기준 구성비중)을
  현재(CPD)와 과거 두 날짜로 받아, 3개씩 묶은 요약 시트 10개에서 종목마다
  현재 비중 · 과거 비중 · 비중차이(현재−과거) · 증가율(현재÷과거−1, 과거에 없으면 '신규편입')을 본다.
  (비교 계산은 화면이 한다 — 여기서는 날짜별 원자료만 쌓는다)

[출처] KRX 정보데이터시스템 'ETF 포트폴리오 구성(PDF)'(MDCSTAT05001) — 날짜 지정, 전 종목.
  로그인 필요(서버 .env KRX_ID·KRX_PW, 사용자가 직접 넣음). COMPST_RTO(구성비중)가 원본 엑셀 값과 같다
  (2026-10-04 실측: MIDAS 중소형액티브·PLUS 코리아HBM반도체 09-23·09-28 대조 일치).
  ⚠ 같은 아이디로 다른 곳(브라우저)에 로그인돼 있으면 '중복 로그인' → 기존 접속을 끊고 들어간다(skipDup).
  KIS ETF 구성종목 API는 상위 30종목만·현재만 줘서 쓰지 않는다.

실행:
  python3 collect_active_etf.py                # 최근 10일(거래일) 중 빠진 날 채우기 (일일 잡)
  python3 collect_active_etf.py --backfill 95  # 최근 95일(달력일) 백필
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)

BASE = 'https://data.krx.co.kr'
HDR = {'User-Agent': 'Mozilla/5.0', 'X-Requested-With': 'XMLHttpRequest',
       'Referer': BASE + '/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201030108'}
PAUSE = 0.3            # KRX 요청 사이 쉬는 시간(초)

# 원본 요약 시트 10개(탭) — 시트 이름과 ETF 순서 그대로
GROUPS = [
    ('반도체 및 중소형 액티브', ['0239Z0', '0239Y0', '438740']),
    ('코스닥액티브',            ['0163Y0', '0162Y0', '0166N0']),
    ('반도체',                  ['494220', '474590', '388420']),
    ('코스닥액티브2, 배당성장', ['0204S0', '0220B0', '441800']),
    ('신재생, 2차전지',         ['385510', '404120', '422420']),
    ('이노, 소비, AI인프라',    ['385710', '0208N0', '487130']),
    ('코스피, 조선, 테크',      ['385720', '445150', '471780']),
    ('수출, 로봇, 컬쳐',        ['0074K0', '445290', '410870']),
    ('밸류업, 제조업, 바이오',  ['495060', '0166S0', '0168K0']),
    ('바이오',                  ['463050', '462900', '0000Z0']),
]
# 거래일 = fear_greed_daily(KIS 지수, 18:22 수집, 휴장일 제외)의 날짜. KRX PDF는 쉬는 날에도 직전 구성을
# 그대로 돌려줘서(10-03 개천절에 176행) 응답만으로는 휴장일을 못 가린다.
CAL_TABLE = 'fear_greed_daily'


def _kst_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


class Krx:
    """KRX 정보데이터시스템 로그인 세션."""

    def __init__(self):
        self.s = requests.Session()

    def login(self):
        uid, pw = os.environ.get('KRX_ID'), os.environ.get('KRX_PW')
        if not uid or not pw:
            raise RuntimeError('.env에 KRX_ID·KRX_PW가 없습니다')
        self.s.get(BASE + '/contents/MDC/MDI/mdiLoader/index.cmd', headers=HDR, timeout=15)
        self.s.get(BASE + '/contents/MDC/COMS/client/MDCCOMS001.cmd', headers=HDR, timeout=15)
        form = {'mbrNm': '', 'telNo': '', 'di': '', 'certType': '', 'mbrId': uid, 'pw': pw}
        url = BASE + '/contents/MDC/COMS/client/MDCCOMS001D1.cmd'
        d = self.s.post(url, data=form, headers=HDR, timeout=15).json()
        if d.get('_error_code') == 'CD011':          # 중복 로그인 → 기존 접속 끊고 들어감
            d = self.s.post(url, data={**form, 'skipDup': 'Y'}, headers=HDR, timeout=15).json()
        if d.get('_error_code') != 'CD001':
            raise RuntimeError(f"KRX 로그인 실패: {d.get('_error_code')} {d.get('_error_message')}")

    def get(self, bld: str, **kw) -> dict:
        for attempt in range(2):
            r = self.s.post(BASE + '/comm/bldAttendant/getJsonData.cmd', headers=HDR, timeout=30,
                            data={'bld': bld, 'locale': 'ko_KR', **kw})
            if r.status_code == 400 and 'LOGOUT' in r.text and attempt == 0:   # 세션 만료 → 다시 로그인
                self.login()
                continue
            r.raise_for_status()
            return r.json()
        return {}

    def etf_list(self) -> dict:
        """단축코드 → {isin, name} (ETF 전종목 기본정보 MDCSTAT04601)."""
        rows = self.get('dbms/MDC/STAT/standard/MDCSTAT04601', share='1', csvxls_isNo='false').get('output') or []
        return {r['ISU_SRT_CD'].upper(): {'isin': r['ISU_CD'], 'name': r['ISU_ABBRV']} for r in rows}

    def pdf(self, isin: str, day: date) -> list:
        """그날 CU당 구성종목 — KRX 순서(금액 큰 순) 그대로."""
        rows = self.get('dbms/MDC/STAT/standard/MDCSTAT05001', isuCd=isin, trdDd=day.strftime('%Y%m%d'),
                        share='1', money='1', csvxls_isNo='false').get('output') or []
        time.sleep(PAUSE)
        return rows


def _num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return None   # '-' (현금 등)


def _rows(day: date, code: str, pdf: list) -> list:
    out, seen = [], set()
    for i, r in enumerate(pdf):
        item = (r.get('COMPST_ISU_CD') or r.get('COMPST_ISU_CD2') or r.get('COMPST_ISU_NM') or '').strip()
        if not item or item in seen:
            continue
        seen.add(item)
        amt = _num(r.get('COMPST_AMT'))
        out.append({'base_date': day.isoformat(), 'etf_code': code, 'item_code': item,
                    'item_name': r.get('COMPST_ISU_NM'), 'mkt': r.get('MKT_ID') or None,
                    'shares': _num(r.get('COMPST_ISU_CU1_SHRS')),
                    'amount': int(amt) if amt is not None else None,
                    'weight': _num(r.get('COMPST_RTO')), 'seq': i + 1})
    return out


def _has_schema(sb) -> bool:
    try:
        sb.table('active_etf_holdings').select('base_date').limit(1).execute()
        sb.table('active_etfs').select('code').limit(1).execute()
        return True
    except Exception:
        return False


def run(days: int = 10) -> dict:
    """최근 days일(달력일) 거래일 중 저장 안 된 (날, ETF)를 채운다. 반환 {'dates': [...], 'rows': n}."""
    sb = get_supabase_client()
    if not _has_schema(sb):
        log.warning('[액티브ETF] sql/active_etf.sql 미실행 — 건너뜀')
        return {}
    krx = Krx()
    krx.login()
    meta = krx.etf_list()
    codes = [c for _, cs in GROUPS for c in cs]
    missing = [c for c in codes if c not in meta]
    if missing:
        log.warning(f'[액티브ETF] KRX 목록에 없는 ETF {missing} — 상장폐지·코드 변경 확인 필요')
    sb.table('active_etfs').upsert([
        {'code': c, 'isin': meta.get(c, {}).get('isin'), 'name': meta.get(c, {}).get('name') or c,
         'grp': g, 'grp_ord': gi, 'ord': oi}
        for gi, (g, cs) in enumerate(GROUPS) for oi, c in enumerate(cs)], on_conflict='code').execute()

    today = _kst_today()
    start = today - timedelta(days=days)
    have = {}
    for r in fetch_all_pages(sb.table('active_etf_holdings').select('base_date,etf_code')
                             .gte('base_date', start.isoformat()).eq('seq', 1)
                             .order('base_date').order('etf_code')):
        have.setdefault(r['base_date'], set()).add(r['etf_code'])

    cal = [r['base_date'] for r in fetch_all_pages(sb.table(CAL_TABLE).select('base_date')
                                                   .gte('base_date', start.isoformat())
                                                   .lte('base_date', today.isoformat()).order('base_date'))]
    done, total = [], 0
    for iso in reversed(cal):
        d = date.fromisoformat(iso)
        todo = [c for c in codes if c in meta and c not in have.get(iso, set())]
        n = 0
        for c in todo:
            rows = _rows(d, c, krx.pdf(meta[c]['isin'], d))
            if rows:   # 그날 아직 상장 전이면 빈 응답
                sb.table('active_etf_holdings').upsert(rows, on_conflict='base_date,etf_code,item_code').execute()
                n += len(rows)
        if n:
            done.append(iso)
            total += n
            log.info(f'[액티브ETF] {iso} {len(todo)}개 ETF · {n}행')
    log.info(f'[액티브ETF] 완료 — 새로 채운 날 {len(done)}일 · {total}행')
    return {'dates': sorted(done), 'rows': total}


if __name__ == '__main__':
    if '--backfill' in sys.argv:
        run(int(sys.argv[sys.argv.index('--backfill') + 1]))
    else:
        run()
