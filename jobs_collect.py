"""
jobs_collect.py — 데이터 수집·집계 잡
─────────────────────────────────────
run_all.py 물리 분할 (2026-07): 재무·시장·수급·신고가·공매도·매크로·요약 생성 등
스케줄 수집 잡 본문. 공통 인프라는 job_infra.py.
"""
import os
import json
import time
import logging
import datetime

import stock_api
from managers import market_timer
from db_utils import fetch_all_pages
from disclosure_store import store_daily_disclosures
from format_utils import fmt_change_pct
from telegram_utils import get_admin_chat_id as _get_admin_chat_id
from config import DEFAULT_CHAT_ID, CHAT_IDS_BY_CODE
from job_infra import _job, _is_enabled, _log_notice, _send_logged, _bridge, _BRIDGE_OK, mark_failed, get_missing_jobs, get_active_jobs, is_dart_maintenance

# ✅ 재무/시장 데이터 수집 + 상장사 동기화
try:
    from collect_financials        import run as run_financials, auto_detect_quarter, fin_report_rows, report_quarter
    # run_financials(year, quarter, monitored_only=True) 형태로 호출 가능
    from collect_market            import run as run_market
    from collect_listed_companies  import run as run_sync_companies
    from grade                     import save_grade_history as _save_grade_history, save_trend_flags as _save_trend_flags
    from collect_insider           import collect_insider_trades as _collect_insider
    from collect_company_info      import collect_one as _collect_company_one
    from collect_short             import run as run_short, check_surge as _check_short_surge, format_surge_msg as _format_surge_msg
    _COLLECTOR_OK = True
except ImportError as _ie:
    _COLLECTOR_OK = False
    run_financials = auto_detect_quarter = fin_report_rows = report_quarter = run_market = run_sync_companies = None
    _save_grade_history = _save_trend_flags = _collect_insider = None
    _collect_company_one = run_short = _check_short_surge = _format_surge_msg = None
    logging.warning(f"수집 모듈 없음 (무시): {_ie}")


@_job()
def job_sync_listed_companies():
    """토요일 새벽 1시 — 코스피+코스닥 전체 상장사 동기화 (신규/사명변경/상폐)"""
    if not _COLLECTOR_OK:
        logging.warning("collect_listed_companies.py 없음 — 스킵")
        return
    try:
        logging.info("🏢 [상장사동기화] 시작")
        res = run_sync_companies() or {}
        logging.info("🏢 [상장사동기화] 완료")
        # 상폐는 되돌릴 일이 생길 수 있어 종목명을 알림에 남긴다(로그만 보면 놓친다)
        gone = res.get("deactivated") or []
        msg = f"[상장사동기화] 완료 — 신규 {res.get('inserted', 0)} · 갱신 {res.get('updated', 0)}"
        if gone:
            names = ", ".join(f"{n}({c})" for n, c in gone[:10])
            msg += f" · 상폐 {len(gone)}종목 비활성화: {names}"
            if len(gone) > 10:
                msg += f" 외 {len(gone) - 10}종목"
        _log_notice("system", msg)
    except Exception as e:
        if is_dart_maintenance(e):
            # 토요일 새벽 DART 점검이 잦다(08-29·10-10). 그대로 두면 상장·상폐 반영이 한 주 밀려
            # 일요일 01:00 job_sync_listed_catchup이 한 번 더 돌린다.
            nxt = "일요일 01:00 재시도" if datetime.date.today().weekday() == 5 else "다음 토요일 재시도"
            e = f"DART 점검(800) — corp_code 목록 못 받음, {nxt} ({e})"
        logging.error(f"❌ [상장사동기화] 오류: {e}")
        mark_failed(e)


@_job()
def job_sync_listed_catchup():
    """일요일 01:00 — 토요일 상장사 동기화가 실패했거나 안 돌았을 때만 다시 돌린다.
    (토요일 새벽 DART 점검으로 corp_code를 못 받는 일이 있다. 동기화는 upsert라 다시 돌려도 안전.)"""
    if not _BRIDGE_OK:
        return
    sat = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    try:
        rows = (_bridge._get_client().table('job_runs').select('ok,finished_at')
                .eq('run_date', sat).eq('job_name', 'job_sync_listed_companies')
                .order('finished_at').execute().data or [])
    except Exception as e:
        logging.warning(f"[상장사동기화-보충] job_runs 조회 불가 — 스킵: {str(e)[:120]}")
        mark_failed(e)
        return
    if rows and rows[-1]['ok']:
        logging.info("🏢 [상장사동기화-보충] 토요일 동기화 정상 — 스킵")
        return
    logging.info(f"🏢 [상장사동기화-보충] 토요일 동기화 {'실패' if rows else '기록 없음'} → 다시 실행")
    job_sync_listed_companies()


@_job()
def job_cleanup_market_data():
    """토요일 새벽 — market_data 정리 (모니터링·비모니터링 모두 120일 보존)

    3달 수익률에는 64거래일 이력이 필요하다. 달력 90일은 주말·공휴일을 빼면
    62거래일뿐이라 **모자란다**(실측: 90일 보존 직후 quarter 계산 불가로 떨어짐).
    여유를 둬 120일(약 83거래일)로 잡는다 — 연휴가 긴 해에도 64를 넘긴다.
    용량 추정(2026-09): market_data 38MB → 약 150MB, DB 총계 132MB → 275MB(500MB 중 55%).
    ⚠️ 대량 DELETE 한 문장으로 처리하면 statement timeout(57014) 발생 →
       오래된 날짜부터 WIN_DAYS 크기 창으로 나눠 삭제한다.
    """
    KEEP_MON = 120   # 모니터링 종목 보존일 (구 90 — 3달 수익률 여유분)
    KEEP_ALL = 120   # 비모니터링 보존일 (구 28)
    WIN_DAYS = 14    # 삭제 배치 날짜 창 (statement timeout 회피)
    try:
        sb = _bridge._get_client() if _BRIDGE_OK else None
        if not sb:
            logging.error("❌ [정리] Supabase 연결 없음 — 스킵")
            return

        today      = datetime.date.today()
        cutoff_all = (today - datetime.timedelta(days=KEEP_ALL)).isoformat()
        cutoff_mon = (today - datetime.timedelta(days=KEEP_MON)).isoformat()

        # 모니터링 종목 코드 목록
        _mon = sb.table('companies').select('code').eq('is_monitored', True).execute()
        mon_codes = [r['code'].split('.')[0] for r in (_mon.data or [])]

        def _windowed_delete(cutoff_iso, apply_filter):
            """base_date < cutoff 행을 오래된 날짜부터 WIN_DAYS 창으로 나눠 삭제.
            한 DELETE가 최대 WIN_DAYS치 데이터만 건드려 statement timeout을 피한다.
            returning='minimal' 로 삭제행 반환(대량 직렬화) 비용도 제거."""
            r0 = (sb.table('market_data').select('base_date')
                    .lt('base_date', cutoff_iso).order('base_date').limit(1).execute())
            if not r0.data:
                return 0
            cur    = datetime.date.fromisoformat(r0.data[0]['base_date'])
            cutoff = datetime.date.fromisoformat(cutoff_iso)
            total  = 0
            guard  = 0
            while cur < cutoff and guard < 2000:
                guard  += 1
                win_end = min(cur + datetime.timedelta(days=WIN_DAYS), cutoff)
                try:
                    q = (sb.table('market_data').delete(count='exact', returning='minimal')
                           .gte('base_date', cur.isoformat())
                           .lt('base_date', win_end.isoformat()))
                    res = apply_filter(q).execute()
                    total += (res.count or 0)
                except Exception as we:
                    logging.warning(f"⚠️ [정리] 창 {cur}~{win_end} 삭제 실패: {we}")
                cur = win_end
            return total

        # 보존일이 같으면 종목 구분 없이 한 번에 — 같은 창을 두 번 훑을 이유가 없다
        if KEEP_ALL == KEEP_MON or not mon_codes:
            n1 = _windowed_delete(cutoff_all, lambda q: q)
            n2 = 0
        else:
            # 1) 비모니터링 — 짧은 보존
            n1 = _windowed_delete(cutoff_all, lambda q: q.not_.in_('stock_code', mon_codes))
            # 2) 모니터링 — 긴 보존
            n2 = _windowed_delete(cutoff_mon, lambda q: q.in_('stock_code', mon_codes))

        logging.info(f"🗑️ [정리] market_data 정리 완료 — 비모니터링 {n1}행 / 모니터링 {n2}행 삭제 (보존 {KEEP_MON}일/{KEEP_ALL}일)")
        _log_notice("system", f"[정리] market_data — 비모니터링 {n1}행 / 모니터링 {n2}행 삭제")
    except Exception as e:
        logging.error(f"❌ [정리] market_data 정리 오류: {e}")
        mark_failed(e)


def bulk_report_summary(row: dict) -> dict:
    """대량보유 보고 한 건의 요약 — insider_trades bulk 행 → 배지용 dict(보고자·증감·비율 전후)."""
    out = {'type': 'bulk', 'change': row.get('shares_change') or 0,
           'ratio_before': row.get('hold_ratio_before'), 'ratio_after': row.get('hold_ratio_after')}
    if row.get('reporter'):
        out['who'] = row['reporter']
    return out


def insider_report_summary(rows: list) -> dict:
    """지분공시(임원·주요주주 소유보고) 한 건의 매매 요약 — insider_trades 행(같은 rcept_no) → 배지용 dict.
    scope='report' = 보고서 한 건 기준(프론트가 행마다 더해도 됨). who = 보고자가 한 명이면 이름."""
    buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
    sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
    who  = {r.get('reporter') for r in rows if r.get('reporter')}
    out = {'type': 'insider', 'scope': 'report', 'buy': buy, 'sell': sell, 'count': len(rows)}
    if len(who) == 1:
        out['who'] = next(iter(who))
    return out


def _preprocess_disclosures(all_disc_records: list, sb) -> list:
    """
    app_config 저장 전 전처리:
      1. 노이즈 공시 제거 (NOISE_KEYWORDS)
      2. 비상장 제거 (stock_code 없는 것) — companies 테이블 조회
      3. 시총 1000억 미만 제거 — market_data 조회 (market_cap 필드도 첨부)
      4. 카테고리 분류 — category 필드 추가
      5. insider 요약 — insider_summary 필드 추가 (rcept_no 기준)
    반환: 전처리된 레코드 리스트 (각 항목에 category, market_cap, insider_summary 포함)
    """

    # ── 1. 노이즈 제거 ──────────────────────────────────────────────
    NOISE_KEYWORDS = [
        '효력발생안내', '투자설명서(일괄신고)', '일괄신고추가서류',
        '일괄신고서(', '증권발행실적보고서', '[발행조건확정]',
        '해산사유발생', '의결권대리행사권유',
        '동일인등출자계열회사와의상품ㆍ용역거래',
        '정정신고서제출요구', '소속부변경', '소액공모공시서류', '집합투자증권',
    ]
    records = [
        d for d in all_disc_records
        if not any(kw in (d.get('report_nm') or '') for kw in NOISE_KEYWORDS)
    ]
    logging.info(f"📋 [전처리] 노이즈 제거: {len(all_disc_records)}→{len(records)}건")

    # ── 2 & 3. companies + market_data 조회 (상장 여부 + 시총) ─────
    corp_codes = list({d['corp_code'] for d in records if d.get('corp_code')})
    listed_set = set()    # 상장 corp_code
    cap_map    = {}       # corp_code → market_cap

    try:
        # 2+3. 상장 여부 + corp_code→stock_code 매핑 — 동일 쿼리 2회 반복하던 것을 1회로 통합
        chunk = 500
        stock_map = {}   # corp_code → stock_code
        for i in range(0, len(corp_codes), chunk):
            res = sb.table('companies').select('corp_code,code') \
                    .in_('corp_code', corp_codes[i:i+chunk]).execute()
            for c in (res.data or []):
                listed_set.add(c['corp_code'])
                sc = (c.get('code') or '').replace('.KS', '').replace('.KQ', '')
                if sc:
                    stock_map[c['corp_code']] = sc

        stock_codes = list(set(stock_map.values()))
        if stock_codes:
            date_res = sb.table('market_data').select('base_date') \
                         .order('base_date', desc=True).limit(1).execute()
            max_date = (date_res.data or [{}])[0].get('base_date')
            if max_date:
                mkt_map = {}
                for i in range(0, len(stock_codes), chunk):
                    mkt_res = sb.table('market_data').select('stock_code,market_cap') \
                                .eq('base_date', max_date) \
                                .in_('stock_code', stock_codes[i:i+chunk]).execute()
                    mkt_map.update({m['stock_code']: m['market_cap'] for m in (mkt_res.data or [])})
                for cc, sc in stock_map.items():
                    if sc in mkt_map:
                        cap_map[cc] = mkt_map[sc]

        logging.info(f"📋 [전처리] 상장사 {len(listed_set)}개 / 시총 {len(cap_map)}개 조회 완료")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] companies/market_data 조회 실패 (필터 스킵): {e}")

    CAP_THRESHOLD = 100_000_000_000  # 1000억

    filtered = []
    for d in records:
        cc = d.get('corp_code')
        # 비상장 제거 (listed_set에 없으면 제외 — 단, listed_set이 비어있으면 전부 통과)
        if listed_set and cc and cc not in listed_set:
            continue
        # 시총 1000억 미만 제거 (market_cap 정보가 있는 경우만)
        cap = cap_map.get(cc)
        if cap is not None and cap < CAP_THRESHOLD:
            continue
        filtered.append(d)
    logging.info(f"📋 [전처리] 상장/시총 필터: {len(records)}→{len(filtered)}건")

    # ── 4. 카테고리 분류 ───────────────────────────────────────────
    CATEGORIES = [
        ('사업보고서',    ['사업보고서']),
        ('반기보고서',    ['반기보고서']),
        ('분기보고서',    ['분기보고서']),
        ('기업설명회(IR)',['기업설명회', 'IR개최', 'NDR']),
        ('잠정실적',      ['잠정', '결산실적', '실적(공정공시)']),
        ('주요사항',      ['주요사항보고']),
        ('증자/감자',     ['유상증자', '무상증자', '감자']),
        ('합병/분할',     ['합병', '분할', '영업양수', '영업양도']),
        ('사채/전환',     ['전환사채', '신주인수권', '교환사채', '사채권']),
        ('자사주',        ['자기주식']),
        ('배당',          ['배당']),
        ('최대주주변동',  ['최대주주등소유주식변동', '최대주주변동']),
        ('대량보유',      ['대량보유상황보고서', '대량보유']),
        ('거래계획(예고)',['거래계획보고서']),
        ('거래계획(철회)',['거래계획철회보고서']),
        ('지분공시',      ['소유상황보고서', '임원ㆍ주요주주', '임원·주요주주']),
        ('임원/주식',     ['임원', '주요주주']),
        ('감사보고서',    ['감사보고서', '내부회계']),
        ('공정공시',      ['공정공시']),
        ('주식매수선택권',['주식매수선택권']),
        ('주요경영사항',  [
            '투자판단관련주요경영사항', '단일판매ㆍ공급계약', '단일판매·공급계약',
            '소송등의판결', '타인에대한채무보증', '특수관계인으로부터자금차입',
            '특수관계인에대한자금대여', '특수관계인에대한출자', '특수관계인으로부터자산양수',
            '특수관계인의유상증자참여', '금전대여결정', '단기차입금증가',
            '기업가치제고계획', '대표이사변경', '사외이사의선임',
            '증권발행결과', '주식병합결정', '감자완료',
        ]),
        ('증권신고',      ['증권신고서', '투자설명서']),
    ]

    def _classify(report_nm: str) -> str:
        for label, keywords in CATEGORIES:
            if any(kw in report_nm for kw in keywords):
                return label
        return '기타'

    for d in filtered:
        d['category'] = _classify(d.get('report_nm') or '')

    # ── 5. insider 요약 첨부 (insider_trades DB에서 오늘 데이터 조회) ──
    try:
        today_str = datetime.date.today().isoformat()
        # 페이지로 다 받음 — 소유보고가 몰리는 날(삼성전자 07-21 하루 761건)은 1,000행을 넘어 뒤쪽 보고서 요약이 빠졌다
        ins_rows = fetch_all_pages(
            sb.table('insider_trades')
              .select('rcept_no,corp_code,reporter,shares_change,hold_ratio_before,hold_ratio_after,plan_period,report_type')
              .eq('base_date', today_str).order('id')
        )

        ins_map = {}  # rcept_no → [rows]
        for r in ins_rows:
            if r.get('rcept_no'):
                ins_map.setdefault(r['rcept_no'], []).append(r)

        for d in filtered:
            rno = d.get('rcept_no')
            cc  = d.get('corp_code')
            cat = d.get('category', '')
            summary = None

            if cat == '지분공시' and rno and rno in ins_map:
                # 그 보고서(rcept_no)의 매매만 — 예전엔 회사의 그날 전체(corp_ins_map) 합계를 그날 지분공시 행마다
                # 똑같이 붙여, 목록 배지가 행마다 같은 큰 값으로 보이고 합치면 몇백 배가 됐다(삼성전자 07-21 761건). 2026-10-18
                summary = insider_report_summary(ins_map[rno])
            elif cat == '대량보유' and rno and rno in ins_map:
                summary = bulk_report_summary(ins_map[rno][0])
            elif cat == '최대주주변동' and rno and rno in ins_map:
                summary = {'type': 'major'}
            elif cat == '거래계획(예고)' and rno and rno in ins_map:
                rows = ins_map[rno]
                buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
                sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
                periods = list({r['plan_period'] for r in rows if r.get('plan_period')})
                summary = {'type': 'plan', 'buy': buy, 'sell': sell,
                           'period': periods[0] if periods else None}
            if summary:
                d['insider_summary'] = summary

        logging.info(f"📋 [전처리] insider 요약 첨부 완료 ({len(ins_rows)}건 참조)")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] insider 요약 첨부 실패 (스킵): {e}")

    return filtered


@_job()
def job_collect_financials():
    """평일 18:30 — 오늘 DART 공시된 종목만 재무 수집"""
    if not _COLLECTOR_OK:
        logging.warning("collect_financials.py 없음 — 수집 스킵")
        return
    if not _is_enabled('collect_financials'):
        logging.info("⏸ 재무수집 비활성화 (DB 설정)")
        return
    try:
        import OpenDartReader
        dart   = OpenDartReader(os.environ.get('DART_API_KEY',''))
        today  = datetime.date.today().strftime('%Y%m%d')
        today_display = datetime.date.today().strftime('%Y-%m-%d')  # 가독성용
        year, quarter = auto_detect_quarter()

        # ── 오늘 전체 공시 조회 (final=False → 임원변동, 주요사항 등 모두 포함) ──
        disc_all  = dart.list(start=today, end=today, final=False)

        # ── 재무수집용: 정기보고서 최종본만 (final=True → 정정 전 원본 중복 방지) ──
        disc_fin  = dart.list(start=today, end=today, final=True)

        # 전체 공시 목록 구성 (disc_all 기준)
        all_disc_records = []
        seen_rcept = set()
        if disc_all is not None and not disc_all.empty:
            for _, row in disc_all.iterrows():
                rno = row.get('rcept_no', '')
                if rno and rno in seen_rcept:
                    continue   # 같은 접수번호는 한 번만(100건씩 나눠 받는 목록이라 겹칠 수 있음 — 저장 쪽도 한 번 더 거름)
                seen_rcept.add(rno)
                all_disc_records.append({
                    'corp_name': row.get('corp_name', ''),
                    'report_nm': row.get('report_nm', ''),
                    'rcept_dt':  row.get('rcept_dt',  ''),
                    'corp_code': row.get('corp_code', ''),
                    'rcept_no':  row.get('rcept_no',  ''),
                })

        # disc_fin의 정기보고서 중 disc_all에 없는 것 병합
        # (final=False에서 누락되는 경우 보완)
        target_types = ['분기보고서', '반기보고서', '사업보고서']
        if disc_fin is not None and not disc_fin.empty:
            fin_only = disc_fin[disc_fin['report_nm'].str.contains('|'.join(target_types), na=False)]
            for _, row in fin_only.iterrows():
                rno = row.get('rcept_no', '')
                if rno and rno not in seen_rcept:
                    all_disc_records.append({
                        'corp_name': row.get('corp_name', ''),
                        'report_nm': row.get('report_nm', ''),
                        'rcept_dt':  row.get('rcept_dt',  ''),
                        'corp_code': row.get('corp_code', ''),
                        'rcept_no':  rno,
                    })
                    seen_rcept.add(rno)

        # ── 분기/반기/사업보고서만 필터 (재무수집 + 실적공시 카드용) ──
        # 보고서 이름 → 실제 분기는 report_quarter('사업보고서 (2025.12)' → ('2025', 'Q4')) — 07:30 재무 보충과 같은 규칙
        today_corps    = []
        quarter_groups = {}  # {(year, quarter): [corp_code]} — 정기보고서 없는 날 NameError 방지
        _lists = [x for x in (disc_all, disc_fin) if x is not None and not x.empty]
        if _lists:
            # 원본 정기보고서만(정정 제외). 최종본(final=True)만 보면 같은 날 정정이 올라온 회사는 정정본만 남아
            # 통째로 빠진다(2026-11-16 종근당 등 7곳) → 전체 목록(final=False)의 원본도 함께 본다.
            filtered = fin_report_rows(*_lists)
            if not filtered.empty:
                # 종목별로 report_nm에서 실제 분기 파싱 → 분기별 그룹핑
                from collect_financials import run_by_corp_codes
                quarter_groups = {}  # {(year, quarter): [corp_code, ...]}
                seen = {}

                for _, row in filtered.iterrows():
                    code  = row['corp_code']
                    nm    = row.get('report_nm', '')
                    is_amended = '정정' in nm or '기재정정' in nm
                    y, q  = report_quarter(nm)

                    # 동일 corp_code 중복 제거 (정정 우선)
                    if code not in seen or (is_amended and not seen[code]['is_amended']):
                        seen[code] = {
                            'corp_code':  code,
                            'corp_name':  row.get('corp_name', ''),
                            'report_nm':  nm,
                            'is_amended': is_amended,
                        }
                        if y and q:
                            key = (y, q)
                            quarter_groups.setdefault(key, [])
                            if code not in quarter_groups[key]:
                                quarter_groups[key].append(code)

                today_corps = list(seen.values())

                # ── KOSPI/KOSDAQ 상장사만 필터 ──────────────────────────
                # DART corp_cls: Y=유가증권(KOSPI), K=코스닥, E=코넥스, N=기타(외감법인/펀드 등)
                # disc_fin에 corp_cls 컬럼이 있으면 직접 필터
                if 'corp_cls' in filtered.columns:
                    listed_codes = set(
                        filtered[filtered['corp_cls'].isin(['Y', 'K'])]['corp_code'].tolist()
                    )
                    today_corps = [c for c in today_corps if c['corp_code'] in listed_codes]
                    logging.info(f"📊 [재무수집] 상장사 필터 적용: {len(seen)}개 → {len(today_corps)}개")
                else:
                    # corp_cls 없으면 companies DB에서 확인
                    if _BRIDGE_OK:
                        try:
                            _sb_tmp = _bridge._get_client()
                            all_corp_codes = [c['corp_code'] for c in today_corps if c.get('corp_code')]
                            if all_corp_codes:
                                chunk = 500
                                listed_set = set()
                                for i in range(0, len(all_corp_codes), chunk):
                                    _res = _sb_tmp.table('companies').select('corp_code') \
                                                  .in_('corp_code', all_corp_codes[i:i+chunk]).execute()
                                    for r in (_res.data or []):
                                        listed_set.add(r['corp_code'])
                                today_corps = [c for c in today_corps if c['corp_code'] in listed_set]
                                logging.info(f"📊 [재무수집] 상장사 필터(DB): {len(seen)}개 → {len(today_corps)}개")
                        except Exception as _fe:
                            logging.warning(f"⚠️ [재무수집] 상장사 필터 실패 (전체 포함): {_fe}")

                # 스팩(SPAC) 제외 — 표준 재무 없음
                _spac = {c['corp_code'] for c in seen.values() if '스팩' in (c.get('corp_name') or '')}
                if _spac:
                    logging.info(f"📊 [재무수집] 스팩 제외: {len(_spac)}개")
                # 상장사 필터·스팩 제외를 실제 수집 목록(quarter_groups)에 적용.
                # 예전엔 상장사 필터가 today_corps에만 걸려 비상장 법인(corp_cls E/N)까지 수집돼
                # stock_code ''로 저장됐다(10-12 고아 229행).
                _keep = {c['corp_code'] for c in today_corps} - _spac
                for _qk in list(quarter_groups.keys()):
                    quarter_groups[_qk] = [cc for cc in quarter_groups[_qk] if cc in _keep]
                    if not quarter_groups[_qk]:
                        del quarter_groups[_qk]
                # 분기별로 나눠서 수집
                total_ok, total_fail = 0, 0
                for (y, q), codes in quarter_groups.items():
                    logging.info(f"📊 [재무수집] {y} {q} — {len(codes)}개 종목 수집 시작")
                    ok, fail = run_by_corp_codes(codes, y, q)
                    total_ok += ok; total_fail += fail
                    logging.info(f"📊 [재무수집] {y} {q} 완료: 성공 {ok}개, 실패 {fail}개")

                logging.info(f"📊 [재무수집] 전체 완료: 성공 {total_ok}개, 실패 {total_fail}개")
                _log_notice("system", f"[재무수집] 오늘 공시 {len(today_corps)}개 → {total_ok}개 완료")
            else:
                logging.info("📊 [재무수집] 오늘 재무 공시 없음 — 재무수집 스킵, 공시목록만 저장")
        else:
            logging.info(f"📊 [재무수집] 오늘({today}) 정기보고서 공시 없음 — 공시목록만 저장")

        # ── 지분공시 파싱 (임원 매수/매도) — app_config 저장 전에 실행 ──
        if all_disc_records and _COLLECTOR_OK and _BRIDGE_OK:
            try:
                dart_key = os.environ.get('DART_API_KEY', '')
                sb2 = _bridge._get_client()
                _collect_insider(all_disc_records, dart_key, sb2)
            except Exception as _ie:
                logging.error(f"❌ [지분공시] 수집 오류: {_ie}")

        # ── 공시 저장 (항상 실행 — 날짜 기준으로 갱신) ──────────────
        if _BRIDGE_OK:
            sb = _bridge._get_client()

            # 전체 공시: 저장 전 전처리 (노이즈 제거 + 상장 필터 + 시총 + 카테고리 + insider 요약)
            # insider_trades는 이미 위에서 _collect_insider로 저장 완료된 상태이므로
            # _preprocess_disclosures가 insider_trades DB를 바로 읽어 요약 첨부 가능
            processed_disclosures = _preprocess_disclosures(all_disc_records, sb)

            # ── 1. 실적 공시 종목 → app_config (소량 JSON, 유지) ──
            sb.table('app_config').upsert({
                'key':         'today_earnings_corps',
                'value':       json.dumps(today_corps, ensure_ascii=False),
                'description': f'{today_display} 실적 공시 종목 목록'
            }, on_conflict='key').execute()

            # ── 2. 전체 공시 → daily_disclosures 테이블 (app_config JSON 탈출) ──
            _today_str = datetime.date.today().isoformat()

            # 행 구성을 먼저 끝낸 뒤 삭제→삽입 — 프론트에 빈 데이터가 노출되는 창 최소화
            rows_to_insert = [{
                'base_date':       _today_str,
                'corp_code':       d.get('corp_code') or '',
                'corp_name':       d.get('corp_name') or '',
                'report_nm':       d.get('report_nm') or '',
                'rcept_no':        d.get('rcept_no') or None,
                'rcept_dt':        d.get('rcept_dt') or None,
                'category':        d.get('category') or '기타',
                'market_cap':      d.get('market_cap') or None,
                'insider_summary': d.get('insider_summary') or None,
            } for d in processed_disclosures]

            # 공시 0건이면 삭제 스킵 — DART 빈 응답(장애/수동 트리거) 시
            # delete→insert가 그날 저장분을 통째로 지우는 것 방지
            _stored = ''
            if not rows_to_insert:
                logging.warning("⚠️ [공시저장] 오늘 공시 0건 — 기존 daily_disclosures 보존(삭제 스킵)")
            else:
                # 삭제 → 100건 묶음 삽입 → 표를 다시 읽어 빠진 것만 다시 넣기(묶음 → 한 건씩).
                # 예전엔 오류 난 묶음을 그대로 다시 넣어, 응답만 잃고 저장된 묶음이 중복 키로 실패해 '유실'로 적혔다(11-13).
                _res = store_daily_disclosures(sb, _today_str, rows_to_insert)
                _stored = f" (저장 {_res['saved']}/{_res['want']}건{'' if _res['verified'] else ', 확인 조회 실패'})"

            logging.info(
                f"📊 [공시저장] 실적공시 {len(today_corps)}건 / "
                f"전체공시 {len(all_disc_records)}→{len(processed_disclosures)}건{_stored} "
                f"daily_disclosures 저장 완료"
            )

        # 재무수집 완료 후 등급 이력 + 추세 신호 저장
        if today_corps and quarter_groups:
            for (y, q) in quarter_groups.keys():
                job_save_grade_history(y, q)
                job_save_trend_flags(y, q)

    except Exception as e:
        if is_dart_maintenance(e):
            # 지금까지 DART 점검(800)은 전부 주말·공휴일에 왔고(08-29~10-10, 6건), 그런 날은
            # 공시 자체가 없다(daily_disclosures 0건). 잃은 게 없으니 실패로 올리지 않는다.
            # 영업일 점검은 실제로 그날 재무를 놓친 것이라 실패로 남긴다.
            if market_timer.is_kr_holiday():
                logging.warning("⏸ [재무수집] DART 점검(800) — 비영업일이라 공시 없음, 스킵")
                return
            e = f"DART 점검(800) — 오늘 공시 재무 미수집 ({e})"
        logging.error(f"❌ [재무수집] 오류: {e}")
        mark_failed(e)


@_job()
def job_backfill_financials():
    """실적시즌 주말 — 당일공시 잡이 놓친 재무를 전수 백필(스킵-기존, 스팩 제외)."""
    if not _is_enabled('collect_financials'):
        logging.info("⏸ 재무백필 비활성화 (DB 설정)")
        return
    import datetime as _dt
    m = _dt.date.today().month
    if m not in (3, 4, 5, 8, 11):
        logging.info(f"📊 [재무백필] 실적시즌 아님(월={m}) — 스킵")
        return
    try:
        from backfill_financials import run_backfill
        y = _dt.date.today().year - 1  # 연초(사업보고서 시즌)엔 전년도가 대상 → 최근 2년 커버
        logging.info(f"📊 [재무백필] {y}~ 누락 재무 전수 백필 시작")
        run_backfill(from_year=y)
        if m == 11:
            # 중단영업 재분류로 비워 둔 전년도 4분기 손익 — 올해 3분기보고서의 재작성 누적으로 다시 계산
            from backfill_financials import refix_q4_basis
            refix_q4_basis(y)
        logging.info("📊 [재무백필] 완료")
    except Exception as e:
        logging.error(f"❌ [재무백필] 실패: {e}")
        mark_failed(e)


@_job(key='collect_financials')
def job_financials_catchup():
    """매일 07:30 — 늦게 뜬 재무 보충(collect_financials.run_catchup). 최근 7일(어제까지) 정기보고서 중 financials에
    그 분기가 아직 없는 상장사만 다시 받고, 채운 분기는 18:30 재무수집처럼 등급 이력·추세 신호를 다시 계산한다.
    오후 늦게 낸 보고서는 그날 저녁 DART 재무 API에 아직 없어 18:30 수집이 놓친다(2026-11-12~16 3분기 139곳).
    실패해도 다음 날 같은 7일을 다시 보므로 19:25 재처리 목록엔 넣지 않는다(저녁 재무수집과 겹치지 않게).
    07:40 실적 국면 보정은 이 잡이 끝난 뒤 돈다(_after)."""
    if not _COLLECTOR_OK:
        logging.warning("collect_financials.py 없음 — 재무 보충 스킵")
        return
    try:
        from collect_financials import run_catchup
        res = run_catchup()
        qs = res['quarters']
        ok = sum(v[0] for v in qs.values())
        fail = sum(v[1] for v in qs.values())
        filled = [k for k, v in qs.items() if v[0] > 0]
        for (y, q) in filled:
            job_save_grade_history(y, q)
            job_save_trend_flags(y, q)
        logging.info(f"=== [재무 보충] 완료: 후보 {res['reports']}곳 · 그 분기 재무 없음 {res['missing']}곳 → "
                     f"채움 {ok} · DART 재무 아직 없음 {res['tried'] - ok - fail} · 실패 {fail} ===")
        if ok:
            _log_notice("system", f"[재무 보충] 늦게 뜬 재무 {ok}곳 채움 ({', '.join(f'{y} {q}' for y, q in filled)})")
    except Exception as e:
        if is_dart_maintenance(e):
            logging.warning(f"⏸ [재무 보충] DART 점검(800) — 다음 날 같은 7일을 다시 봄: {e}")
            return
        logging.error(f"❌ [재무 보충] 오류: {e}")
        mark_failed(e)


def job_save_grade_history(year: str = None, quarter: str = None):
    """재무수집 완료 후 — 등급 계산 & earnings_grade_history 저장 + 등급 변경 알림"""
    if not _BRIDGE_OK:
        return
    try:
        if not year or not quarter:
            year, quarter = auto_detect_quarter()
        sb = _bridge._get_client()
        result = _save_grade_history(sb, year, quarter)

        # ── 등급 변경 알림 발송 ──────────────────────────────────────
        new_list  = result.get('new',  [])
        up_list   = result.get('up',   [])
        down_list = result.get('down', [])

        if not (new_list or up_list or down_list):
            return

        GRADE_EMOJI = {'S': '🏆', 'A': '🥇', 'B': '🥈', '관찰': '⚡'}

        def fmt_row(r, change_label):
            g  = r['grade']
            em = GRADE_EMOJI.get(g, '')
            rev_str = ''
            if r.get('rev_yoy') is not None:
                rev_str = f" | YoY {fmt_change_pct(r['rev_yoy'], 1)}"
            op_str = ''
            if r.get('op_yoy') is not None:
                op_str = f" / 영업익 {fmt_change_pct(r['op_yoy'], 1)}"
            return f"{em} <b>{r['corp_name']}</b> {g}급 {change_label}{rev_str}{op_str}"

        sections = []

        if new_list:
            lines = [fmt_row(r, '신규진입') for r in new_list[:15]]
            if len(new_list) > 15:
                lines.append(f"... 외 {len(new_list) - 15}개")
            sections.append("🆕 <b>신규 진입</b>\n" + "\n".join(lines))

        if up_list:
            lines = [fmt_row(r, '↑') for r in up_list[:10]]
            if len(up_list) > 10:
                lines.append(f"... 외 {len(up_list) - 10}개")
            sections.append("📈 <b>등급 향상</b>\n" + "\n".join(lines))

        if down_list:
            lines = [fmt_row(r, '↓') for r in down_list[:10]]
            if len(down_list) > 10:
                lines.append(f"... 외 {len(down_list) - 10}개")
            sections.append("📉 <b>등급 하락</b>\n" + "\n".join(lines))

        msg = (
            f"📊 <b>[실적 등급 변동] {year} {quarter}</b>\n"
            f"════════════\n"
            + "\n\n".join(sections)
        )

        # 개인 채팅방(admin_chat_id)으로만 발송
        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)
        stock_api.send_telegram(target, msg)
        logging.info(
            f"📢 [등급알림] {year} {quarter} → {target} — "
            f"신규 {len(new_list)}개 / 향상 {len(up_list)}개 / 하락 {len(down_list)}개"
        )

    except Exception as e:
        logging.error(f"❌ [등급이력] 오류: {e}")
        mark_failed(e)


def job_save_trend_flags(year: str = None, quarter: str = None):
    """재무수집/등급이력 완료 후 — 재무 추세 신호 탐지 & financials.trend_flags 저장"""
    if not _BRIDGE_OK or not _COLLECTOR_OK:
        return
    try:
        if not year or not quarter:
            year, quarter = auto_detect_quarter()
        sb = _bridge._get_client()
        flagged = _save_trend_flags(sb, year, quarter)
        logging.info(f"📈 [추세신호] {year} {quarter} 완료 — 경고 {flagged}개")
    except Exception as e:
        logging.error(f"❌ [추세신호] 오류: {e}")
        mark_failed(e)


@_job()
def job_collect_macro():
    """글로벌 매크로 데이터 수집 (지수/환율/원자재) — 06:30 아침 수집 시 메인 채널 발송 포함"""
    if not _is_enabled('collect_macro'):
        return
    # 주말 스킵 — 단, 토요일 새벽(06시대)은 미국 금요일장 마감분 수집을 위해 허용.
    # 일요일·주말 저녁 실행은 토요일 값의 복제 행만 만들어 macro_data에
    # 비거래일 base_date를 쌓는다 (섹터요약 거래일 산정 오염의 원인이었음).
    _now = datetime.datetime.now()
    if _now.weekday() >= 5 and not (_now.weekday() == 5 and _now.hour == 6):
        logging.info("⏸ [매크로] 주말 스킵 (토 06시대 미국 마감분 수집만 허용)")
        return
    logging.info("=== 매크로 데이터 수집 시작 ===")
    try:
        import collect_macro
        data = collect_macro.collect_all()
        collect_macro.save_to_db(data)
        logging.info("=== 매크로 데이터 수집 완료 ===")

        # 06:xx 아침 수집에만 메인 채널 브리핑 발송 (16:10 저녁 수집 제외, 휴장일 제외)
        if datetime.datetime.now().hour == 6 and _is_enabled('macro_briefing') and not market_timer.is_us_holiday():
            try:
                _sectors, _sec_date = collect_macro.fetch_us_sectors()
            except Exception as _se:
                logging.warning(f"[매크로] 미국 섹터 조회 실패 — 섹터 섹션 생략: {_se}")
                _sectors, _sec_date = {}, None
            msg = stock_api.get_macro_briefing(data, sectors=_sectors, sector_date=_sec_date)
            if msg:
                _send_logged(DEFAULT_CHAT_ID, "매크로 브리핑", msg)
                logging.info("📊 [매크로 브리핑] 메인 채널 발송 완료")
    except Exception as e:
        logging.error(f"매크로 데이터 수집 실패: {e}")
        mark_failed(e)


# 증권사 리포트는 영업일에만 나온다. 휴장일 실행은 같은 30일 윈도를 재수집할 뿐이고,
# 다음 거래일에 어차피 같은 윈도를 다시 훑으므로 스킵해도 잃는 게 없다.
@_job(holiday=True)
def job_collect_analyst_opinions():
    """증권사 투자의견 수집 (하루 2회: 장전 + 장후)"""
    logging.info("📋 [투자의견] 증권사 투자의견 수집 시작")
    try:
        import collect_market
        saved = collect_market.collect_analyst_opinions()
        logging.info(f"📋 [투자의견] 완료: {saved}건 저장")
    except Exception as e:
        logging.error(f"❌ [투자의견] 수집 실패: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_foreign_institution():
    """기관/외국인 매매가집계 수집 (장중 4회 + 장마감 후)"""
    logging.info("=== 기관/외국인 수급 수집 시작 ===")
    try:
        import collect_market
        result = collect_market.collect_foreign_institution()
        logging.info(f"=== 수급 수집 완료: 외국인 {len(result['frgn_buy'])}개 / 기관 {len(result['orgn_buy'])}개 / 동시매수 {len(result['both_buy'])}개 ===")
    except Exception as e:
        logging.error(f"❌ 수급 수집 실패: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_new_high():
    """장 마감 후 신고가 종목 수집 + 알림 발송"""
    logging.info("=== 신고가 종목 수집 시작 ===")
    try:
        import collect_market
        rows = collect_market.collect_new_high()
        logging.info(f"=== 신고가 수집 완료: {len(rows)}개 ===")
        _alert_new_high(rows)
    except Exception as e:
        logging.error(f"❌ 신고가 수집 실패: {e}")
        mark_failed(e)


def _new_high_streak_badge(streak: int, capped: bool = False, long: bool = False) -> str:
    """52주 신고가 연속 경신 배지 텍스트.
    streak<=1 → 신규(오늘 처음 경신), >=2 → N일 연속. capped면 '+' 표시.
    long=True는 종목방 개별 알림용(경신 문구 포함), False는 메인방 한 줄용.
    """
    if not streak or streak <= 1:
        return "🆕 오늘 신규 경신" if long else "🆕 신규"
    plus = "+" if capped else ""
    return f"🔥 {streak}일{plus} 연속 경신" if long else f"🔥 {streak}일{plus} 연속"


def _alert_new_high(rows: list):
    """
    52주 신고가 갱신 알림 발송.
    ① 모니터링 종목 → 해당 종목 채팅방 개별 발송
    ② 모니터링 종목 중 신고가 갱신 전체 → 메인 채팅방 묶음 발송
    """
    if not rows:
        return

    from sector_classify import SECTOR_EMOJI

    today = datetime.date.today().isoformat()

    # 모니터링 종목 코드 → 채팅방 ID 매핑 (CHAT_IDS_BY_CODE: {code: chat_id})
    monitored = {}  # code → chat_id
    for code, chat_id in CHAT_IDS_BY_CODE.items():
        monitored[str(code).strip()] = chat_id

    # 메인방 섹터별 그룹핑용: sector_group → [(chg_pct, line), ...]
    by_sector: dict = {}

    for r in rows:
        code     = str(r.get('code', '')).strip()
        name     = r.get('name', code)
        chg_pct  = r.get('chg_pct', 0) or 0
        d52_high = r.get('d52_high', 0) or 0
        d52_low  = r.get('d52_low',  0) or 0
        streak   = int(r.get('streak', 1) or 1)
        capped   = bool(r.get('streak_capped', False))
        sector   = r.get('sector_group') or '기타'

        chg_str  = fmt_change_pct(chg_pct)
        chg_icon = '🔴' if chg_pct > 0 else ('🔵' if chg_pct < 0 else '➖')
        badge      = _new_high_streak_badge(streak, capped)             # 메인방 한 줄용
        badge_long = _new_high_streak_badge(streak, capped, long=True)  # 종목방 개별용

        # ① 종목 채팅방 개별 알림 (현재가는 신고가 당일 52주 고가와 동일해 생략)
        if code in monitored:
            sec_emoji = SECTOR_EMOJI.get(sector, '▫️')
            msg = (
                f"🏆 <b>[{name}] 52주 신고가 갱신!</b>\n"
                f"════════════\n"
                f"{chg_icon}{chg_str}  {badge_long}\n"
                f"🏷️ 섹터: {sec_emoji} {sector}\n"
                f"📈 52주 고가: {d52_high:,}원\n"
                f"📉 52주 저가: {d52_low:,}원"
            )
            stock_api.send_telegram(monitored[code], msg)

        # ② 메인방 섹터별 그룹핑용 라인 누적 (현재가 생략, 등락률 + 연속 배지)
        by_sector.setdefault(sector, []).append(
            (chg_pct, f"  • <b>{name}</b>  {chg_icon}{chg_str}  {badge}")
        )

    # ② 메인 채팅방 묶음 발송 — 섹터별 그룹핑
    if by_sector:
        # 섹터 정렬: 구성 종목 수 desc → 섹터 내 최고 등락률 desc
        ordered = sorted(
            by_sector.items(),
            key=lambda kv: (len(kv[1]), max((c for c, _ in kv[1]), default=0)),
            reverse=True,
        )
        blocks = []
        for sec, items in ordered:
            items.sort(key=lambda t: t[0], reverse=True)   # 섹터 내 등락률 desc
            sec_emoji = SECTOR_EMOJI.get(sec, '▫️')
            blocks.append(
                f"{sec_emoji} <b>{sec}</b> ({len(items)})\n"
                + "\n".join(line for _, line in items)
            )

        header = f"🏆 <b>[52주 신고가 갱신] {today}</b> · {len(rows)}종목\n════════════\n"
        # 섹터 블록 사이 빈 줄로 가독성 확보
        _send_logged(DEFAULT_CHAT_ID, "52주 신고가", header + "\n\n".join(blocks))   # 예전엔 기록 없음
        logging.info(f"🏆 [신고가 알림] 메인방 {len(rows)}개 / {len(ordered)}섹터 발송")


@_job("collect_us_etf")
def job_collect_us_etf():
    """미국 산업별 ETF 수집 → us_market 테이블"""
    logging.info("=== US ETF 수집 시작 ===")
    try:
        import collect_us_etf
        collect_us_etf.collect_and_save(days=5)
        logging.info("=== US ETF 수집 완료 ===")
    except Exception as e:
        logging.error(f"❌ US ETF 수집 실패: {e}")
        mark_failed(e)


@_job()
def job_collect_market():
    """평일 장중 — KIS 모니터링 종목(306개) 시장 데이터 수집"""
    if datetime.datetime.now().weekday() >= 5 or market_timer.is_kr_holiday():
        logging.info("⏸ [시장수집] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📈 [시장수집] 시작")
        ok, fail = run_market()
        logging.info(f"📈 [시장수집] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집] 완료 ({ok}개)")
    except Exception as e:
        logging.error(f"❌ [시장수집] 오류: {e}")
        mark_failed(e)


def _check_market_warnings():
    """
    모니터링 종목 중 관리종목·투자유의·시장경보 진입 종목 감지 → 개인 채팅방 알림.
    시장경보 코드: 00=정상, 01=주의, 02=경고, 03=위험
    """
    if not _BRIDGE_OK:
        return
    try:
        sb = _bridge._get_client()

        # 최근 2개 거래일 (주말/공휴일 무관 — market_data엔 거래일만 존재)
        dres = sb.table('market_data').select('base_date') \
                 .order('base_date', desc=True).limit(1).execute()
        latest = (dres.data or [{}])[0].get('base_date')
        if not latest:
            return
        pres = sb.table('market_data').select('base_date') \
                 .lt('base_date', latest).order('base_date', desc=True).limit(1).execute()
        prev = (pres.data or [{}])[0].get('base_date')

        # 최신 거래일 경보 종목 (전체시장 — PostgREST 1000행 한도 회피)
        alerts = fetch_all_pages(
            sb.table('market_data')
              .select('stock_code,corp_name,market_warn_code')
              .eq('base_date', latest)
              .neq('market_warn_code', '00')
        )
        if not alerts:
            return

        # 직전 거래일 경보 이력 (신규 진입만 알림)
        prev_warn = set()
        if prev:
            prev_rows = fetch_all_pages(
                sb.table('market_data')
                  .select('stock_code')
                  .eq('base_date', prev)
                  .neq('market_warn_code', '00')
            )
            prev_warn = {r['stock_code'] for r in prev_rows}

        # 신규 진입만 필터
        new_alerts = [a for a in alerts if a['stock_code'] not in prev_warn]
        if not new_alerts:
            return

        # 시장 전체 정보 → 메인채팅방 발송 (개인/관리자방 아님)
        target = DEFAULT_CHAT_ID

        # 그룹별 분류
        GROUPS = [
            ('03', '🆘 투자위험 종목 지정',   lambda a: a.get('market_warn_code') == '03'),
            ('02', '🚨 투자경고 종목 지정',   lambda a: a.get('market_warn_code') == '02'),
            ('01', '⚠️ 투자주의 종목 지정',   lambda a: a.get('market_warn_code') == '01'),
        ]
        # 지정 효력 안내 (거래소 시장경보제도 기준) — 헤더 밑 한 줄로 노출
        NOTES = {
            '03': "지정 당일 1일 매매거래정지 · 위탁증거금 100%·신용 금지 · 해제 시 투자경고로 하향",
            '02': "위탁증거금 100%(현금)·신용/미수 금지 · 추가 급등 시 1일 매매정지 가능 · 최소 10거래일 유지",
            '01': "당일 1일만 효력 → 다음 거래일 자동 해제 · 매매 제약 없는 주의 환기용",
        }

        sections = []
        for code, label, fn in GROUPS:
            group = [a for a in new_alerts if fn(a)]
            if not group:
                continue
            names = " · ".join(a['corp_name'] for a in group)
            head = f"<b>{label} ({len(group)})</b>"
            note = NOTES.get(code)
            if note:
                head += f"\n<i>ℹ️ {note}</i>"
            sections.append(head + "\n" + names)

        if not sections:
            return

        msg = (
            f"📢 <b>[거래소 신규 지정 종목] {latest}</b>\n"
            f"════════════\n\n"
            + "\n\n".join(sections)
        )
        stock_api.send_telegram(target, msg)
        logging.info(f"📢 [시장경보] 신규 지정 {len(new_alerts)}개 알림 발송")

    except Exception as e:
        logging.debug(f"시장경보 체크 오류: {e}")


@_job()
def job_collect_market_closing():
    """평일 장 마감 후 (15:40) — 전체 상장사 시장 데이터 수집"""
    if market_timer.is_kr_holiday():  # 주말/공휴일 스킵
        logging.info("⏸ [시장수집-전체] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📊 [시장수집-전체] 장 마감 후 전체 상장사 수집 시작")
        ok, fail = run_market(all_listed=True)
        logging.info(f"📊 [시장수집-전체] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집-전체] 완료 ({ok}개)")

        # 수집 완료 후 기간별 수익률 일괄 계산
        try:
            from collect_market import calculate_returns
            _sb_ret = _bridge._get_client() if _BRIDGE_OK else None
            if _sb_ret:
                calculate_returns(_sb_ret)
        except Exception as _re:
            logging.error(f"❌ [수익률] 계산 오류: {_re}")
    except Exception as e:
        logging.error(f"❌ [시장수집-전체] 오류: {e}")
        mark_failed(e)

    # (관심가·목표가 도달 알림 job_watchlist_alert는 10-04 투자노트가 원본 포트폴리오 표로 바뀌며
    #  관심가·목표가 칸이 없어져 껐다 — 화면에서 고칠 수 없는 옛 값으로 메인 채팅방 알림이 나가던 것)

    # 거래소 신규 지정 종목 알림 (장 마감 후 1회)
    _check_market_warnings()


@_job(holiday=True)
def job_short_surge():
    """평일 17:05 — KRX 공매도 비중 수집 + 5거래일 평균 대비 2배 급증 종목 텔레그램 알림"""
    if not _COLLECTOR_OK:
        logging.warning("[공매도급증] 수집 모듈 없음 — 스킵")
        return
    try:
        # 1. 당일 공매도 데이터 수집
        saved, _ = run_short()
        logging.info(f"📉 [공매도급증] 수집 완료 {saved}건")

        # 2. 급증 탐지
        if not _BRIDGE_OK:
            return
        sb = _bridge._get_client()
        if not sb:
            return
        surges = _check_short_surge(sb, n_days=5, multiplier=2.0)
        if not surges:
            logging.info("📉 [공매도급증] 급증 종목 없음")
            return

        # 3. 메인 채널 알림 (collect_short 공용 포매터)
        msg = _format_surge_msg(surges, multiplier=2.0)
        _send_logged(DEFAULT_CHAT_ID, "공매도 급증", msg)   # 예전엔 기록 없음
        logging.info(f"📉 [공매도급증] 알림 발송 완료 ({len(surges)}건)")
    except Exception as e:
        logging.error(f"❌ [공매도급증] job 실패: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_investor_trend():
    """평일 장 마감 후 (16:45) — 종목별 외국인·기관 순매수 확정 수집 (모니터링 종목).
    inquire-investor(FHKST01010900)로 market_data.foreign_net_buy/institution_net_buy 갱신.
    일별 quote 수집(frgn_ntby_qty)이 장중 추정치라 마감 후 0으로 비는 문제를 근본 해결.
    sector_daily_summary(17:15) 집계 전에 실행해 같은 날 수급이 반영되도록 한다."""
    if not _COLLECTOR_OK:
        logging.warning("[투자자수급] 수집 모듈 없음 — 스킵")
        return
    try:
        import collect_market
        updated, failed = collect_market.collect_investor_trend(all_listed=False)
        logging.info(f"=== [투자자수급] 완료: {updated}개 갱신 / 미수집 {failed} ===")
    except Exception as e:
        logging.error(f"❌ [투자자수급] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_investor_trend_ranked():
    """평일 18:15 — 수급 확정 정산 + 마감 브리핑 랭킹용 모집단 확장.
    모니터링 312종목에 더해 당일 거래대금이 RANK_TURNOVER_MIN 이상인 비모니터링 종목까지
    수집한다. |순매수대금| ≤ 거래대금 이므로 18:30 "오늘의 수급" Top3가 시장 전체 기준이 된다.
    (16:45 잡은 모니터링 종목만 — 17:15 sector_daily_summary가 is_monitored 기준이라 불필요)"""
    if not _COLLECTOR_OK:
        logging.warning("[투자자수급] 수집 모듈 없음 — 스킵")
        return
    try:
        import collect_market
        from config import RANK_TURNOVER_MIN
        updated, failed = collect_market.collect_investor_trend(
            all_listed=False, extra_turnover_min=RANK_TURNOVER_MIN)
        logging.info(f"=== [투자자수급·랭킹] 완료: {updated}개 갱신 / 미수집 {failed} ===")
    except Exception as e:
        logging.error(f"❌ [투자자수급·랭킹] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_flow_empty():
    """평일 18:50 — 전종목 수급 정산 + 빈집 판정 (market_data.flow_quad / flow_pctl).

    ① 전 상장사 외국인·기관 순매수 30거래일을 KIS와 맞춘다. 비모니터링 종목의 일별 수급을
       쌓고, 18:15 수집 뒤 KRX가 고친 값도 반영한다(수집값의 88%가 며칠 뒤 달라짐, 09-27 실측).
    ② 빈집 판정 — 수급 지도(flow-map.js 빈집 모드)와 같은 정의, 순위 집단만 WICS 업종.
    기업분석 표의 '수급빈집' 칩이 이 컬럼만 읽는다(화면 계산 시 로딩 2배).
    ③ 태린이아빠 전략 조건(collect_leading) — 주도 업종 보드·RS·후보 조건(lead_flags).
    ④ 업종 수급 오실레이터(collect_sector_flow) — WICS 중분류 × 시장, KIS 호출 없음(DB 합산).
    25~35분 걸려 스레드로 돈다(run_all). 18:40 추정실적(KIS 약 9분)이 끝난 뒤 19:25 재처리
    전까지 KIS를 쓰는 대량 잡이 없는 구간이다. run_all이 18:42 당일 매수대금·18:40 추정실적의
    '완료'에 묶어 띄운다(_after) — 두 잡이 늦게 끝난 날엔 그만큼 늦게 시작한다."""
    failed = None
    try:
        import collect_flow_empty
        n = collect_flow_empty.run()
        logging.info(f"=== [빈집] 완료: {n}행 기록 ===")
    except Exception as e:
        logging.error(f"❌ [빈집] 오류: {e}")
        failed = e
    # ③ 태린이아빠 전략 조건 — 주도 업종(WICS 중분류 모멘텀 ∩ 매수) · RS · 후보 조건.
    #    빈집 판정이 실패해도 따로 돈다(업종 보드는 빈집과 무관하게 쓸모가 있다). 약 4분(RS 주봉 조회)
    try:
        import collect_leading
        r = collect_leading.run()
        logging.info(f"=== [주도업종] 완료: {(r or {}).get('info', {})} ===")
    except Exception as e:
        logging.error(f"❌ [주도업종] 오류: {e}")
        failed = failed or e
    # ④ 업종 수급 오실레이터 — WICS 중분류 × 시장. 위 수급 정산(30거래일 사후 수정 반영) 뒤라야 한다
    try:
        import collect_sector_flow
        n = collect_sector_flow.run()
        logging.info(f"=== [업종수급] 완료: 오실레이터 {n}행 ===")
    except Exception as e:
        logging.error(f"❌ [업종수급] 오류: {e}")
        failed = failed or e
    # ⑤ 업종 쏠림지수·브레드스 국면 — WICS 중분류 지수(wiseindex) + 코스피200 동일가중(KIS 1~2회)
    try:
        import collect_sector_market
        n = collect_sector_market.run()
        logging.info(f"=== [업종쏠림] 완료: {n}일 ===")
    except Exception as e:
        logging.error(f"❌ [업종쏠림] 오류: {e}")
        failed = failed or e
    if failed:
        mark_failed(failed)


@_job(holiday=True)
def job_krx_buy():
    """평일 18:42 · 20:40 — KRX 당일 투자자별 매수대금(사모·투신·연기금·외국인) → market_data 매수대금 컬럼.

    KIS는 전일까지만 줘서(18:50 sync_buy) 화면 '일별 수급'과 공급 컨셉이 하루 늦었다. KRX 로그인 세션으로
    당일분을 먼저 채운다(값이 다른 행만 써서 멱등). 18:42 = 빈집·컨셉 계산(18:50) 앞, 20:40 = 시간외 반영 뒤 한 번 더."""
    try:
        logging.info("💰 [당일매수대금] collect_krx_buy 실행 시작")
        import collect_krx_buy
        r = collect_krx_buy.run()
        logging.info(f"=== [당일매수대금] 완료: {r['date']} 변경 {r['changed']}행 · 기록 {r['written']}행 ===")
    except Exception as e:
        logging.error(f"❌ [당일매수대금] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_sio():
    """평일 16:02 · 18:46 — SIO(코스피200·코스닥150·코스피대형주 시장 내부 강도) → sio_daily.

    구성종목은 KRX(로그인), 시세·거래량은 15:45 전 종목 마감 수집분(market_data). 최근 10거래일 중 빠진 날을
    채우고 오늘은 늘 다시 계산한다(upsert라 멱등). 표가 비어 있으면 남아 있는 날짜 전부(약 4분)."""
    try:
        logging.info("📐 [SIO] collect_sio 실행 시작")
        import collect_sio
        r = collect_sio.run()
        logging.info(f"=== [SIO] 완료: {len(r.get('dates', []))}일 · {r.get('rows', 0)}행 ===")
    except Exception as e:
        logging.error(f"❌ [SIO] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_index():
    """매일 06:50 · 08:10 — 미국 지수·ETF 종가(yfinance) → us_index_daily (미국 위험신호 · 미국 피어앤그리드 원재료).

    한국 휴장일에도 돈다(미국 장은 열린다). 저장값과 다른 날만 써서 주말·미국 휴장일엔 쓰는 줄이 없다."""
    try:
        logging.info("🗽 [미국지수] collect_us_index 실행 시작")
        import collect_us_index
        r = collect_us_index.run()
        logging.info(f"=== [미국지수] 완료: {r.get('last')} · 쓴 {r.get('written', 0)}일 ===")
    except Exception as e:
        logging.error(f"❌ [미국지수] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_liquidity():
    """매일 07:10 · 12:10 — 미국 유동성 체크(FRED 15종) → us_liquidity_daily (하루 한 줄, 같은 날은 덮어씀).

    금리·Fed 자산·TGA·RRP·은행대출·M2·엔캐리 점검을 원본 식 그대로 계산한다. FRED는 미국 영업일 저녁에 갱신된다."""
    try:
        logging.info("💧 [미국유동성] collect_us_liquidity 실행 시작")
        import collect_us_liquidity
        r = collect_us_liquidity.run()
        logging.info(f"=== [미국유동성] 완료: {r.get('date')} 자료 {r.get('data_date')} · "
                     f"Liquidity {r.get('score')}/4 · Credit {r.get('credit')}/2 ===")
    except Exception as e:
        logging.error(f"❌ [미국유동성] 오류: {e}")
        mark_failed(e)


@_job(weekday_only=True)
def job_jp_tw():
    """평일 16:40 · 18:20 — 일본·대만 AI·반도체 공급망 97종목 시세 점검(Yahoo 일봉) → market_snapshot(kind='jp_tw').

    일본 15:30 · 대만 14:30(한국 시각) 마감 뒤 끝난 일봉만으로 일간·5일·20일 수익률, 테마 Breadth·Strength Score, 신호를 계산한다.
    한국 휴장일에도 돈다(두 나라 장은 열린다). 같은 기준일은 덮어쓴다."""
    try:
        logging.info("🗾 [일본대만] collect_jp_tw 실행 시작")
        import collect_jp_tw
        r = collect_jp_tw.run()
        logging.info(f"=== [일본대만] 완료: {r.get('date')} 유효 {r.get('valid')}/{r.get('total')}종목 ===")
    except Exception as e:
        logging.error(f"❌ [일본대만] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_52w():
    """매일 07:30 · 08:40 — 미국 52주 신고가 전략(원본 v11.63 개선 52W + 성장 ETF + MA40 2일) → market_snapshot(kind='us_52w').

    SPY·QQQ·업종 ETF 57개 수정 종가(2018-12-07~, 2000거래일)로 옛 52W FIXED · Rotation_B와 개선 전략 백테스트,
    다음 거래일 목표 비중을 계산한다(약 25초). 한국 휴장일에도 돈다. 같은 기준일은 덮어쓴다."""
    try:
        logging.info("🏁 [미국52주] collect_us_52w 실행 시작")
        import collect_us_52w
        r = collect_us_52w.run()
        logging.info(f"=== [미국52주] 완료: {r.get('date')} 개선 {r.get('mode')} · 52W 안 {r.get('regime')} ===")
    except Exception as e:
        logging.error(f"❌ [미국52주] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_etf_rs():
    """매일 07:20 · 08:30 — 미국 업종 ETF 99개 상대강도 · 소르티노 점검(Yahoo 수정 종가 3년) → market_snapshot(kind='us_etf').

    SPY 대비 Mansfield RS, 변동성 조정 모멘텀 · 소르티노 최근 10거래일 상위 20, 30일 이격도 과열 점검, 이동평균 정배열.
    한국 휴장일에도 돈다. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("📊 [미국ETF] collect_us_etf_rs 실행 시작")
        import collect_us_etf_rs
        r = collect_us_etf_rs.run()
        logging.info(f"=== [미국ETF] 완료: {r.get('date')} ETF {r.get('n')} · RS 70 이상 {r.get('strong')}개 · 완전 정배열 {r.get('full')}개 ===")
    except Exception as e:
        logging.error(f"❌ [미국ETF] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_kr_etf_rs():
    """평일 16:50 · 19:20 — 한국 업종 ETF 38개 + 대형주 4종목 상대강도 · 시장 폭 · 52주 신고가 전략 → market_snapshot(kind='kr_etf').

    코스피 대비 Mansfield RS, 모멘텀 · 소르티노 · ETR 최근 10거래일 상위 20, 쏠림지수 · Breadth, 30일 이격도 과열 점검, 정배열,
    52주 신고가 전략 백테스트(2025-01-21부터). 야후 종가(빈 날은 KRX로 채움), 약 15초. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("🧭 [한국ETF] collect_kr_etf_rs 실행 시작")
        import collect_kr_etf_rs
        r = collect_kr_etf_rs.run()
        p = r.get('payload') or {}
        logging.info(f"=== [한국ETF] 완료: {r.get('date')} 종목 {p.get('n')} · 쏠림 {(p.get('crowd') or {}).get('value')} ===")
    except Exception as e:
        logging.error(f"❌ [한국ETF] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_kr_monthly():
    """평일 20:50 · 07:50 — 국내 월 리밸런싱 V2.6(WICS 업종지수 26개 Breadth · 코스피 상대강도 · 주간 순위 가점 · 회복 · 역추세 · F&G · 고변동성 제한)
    → market_snapshot(kind='kr_monthly').

    wiseindex 업종지수(장 마감 뒤 저녁에 그날 값이 올라옴) + 코스피를 2015-12-30부터 받아 원본 백테스트(V2.5 · V2.6)와
    이번 달 확정 · 다음 달 예상 포지션을 계산한다(약 20초). 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("📅 [국내월간] collect_kr_monthly 실행 시작")
        import collect_kr_monthly
        r = collect_kr_monthly.run()
        v = (r.get('payload') or {}).get('preview') or {}
        logging.info(f"=== [국내월간] 완료: {r.get('date')} 다음 달 예상 {v.get('mode')} {v.get('exp')} ===")
    except Exception as e:
        logging.error(f"❌ [국내월간] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_monthly():
    """매일 07:45 · 08:50 — 미국 월 리밸런싱 V8.2 · 개선 2조건(ETF 52개 Breadth · QQQ/SPY 상대강도 · 업종 모멘텀 · 금속)
    → market_snapshot(kind='us_monthly').

    야후 수정 종가 52개(2018-12-07~)로 원본 신호 · 다음 거래일 종가 체결 백테스트 · 전체 기간 SPY · QQQ 비교와
    이번 달 확정 · 다음 달 예상 포지션을 계산한다(약 15초). 한국 휴장일에도 돈다. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("🗽 [미국월간] collect_us_monthly 실행 시작")
        import collect_us_monthly
        r = collect_us_monthly.run()
        v = (r.get('payload') or {}).get('preview') or {}
        logging.info(f"=== [미국월간] 완료: {r.get('date')} 다음 달 예상 {v.get('mode')} {v.get('exp')} ===")
    except Exception as e:
        logging.error(f"❌ [미국월간] 오류: {e}")
        mark_failed(e)


@_job()
def job_us_inverse():
    """매일 07:35 · 08:45 — 미국 인버스 혼용 전략(QQQ · TIP 월봉 6개월선 → 상승 QQQ · QLD · SOXX / 하락 SPMO · PSQ)
    → market_snapshot(kind='us_inverse').

    야후 수정 종가 6개(2020-12-02~)로 월말 신호 · 다음 달 고정 비중 백테스트, 이번 달 확정 · 다음 달 임시 포지션을 계산한다(약 5초).
    한국 휴장일에도 돈다. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("🔻 [미국인버스] collect_us_inverse 실행 시작")
        import collect_us_inverse
        r = collect_us_inverse.run()
        p = r.get('payload') or {}
        nxt = (p.get('preview') or p.get('confirmed') or {}).get('w') or {}
        logging.info(f"=== [미국인버스] 완료: {r.get('date')} 다음 달 "
                     f"{' / '.join(f'{a} {v * 100:.0f}%' for a, v in nxt.items() if v)} ===")
    except Exception as e:
        logging.error(f"❌ [미국인버스] 오류: {e}")
        mark_failed(e)


@_job()
def job_ai_hw():
    """매일 07:00 · 08:20 — AI 하드웨어 41종목 Breadth · 피크아웃 점검(Yahoo 수정 종가 3년) → market_snapshot(kind='ai_hw').

    미국 장 마감 뒤 계산한다(미국 26 · 한국 3 · 일본 8 · 대만/ADR 4). 한국 휴장일에도 돈다. 같은 기준일은 덮어쓰고 지난 줄은 10일만 남긴다."""
    try:
        logging.info("🧠 [AI하드웨어] collect_ai_hw 실행 시작")
        import collect_ai_hw
        r = collect_ai_hw.run()
        logging.info(f"=== [AI하드웨어] 완료: {r.get('date')} 종목 {r.get('n')} · Breadth MA5 {r.get('ma5')} · 단계 {r.get('risk')} ===")
    except Exception as e:
        logging.error(f"❌ [AI하드웨어] 오류: {e}")
        mark_failed(e)


@_job()
def job_tw_revenue():
    """매일 21:10 — 대만 AI·반도체 공급망 월매출(MOPS 공식 공시) → tw_monthly_revenue.

    대만 기업은 매달 10일까지 전달 매출을 신고한다. 평소엔 최근 3개월만 다시 받아 값이 다른 줄만 쓰고(약 1분),
    표가 비었거나 종목이 바뀌면 최근 4개 연도를 전부 받는다(약 12분)."""
    try:
        logging.info("🏭 [대만월매출] collect_tw_revenue 실행 시작")
        import collect_tw_revenue
        r = collect_tw_revenue.run()
        logging.info(f"=== [대만월매출] 완료: 최신 {str(r.get('latest'))[:7]} {r.get('latest_count')}종목 · 쓴 {r.get('written', 0)}줄 ===")
    except Exception as e:
        logging.error(f"❌ [대만월매출] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_sector_summary():
    """평일 장 마감 후 (17:15) — 산업별 일별 요약 집계 (sector_daily_summary)"""
    try:
        logging.info("📊 [섹터요약] collect_sector_summary 실행")
        from collect_sector_summary import run as run_sector
        run_sector()
        logging.info("✅ [섹터요약] 완료")
    except Exception as e:
        logging.error(f"❌ [섹터요약] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_estimates():
    """평일 장 마감 후 (18:40) — KIS 종목추정실적(미래 매출/영업이익) 수집.
    consensus_estimates 스냅샷 누적 + est_date 변경 시 estimate_revisions 기록."""
    try:
        logging.info("📈 [추정실적] collect_estimates 실행 시작")
        import collect_estimates
        covered, revisions = collect_estimates.run()
        logging.info(f"=== [추정실적] 완료: 커버 {covered}종목 / 갱신감지 {revisions}건 ===")
    except Exception as e:
        logging.error(f"❌ [추정실적] 오류: {e}")
        mark_failed(e)


@_job('earnings_surprise', holiday=True)
def job_snapshot_qtr_consensus():
    """평일 장전 (08:40) — 네이버 분기 컨센서스 스냅샷 (어닝 서프라이즈 판정용)."""
    import collect_qtr_consensus
    n = collect_qtr_consensus.run()
    logging.info(f"=== [분기컨센] 스냅샷 완료: {n}종목 ===")


@_job('earnings_surprise', holiday=True)
def job_earnings_reconcile():
    """평일 장 마감 후 (19:05) — 재무 확정치 기반 어닝 서프라이즈/영업익 폭증 재조정.

    실시간 잠정공시 훅에만 의존하던 종전 구조는 훅 누락(2026-08 리팩토링으로 소실) 시
    적재가 0이 됐다. 이 잡은 financials 확정치를 스캔해 컨센 +10%↑·흑자전환·영업익
    YoY/QoQ +100%↑ 폭증을 earnings_surprise에 신규만 멱등 적재한다(각 종목 1회).
    19:10 브리핑이 당일 적재분을 메인채널로 발송한다.
    순서는 run_all이 '완료'로 묶는다(_after): 18:30 재무수집 완료 → 이 잡 → 브리핑. 브리핑은 그날
    base_date 행만 읽고 이 잡은 새 (종목, 분기)만 넣으므로, 브리핑이 먼저 나가면 그날 새로 잡힌 종목은
    다음 날에도 실리지 않는다(2026-11-13 17건)."""
    if not _COLLECTOR_OK:
        logging.warning("[어닝재조정] auto_detect_quarter 불가 — 스킵")
        return
    import earnings_surprise
    import datetime as _dt
    # ⚠️ auto_detect_quarter()는 Q4를 절대 반환하지 않는다(1~3월=전년 Q3·12월=Q3).
    #    그대로 쓰면 사업보고서(Q4/연간) 시즌(2~3월 제출)이 통째로 누락되므로
    #    1~4월엔 전년 Q4를 대상에 명시적으로 추가한다(중복은 skip-existing으로 무해).
    year, quarter = auto_detect_quarter()
    targets = [(year, quarter)]
    if _dt.date.today().month <= 4:
        targets.append((str(_dt.date.today().year - 1), "Q4"))
    total = 0
    for (ty, tq) in targets:
        det = earnings_surprise.reconcile_quarter(ty, tq, persist=True)
        total += len(det)
        logging.info(f"=== [어닝재조정] {ty} {tq}: 탐지 {len(det)}건 ===")
    logging.info(f"=== [어닝재조정] 완료: 대상 {len(targets)}분기 · 총 탐지 {total}건 ===")


@_job('earnings_surprise', holiday=True)
def job_earnings_surprise_briefing():
    """평일 장 마감 후 (19:10) — 당일 어닝 서프라이즈 리스트 → 메인채널."""
    import earnings_surprise
    msg = earnings_surprise.build_briefing()
    if msg:
        _send_logged(DEFAULT_CHAT_ID, "어닝서프라이즈", msg)
        logging.info("📊 [어닝서프라이즈] 메인채널 발송 완료")
    else:
        logging.info("[어닝서프라이즈] 당일 대상 없음 — 발송 스킵")


@_job(holiday=True)
def job_collect_investor_market():
    """평일 장 마감 후 (18:20) — KIS 시장별 투자자매매동향 수집 (시황 투자자별 카드)"""
    try:
        logging.info("💰 [투자자동향] collect_investor_market 실행 시작")
        import collect_investor_market
        n = collect_investor_market.run()
        logging.info(f"=== [투자자동향] 완료: {n}건 upsert ===")
    except Exception as e:
        logging.error(f"❌ [투자자동향] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_fear_greed():
    """평일 장 마감 후 (18:22) — 한국 피어앤그리드 원재료(지수·VKOSPI·국채선물지수·옵션 풋/콜 거래량)
    수집 (시황 피어앤그리드 카드). 옵션 거래량은 당일 값만 주는 API라 거래일 저녁에 꼭 돌아야 한다."""
    try:
        logging.info("😨 [피어앤그리드] collect_fear_greed 실행 시작")
        import collect_fear_greed
        n = collect_fear_greed.run()
        logging.info(f"=== [피어앤그리드] 완료: 지수 {n}일 upsert ===")
    except Exception as e:
        logging.error(f"❌ [피어앤그리드] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_portfolio_nav():
    """평일 16:30 — 투자노트(원본 포트폴리오 표) 순자산·기준가 일별 기록. 정규장 종가(15:45 수집) 뒤.
    순자산 = Σ(현보유 × 종가) + 현금, 기준가 = 순자산 ÷ 좌수 × 1000 (collect_portfolio_nav.py)."""
    try:
        logging.info("📒 [기준가] collect_portfolio_nav 실행 시작")
        import collect_portfolio_nav
        r = collect_portfolio_nav.run()
        logging.info(f"=== [기준가] 완료: {r.get('date')} 기준가 {r.get('price')} ===")
    except Exception as e:
        logging.error(f"❌ [기준가] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_active_etf():
    """평일 저녁 (19:35) — 액티브 ETF 30개 구성종목(PDF) 일별 저장 (화면 '액티브 ETF' 페이지).
    KRX 정보데이터시스템(로그인, .env KRX_ID·KRX_PW). 최근 10일 중 빠진 날만 채워 멱등."""
    try:
        logging.info("📦 [액티브ETF] collect_active_etf 실행 시작")
        import collect_active_etf
        r = collect_active_etf.run()
        logging.info(f"=== [액티브ETF] 완료: {len(r.get('dates', []))}일 · {r.get('rows', 0)}행 ===")
    except Exception as e:
        logging.error(f"❌ [액티브ETF] 오류: {e}")
        mark_failed(e)


@_job()
def job_collect_exports():
    """매일 10:10 — 관세청 품목·국가·시군구 월별 수출입(최근 3개월) 저장 (화면 '수출 데이터' 페이지).
    전월 확정치가 매월 15일경 올라오고 정정이 반영되므로 날마다 최근 3개월을 다시 받는다(멱등).
    휴장일과 무관한 통계라 매일 돈다. 키: .env DATA_GO_KR_KEY (없으면 실패 알림)."""
    try:
        logging.info("🚢 [수출] collect_exports 실행 시작")
        import collect_exports
        r = collect_exports.run()
        logging.info(f"=== [수출] 완료: {r.get('series', 0)}계열 · {r.get('rows', 0)}행 · API {r.get('calls', 0)}회 ===")
    except Exception as e:
        logging.error(f"❌ [수출] 오류: {e}")
        mark_failed(e)


@_job()
def job_earnings_stage():
    """매일 20:10·07:40 — 실적 국면(태린이아빠 「확정실적 분석」) 전 종목 재계산 → earnings_stage
    (화면 '실적 국면' 페이지). financials를 다시 읽어 국면 사슬을 처음부터 계산하므로 멱등이다.
    18:30 재무수집이 마감일엔 자정 넘어까지 돌고 토요일 02:00 백필도 있어 아침에 한 번 더 돈다.
    테이블이 없으면(sql/earnings_stage.sql 실행 전) 조용히 스킵. 약 20초."""
    try:
        import earnings_stage
        r = earnings_stage.run()
        logging.info(f"=== [실적국면] 완료: {r.get('stocks', 0)}종목 · 기준분기 {r.get('period')} · {r.get('dist')} ===")
    except Exception as e:
        logging.error(f"❌ [실적국면] 오류: {e}")
        mark_failed(e)


@_job()
def job_collect_backlog():
    """매일 20:20 — 반도체 장비(전공정·후공정) 수주잔고. 최근 7일 정기보고서 중 아직 없는 분기·정정 보고서만
    본문 '매출 및 수주상황' 표에서 기말 잔고 합계를 읽어 dart_segment_revenue('backlog_total')에 저장
    (collect_backlog.py). 종목리포트 '동종비교' 탭·기업 비교 '수주잔고' 지표가 읽는다.
    보고서 본문이 한 건 ~15초(4MB 안팎)라 마감일(대상 수십 곳)엔 10분 남짓 — 스레드로 돈다."""
    try:
        import collect_backlog
        res = collect_backlog.run_recent(7)
        n = sum(1 for r in res if r[3] is not None)
        logging.info(f"=== [수주잔고] 보고서 {len(res)}건 읽음 · 잔고 {n}건 저장 ===")
    except Exception as e:
        logging.error(f"❌ [수주잔고] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_collect_credit_balance():
    """평일 저녁 (19:00, +10:30 보정) — KOFIA 신용공여 잔고(신용거래융자 등) 수집.
    직전 영업일분이 당일 오후 발표되므로(2026-07-22 실측) 저녁 실행이 본 수집,
    발표가 밀린 날은 다음날 10:30 보정 실행이 회수한다. 14일 윈도 멱등 upsert."""
    try:
        logging.info("💳 [신용잔고] collect_credit_balance 실행 시작")
        import collect_credit_balance
        n = collect_credit_balance.run()
        logging.info(f"=== [신용잔고] 완료: {n}건 upsert ===")
        try:
            import credit_chart
            credit_chart.run()   # 신규 기준일이 있을 때만 본방 차트 발송
        except Exception as e:
            logging.error(f"❌ [신용잔고차트] 발송 오류: {e}")
    except Exception as e:
        logging.error(f"❌ [신용잔고] 오류: {e}")


@_job()
def job_collect_wics():
    """매일 22:00 — 네이버 증권 WICS 업종 분류 수집(companies.wics_*).
    휴장일에도 돌린다 — 시세가 아니라 분류라 거래일과 무관하고,
    신규 상장 종목이 빨리 칄질수록 좋다.
    장 끝난 밤에 돈다. 영업일 08:20엔 '업종 0종'이었는데, 장 시작 전에만 업종별 종목 수가
    0으로 와서였다 — 지금은 최상위 값으로 폴백해 시각과 무관(collect_wics 머리말).
    불완전 수집은 collect_wics가 예외로 올리므로 여기서 실패로 기록·알림된다."""
    try:
        logging.info("🏷 [업종] collect_wics 실행 시작")
        import collect_wics
        n = collect_wics.run()
        logging.info(f"=== [업종] 완료: {n}개 갱신 ===")
    except Exception as e:
        logging.error(f"❌ [업종] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_leading_stocks():
    """평일 장 마감 후 (17:30) — 주도주 탐색기 스코어 계산 및 저장.
    예외를 여기서 삼키면 0건 저장이 성공으로 위장됨(07-14·16 실측) — _job이 기록·알림."""
    logging.info("🚀 [주도주] leading_stocks_generator 실행 시작")
    from leading_stocks_generator import run as run_leading
    run_leading()
    logging.info("✅ [주도주] 생성 완료")


@_job(holiday=True)
def job_market_summary():
    """평일 장 마감 후 (18:30) — 투자포인트 요약 생성 (market_investment_summary).
    18:15 수급 확정 정산 이후 실행 — 프론트 '오늘의 시장 판단' Zone B의 DB 경로를 채운다."""
    try:
        logging.info("📝 [시장요약] market_summary_generator 실행")
        from market_summary_generator import run as run_summary
        run_summary()
        logging.info("✅ [시장요약] 완료")
    except Exception as e:
        logging.error(f"❌ [시장요약] 오류: {e}")
        mark_failed(e)


@_job(holiday=True)
def job_daily_summary():
    """평일 18:55 — 종목별 저녁 요약 생성 (daily_summaries).

    18:30 job_collect_financials가 daily_disclosures를 채운 뒤 실행해야 한다 — run_all이 재무수집의
    '완료'에 묶어 띄운다(_after). 재무수집이 18:55를 넘기는 실적 마감 주간엔 그만큼 늦게 시작한다.
    뉴스는 뉴스봇이 발송 시점에 적재한 daily_news를 읽는다(재조회 불가).
    """
    try:
        logging.info("🌙 [저녁요약] daily_summary 실행")
        import daily_summary
        saved = daily_summary.run()
        logging.info(f"✅ [저녁요약] 완료 — {saved}개 종목")
        # 활동 있는 종목을 기업채팅방으로 발송 (방 없는 종목은 웹 카드로만 노출)
        sent = daily_summary.broadcast()
        logging.info(f"✅ [저녁요약] 기업채팅방 발송 {sent}건")
    except Exception as e:
        logging.error(f"❌ [저녁요약] 오류: {e}")
        mark_failed(e)


# ── 실패 잡 자동 재처리 (job_runs 기반) ──────────────────────────────

# 멱등 잡만 — upsert·자체 중복가드가 있어 재실행해도 데이터·발송이 중복되지 않는 잡.
# 신고가(무가드 텔레그램 발송)·추정실적(테이블 생성 전 API 낭비)은 의도적으로 제외.
_RETRYABLE_JOBS = {
    'job_leading_stocks':           job_leading_stocks,
    'job_sector_summary':           job_sector_summary,
    'job_collect_investor_trend':   job_collect_investor_trend,
    'job_collect_investor_trend_ranked': job_collect_investor_trend_ranked,
    'job_collect_investor_market':  job_collect_investor_market,
    'job_collect_fear_greed':       job_collect_fear_greed,
    'job_flow_empty':               job_flow_empty,
    'job_krx_buy':                  job_krx_buy,
    'job_sio':                      job_sio,
    'job_us_index':                 job_us_index,
    'job_us_liquidity':             job_us_liquidity,
    'job_tw_revenue':               job_tw_revenue,
    'job_jp_tw':                    job_jp_tw,
    'job_ai_hw':                    job_ai_hw,
    'job_us_etf_rs':                job_us_etf_rs,
    'job_us_52w':                   job_us_52w,
    'job_kr_etf_rs':                job_kr_etf_rs,
    'job_kr_monthly':               job_kr_monthly,
    'job_us_monthly':               job_us_monthly,
    'job_us_inverse':               job_us_inverse,
    'job_market_summary':           job_market_summary,
    'job_collect_us_etf':           job_collect_us_etf,
    'job_collect_analyst_opinions': job_collect_analyst_opinions,
    'job_collect_credit_balance':   job_collect_credit_balance,
    'job_earnings_stage':           job_earnings_stage,
    # 공매도: 17:05에 KRX 당일분이 아직 안 올라와 0건이면 실패로 기록된다 → 19:25에 한 번 더.
    # 알림은 수집 성공 뒤에만 보내므로 실패한 회차를 다시 돌려도 중복 발송이 없다.
    'job_short_surge':              job_short_surge,
}


@_job(key='auto_retry', holiday=True)
def job_retry_failed():
    """평일 19:25 — ①오늘 마지막 실행이 실패했거나 ②예정시각이 지났는데 실행 기록이
    아예 없는(배포 재시작 등으로 실행 도중 소실) 멱등 잡을 1회 재실행.
    19:00 freshness 알림 후·19:50 운영요약 전이라 재처리 결과가 요약에 반영된다."""
    if not _BRIDGE_OK:
        return
    try:
        sb = _bridge._get_client()
        today = datetime.date.today().isoformat()
        rows = (sb.table('job_runs').select('job_name,ok,finished_at')
                .eq('run_date', today).order('finished_at').execute().data or [])
    except Exception as e:
        logging.warning(f"[재처리] job_runs 조회 불가 — 스킵: {str(e)[:120]}")
        mark_failed(e)
        return
    last = {}
    for r in rows:
        last[r['job_name']] = r['ok']
    # ① 실행됐으나 실패한 잡
    failed = [n for n, ok in last.items() if not ok and n in _RETRYABLE_JOBS]
    # ② 예정시각이 지났는데 실행 기록이 아예 없는 잡 — 배포 재시작 등으로 실행 도중
    #    프로세스가 죽으면 실패 기록조차 남지 않아 ①에서 못 잡는다. get_missing_jobs로
    #    보완(가드·요일 판정은 그쪽이 수행). market_closing은 _RETRYABLE_JOBS에 없어 제외(알림 발송).
    missing, _disabled = get_missing_jobs(set(last))
    unrun = [n for n, _at in missing if n in _RETRYABLE_JOBS]
    targets = list(dict.fromkeys(failed + unrun))  # 순서 보존 dedup
    # 지금 돌고 있거나 선행 잡을 기다리는 잡은 건드리지 않는다 — 겹쳐 돌리면 둘 다 느려지고
    # 이 잡(스케줄 루프)이 그동안 막힌다. ②는 get_missing_jobs가 이미 빼지만 ①(앞 회차 실패 뒤
    # 다음 회차가 도는 중)도 같은 이유로 뺀다.
    _running, _waiting = get_active_jobs()
    busy = [n for n in targets if n in _running or n in _waiting]
    if busy:
        logging.info("[재처리] 진행 중이라 건너뜀: " + ", ".join(busy))
        targets = [n for n in targets if n not in busy]
    if not targets:
        logging.info("[재처리] 재실행 대상 없음")
        return
    logging.info(f"[재처리] {len(targets)}개 재실행: " + ", ".join(targets))
    for name in targets:
        _RETRYABLE_JOBS[name]()
