import OpenDartReader
# ✅ 구글 최신 라이브러리 사용
from google import genai
from bs4 import BeautifulSoup
import logging
import requests
import re
import time
from config import DART_API_KEY, GOOGLE_API_KEY
from managers import global_session as _session

# ==========================================
# [설정] 모델 선택 (1.5 Flash 추천)
# ==========================================
AI_MODEL_ID = "gemini-2.5-flash"
# 네이버 리포트 PDF 요약 전용
REPORT_MODEL_ID = "gemini-3-flash-preview"   # 품질 우선 (무료 최상위, preview)
REPORT_FALLBACK_MODEL_ID = "gemini-2.5-flash"  # preview 과부하(503) 시 안정 폴백

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
        res.encoding = 'utf-8' # 한글 깨짐 방지
        
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
        2. '호재', '악재', '중립', '정보' 중 하나를 판단하여 맨 앞에 표시할 것. (예: [호재])
        3. 금액, 날짜, 계약 상대 등 숫자가 있다면 반드시 포함할 것.
        4. 초보자도 이해하기 쉬운 구어체(해요체)로 작성할 것.

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


def summarize_report_pdf(pdf_bytes, corp_name=""):
    """
    증권사 기업분석 리포트 PDF를 Gemini로 요약 (텔레그램 캡션용 짧은 요약).
    PDF 바이트를 모델에 직접 전달(네이티브 PDF) → 텍스트 추출 라이브러리 불필요.
    실패 시 None 반환 (호출부는 요약 없이 정상 발송).
    """
    if not client or not pdf_bytes:
        return None

    try:
        from google.genai import types

        prompt = (
            "당신은 주식 애널리스트입니다. 첨부된 증권사 기업분석 리포트를 "
            "개인 투자자용으로 짧게 요약하세요.\n"
            f"기업명: {corp_name}\n\n"
            "[지시사항]\n"
            "1. 총 250자 이내, 4줄 이내로 핵심만.\n"
            "2. 투자의견·목표주가가 있으면 첫 줄에 반드시 표시.\n"
            "3. 실적·전망의 핵심 숫자(매출·영업이익 증감 등)를 포함.\n"
            "4. 구어체(해요체)로, 마크다운·머리표 기호 없이 평문으로 작성."
        )
        part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

        # preview 모델은 일시적 503(과부하)이 잦음 → 2회 재시도 후 안정 모델로 폴백
        for model_id in (REPORT_MODEL_ID, REPORT_MODEL_ID, REPORT_FALLBACK_MODEL_ID):
            try:
                response = client.models.generate_content(
                    model=model_id, contents=[part, prompt]
                )
                text = (response.text or "").strip()
                if text:
                    return text
            except Exception as e:
                logging.warning(
                    f"리포트 요약 시도 실패 [{model_id}] ({corp_name}): {str(e)[:120]}"
                )
                time.sleep(3)

        return None

    except Exception as e:
        logging.error(f"❌ 리포트 요약 실패 ({corp_name}): {e}")
        return None