# -*- coding: utf-8 -*-
"""collect_utils 테스트 — 수집 공용 유틸(가짜 Supabase 클라이언트로 DB 없이 검증).

batch_upsert는 여러 수집 스크립트가 채택한 공용 경로라 청킹·계속-on-error·
건수 반환 계약을 고정한다. batch_update_existing은 스켈레톤 행 방지 로직.
"""
import pytest

from collect_utils import (
    batch_upsert, batch_update_existing, require_env, fetch_all_pages,
)


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """table().upsert/select/update ... .execute() 체인 흉내."""
    def __init__(self, sb, table):
        self.sb = sb
        self.table = table
        self._kind = None
        self._payload = None
        self._filters = []

    def upsert(self, batch, on_conflict=None, ignore_duplicates=False):
        self._kind, self._payload = "upsert", batch
        self._upsert_kwargs = {"on_conflict": on_conflict,
                               "ignore_duplicates": ignore_duplicates}
        return self

    def update(self, vals):
        self._kind, self._payload = "update", vals
        return self

    def select(self, cols):
        self._kind = "select"
        return self

    def eq(self, k, v):
        self._filters.append(("eq", k, v))
        return self

    def in_(self, k, vals):
        self._filters.append(("in", k, list(vals)))
        return self

    def execute(self):
        if self._kind == "upsert":
            self.sb.upsert_kwargs.append(self._upsert_kwargs)
            if self.sb.fail_upsert:
                raise RuntimeError("upsert boom")
            self.sb.upserted.extend(self._payload)
            self.sb.upsert_calls += 1
            return FakeResp(self._payload)
        if self._kind == "select":
            return FakeResp(list(self.sb.existing_rows))
        if self._kind == "update":
            self.sb.updates.append((dict(self._payload), list(self._filters)))
            return FakeResp([])
        return FakeResp([])


class FakeSB:
    def __init__(self, fail_upsert=False, existing_rows=None):
        self.fail_upsert = fail_upsert
        self.existing_rows = existing_rows or []
        self.upserted = []
        self.upsert_calls = 0
        self.updates = []
        self.upsert_kwargs = []

    def table(self, t):
        return FakeQuery(self, t)


# ── batch_upsert ──

def test_batch_upsert_empty_no_calls():
    sb = FakeSB()
    assert batch_upsert(sb, "t", [], "id") == 0
    assert sb.upsert_calls == 0


def test_batch_upsert_chunks_and_counts():
    sb = FakeSB()
    recs = [{"id": i} for i in range(250)]
    n = batch_upsert(sb, "t", recs, "id", chunk=100)
    assert n == 250
    assert sb.upsert_calls == 3           # 100 + 100 + 50
    assert len(sb.upserted) == 250


def test_batch_upsert_continues_on_failure():
    """청크 실패 시 예외를 던지지 않고 0 반환(로그만) — 계약."""
    sb = FakeSB(fail_upsert=True)
    n = batch_upsert(sb, "t", [{"id": 1}, {"id": 2}], "id", chunk=100)
    assert n == 0
    assert sb.upserted == []


def test_batch_upsert_default_no_ignore_duplicates():
    """기본 호출(하위호환): ignore_duplicates 미전달과 동일하게 False."""
    sb = FakeSB()
    batch_upsert(sb, "t", [{"id": 1}], "id")
    assert sb.upsert_kwargs[0]["ignore_duplicates"] is False
    assert sb.upsert_kwargs[0]["on_conflict"] == "id"


def test_batch_upsert_ignore_duplicates_param():
    """ignore_duplicates=True → upsert에 전달."""
    sb = FakeSB()
    batch_upsert(sb, "t", [{"id": 1}], "id", ignore_duplicates=True)
    assert sb.upsert_kwargs[0]["ignore_duplicates"] is True


def test_batch_upsert_progress_label_logs(caplog):
    """progress_label 지정 시 청크 성공마다 진행 로그."""
    import logging
    sb = FakeSB()
    with caplog.at_level(logging.INFO):
        batch_upsert(sb, "t", [{"id": i} for i in range(150)], "id",
                     chunk=100, progress_label="DB 저장")
    msgs = [r.message for r in caplog.records]
    assert "DB 저장: 100/150개" in msgs
    assert "DB 저장: 150/150개" in msgs


def test_batch_upsert_sleep_between_chunks(monkeypatch):
    """sleep 파라미터 → 청크마다 time.sleep 호출."""
    import collect_utils
    slept = []
    monkeypatch.setattr(collect_utils.time, "sleep", lambda s: slept.append(s))
    sb = FakeSB()
    batch_upsert(sb, "t", [{"id": i} for i in range(150)], "id",
                 chunk=100, sleep=0.2)
    assert slept == [0.2, 0.2]   # 2청크


def test_batch_upsert_raise_on_error_aborts():
    """raise_on_error=True → 청크 실패를 재발생(중단)."""
    sb = FakeSB(fail_upsert=True)
    with pytest.raises(RuntimeError):
        batch_upsert(sb, "t", [{"id": 1}], "id", raise_on_error=True)


# ── require_env ──

def test_require_env_ok(monkeypatch):
    monkeypatch.setenv("FOO_X", "a")
    monkeypatch.setenv("BAR_X", "b")
    assert require_env("FOO_X", "BAR_X") == {"FOO_X": "a", "BAR_X": "b"}


def test_require_env_missing_raises(monkeypatch):
    monkeypatch.delenv("MISSING_ENV_XYZ", raising=False)
    with pytest.raises(RuntimeError):
        require_env("MISSING_ENV_XYZ")


# ── batch_update_existing ──

def test_batch_update_existing_skips_missing_keys():
    """존재 행(A)만 부분 update, 미존재(B)는 스킵 → 스켈레톤 행 방지."""
    sb = FakeSB(existing_rows=[{"stock_code": "A"}])
    recs = [
        {"stock_code": "A", "base_date": "2025-01-01", "price": 100},
        {"stock_code": "B", "base_date": "2025-01-01", "price": 200},
    ]
    n = batch_update_existing(sb, "market_data", recs)
    assert n == 1
    assert len(sb.updates) == 1
    vals, filters = sb.updates[0]
    assert vals == {"price": 100}                     # key_cols 제외한 부분 컬럼만
    assert ("eq", "stock_code", "A") in filters
    assert ("eq", "base_date", "2025-01-01") in filters


# ── fetch_all_pages (db_utils 위임) ──

class FakePagedQuery:
    def __init__(self, rows):
        self.rows = rows
        self._slice = []

    def range(self, start, end):
        self._slice = self.rows[start:end + 1]
        return self

    def execute(self):
        return FakeResp(self._slice)


def test_fetch_all_pages_paginates_until_short_page():
    rows = [{"i": i} for i in range(2500)]
    out = fetch_all_pages(FakePagedQuery(rows), page_size=1000)
    assert len(out) == 2500
    assert out[0] == {"i": 0}
    assert out[-1] == {"i": 2499}


def test_fetch_all_pages_single_short_page():
    rows = [{"i": i} for i in range(10)]
    out = fetch_all_pages(FakePagedQuery(rows), page_size=1000)
    assert len(out) == 10
