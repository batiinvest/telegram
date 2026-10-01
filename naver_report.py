"""
naver_report.py — 네이버 증권 리포트 수집·전송
──────────────────────────────────────────────
stock_api.py 물리 분할 (2026-07): 리포트 크롤링·PDF 전송·AI 요약 캡션.
stock_api 가 하위호환을 위해 주요 심볼을 재수출한다 (backfill_reports 등).
"""
import re
import time
from io import BytesIO
from datetime import datetime
from typing import Optional

from logger_config import get_logger
from managers import (global_session as _session, HistoryManager,
                      telegram_bot as _telegram_bot, note_send_failure)
from config import (
    TELEGRAM_BOT_TOKEN,
    COMPANY_CHAT_IDS, INDUSTRY_CHAT_IDS, COMPANY_TO_INDUSTRY,
)

log = get_logger(__name__)

# =================================================================================
# 📑 [Naver Report] 네이버 증권 리포트 수집 및 전송 (통합 모듈)
# =================================================================================

# 리포트 관련 상수 설정
NAVER_REPORT_CHAT_ID = "@batiarchive"  # 네이버 리포트 전용 채널 (기본값 — DB report_chat_id로 덮어씀)
# 네이버 증권 리서치 API. 2026-09 개편으로 finance.naver.com/research/*_list.naver(HTML 표)가
# stock.naver.com/research/*(Next.js)로 넘어갔고, 화면은 이 JSON API로 그린다.
#   목록: {API}/{type}?index=0..&size=..&startDate=&endDate=  → {items, hasNext, totalCount}
#   상세: {API}/{type}/{nid}                                   → attachUrl(PDF) — 목록엔 PDF 주소가 없다
NAVER_RESEARCH_API = "https://stock.naver.com/api/stockSecurity/researches/v2"
REPORT_API_TYPES = {"기업분석": "company", "산업분석": "industry"}

# 네이버 리포트 분류 -> config.py 산업군 키 매핑
REPORT_INDUSTRY_MAP = {
    "반도체": "반도체",
    "IT": "테크", "게임": "테크", "휴대폰": "테크", "디스플레이": "테크",
    "전기전자": "테크", "통신": "테크", "인터넷포탈": "테크", "소프트웨어": "테크",
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

ROBOT_KEYWORDS = ["로봇", "액츄에이터", "로보틱스", "휴머로이드", "AMR", "AGV", "감속기", "서보모터", "휴머노이드"]

# ── 발송·크롤 튜닝 상수 (기존 하드코딩 값 그대로 상수화) ──────────
_CAPTION_LIMIT          = 1024   # 텔레그램 캡션 최대 길이
_SUMMARY_CHUNK_SIZE     = 30     # 요약 메시지 1건당 항목 수
_REPORT_HISTORY_MAX     = 2000   # sent_reports.txt 보관 최대 줄 수
_MAX_DOC_RETRY          = 3      # PDF 전송 최대 재시도 횟수
_RATELIMIT_DEFAULT_WAIT = 10     # 429 응답에 retry_after 없을 때 대기(초)
_NET_ERROR_RETRY_WAIT   = 5      # 네트워크 오류 시 재시도 대기(초)
_DOC_SEND_INTERVAL_SEC  = 1.0    # 연속 문서 전송 간 간격(초)
_SUMMARY_SEND_DELAY_SEC = 0.5    # 요약 청크 전송 간 간격(초)
_PAGE_DELAY_SEC         = 0.2    # 페이지네이션 크롤 간 간격(초)
_DETAIL_DELAY_SEC       = 0.1    # 상세(PDF 주소) 조회 간 간격(초)
_PDF_DOWNLOAD_TIMEOUT   = 30     # PDF 다운로드 타임아웃(초)
_PDF_CHUNK_BYTES        = 8192   # PDF 스트리밍 청크 크기
_LIST_TIMEOUT_SEC       = 15     # 목록·상세 API 타임아웃(초) — 구: None(무제한)이라 응답이 멈추면 잡 스레드도 멈췄다
_LIST_PAGE_SIZE         = 50     # 목록 API 1회 건수 (API 상한 50, 넘기면 400 too_big)
_MAX_LIST_PAGES         = 20     # 하루치 목록 페이지 상한 — hasNext가 고장 나도 무한 루프 방지

# 네이버 요청 헤더. 구: global_session 기본값 그대로 — UA는 python-requests, GET에
# Content-Type: application/json까지 붙었다. 다른 네이버 수집기(collect_wics·collect_qtr_consensus·
# stock_api 지수)와 같게 브라우저 UA를 보낸다.
# Content-Type: None — 세션 기본 헤더에서 이 요청만 그 키를 뺀다(requests 병합 규칙).
_NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://stock.naver.com/research/",
    "Content-Type": None,
}


class ReportCrawlError(RuntimeError):
    """리서치 API 응답이 예상한 모양이 아님 — 차단·오류·개편.

    '오늘 리포트 0건'과 구분하려고 따로 둔다. 구: 둘 다 빈 리스트라
    크롤이 깨져도 잡이 성공으로 기록되고 실패 알림도 가지 않았다."""

# -----------------------------------------------------------
# 🛠️ [Internal] 리포트 파싱 및 유틸리티
# -----------------------------------------------------------
def _sanitize_filename(file_name: str) -> str:
    return re.sub(r'[<>:"/\\\\|?*]', "_", file_name)

def _safe_caption(file_name: str) -> str:
    base = file_name[:-4] if file_name.lower().endswith(".pdf") else file_name
    return base.replace("_", " ")[:_CAPTION_LIMIT]

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

def _report_file_name(item: dict, page_type: str):
    """목록 API 항목 → (file_name, tag). 이름 붙일 필드가 없으면 None.

    파일명은 구 목록 표 시절과 같은 꼴 — sent_reports.txt 중복 판정·캡션·해시태그가 그대로 이어진다.
      기업분석: {종목명}_{YYMMDD}_{증권사}.pdf          tag=종목명 (기업방·산업방 라우팅 키)
      산업분석: [{분류}] {제목}_{YYMMDD}_{증권사}.pdf   tag=분류 ('기타'+로봇 키워드 → '로봇')
    """
    firm_name = str(item.get("brokerName") or "").strip()
    # writeDate '2026-09-30' → '260930' (구 표의 '26.09.30'에서 점을 뺀 값과 같다)
    report_date = str(item.get("writeDate") or "").replace("-", "")[2:] or "000000"

    tag = None
    if page_type == "산업분석":
        title = str(item.get("title") or "").strip()
        industry = str(item.get("industryKoreanName") or "").strip()
        if industry in ("기타",) and _is_robot_topic(title):
            industry = "로봇"
        if industry:
            title = f"[{industry}] {title}"
            tag = industry
    else:
        title = tag = str(item.get("itemName") or "").strip()   # 기업명

    if not title or not item.get("nid"):
        return None
    return _sanitize_filename(f"{title}_{report_date}_{firm_name}.pdf"), tag

def _fetch_pdf_file(pdf_url: str) -> Optional[BytesIO]:
    """PDF 파일을 메모리로 다운로드 (global_session 사용)"""
    try:
        # 파일 다운로드는 stream=True 권장
        with _session.get(pdf_url, stream=True, timeout=_PDF_DOWNLOAD_TIMEOUT,
                          headers=_NAVER_HEADERS) as r:
            r.raise_for_status()
            buf = BytesIO()
            for chunk in r.iter_content(chunk_size=_PDF_CHUNK_BYTES):
                if chunk: buf.write(chunk)
            buf.seek(0)
            return buf
    except Exception as e:
        log.error(f"PDF Download Fail: {e}")
        return None

def _send_telegram_doc(chat_id: str, document, file_name: str, caption: str = None, retry_count: int = 0) -> bool:
    """텔레그램 문서(PDF) 전송 — 429 속도제한 시 대기 후 재전송. 성공 시 True.

    실패는 note_send_failure로 남겨 19:50 운영 요약의 '📵 발송실패'에 나온다
    (구: 로그 한 줄뿐이라 채널 권한이 빠져도 아무도 몰랐다)."""
    if not TELEGRAM_BOT_TOKEN: return False

    # 최대 _MAX_DOC_RETRY회까지만 재시도 (무한 루프 방지)
    if retry_count > _MAX_DOC_RETRY:
        log.error(f"❌ [Telegram] {_MAX_DOC_RETRY}회 재시도 실패, 전송 포기 ({file_name})")
        note_send_failure(chat_id, "sendDocument 429 재시도 초과")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    try:
        data = {"chat_id": chat_id, "caption": caption[:_CAPTION_LIMIT], "parse_mode": "HTML"}

        # 문서 전송 시도
        if isinstance(document, str): # URL인 경우
            data["document"] = document
            res = _session.post(url, data=data)
        else: # 파일 객체인 경우
            document.seek(0)
            files = {"document": (file_name, document, "application/pdf")}
            # 헤더 충돌 방지
            res = _session.post(url, data=data, files=files, headers={"Content-Type": None})

        # 429 Too Many Requests (속도 제한) 처리
        if res.status_code == 429:
            wait_time = res.json().get("parameters", {}).get("retry_after", _RATELIMIT_DEFAULT_WAIT)
            log.warning(f"⏳ [Telegram] 속도 제한 감지! {wait_time}초 대기 후 재전송... ({file_name})")

            # 지정된 시간만큼 멈춤 (이때 스케줄러도 멈춰서 기다림)
            time.sleep(wait_time + 1)

            # 재귀 호출로 다시 시도
            return _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)

        ok = res.status_code == 200
        if not ok:
            log.error(f"⚠️ [Telegram] 전송 실패 ({chat_id}, {res.status_code}): {res.text}")
            note_send_failure(chat_id, f"sendDocument {res.status_code} {res.text[:80]}")

        # 성공 시에도 연속 전송 방지를 위해 약간 대기
        time.sleep(_DOC_SEND_INTERVAL_SEC)
        return ok

    except Exception as e:
        log.error(f"❌ [Telegram] Doc Error ({chat_id}): {e}")
        # 네트워크 에러 시에도 1번은 재시도
        if retry_count < 1:
            time.sleep(_NET_ERROR_RETRY_WAIT)
            return _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)
        note_send_failure(chat_id, f"sendDocument 연결 에러: {e}")
        return False


_REPORT_NUMS = ("①", "②", "③")


def _norm_target_price(tp: str) -> str:
    """목표주가 표기 정규화: 천단위 콤마 통일. '미제시'/예상밖 형식은 원본 유지."""
    if not tp or tp == "미제시":
        return "미제시"
    m = re.search(r'([\d,]+)\s*원', tp)
    if m:
        return f"{int(m.group(1).replace(',', '')):,}원"
    return tp


def _norm_upside(up: str) -> str:
    """상승여력 정규화: '현재가 대비' 군더더기 제거 + 부호·소수1자리 통일. 없으면 ''."""
    if not up or up == "N/A":
        return ""
    up = up.replace("현재가 대비", "").strip()
    m = re.search(r'([+-]?)\s*(\d+(?:\.\d+)?)\s*%', up)
    if m:
        sign = m.group(1) or "+"
        return f"{sign}{float(m.group(2)):.1f}%"
    return up


def _build_report_caption(file_name: str, tag: str, hashtags: str, fields: dict = None) -> str:
    """
    리포트 PDF 캡션 생성. AI 구조화 요약(fields)이 있으면 투자노트 양식으로,
    없거나 추출 실패 시 기존 평문 형식(링크+파일명+해시태그)으로 발송.
    텔레그램 캡션 1024자 제한 가드 포함.
    """
    # 폴백: AI 필드 없음/투자사유 없음 → 기존 평문 캡션
    if not fields or not fields.get("포인트"):
        head = (
            f"📌 <a href='https://t.me/batiarchive'>바티아카이브</a> — 리포트·IR자료\n\n"
            f"{_safe_caption(file_name)}"
        )
        return f"{head}\n\n{hashtags}"[:_CAPTION_LIMIT]

    firm = _extract_firm(file_name)
    lines = [f"📑 <b>{tag}</b> · {firm}", "━━━━━━━━━━━━"]

    # 콜: 투자의견 / 목표주가(+상승여력) — 없으면 '미제시' 명시
    lines.append(f"📈 투자의견  {fields.get('투자의견') or '미제시'}")
    tp = _norm_target_price(fields.get("목표주가"))
    up = _norm_upside(fields.get("상승여력", ""))
    tp_line = f"🎯 목표주가  {tp}"
    if tp != "미제시" and up:
        tp_line += f"  (상승여력 {up})"
    lines.append(tp_line)

    # 실적·밸류 (있을 때만)
    mv = fields.get("실적밸류", "")
    if mv and mv != "N/A":
        lines.append(f"📊 실적·밸류  {mv}")

    # 투자사유
    lines.append("")
    lines.append("💡 <b>투자사유</b>")
    for i, p in enumerate(fields["포인트"][:3]):
        lines.append(f"{_REPORT_NUMS[i]} {p}")

    # 리스크 (있을 때만)
    rk = fields.get("리스크", "")
    if rk and rk != "N/A":
        lines.append(f"⚠️ 리스크  {rk}")

    lines.append("")
    lines.append("📎 <a href='https://t.me/batiarchive'>바티아카이브</a>")
    lines.append(hashtags)

    return "\n".join(lines)[:_CAPTION_LIMIT]


def _get_research_json(path: str, params: Optional[dict], timeout) -> dict:
    """리서치 API GET → dict. HTTP 오류·JSON 아님·다른 주소로 이동이면 ReportCrawlError.

    알림이 200자에서 잘리니 사유는 짧게 — 이동한 주소가 개편의 가장 큰 단서다."""
    url = f"{NAVER_RESEARCH_API}/{path}"
    res = _session.get(url, params=params, headers=_NAVER_HEADERS, timeout=timeout)
    final = (res.url or "").split("?")[0]
    moved = f", 이동→{final}" if final and final != url else ""
    if res.status_code != 200:
        raise ReportCrawlError(f"HTTP {res.status_code} {path} {res.text[:60]}{moved}")
    try:
        data = res.json()
    except ValueError:
        raise ReportCrawlError(f"JSON 아님(개편 의심) {path} 본문 {len(res.text)}자{moved}") from None
    if not isinstance(data, dict):
        raise ReportCrawlError(f"응답 형식 변경 의심 {path}: {type(data).__name__}")
    return data


def crawl_report_pages(page_type: str, date_str: str, history: HistoryManager,
                       *, skip_history: bool = False, timeout=_LIST_TIMEOUT_SEC) -> list:
    """네이버 리서치 목록을 페이지네이션하며 (pdf_url, file_name, tag) 튜플 리스트로 수집.

    run_naver_report_job(오늘)·backfill_reports(과거 날짜) 공용 크롤러.
      date_str            : 조회 기준일 (startDate=endDate=date_str)
      skip_history=False  : history 중복분 제외 (True면 재전송 허용)
      timeout             : requests 타임아웃(초)

    목록엔 PDF 주소가 없어 중복을 거른 뒤 남은 것만 상세 API로 attachUrl을 받는다.
    첫 페이지부터 못 읽거나 상세를 하나도 못 받으면 ReportCrawlError — 빈 리스트
    (=그날 리포트 0건)와 구분한다. 둘째 페이지 이후·일부 상세 실패는 로그만 남기고
    모은 것을 돌려준다(못 받은 건 history에 안 남으니 다음 실행에 다시 시도).
    """
    api_type = REPORT_API_TYPES[page_type]
    pending, index = [], 0                 # (nid, file_name, tag) — 상세 조회 대기
    while index < _MAX_LIST_PAGES:
        try:
            data = _get_research_json(api_type, {
                "index": index, "size": _LIST_PAGE_SIZE,
                "startDate": date_str, "endDate": date_str,
            }, timeout)
            items = data.get("items")
            if not isinstance(items, list):
                raise ReportCrawlError(f"목록에 items 없음(개편 의심) 키 {sorted(data)[:6]}")

            named = 0
            for item in items:
                parsed = _report_file_name(item, page_type) if isinstance(item, dict) else None
                if not parsed:
                    continue
                named += 1
                file_name, tag = parsed
                if not skip_history and history.contains(file_name):
                    continue
                pending.append((str(item["nid"]), file_name, tag))

            # 항목은 있는데 한 건도 이름을 못 붙였다 = 필드명이 바뀌었다 (_report_file_name 점검)
            if items and not named:
                raise ReportCrawlError(
                    f"항목 {len(items)}개가 있는데 해석된 항목 0개 — 필드 변경 의심 {sorted(items[0])[:8]}")

            if not data.get("hasNext") or not items:
                break
            index += 1
            time.sleep(_PAGE_DELAY_SEC)
        except Exception as e:
            if index == 0:
                raise ReportCrawlError(f"{page_type} {date_str}: {e}") from e
            log.error(f"[리포트 크롤] {page_type} {date_str} p.{index + 1}: {e}")
            break

    reports, detail_fail = [], 0
    for nid, file_name, tag in pending:
        try:
            pdf_url = _get_research_json(f"{api_type}/{nid}", None, timeout).get("attachUrl")
        except Exception as e:
            detail_fail += 1
            log.error(f"[리포트 크롤] {page_type} {date_str} 상세 {nid}: {e}")
            continue
        if pdf_url:
            reports.append((pdf_url, file_name, tag))
        else:
            log.info(f"[리포트 크롤] 첨부 없음 — 건너뜀: {file_name}")
        time.sleep(_DETAIL_DELAY_SEC)

    if pending and detail_fail == len(pending):
        raise ReportCrawlError(
            f"{page_type} {date_str}: 상세(PDF 주소) 조회 {detail_fail}건 모두 실패")
    return reports


def _resolve_report_targets(page_type: str, tag) -> set:
    """리포트 타입·태그에 따른 산업방/기업방 타겟 chat_id 집합 반환."""
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
    return targets


def run_naver_report_job():
    """네이버 리포트 수집/전송 (페이지네이션 + 중복 방지 + 메시지 분할).

    크롤 실패나 리포트 채널 발송 실패가 있으면 나머지를 다 처리한 뒤 RuntimeError —
    job_naver_report가 실패로 기록하고 관리자 방에 알린다."""
    # DB에서 리포트 채널 ID 동적 로드 (app_config.report_chat_id)
    # AI 요약 기능은 app_config.report_ai_summary 로 토글 (기본 OFF, 승인 후 'on')
    try:
        from supabase_bridge import bridge as _b
        _report_cid = _b.get_config('report_chat_id', NAVER_REPORT_CHAT_ID)
        _ai_summary_on = str(_b.get_config('report_ai_summary', 'off')).lower() in ('on', 'true', '1', 'yes')
    except Exception:
        _report_cid = NAVER_REPORT_CHAT_ID
        _ai_summary_on = False

    today_str = datetime.now().strftime("%Y-%m-%d")
    log.info(f"📑 네이버 리포트 수집 시작 ({today_str})")

    # 히스토리 매니저 로드
    history = HistoryManager("sent_reports.txt", max_len=_REPORT_HISTORY_MAX)
    failures = []

    if not _report_cid:
        failures.append("report_chat_id 비어 있음 — 아카이브 채널 발송 생략")

    for page_type in ["산업분석", "기업분석"]:
        # 오늘자 리포트 수집 (공통 크롤러 — 중복 제외)
        try:
            reports = crawl_report_pages(page_type, today_str, history)
        except ReportCrawlError as e:
            log.error(f"❌ [리포트 크롤] {e}")
            failures.append(str(e))
            continue

        if not reports:
            log.info(f"   -> {page_type}: 전송할 신규 리포트 없음")
            continue

        # 요약본 메인방 전송 (길이 제한 고려하여 분할 전송)
        if _report_cid:
            header = f"📑 <b>[{today_str}] {page_type} 리포트</b> (총 {len(reports)}개)\n\n"
            chunk_size = _SUMMARY_CHUNK_SIZE # 한 번에 N개씩 끊어서 전송

            for i in range(0, len(reports), chunk_size):
                chunk = reports[i:i+chunk_size]
                msg_lines = []
                if i == 0: msg_lines.append(header)

                for j, item in enumerate(chunk):
                    # item: (pdf_url, file_name, tag)
                    clean_name = item[1].replace(".pdf", "").replace("_", " ")
                    msg_lines.append(f"{i+j+1}. {clean_name}")

                final_msg = "\n".join(msg_lines) + f"\n\n{_make_hashtag(page_type)}"
                if not _telegram_bot.send_message(_report_cid, final_msg):
                    failures.append(f"{page_type} 목록 메시지 발송 실패 → {_report_cid}")
                time.sleep(_SUMMARY_SEND_DELAY_SEC)

        # 개별 파일 전송
        doc_fail = 0
        for pdf_url, file_name, tag in reports:
            # PDF 다운로드
            pdf_buf = _fetch_pdf_file(pdf_url)
            target_doc = pdf_buf if pdf_buf else pdf_url
            hashtags = _report_hashtags(page_type, tag, file_name)

            # 전체 기업분석 리포트에 AI 요약을 캡션에 첨부 (Gemini 무료 등급, 리포트당 1회)
            # report_ai_summary 플래그가 켜져 있을 때만 동작 (기본 OFF)
            ai_fields = None
            if _ai_summary_on and page_type == "기업분석" and pdf_buf:
                try:
                    from ai_analyst import summarize_report_pdf
                    ai_fields = summarize_report_pdf(pdf_buf.getvalue(), tag)
                except Exception as e:
                    log.error(f"리포트 요약 호출 실패 ({file_name}): {e}")

            caption = _build_report_caption(file_name, tag, hashtags, ai_fields)

            # 1. 리포트 채널 전송 (batiarchive)
            if _report_cid and not _send_telegram_doc(_report_cid, target_doc, file_name, caption):
                doc_fail += 1


            # 2. 타겟 채널(산업방·기업방) 찾아 전송
            targets = _resolve_report_targets(page_type, tag)
            for chat_id in targets:
                if pdf_buf: pdf_buf.seek(0)
                _send_telegram_doc(chat_id, target_doc, file_name, caption)
            
            # [중요] 전송 성공 후에만 히스토리에 기록
            history.add(file_name)
            log.info(f"   -> 리포트 전송 완료: {file_name}")

        if doc_fail:
            failures.append(f"{page_type} PDF 발송 실패 {doc_fail}/{len(reports)}건 → {_report_cid}")

    log.info("📑 리포트 작업 종료")
    if failures:
        raise RuntimeError(" | ".join(failures))
