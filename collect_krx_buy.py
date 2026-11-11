"""KRX 당일 투자자별 매수대금 — market_data 매수대금 4컬럼을 당일분까지 채운다

[왜] 화면 '일별 수급'(js/flow-rank.js)과 유동성 공급 컨셉(collect_flow_empty.concept_rank)은
  market_data의 pef/trust/pension/foreign_buy_amt(백만원)를 읽는다. 이 컬럼은 KIS FHPTJ04160001로
  채우는데 그 API는 '오늘 이전'만 줘서(collect_flow_empty.sync_buy) 당일 수급이 다음 날 18:50에야 들어왔다.

[출처] KRX 정보데이터시스템 '투자자별 순매수상위종목'(MDCSTAT02401) — 날짜·투자자 지정, 전 종목,
  BID_TRDVAL = 거래대금_매수(원). 태린이아빠 대시보드 '한국 일별 수급 동향 점검'이 올리는 KRX 파일
  (사모·투신·연기금·외국인 시트의 '거래대금_매수' 열)과 같은 통계다. 로그인 필요(collect_active_etf.Krx).

[KIS와 같은 값 — 2026-11-11에 11-10 전 종목 대조]
  사모 3100 · 투신 3000 · 연기금 6000 : round(원 / 1e6) = KIS 값, 767 · 781 · 486종목 전부 일치.
  외국인 : KRX 외국인 9000 + 기타외국인 9001 = KIS 외국인, 2,560 / 2,560 일치
           (9000만 쓰면 1,761 / 2,455 — 삼성카드 870 vs 2,123처럼 기타외국인이 큰 종목이 어긋난다).
  목록에 없는 종목 = 그 투자자가 그날 사지 않은 종목 → 0 (KIS도 0, 어긋난 종목 0).
  ⇒ 같은 컬럼에 같은 정의로 적는다. 원본 화면의 '외국인'은 9000만이라 종목에 따라 조금 다르다
    (11-11 금액 50위는 구성 같음, 시총대비 50위는 49 / 50).

[시각] 장 마감 직후부터 잠정치가 나오고(원본은 16:20에 올림), 시간외·대량매매가 끝나는 18:00 뒤에
  외국인 값이 커진다(11-11 SK하이닉스 외국인 16:20 15,535억 → 21:40 18,551억).
  18:42(18:50 빈집·컨셉 계산 앞) · 20:40 두 번 돈다 — 값이 다른 행만 고쳐 써서 여러 번 돌아도 같다.
  며칠 뒤 KRX가 고치는 값은 다음 날부터 sync_buy(KIS 30일 정산)가 덮어쓴다.

실행: python3 collect_krx_buy.py [--date YYYY-MM-DD] [--dry]
"""

import time
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)

BLD = 'dbms/MDC/STAT/standard/MDCSTAT02401'
PAUSE = 0.4   # KRX 요청 사이 쉬는 시간(초)
# market_data 컬럼 ← KRX 투자자 코드(여럿이면 합산). collect_flow_empty.BUY_COLS와 같은 컬럼.
INVESTORS = {
    'pef_buy_amt':     ('3100',),           # 사모
    'trust_buy_amt':   ('3000',),           # 투신
    'pension_buy_amt': ('6000',),           # 연기금 등
    'foreign_buy_amt': ('9000', '9001'),    # 외국인 + 기타외국인 (KIS 외국인과 같은 범위)
}


def _kst_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def fetch_buy(krx, day: date) -> dict:
    """그날 투자자별 매수대금 — {컬럼: {종목코드: 원}}. 응답이 빈 투자자는 키를 넣지 않는다."""
    ymd = day.strftime('%Y%m%d')
    out = {}
    for col, codes in INVESTORS.items():
        won, got = {}, 0
        for inv in codes:
            rows = krx.get(BLD, mktId='ALL', invstTpCd=inv, strtDd=ymd, endDd=ymd,
                           share='1', money='1', csvxls_isNo='false').get('output') or []
            time.sleep(PAUSE)
            got += len(rows)
            for r in rows:
                code = (r.get('ISU_SRT_CD') or '').strip().upper()
                try:
                    v = int((r.get('BID_TRDVAL') or '0').replace(',', ''))
                except ValueError:
                    continue
                if code:
                    won[code] = won.get(code, 0) + v
        if got:
            out[col] = won
    return out


def to_rows(db_rows: list, buy: dict, day_iso: str) -> list:
    """DB 행과 KRX 값을 견줘 고쳐 쓸 행만 돌려준다 (DB·네트워크 없음).

    buy에 없는 컬럼(응답이 빈 투자자)은 건드리지 않는다. 목록에 없는 종목은 0.
    """
    rows = []
    for r in db_rows:
        code = r['stock_code']
        new = {col: round(won.get(code, 0) / 1e6) for col, won in buy.items()}
        if any(r.get(col) != v for col, v in new.items()):
            rows.append({'stock_code': code, 'base_date': day_iso, **new})
    return rows


def run(day: date = None, dry: bool = False) -> dict:
    """그날(기본 오늘) market_data 행에 KRX 매수대금을 적는다. 값이 다른 행만 쓴다."""
    from collect_active_etf import Krx
    from collect_flow_empty import _patch_rows

    day = day or _kst_today()
    iso = day.isoformat()
    sb = get_supabase_client()
    cols = list(INVESTORS)
    db_rows = fetch_all_pages(sb.table('market_data').select('stock_code,' + ','.join(cols))
                              .eq('base_date', iso).order('stock_code'))
    if not db_rows:
        log.warning(f'[당일매수대금] {iso} market_data 행이 없습니다 — 건너뜀')
        return {'date': iso, 'rows': 0, 'changed': 0, 'written': 0}

    krx = Krx()
    krx.login()
    buy = fetch_buy(krx, day)
    if 'foreign_buy_amt' not in buy:
        # 거래일 저녁에 외국인 목록이 비면 KRX 쪽 문제 — 0으로 덮어쓰지 않고 실패로 알린다
        raise RuntimeError(f'KRX {iso} 외국인 매수대금 응답이 비었습니다')
    missing = [c for c in cols if c not in buy]
    if missing:
        log.warning(f'[당일매수대금] {iso} 응답이 빈 투자자 {missing} — 그 컬럼은 그대로 둡니다')

    rows = to_rows(db_rows, buy, iso)
    known = {r['stock_code'] for r in db_rows}
    info = ' · '.join(f"{c.split('_')[0]} {sum(1 for k, v in buy[c].items() if v and k in known)}종목"
                      for c in cols if c in buy)
    if dry:
        log.info(f'[당일매수대금] dry-run {iso} — {len(db_rows)}행 중 {len(rows)}행 변경 예정 ({info})')
        return {'date': iso, 'rows': len(db_rows), 'changed': len(rows), 'written': 0}
    written = _patch_rows(sb, rows, '당일매수대금') if rows else 0
    log.info(f'[당일매수대금] {iso} — {len(db_rows)}행 중 변경 {len(rows)} · 기록 {written} ({info})')
    return {'date': iso, 'rows': len(db_rows), 'changed': len(rows), 'written': written}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='YYYY-MM-DD (기본 오늘)')
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 변경 행 수만')
    a = ap.parse_args()
    print(run(date.fromisoformat(a.date) if a.date else None, dry=a.dry))
