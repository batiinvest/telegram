import OpenDartReader
# ✅ 구글 최신 라이브러리 사용
from google import genai
from bs4 import BeautifulSoup
import logging
import time
from config import DART_API_KEY, GOOGLE_API_KEY
from managers import global_session as _session

# ==========================================
# [설정] 모델 선택 (1.5 Flash 추천)
# ==========================================
AI_MODEL_ID = "gemini-2.5-flash"
# 네이버 리포트 PDF 요약 전용 — 무료 등급 호출 최소화(리포트당 1회) 위해 단일 모델 사용.
# (preview는 503 상습·무료 쿼터 작아 제거. throughput 더 필요하면 gemini-2.5-flash-lite)
REPORT_MODEL_ID = "gemini-2.5-flash"

# 초기화
try:
    dart = OpenDartReader(DART_API_KEY)
    client = genai.Client(api_key=GOOGLE_API_KEY)
except Exception as e:
    logging.error(f"AI Init Failed: {e}")
    dart = None
    client = None

def clean_html_text(raw_html):
    """HTML 태그 제거 및 텍스트 정제"""
    if not raw_html: return ""
    try:
        soup = BeautifulSoup(raw_html, 'html.parser')
        
        # 스타일, 스크립트 제거
        for script in soup(["script", "style"]):
            script.decompose()
            
        text = soup.get_text(separator='\n\n')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return '\n'.join(lines)
    except:
        return raw_html

# -----------------------------------------------------------
# 📱 [신규] 모바일 DART 크롤러 (성공률 99%)
# -----------------------------------------------------------
def crawl_dart_mobile(rcept_no):
    """
    PC 버전이 복잡하므로, 구조가 단순한 모바일 DART 페이지를 긁어옵니다.
    """
    try:
        # 모바일 공시 뷰어 URL
        url = f"http://m.dart.fss.or.kr/html_mdart/MD1007.html?rcpNo={rcept_no}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
        }
        
        res = _session.get(url, headers=headers, timeout=5)
        # 한글 깨짐 방지 — 서버가 euc-kr을 주는 경우 대비 감지 폴백
        res.encoding = res.apparent_encoding or 'utf-8'
        
        # 모바일 페이지는 별도 파싱 없이 본문이 바로 HTML에 들어있음
        if res.status_code == 200:
            logging.info(f"📱 모바일 크롤링 성공! ({rcept_no})")
            return res.text
        else:
            logging.error(f"📱 모바일 접속 실패: {res.status_code}")
            return None

    except Exception as e:
        logging.error(f"📱 크롤링 에러: {e}")
        return None
# -----------------------------------------------------------

def analyze_disclosure_gemini(corp_name, report_nm, rcept_no):
    """
    1차: API -> 2차: 모바일 크롤링 -> 3차: AI 분석
    """
    if not dart or not client:
        return None

    try:
        raw_text = None
        
        # [1단계] API 호출
        try:
            raw_text = dart.document(rcept_no)
        except Exception as e:
            # 파일 없음(014) 등 에러 발생 시
            if "'status': '014'" in str(e) or "파일이 존재하지 않습니다" in str(e):
                logging.info(f"⚠️ API 데이터 없음. 모바일 크롤링 전환... ({corp_name})")
                # [2단계] 모바일 크롤러 가동
                raw_text = crawl_dart_mobile(rcept_no)
            else:
                logging.error(f"API Error: {e}")
                return None

        if not raw_text:
            logging.info(f"ℹ️ 분석 중단: 데이터 확보 불가 ({corp_name})")
            return None

        # [3단계] 정제
        clean_text = clean_html_text(raw_text)[:30000]
        
        if len(clean_text) < 50:
             logging.info(f"ℹ️ 내용 너무 짧음 (Skip): {corp_name}")
             return None

        # [4단계] 프롬프트
        prompt = f"""
        당신은 주식 시장 전문가입니다. 아래 공시를 분석하여 투자자에게 필요한 핵심 정보를 요약하세요.
        
        기업명: {corp_name}
        공시제목: {report_nm}
        
        [지시사항]
        1. 3줄 이내로 핵심만 요약할 것.
        2. '긍정적 참고', '부정적 참고', '중립', '정보' 중 하나로 성격을 맨 앞에 표시할 것. (예: [긍정적 참고])
           — 호재/악재 단정, 매수/매도 권유, 주가 전망 표현은 금지.
        3. 금액, 날짜, 계약 상대 등 숫자가 있다면 반드시 포함할 것.
        4. 공시에 없는 내용은 절대 추가하지 말 것.
        5. 초보자도 이해하기 쉬운 구어체(해요체)로 작성할 것.

        [공시 본문]
        {clean_text}
        """

        # AI 호출 (New SDK)
        response = client.models.generate_content(
            model=AI_MODEL_ID,
            contents=prompt
        )
        
        return response.text

    except Exception as e:
        logging.error(f"❌ Analysis Error ({corp_name}): {e}")
        return None


def _parse_report_fields(raw):
    """라벨 형식(키|값) 추출 결과를 dict로 파싱. 포인트는 리스트로 누적."""
    fields = {"포인트": []}
    for line in (raw or "").splitlines():
        if "|" not in line:
            continue
        key, val = line.split("|", 1)
        key, val = key.strip(), val.strip()
        if key == "포인트":
            if val and val != "N/A":
                fields["포인트"].append(val)
        elif key:
            fields[key] = val
    return fields


def summarize_report_pdf(pdf_bytes, corp_name=""):
    """
    증권사 기업분석 리포트 PDF를 Gemini로 구조화 추출 (텔레그램 캡션용).
    PDF 바이트를 모델에 직접 전달(네이티브 PDF) → 텍스트 추출 라이브러리 불필요.
    반환: 필드 dict(투자의견/목표주가/상승여력/실적밸류/포인트[]/리스크) 또는 None.
          투자사유(포인트)가 하나도 없으면 추출 실패로 보고 None (호출부는 평문 캡션 폴백).
    """
    if not client or not pdf_bytes:
        return None

    try:
        from google.genai import types

        prompt = (
            "당신은 주식 애널리스트입니다. 첨부된 증권사 기업분석 리포트에서 "
            "아래 항목을 추출하세요.\n"
            f"기업명: {corp_name}\n\n"
            "출력은 정확히 아래 형식으로, 각 항목을 한 줄씩, "
            "마크다운·머리표 기호 없이 평문으로 작성하세요.\n"
            "투자의견|<매수/중립/매도 + 괄호로 (상향/유지/하향). 리포트에 없으면 '미제시'>\n"
            "목표주가|<숫자와 단위(원). 없으면 '미제시'>\n"
            "상승여력|<현재가 대비 +XX% 또는 -XX%. 산출 불가 시 N/A>\n"
            "실적밸류|<매출·영업이익 증감(YoY 등)과 PER/PBR 등 밸류 지표를 한 줄로. 없으면 N/A>\n"
            "포인트|<핵심 투자사유 1>\n"
            "포인트|<핵심 투자사유 2>\n"
            "포인트|<핵심 투자사유 3>\n"
            "리스크|<핵심 리스크 1개. 없으면 N/A>\n\n"
            "[규칙]\n"
            "- 포인트·리스크·실적밸류는 각 45자 이내, 촉매·숫자 중심의 간결한 개조식.\n"
            "- 리포트에 근거 없는 내용은 절대 추가하지 말 것."
        )
        part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

        # 무료 등급 호출 최소화: 기본 1회. 일시적 오류(503/네트워크)일 때만 1회 재시도.
        # 429(쿼터소진)는 재시도 무의미 → 즉시 중단. 응답은 왔으나 추출 실패면 폴백(재호출 안 함).
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=REPORT_MODEL_ID, contents=[part, prompt]
                )
                fields = _parse_report_fields(response.text)
                return fields if fields["포인트"] else None
            except Exception as e:
                msg = str(e)
                logging.warning(
                    f"리포트 요약 시도 실패 ({corp_name}, {attempt+1}/2): {msg[:120]}"
                )
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    break  # 쿼터 소진 → 재시도해도 동일
                if attempt == 0:
                    time.sleep(4)

        return None

    except Exception as e:
        logging.error(f"❌ 리포트 요약 실패 ({corp_name}): {e}")
        return None