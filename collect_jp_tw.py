"""일본·대만 AI·반도체 공급망 시세 점검 → market_snapshot(kind='jp_tw', 하루 한 줄)

[원본] 태린이아빠 Market Dashboard '일본과 대만 시세점검'(공개 소스 app.py JP_TW_MARKET_SRC, v6 DAILY-FIRST).
  · 종목 97개 = 일본 40(.T) + 대만 57(.TW 상장 · .TWO 장외), 나라별 업종 27개 · 글로벌 테마 16개.
  · 시세 = Yahoo 일봉 Close(auto_adjust=False), 최근 4개월.
  · 종목별
      일간 % = 기준일 종가 ÷ 직전 거래일 종가 − 1
      5일 % · 20일 % = 기준일 종가 ÷ (그 나라의 5번째 · 20번째 앞 거래일 종가) − 1
        나라 거래일 = 그 나라 종목 가운데 하나라도 값이 있는 날. 그날 그 종목 값이 없으면 계산하지 않는다(더 옛 값으로 대신하지 않음).
  · 테마(나라를 묶은 16개) · 업종(나라별 27개)
      일간 · 5일 · 20일 = 종목 평균, Breadth = 일간이 오른 종목 비율(%)
      Strength Score = 0.4·일간 + 0.3·5일 + 0.2·20일 + 0.1·(Breadth − 50) ÷ 10
      Coverage = 유효 종목 ÷ 전체, 세 수익률이 모두 있는 종목 ÷ 전체 — 둘 다 80% 이상이어야 순위·신호에 넣는다.
  · 신호(커버리지 80% 이상 테마만, 아래로 갈수록 우선)
      NEW ROTATION?  일간 > 1.5 그리고 5일 ≤ 0
      HOT            일간 > 1.0 그리고 5일 > 2.0 그리고 Breadth ≥ 70
      STRONG TREND   5일 > 3.0 그리고 20일 > 5.0
  · 추세선 = Strength 상위 8개 테마. 최근 45일을 3일까지 앞 값으로 메운 뒤 마지막 21일, 첫날 = 100, 종목 평균.

[원본과 다른 점] 원본은 장중에도 돌려서 '진행 중인 봉'을 5분봉 · 대만거래소 실시간으로 검증한다.
  여기서는 장이 끝난 뒤에만 돌리고 끝난 일봉만 쓴다(그 나라 시각으로 마감 + 20분 전이면 오늘 봉은 버린다).
  기준일이 그 나라의 최신 거래일이 아닌 종목(거래 정지 · 값 누락)은 원본처럼 표에는 두되 평균 · 순위에서 뺀다.

[저장] base_date = 두 나라 기준일 가운데 늦은 날. 같은 날 다시 돌리면 덮어쓴다. 숫자와 신호 코드만 저장하고 문장은 화면이 붙인다.

[시각] 평일 16:40(일본 15:30 · 대만 14:30 마감 뒤) · 18:20(야후 값이 늦게 굳은 날 대비).

실행: python3 collect_jp_tw.py [--dry]
"""

import json
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from logger_config import get_logger
from db_client import get_supabase_client

log = get_logger(__name__)

TABLE = 'market_snapshot'
KIND = 'jp_tw'
LOOKBACK_PERIOD = '4mo'
BATCH_SIZE = 10
MIN_THEME_COVERAGE = 0.80
W_DAILY, W_5D, W_20D, W_BREADTH = 0.4, 0.3, 0.2, 0.1
# (시간대, 마감 시·분) — 마감 + 20분 전이면 오늘 봉은 진행 중으로 본다
MARKET = {'Japan': ('Asia/Tokyo', (15, 30)), 'Taiwan': ('Asia/Taipei', (13, 30))}
CLOSE_BUFFER_MIN = 20

STOCKS = {
    'Japan - MLCC / Components': {'Murata': '6981.T', 'TDK': '6762.T', 'Taiyo Yuden': '6976.T', 'Kyocera': '6971.T'},
    'Japan - Semiconductor Equipment': {'Tokyo Electron': '8035.T', 'SCREEN Holdings': '7735.T', 'Kokusai Electric': '6525.T',
                                        'Shibaura Mechatronics': '6590.T', 'Lasertec': '6920.T'},
    'Japan - Semiconductor Test': {'Advantest': '6857.T', 'Micronics Japan': '6871.T'},
    'Japan - Advanced Packaging / Back-End': {'DISCO': '6146.T', 'TOWA': '6315.T', 'Tokyo Seimitsu': '7729.T'},
    'Japan - Optical / Cable': {'Fujikura': '5803.T', 'Furukawa Electric': '5801.T', 'Sumitomo Electric': '5802.T',
                                'SEIKOH GIKEN': '6834.T', 'Kohoku Kogyo': '6524.T'},
    'Japan - Semiconductor Materials': {'Shin-Etsu Chemical': '4063.T', 'SUMCO': '3436.T', 'Resonac': '4004.T',
                                        'Mitsui Mining & Smelting': '5706.T', 'Tokyo Ohka Kogyo': '4186.T',
                                        'JX Advanced Metals': '5016.T', 'MEC': '4971.T', 'MARUWA': '5344.T',
                                        'Sumitomo Bakelite': '4203.T'},
    'Japan - AI Substrate / PCB': {'Ibiden': '4062.T', 'Ajinomoto': '2802.T', 'Meiko Electronics': '6787.T'},
    'Japan - Memory': {'Kioxia': '285A.T'},
    'Japan - ASIC / SoC': {'Socionext': '6526.T'},
    'Japan - Power Semiconductor': {'ROHM': '6963.T', 'Mitsubishi Electric': '6503.T', 'Fuji Electric': '6504.T', 'Renesas': '6723.T'},
    'Japan - Data Center Power / Cooling': {'Hitachi': '6501.T', 'Daikin': '6367.T', 'Nidec': '6594.T'},
    'Taiwan - Foundry / IC': {'TSMC': '2330.TW', 'UMC': '2303.TW', 'MediaTek': '2454.TW'},
    'Taiwan - Memory': {'Nanya Technology': '2408.TW', 'Winbond': '2344.TW', 'Macronix': '2337.TW'},
    'Taiwan - OSAT / Packaging': {'ASE Technology': '3711.TW', 'ChipMOS': '8150.TW', 'Powertech': '6239.TW'},
    'Taiwan - AI Server ODM': {'Quanta': '2382.TW', 'Wiwynn': '6669.TW', 'Wistron': '3231.TW', 'Hon Hai': '2317.TW',
                               'Inventec': '2356.TW', 'Gigabyte': '2376.TW'},
    'Taiwan - PCB / Substrate': {'Unimicron': '3037.TW', 'Kinsus': '3189.TW', 'Zhen Ding': '4958.TW', 'Compeq': '2313.TW',
                                 'Tripod': '3044.TW', 'Elite Material': '2383.TW', 'Gold Circuit Electronics': '2368.TW',
                                 'Nan Ya PCB': '8046.TW'},
    'Taiwan - Network': {'Accton': '2345.TW', 'Wistron NeWeb': '6285.TW', 'Sercomm': '5388.TW'},
    'Taiwan - Optical / CPO': {'LandMark Optoelectronics': '3081.TWO', 'LuxNet': '4979.TWO', 'FOCI': '3363.TWO',
                               'Browave': '3163.TWO', 'EZconn': '6442.TW'},
    'Taiwan - Server Power': {'Delta Electronics': '2308.TW', 'Lite-On': '2301.TW', 'Chicony Power': '6412.TW', 'AcBel': '6282.TW'},
    'Taiwan - Advanced Cooling': {'Auras Technology': '3324.TWO', 'Asia Vital Components': '3017.TW', 'Jentech Precision': '3653.TW',
                                  'Fositek': '6805.TW', 'Sunon': '2421.TW', 'Kaori': '8996.TW'},
    'Taiwan - Semiconductor Equipment': {'Gudeng Precision': '3680.TWO', 'Scientech': '3583.TW', 'Grand Process': '3131.TWO'},
    'Taiwan - Semiconductor Test': {'WinWay': '6515.TW', 'MPI': '6223.TWO', 'CHPT': '6510.TWO',
                                    'CCP Contact Probes': '6217.TWO', 'KYEC': '2449.TW'},
    'Taiwan - ASIC / Custom Silicon': {'Global Unichip': '3443.TW', 'Alchip': '3661.TW', 'Faraday Technology': '3035.TW', 'ASPEED': '5274.TWO'},
    'Taiwan - MLCC / Components': {'Yageo': '2327.TW', 'Walsin Technology': '2492.TW'},
    'Taiwan - Server Components': {'Lotes': '3533.TW', 'King Slide': '2059.TW'},
}

GLOBAL_THEMES = {
    'MLCC / Electronic Components': ['Murata', 'TDK', 'Taiyo Yuden', 'Kyocera', 'Yageo', 'Walsin Technology'],
    'Wafer Fab Equipment': ['Tokyo Electron', 'SCREEN Holdings', 'Kokusai Electric', 'Shibaura Mechatronics', 'Lasertec',
                            'Gudeng Precision', 'Scientech', 'Grand Process'],
    'AI Semiconductor Test': ['Advantest', 'Micronics Japan', 'WinWay', 'MPI', 'CHPT', 'CCP Contact Probes', 'KYEC'],
    'Advanced Packaging / Back-End': ['DISCO', 'TOWA', 'Tokyo Seimitsu', 'ASE Technology', 'ChipMOS', 'Powertech'],
    'Memory': ['Kioxia', 'Nanya Technology', 'Winbond', 'Macronix'],
    'Custom ASIC / SoC': ['Socionext', 'Global Unichip', 'Alchip', 'Faraday Technology', 'ASPEED', 'MediaTek'],
    'AI Server': ['Quanta', 'Wiwynn', 'Wistron', 'Hon Hai', 'Inventec', 'Gigabyte'],
    'PCB / Substrate': ['Ibiden', 'Ajinomoto', 'Meiko Electronics', 'Unimicron', 'Kinsus', 'Zhen Ding', 'Compeq', 'Tripod',
                        'Elite Material', 'Gold Circuit Electronics', 'Nan Ya PCB'],
    'Optical / CPO': ['Fujikura', 'Furukawa Electric', 'Sumitomo Electric', 'SEIKOH GIKEN', 'Kohoku Kogyo',
                      'LandMark Optoelectronics', 'LuxNet', 'FOCI', 'Browave', 'EZconn'],
    'Network': ['Accton', 'Wistron NeWeb', 'Sercomm'],
    'Power Semiconductor': ['ROHM', 'Mitsubishi Electric', 'Fuji Electric', 'Renesas'],
    'Data Center Power': ['Hitachi', 'Nidec', 'Delta Electronics', 'Lite-On', 'Chicony Power', 'AcBel'],
    'Data Center Cooling': ['Daikin', 'Auras Technology', 'Asia Vital Components', 'Jentech Precision', 'Fositek', 'Sunon', 'Kaori'],
    'Foundry / IC': ['TSMC', 'UMC', 'MediaTek'],
    'Semiconductor Materials': ['Shin-Etsu Chemical', 'SUMCO', 'Resonac', 'Mitsui Mining & Smelting', 'Tokyo Ohka Kogyo',
                                'JX Advanced Metals', 'MEC', 'MARUWA', 'Sumitomo Bakelite'],
    'Server Components': ['Lotes', 'King Slide'],
}

# 원본 KOREA_WATCHLIST — 테마가 움직일 때 같이 볼 한국 관련주(원본 영문 표기를 한글 종목명으로)
#   이름은 companies.name과 같게 적는다(화면이 이름으로 종목 코드를 찾아 종목 창을 연다 — 예: LS ELECTRIC = '엘에스일렉트릭').
#   원본의 'RFHIC-related names' · 'HVAC / liquid-cooling related Korea names'처럼 묶어 적은 것은 대표 이름 · 설명으로 둔다.
KOREA_WATCHLIST = {
    'MLCC / Electronic Components': ['삼성전기', '삼화콘덴서공업'],
    'Wafer Fab Equipment': ['테스', '피에스케이', '주성엔지니어링', '유진테크', 'HPSP', '원익IPS', '이오테크닉스'],
    'AI Semiconductor Test': ['ISC', '메가터치', '티에프이', '리노공업', '마이크로컨텍솔', '오킨스전자'],
    'Advanced Packaging / Back-End': ['한미반도체', '한양디지텍', '하나마이크론', '네패스아크', '티에프이'],
    'Memory': ['삼성전자', 'SK하이닉스'],
    'Custom ASIC / SoC': ['ISC', '티에프이', '삼성전자'],
    'AI Server': ['삼성전자', 'SK하이닉스', '삼성전기', '이수페타시스'],
    'PCB / Substrate': ['코리아써키트', '대덕전자', '이수페타시스', '삼성전기'],
    'Optical / CPO': ['RF머트리얼즈', '오이솔루션', '우리로', '티에프이'],
    'Network': ['이수페타시스', '대덕전자', 'RF머트리얼즈', '오이솔루션'],
    'Power Semiconductor': ['DB하이텍', 'KEC', 'RFHIC'],
    'Data Center Power': ['HD현대일렉트릭', '효성중공업', '엘에스일렉트릭', 'LS', '일진전기', '가온전선'],
    'Data Center Cooling': ['GST', '공조·액체냉각 관련주'],
    'Foundry / IC': ['삼성전자', 'DB하이텍'],
    'Semiconductor Materials': ['이엔에프테크놀로지', '솔브레인', '한솔케미칼', '동진쎄미켐'],
    'Server Components': ['삼성전기', '대덕전자', '이수페타시스'],
}

SPECIAL_WATCH = {
    'AI_TEST_CHAIN': ['Advantest', 'Micronics Japan', 'WinWay', 'MPI', 'CHPT', 'CCP Contact Probes', 'KYEC'],
    'CUSTOM_ASIC_CHAIN': ['Socionext', 'Global Unichip', 'Alchip', 'Faraday Technology', 'ASPEED', 'MediaTek'],
    'OPTICAL_CPO_CHAIN': ['Fujikura', 'Furukawa Electric', 'Sumitomo Electric', 'SEIKOH GIKEN', 'Kohoku Kogyo',
                          'LandMark Optoelectronics', 'LuxNet', 'FOCI', 'Browave', 'EZconn'],
    'AI_COOLING_CHAIN': ['Daikin', 'Auras Technology', 'Asia Vital Components', 'Jentech Precision', 'Fositek', 'Sunon', 'Kaori'],
    'MLCC_CHAIN': ['Murata', 'TDK', 'Taiyo Yuden', 'Kyocera', 'Yageo', 'Walsin Technology'],
    'PCB_SUBSTRATE_CHAIN': ['Ibiden', 'Ajinomoto', 'Meiko Electronics', 'Unimicron', 'Kinsus', 'Zhen Ding', 'Compeq', 'Tripod',
                            'Elite Material', 'Gold Circuit Electronics', 'Nan Ya PCB'],
}

INFO = {t: {'country': 'Japan' if t.endswith('.T') else 'Taiwan', 'sector': sec, 'company': co}
        for sec, cos in STOCKS.items() for co, t in cos.items()}
TICKERS = list(INFO)


def _has_schema(sb) -> bool:
    try:
        sb.table(TABLE).select('base_date').limit(1).execute()
        return True
    except Exception:
        return False


def _num(v, nd=2):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def fetch_history():
    """Yahoo 일봉 종가(수정 전) → DataFrame(날짜 × 티커). 10개씩 받고, 통째로 빈 티커는 하나씩 다시 받는다."""
    import pandas as pd
    import yfinance as yf

    chunks = []
    for i in range(0, len(TICKERS), BATCH_SIZE):
        batch = TICKERS[i:i + BATCH_SIZE]
        try:
            raw = yf.download(tickers=batch, period=LOOKBACK_PERIOD, interval='1d', auto_adjust=False, actions=False,
                              progress=False, threads=False, group_by='column', ignore_tz=True, timeout=15)
            if raw is not None and not raw.empty:
                q = raw['Close'] if getattr(raw.columns, 'nlevels', 1) > 1 else raw[['Close']].rename(columns={'Close': batch[0]})
                chunks.append(q.apply(pd.to_numeric, errors='coerce'))
        except Exception as e:
            log.warning(f'[일본대만] 야후 묶음 실패({batch[0]}…): {str(e)[:100]}')
        time.sleep(0.15)
    hist = pd.concat(chunks, axis=1).sort_index() if chunks else pd.DataFrame()
    hist = hist.loc[:, ~hist.columns.duplicated()] if not hist.empty else hist
    missing = [t for t in TICKERS if t not in hist.columns or not hist[t].notna().any()]
    for t in missing[:18]:
        try:
            s = yf.Ticker(t).history(period=LOOKBACK_PERIOD, interval='1d', auto_adjust=False, actions=False, timeout=12)['Close']
            s = pd.to_numeric(s, errors='coerce').dropna()
            if len(s):
                s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
                hist = hist.reindex(hist.index.union(s.index)) if not hist.empty else pd.DataFrame(index=s.index)
                hist[t] = s.reindex(hist.index)
        except Exception as e:
            log.warning(f'[일본대만] {t} 개별 받기 실패: {str(e)[:80]}')
        time.sleep(0.35)
    if hist.empty:
        raise RuntimeError('야후 일봉을 받지 못했습니다')
    hist.index = pd.DatetimeIndex(hist.index).tz_localize(None).normalize()
    hist = hist.loc[~hist.index.duplicated(keep='last')].sort_index()
    return hist.reindex(columns=TICKERS).where(lambda d: d > 0)


def drop_unfinished(hist, now_utc=None):
    """그 나라 시각으로 마감 + 20분 전이면 오늘 봉은 진행 중 — 버린다."""
    import numpy as np
    import pandas as pd

    now_utc = now_utc or datetime.now(timezone.utc)
    hist = hist.copy()
    for country, (tz, (hh, mm)) in MARKET.items():
        local = now_utc.astimezone(ZoneInfo(tz))
        if (local.hour, local.minute) >= divmod(hh * 60 + mm + CLOSE_BUFFER_MIN, 60):
            continue
        key = pd.Timestamp(local.date())
        if key in hist.index:
            cols = [t for t in TICKERS if INFO[t]['country'] == country]
            hist.loc[key, cols] = np.nan
    return hist.dropna(how='all')


def compute(hist) -> dict:
    """원본 3) ~ 7) — 종목 수익률 · 테마/업종 요약 · 신호 · 추세선."""
    import numpy as np
    import pandas as pd

    sessions = {c: hist[[t for t in TICKERS if INFO[t]['country'] == c]].dropna(how='all').index for c in MARKET}
    asof = {c: (s[-1] if len(s) else None) for c, s in sessions.items()}

    rows = []
    for t in TICKERS:
        meta = INFO[t]
        ses = sessions[meta['country']]
        s = hist[t].dropna()
        row = {'country': meta['country'], 'sector': meta['sector'], 'company': meta['company'], 'ticker': t,
               'last': None, 'prev': None, 'd': np.nan, 'f': np.nan, 'm': np.nan, 'asof': None, 'ok': False}
        if len(s):
            own = s.index[-1]
            row['last'], row['asof'] = float(s.iloc[-1]), own.date().isoformat()
            before = ses[ses < own]
            if own == asof[meta['country']] and len(before) and before[-1] in s.index:
                px = float(s.iloc[-1])
                row['prev'] = float(s.loc[before[-1]])
                row['d'] = (px / row['prev'] - 1) * 100
                row['ok'] = True
                for key, n in (('f', 5), ('m', 20)):
                    if len(before) >= n and before[-n] in s.index:
                        row[key] = (px / float(s.loc[before[-n]]) - 1) * 100
        rows.append(row)
    df = pd.DataFrame(rows)
    ok = df[df['ok']]

    def summary(temp, total):
        if not len(temp):
            return {'n': 0, 'total': total, 'cov': 0.0, 'icov': 0.0, 'complete': False,
                    'd': None, 'med': None, 'f': None, 'm': None, 'b': None, 'score': None}
        d, f, m = temp['d'].mean(), temp['f'].mean(), temp['m'].mean()
        b = (temp['d'] > 0).mean() * 100
        cov = 100 * len(temp) / total
        icov = 100 * temp[['d', 'f', 'm']].notna().all(axis=1).sum() / total
        return {'n': int(len(temp)), 'total': total, 'cov': _num(cov, 1), 'icov': _num(icov, 1),
                'complete': bool(cov >= MIN_THEME_COVERAGE * 100 and icov >= MIN_THEME_COVERAGE * 100),
                'd': _num(d, 4), 'med': _num(temp['d'].median(), 4), 'f': _num(f, 4), 'm': _num(m, 4), 'b': _num(b, 2),
                'score': _num(W_DAILY * d + W_5D * f + W_20D * m + W_BREADTH * (b - 50) / 10, 4)}

    themes = []
    for theme, companies in GLOBAL_THEMES.items():
        s = summary(ok[ok['company'].isin(companies)], len(companies))
        signal = None
        if s['complete']:
            signal = 'NEUTRAL'
            if s['d'] > 1.5 and s['f'] <= 0:
                signal = 'NEW_ROTATION'
            if s['d'] > 1.0 and s['f'] > 2.0 and s['b'] >= 70:
                signal = 'HOT'
            if s['f'] > 3.0 and s['m'] > 5.0:
                signal = 'STRONG_TREND'
        themes.append({'theme': theme, **s, 'signal': signal, 'members': companies, 'korea': KOREA_WATCHLIST.get(theme, [])})
    themes.sort(key=lambda x: (x['score'] is None, -(x['score'] or 0)))

    sectors = []
    for sec, cos in STOCKS.items():
        s = summary(ok[ok['sector'] == sec], len(cos))
        sectors.append({'country': 'Japan' if sec.startswith('Japan') else 'Taiwan', 'sector': sec, **s})
    sectors.sort(key=lambda x: (x['score'] is None, -(x['score'] or 0)))

    # 추세선 — 커버리지 80% 이상 테마 가운데 Strength 상위 8개, 최근 21일 · 첫날 = 100
    co2t = {v['company']: t for t, v in INFO.items()}
    curves = {}
    for th in [x for x in themes if x['complete']][:8]:
        names = GLOBAL_THEMES[th['theme']]
        raw = hist[[co2t[n] for n in names if n in co2t]].tail(45).ffill(limit=3).tail(21)
        if len(raw) < 15:
            continue
        first = raw.iloc[0]
        good = first[first.notna() & (first > 0)].index
        if len(good) < max(1, int(.8 * len(names))):
            continue
        curves[th['theme']] = raw[good].div(first[good]).mul(100).mean(axis=1, skipna=False)
    curve = {'dates': [], 'series': {}}
    if curves:
        cdf = pd.DataFrame(curves)
        curve = {'dates': [d.date().isoformat() for d in cdf.index],
                 'series': {k: [_num(v, 2) for v in cdf[k]] for k in cdf}}

    stocks = [{'c': r['country'], 's': r['sector'], 'n': r['company'], 't': r['ticker'],
               'last': _num(r['last'], 2), 'prev': _num(r['prev'], 2),
               'd': _num(r['d']), 'f': _num(r['f']), 'm': _num(r['m']), 'asof': r['asof'], 'ok': bool(r['ok'])} for r in rows]
    return {'asof': {c: (v.date().isoformat() if v is not None else None) for c, v in asof.items()},
            'valid': int(len(ok)), 'total': len(TICKERS),
            'stocks': stocks, 'themes': themes, 'sectors': sectors, 'curve': curve, 'special': SPECIAL_WATCH}


def run(dry: bool = False) -> dict:
    sb = get_supabase_client()
    if not dry and not _has_schema(sb):
        log.warning(f'[일본대만] {TABLE} 테이블 없음 — sql/market_snapshot.sql을 먼저 실행하세요 (건너뜀)')
        return {'date': None, 'skipped': 'no_table'}
    p = compute(drop_unfinished(fetch_history()))
    dates = [d for d in p['asof'].values() if d]
    if not dates or p['valid'] < len(TICKERS) * 0.5:
        raise RuntimeError(f"유효 종목이 {p['valid']}/{len(TICKERS)}뿐입니다(야후 수신 확인)")
    base = max(dates)
    if not dry:
        sb.table(TABLE).upsert({'kind': KIND, 'base_date': base, 'payload': p,
                                'updated_at': datetime.now(timezone.utc).isoformat()}, on_conflict='kind,base_date').execute()
    top = [f"{t['theme']} {t['score']:+.2f}{'(' + t['signal'] + ')' if t['signal'] not in (None, 'NEUTRAL') else ''}"
           for t in p['themes'] if t['complete']][:3]
    log.info(f"[일본대만] {'dry-run ' if dry else ''}{base} 일본 {p['asof']['Japan']} · 대만 {p['asof']['Taiwan']} · "
             f"유효 {p['valid']}/{p['total']} · 상위 {' / '.join(top)}")
    return {'date': base, 'valid': p['valid'], 'total': p['total'], 'payload': p}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='쓰지 않고 계산만')
    ap.add_argument('--dump', help='계산 결과를 이 경로에 JSON으로 저장')
    a = ap.parse_args()
    r = run(dry=a.dry)
    if a.dump and r.get('payload'):
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(r['payload'], f, ensure_ascii=False)
    print({k: v for k, v in r.items() if k != 'payload'})
