"""
watchdog_flags.py — 워치독: 수동 트리거 플래그·재로드·봇 요청 큐
────────────────────────────────────────────────────────────────
run_all.py 물리 분할 (2026-07): app_config 타임스탬프 플래그(_TS_FLAG_JOBS),
reload_flag(종목 재로드 + 신규 종목 백필), 공시수집 수동 트리거, bot_requests 처리.
run_all.main() 루프에서 매 60초 _run_watchdog_flags() 호출.
"""
import os
import json
import time
import logging
import datetime

from managers import market_timer
from job_infra import _bridge, _BRIDGE_OK, _start_daemon
from jobs_collect import (
    _COLLECTOR_OK, _collect_company_one,
    job_collect_financials, job_collect_macro,
    job_collect_foreign_institution, job_collect_investor_trend,
    run_market,
)
from jobs_briefing import _PRO_OK, _pro

# ✅ 대시보드 봇 요청 큐 (멤버 동기화·공지 발송 위임 — bot_requests 테이블)
try:
    import bot_requests as _botreq
    _BOTREQ_OK = True
    logging.info("✅ [BotReq] 봇 요청 큐 모듈 로드 완료")
except ImportError as _bqe:
    _botreq = None
    _BOTREQ_OK = False
    logging.warning(f"⚠️ [BotReq] bot_requests 모듈 없음 (무시): {_bqe}")

# 공시수집 중복 실행 방지 플래그
_disclosure_running = False


def _read_ts_flag(sb_client, key):
    """플래그 경과 시간(초) 반환. 미발동(0·빈값·비숫자·row없음)이면 None."""
    try:
        res = sb_client.table('app_config').select('value').eq('key', key).maybe_single().execute()
    except Exception:
        # maybe_single 미지원 구버전 supabase-py fallback
        try:
            res = sb_client.table('app_config').select('value').eq('key', key).limit(1).execute()
            if not res.data:
                return None
            res = type('R', (), {'data': res.data[0]})()
        except Exception:
            return None
    if res.data is None:
        return None
    val = res.data.get('value', '0') if isinstance(res.data, dict) else '0'
    if not val or val == '0':
        return None
    val_str = str(val).strip()
    if not val_str.isdigit():
        return None
    return (time.time() * 1000 - float(val_str)) / 1000


def _clear_ts_flag(sb_client, key):
    """플래그를 '0'으로 초기화 (중복 실행 방지)."""
    sb_client.table('app_config').upsert({'key': key, 'value': '0'}, on_conflict='key').execute()


def _manual_market_all():
    try:
        ok, fail = run_market(all_listed=True)
        logging.info(f"📡 [시장수집-전체] 완료: 성공 {ok}개, 실패 {fail}개")
    except Exception as _me:
        logging.error(f"❌ [시장수집-전체] 오류: {_me}")


def _manual_etf_collect():
    try:
        import collect_us_etf
        collect_us_etf.collect_and_save(days=90)
        logging.info("✅ [US ETF] 수집 완료")
    except Exception as _ee:
        logging.error(f"❌ [US ETF] 수집 오류: {_ee}")


def _manual_leading_stocks():
    try:
        from leading_stocks_generator import run as run_leading
        run_leading()
        logging.info("✅ [주도주] 수동 생성 완료")
    except Exception as _le:
        logging.error(f"❌ [주도주] 수동 생성 오류: {_le}")


def _manual_sector_summary():
    try:
        from collect_sector_summary import run as run_ss
        run_ss()
        logging.info("✅ [섹터요약] 수동 집계 완료")
    except Exception as _sse:
        logging.error(f"❌ [섹터요약] 수동 집계 오류: {_sse}")


_TS_FLAG_JOBS = [
    ('run_market_all_flag',     300, True,  True,  [("Thread-ManualMarketAll", _manual_market_all)]),
    ('run_macro_flag',          180, True,  False, [("Thread-ManualMacro",     lambda: job_collect_macro())]),
    ('etf_collect_flag',        300, False, False, [("Thread-EtfCollect",      _manual_etf_collect)]),
    ('run_flow_flag',           300, True,  False, [("Thread-ManualFlow",      lambda: job_collect_foreign_institution()),
                                                    ("Thread-ManualInvestor",  lambda: job_collect_investor_trend())]),
    ('run_leading_stocks_flag', 300, False, False, [("Thread-LeadingStocks",   _manual_leading_stocks)]),
    ('run_sector_summary_flag', 300, False, False, [("Thread-SectorSummary",   _manual_sector_summary)]),
]


def _run_watchdog_flags(threads: dict):
    """워치독 루프 1회 tick — 각종 앱 플래그 체크 및 배경 작업 트리거.
    main() while True 루프에서 매 60초마다 호출됨."""
    global _disclosure_running

    # ── reload_flag — 대시보드 종목 변경 시 봇 데이터 재로드 ──
    if _BRIDGE_OK:
        try:
            if _bridge.check_reload_flag():
                _sb_r     = _bridge._get_client()
                _prev_mon = getattr(_bridge, '_prev_mon_codes', set())

                from config import reload_company_data
                reload_company_data()
                logging.info("🔄 [Reload] 봇 종목 데이터 갱신 완료")
                # 산업 매핑 캐시 갱신 (프론트엔드 app_config 조회용)
                try:
                    _bridge.update_industry_map_cache()
                except Exception as _ice:
                    logging.debug(f"industry_map 캐시 갱신 오류 (무시): {_ice}")

                _all = _sb_r.table('companies').select('code,name') \
                           .eq('is_monitored', True).execute()
                _all_codes = {r['code'].split('.')[0]
                              for r in (_all.data or []) if r.get('code')}

                _added   = _all_codes - _prev_mon
                _removed = _prev_mon - _all_codes
                _bridge._prev_mon_codes = _all_codes

                # 제거된 종목 → 90일 초과 데이터 삭제
                if _removed:
                    _cutoff90 = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
                    _rm_list  = list(_removed)
                    for _i in range(0, len(_rm_list), 200):
                        _sb_r.table('market_data').delete() \
                             .lt('base_date', _cutoff90) \
                             .in_('stock_code', _rm_list[_i:_i+200]).execute()
                    logging.info(f"🗑️ [Reload] 제거 종목 {len(_removed)}개 — 90일 초과 데이터 삭제")

                # 신규 추가 종목 → 90일 시장 데이터 백필 (backfill_market_90d.run_for_codes 위임)
                if _added and _COLLECTOR_OK:
                    logging.info(f"📈 [Reload] 신규 모니터링 {len(_added)}개 — 90일 시장 데이터 백필 시작")
                    def _backfill_market(_codes):
                        try:
                            from backfill_market_90d import run_for_codes
                            run_for_codes(list(_codes))
                        except Exception as _be:
                            logging.error(f"❌ [Reload] 백필 오류: {_be}")
                    _start_daemon(_backfill_market, "Thread-MarketBackfill", (list(_added),))

                # 신규 종목 기업정보 자동수집
                if _added and _COLLECTOR_OK:
                    try:
                        _existing   = _sb_r.table('company_info').select('stock_code') \
                                           .in_('stock_code', list(_added)).execute()
                        _done       = {r['stock_code'] for r in (_existing.data or [])}
                        _new_codes  = list(_added - _done)
                        if _new_codes:
                            logging.info(f"📋 [기업정보] 신규 모니터링 종목 {len(_new_codes)}개 감지 → 자동수집")
                            def _collect_new():
                                try:
                                    _dart2 = __import__('OpenDartReader')(os.environ.get('DART_API_KEY',''))
                                    _sb3   = _bridge._get_client()
                                    for _c in _new_codes:
                                        try:
                                            _collect_company_one(_dart2, _sb3, _c, force=False)
                                            time.sleep(0.5)
                                        except Exception as _e:
                                            logging.error(f"  [{_c}] 기업정보 수집 오류: {_e}")
                                    logging.info(f"📋 [기업정보] 자동수집 완료 ({len(_new_codes)}개)")
                                except Exception as _fe:
                                    logging.error(f"❌ [기업정보] 자동수집 스레드 오류: {_fe}")
                            _start_daemon(_collect_new, "Thread-CompanyInfo")
                    except Exception as _cie:
                        logging.debug(f"기업정보 신규감지 오류: {_cie}")

                # 신규 종목 재무 데이터 자동수집
                if _added and _COLLECTOR_OK:
                    try:
                        _corp_res = _sb_r.table('companies').select('code,corp_code,name') \
                                         .eq('is_monitored', True).execute()
                        _new_corp_codes = [
                            r['corp_code'] for r in (_corp_res.data or [])
                            if r.get('corp_code') and r['code'].split('.')[0] in _added
                        ]
                        if _new_corp_codes:
                            logging.info(f"💰 [재무] 신규 종목 {len(_new_corp_codes)}개 — 2023년~현재 재무 수집 시작")
                            def _collect_fin_new(_corp_codes):
                                try:
                                    from collect_financials import run_by_corp_codes_all_history
                                    run_by_corp_codes_all_history(_corp_codes, from_year=2023, max_workers=2)
                                    logging.info(f"💰 [재무] 신규 종목 재무 수집 완료 ({len(_corp_codes)}개)")
                                except Exception as _fe2:
                                    logging.error(f"❌ [재무] 신규 종목 재무 수집 오류: {_fe2}")
                            _start_daemon(_collect_fin_new, "Thread-FinancialsNew", (_new_corp_codes,))
                    except Exception as _fie:
                        logging.debug(f"재무 신규감지 오류: {_fie}")

                if not _prev_mon:
                    _bridge._prev_mon_codes = _all_codes

        except Exception as _re:
            logging.debug(f"reload_flag 체크 오류: {_re}")

    # ── collect_company_info_request — 대시보드 DART 자동수집 버튼 ──
    if _BRIDGE_OK and _COLLECTOR_OK:
        try:
            _sb  = _bridge._get_client()
            _req = _sb.table('app_config').select('value') \
                      .eq('key', 'collect_company_info_request').single().execute()
            if _req.data and _req.data.get('value'):
                _req_data = json.loads(_req.data['value'])
                _req_code = _req_data.get('code', '')
                _req_time = _req_data.get('requested_at', '')
                if _req_code and _req_time:
                    _elapsed = (datetime.datetime.now(tz=datetime.timezone.utc) -
                               datetime.datetime.fromisoformat(_req_time.replace('Z','+00:00'))).total_seconds()
                    if _elapsed < 300:
                        logging.info(f"📋 [기업정보] 수동 수집 요청 감지: {_req_code}")
                        _dart = __import__('OpenDartReader')(os.environ.get('DART_API_KEY',''))
                        _collect_company_one(_dart, _sb, _req_code, force=True)
                        _sb.table('app_config').upsert(
                            {'key': 'collect_company_info_request', 'value': '{}'}, on_conflict='key').execute()
        except Exception as _ce:
            logging.debug(f"기업정보 수집 요청 체크 오류: {_ce}")

    # ── 수동 트리거 ts-flag 일괄 처리 (_TS_FLAG_JOBS 테이블 드리븐) ──
    if _BRIDGE_OK:
        for _fkey, _fwin, _fneed_col, _fholiday, _factions in _TS_FLAG_JOBS:
            if _fneed_col and not _COLLECTOR_OK:
                continue
            try:
                _sb_f     = _bridge._get_client()
                _felapsed = _read_ts_flag(_sb_f, _fkey)
                if _felapsed is None or not (0 < _felapsed < _fwin):
                    continue
                _clear_ts_flag(_sb_f, _fkey)
                if _fholiday and market_timer.is_kr_holiday():
                    logging.info(f"⏸ [{_fkey}] 주말/공휴일 — 수동 트리거 무시")
                    continue
                logging.info(f"📡 [{_fkey}] 수동 트리거 감지")
                for _tname, _tfn in _factions:
                    _start_daemon(_tfn, _tname)
            except Exception as _fe:
                logging.debug(f"{_fkey} 체크 오류: {_fe}")

    # ── run_disclosure_flag — 공시수집 즉시 실행 ──
    if _BRIDGE_OK:
        try:
            if _bridge.check_disclosure_flag() and not _disclosure_running:
                _disclosure_running = True
                logging.info("🔄 [공시수집] 수동 트리거 — job_collect_financials 즉시 실행")
                def _run_disclosure():
                    global _disclosure_running
                    try:
                        job_collect_financials()
                    finally:
                        _disclosure_running = False
                _start_daemon(_run_disclosure, "Thread-ManualDisclosure")
        except Exception as _de:
            logging.debug(f"disclosure_flag 체크 오류: {_de}")

    # ── pro_action_flag — 프로 채널 초대/퇴장/연장 ──
    if _PRO_OK:
        try:
            _pro.process_pro_action_flag()
        except Exception as _proe:
            logging.debug(f"pro_action_flag 체크 오류: {_proe}")

    # ── bot_requests — 대시보드 봇 요청 큐 (멤버 동기화·공지 발송) ──
    if _BOTREQ_OK:
        try:
            _botreq.process_bot_requests()
        except Exception as _bqe2:
            logging.debug(f"bot_requests 처리 오류: {_bqe2}")

    # ── 전체 봇 heartbeat 업데이트 ──
    if _BRIDGE_OK:
        try:
            for bot_name, key in [
                ("price_bot", "Thread-Price"), ("news_bot",  "Thread-News"),
                ("dart_bot",  "Thread-Dart"),  ("scheduler_bot", "Thread-Sched"),
            ]:
                if threads[key]["thread"].is_alive():
                    _bridge.heartbeat(bot_name)
        except Exception:
            pass
