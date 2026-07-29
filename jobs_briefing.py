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
                       _broadcast_to_industries, _broadcast_to_companies,
                       mark_failed, get_missing_jobs)
from managers import pop_send_failures

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
        mark_failed(e)


@_job("closing", holiday=True)
def job_daily_closing():
    logging.info("🏁 [마감 브리핑] 시작")

    _log_notice(DEFAULT_CHAT_ID, "[마감 브리핑] 시작")

    # 관리종목 시세 1회 배치 조회 → 전광판·유니버스·테마 공유 (KIS 중복 호출 제거)
    prices = stock_api.get_universe_price_map()

    # 마감 무버 촉매 태그 + 시장폭 계산 — 실패해도 브리핑은 진행
    # (_sbc 선초기화 — 클라이언트 획득 실패 시에도 아래 섹션들이 NameError 없이 스킵되도록)
    _sbc = None
    try:
        _sbc = _bridge._get_client()
        _tags = stock_api.get_catalyst_tags(_sbc)
        _breadth = stock_api.get_market_breadth(_sbc)
        _flow = stock_api.get_flow_map(_sbc)
        _daily_flow = stock_api.get_daily_flow_summary(_sbc)
        _uni_flow = stock_api.get_daily_flow_summary(_sbc, monitored_only=True)
    except Exception as _te:
        logging.error(f"[마감] 촉매태그/시장폭/수급 오류: {_te}")
        _tags = None
        _breadth = None
        _flow = None
        _daily_flow = None
        _uni_flow = None
        mark_failed(_te)

    # 마감 브리핑은 2개 메시지로 분리 — ①시장 전체 시황 ②관심종목.
    # 지수·시장폭·투자자별 순매수·수급 Top3는 시장 전체 데이터, 섹터 랭킹·유니버스
    # 랭킹은 관심종목 시총가중/등락률이라 출처 기준으로 갈랐다.
    try:
        _investor = stock_api.get_market_investor_summary()
    except Exception as _ie:
        logging.error(f"[마감] 투자자동향 오류: {_ie}")
        _investor = ""
        mark_failed(_ie)

    # ① 시장 전체 시황 — 판단(요약) 먼저, 근거 데이터 뒤
    intro = "🏁 <b>[마감 시황]</b> 오늘 하루 고생 많으셨습니다."
    msg = intro
    try:
        _judgment = stock_api.get_market_judgment_summary(_sbc)
    except Exception as _je:
        logging.error(f"[마감] 시장판단 오류: {_je}")
        _judgment = ""
        mark_failed(_je)
    if _judgment:
        msg += f"\n\n{_judgment}"
    msg += "\n\n" + stock_api.get_market_scoreboard(
        shared_prices=prices, breadth=_breadth, section="index")
    if _investor:
        msg += f"\n\n{_investor}"
    if _daily_flow:
        msg += f"\n\n{_daily_flow}"
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg)

    # ② 관심종목
    msg2 = "🎯 <b>[관심종목 마감]</b> 바티인베스트가 보는 종목들입니다.\n\n"
    msg2 += stock_api.get_market_scoreboard(shared_prices=prices, section="sector")
    msg2 += f"\n\n{stock_api.get_universe_ranking(shared_prices=prices, tag_map=_tags)}"
    try:
        _leaders = stock_api.get_leading_stocks_summary(_sbc)
    except Exception as _le:
        logging.error(f"[마감] 주도주 오류: {_le}")
        _leaders = ""
        mark_failed(_le)
    if _leaders:
        msg2 += f"\n\n{_leaders}"
    if _uni_flow:
        msg2 += f"\n\n{_uni_flow}"
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg2, keyboard=COMMON_BUTTON)

    _broadcast_to_industries(
        stock_api.get_industry_theme_ranking, prices, _flow,
        keyboard=COMMON_BUTTON, label="마감 브리핑"
    )
    # 종목 시세 상세는 저녁요약(daily_summary.broadcast, 18:55)에 통합 발송 → 여기선 제거


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
        mark_failed(e)


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
        mark_failed(e)


def _hhmm(ts) -> str:
    """job_runs.finished_at(timestamptz)은 PostgREST가 UTC로 돌려준다 — KST로 변환.
    구: 문자열을 그대로 잘라 써서 요약 시각이 9시간 밀려 표시됐다."""
    try:
        return datetime.datetime.fromisoformat(ts).astimezone().strftime('%H:%M')
    except Exception:
        return (ts or '')[11:16]


@_job("disclosure_digest", holiday=True)
def job_disclosure_digest():
    """매일 19:00 — 오늘 발송한 주요·긴급 공시를 카테고리별로 요약해 메인 채널 발송.
    실적/계약/청약/상장폐지는 원문 재파싱으로 핵심 수치 enrich (disclosure_digest.py).
    주요·긴급 공시가 없는 날(휴장 등)은 발송 생략."""
    import disclosure_digest
    msg = disclosure_digest.generate(_bridge._get_client())
    if not msg:
        logging.info("📭 [공시 다이제스트] 오늘 주요·긴급 공시 없음 — 발송 생략")
        return
    if stock_api.send_telegram(DEFAULT_CHAT_ID, msg):
        _log_notice(DEFAULT_CHAT_ID, "[공시 다이제스트] 메인 발송")
        logging.info("🌆 [공시 다이제스트] 메인 채널 발송 완료")


@_job()
def job_daily_ops_summary():
    """매일 19:50 — 오늘 잡 실행 결과 요약을 관리자 방으로 발송 (운영 가시성).
    job_runs(DB)가 1순위 — 재시작해도 하루치가 보존됨. 미생성이면 인메모리 폴백.
    실행분뿐 아니라 **아예 안 돈 잡**(스케줄 누락·스케줄러 정지)과 텔레그램 발송
    영구실패 채널도 함께 보고한다."""
    today = datetime.date.today().isoformat()
    runs = {}   # job_name → 마지막 실행 {'time','ok','error','elapsed','recovered'}
    try:
        db_rows = (_bridge._get_client().table('job_runs')
                   .select('job_name,ok,error,elapsed_sec,finished_at')
                   .eq('run_date', today).order('finished_at').execute().data or [])
        for r in db_rows:
            prev = runs.get(r['job_name'])
            runs[r['job_name']] = {
                'time':      _hhmm(r.get('finished_at')),
                'ok':        r['ok'],
                'error':     r.get('error'),
                'elapsed':   r.get('elapsed_sec') or 0,
                'recovered': bool(prev and not prev['ok'] and r['ok']),
            }
    except Exception:
        pass
    if not runs:
        runs = {k: dict(v, recovered=False)
                for k, v in _JOB_RESULTS.items() if v.get('date') == today}

    missing, disabled = get_missing_jobs(set(runs))
    send_fails = pop_send_failures()
    if not runs and not missing:
        return

    items = sorted(runs.items())
    fails = [(k, v) for k, v in items if not v['ok']]
    slows = [(k, v) for k, v in items if v['ok'] and v['elapsed'] >= 300]
    recov = [k for k, v in items if v.get('recovered')]
    head = f"🗒 <b>[운영 요약] {today}</b> — 실행 {len(runs)}개 / 실패 {len(fails)}개"
    if missing:
        head += f" / 미실행 {len(missing)}개"
    lines = [head]
    for k, v in fails:
        lines.append(f"❌ {v['time']} {k}: {v['error']}")
    for k, at in missing:
        lines.append(f"🕳 미실행 {k}" + (f" (예정 {at})" if at else ""))
    for k in recov:
        lines.append(f"♻️ {k}: 실패 후 재처리 성공")
    for k, v in slows:
        lines.append(f"🐢 {v['time']} {k}: {v['elapsed']:.0f}초 소요")
    for cid, rec in sorted(send_fails.items(), key=lambda x: -x[1]['count']):
        lines.append(f"📵 발송실패 {cid} ×{rec['count']} — {rec['reason']}")
    if disabled:
        lines.append("⏸ 비활성: " + ", ".join(disabled))
    if not fails and not slows and not recov and not missing and not send_fails:
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
        mark_failed(e)


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
        mark_failed(e)


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
