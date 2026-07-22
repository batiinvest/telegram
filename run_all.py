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

from job_infra import _bridge, _BRIDGE_OK
from jobs_collect import (
    job_sync_listed_companies, job_cleanup_market_data, job_collect_financials,
    job_collect_macro, job_collect_analyst_opinions, job_collect_foreign_institution,
    job_collect_new_high, job_collect_us_etf, job_collect_market,
    job_collect_market_closing, job_short_surge, job_collect_investor_trend,
    job_collect_investor_trend_ranked,
    job_sector_summary, job_collect_estimates, job_leading_stocks, job_market_summary,
    job_collect_credit_balance,
    job_collect_investor_market, job_retry_failed,
)
from jobs_briefing import (
    job_lunch_briefing, job_naver_report, job_daily_closing, job_kind_ir,
    job_pro_channel_check, job_daily_ops_summary,
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
    스레드화하지 않는다 — 직렬 실행이 곧 순서 보장."""
    @functools.wraps(job_fn)
    def runner():
        threading.Thread(target=job_fn, name=f"Job-{job_fn.__name__}", daemon=True).start()
    return runner


def run_scheduler():
    logging.info("🚀 [스케줄러] 시작 (schedule 라이브러리 적용)")
    schedule.clear()  # 재시작 시 중복 job 방지 — 없으면 이미 지난 시각 job이 즉시 실행됨

    schedule.every().day.at("09:00").do(_threaded(job_pro_channel_check))  # 프로 채널 구독 만료 체크
    schedule.every().day.at("09:05").do(_threaded(job_kind_ir))            # KIND IR자료 오전 수집
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
    schedule.every().day.at("17:20").do(job_collect_new_high)          # 신고가 종목 수집 (장마감 확정 수집 17:00 이후 — market_data 기준)
    schedule.every().day.at("16:45").do(job_collect_investor_trend)    # 종목별 외국인·기관 순매수 확정 (sector_summary 전)
    schedule.every().day.at("18:15").do(job_collect_investor_trend_ranked)  # 수급 확정 정산 + 거래대금 상위 종목 확장 (18:30 브리핑 Top3를 시장 전체 기준으로)
    schedule.every().day.at("17:00").do(job_collect_market_closing)    # 장 마감 확정치 수집 (외국인 집계 완료 후)
    schedule.every().day.at("17:05").do(job_short_surge)               # 공매도 수집 + 5일 평균 대비 2배 급증 알림
    schedule.every().day.at("17:15").do(job_sector_summary)            # 산업별 일별 요약 집계
    schedule.every().day.at("19:45").do(job_sector_summary)            # 산업집계 재계산 (19:30 수급 확정 후 — 당일 최종 반영)
    schedule.every().day.at("19:25").do(job_retry_failed)              # 실패 잡 자동 재처리 (job_runs 기반, 멱등 잡만)
    schedule.every().day.at("17:30").do(job_leading_stocks)            # 주도주 탐색기 스코어 계산
    schedule.every().day.at("18:20").do(job_collect_investor_market)   # KIS 시장별 투자자매매동향 (장마감 확정 후, 시황 카드)
    schedule.every().day.at("18:30").do(job_market_summary)            # 투자포인트 요약 생성 (18:15 수급 확정 후)
    schedule.every().day.at("18:00").do(_threaded(job_naver_report))
    schedule.every().day.at("18:10").do(_threaded(job_kind_ir))       # KIND IR자료 오후 수집
    schedule.every().day.at("18:30").do(_threaded(job_daily_closing)) # 마감 브리핑 — 18:30 재무수집·시장요약과 병렬
    # 봇 시작 시 초기 재무 데이터 수집 (최초 1회만)
    # job_initial_financials 제거 — 필요시 수동 실행
    logging.info("🚀 [초기수집] 백그라운드 스레드 시작")

    schedule.every().saturday.at("00:30").do(job_cleanup_market_data)   # 새벽 market_data 정리
    schedule.every().saturday.at("01:00").do(job_sync_listed_companies) # 새벽 상장사 동기화
    schedule.every().day.at("18:30").do(job_collect_financials)        # 장 마감 후 재무수집 (공시 기반)
    schedule.every().day.at("18:35").do(_threaded(job_collect_analyst_opinions))  # 투자의견 (장후)
    schedule.every().day.at("18:40").do(job_collect_estimates)        # 종목추정실적 (미래 매출/영업이익 + 상향감지)
    schedule.every().day.at("19:00").do(job_collect_credit_balance)    # KOFIA 신용공여 잔고 — 직전 영업일분이 당일 오후 발표(2026-07-22 실측: 10:30엔 없고 18:18엔 있음)
    schedule.every().day.at("10:30").do(job_collect_credit_balance)    # 보정 실행 — 전날 발표가 19:00 이후로 밀린 경우 회수(멱등 upsert, 신규 기준일만 발송)
    schedule.every().day.at("19:50").do(job_daily_ops_summary)        # 일일 운영 요약 (잡 성공/실패/소요시간) → 관리자 방
    schedule.every().saturday.at("10:00").do(job_saturday_main_ranking)
    schedule.every().saturday.at("10:30").do(job_saturday_industry_report)
    schedule.every().saturday.at("11:00").do(job_saturday_flow_summary)   # 주간 수급 요약
    schedule.every().sunday.at("10:00").do(job_sunday_industry_recap)
    schedule.every().sunday.at("10:30").do(job_sunday_company_diagnosis)

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
    if _SMS_WH_OK:
        _sms_wh.start_thread()
        logging.info("🌐 [SMS] 웹훅 서버 기동 (포트 5001)")

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
