"""
collect_portfolio_nav.py
────────────────────────
투자노트(원본 포트폴리오 표) 일별 순자산·기준가 → portfolio_nav  (평일 16:30)

[원본] 태린이아빠 「포트폴리오 관리샘플」 국장 시트 — 원본좌수 1억, 기준가 = 순자산 ÷ 좌수 × 1000.
  원본은 수식상 순자산 = 원본좌수라 사람이 순자산을 고칠 때만 기준가가 움직인다.
  앱은 순자산을 자동으로 계산한다(10-04 사용자 결정):
    순자산 = Σ(현보유 × 종가) + 현금   — 현보유 = watchlist.quantity, 현금 = app_config portfolio_cash
    좌수   = Σ portfolio_flows.units    — 입출금 때 화면이 그때 기준가로 좌수를 늘리고 줄여 기록
  종가: market_data(정규장 마감 수집 15:45). 거기 없는 종목(인버스·레버리지 ETF 등 헤지 칸)은 KIS 현재가
  (장 마감 뒤라 종가)로 받아 detail에 함께 저장 — 화면은 시세가 없는 종목 가격을 여기서 읽는다.

실행:  python3 collect_portfolio_nav.py
"""
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client
from db_utils import fetch_all_pages

log = get_logger(__name__)


def _kis_close(code: str):
    from managers import kis_auth
    d = kis_auth.call_api(tr_id='FHKST01010100', path='quotations/inquire-price', code=code)
    try:
        p = float(((d or {}).get('output') or {}).get('stck_prpr') or 0)
    except ValueError:
        p = 0
    return p or None


def _has_schema(sb) -> bool:
    try:
        sb.table('portfolio_nav').select('base_date').limit(1).execute()
        sb.table('portfolio_flows').select('id').limit(1).execute()
        sb.table('watchlist').select('hedge').limit(1).execute()
        return True
    except Exception:
        return False


def run() -> dict:
    sb = get_supabase_client()
    if not _has_schema(sb):
        log.warning('[기준가] sql/portfolio_book.sql 미실행 — 건너뜀')
        return {}
    latest = sb.table('market_data').select('base_date').order('base_date', desc=True).limit(1).execute().data
    day = latest[0]['base_date'] if latest else (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()

    rows = [r for r in fetch_all_pages(sb.table('watchlist').select('stock_code,corp_name,quantity').order('id'))
            if (r.get('quantity') or 0) > 0]
    codes = sorted({r['stock_code'] for r in rows})
    close = {}
    if codes:
        for r in sb.table('market_data').select('stock_code,price').eq('base_date', day).in_('stock_code', codes).execute().data:
            if r.get('price'):
                close[r['stock_code']] = float(r['price'])
    for c in codes:
        if c not in close:                 # ETF 등 — KIS 현재가(장 마감 뒤 = 종가)
            p = _kis_close(c)
            if p:
                close[c] = p
            else:
                log.warning(f'[기준가] {c} 가격 없음 — 평가액에서 빠짐')

    qty = {}
    for r in rows:
        qty[r['stock_code']] = qty.get(r['stock_code'], 0) + int(r['quantity'])
    stock_value = sum(q * close[c] for c, q in qty.items() if c in close)
    cfg = sb.table('app_config').select('value').eq('key', 'portfolio_cash').limit(1).execute().data
    try:
        cash = float(cfg[0]['value']) if cfg else 0.0
    except (TypeError, ValueError):
        cash = 0.0
    units = sum(float(f['units'] or 0) for f in
                fetch_all_pages(sb.table('portfolio_flows').select('units,flow_date').lte('flow_date', day).order('id')))
    nav = stock_value + cash
    price = nav / units * 1000 if units > 0 else None
    sb.table('portfolio_nav').upsert({
        'base_date': day, 'stock_value': round(stock_value), 'cash': round(cash), 'nav': round(nav),
        'units': units, 'price': round(price, 4) if price is not None else None,
        'detail': {c: [q, close.get(c)] for c, q in qty.items()},
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }, on_conflict='base_date').execute()
    log.info(f'[기준가] {day} 순자산 {nav:,.0f} (주식 {stock_value:,.0f} + 현금 {cash:,.0f}) · 좌수 {units:,.0f}'
             f" · 기준가 {price:,.2f}" if price is not None else f'[기준가] {day} 순자산 {nav:,.0f} · 좌수 없음(입금 기록 전)')
    return {'date': day, 'nav': nav, 'price': price}


if __name__ == '__main__':
    run()
