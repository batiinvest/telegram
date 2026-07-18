"""
credit_chart.py — 신용융자 잔고 차트 텔레그램 발송
──────────────────────────────────────────────────
credit_balance_history(KOFIA, 매일 10:30 수집)에서 최근 3달을 읽어
코스피·코스닥 이중축 차트 PNG를 그려 본방으로 sendPhoto 발송.

중복 방지: sent_credit_chart.txt(gitignore)에 마지막 발송 base_date 기록 —
같은 기준일은 재발송하지 않으므로 휴장·발표지연으로 새 데이터가 없는 날은
자동으로 건너뛴다. jobs_collect.job_collect_credit_balance가 수집 직후 호출.

실행:
  python3 credit_chart.py                  # 신규 데이터 있을 때만 본방 발송
  python3 credit_chart.py --force          # 마커 무시하고 본방 발송
  python3 credit_chart.py --force --admin  # 관리자방으로 테스트 (마커 미기록)
"""
import io
import os
import sys
from datetime import date, timedelta

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

from config import DEFAULT_CHAT_ID, TELEGRAM_BOT_TOKEN
from db_client import get_supabase_client
from format_utils import chg_icon, fmt_change_pct
from logger_config import get_logger

log = get_logger(__name__)

PERIOD_DAYS = 90     # 차트 기간 (프론트 신용융자 카드 기본값 3달과 동일)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'sent_credit_chart.txt')

# 색은 프론트 js/credit-balance.js CB_SERIES와 동일 (코스피 좌축 / 코스닥 우축)
BG, GRID, TXT = '#171a21', '#2a2f3a', '#8b93a3'
C_KOSPI, C_KOSDAQ = '#2dce89', '#ffd600'


def _load_rows():
    """최근 PERIOD_DAYS일 시계열 (오름차순, 값 없는 행 제외)."""
    sb = get_supabase_client()
    cutoff = (date.today() - timedelta(days=PERIOD_DAYS)).isoformat()
    rows = (sb.table('credit_balance_history')
              .select('base_date,loan_kospi,loan_kosdaq')
              .gte('base_date', cutoff)
              .order('base_date').execute().data)
    return [r for r in rows
            if r['loan_kospi'] is not None and r['loan_kosdaq'] is not None]


def _render_png(rows) -> bytes:
    """이중축 라인차트 PNG. 서버에 한글 폰트가 없어 차트 내 라벨은 영문."""
    dates = [date.fromisoformat(r['base_date']) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax2 = ax.twinx()
    ax.plot(dates, [r['loan_kospi'] / 1e6 for r in rows], color=C_KOSPI, lw=2)
    ax2.plot(dates, [r['loan_kosdaq'] / 1e6 for r in rows], color=C_KOSDAQ, lw=2)
    ax.set_ylabel('KOSPI (trn KRW)', color=C_KOSPI, fontsize=9)
    ax2.set_ylabel('KOSDAQ (trn KRW)', color=C_KOSDAQ, fontsize=9)
    ax.tick_params(colors=C_KOSPI, labelsize=9)
    ax2.tick_params(colors=C_KOSDAQ, labelsize=9)
    ax.tick_params(axis='x', colors=TXT)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=7))
    ax.grid(color=GRID, lw=0.5)
    for sp in list(ax.spines.values()) + list(ax2.spines.values()):
        sp.set_color(GRID)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _market_block(name: str, rows, col: str) -> str:
    cur, prev, base = rows[-1][col], rows[-2][col], rows[0][col]
    d1_amt = int(round((cur - prev) / 100))          # 백만원 → 억
    d1_pct = (cur - prev) / prev * 100
    pd_pct = (cur - base) / base * 100
    return ('<b>{} {:.1f}조원</b>\n'
            '{} 전일 {}억 ({})\n'
            '{} 3달 {}').format(
        name, cur / 1e6,
        chg_icon(d1_pct), format(d1_amt, '+,'), fmt_change_pct(d1_pct, 1),
        chg_icon(pd_pct), fmt_change_pct(pd_pct, 1))


def _caption(rows) -> str:
    return ('🏦 <b>신용융자 잔고</b> · {} 기준\n\n{}\n\n{}').format(
        rows[-1]['base_date'],
        _market_block('코스피', rows, 'loan_kospi'),
        _market_block('코스닥', rows, 'loan_kosdaq'))


def _last_sent() -> str:
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''


def run(force: bool = False, chat_id: str = None) -> bool:
    """신규 기준일이 있으면 차트 발송. 발송했으면 True."""
    rows = _load_rows()
    if len(rows) < 2:
        log.warning('[신용잔고차트] 데이터 부족(%d행) — 발송 생략', len(rows))
        return False

    latest = rows[-1]['base_date']
    is_test = chat_id is not None
    if not force and latest == _last_sent():
        log.info('[신용잔고차트] %s 이미 발송됨 — 생략', latest)
        return False

    target = chat_id or DEFAULT_CHAT_ID
    png = _render_png(rows)
    r = requests.post(
        'https://api.telegram.org/bot{}/sendPhoto'.format(TELEGRAM_BOT_TOKEN),
        data={'chat_id': target, 'caption': _caption(rows), 'parse_mode': 'HTML'},
        files={'photo': ('credit_balance.png', png, 'image/png')},
        timeout=30)
    ok = r.status_code == 200 and r.json().get('ok')
    if not ok:
        log.error('[신용잔고차트] 발송 실패: %s %s', r.status_code, r.text[:300])
        return False

    log.info('[신용잔고차트] %s 기준 발송 완료 → %s', latest, target)
    if not is_test:                      # 테스트 발송은 마커를 건드리지 않음
        with open(STATE_FILE, 'w') as f:
            f.write(latest)
    return True


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv('/home/kjhofone/.env')
    to_admin = '--admin' in sys.argv
    if to_admin:
        from telegram_utils import get_admin_chat_id
    run(force='--force' in sys.argv,
        chat_id=get_admin_chat_id(fallback='@batiinvest') if to_admin else None)
