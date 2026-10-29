# -*- coding: utf-8 -*-
"""spam_guard characterization 테스트 — 채팅방 스팸 탐지 순수 로직.

_detect 오탐은 정상 사용자 메시지 삭제로 직결되므로 회귀 방지가 중요.
_is_flood/_detect의 도배 판정은 모듈 전역 상태(_recent)를 쓰므로,
각 테스트는 고유 텍스트를 사용해 상호 간섭을 피한다.
기대값은 2026-07-25 실제 실행 출력에서 캡처.
"""
import pytest

import spam_guard as SG


@pytest.mark.parametrize("text, expected", [
    ("https://n.news.naver.com/article/123", True),
    ("그냥 텍스트 메시지입니다",                False),
    ("영상 youtu.be/abc",                     True),   # youtube도 정상 도메인
])
def test_has_news_link(text, expected):
    assert SG._has_news_link(text) is expected


def test_detect_invite_link():
    """텔레그램 외부 초대링크 → 스팸 + 자동삭제."""
    assert SG._detect("가입하세요 https://t.me/+abcdef", 111) == (
        True, "텔레그램 외부 초대링크", True)


def test_detect_kakao_openchat():
    """카카오 오픈채팅 → 스팸 + 자동삭제."""
    assert SG._detect("오픈채팅 open.kakao.com/o/xyz", 111) == (
        True, "카카오 오픈채팅", True)


def test_detect_normal_message():
    """평범한 메시지 → 스팸 아님."""
    assert SG._detect("안녕하세요 오늘 시장 어떤가요", 111) == (False, "", False)


def test_detect_flood_progression():
    """동일 텍스트가 3개 방 이상 → 도배 의심(알림만, 자동삭제 X)."""
    text = "테스트용 고유 도배문구 사세요 대박 종목"  # 이 테스트 전용 고유 텍스트
    assert SG._detect(text, 2001) == (False, "", False)   # 1개 방
    assert SG._detect(text, 2002) == (False, "", False)   # 2개 방
    assert SG._detect(text, 2003) == (True, "다중방 도배(의심)", False)  # 3개 방


def test_detect_flood_exempt_when_news_link():
    """뉴스 링크 포함 시 다중방이어도 도배 면제(정상 뉴스 공유 오탐 방지)."""
    text = "이 기사 공유 naver.com/news/unique-flood-exempt 필독"
    results = [SG._detect(text, cid) for cid in (3001, 3002, 3003)]
    assert all(r == (False, "", False) for r in results)


def test_flood_kind_short_text_ignored():
    """최소 길이(12자) 미만은 도배 추적에서 제외."""
    assert SG._flood_kind("짧음", 4001) is None


# ── 10-29 도배 개선: 고신뢰 도배 자동 삭제·숨은 링크·수정 메시지 ──────────

def test_detect_multi_account_flood_auto_deletes():
    """여러 계정이 같은 문구를 3개 방 이상 → 자동 삭제(10-29 실제: 계정 4개 × 방 4곳)."""
    text = "고유문구 무료 리딩방 입장 안내 드립니다 5001"
    assert SG._detect(text, 5001, uid=11) == (False, "", False)
    assert SG._detect(text, 5002, uid=12) == (False, "", False)
    assert SG._detect(text, 5003, uid=13) == (True, "여러 계정 다중방 도배", True)


def test_detect_single_account_multi_room_alert_only():
    """한 계정이 3개 방에 같은 문구 → 알림만(뉴스 공유 등 오탐 여지)."""
    text = "고유문구 한 사람이 여러 방에 공유한 글 5101"
    results = [SG._detect(text, cid, uid=21) for cid in (5101, 5102, 5103)]
    assert results[-1] == (True, "다중방 도배(의심)", False)


def test_detect_same_room_repeat_auto_deletes():
    """한 사람이 같은 방에 같은 문구 3번 → 자동 삭제(방 1곳이어도)."""
    text = "고유문구 같은 방에 반복해서 올리는 홍보 5201"
    assert SG._detect(text, 5201, uid=31)[0] is False
    assert SG._detect(text, 5201, uid=31)[0] is False
    assert SG._detect(text, 5201, uid=31) == (True, "같은 방 반복 도배", True)


def test_detect_repeat_needs_same_person():
    """같은 방이라도 서로 다른 사람이 같은 문구를 쓴 건 반복 도배가 아님."""
    text = "고유문구 다른 사람들이 같은 말을 한 경우 5301"
    assert all(SG._detect(text, 5301, uid=u) == (False, "", False) for u in (41, 42, 43))


def test_detect_multi_account_news_link_exempt():
    """뉴스 링크는 여러 계정·여러 방이어도 도배 아님."""
    text = "고유 기사 공유 hankyung.com/article/5401 필독"
    results = [SG._detect(text, cid, uid=u) for cid, u in ((5401, 51), (5402, 52), (5403, 53))]
    assert all(r == (False, "", False) for r in results)


def test_detect_hidden_invite_link():
    """글자에 숨긴 초대링크도 잡는다."""
    assert SG._detect("여기 누르세요", 5501, links="https://t.me/+hidden") == (
        True, "텔레그램 외부 초대링크", True)


def test_hidden_urls_from_entities_and_buttons():
    msg = {"entities": [{"type": "text_link", "url": "https://t.me/+a"}, {"type": "bold"}],
           "caption_entities": [{"type": "text_link", "url": "https://open.kakao.com/o/b"}],
           "reply_markup": {"inline_keyboard": [[{"text": "x", "url": "https://c.example"}]]}}
    assert SG._hidden_urls(msg) == ["https://t.me/+a", "https://open.kakao.com/o/b", "https://c.example"]


def test_detect_untracked_skips_flood():
    """수정된 메시지(track=False)는 도배 집계에 안 넣는다."""
    text = "고유문구 수정된 메시지는 집계 제외 5601"
    assert all(SG._detect(text, cid, uid=61, track=False) == (False, "", False) for cid in (5601, 5602, 5603))
    assert SG._recent.get(SG._text_hash(text)) is None


# ── 묶음 처리 시뮬레이션 (텔레그램 호출은 가짜) ───────────────────────

@pytest.fixture
def fake_tg(monkeypatch):
    calls = []
    mid = [9000]

    def _tg(method, **p):
        calls.append((method, p))
        if method == "getChatAdministrators":
            return {"ok": True, "result": [{"user": {"id": 999}}]}
        if method == "sendMessage":
            mid[0] += 1
            return {"ok": True, "result": {"message_id": mid[0]}}
        return {"ok": True}

    monkeypatch.setattr(SG, "_tg", _tg)
    monkeypatch.setattr(SG, "_get_admin_chat", lambda fallback="": "-100777")
    monkeypatch.setattr(SG, "_all_room_cids", lambda: [-1, -2, -3, "@industry"])
    monkeypatch.setattr(SG, "_admin_cache", {})
    for d in (SG._recent, SG._bursts, SG._burst_keys, SG._ok_until):
        d.clear()
    return calls


def _msg(cid, uid, mid, text, name="spam"):
    return {"chat": {"id": cid, "type": "supergroup", "title": f"방{cid}"},
            "from": {"id": uid, "first_name": name}, "message_id": mid, "text": text}


def test_burst_multi_account_deletes_all_and_alerts_once(fake_tg):
    """10-29 패턴: 계정 3개가 각자 방에 같은 문구 → 3번째에 앞 메시지까지 삭제, 알림 1건."""
    text = "무료 급등주 리딩방 지금 바로 문의주세요 7001"
    SG.check_message(None, _msg(-1, 101, 1, text))
    SG.check_message(None, _msg(-2, 102, 2, text))
    assert not [c for c in fake_tg if c[0] == "deleteMessage"]
    SG.check_message(None, _msg(-3, 103, 3, text))
    deleted = {(p["chat_id"], p["message_id"]) for m, p in fake_tg if m == "deleteMessage"}
    assert deleted == {(-1, 1), (-2, 2), (-3, 3)}
    sends = [p for m, p in fake_tg if m == "sendMessage"]
    assert len(sends) == 1 and "자동 삭제 중" in sends[0]["text"] and "계정 3명" in sends[0]["text"]
    # 이어서 올라오는 같은 문구는 바로 삭제, 새 알림 없이 기존 알림 수정(계정 수 변하지 않으면 1분 간격)
    SG.check_message(None, _msg(-1, 101, 4, text))
    assert ("deleteMessage", {"chat_id": -1, "message_id": 4}) in fake_tg
    assert len([1 for m, _ in fake_tg if m == "sendMessage"]) == 1


def test_burst_ban_button_bans_every_account(fake_tg):
    """'전원 차단·삭제' 버튼 → 묶음의 계정 전원을 모든 방에서 차단."""
    text = "무료 급등주 리딩방 지금 바로 문의주세요 7101"
    for cid, uid, mid in ((-1, 201, 1), (-2, 202, 2), (-3, 203, 3)):
        SG.check_message(None, _msg(cid, uid, mid, text))
    key = next(iter(SG._bursts.values()))["key"]
    status = SG.flood_action(key, "ban", 12345)
    bans = {(p["chat_id"], p["user_id"]) for m, p in fake_tg if m == "banChatMember"}
    assert bans == {(c, u) for c in (-1, -2, -3, "@industry") for u in (201, 202, 203)}
    assert "3명 전체 차단(4개 방)" in status
    edits = {p["message_id"] for m, p in fake_tg if m == "editMessageText"}
    assert 12345 in edits                     # 누른 알림도 결과로 갱신


def test_burst_single_account_alert_then_delete_button(fake_tg):
    """한 계정 다중방 → 삭제 안 하고 알림(삭제 버튼). 버튼 누르면 지우고 이후 자동 삭제."""
    text = "제가 정리한 오늘 시장 요약 공유드립니다 7201"
    for cid, mid in ((-1, 1), (-2, 2), (-3, 3)):
        SG.check_message(None, _msg(cid, 301, mid, text))
    assert not [c for c in fake_tg if c[0] == "deleteMessage"]
    send = [p for m, p in fake_tg if m == "sendMessage"][0]
    btns = [b["callback_data"] for b in send["reply_markup"]["inline_keyboard"][0]]
    assert btns[0].startswith("SPAM|fdel|")
    SG.flood_action(btns[0].split("|")[2], "del")
    assert len([1 for m, _ in fake_tg if m == "deleteMessage"]) == 3


def test_burst_ok_button_stops_deleting(fake_tg):
    """'정상' → 6시간 동안 같은 문구는 삭제·알림 안 함."""
    text = "고유문구 정상인데 여러 명이 같은 말 7301"
    for cid, uid, mid in ((-1, 401, 1), (-2, 402, 2), (-3, 403, 3)):
        SG.check_message(None, _msg(cid, uid, mid, text))
    key = next(iter(SG._bursts.values()))["key"]
    SG.flood_action(key, "ok")
    n = len(fake_tg)
    SG.check_message(None, _msg(-1, 404, 4, text))
    assert len(fake_tg) == n


def test_burst_excludes_room_admin(fake_tg):
    """방 관리자(999)의 같은 문구는 계정 수·삭제 대상에서 빠진다."""
    text = "고유문구 관리자 공지를 따라 쓴 경우 7401"
    SG.check_message(None, _msg(-1, 999, 1, text))   # 관리자
    SG.check_message(None, _msg(-2, 501, 2, text))
    SG.check_message(None, _msg(-3, 501, 3, text))   # 일반 1명 + 관리자 → 한 계정 다중방(알림만)
    assert not [c for c in fake_tg if c[0] == "deleteMessage"]


def test_edited_message_with_hidden_link_deleted(fake_tg):
    """올린 뒤 숨은 초대링크로 수정 → 삭제."""
    m = _msg(-1, 601, 8, "좋은 정보 있어요")
    m["entities"] = [{"type": "text_link", "offset": 0, "length": 2, "url": "https://t.me/+x"}]
    SG.check_message(None, m, edited=True)
    assert ("deleteMessage", {"chat_id": -1, "message_id": 8}) in fake_tg
    assert "수정된 메시지" in [p for k, p in fake_tg if k == "sendMessage"][0]["text"]
