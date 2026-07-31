"""
naver_report.py — 네이버 증권 리포트 수집·전송
──────────────────────────────────────────────
stock_api.py 물리 분할 (2026-07): 리포트 크롤링·PDF 전송·AI 요약 캡션.
stock_api 가 하위호환을 위해 주요 심볼을 재수출한다 (backfill_reports 등).
"""
import re
import time
from io import BytesIO
from urllib.parse import urljoin, urlencode
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from logger_config import get_logger
from managers import global_session as _session, HistoryManager, telegram_bot as _telegram_bot
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
_PDF_DOWNLOAD_TIMEOUT   = 30     # PDF 다운로드 타임아웃(초)
_PDF_CHUNK_BYTES        = 8192   # PDF 스트리밍 청크 크기

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

def _get_total_pages(soup) -> int:
    """네이버 페이지네이션에서 마지막 페이지 번호 추출"""
    try:
        last_page_tag = soup.select_one("td.pgRR a")
        if last_page_tag:
            return int(last_page_tag["href"].split("page=")[-1])
    except Exception:
        pass
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
        with _session.get(pdf_url, stream=True, timeout=_PDF_DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            buf = BytesIO()
            for chunk in r.iter_content(chunk_size=_PDF_CHUNK_BYTES):
                if chunk: buf.write(chunk)
            buf.seek(0)
            return buf
    except Exception as e:
        log.error(f"PDF Download Fail: {e}")
        return None

def _send_telegram_doc(chat_id: str, document, file_name: str, caption: str = None, retry_count: int = 0):
    """텔레그램 문서(PDF) 전송 — 429 속도제한 시 대기 후 재전송."""
    if not TELEGRAM_BOT_TOKEN: return

    # 최대 _MAX_DOC_RETRY회까지만 재시도 (무한 루프 방지)
    if retry_count > _MAX_DOC_RETRY:
        log.error(f"❌ [Telegram] {_MAX_DOC_RETRY}회 재시도 실패, 전송 포기 ({file_name})")
        return

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
            _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)
            return

        if res.status_code != 200:
            log.error(f"⚠️ [Telegram] 전송 실패 ({res.status_code}): {res.text}")

        # 성공 시에도 연속 전송 방지를 위해 약간 대기
        time.sleep(_DOC_SEND_INTERVAL_SEC)

    except Exception as e:
        log.error(f"❌ [Telegram] Doc Error ({chat_id}): {e}")
        # 네트워크 에러 시에도 1번은 재시도
        if retry_count < 1:
            time.sleep(_NET_ERROR_RETRY_WAIT)
            _send_telegram_doc(chat_id, document, file_name, caption, retry_count + 1)


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


def run_naver_report_job():
    """네이버 리포트 수집/전송 (페이지네이션 + 중복 방지 + 메시지 분할)."""
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

                for row in rows:
                    data = _parse_report_row(row, base_url, page_type)
                    if data:
                        pdf_url, file_name, tag = data
                        
                        # 이미 보낸 리포트는 건너뜀 (중복 방지)
                        if history.contains(file_name): continue
                        
                        reports.append(data)
                
                # 마지막 페이지 체크
                total_pages = _get_total_pages(soup)
                if page >= total_pages:
                    break
                
                page += 1
                time.sleep(_PAGE_DELAY_SEC) # 페이지 넘김 딜레이

            except Exception as e:
                log.error(f"Report Crawl Error ({page_type} p.{page}): {e}")
                break

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
                _telegram_bot.send_message(_report_cid, final_msg)
                time.sleep(_SUMMARY_SEND_DELAY_SEC)

        # 개별 파일 전송
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
            log.info(f"   -> 리포트 전송 완료: {file_name}")

    log.info("📑 리포트 작업 종료")
