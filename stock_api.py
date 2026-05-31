import requests
import json
import os
import logging
import math
import time
import re
import html
from io import BytesIO
from urllib.parse import urljoin, urlencode
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# ✅ 매니저 모듈 임포트 (HistoryManager 추가됨)
from managers import (
    global_session as _session,
    kis_auth as _auth_manager,
    sheet_manager as _sheet_manager,
    telegram_bot as _telegram_bot,
    execution_manager as _exec_manager,
    HistoryManager,
    safe_float,   # managers 통합 버전 사용
    safe_int,     # managers 통합 버전 사용
)

# ✅ 파일 잠금 (Race Condition 방지)
try:
    from filelock import FileLock
except ImportError:
    from contextlib import contextmanager
    @contextmanager
    def FileLock(file_name, timeout=None): yield

# ✅ 구글 시트 라이브러리 체크
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    logging.warning("⚠️ gspread 또는 google-auth가 설치되지 않았습니다.")

try:
    from config import (
        KIS_APP_KEY, KIS_APP_SECRET, TELEGRAM_BOT_TOKEN,
        GOOGLE_JSON_FILE, GOOGLE_SHEET_URL, COMPANY_TO_INDUSTRY,
        COMPANY_CODES, INDUSTRY_HIERARCHY, INDUSTRY_CHAT_IDS, THEME_MAP, COMPANY_CHAT_IDS,
        CHAT_IDS_BY_CODE,
        DEFAULT_CHAT_ID # ✅ run_naver_report_job에서 사용
    )
except ImportError:
    logging.error("❌ [오류] config.py를 찾을 수 없거나 키 변수가 없습니다.")
    KIS_APP_KEY = None
    KIS_APP_SECRET = None
    TELEGRAM_BOT_TOKEN = None
    COMPANY_CODES = {}
    INDUSTRY_HIERARCHY = {}
    INDUSTRY_CHAT_IDS = {}
    COMPANY_CHAT_IDSS = {}
    CHAT_IDS_BY_CODE = {}
    THEME_MAP = {}
    DEFAULT_CHAT_ID = None

# ==========================================
# ⚙️ [Infrastructure] 설정
# ==========================================
BATCH_WORKERS = 10 

# ==========================================
# 🛠️ [Util] 헬퍼 함수
# ==========================================
def format_volume(val):
    if val >= 10000: return f"{val/10000:.1f}만"
    return f"{val}"

def format_money(val_100m: int, short: bool = False) -> str:
    """억 단위 변환 — format_utils.fmt_money() 위임 (인터페이스 유지)"""
    from format_utils import fmt_money
    return fmt_money(val_100m, short=short)

def get_weather_icon(rate: float) -> str:
    if rate >= 1.0: return "🔥"
    if rate > 0: return "🌤"
    if rate > -1.0: return "☁️"
    return "☔️"

def get_arrow(val):
    return "🔺" if val > 0 else "🔹" if val < 0 else ""

def _get_industry_targets(industry_name: str) -> tuple[str, list, dict] | None:
    """
    산업 분석 함수 공통 전처리. 6개 함수(get_sector_*, get_industry_*) 에서 공유.

    Args:
        industry_name: 산업명 (INDUSTRY_HIERARCHY 키)

    Returns:
        (industry_name, target_names, codes_map) 또는
        실패 시 에러 메시지 문자열 반환 (함수가 바로 return 할 수 있도록)

    Usage:
        result = _get_industry_targets(industry_name)
        if isinstance(result, str):  # 에러 메시지
            return result
        _, names, codes_map = result
    """
    if industry_name not in INDUSTRY_HIERARCHY:
        return f"⚠️ <b>'{industry_name}'</b> 섹터 정보를 찾을 수 없습니다."
    names = _get_targets_by_group(industry_name)
    if not names:
        return f"⚠️ <b>[{industry_name}]</b> 섹터에 등록된 종목이 없습니다."
    codes_map = {n: COMPANY_CODES[n] for n in names if n in COMPANY_CODES}
    if not codes_map:
        return f"⚠️ <b>[{industry_name}]</b> 등록된 종목코드가 없습니다."
    return industry_name, names, codes_map


def _get_targets_by_group(group_name: str) -> List[str]:
    """섹터명 또는 테마명으로 속한 모든 종목명 리스트 반환 (중복 제거)"""
    targets = []
    if group_name in INDUSTRY_HIERARCHY:
        for names in INDUSTRY_HIERARCHY[group_name].values():
            targets.extend(names)
    elif group_name in THEME_MAP:
        targets = THEME_MAP[group_name]
    else:
        for k, v in THEME_MAP.items():
            if group_name in k:
                targets = v
                break
    return list(set(targets))

def _call_kis_api(tr_id: str, path: str, code: str, extra_params: Dict = None, custtype: str = None, timeout: int = 10) -> Optional[Dict]:
    """매니저의 call_api로 위임"""
    return _auth_manager.call_api(tr_id, path, code, extra_params, custtype, timeout)

# ==========================================
# ⚡ [Refactor] 배치 작업 공통 실행기 (중복 제거 핵심)
# ==========================================
def _run_batch_job(items: List[Any], worker_func) -> List[Any]:
    """
    [최적화] 반복되는 ThreadPoolExecutor 로직을 하나로 통합
    - items: 실행할 인자들의 리스트 (인자가 여러 개면 튜플로 전달)
    - worker_func: 실행할 함수
    """
    results = []
    futures = []
    
    for item in items:
        # 인자가 튜플이면 언패킹(*item), 아니면 단일 인자로 전달
        if isinstance(item, tuple):
            futures.append(_exec_manager.submit_task(worker_func, *item))
        else:
            futures.append(_exec_manager.submit_task(worker_func, item))
            
    for future in as_completed(futures):
        try:
            res = future.result()
            if res: results.append(res)
        except Exception as e:
            # 개별 작업 에러는 로그만 남기고 전체 프로세스는 유지
            logging.error(f"Batch Job Error: {e}")
            
    return results

# ==========================================
# 📊 [Data Fetching] 데이터 수집
# ==========================================
def _fetch_naver_financials(code: str) -> Optional[Dict]:
    try:
        clean_code = code.split('.')[0]
        url = f"https://finance.naver.com/item/main.nhn?code={clean_code}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        target_table = soup.select_one('div.section.cop_analysis table')
        if not target_table: return None

        rows = target_table.select('tr')
        dates = []
        header_idx = -1
        for i, row in enumerate(rows):
            if re.search(r'20\d{2}[\./]\d{2}', row.text):
                cols = row.select('th') + row.select('td')
                dates = [c.text.strip() for c in cols if re.search(r'20\d{2}[\./]\d{2}', c.text.strip())]
                header_idx = i
                break
        
        if not dates: return None
        total_cols = len(dates)

        num_annual = 4
        try:
            header_row = target_table.select_one('thead tr')
            if header_row:
                ths = header_row.find_all('th')
                quarter_colspan = sum(int(th.get('colspan', 1)) for th in ths if "분기" in th.get_text())
                if quarter_colspan > 0: num_annual = total_cols - quarter_colspan
        except: pass

        sales_data, op_data = [], []
        for row in rows[header_idx+1:]:
            title = row.select_one('th').text.strip() if row.select_one('th') else ""
            cols = [c.text.strip().replace(',', '') for c in row.select('td')]
            
            if "매출액" in title and "률" not in title: sales_data = cols
            elif "영업이익" in title and "률" not in title: op_data = cols

        def clean_list(lst):
            return [int(x) if x.lstrip('-').isdigit() else 0 for x in lst]

        sales_data = clean_list(sales_data)
        op_data = clean_list(op_data)

        if len(sales_data) > total_cols: sales_data = sales_data[-total_cols:]
        if len(op_data) > total_cols: op_data = op_data[-total_cols:]
        
        return {
            "dates": dates, "sales": sales_data, "op": op_data, "num_annual": num_annual
        }
    except Exception:
        return None

def _format_financial_msg(data: Dict) -> str:
    if not data: return "(재무 정보 없음)"

    dates, sales, op = data['dates'], data['sales'], data['op']
    num_annual = data['num_annual']

    def fmt_section(d_list, s_list, o_list):
        txt = ""
        for i in range(min(len(d_list), len(s_list))):
            d = d_list[i].replace("(E)", "").strip()
            suffix = "(E)" if "(E)" in d_list[i] else ""
            d_str = (d[2:7] + suffix) if len(d) >= 7 else d
            
            s_val, o_val = s_list[i], o_list[i]
            if s_val == 0: continue
            
            margin = (o_val / s_val) * 100
            s_str = f"{s_val//10000}조" if s_val >= 10000 else f"{s_val}억"
            o_str = f"{o_val//10000}조" if abs(o_val) >= 10000 else f"{o_val}억"
            txt += f"{d_str}: {s_str} / {o_str} ({margin:.1f}%)\n"
        return txt.strip()

    y_str = fmt_section(dates[:num_annual], sales[:num_annual], op[:num_annual])
    q_str = fmt_section(dates[num_annual:], sales[num_annual:], op[num_annual:])

    return (
        f"📋 <b>실적 추이 (매출 / 영업익 / 이익률)</b>\n"
        f"-------------------\n<b>[연간]</b>\n{y_str}\n"
        f"-------------------\n<b>[분기]</b>\n{q_str}"
    )

# ==========================================
# 🚀 [Public API] 외부 호출 함수
# ==========================================

def get_company_chat_id(corp_name: str, stock_code: str = "") -> str | None:
    """
    corp_name → chat_id 조회. 전 파일 공통 사용 함수.

    종목명이 변경된 경우 stock_code 기반 fallback 사용.
    사용처: main.py, news_main.py, run_all.py 등 모든 발송 로직.

    Args:
        corp_name:  종목명 (DART/뉴스에서 받은 값)
        stock_code: 종목코드 (6자리 또는 .KS/.KQ suffix 포함 가능)

    Returns:
        chat_id 문자열 또는 None
    """
    # 1순위: 종목명 직접 매칭
    if corp_name in COMPANY_CHAT_IDS:
        return COMPANY_CHAT_IDS[corp_name]
    # 2순위: stock_code 기반 fallback (종목명 변경 시)
    if stock_code:
        code = stock_code.split(".")[0]
        chat_id = CHAT_IDS_BY_CODE.get(code)
        if chat_id:
            logging.info(
                f"ℹ️ [채팅방] '{corp_name}' 종목명 매칭 실패 "
                f"→ stock_code({code}) fallback"
            )
            return chat_id
    return None


def send_telegram(chat_id: str, text: str, preview: bool = False, keyboard: Dict = None):
    """매니저의 send_message로 위임"""
    _telegram_bot.send_message(chat_id, text, preview, keyboard)

def get_raw_price(code: str) -> Optional[Dict[str, Any]]:
    data = _call_kis_api(tr_id="FHKST01010100", path="quotations/inquire-price", code=code)
    return data['output'] if data else None

def get_investor_trend(code: str) -> Dict[str, str]:
    data = _call_kis_api(tr_id="FHKST01010900", path="quotations/inquire-investor", code=code)
    if not data or not data.get('output'): return {}
    
    output = data['output'][0]
    def fmt(val):
        v = safe_int(val)
        icon = get_arrow(v)
        if abs(v) >= 10000: return f"{icon} {v/10000:.1f}만"
        return f"{icon} {v}"

    return {
        "personal": fmt(output.get('prsn_ntby_qty')),
        "foreigner": fmt(output.get('frgn_ntby_qty')),
        "institution": fmt(output.get('orgn_ntby_qty'))
    }

def get_returns(code: str) -> Dict[str, Any]:
    try:
        end_dt = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
        
        data = _call_kis_api(
            tr_id="FHKST03010100", path="quotations/inquire-daily-itemchartprice",
            code=code, custtype="P",
            extra_params={
                "FID_INPUT_DATE_1": start_dt, "FID_INPUT_DATE_2": end_dt,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"
            }
        )
        
        result = {"1w": "-", "1m": "-", "3m": "-", "prev_vol": 0}
        if not data: return result

        output = data.get('output2', [])
        if not output: return result
        
        if len(output) >= 2: result["prev_vol"] = int(output[1]['acml_vol'])
        prices = [int(x['stck_clpr']) for x in output]
        
        def calc_ret(days_idx):
            if len(prices) > days_idx:
                past = prices[days_idx]
                if past == 0: return "-"
                val = (prices[0] - past) / past * 100
                icon = "+" if val > 0 else ""
                return f"{icon}{val:.1f}%"
            return "-"

        result["1w"] = calc_ret(5)
        result["1m"] = calc_ret(20)
        result["3m"] = calc_ret(60)
        return result
    except Exception as e:
        logging.error(f"Returns Calc Error: {e}")
        return {}

def get_stock_price(code: str) -> Optional[str]:
    output = get_raw_price(code)
    if not output: return None
    try:
        price = safe_int(output.get('stck_prpr'))
        rate = safe_float(output.get('prdy_ctrt'))
        cap_100m = safe_int(output.get('hts_avls'))
        cap_str = format_money(cap_100m)
        arrow = get_arrow(rate)
        return f"{price:,}원 ({arrow}{rate}%) | 시총 {cap_str}"
    except: return None

def get_investor_trend_cumulative(code: str, name: str) -> Optional[str]:
    try:
        data = _call_kis_api(tr_id="FHKST01010900", path="quotations/inquire-investor", code=code)
        if not data or not data.get('output'): return "❌ 데이터 조회 실패"
        
        daily_list = data['output']
        today = daily_list[0]
        
        p_today = safe_int(today['prsn_ntby_qty'])
        f_today = safe_int(today['frgn_ntby_qty'])
        i_today = safe_int(today['orgn_ntby_qty'])

        p_acc, f_acc, i_acc = 0, 0, 0
        for i in range(min(len(daily_list), 5)):
            item = daily_list[i]
            p_acc += safe_int(item['prsn_ntby_qty'])
            f_acc += safe_int(item['frgn_ntby_qty'])
            i_acc += safe_int(item['orgn_ntby_qty'])

        def analyze_supply(p, f, i):
            if f > 0 and i > 0: return "🔥 메이저 쌍끌이 (강세)", "👽외인 & 🏢기관 동시 매수!"
            elif f > 0 and i < 0: return ("👽 외국인 매수 우위", "외국인이 하락 방어 중") if f > abs(i) else ("📉 기관 매도세 출회", "외인이 사지만 기관 매도 강함")
            elif f < 0 and i > 0: return ("🏢 기관 매수 우위", "기관이 물량 받아내는 중") if i > abs(f) else ("📉 외국인 매도세", "기관이 사지만 외인 매도 강함")
            elif p > 0 and f < 0 and i < 0: return "🐜 개인만 매수 (불안)", "메이저 이탈"
            elif f < 0 and i < 0: return "📉 양매도 (수급 악화)", "모든 주체가 매도"
            else: return "⚖️ 혼조세", "뚜렷한 주포 없음"

        t_title, t_desc = analyze_supply(p_today, f_today, i_today)
        a_title, a_desc = analyze_supply(p_acc, f_acc, i_acc)

        def fmt(val):
            icon = get_arrow(val)
            if abs(val) >= 10000: return f"{icon} {val/10000:+.1f}만"
            return f"{icon} {val:+}"

        return (
            f"📊 <b>[{name}] 수급 진단</b>\n"
            f"════════════\n"
            f"📅 <b>[오늘]</b> {t_title}\n"
            f"└ {t_desc}\n"
            f"  • 👽 외인: {fmt(f_today)}\n"
            f"  • 🏢 기관: {fmt(i_today)}\n"
            f"  • 👤 개인: {fmt(p_today)}\n\n"
            f"📈 <b>[5일]</b> {a_title}\n"
            f"└ {a_desc}\n"
            f"  • 👽 외인: {fmt(f_acc)}\n"
            f"  • 🏢 기관: {fmt(i_acc)}\n"
            f"  • 👤 개인: {fmt(p_acc)}\n"
            f"════════════"
        )
    except Exception as e:
        logging.error(f"Investor Cumul Error: {e}")
        return None

def get_financial_summary(code: str) -> str:
    try:
        data = _fetch_naver_financials(code)
        if not data: return "(재무 데이터 불러오기 오류)"
        return _format_financial_msg(data)
    except Exception as e:
        logging.error(f"Financial Summary Error: {e}")
        return "(데이터 처리 오류)"

def get_stock_fundamental(code: str, name: str) -> Optional[str]:
    output = get_raw_price(code)
    if not output: return None
    try:
        per = safe_float(output.get('per'))
        pbr = safe_float(output.get('pbr'))
        eps = safe_int(output.get('eps'))
        roe = (pbr / per) * 100 if per > 0 else 0.0
        
        fin_trend = get_financial_summary(code)
        return (
            f"👑 <b>[{name}] 펀더멘털 및 실적 추이</b>\n"
            f"════════════\n"
            f"📊 <b>가치 지표</b>\n"
            f"• PER: {per:.2f}배 | EPS: {eps:,}원\n"
            f"• PBR: {pbr:.2f}배 | ROE: {roe:.1f}%\n"
            f"════════════\n"
            f"{fin_trend}\n" 
            f"════════════"
        )
    except Exception as e:
        logging.error(f"Fundamental Error: {e}")
        return None

def get_stock_detail(code: str, name: str = None) -> Optional[str]:
    output = get_raw_price(code)
    if not output: return None
    returns = get_returns(code)

    try:
        price = safe_int(output.get('stck_prpr'))
        rate = safe_float(output.get('prdy_ctrt'))
        val = safe_int(output.get('acml_tr_pbmn'))
        vol = safe_int(output.get('acml_vol'))
        prev_vol = returns.get('prev_vol', 0)
        vol_ratio = (vol / prev_vol) * 100 if prev_vol > 0 else 0
        total_shares = safe_int(output.get('lstn_stcn'))
        cap_100m = safe_int(output.get('hts_avls'))
        cap_str = format_money(cap_100m)
        high, low = safe_int(output.get('stck_hgpr')), safe_int(output.get('stck_lwpr'))
        w52_high, w52_low = safe_int(output.get('w52_hgpr')), safe_int(output.get('w52_lwpr'))
        frgn_rate = safe_float(output.get('hts_frgn_ehrt'))
        arrow = get_arrow(rate)

        returns_msg = ""
        if returns:
            returns_msg = (
                f"\n📅 <b>기간별 수익률</b>\n"
                f"• 1주: {returns.get('1w')}\n"
                f"• 1달: {returns.get('1m')}\n"
                f"• 3달: {returns.get('3m')}"
            )

        header = f"<b>[{name}]</b>\n" if name else ""

        return (
            f"{header}"
            f"════════════\n"
            f"<b>시총:</b> {cap_str}\n"
            f"<b>주식수:</b> {total_shares:,}주\n" 
            f"<b>현재가:</b> {price:,}<b>원</b> ({arrow}{rate}%)\n"
            f"<b>거래량:</b> {vol:,}주 ({vol_ratio:.0f}%)\n"
            f"<b>거래대금:</b> {val // 100000000:,}억\n"
            f"<b>당일 고가/저가:</b> {high:,} / {low:,}\n"
            f"<b>52주 최고/최저:</b> {w52_high:,} / {w52_low:,}\n"       
            f"<b>외인비중:</b> {frgn_rate}%\n"
            f"════════════"
            f"{returns_msg}"
        )
    except Exception as e:
        logging.error(f"Stock Detail Error: {e}")
        return None

def get_stock_chart(code: str, name: str = None) -> Optional[str]:
    try:
        end_dt = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

        data = _call_kis_api(
            tr_id="FHKST03010100", path="quotations/inquire-daily-itemchartprice",
            code=code, custtype="P",
            extra_params={
                "FID_INPUT_DATE_1": start_dt, "FID_INPUT_DATE_2": end_dt,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"
            }
        )

        if not data: return None
        output = data.get('output2', [])
        
        if not output or len(output) < 2:
            return f"📉 <b>[{name or code}] 차트 데이터 부족</b>\n신규 상장 등으로 기술적 분석을 위한 데이터가 부족합니다."

        prices = [int(x['stck_clpr']) for x in output]
        vols = [int(x['acml_vol']) for x in output]
        
        curr_price, prev_price = prices[0], prices[1]
        change_val = curr_price - prev_price
        
        curr_vol = vols[0]
        prev_vol = vols[1] if len(vols) > 1 and vols[1] > 0 else 1
        vol_ratio = (curr_vol / prev_vol) * 100
        
        vol_desc = "거래 급감"
        if vol_ratio > 200: vol_desc = "거래 폭발 🔥"
        elif vol_ratio > 120: vol_desc = "거래 증가"
        elif vol_ratio > 80: vol_desc = "평소 수준"
        elif vol_ratio < 50: vol_desc = "거래 감소"
        
        def ma(n): return sum(prices[:n]) // n if len(prices) >= n else 0
        ma5, ma20, ma60 = ma(5), ma(20), ma(60)

        trend_main = "판단 불가 (데이터 부족)"
        pos_desc = "위치 확인 불가"

        if ma60 > 0:
            if ma5 > ma20 > ma60: trend_main = "🚀 정배열 (상승세)"
            elif ma5 < ma20 < ma60: trend_main = "📉 역배열 (하락세)"
            elif ma20 > ma60 and curr_price > ma20: trend_main = "📈 눌림목/반등 시도"
            else: trend_main = "➡️ 혼조세 (박스권)"
        elif ma20 > 0:
            if curr_price > ma20: trend_main = "📈 단기 상승세"
            else: trend_main = "📉 단기 약세"
        
        if ma20 > 0:
            if curr_price > ma20: pos_desc = f"20일선({ma20:,}) 상회 ✨"
            elif curr_price < ma20: pos_desc = f"20일선({ma20:,}) 저항 ☁️"
            else: pos_desc = "20일선 공방 중"

        rsi_val, rsi_state, sentiment = 0, "데이터 부족", "중립"
        if len(prices) > 15:
            delta = [prices[i] - prices[i+1] for i in range(len(prices)-1)][::-1]
            ups = [x if x > 0 else 0 for x in delta]
            downs = [abs(x) if x < 0 else 0 for x in delta]
            
            period = 14 if len(prices) >= 15 else len(prices) - 1
            if period > 0:
                au = sum(ups[:period]) / period
                ad = sum(downs[:period]) / period
                rs = au / ad if ad != 0 else 0
                rsi_val = 100 - (100 / (1 + rs)) if rs != 0 else 50
                
                if rsi_val >= 70: rsi_state, sentiment = "🔥 과열 구간", "차익실현 욕구↑"
                elif rsi_val <= 30: rsi_state, sentiment = "🥶 침체 구간", "매도 우위 (과매도)"
                elif rsi_val >= 50: rsi_state, sentiment = "✨ 매수 우위", "매수세 유입"
                else: rsi_state, sentiment = "☁️ 매도 우위", "관망세 우세"

        bb_desc, disparity, is_squeeze = "산출 불가", "확인 불가", False
        if len(prices) >= 20:
            avg_20 = sum(prices[:20]) / 20
            variance = sum([(x - avg_20)**2 for x in prices[:20]]) / 20
            std_dev = math.sqrt(variance)
            upper, lower = avg_20 + (2 * std_dev), avg_20 - (2 * std_dev)
            bw = ((upper - lower) / avg_20) * 100 if avg_20 > 0 else 0
            
            if curr_price > upper: bb_desc = "🔥 상단 밴드 돌파"
            elif curr_price < lower: bb_desc = "💧 하단 밴드 이탈"
            elif bw < 5: bb_desc, is_squeeze = "🎻 스퀴즈 (폭발 임박)", True
            else: bb_desc = "밴드 내 등락"
            
            diff_range = upper - lower
            if diff_range > 0:
                if (curr_price - lower) < diff_range * 0.1: disparity = "단기 과낙폭 상태"
                elif (upper - curr_price) < diff_range * 0.1: disparity = "단기 고점 부담"
                else: disparity = "적정 범위"

        summary = "데이터 수집 중 (신규/거래부족)"
        if ma20 > 0:
            summary = "방향성 탐색 구간"
            if "정배열" in trend_main and rsi_val > 50: summary = "견조한 상승 추세"
            elif "역배열" in trend_main and rsi_val < 30: summary = "과매도 구간 반등 기대"
            elif vol_ratio > 200 and curr_price > ma20: summary = "거래량 동반 상승세"
            elif rsi_val >= 70: summary = "단기 과열 주의"
            elif is_squeeze: summary = "변동성 축소, 방향성 결정 임박"

        arrow = "🔺" if change_val > 0 else "🔹" if change_val < 0 else ""
        header = f"📈 <b>[{name or code}] 기술적 심층 진단</b>"
        return (
            f"{header}\n"
            f"════════════\n" 
            f"💰 {curr_price:,}원 ({arrow}{change_val:,})\n"
            f"📊 <b>거래강도:</b> 전일대비 {vol_ratio:.0f}% ({vol_desc})\n\n"
            f"1️⃣ <b>추세 (Trend)</b>\n"
            f"• 이평선: {trend_main}\n"
            f"• 위치: {pos_desc}\n\n"
            f"2️⃣ <b>모멘텀 (Momentum)</b>\n"
            f"• RSI: {rsi_val:.1f} ({rsi_state})\n"
            f"• 심리: {sentiment}\n\n"
            f"3️⃣ <b>변동성 (Volatility)</b>\n"
            f"• 볼린저: {bb_desc}\n"
            f"• 이격도: {disparity}\n\n"
            f"🏁 <b>[요약]</b> {summary}"
        )
    except Exception as e:
        logging.error(f"Chart Analysis Error: {e}")
        return None

def get_stock_briefing(target_name: str) -> str:
    try:
        footer_date_str = "" 
        point_msg = ""
        try:
            records = _sheet_manager.get_worksheet_data("투자포인트")
            item = next((r for r in records if str(r.get('종목명', '')).strip() == target_name), None)
            
            if item:
                raw_content = str(item.get('핵심포인트', item.get('내용', ''))).strip()
                note_content = str(item.get('비고', '')).strip()
                
                if raw_content:
                    lines = raw_content.split('\n')
                    formatted_lines = []
                    for line in lines:
                        stripped = line.strip()
                        if not stripped: continue
                        if re.match(r'^\d+\.', stripped):
                            prefix = "\n" if formatted_lines else ""
                            formatted_lines.append(f"{prefix}<b>{stripped}</b>")
                        else:
                            clean_text = stripped.lstrip('-• ').strip()
                            if clean_text: formatted_lines.append(clean_text)
                    point_msg += f"💡 <b>[투자 포인트]</b>\n{chr(10).join(formatted_lines)}\n"

                if note_content:
                    clean_date = note_content.replace("(", "").replace(")", "").replace("업데이트", "").replace("수정등록", "").strip()
                    if clean_date: footer_date_str = clean_date
        except Exception as e:
            logging.error(f"Point Error: {e}")

        separator, schedule_msg = "", ""
        try:
            all_events = _sheet_manager.get_worksheet_data("일정")
            events = [r for r in all_events if str(r.get('종목명', '')).strip() == target_name]
            
            if events:
                events = [e for e in events if str(e.get('날짜', '')).strip()]
                events.sort(key=lambda x: str(x.get('날짜', '9999-12-31')))
                
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                lines = []
                found_nearest = False
                
                for evt in events:
                    dt_str = str(evt.get('날짜', '')).strip()
                    note = str(evt.get('내용', '')).strip()
                    try:
                        target_date = datetime.strptime(dt_str, "%Y-%m-%d")
                        d_day = (target_date - today).days
                        if d_day < 0: lines.append(f"<s>• {dt_str} : {note}</s>")
                        else:
                            d_mark = "D-Day" if d_day == 0 else f"D-{d_day}"
                            if not found_nearest:
                                lines.append(f"• {dt_str} : <b>{note} ({d_mark})</b>")
                                found_nearest = True
                            else:
                                lines.append(f"• {dt_str} : {note} ({d_mark})")
                    except ValueError:
                        lines.append(f"• {dt_str} : {note}")
                if lines: schedule_msg = "🗓 <b>[주요 일정]</b>\n" + "\n".join(lines)
        except Exception as e:
            logging.error(f"Schedule Error: {e}")

        if point_msg and schedule_msg: separator = "════════════\n"
        elif not point_msg and not schedule_msg: return f"📋 <b>[{target_name}]</b>\n(등록된 정보가 없습니다)"

        final_msg = f"📋 <b>[{target_name}] 브리핑</b>\n════════════\n{point_msg}{separator}{schedule_msg}"
        if footer_date_str: final_msg += f"\n\n(Update: {footer_date_str})"
        return final_msg

    except Exception as e:
        logging.error(f"Briefing Critical Error: {e}")
        return f"❌ 브리핑 조회 실패: {e}"

def add_to_google_sheet(tab_name: str, data: list) -> bool:
    doc = _sheet_manager.get_doc()
    if not doc: return False
    try:
        doc.worksheet(tab_name).append_row(data)
        return True
    except Exception as e:
        logging.error(f"Sheet Write Error: {e}")
        return False
    
def update_investment_point(stock_name: str, new_content: str, note_date: str) -> bool:
    doc = _sheet_manager.get_doc()
    if not doc: return False
    
    try:
        sheet = doc.worksheet("투자포인트")
        try: cell = sheet.find(stock_name, in_column=1)
        except: cell = None

        if cell:
            row_idx = cell.row
            old_content = sheet.cell(row_idx, 2).value
            updated_content = f"{old_content}\n- {new_content}" if old_content else f"- {new_content}"
            sheet.update_cell(row_idx, 2, updated_content)
            sheet.update_cell(row_idx, 3, note_date)
            logging.info(f"✅ [{stock_name}] 투자포인트 업데이트 완료 (Row {row_idx})")
        else:
            sheet.append_row([stock_name, f"- {new_content}", note_date])
            logging.info(f"✅ [{stock_name}] 투자포인트 신규 추가 완료")
            
        _sheet_manager.clear_cache("투자포인트")
        return True
    except Exception as e:
        logging.error(f"Sheet Update Error: {e}")
        return False

def get_sector_status(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, target_names, _ = _t


    # ✅ [Refactor Step 2] 공통 배치 실행기 사용
    code_map = {name: COMPANY_CODES[name] for name in target_names if name in COMPANY_CODES}
    price_data = fetch_prices_batch(list(code_map.values()))
    
    if not price_data: return f"❌ <b>[{industry_name}]</b> 시세 데이터를 불러오지 못했습니다."

    results = [
        {"name": name, "rate": safe_float(data.get('prdy_ctrt'))}
        for name, code in code_map.items()
        if (data := price_data.get(code))
    ]
    
    if not results: return f"❌ 데이터를 처리할 수 없습니다."
    
    avg_rate = sum(r['rate'] for r in results) / len(results)
    results.sort(key=lambda x: x['rate'], reverse=True)
    
    leaders = results[:3]
    laggards = sorted(results[-3:], key=lambda x: x['rate']) if len(results) >= 5 else []

    status_icon = get_weather_icon(avg_rate)
    status_text = "불장" if avg_rate >= 1.0 else "맑음" if avg_rate > 0 else "흐림" if avg_rate > -1.0 else "폭우"
    arrow = get_arrow(avg_rate)

    msg = (
        f"🏭 <b>[{industry_name} 섹터 현황]</b>\n"
        f"════════════\n"
        f"<b>📊 평균 등락:</b> {arrow} {avg_rate:.2f}% ({status_icon} {status_text})\n\n"
        f"🚀 <b>상승 주도</b>\n"
    )
    
    for i, item in enumerate(leaders):
        msg += f"{i+1}. <b>{item['name']}</b> ({get_arrow(item['rate'])}{item['rate']}%)\n"

    if laggards:
        msg += f"\n💧 <b>하락 소외</b>\n"
        for i, item in enumerate(laggards):
            msg += f"{i+1}. {item['name']} ({get_arrow(item['rate'])}{item['rate']}%)\n"

    msg += f"════════════\n(총 {len(results)}개 종목 반영)"
    return msg

def get_sector_funds(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, target_names, _ = _t


    # 내부 워커 함수
    def _fetch_fund(name):
        if name not in COMPANY_CODES: return None
        code = COMPANY_CODES[name]
        
        # 1. 시세 조회
        p_data = get_raw_price(code)
        if not p_data: return None
        curr_price = safe_int(p_data.get('stck_prpr'))
        
        # 2. 수급 조회
        i_data = _call_kis_api(tr_id="FHKST01010900", path="quotations/inquire-investor", code=code, timeout=5)
        if not i_data or not i_data.get('output'): return None
        
        item = i_data['output'][0]
        f_amt = safe_int(item.get('frgn_ntby_qty')) * curr_price
        i_amt = safe_int(item.get('orgn_ntby_qty')) * curr_price
        p_amt = safe_int(item.get('prsn_ntby_qty')) * curr_price
        
        return {
            "name": name, "f_amt": f_amt, "i_amt": i_amt, "p_amt": p_amt, 
            "sum_major": f_amt + i_amt
        }

    # ✅ [Refactor Step 2] 공통 배치 실행기 사용
    stock_flows = _run_batch_job(target_names, _fetch_fund)

    if not stock_flows: return f"❌ <b>[{industry_name}]</b> 자금 데이터 집계 실패"

    total_f = sum(x['f_amt'] for x in stock_flows)
    total_i = sum(x['i_amt'] for x in stock_flows)
    total_p = sum(x['p_amt'] for x in stock_flows)

    def to_eok(val): 
        eok = val // 100000000
        icon = "🔺" if eok > 0 else "🔻" if eok < 0 else ""
        return f"{icon} {eok:+,}억"

    stock_flows.sort(key=lambda x: x['sum_major'], reverse=True)
    
    status_msg = ""
    if total_f > 0 and total_i > 0: status_msg = "(쌍끌이 🔥)"
    elif total_f > 0 and abs(total_f) > abs(total_i)*2: status_msg = "(외인 주도 👽)"
    elif total_i > 0 and abs(total_i) > abs(total_f)*2: status_msg = "(기관 주도 🏢)"
    elif (total_f + total_i) < 0 and total_p > 0: status_msg = "(개인만 매수 🐜)"

    from managers import market_timer
    footer_text = "(금일 마감 확정치)" if not market_timer.is_market_open() else "(장중 잠정치 합산)"

    msg = (
        f"💰 <b>[{industry_name} 섹터 자금 동향]</b>\n"
        f"════════════\n"
        f"👽 <b>외인:</b> {to_eok(total_f)}\n"
        f"🏢 <b>기관:</b> {to_eok(total_i)}\n"
        f"🐜 <b>개인:</b> {to_eok(total_p)} {status_msg}\n"
        f"════════════\n"
        f"🏆 <b>메이저 수급 주도 (Top 3)</b>\n"
    )

    for item in stock_flows[:3]:
        f_str = f"{item['f_amt'] // 100000000:+}억"
        i_str = f"{item['i_amt'] // 100000000:+}억"
        msg += f"• <b>{item['name']}:</b> 외인 {f_str} / 기관 {i_str}\n"

    msg += f"════════════\n{footer_text}"
    return msg

def fetch_prices_batch(codes: List[str], max_workers=BATCH_WORKERS) -> Dict[str, Dict]:
    """
    [Refactored] 전역 실행 관리자를 사용하여 스레드 생성 오버헤드 제거
    """
    results = {}
    unique_codes = list(set(filter(None, codes)))
    
    # 청크 처리는 그대로 유지 (KIS API 부하 분산)
    chunk_size = 50
    for i in range(0, len(unique_codes), chunk_size):
        chunk = unique_codes[i:i + chunk_size]
        
        # ✅ [Refactor Step 2] 내부적으로 _run_batch_job 패턴과 유사하게 전역 매니저 사용
        futures = []
        future_to_code = {}
        for code in chunk:
            future = _exec_manager.submit_task(get_raw_price, code)
            future_to_code[future] = code
            futures.append(future)
            
        for future in as_completed(futures):
            code = future_to_code[future]
            try:
                data = future.result()
                if data: results[code] = data
            except Exception as e:
                logging.error(f"Batch Fetch Error ({code}): {e}")
        
        time.sleep(0.1)
        
    return results

def get_market_scoreboard() -> str:
    if not INDUSTRY_HIERARCHY: return "⚠️ 산업 분류 데이터가 없습니다."

    # 1. 지수 조회
    kospi = get_index_status("0001")
    kosdaq = get_index_status("1001")
    
    def fmt_idx(data, name):
        if not data: return f"{name} (데이터 없음)"
        p, r = data['price'], data['rate']
        icon = "🔺" if r > 0 else "🔹" if r < 0 else ""
        rate_str = f"+{r}%" if r > 0 else f"{r}%"
        return f"<b>{name}</b> {p:,.2f} ({icon}{rate_str})"

    market_summary = (
        f"📉 <b>[Market Summary]</b>\n"
        f"{fmt_idx(kospi, '1. 코스피')}\n"
        f"{fmt_idx(kosdaq, '2. 코스닥')}\n"
    )

    # 2. 섹터별 평균 수익률 계산
    all_target_codes = []
    sector_map = {} 

    for ind_name, sub_sectors in INDUSTRY_HIERARCHY.items():
        codes_in_sector = []
        for names in sub_sectors.values():
            for name in names:
                if name in COMPANY_CODES:
                    code = COMPANY_CODES[name]
                    codes_in_sector.append(code)
                    all_target_codes.append(code)
        sector_map[ind_name] = codes_in_sector

    # 병렬 시세 조회 (Batch Fetch)
    price_map = fetch_prices_batch(all_target_codes, max_workers=BATCH_WORKERS)
    sector_data = []
    
    for ind_name, codes in sector_map.items():
        if not codes: continue
        total_rate = 0.0
        count = 0
        for code in codes:
            if code in price_map:
                data = price_map[code]
                rate = safe_float(data.get('prdy_ctrt'))
                total_rate += rate
                count += 1
        
        if count > 0:
            avg_rate = total_rate / count
            sector_data.append({"name": ind_name, "rate": avg_rate})

    if not sector_data: return "❌ 전광판 데이터를 집계하지 못했습니다."

    sector_data.sort(key=lambda x: x['rate'], reverse=True)

    from managers import market_timer
    time_str = "장중 추정" if market_timer.is_market_open() else "마감 확정"

    msg = (
        f"🔭 <b>[바티인베스트 시장 전광판]</b>\n"
        f"({datetime.now().strftime('%H:%M')} {time_str} 기준)\n"
        f"════════════\n"
        f"{market_summary}"
        f"════════════\n"
        f"🏭 <b>[섹터 랭킹]</b>\n"
    )

    for i, sec in enumerate(sector_data):
        rank = i + 1
        r_arrow = get_weather_icon(sec['rate'])
        r_str = f"+{sec['rate']:.2f}" if sec['rate'] > 0 else f"{sec['rate']:.2f}"
        msg += f"{rank}. <b>{sec['name']}</b> {r_arrow} {r_str}%\n"

    msg += f"════════════\n💡 <b>Tip:</b> 상세 분석은 각 산업방에서 <i>/업황</i> 또는 <i>/자금</i>"
    return msg

def get_universe_ranking() -> str:
    if not COMPANY_CODES: return "⚠️ 관리 중인 종목 데이터가 없습니다."

    all_codes = list(COMPANY_CODES.values())
    price_map = fetch_prices_batch(all_codes, max_workers=BATCH_WORKERS)
    
    ranking_data = []
    code_to_name = {v: k for k, v in COMPANY_CODES.items()}
    
    for code, data in price_map.items():
        name = code_to_name.get(code, code)
        rate = safe_float(data.get('prdy_ctrt'))
        ranking_data.append({"name": name, "rate": rate})
            
    if not ranking_data: return "❌ 랭킹 데이터를 집계하지 못했습니다."

    ranking_data.sort(key=lambda x: x['rate'], reverse=True)
    top_5 = ranking_data[:5]
    bottom_5 = ranking_data[-5:]
    bottom_5.sort(key=lambda x: x['rate']) 

    msg = (
        f"🏆 <b>[오늘의 유니버스 랭킹]</b>\n"
        f"({datetime.now().strftime('%H:%M')} 기준)\n"
        f"════════════\n"
        f"🚀 <b>급등 Top 5</b>\n"
    )
    
    for i, item in enumerate(top_5):
        msg += f"{i+1}. <b>{item['name']}</b> (🔺{item['rate']}%) \n"
        
    msg += f"\n💧 <b>급락 Bottom 5</b>\n"
    for i, item in enumerate(bottom_5):
        msg += f"{i+1}. {item['name']} (🔹{item['rate']}%) \n"

    msg += f"════════════\n(총 {len(ranking_data)}개 종목 분석)"
    return msg

def get_index_status(market_code: str):
    """
    네이버 증권에서 지수 정보를 크롤링하여 현재가와 등락률을 반환합니다.
    하락 시 마이너스(-) 부호가 누락되지 않도록 판별 로직을 강화했습니다.
    """
    result = {"price": 0.0, "rate": 0.0}
    target = "KOSPI" if market_code == "0001" else "KOSDAQ"
    try:
        url = f"https://finance.naver.com/sise/sise_index.naver?code={target}"
        headers = {'User-Agent': 'Mozilla/5.0'} 
        res = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(res.text, "html.parser")

        # 1. 현재 지수 추출
        now_val = soup.select_one("#now_value")
        if now_val: 
            result["price"] = float(now_val.text.replace(',', ''))
            
        # 2. 등락 정보 추출 및 하락 판별
        change_tag = soup.select_one("#change_value_and_rate")
        
        if change_tag:
            raw_text = change_tag.text.strip()
            # 숫자(등락률)만 추출 (예: 2.49)
            match = re.search(r'([\d\.]+)\s*%', raw_text)
            if match:
                rate = float(match.group(1))
                
                # [개선된 판별 로직]
                # 텍스트에 하락 기호(▼)가 있거나, 
                # 태그 클래스에 하락을 뜻하는 'nv'가 포함되어 있다면 마이너스 처리
                is_down = any(keyword in raw_text for keyword in ["▼", "하락", "-"]) or "nv" in str(change_tag)
                
                result["rate"] = -rate if is_down else rate
                
        return result
    except Exception as e:
        logging.error(f"❌ [{target}] 지수 크롤링 실패: {e}")
        return None
    
def compare_sectors(sec1: str, sec2: str) -> str:
    if sec1 not in INDUSTRY_HIERARCHY: return f"⚠️ '{sec1}' 섹터 정보가 없습니다."
    if sec2 not in INDUSTRY_HIERARCHY: return f"⚠️ '{sec2}' 섹터 정보가 없습니다."

    def analyze_group(group_name):
        targets = _get_targets_by_group(group_name)
        if not targets: return None
        
        code_map = {n: COMPANY_CODES[n] for n in targets if n in COMPANY_CODES}
        # 시세 조회
        data_map = fetch_prices_batch(list(code_map.values()))
        
        valid_items = []
        for name, code in code_map.items():
            if d := data_map.get(code):
                valid_items.append({
                    "name": name,
                    "rate": safe_float(d.get('prdy_ctrt'))
                })
        
        if not valid_items: return None
        avg = sum(x['rate'] for x in valid_items) / len(valid_items)
        top_stock = max(valid_items, key=lambda x: x['rate'])
        return {"avg": avg, "top": top_stock}

    d1, d2 = analyze_group(sec1), analyze_group(sec2)
    if not d1 or not d2: return "❌ 데이터 집계 실패"

    winner = sec1 if d1['avg'] > d2['avg'] else sec2
    diff = abs(d1['avg'] - d2['avg'])
    
    def get_icon(avg): return get_weather_icon(avg)

    return (
        f"⚖️ <b>[섹터 라이벌 매치]</b>\n"
        f"🛡️ <b>{sec1}</b> vs ⚔️ <b>{sec2}</b>\n"
        f"════════════\n"
        f"📊 <b>승자: {winner} (격차 {diff:.2f}%)</b>\n\n"
        f"1️⃣ <b>{sec1}</b> {get_icon(d1['avg'])}\n"
        f"• 평균: {d1['avg']:.2f}%\n"
        f"• 대장: {d1['top']['name']} ({d1['top']['rate']:.2f}%)\n\n"
        f"2️⃣ <b>{sec2}</b> {get_icon(d2['avg'])}\n"
        f"• 평균: {d2['avg']:.2f}%\n"
        f"• 대장: {d2['top']['name']} ({d2['top']['rate']:.2f}%)"
    )

def get_theme_analysis(theme_name: str) -> str:
    target_list = _get_targets_by_group(theme_name)
    real_name = theme_name 
    
    if not target_list: return f"⚠️ <b>'{theme_name}'</b> 관련 테마를 찾을 수 없습니다."

    code_map = {name: COMPANY_CODES[name] for name in target_list if name in COMPANY_CODES}
    price_data = fetch_prices_batch(list(code_map.values()))
    
    if not price_data: return f"❌ <b>[{theme_name}]</b> 테마 데이터가 없습니다."

    results = [
        {
            "name": name, 
            "rate": safe_float(d.get('prdy_ctrt')), 
            "cap": safe_int(d.get('hts_avls'))
        }
        for name, code in code_map.items()
        if (d := price_data.get(code))
    ]
    
    if not results: return f"❌ <b>[{theme_name}]</b> 데이터 처리 실패"

    results.sort(key=lambda x: x['rate'], reverse=True)
    avg_rate = sum(r['rate'] for r in results) / len(results)
    
    status_icon = get_weather_icon(avg_rate)
    avg_str = f"+{avg_rate:.2f}" if avg_rate > 0 else f"{avg_rate:.2f}"

    msg = (
        f"🧩 <b>[테마: {real_name}]</b>\n"
        f"📊 <b>평균: {avg_str}%</b> {status_icon} (총 {len(results)}개)\n"
        f"════════════\n"
    )
    
    for i, item in enumerate(results):
        rank = i + 1
        rate_txt = f"+{item['rate']}%" if item['rate'] > 0 else f"{item['rate']}%"
        msg += f"{rank}. <b>{item['name']}</b> {get_arrow(item['rate'])} {rate_txt} ({format_money(item['cap'])})\n"
        
    msg += f"════════════"
    return msg

def get_industry_theme_ranking(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, _, _ = _t  # names, codes_map 사용 안 함 — 아래서 INDUSTRY_HIERARCHY 직접 순회

    sub_sectors = INDUSTRY_HIERARCHY[industry_name]
    if not sub_sectors: return f"⚠️ <b>[{industry_name}]</b> 섹터에는 등록된 테마가 없습니다."

    theme_stats = []
    for sub_name, company_list in sub_sectors.items():
        total_rate = 0.0
        count = 0
        stocks_detail = []

        for name in company_list:
            if name not in COMPANY_CODES: continue
            data = get_raw_price(COMPANY_CODES[name])
            if data:
                rate = safe_float(data.get('prdy_ctrt'))
                cap_100m = safe_int(data.get('hts_avls')) 
                total_rate += rate
                count += 1
                stocks_detail.append({"name": name, "rate": rate, "cap": cap_100m})
        
        if count > 0:
            avg_rate = total_rate / count
            stocks_detail.sort(key=lambda x: x['rate'], reverse=True)
            theme_stats.append({"theme": sub_name, "avg": avg_rate, "stocks": stocks_detail})

    if not theme_stats: return f"❌ <b>[{industry_name}]</b> 테마 데이터를 집계하지 못했습니다."

    theme_stats.sort(key=lambda x: x['avg'], reverse=True)

    msg = (
        f"🏭 <b>[{industry_name} 테마 상세 현황]</b>\n"
        f"({datetime.now().strftime('%H:%M')} 기준)\n"
        f"════════════\n"
    )

    for i, item in enumerate(theme_stats):
        rank = i + 1
        t_name = item['theme']
        avg = item['avg']
        
        icon = "🔥" if avg >= 1.0 else "🔺" if avg > 0 else "🔹"
        avg_str = f"+{avg:.2f}" if avg > 0 else f"{avg:.2f}"
        msg += f"<b>{rank}. {t_name}</b> {icon} {avg_str}%\n"
        
        for stock in item['stocks']:
            s_name = stock['name']
            s_rate = stock['rate']
            cap_str = format_money(stock['cap'])
            s_icon = "🔺" if s_rate > 0 else "🔹" if s_rate < 0 else ""
            s_rate_txt = f"+{s_rate}%" if s_rate > 0 else f"{s_rate}%"
            msg += f"  └ {s_name} {s_rate_txt} ({cap_str})\n"
            
        msg += "\n"
    msg += f"════════════"
    return msg

def _get_trend_raw(code: str) -> Dict[str, float]:
    res = {"5d": 0.0, "20d": 0.0, "60d": 0.0}
    try:
        end_dt = datetime.now().strftime("%Y%m%d")
        start_dt = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
        
        data = _call_kis_api(
            tr_id="FHKST03010100", path="quotations/inquire-daily-itemchartprice",
            code=code, custtype="P",
            extra_params={
                "FID_INPUT_DATE_1": start_dt, "FID_INPUT_DATE_2": end_dt,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"
            }, timeout=5
        )
        if not data: return res
        output = data.get('output2', [])
        prices = [int(x['stck_clpr']) for x in output]
        if not prices: return res

        def calc(idx):
            if len(prices) > idx and prices[idx] > 0:
                return ((prices[0] - prices[idx]) / prices[idx]) * 100
            return 0.0
        return {"5d": calc(5), "20d": calc(20), "60d": calc(60)}
    except Exception: return res
    
def _crawl_financial_raw(code: str) -> Dict[str, Any]:
    res_data = {"full_msg": "(데이터 없음)", "latest_opm": -999.0}
    
    data = _fetch_naver_financials(code)
    if not data: return res_data
    
    res_data["full_msg"] = _format_financial_msg(data)
    q_sales = data['sales'][data['num_annual']:]
    q_ops = data['op'][data['num_annual']:]
    
    if q_sales and q_ops:
        last_s, last_o = q_sales[-1], q_ops[-1]
        if last_s != 0: res_data["latest_opm"] = (last_o / last_s) * 100

    return res_data

def get_sector_fundamental_comparison(target_name: str) -> str:
    targets = _get_targets_by_group(target_name)
    mode = f"그룹: {target_name}"
    
    if not targets: return f"⚠️ <b>'{target_name}'</b> 정보를 찾을 수 없습니다."
    targets = list(set(targets))[:10]
    
    def fetch_full_data(name):
        if name not in COMPANY_CODES: return None
        code = COMPANY_CODES[name]
        
        basic = get_raw_price(code)
        if not basic: return None
        
        cap_100m = safe_int(basic.get('hts_avls'))
        cap_str = format_money(cap_100m)

        fin = _crawl_financial_raw(code)
        trend = _get_trend_raw(code)

        per = safe_float(basic.get('per'))
        pbr = safe_float(basic.get('pbr'))
        eps = safe_int(basic.get('eps'))
        roe = (pbr / per * 100) if per > 0 else 0.0
        
        return {
            "name": name, "rate": safe_float(basic.get('prdy_ctrt')),
            "cap_str": cap_str, "per": per, "pbr": pbr, "eps": eps, "roe": roe,
            "t_5d": trend['5d'], "t_20d": trend['20d'], "t_60d": trend['60d'],
            "financial_msg": fin['full_msg'], "latest_opm": fin['latest_opm']
        }

    # ✅ [Refactor Step 2] 공통 배치 실행기 사용
    results = _run_batch_job(targets, fetch_full_data)
            
    if not results: return f"❌ <b>[{mode}]</b> 데이터 수집 실패."
    results.sort(key=lambda x: x['latest_opm'], reverse=True)

    msg = f"📑 <b>[{mode}] 심층 비교 분석</b>\n════════════\n"

    for i, d in enumerate(results):
        i_5 = "🔺" if d['t_5d'] > 0 else "🔹"
        i_20 = "🔺" if d['t_20d'] > 0 else "🔹"
        i_60 = "🔺" if d['t_60d'] > 0 else "🔹"
        
        msg += f"{i+1}️⃣ <b>{d['name']}</b> ({d['cap_str']})\n"
        msg += (
            f"<b>추세:</b> 5일 {i_5}{d['t_5d']:.1f}% │ "
            f"20일 {i_20}{d['t_20d']:.1f}% │ "
            f"60일 {i_60}{d['t_60d']:.1f}%\n"
        )
        msg += f"════════════\n" 
        msg += f"📊 <b>가치 지표</b>\n"
        msg += f"• PER: {d['per']:.2f}배 | EPS: {d['eps']:,}원\n"
        msg += f"• PBR: {d['pbr']:.2f}배 | ROE: {d['roe']:.1f}%\n"
        msg += f"════════════\n"
        msg += f"{d['financial_msg']}\n\n"

    msg += f"════════════"
    return msg

def _fetch_weekly_stats_batch(target_codes: Dict[str, str]) -> List[Dict]:
    def _worker(name, code):
        ret = get_returns(code)
        if not ret or ret.get('1w') == '-': return None
        try:
            val = float(ret['1w'].replace('%', ''))
            return {
                "name": name, "rate": val, 
                "r_1w": ret['1w'], "r_1m": ret.get('1m', '-'), "r_3m": ret.get('3m', '-')
            }
        except: return None

    # ✅ [Refactor Step 2] 공통 배치 실행기 사용
    # target_codes.items()는 (name, code) 튜플을 반환하므로 _run_batch_job이 자동으로 언패킹하여 _worker 호출
    items = list(target_codes.items())
    return _run_batch_job(items, _worker)

def _calculate_group_stats(stock_data: List[Dict], group_map: Dict[str, List[str]]) -> List[Dict]:
    stats = []
    for group_name, members in group_map.items():
        rates = [d['rate'] for d in stock_data if d['name'] in members]
        if rates:
            avg_rate = sum(rates) / len(rates)
            stats.append({"name": group_name, "avg": avg_rate})
    stats.sort(key=lambda x: x['avg'], reverse=True)
    return stats

def _format_top_bottom_msg(stock_data: List[Dict]) -> str:
    stock_data.sort(key=lambda x: x['rate'], reverse=True)
    top_5 = stock_data[:5]
    bottom_5 = stock_data[-5:]
    bottom_5.sort(key=lambda x: x['rate']) 

    msg = f"🚀 <b>주간 급등 Top 5</b>\n"
    for i, item in enumerate(top_5):
        msg += f"{i+1}. <b>{item['name']}</b> ({item['r_1w']} |{item['r_1m']} | {item['r_3m']})\n"

    msg += f"\n📉 <b>주간 급락 Top 5</b>\n"
    for i, item in enumerate(bottom_5):
        msg += f"{i+1}. <b>{item['name']}</b> ({item['r_1w']} | {item['r_1m']} | {item['r_3m']})\n"
    return msg

def get_weekly_universe_ranking() -> str:
    if not COMPANY_CODES: return "⚠️ 관리 종목 데이터가 없습니다."
    stock_data = _fetch_weekly_stats_batch(COMPANY_CODES)
    if not stock_data: return "❌ 주간 데이터 집계 실패"

    sector_map_flat = {}
    for ind, subs in INDUSTRY_HIERARCHY.items():
        all_members = []
        for sub_members in subs.values():
            all_members.extend(sub_members)
        sector_map_flat[ind] = list(set(all_members))

    sector_stats = _calculate_group_stats(stock_data, sector_map_flat)
    today_str = datetime.now().strftime('%m월 %d일')
    msg = (
        f"🏆 <b>[주간 시장 결산]</b>\n"
        f"({today_str} 기준 1주간 수익률)\n"
        f"════════════\n"
        f"🏭 <b>섹터별 성적표 (전체)</b>\n"
    )

    for i, sec in enumerate(sector_stats):
        avg = sec['avg']
        icon = "🥇" if i == 0 else "🔥" if avg > 0 else "💧"
        rate_str = f"+{avg:.2f}" if avg > 0 else f"{avg:.2f}"
        msg += f"{i+1}. <b>{sec['name']}</b> {icon} {rate_str}%\n"

    msg += f"════════════\n"
    msg += _format_top_bottom_msg(stock_data) 
    msg += f"════════════\n💡 괄호 안은 (1주 | 20일 | 60일) 수익률입니다."
    return msg

def get_industry_weekly_ranking(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, target_names, target_codes_map = _t

    sub_sectors = INDUSTRY_HIERARCHY.get(industry_name, {})

    stock_data = _fetch_weekly_stats_batch(target_codes_map)
    if not stock_data: return f"❌ [{industry_name}] 주간 데이터 집계 실패"

    theme_stats = _calculate_group_stats(stock_data, sub_sectors)
    today_str = datetime.now().strftime('%m월 %d일')
    msg = (
        f"🏭 <b>[{industry_name} 산업 주간 결산]</b>\n"
        f"({today_str} 기준 1주간 수익률)\n"
        f"════════════\n"
        f"🧩 <b>테마별 성적표 (전체)</b>\n\n"
    )

    for i, t in enumerate(theme_stats):
        avg = t['avg']
        icon = "🥇" if i == 0 else "🔥" if avg > 0 else "💧"
        rate_str = f"+{avg:.2f}" if avg > 0 else f"{avg:.2f}"
        msg += f"{i+1}. <b>{t['name']}</b> {icon} {rate_str}%\n"

    msg += f"════════════\n"
    msg += _format_top_bottom_msg(stock_data) 
    msg += f"════════════\n💡 괄호 안은 (1주 | 20일 | 60일) 수익률입니다."
    return msg

def get_industry_cap_ranking(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, target_names, target_codes_map = _t

    
    price_data_map = fetch_prices_batch(list(target_codes_map.values()), max_workers=10)
    returns_list = _fetch_weekly_stats_batch(target_codes_map)
    returns_map = {item['name']: item for item in returns_list}

    combined_list = []
    for name, code in target_codes_map.items():
        raw_price = price_data_map.get(code, {})
        cap_100m = safe_int(raw_price.get('hts_avls', 0))
        
        ret_data = returns_map.get(name, {})
        r_1w = ret_data.get('r_1w', '-').replace('%', '')
        r_1m = ret_data.get('r_1m', '-').replace('%', '')
        r_3m = ret_data.get('r_3m', '-').replace('%', '')
        
        combined_list.append({
            "name": name, "cap": cap_100m,
            "r_1w": r_1w, "r_1m": r_1m, "r_3m": r_3m
        })

    combined_list.sort(key=lambda x: x['cap'], reverse=True)

    today_str = datetime.now().strftime('%m/%d')
    msg = (
        f"🏭 <b>[{industry_name} 전체]</b> ({today_str})\n"
        f"<pre>순위 종목(시총) 1주|1달|3달</pre>\n"
    )

    for i, item in enumerate(combined_list):
        rank = i + 1
        cap_str = format_money(item['cap'], short=True)
        
        msg += (
            f"{rank}.<b>{item['name']}</b>({cap_str}) "
            f"{item['r_1w']}|{item['r_1m']}|{item['r_3m']}\n"
        )

    msg += f"──────────────\n(총 {len(combined_list)}개)"
    return msg

def _fetch_financials_batch(target_codes_map: Dict[str, str]) -> Dict[str, Dict]:
    def _worker(name, code):
        data = _fetch_naver_financials(code)
        if not data: return None
        
        try:
            num_annual = data['num_annual']
            if num_annual < 1: return None
            
            target_idx = num_annual - 1
            
            def get_data_at(idx):
                s = data['sales'][idx]
                o = data['op'][idx]
                d = data['dates'][idx]
                return s, o, d

            sales, op, date = get_data_at(target_idx)
            is_fallback = False

            if sales == 0 and target_idx > 0:
                prev_sales, prev_op, prev_date = get_data_at(target_idx - 1)
                if prev_sales != 0:
                    sales = prev_sales
                    op = prev_op
                    date = prev_date
                    is_fallback = True 

            opm = 0.0
            if sales != 0:
                opm = (op / sales) * 100
                
            return {
                "name": name, "date": date, "sales": sales, "op": op, "opm": opm,
                "is_fallback": is_fallback 
            }
        except: return None

    # ✅ [Refactor Step 2] 공통 배치 실행기 사용
    items = list(target_codes_map.items())
    results_list = _run_batch_job(items, _worker)
    
    # 리스트 결과를 딕셔너리로 변환
    return {item['name']: item for item in results_list}

def get_industry_financial_ranking(industry_name: str) -> str:
    _t = _get_industry_targets(industry_name)
    if isinstance(_t, str): return _t
    _, target_names, target_codes_map = _t

    price_data_map = fetch_prices_batch(list(target_codes_map.values()), max_workers=10)
    fin_map = _fetch_financials_batch(target_codes_map)
    
    combined_list = []
    
    year_counts = {}

    for name, code in target_codes_map.items():
        raw_price = price_data_map.get(code, {})
        cap_100m = safe_int(raw_price.get('hts_avls', 0))
        
        fin = fin_map.get(name, {})
        sales = fin.get('sales', 0)
        op = fin.get('op', 0)
        opm = fin.get('opm', 0.0)
        is_fallback = fin.get('is_fallback', False)
        
        if fin.get('date'):
            y = fin['date'].split('.')[0]
            year_counts[y] = year_counts.get(y, 0) + 1

        combined_list.append({
            "name": name, "cap": cap_100m,
            "sales": sales, "op": op, "opm": opm,
            "is_fallback": is_fallback
        })

    main_year = str(datetime.now().year) 
    
    if year_counts:
        max_year_val = max([int(y) for y in year_counts.keys()])
        main_year = str(max_year_val)

    try:
        prev_year_val = int(main_year) - 1
        prev_year_str = str(prev_year_val)[2:] 
    except:
        prev_year_str = "전년"

    combined_list.sort(key=lambda x: x['cap'], reverse=True)

    msg = (
        f"📊 <b>[{industry_name} 실적 현황]</b> ({main_year} 기준)\n"
        f"<pre>순위 종목(시총) 매출|영업익|이익률</pre>\n"
    )

    for i, item in enumerate(combined_list):
        rank = i + 1
        cap_str = format_money(item['cap'], short=True)
        s_str = format_money(item['sales'], short=True)
        o_str = format_money(item['op'], short=True)
        opm_str = f"{item['opm']:.1f}%"
        
        data_part = f"{s_str}|{o_str}|{opm_str}"
        
        if item['is_fallback']:
            data_part = f"<u>{data_part}</u>"
        
        msg += (
            f"{rank}.<b>{item['name']}</b>({cap_str}) "
            f"{data_part}\n"
        )

    msg += f"──────────────\n"
    msg += f"ℹ️ <u>밑줄</u>: 추정치 없어 '{prev_year_str}년 실적 반영\n"
    msg += f"(총 {len(combined_list)}개 종목)"
    return msg

# =================================================================================
# 📑 [Naver Report] 네이버 증권 리포트 수집 및 전송 (통합 모듈)
# =================================================================================

# 리포트 관련 상수 설정
NAVER_REPORT_CHAT_ID = "@batiarchive"  # 네이버 리포트 전용 채널 (기본값 — DB report_chat_id로 덮어씀)
NAVER_REPORT_URLS = {
    "기업분석": "https://finance.naver.com/research/company_list.naver",
    "시장정보": "https://finance.naver.com/research/market_info_list.naver",
    "산업분석": "https://finance.naver.com/research/industry_list.naver",
}

# 네이버 리포트 분류 -> config.py 산업군 키 매핑
REPORT_INDUSTRY_MAP = {
    "반도체": "반도체",
    "IT": "테크", "게임": "테크", "휴대폰": "테크", "디스플레이": "테크",
    "전기전자": "테크", "통신": "테크", "인터넷 포탈": "테크", "소프트웨어": "테크",
    "자동차": "2차전지", "2차전지": "2차전지",
    "바이오": "바이오", "제약": "바이오",
    "화장품": "뷰티",
    "조선": "조선", "해운": "조선",
    "유틸리티": "신재생", "에너지": "신재생",
    "담배": "소비재", "종이": "소비재", "홈쇼핑": "소비재",
    "음식료": "소비재", "섬유의류": "소비재", "여행": "소비재",
    "로봇": "로봇",
    "미디어": "엔터", "광고": "엔터",
}

REPORT_CONFIG = {
    "기업분석": {"title_idx": 0, "firm_idx": 2, "date_idx": 4, "link_idx": 3},
    "시장정보": {"title_idx": 0, "firm_idx": 1, "date_idx": 3, "link_idx": 2},
    "산업분석": {"industry_idx": 0, "title_idx": 1, "firm_idx": 2, "date_idx": 4, "link_idx": 3},
}

ROBOT_KEYWORDS = ["로봇", "액츄에이터", "로보틱스", "휴머로이드", "AMR", "AGV", "감속기", "서보모터", "휴머노이드"]

# -----------------------------------------------------------
# 🛠️ [Internal] 리포트 파싱 및 유틸리티
# -----------------------------------------------------------
def _sanitize_filename(file_name: str) -> str:
    return re.sub(r'[<>:"/\\\\|?*]', "_", file_name)

def _safe_caption(file_name: str) -> str:
    base = file_name[:-4] if file_name.lower().endswith(".pdf") else file_name
    return base.replace("_", " ")[:1024]

def _is_robot_topic(text: str) -> bool:
    return text and any(k.lower() in text.lower() for k in ROBOT_KEYWORDS)

def _make_hashtag(text: str) -> str:
    """텍스트 → 텔레그램 해시태그 (한글·영문·숫자만 허용, 공백/특수문자 제거)"""
    clean = re.sub(r'[^\w가-힣]', '', str(text).replace(' ', ''))
    return f'#{clean}' if clean else ''

def _extract_firm(file_name: str) -> str:
    """파일명 마지막 _XXX 부분에서 증권사명 추출
    예: 삼성전자_260519_하나증권.pdf → 하나증권
    """
    base = file_name[:-4] if file_name.lower().endswith('.pdf') else file_name
    parts = base.split('_')
    return parts[-1] if parts else ''

def _report_hashtags(page_type: str, tag: str, file_name: str) -> str:
    """리포트 해시태그 문자열 생성
    예: #기업분석 #삼성전자 #하나증권
    """
    tags = []
    pt = _make_hashtag(page_type)           # #산업분석 | #기업분석
    if pt: tags.append(pt)
    if tag:
        ht = _make_hashtag(tag)             # #자동차 | #삼성전자
        if ht and ht not in tags: tags.append(ht)
    firm = _extract_firm(file_name)
    if firm:
        ht = _make_hashtag(firm)            # #하나증권
        if ht and ht not in tags: tags.append(ht)
    return ' '.join(tags)

def _get_total_pages(soup) -> int:
    """네이버 페이지네이션에서 마지막 페이지 번호 추출"""
    try:
        last_page_tag = soup.select_one("td.pgRR a")
        if last_page_tag:
            return int(last_page_tag["href"].split("page=")[-1])
    except: pass
    return 1

def _parse_report_row(row, base_url: str, page_type: str):
    config = REPORT_CONFIG.get(page_type)
    if not config: return None

    cols = row.find_all("td")
    if not cols or len(cols) < max(config.values()) + 1: return None

    title_tag = cols[config["title_idx"]].find("a")
    if not title_tag: return None
    
    title = title_tag.text.strip()
    firm_name = cols[config["firm_idx"]].text.strip()
    raw_date = cols[config["date_idx"]].text.strip()
    link_tag = cols[config["link_idx"]].find("a", href=True)
    report_date = raw_date.replace(".", "") if raw_date else "000000"

    tag = None
    if page_type == "산업분석":
        industry = cols[config["industry_idx"]].text.strip()
        if industry in ("기타",) and _is_robot_topic(title):
            industry = "로봇"
        if industry:
            title = f"[{industry}] {title}"
            tag = industry
    elif page_type == "기업분석":
        tag = title # 기업명

    if link_tag:
        pdf_url = urljoin(base_url, link_tag["href"])
        file_name = _sanitize_filename(f"{title}_{report_date}_{firm_name}.pdf")
        return pdf_url, file_name, tag
    return None

def _fetch_pdf_file(pdf_url: str) -> Optional[BytesIO]:
    """PDF 파일을 메모리로 다운로드 (global_session 사용)"""
    try:
        # 파일 다운로드는 stream=True 권장
        with _session.get(pdf_url, stream=True, timeout=30) as r:
            r.raise_for_status()
            buf = BytesIO()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk: buf.write(chunk)
            buf.seek(0)
            return buf
    except Exception as e:
        logging.error(f"PDF Download Fail: {e}")
        return None

def _send_telegram_doc(chat_id: str, document, file_name: str, caption: str = None, retry_count: int = 0):
    """
    [수정됨] 텔레그램 문서(PDF) 전송 (429 속도제한 발생 시 대기 후 재전송)
    """
    if not TELEGRAM_BOT_TOKEN: return
    
    # 최대 3회까지만 재시도 (무한 루프 방지)
    if retry_count > 3:
        logging.error(f"❌ [Telegram] 3회 재시도 실패, 전송 포기 ({file_name})")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    
    try:
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        
        # 문서 전송 시도
        if isinstance(document, str): # URL인 경우
            data["document"] = document
            res = _session.post(url, data=data)
        else: # 파일 객체인 경우
            document.seek(0)
            files = {"document": (file_name, document, "application/pdf")}
            # 헤더 충돌 방지
            res = _session.post(url, data=data, files=files, headers={"Content-Type": None})
            
        # 🚨 [핵심 수정] 429 Too Many Requests (속도 제한) 처리
        if res.status_code == 429:
            wait_time = res.json().get("parameters", {}).get("retry_after", 10)
            logging.warning(f"⏳ [Telegram] 속도 제한 감지! {wait_time}초 대기 후 재전송... ({file_name})")
            
            # 지정된 시간만큼 멈춤 (이때 스케줄러도 멈춰서 기다림)
            time.sleep(wait_time + 1)
            
            # 재귀 호출로 다시 시도
            _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)
            return

        if res.status_code != 200:
            logging.error(f"⚠️ [Telegram] 전송 실패 ({res.status_code}): {res.text}")

        # 성공 시에도 연속 전송 방지를 위해 약간 대기 (1초)
        time.sleep(1.0)
        
    except Exception as e:
        logging.error(f"❌ [Telegram] Doc Error ({chat_id}): {e}")
        # 네트워크 에러 시에도 1번은 5초 뒤 재시도
        if retry_count < 1:
            time.sleep(5)
            _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)

def get_macro_briefing(data: dict) -> str | None:
    """
    글로벌 매크로 브리핑 메시지 생성 (06:30 KST 아침 발송용)

    data: collect_macro.collect_all() 반환 딕셔너리
      Keys: sp500/nasdaq/dow/vix/us10y,
            sp500_fut/nasdaq_fut,
            bitcoin,
            usd_krw/jpy_krw/eur_krw/cny_krw,
            wti/gold/gas/copper
            (+ 각 키 + '_chg' 로 전일 대비 등락률 %)
    """
    if not data:
        return None

    def _chg(chg):
        """등락률 포맷: ▲/▼ + 소수점 2자리. None이면 빈 문자열."""
        if chg is None:
            return ''
        arrow = '▲' if chg >= 0 else '▼'
        return f"  {arrow}{abs(chg):.2f}%"

    date_str = datetime.now().strftime('%m.%d')
    lines = [f"🌏 <b>글로벌 매크로</b>  {date_str}", ""]

    # ── 미국 마감 ────────────────────────────────────────────────
    sp = data.get('sp500');    sp_c = data.get('sp500_chg')
    nq = data.get('nasdaq');   nq_c = data.get('nasdaq_chg')
    dw = data.get('dow');      dw_c = data.get('dow_chg')
    vx = data.get('vix');      vx_c = data.get('vix_chg')

    if any(v is not None for v in [sp, nq, dw, vx]):
        lines.append("🇺🇸 <b>미국 마감</b>")
        if sp is not None: lines.append(f"S&amp;P500    {sp:>10,.0f}{_chg(sp_c)}")
        if nq is not None: lines.append(f"나스닥    {nq:>10,.0f}{_chg(nq_c)}")
        if dw is not None: lines.append(f"다우       {dw:>10,.0f}{_chg(dw_c)}")
        if vx is not None:
            # VIX는 하락이 긍정적(공포 완화) — 방향에 맥락 추가
            vix_note = ''
            if vx_c is not None:
                if vx_c <= -2.0:   vix_note = '  (공포 완화)'
                elif vx_c >= 2.0:  vix_note = '  (공포 확산)'
            lines.append(f"VIX         {vx:>10.2f}{_chg(vx_c)}{vix_note}")
        lines.append("")

    # ── 야간 선물 ────────────────────────────────────────────────
    sf = data.get('sp500_fut');  sf_c = data.get('sp500_fut_chg')
    nf = data.get('nasdaq_fut'); nf_c = data.get('nasdaq_fut_chg')

    if sf is not None or nf is not None:
        lines.append("📡 <b>야간 선물</b>")
        if sf is not None: lines.append(f"S&amp;P500F   {sf:>10,.0f}{_chg(sf_c)}")
        if nf is not None: lines.append(f"나스닥F   {nf:>10,.0f}{_chg(nf_c)}")
        lines.append("")

    # ── 미 10년물 + 달러/원 (자금흐름 핵심 지표) ─────────────────
    y10 = data.get('us10y');    y10_c = data.get('us10y_chg')
    usd = data.get('usd_krw');  usd_c = data.get('usd_krw_chg')

    if y10 is not None:
        lines.append(f"🏦 미 10년물  <b>{y10:.2f}%</b>{_chg(y10_c)}")
    if usd is not None:
        lines.append(f"💵 달러/원   <b>{usd:,.0f}원</b>{_chg(usd_c)}")
    if y10 is not None or usd is not None:
        lines.append("")

    # ── 기타 환율 ────────────────────────────────────────────────
    jpy = data.get('jpy_krw');  jpy_c = data.get('jpy_krw_chg')
    eur = data.get('eur_krw');  eur_c = data.get('eur_krw_chg')
    cny = data.get('cny_krw');  cny_c = data.get('cny_krw_chg')

    if any(v is not None for v in [jpy, eur, cny]):
        lines.append("💱 <b>기타 환율</b>")
        if jpy is not None: lines.append(f"엔화    {jpy:,.1f}원/100엔{_chg(jpy_c)}")
        if eur is not None: lines.append(f"유로    {eur:,.0f}원{_chg(eur_c)}")
        if cny is not None: lines.append(f"위안    {cny:.1f}원{_chg(cny_c)}")
        lines.append("")

    # ── 원자재 ──────────────────────────────────────────────────
    wti = data.get('wti');    wti_c = data.get('wti_chg')
    gld = data.get('gold');   gld_c = data.get('gold_chg')
    gas = data.get('gas');    gas_c = data.get('gas_chg')
    cop = data.get('copper'); cop_c = data.get('copper_chg')

    if any(v is not None for v in [wti, gld, gas, cop]):
        lines.append("🛢 <b>원자재</b>")
        if wti is not None: lines.append(f"WTI        {wti:.1f}${_chg(wti_c)}")
        if gld is not None: lines.append(f"금        {gld:,.0f}${_chg(gld_c)}")
        if gas is not None: lines.append(f"천연가스   {gas:.3f}${_chg(gas_c)}")
        if cop is not None: lines.append(f"구리       {cop:.3f}${_chg(cop_c)}")
        lines.append("")

    # ── 비트코인 ────────────────────────────────────────────────
    btc = data.get('bitcoin'); btc_c = data.get('bitcoin_chg')
    if btc is not None:
        lines.append(f"₿  <b>{btc:,.0f}$</b>{_chg(btc_c)}")

    # 마지막 빈줄 정리
    while lines and lines[-1] == '':
        lines.pop()

    return '\n'.join(lines)


def run_naver_report_job():
    """
    [최종 수정] 네이버 리포트 수집/전송 (페이지네이션 복원 + 중복 방지 + 메시지 분할)
    """
    # DB에서 리포트 채널 ID 동적 로드 (app_config.report_chat_id)
    try:
        from supabase_bridge import bridge as _b
        _report_cid = _b.get_config('report_chat_id', NAVER_REPORT_CHAT_ID)
    except Exception:
        _report_cid = NAVER_REPORT_CHAT_ID

    today_str = datetime.now().strftime("%Y-%m-%d")
    logging.info(f"📑 네이버 리포트 수집 시작 ({today_str})")

    # 히스토리 매니저 로드
    history = HistoryManager("sent_reports.txt", max_len=2000)

    for page_type in ["산업분석", "기업분석"]:
        base_url = NAVER_REPORT_URLS[page_type]
        reports = []
        page = 1
        
        while True:
            try:
                # 페이지별 요청
                params = {"searchType": "writeDate", "writeFromDate": today_str, "writeToDate": today_str, "page": page}
                res = _session.get(f"{base_url}?{urlencode(params)}")
                soup = BeautifulSoup(res.text, "html.parser")
                
                # 테이블 파싱
                table = soup.find("table", {"class": "type_1"})
                if not table: break # 테이블 없으면 종료

                # 행 단위 데이터 추출
                rows = table.find_all("tr")
                # 데이터가 없는 경우 (네이버는 데이터 없어도 빈 테이블 구조일 수 있음)
                if not rows: break 

                page_has_data = False
                for row in rows:
                    data = _parse_report_row(row, base_url, page_type)
                    if data:
                        page_has_data = True
                        pdf_url, file_name, tag = data
                        
                        # 이미 보낸 리포트는 건너뜀 (중복 방지)
                        if history.contains(file_name): continue
                        
                        reports.append(data)
                
                # 마지막 페이지 체크
                total_pages = _get_total_pages(soup)
                if page >= total_pages:
                    break
                
                page += 1
                time.sleep(0.2) # 페이지 넘김 딜레이

            except Exception as e:
                logging.error(f"Report Crawl Error ({page_type} p.{page}): {e}")
                break

        if not reports:
            logging.info(f"   -> {page_type}: 전송할 신규 리포트 없음")
            continue

        # [수정] 요약본 메인방 전송 (길이 제한 고려하여 분할 전송)
        if _report_cid:
            header = f"📑 <b>[{today_str}] {page_type} 리포트</b> (총 {len(reports)}개)\n\n"
            chunk_size = 30 # 한 번에 30개씩 끊어서 전송

            for i in range(0, len(reports), chunk_size):
                chunk = reports[i:i+chunk_size]
                msg_lines = []
                if i == 0: msg_lines.append(header)

                for j, item in enumerate(chunk):
                    # item: (pdf_url, file_name, tag)
                    clean_name = item[1].replace(".pdf", "").replace("_", " ")
                    msg_lines.append(f"{i+j+1}. {clean_name}")

                final_msg = "\n".join(msg_lines) + f"\n\n{_make_hashtag(page_type)}"
                send_telegram(_report_cid, final_msg)
                time.sleep(0.5)

        # 개별 파일 전송
        for pdf_url, file_name, tag in reports:
            # PDF 다운로드
            pdf_buf = _fetch_pdf_file(pdf_url)
            target_doc = pdf_buf if pdf_buf else pdf_url
            hashtags = _report_hashtags(page_type, tag, file_name)
            caption  = (
                f"📌 <a href='https://t.me/batiarchive'>바티아카이브</a> — 리포트·IR자료\n\n"
                f"{_safe_caption(file_name)}\n\n{hashtags}"
            )[:1024]

            # 1. 리포트 채널 전송 (batiarchive)
            if _report_cid:
                _send_telegram_doc(_report_cid, target_doc, file_name, caption)


            # 2. 타겟 채널 찾기
            targets = set()
            
            if page_type == "산업분석":
                mapped_ind = REPORT_INDUSTRY_MAP.get(tag)
                if mapped_ind and mapped_ind in INDUSTRY_CHAT_IDS:
                    targets.add(INDUSTRY_CHAT_IDS[mapped_ind])
            
            elif page_type == "기업분석":
                if tag in COMPANY_CHAT_IDS:
                    targets.add(COMPANY_CHAT_IDS[tag])
                if COMPANY_TO_INDUSTRY:
                    ind = COMPANY_TO_INDUSTRY.get(tag)
                    if ind and ind in INDUSTRY_CHAT_IDS:
                        targets.add(INDUSTRY_CHAT_IDS[ind])

            # 타겟 방들에 전송
            for chat_id in targets:
                if pdf_buf: pdf_buf.seek(0)
                _send_telegram_doc(chat_id, target_doc, file_name, caption)
            
            # [중요] 전송 성공 후에만 히스토리에 기록
            history.add(file_name)
            logging.info(f"   -> 리포트 전송 완료: {file_name}")

    logging.info("📑 리포트 작업 종료")