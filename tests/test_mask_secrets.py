"""logger_config.mask_secrets — 오류 문구·로그에서 키 가리기 (2026-11-27).

job_runs.error에 requests 오류 문구가 그대로 저장돼 DART 키(crtfc_key=…)가 남던 문제.
여기 값은 모두 가짜다.
"""
import io
import logging

import pytest

import logger_config
from logger_config import mask_secrets, SensitiveDataFilter

FAKE_DART = '0123456789abcdef0123456789abcdef01234567'          # 40자 hex 꼴
FAKE_TG = '1234567890:AAH' + 'x' * 32                             # 봇 토큰 꼴
FAKE_JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXRlc3Q'


@pytest.fixture(autouse=True)
def _fresh_literals(monkeypatch):
    # 환경변수 비밀값 캐시가 테스트끼리 섞이지 않게
    monkeypatch.setattr(logger_config, '_SECRET_LITERALS', {'n': -1, 'at': 0.0, 'vals': ()})


def test_requests_error_url_crtfc_key():
    s = ("HTTPSConnectionPool(host='opendart.fss.or.kr', port=443): Max retries exceeded with url: "
         f"/api/list.json?crtfc_key={FAKE_DART}&bgn_de=20261125&page_no=1 "
         "(Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x7f>: failed'))")
    out = mask_secrets(s)
    assert FAKE_DART not in out
    assert '/api/list.json?crtfc_key=***&bgn_de=20261125&page_no=1 (Caused by' in out


def test_key_first_then_truncate_keeps_no_prefix():
    s = 'x' * 90 + f'?crtfc_key={FAKE_DART}'
    assert FAKE_DART[:10] not in mask_secrets(s)[:100]


def test_telegram_bot_token_in_url():
    s = f"Max retries exceeded with url: /bot{FAKE_TG}/sendMessage (Caused by ReadTimeoutError)"
    out = mask_secrets(s)
    assert FAKE_TG.split(':')[1] not in out
    assert '/bot1234567890:***/sendMessage' in out


def test_header_dict_repr_and_service_key():
    s = (f"headers={{'appkey': 'PSabcdefghijkl123', 'appsecret': 'zzzzzzzzzzzzzzzz==', "
         f"'X-Naver-Client-Secret': 'nvSecret123'}} url=/getList?serviceKey=abc%2Bdef%3D%3D&numOfRows=10")
    out = mask_secrets(s)
    for leaked in ('PSabcdefghijkl123', 'zzzzzzzzzzzzzzzz==', 'nvSecret123', 'abc%2Bdef'):
        assert leaked not in out
    assert "'appkey': '***'" in out and 'serviceKey=***&numOfRows=10' in out


def test_bearer_and_jwt():
    out = mask_secrets(f"Authorization: Bearer {FAKE_JWT} apikey={FAKE_JWT}")
    assert 'c2lnbmF0dXJlLXRlc3Q' not in out


def test_env_literal_value_without_name(monkeypatch):
    monkeypatch.setenv('DART_API_KEY', FAKE_DART)
    out = mask_secrets(f"dart_fss: invalid key {FAKE_DART} (status 010)")
    assert FAKE_DART not in out and 'invalid key *** (status 010)' in out


def test_env_literal_url_encoded(monkeypatch):
    monkeypatch.setenv('DATA_GO_KR_KEY', 'ab+cd/ef==ghij')
    out = mask_secrets('url: /x?k=ab%2Bcd%2Fef%3D%3Dghij&y=1')
    assert 'ab%2Bcd' not in out


def test_html_tags_survive():
    out = mask_secrets(f"❌ <b>[잡 실패]</b>\n<code>crtfc_key={FAKE_DART}</code>")
    assert out == "❌ <b>[잡 실패]</b>\n<code>crtfc_key=***</code>"


@pytest.mark.parametrize('text', [
    "📈 <b>[마감 브리핑]</b> 코스피 2,612.35 (+0.84%) · 외국인 +3,120억",
    "Victoria's Secret: Q3 실적 발표 · token 발급 완료 · 비밀번호 변경 안내",
    "<a href=\"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261127000123\">공시 원문</a>",
    "[상장사동기화] 완료 — 신규 3 · 갱신 2,510 · 상폐 1종목 비활성화: 삼성전자(005930)",
    "시각 2026-11-27T07:03:45+09:00 · 커밋 3871090 · 해시 " + "f" * 40,
])
def test_ordinary_messages_unchanged(text):
    assert mask_secrets(text) == text


def test_idempotent_and_none():
    s = f"?crtfc_key={FAKE_DART}&a=1 /bot{FAKE_TG}/x"
    assert mask_secrets(mask_secrets(s)) == mask_secrets(s)
    assert mask_secrets(None) == ''
    assert mask_secrets(ValueError(f"crtfc_key={FAKE_DART}")) == 'crtfc_key=***'


def test_log_filter_masks_message_and_traceback():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter('%(message)s'))
    h.addFilter(SensitiveDataFilter())
    lg = logging.getLogger('test_mask_secrets')
    lg.propagate = False
    lg.addHandler(h)
    try:
        try:
            raise ConnectionError(f"Max retries exceeded with url: /api/list.json?crtfc_key={FAKE_DART}&p=1")
        except ConnectionError as e:
            lg.error("잡 실행 실패: %s", e, exc_info=True)
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert FAKE_DART not in out
    assert out.count('crtfc_key=***') == 2          # 본문 한 번 + traceback 마지막 줄 한 번
