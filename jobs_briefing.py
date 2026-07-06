"""
jobs_briefing.py — 브리핑·발송·운영 잡
──────────────────────────────────────
run_all.py 물리 분할 (2026-07): 점심/마감 브리핑, 리포트, KIND IR, 프로채널,
주말 랭킹, 일일 운영 요약. 공통 인프라는 job_infra.py.
"""
import logging
import datetime

import stock_api
from telegram_utils import get_admin_chat_id as _get_admin_chat_id
from config import DEFAULT_CHAT_ID, COMMON_BUTTON
from job_infra import (_job, _log_notice, _bridge, _JOB_RESULTS,
                       _broadcast_to_industries, _broadcast_to_companies)

# ✅ 프로 채널 관리 모듈
try:
    import pro_channel as _pro
    _PRO_OK = True
    logging.info("✅ [Pro] 프로 채널 관리 모듈 로드 완료")
except ImportError as _pe:
    _pro = None
    _PRO_OK = False
    logging.warning(f"⚠️ [Pro] pro_channel 모듈 없음 (무시): {_pe}")

# ✅ KIND IR자료 수집 모듈
try:
    import kind_ir as _kind_ir
    _KIND_IR_OK = True
    logging.info("✅ [KIND IR] 모듈 로드 완료")
except ImportError as _kie:
    _kind_ir = None
    _KIND_IR_OK = False
    logging.warning(f"⚠️ [KIND IR] kind_ir 모듈 없음 (무시): {_kie}")


@_job("lunch", holiday=True)
def job_lunch_briefing():
    logging.info("🍱 [점심 브리핑] 시작")

    _log_notice(DEFAULT_CHAT_ID, "[점심 브리핑] 시작")

    # 관리종목 시세 1회 배치 조회 → 전광판·유니버스·테마 공유 (KIS 중복 호출 제거)
    prices = stock_api.get_universe_price_map()

    # 인사 + 시장 전광판 + 유니버스 랭킹을 한 메시지로 통합 (알림 3건 → 1건)
    intro = "🍱 <b>[점심 시황]</b> 맛점하세요! 오전 장 요약입니다."
    msg = f"{intro}\n\n{stock_api.get_market_scoreboard(shared_prices=prices)}\n\n{stock_api.get_universe_ranking(shared_prices=prices)}"
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg)

    _broadcast_to_industries(
        stock_api.get_industry_theme_ranking, prices,
        keyboard=COMMON_BUTTON, label="점심 브리핑"
    )


@_job("report", holiday=True)
def job_naver_report():
    logging.info("📑 [네이버 리포트] 발송 시작")
    try:
        stock_api.run_naver_report_job()
        _log_notice(DEFAULT_CHAT_ID, "[네이버 리포트] 발송")
    except Exception as e:
        logging.error(f"네이버 리포트 발송 에러: {e}")


@_job("closing", holiday=True)
def job_daily_closing():
    logging.info("🏁 [마감 브리핑] 시작")

    _log_notice(DEFAULT_CHAT_ID, "[마감 브리핑] 시작")

    # 관리종목 시세 1회 배치 조회 → 전광판·유니버스·테마 공유 (KIS 중복 호출 제거)
    prices = stock_api.get_universe_price_map()

    # 마감 무버 촉매 태그 + 시장폭 계산 — 실패해도 브리핑은 진행
    try:
        _sbc = _bridge._get_client()
        _tags = stock_api.get_catalyst_tags(_sbc)
        _breadth = stock_api.get_market_breadth(_sbc)
        _flow = stock_api.get_flow_map(_sbc)
        _daily_flow = stock_api.get_daily_flow_summary(_sbc)
    except Exception as _te:
        logging.error(f"[마감] 촉매태그/시장폭/수급 오류: {_te}")
        _tags = None
        _breadth = None
        _flow = None
        _daily_flow = None

    # 인사 + 시장 전광판(시장폭) + 유니버스(촉매태그) 랭킹을 한 메시지로 통합 (알림 1건)
    intro = "🏁 <b>[마감 시황]</b> 오늘 하루 고생 많으셨습니다."
    msg = f"{intro}\n\n{stock_api.get_market_scoreboard(shared_prices=prices, breadth=_breadth)}\n\n{stock_api.get_universe_ranking(shared_prices=prices, tag_map=_tags)}"
    if _daily_flow:
        msg += f"\n\n{_daily_flow}"
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg, keyboard=COMMON_BUTTON)

    _broadcast_to_industries(
        stock_api.get_industry_theme_ranking, prices,
        keyboard=COMMON_BUTTON, label="마감 브리핑"
    )
    _broadcast_to_companies(
        stock_api.get_stock_detail, _flow,
        keyboard=COMMON_BUTTON, label="마감 브리핑"
    )


@_job("kind_ir")
def job_kind_ir():
    """매일 2회 (09:05, 18:05) — KIND IR자료실 수집 → @batiarchive 전송"""
    if not _KIND_IR_OK:
        return
    try:
        logging.info("📋 [KIND IR] 수집 시작")
        _kind_ir.run_kind_ir_job()
    except Exception as e:
        logging.error(f"❌ [KIND IR] 오류: {e}")


@_job()
def job_pro_channel_check():
    """매일 09:00 — 프로 채널 구독 만료 멤버 퇴장 + D-3 예고 알림"""
    if not _PRO_OK:
        return
    try:
        logging.info("🔐 [프로채널] 구독 만료 체크 시작")
        result = _pro.check_expired()
        logging.info(
            f"🔐 [프로채널] 완료 — "
            f"퇴장 {len(result['kicked'])}명 / "
            f"예고 {len(result['notified'])}명 / "
            f"오류 {len(result['errors'])}건"
        )
    except Exception as e:
        logging.error(f"❌ [프로채널] 만료 체크 오류: {e}")


@_job()
def job_daily_ops_summary():
    """매일 19:50 — 오늘 잡 실행 결과 요약을 관리자 방으로 발송 (운영 가시성)."""
    today = datetime.date.today().isoformat()
    rows = [(k, v) for k, v in sorted(_JOB_RESULTS.items()) if v.get('date') == today]
    if not rows:
        return
    fails = [(k, v) for k, v in rows if not v['ok']]
    slows = [(k, v) for k, v in rows if v['ok'] and v['elapsed'] >= 300]
    lines = [f"🗒 <b>[운영 요약] {today}</b> — 실행 {len(rows)}개 / 실패 {len(fails)}개"]
    for k, v in fails:
        lines.append(f"❌ {v['time']} {k}: {v['error']}")
    for k, v in slows:
        lines.append(f"🐢 {v['time']} {k}: {v['elapsed']:.0f}초 소요")
    if not fails and not slows:
        lines.append("✅ 전 잡 정상")
    target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)
    stock_api.send_telegram(target, "\n".join(lines))


@_job("saturday")
def job_saturday_main_ranking():
    logging.info("🏆 [주간 랭킹] 메인방 발송 시작")
    try:
        msg = stock_api.get_weekly_universe_ranking()
        stock_api.send_telegram(DEFAULT_CHAT_ID, msg, keyboard=COMMON_BUTTON)
        _log_notice(DEFAULT_CHAT_ID, "[토요일] 주간 랭킹 발송")
    except Exception as e:
        logging.error(f"주간 메인 랭킹 에러: {e}")


@_job("saturday")
def job_saturday_flow_summary():
    """토요일 — 주간 외국인·기관 순매수 누적 Top/Bottom 5 메인채널 발송"""
    logging.info("💰 [주간수급] 발송 시작")
    try:
        sb = _bridge._get_client()
        msg = stock_api.get_weekly_flow_summary(sb)
        stock_api.send_telegram(DEFAULT_CHAT_ID, msg, keyboard=COMMON_BUTTON)
        _log_notice(DEFAULT_CHAT_ID, "[토요일] 주간 수급 요약 발송")
    except Exception as e:
        logging.error(f"❌ [주간수급] 오류: {e}")


@_job("saturday")
def job_saturday_industry_report():
    logging.info("🏭 [주간 산업 리포트] 각 산업방 발송 시작")
    _broadcast_to_industries(
        stock_api.get_industry_weekly_ranking,
        sleep=1.0, keyboard=COMMON_BUTTON, label="주간 산업 리포트"
    )


@_job("sunday")
def job_sunday_industry_recap():
    logging.info("🗓 일요일 산업별 시총 리포트 발송 시작")
    _broadcast_to_industries(
        stock_api.get_industry_cap_ranking,
        sleep=2.0, label="일요일 시총 리포트"
    )


@_job("sunday")
def job_sunday_company_diagnosis():
    logging.info("🗓 일요일 종목별 기술적 진단 발송 시작")
    _broadcast_to_companies(
        stock_api.get_stock_chart,
        sleep=2.0, label="일요일 기술적 진단"
    )
