# -*- coding: utf-8 -*-
"""뉴스봇 '몰리면 묶음' — 방당 60분 안 3건 개별 발송 뒤엔 1시간마다 묶음 1통(기사는 버리지 않음).

외부 의존 없음: test_reports처럼 import 전 config/managers/stock_api/supabase_bridge를 스텁하고
import 직후 걷어낸다. 실제 모듈을 불러오면 stock_api가 naver_report까지 캐시해, 뒤에 도는
test_reports가 스텁 없는 naver_report를 받아 실패한다(2026-10-27 실측).
"""
import datetime as _dt
import sys
import types


def _import_news_main():
    cfg = types.ModuleType("config")
    cfg.NAVER_KEYS, cfg.DEFAULT_CHAT_ID, cfg.COMPANY_KEYWORDS = [], "@main", []
    cfg.COMPANY_CODES, cfg.INDUSTRY_CHAT_IDS, cfg.COMMON_BUTTON, cfg.COMPANY_TO_INDUSTRY = {}, {}, None, {}
    mgr = types.ModuleType("managers")
    mgr.market_timer, mgr.HistoryManager, mgr.get_session = None, object, lambda: None
    sa = types.ModuleType("stock_api")
    sa.send_telegram = lambda *a, **k: True
    sb = types.ModuleType("supabase_bridge")          # bridge 없음 → news_main의 _BRIDGE_OK=False
    names = ("config", "managers", "stock_api", "supabase_bridge", "news_main")
    saved = {n: sys.modules.get(n) for n in names}
    sys.modules.update({"config": cfg, "managers": mgr, "stock_api": sa, "supabase_bridge": sb})
    sys.modules.pop("news_main", None)
    try:
        import news_main
        return news_main
    finally:
        for n, orig in saved.items():
            if orig is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = orig


nm = _import_news_main()


def _bot(monkeypatch, tmp_path):
    """네트워크·DB 없이 묶음 로직만 쓰는 봇 — __init__을 건너뛰고 필요한 상태만 채운다."""
    monkeypatch.setattr(nm, "BUNDLE_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(nm, "_BRIDGE_OK", False)
    bot = nm.NaverNewsBot.__new__(nm.NaverNewsBot)
    bot._room_sends, bot._bundles = {}, {}
    clock = {"now": _dt.datetime(2026, 10, 27, 13, 0)}

    class FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(nm, "datetime", types.SimpleNamespace(
        datetime=FakeDT, timedelta=_dt.timedelta, timezone=_dt.timezone))
    sent = []
    monkeypatch.setattr(nm.stock_api, "send_telegram",
                        lambda chat_id, text, preview=False, keyboard=None: sent.append((chat_id, text, preview)) or True)
    return bot, clock, sent


def test_route_bundles_after_three_and_flushes_hourly(monkeypatch, tmp_path):
    bot, clock, sent = _bot(monkeypatch, tmp_path)
    for i in range(6):                                   # 13:00~13:05 한 방에 6건
        clock["now"] = _dt.datetime(2026, 10, 27, 13, i)
        assert bot._route("room", "개별", ["한화오션", f"13:0{i}", f"기사{i}", f"https://n/{i}"])
    assert [s[2] for s in sent] == [True, True, True]    # 앞 3건만 개별(미리보기 켬)
    assert len(bot._bundles["room"]["items"]) == 3

    clock["now"] = _dt.datetime(2026, 10, 27, 13, 59)    # 첫 묶음 기사(13:03) 1시간 전 — 아직
    bot._flush_bundles()
    assert len(sent) == 3
    clock["now"] = _dt.datetime(2026, 10, 27, 14, 3)
    bot._flush_bundles()
    assert len(sent) == 4 and sent[-1][2] is False       # 묶음 1통(미리보기 끔)
    assert sent[-1][1].count("<a href=") == 3 and "· 한화오션" in sent[-1][1]
    assert bot._bundles == {}


def test_bundle_messages_multi_company_and_split(monkeypatch):
    items = [["삼성전자", "09:00", "제목 & <특징주>", "https://a?b=1&c=2"], ["SK하이닉스", "09:01", "제목2", "https://b"]]
    msg = nm.NaverNewsBot._bundle_messages(items)[0]
    assert "[삼성전자] " in msg and "[SK하이닉스] " in msg          # 산업방은 종목명
    assert "&lt;특징주&gt;" in msg and "&amp;c=2" in msg            # HTML 이스케이프
    monkeypatch.setattr(nm, "BUNDLE_MAX_CHARS", 120)
    many = [["루닛", "10:00", "가" * 40, f"https://x/{i}"] for i in range(6)]
    parts = nm.NaverNewsBot._bundle_messages(many)
    assert len(parts) > 1 and parts[1].startswith("📰 <b>[뉴스 묶음]</b> (이어서)")
    assert sum(p.count("<a href=") for p in parts) == 6           # 나눠도 기사 수 그대로
