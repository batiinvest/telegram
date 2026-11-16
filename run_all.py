"""
run_all.py — 통합 시스템 엔트리포인트
─────────────────────────────────────
물리 분할 (2026-07): 이 파일은 스케줄 등록 + 봇 스레드 기동 + 워치독 루프만 담당.
  - job_infra.py      : _job 데코레이터·결과기록·브로드캐스트·브릿지
  - jobs_collect.py   : 수집·집계 잡 본문
  - jobs_briefing.py  : 브리핑·발송·운영 잡 본문
  - watchdog_flags.py : 수동 트리거 플래그·재로드·bot_requests
"""
import sys
import time
import logging
import functools
import threading

# 로깅은 하위 모듈 import 전에 초기화 — import 시점 로그도 파일에 기록
from logger_config import setup_root_logger
setup_root_logger()   # logs/bati.log (RotatingFileHandler 10MB×5) + 콘솔

# ✅ 스케줄링 라이브러리
import schedule

# ✅ 기존 봇 모듈 임포트
try:
    from realtime_alert import KisMyStockScanner
    from news_main import NaverNewsBot
    from main import DartRoutingBot
except ImportError as e:
    print(f"❌ 필수 파일이 누락되었습니다: {e}")
    sys.exit(1)

import job_state
from job_infra import _bridge, _BRIDGE_OK, set_expected_jobs, wait_for_jobs
from jobs_collect import (
    job_sync_listed_companies, job_sync_listed_catchup, job_cleanup_market_data, job_collect_financials, job_backfill_financials,
    job_collect_macro, job_collect_analyst_opinions, job_collect_foreign_institution,
    job_collect_new_high, job_collect_us_etf, job_collect_market,
    job_collect_market_closing, job_short_surge, job_collect_investor_trend,
    job_collect_investor_trend_ranked,
    job_flow_empty, job_krx_buy, job_sio, job_us_index, job_us_liquidity, job_tw_revenue, job_jp_tw, job_ai_hw,
    job_sector_summary, job_collect_estimates, job_leading_stocks, job_market_summary,
    job_daily_summary, job_collect_credit_balance, job_collect_wics,
    job_collect_investor_market, job_collect_fear_greed, job_retry_failed, job_collect_active_etf, job_portfolio_nav,
    job_snapshot_qtr_consensus, job_earnings_surprise_briefing, job_earnings_reconcile, job_collect_exports,
    job_earnings_stage, job_collect_backlog,
)
from jobs_briefing import (
    job_lunch_briefing, job_naver_report, job_daily_closing, job_kind_ir,  # noqa: F401 — kind_ir 발송 중단, 복원용
    job_industry_summary,
    job_pro_channel_check, job_daily_ops_summary, job_disclosure_digest,
    job_saturday_main_ranking, job_saturday_flow_summary, job_saturday_industry_report,
    job_sunday_industry_recap, job_sunday_company_diagnosis,
)
from watchdog_flags import _run_watchdog_flags

# ✅ SMS 웹훅 서버 (입금 자동 처리)
try:
    import sms_webhook as _sms_wh
    _SMS_WH_OK = True
    logging.info("✅ [SMS] 웹훅 모듈 로드 완료")
except ImportError as _swe:
    _sms_wh = None
    _SMS_WH_OK = False
    logging.warning(f"⚠️ [SMS] sms_webhook 모듈 없음 (pip install flask): {_swe}")

# ✅ 봇 명령어 수신 (/myid, /status, /start)
# — getUpdates 중복 호출 방지: 실제 처리는 realtime_alert.telegram_listener가 위임 호출
try:
    import bot_commands as _bot_cmd
    _BOT_CMD_OK = True
    logging.info("✅ [BotCmd] 명령어 모듈 로드 완료")
except ImportError as _bce:
    _bot_cmd = None
    _BOT_CMD_OK = False
    logging.warning(f"⚠️ [BotCmd] 명령어 모듈 없음: {_bce}")


def run_scanner_bot():
    """실시간 시세 감시 및 챗봇 리스너"""
    try:
        logging.info("🚀 [시세감시 봇] 시작")
        scanner = KisMyStockScanner()
        scanner.run()
    except Exception as e:
        logging.error(f"❌ [시세감시 봇] 종료됨 (에러): {e}")


def run_news_bot():
    """네이버 뉴스 봇"""
    try:
        logging.info("🚀 [뉴스 봇] 시작")
        bot = NaverNewsBot()
        bot.run()
    except Exception as e:
        logging.error(f"❌ [뉴스 봇] 종료됨 (에러): {e}")


def run_dart_bot():
    """DART 공시 봇"""
    try:
        logging.info("🚀 [공시 봇] 시작")
        bot = DartRoutingBot()
        bot.run()
    except Exception as e:
        logging.error(f"❌ [공시 봇] 종료됨 (에러): {e}")


def _threaded(job_fn):
    """독립 잡을 백그라운드 스레드로 실행 — schedule은 단일 스레드 직렬이라
    긴 잡(전체 수집 ~10분, 브리핑 방 순회)이 후속 잡을 밀어내는 문제 완화.
    ⚠️ 수집 체인(시장수집→수급→섹터요약→신고가→주도주→시장요약)은 순서 의존이므로
    스레드화하지 않는다 — 직렬 실행이 곧 순서 보장.
    스레드로 뺀 잡끼리 순서가 필요하면 예약 시각 간격에 기대지 말고 _after로 '완료'에 묶는다.
    스레드를 띄우기 전에 진행 중으로 표시한다(job_state) — 뒤이어 뜨는 후행 잡이 이걸 보고 기다린다."""
    @functools.wraps(job_fn)
    def runner():
        name = job_fn.__name__
        began = job_state.enter(name)

        def _run():
            try:
                job_fn()
            finally:
                job_state.leave(name, began)
        try:
            threading.Thread(target=_run, name=f"Job-{name}", daemon=True).start()
        except Exception:
            job_state.leave(name)   # 스레드를 못 띄웠으면 '진행 중'으로 남기지 않는다
            raise
    return runner


_ORDER_DEPS: list = []   # (후행 잡, 선행 잡, 'HH:MM') — 등록 뒤 실제 스케줄과 대조(_check_order_deps)


def _after(job_fn, *deps, timeout_min: float = 120):
    """순서가 중요한 잡을 예약 시각이 아니라 선행 잡의 **완료**에 묶는다. _threaded 안쪽에 쓴다.

    deps = (선행 잡 함수, 'HH:MM' 그 잡의 예약 시각) … — 예약 시각이 되면 스레드가 뜨되, 오늘 그 시각에
    뜬 선행 잡이 끝날 때까지 본문을 미룬다. 선행 잡이 제때 끝나는 평소엔 예약 시각 그대로 돈다.
    선행 잡의 성공·실패는 가리지 않는다. timeout_min을 넘기거나 선행 잡이 뜬 흔적이 없으면(재시작으로
    예정 시각을 놓친 날) 기다리지 않고 실행한다 — '언젠가는 돈다'는 그대로.
    2026-11-13: 재무수집이 31분 걸려 19:05 재조정·19:10 어닝 브리핑이 동시에 시작, 브리핑이 4초 먼저 나갔다."""
    named = [(d.__name__, at) for d, at in deps]
    _ORDER_DEPS.extend((job_fn.__name__, n, at) for n, at in named)

    @functools.wraps(job_fn)
    def runner():
        wait_for_jobs(job_fn.__name__, named, timeout_min=timeout_min)
        job_fn()
    return runner


def _check_order_deps(jobs):
    """_after에 적은 (선행 잡, 시각)이 실제 등록된 스케줄과 맞는지 대조. 한쪽 시각만 고치면 후행 잡이
    선행 잡을 못 찾아 기다리지 않게 되므로(순서 보장이 조용히 사라짐) 시작할 때 바로 알린다."""
    registered = set()
    for j in jobs:
        name = getattr(getattr(j, 'job_func', None), '__name__', None)
        if name and getattr(j, 'at_time', None):
            registered.add((name, j.at_time.strftime('%H:%M')))
    bad = [(w, d, at) for w, d, at in _ORDER_DEPS if (d, at) not in registered]
    if bad:
        desc = ", ".join(f"{w} ← {d}({at})" for w, d, at in bad)
        logging.error(f"❌ [순서] _after의 선행 잡 시각이 스케줄과 다릅니다: {desc}")
        _notify_admin(f"❌ <b>[스케줄 설정 오류]</b> 선행 잡 시각 불일치 — 순서 보장이 풀려 있습니다.\n{desc}")
    else:
        logging.info(f"🔗 [순서] 완료에 묶인 잡 {len(_ORDER_DEPS)}쌍 확인")


def run_scheduler():
    logging.info("🚀 [스케줄러] 시작 (schedule 라이브러리 적용)")
    schedule.clear()  # 재시작 시 중복 job 방지 — 없으면 이미 지난 시각 job이 즉시 실행됨
    _ORDER_DEPS.clear()

    schedule.every().day.at("09:00").do(_threaded(job_pro_channel_check))  # 프로 채널 구독 만료 체크
    # KIND IR자료(09:05·18:10) 발송 중단 (2026-10-03) — kind.krx.co.kr이 08-13부터 이 서버 IP를
    # 403 차단(다른 KRX 사이트는 정상, 한국 IP는 정상). 한국 IP 경유 수단이 생기면 두 줄 복원:
    #   schedule.every().day.at("09:05").do(_threaded(job_kind_ir))
    #   schedule.every().day.at("18:10").do(_threaded(job_kind_ir))
    schedule.every().day.at("08:50").do(_threaded(job_naver_report))
    schedule.every().day.at("08:55").do(_threaded(job_collect_analyst_opinions))  # 투자의견 (장전)
    schedule.every().day.at("06:30").do(_threaded(job_collect_macro))             # 글로벌 매크로 수집 + 메인 채널 브리핑 (서머타임 05:00/겨울 06:00 마감 → 06:30 안전)
    schedule.every().day.at("06:35").do(_threaded(job_collect_us_etf))            # US ETF 수집 (미국 장 마감 직후)
    schedule.every().day.at("09:30").do(job_collect_market)            # 장 시작 모니터링 수집
    schedule.every().day.at("09:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ①
    schedule.every().day.at("11:25").do(job_collect_foreign_institution)  # 기관/외국인 수급 ②
    schedule.every().day.at("11:30").do(_threaded(job_lunch_briefing))
    schedule.every().day.at("12:00").do(job_collect_market)            # 점심 모니터링 수집
    schedule.every().day.at("13:25").do(job_collect_foreign_institution)  # 기관/외국인 수급 ③
    schedule.every().day.at("14:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ④
    schedule.every().day.at("15:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ⑤ (장 마감 최종)
    schedule.every().day.at("16:10").do(_threaded(job_collect_macro))  # 장 마감 후 매크로 수집
    schedule.every().day.at("16:20").do(_threaded(job_collect_us_etf)) # US ETF 수집 (미장 전일 종가)
    schedule.every().day.at("16:30").do(job_portfolio_nav)             # 투자노트 순자산·기준가 일별 (정규장 종가 수집 15:45 뒤)
    schedule.every().day.at("17:20").do(job_collect_new_high)          # 신고가 종목 수집 (장마감 확정 수집 17:00 이후 — market_data 기준)
    schedule.every().day.at("16:45").do(job_collect_investor_trend)    # 종목별 외국인·기관 순매수 확정 (sector_summary 전)
    schedule.every().day.at("18:15").do(job_collect_investor_trend_ranked)  # 수급 확정 정산 + 거래대금 상위 종목 확장 (18:30 브리핑 Top3를 시장 전체 기준으로)
    schedule.every().day.at("15:45").do(job_collect_market_closing)    # 장 마감 확정치 수집 (정규장 종가 직후·시간외 前 — 17:00은 시간외단일가라 종가 오염됨. 외국인 순매수는 별도 잡)
    schedule.every().day.at("17:05").do(job_short_surge)               # 공매도 수집 + 5일 평균 대비 2배 급증 알림
    schedule.every().day.at("17:15").do(job_sector_summary)            # 산업별 일별 요약 집계
    schedule.every().day.at("19:45").do(job_sector_summary)            # 산업집계 재계산 (19:30 수급 확정 후 — 당일 최종 반영)
    schedule.every().day.at("19:00").do(_threaded(job_disclosure_digest))  # 오늘 주요·긴급 공시 다이제스트 → 메인 채널
    schedule.every().day.at("19:25").do(job_retry_failed)              # 실패 잡 자동 재처리 (job_runs 기반, 멱등 잡만)
    schedule.every().day.at("19:35").do(_threaded(job_collect_active_etf))  # 액티브 ETF 구성종목(KRX PDF, 로그인) — 빠진 날 채움
    schedule.every().day.at("17:30").do(job_leading_stocks)            # 주도주 탐색기 스코어 계산
    schedule.every().day.at("18:20").do(job_collect_investor_market)   # KIS 시장별 투자자매매동향 (장마감 확정 후, 시황 카드)
    schedule.every().day.at("18:22").do(job_collect_fear_greed)        # 한국 피어앤그리드 원재료 (옵션 풋/콜은 당일 값만 → 거래일 저녁 필수)
    schedule.every().day.at("18:30").do(job_market_summary)            # 투자포인트 요약 생성 (18:15 수급 확정 후)
    schedule.every().day.at("18:00").do(_threaded(job_naver_report))
    schedule.every().day.at("18:30").do(_threaded(job_daily_closing)) # 마감 브리핑 — 18:30 재무수집·시장요약과 병렬
    # 봇 시작 시 초기 재무 데이터 수집 (최초 1회만)
    # job_initial_financials 제거 — 필요시 수동 실행
    logging.info("🚀 [초기수집] 백그라운드 스레드 시작")

    schedule.every().saturday.at("00:30").do(job_cleanup_market_data)   # 새벽 market_data 정리
    schedule.every().saturday.at("01:00").do(job_sync_listed_companies) # 새벽 상장사 동기화
    schedule.every().sunday.at("01:00").do(job_sync_listed_catchup)     # 토요일 동기화 실패(주로 DART 점검) 시에만 재실행
    schedule.every().saturday.at("02:00").do(_threaded(job_backfill_financials))  # 실적시즌 재무 전수 백필(스킵-기존)
    # ── 저녁 재무·실적 묶음 (2026-11-13 수리) ──────────────────────────────────────────
    # 재무수집(평일 4~10분, 실적 마감 주간 30~80분)·추정실적(매일 약 9분)·신용잔고(2~6분)가 스레드 없이
    # 스케줄 루프를 붙잡아 18:35~19:10 잡이 밀리고 한꺼번에 시작했다 → 셋 다 스레드로 빼고,
    # 순서가 필요한 잡은 _after로 선행 잡의 '완료'에 묶는다(예약 시각은 '가장 이른 시작'일 뿐).
    schedule.every().day.at("18:30").do(_threaded(job_collect_financials))  # 장 마감 후 재무수집 (공시 기반) — 끝에 daily_disclosures 저장
    schedule.every().day.at("18:55").do(_threaded(_after(job_daily_summary, (job_collect_financials, "18:30"))))  # 종목별 저녁 요약 — 재무수집(공시 저장)이 끝난 뒤
    schedule.every().day.at("19:10").do(_threaded(_after(job_industry_summary, (job_daily_summary, "18:55"))))  # 산업별 마감 브리핑 → 산업 채팅방 — 종목 요약이 끝난 뒤
    schedule.every().day.at("18:35").do(_threaded(job_collect_analyst_opinions))  # 투자의견 (장후)
    schedule.every().day.at("18:40").do(_threaded(job_collect_estimates))  # 종목추정실적 (미래 매출/영업이익 + 상향감지) — KIS 약 9분
    schedule.every().day.at("18:50").do(_threaded(_after(job_flow_empty, (job_krx_buy, "18:42"), (job_collect_estimates, "18:40"), timeout_min=20)))  # 전종목 수급 정산(30일)+빈집 판정 — 25~35분. 당일 매수대금·추정실적(KIS)이 끝난 뒤
    schedule.every().day.at("16:02").do(_threaded(job_sio))  # SIO 과매도·과매수(코스피200·코스닥150·코스피대형주) — 15:45 전 종목 마감 수집(~15:55) 뒤
    schedule.every().day.at("18:46").do(_threaded(job_sio))  # 한 번 더 — 수집값이 고쳐졌으면 반영(upsert)
    schedule.every().day.at("18:42").do(_threaded(job_krx_buy))  # KRX 당일 투자자별 매수대금 → market_data (18:50 컨셉 계산 앞, 약 10초)
    schedule.every().day.at("20:40").do(_threaded(job_krx_buy))  # 한 번 더 — 시간외·대량매매 반영 뒤 값 (다른 행만 씀)
    schedule.every().day.at("06:50").do(_threaded(job_us_index))  # 미국 지수·ETF 종가 → 미국 위험신호·피어앤그리드 원재료 (06:35 US ETF 수집 뒤, 약 5초)
    schedule.every().day.at("08:10").do(_threaded(job_us_index))  # 한 번 더 — 야후 값이 늦게 굳은 날 (다른 날만 씀)
    schedule.every().day.at("07:10").do(_threaded(job_us_liquidity))  # 미국 유동성 체크(FRED 15종, 약 15초) — 금리 갱신(미국 16:15) 뒤
    schedule.every().day.at("12:10").do(_threaded(job_us_liquidity))  # 한 번 더 — 아침 실패·늦은 갱신 대비 (같은 날은 덮어씀)
    schedule.every().day.at("21:10").do(_threaded(job_tw_revenue))  # 대만 AI·반도체 공급망 월매출(MOPS, 매달 10일까지 신고) — 최근 3개월 재확인, 약 1분
    schedule.every().day.at("16:40").do(_threaded(job_jp_tw))  # 일본·대만 공급망 97종목 시세 점검 — 일본 15:30·대만 14:30 마감 뒤(약 40초)
    schedule.every().day.at("18:20").do(_threaded(job_jp_tw))  # 한 번 더 — 야후 값이 늦게 굳은 날 (같은 기준일은 덮어씀)
    schedule.every().day.at("07:00").do(_threaded(job_ai_hw))  # AI 하드웨어 41종목 Breadth·피크아웃 점검 — 미국 마감 뒤(약 20초)
    schedule.every().day.at("08:20").do(_threaded(job_ai_hw))  # 한 번 더 — 야후 값이 늦게 굳은 날 (같은 기준일은 덮어씀)
    schedule.every().day.at("08:40").do(_threaded(job_snapshot_qtr_consensus))  # 분기 컨센 스냅샷 (어닝 서프라이즈용, 장전)
    schedule.every().day.at("19:05").do(_threaded(_after(job_earnings_reconcile, (job_collect_financials, "18:30"))))  # 재무 확정치 기반 어닝 서프라이즈/영업익 폭증 재조정 — 재무수집이 끝난 뒤
    schedule.every().day.at("19:10").do(_threaded(_after(job_earnings_surprise_briefing, (job_earnings_reconcile, "19:05"))))  # 어닝 서프라이즈 리스트 → 메인채널 — 재조정이 끝난 뒤에만
    schedule.every().day.at("19:00").do(_threaded(job_collect_credit_balance))  # KOFIA 신용공여 잔고 — 직전 영업일분이 당일 오후 발표(2026-07-22 실측: 10:30엔 없고 18:18엔 있음)
    schedule.every().day.at("10:30").do(_threaded(job_collect_credit_balance))  # 보정 실행 — 전날 발표가 19:00 이후로 밀린 경우 회수(멱등 upsert, 신규 기준일만 발송)
    schedule.every().day.at("22:00").do(_threaded(job_collect_wics))  # WICS 업종 분류(네이버) — 장 끝난 뒤(영업일 08:20엔 0종), 재시도 대기가 있어 스레드
    schedule.every().day.at("10:10").do(_threaded(job_collect_exports))  # 관세청 품목·국가·시군구 수출(최근 3개월, 15일경 전월 확정) — 약 600회 호출
    schedule.every().day.at("20:10").do(_threaded(_after(job_earnings_stage, (job_collect_financials, "18:30"))))  # 실적 국면 전 종목 재계산 — 재무수집이 끝난 뒤(마감일엔 20:10을 넘길 수 있다), 약 20초
    schedule.every().day.at("20:20").do(_threaded(job_collect_backlog))  # 반도체 장비 수주잔고(최근 7일 정기보고서, 없는 분기·정정만)
    schedule.every().day.at("07:40").do(_threaded(job_earnings_stage))  # 실적 국면 보정 — 마감일 재무수집이 자정 넘겨 끝난 분·토요일 02:00 백필 반영
    schedule.every().day.at("19:50").do(job_daily_ops_summary)        # 일일 운영 요약 (잡 성공/실패/소요시간) → 관리자 방
    schedule.every().saturday.at("10:00").do(job_saturday_main_ranking)
    schedule.every().saturday.at("10:30").do(job_saturday_industry_report)
    schedule.every().saturday.at("11:00").do(job_saturday_flow_summary)   # 주간 수급 요약
    schedule.every().sunday.at("10:00").do(job_sunday_industry_recap)
    schedule.every().sunday.at("10:30").do(job_sunday_company_diagnosis)

    # 등록된 잡 목록 스냅샷 — 19:50 운영요약의 '미실행' 판정 기준
    set_expected_jobs(schedule.get_jobs())
    _check_order_deps(schedule.get_jobs())

    loop_count = 0
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)

            # ✅ [추가] 60초마다 heartbeat 전송
            loop_count += 1
            if loop_count % 60 == 0 and _BRIDGE_OK:
                try:
                    _bridge.heartbeat("scheduler_bot")
                except Exception:
                    pass

        except Exception as e:
            logging.error(f"❌ 스케줄러 실행 중 에러: {e}")
            time.sleep(60)


def _notify_admin(msg: str):
    """관리자 방으로 운영 알림 발송. admin_chat_id 미설정이면 로그만 (공개 채널 폴백 없음)."""
    try:
        import stock_api
        from telegram_utils import get_admin_chat_id
        target = get_admin_chat_id()
        if target:
            stock_api.send_telegram(target, msg)
        else:
            logging.info(f"[관리자알림] admin_chat_id 미설정 — 스킵: {msg[:80]}")
    except Exception as e:
        logging.debug(f"[관리자알림] 발송 실패 (무시): {e}")


def _git_commit_short() -> str:
    """현재 배포 커밋 해시 (재시작 알림용). 실패 시 빈 문자열."""
    try:
        import subprocess, os
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def restart_thread(target_func, name):
    logging.warning(f"🔄 [{name}] 재시작을 시도합니다...")
    new_thread = threading.Thread(target=target_func, name=name, daemon=True)
    new_thread.start()
    logging.info(f"✅ [{name}] 재시작 성공!")
    # 스레드가 죽어 워치독이 되살린 건 장애 신호 — 관리자에게 즉시 알림
    _notify_admin(f"⚠️ <b>[스레드 재시작]</b> {name} 응답 없음 → 워치독이 재기동했습니다.")
    return new_thread


def main():
    logging.info("📢 바티인베스트 통합 시스템 가동 (Schedule Mode)")

    t_scanner   = threading.Thread(target=run_scanner_bot, name="Thread-Price", daemon=True)
    t_news      = threading.Thread(target=run_news_bot,    name="Thread-News",  daemon=True)
    t_dart      = threading.Thread(target=run_dart_bot,    name="Thread-Dart",  daemon=True)
    t_scheduler = threading.Thread(target=run_scheduler,   name="Thread-Sched", daemon=True)

    # SMS 웹훅 서버 (입금 자동 처리)
    if _SMS_WH_OK and getattr(_sms_wh, '_FLASK_OK', False):
        _sms_wh.start_thread()
        logging.info("🌐 [SMS] 웹훅 서버 기동 (포트 5001)")
    else:
        # 기동 로그만 찍고 스레드는 즉시 죽던 구간 — 미사용이면 스폰 자체를 생략
        logging.info("⏸ [SMS] 웹훅 비활성 (flask 미설치 — 현재 미사용)")

    # 봇 명령어 수신은 realtime_alert.py의 telegram_listener에서 처리
    # (getUpdates 중복 호출 방지 — bot_commands._handle을 직접 호출하는 방식으로 통합)

    threads = {
        "Thread-Price": {"thread": t_scanner,   "target": run_scanner_bot},
        "Thread-News":  {"thread": t_news,       "target": run_news_bot},
        "Thread-Dart":  {"thread": t_dart,       "target": run_dart_bot},
        "Thread-Sched": {"thread": t_scheduler,  "target": run_scheduler},
    }

    for name, info in threads.items():
        info["thread"].start()
        time.sleep(2)

    # 프로세스 기동 알림 — systemd 재시작·배포 확인용 (관리자 방 전용, 공개 채널 발송 없음)
    _commit = _git_commit_short()
    _notify_admin(
        f"🔄 <b>[시스템 재시작]</b> bati_bot 기동 완료\n"
        f"🕐 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        + (f" · <code>{_commit}</code>" if _commit else "")
        + f"\n🧵 스레드 {len(threads)}개 기동 (시세·뉴스·공시·스케줄러)"
    )

    while True:
        try:
            time.sleep(60)

            for name, info in threads.items():
                if not info["thread"].is_alive():
                    logging.error(f"💀 [{name}] 응답 없음! 심폐소생술 실시.")
                    info["thread"] = restart_thread(info["target"], name)

            # 기존 파일 heartbeat 유지
            with open("heartbeat.txt", "w") as f:
                f.write(str(time.time()))

            _run_watchdog_flags(threads)

        except KeyboardInterrupt:
            logging.info("🛑 시스템 종료 요청 받음. 종료합니다.")
            break
        except Exception as e:
            logging.error(f"❌ 메인 루프 에러: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
