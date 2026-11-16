# -*- coding: utf-8 -*-
"""disclosure_store 테스트 — 그날 공시 저장이 한 건도 빠지지 않는지(가짜 Supabase, DB 없이).

지키려는 것(2026-11-13 사고): 묶음 삽입이 '저장은 됐는데 응답만 끊긴' 오류를 내도 유실로 적거나
같은 묶음을 다시 넣다 중복 키로 잃지 않는다. 한 건이 불량이면 그 한 건만 빠진다.
"""
import pytest

import collect_utils
import db_utils
import disclosure_store as ds

DAY = '2026-11-13'


class FakeAPIError(Exception):
    """postgrest APIError 흉내 — .code와 dict 모양 문자열."""
    def __init__(self, code, message):
        super().__init__(str({'message': message, 'code': code, 'hint': None, 'details': None}))
        self.code = code


class FakeResp:
    def __init__(self, data):
        self.data = data


class FakeDB:
    """daily_disclosures 한 표 — (base_date, rcept_no) 고유(접수번호 NULL은 고유 검사 밖), 묶음 삽입은 원자적."""
    def __init__(self):
        self.rows, self.next_id = [], 1
        self.insert_calls = 0
        self.committed_then_drop = set()   # 이 번째 insert는 저장한 뒤 연결 끊김 오류(11-13 청크 3)
        self.drop_before_commit = set()    # 이 번째 insert는 저장 전에 연결 끊김
        self.bad_rcept = set()             # 이 접수번호가 든 묶음은 값 오류로 통째 실패
        self.always_fail = False           # DB 장애 — 모든 insert 실패
        self.select_fail = False           # 확인 조회 실패
        self.delete_drop_once = False      # 첫 삭제는 지운 뒤 연결 끊김
        self.after_insert = None           # hook(db, n) — 다른 실행이 같은 날을 지우는 상황 등
        self.delete_calls = 0

    def table(self, name):
        assert name == 'daily_disclosures'
        return FakeQuery(self)

    def day(self, d=DAY):
        return [r for r in self.rows if r['base_date'] == d]

    def _insert(self, payload):
        self.insert_calls += 1
        n = self.insert_calls
        batch = payload if isinstance(payload, list) else [payload]
        if n in self.drop_before_commit:
            raise Exception('Server disconnected without sending a response.')
        if self.always_fail:
            raise FakeAPIError('57014', 'canceling statement due to statement timeout')
        if any(r.get('rcept_no') in self.bad_rcept for r in batch):
            raise FakeAPIError('22P02', 'invalid input syntax')
        keys = {(r['base_date'], r['rcept_no']) for r in self.rows if r.get('rcept_no')}
        for r in batch:
            if not r.get('rcept_no'):
                continue
            k = (r['base_date'], r['rcept_no'])
            if k in keys:
                raise FakeAPIError('23505', 'duplicate key value violates unique constraint "idx_daily_disc_unique"')
            keys.add(k)
        for r in batch:
            self.rows.append(dict(r, id=self.next_id))
            self.next_id += 1
        if n in self.committed_then_drop:
            raise Exception('Server disconnected')
        if self.after_insert:
            self.after_insert(self, n)


class FakeQuery:
    def __init__(self, db):
        self.db, self.kind, self.payload, self.filters, self.rng = db, None, None, [], None

    def insert(self, payload):
        self.kind, self.payload = 'insert', payload
        return self

    def delete(self):
        self.kind = 'delete'
        return self

    def select(self, cols):
        self.kind = 'select'
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def execute(self):
        db = self.db
        if self.kind == 'insert':
            db._insert(self.payload)
            return FakeResp([])
        match = [r for r in db.rows if all(r.get(c) == v for c, v in self.filters)]
        if self.kind == 'delete':
            db.delete_calls += 1
            ids = {r['id'] for r in match}
            db.rows = [r for r in db.rows if r['id'] not in ids]
            if db.delete_drop_once:
                db.delete_drop_once = False
                raise Exception('Server disconnected')
            return FakeResp(match)
        if db.select_fail:
            raise Exception('Server disconnected')
        match.sort(key=lambda r: r['id'])
        if self.rng:
            match = match[self.rng[0]:self.rng[1] + 1]
        return FakeResp([{'rcept_no': r.get('rcept_no')} for r in match])


def _rows(n, day=DAY, start=1000):
    return [{'base_date': day, 'corp_code': f'{i:08d}', 'corp_name': f'회사{i}',
             'report_nm': '분기보고서 (2026.09)', 'rcept_no': f'{day.replace("-", "")}{start + i:06d}',
             'rcept_dt': day.replace('-', ''), 'category': '분기보고서',
             'market_cap': None, 'insider_summary': None} for i in range(n)]


def _keys(db, d=DAY):
    return sorted(r['rcept_no'] for r in db.day(d) if r.get('rcept_no'))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    for mod in (ds, collect_utils, db_utils):
        monkeypatch.setattr(mod.time, 'sleep', lambda s: None)


# ── 중복 거르기 ──────────────────────────────────────────────────────────────

def test_dedupe_keeps_first_and_rows_without_rcept():
    rows = [{'rcept_no': 'A', 'v': 1}, {'rcept_no': 'B'}, {'rcept_no': 'A', 'v': 2},
            {'rcept_no': None}, {'rcept_no': ''}, {'rcept_no': None}]
    out = ds.dedupe_by_rcept(rows)
    assert [r.get('rcept_no') for r in out] == ['A', 'B', None, '', None]
    assert out[0]['v'] == 1


# ── 저장 ────────────────────────────────────────────────────────────────────

def test_normal_save_replaces_only_that_day():
    db = FakeDB()
    db.rows = [dict(r, id=i) for i, r in enumerate(_rows(5, start=9000) + _rows(3, day='2026-11-12'), 1)]
    db.next_id = 100
    want = _rows(250)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res == {'want': 250, 'saved': 250, 'failed': [], 'retried': 0, 'verified': True}
    assert _keys(db) == sorted(r['rcept_no'] for r in want)      # 예전 그날 행은 지워짐
    assert len(db.day('2026-11-12')) == 3                         # 다른 날은 그대로
    assert db.insert_calls == 3


def test_committed_but_dropped_chunk_is_not_lost_or_reinserted(caplog):
    """11-13 청크 3 — 저장된 뒤 연결이 끊긴 묶음. 예전엔 같은 묶음을 다시 넣다 중복 키로 '유실'이라 적었다."""
    db = FakeDB()
    db.committed_then_drop = {4}           # 4번째 insert = 청크 3
    want = _rows(643)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['saved'] == 643 and res['failed'] == [] and res['retried'] == 0
    assert _keys(db) == sorted(r['rcept_no'] for r in want)
    assert len(db.day()) == 643                                   # 두 번 들어가지도 않음
    assert db.insert_calls == 7                                   # 다시 넣기 없음
    assert not [r for r in caplog.records if r.levelname == 'ERROR']


def test_chunk_dropped_before_commit_is_reinserted():
    db = FakeDB()
    db.drop_before_commit = {2}
    want = _rows(250)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['saved'] == 250 and res['retried'] == 100 and res['failed'] == []
    assert _keys(db) == sorted(r['rcept_no'] for r in want)


def test_one_bad_row_loses_only_itself():
    db = FakeDB()
    want = _rows(250)
    bad = want[150]['rcept_no']
    db.bad_rcept = {bad}
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['failed'] == [bad]
    assert res['saved'] == 249
    assert _keys(db) == sorted(r['rcept_no'] for r in want if r['rcept_no'] != bad)


def test_duplicate_rcept_in_input_saved_once():
    want = _rows(250)
    dup_in_chunk = dict(want[10])                 # 같은 묶음 안
    dup_across = dict(want[99])                   # 묶음 경계 너머(청크 0의 끝 → 청크 1)
    rows = want[:50] + [dup_in_chunk] + want[50:100] + [dup_across] + want[100:]
    db = FakeDB()
    res = ds.store_daily_disclosures(db, DAY, rows)
    assert res['want'] == 250 and res['saved'] == 250 and res['failed'] == []
    assert len(db.day()) == 250


def test_verify_read_failure_falls_back_to_errored_rows():
    db = FakeDB()
    db.committed_then_drop = {1}       # 청크 0은 저장됐는데 오류
    db.drop_before_commit = {2}        # 청크 1은 저장 안 됨
    db.select_fail = True
    want = _rows(250)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['verified'] is False
    assert res['failed'] == [] and res['saved'] == 250
    assert len(db.day()) == 250 and _keys(db) == sorted(r['rcept_no'] for r in want)


def test_rows_without_rcept_in_failed_chunk_are_reinserted():
    db = FakeDB()
    db.drop_before_commit = {1}
    want = _rows(120)
    want[5]['rcept_no'] = None
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['saved'] == 120 and res['failed'] == []
    assert len(db.day()) == 120


def test_other_run_wiping_the_day_midway_is_refilled():
    """수동 트리거·재기동 직후 재실행이 같은 날을 지워도 확인 단계가 빠진 것을 채운다."""
    def wipe_after_second(db, n):
        if n == 2:
            db.rows = [r for r in db.rows if r['base_date'] != DAY]
    db = FakeDB()
    db.after_insert = wipe_after_second
    want = _rows(300)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['saved'] == 300 and res['retried'] == 200 and res['failed'] == []
    assert _keys(db) == sorted(r['rcept_no'] for r in want)


def test_delete_dropped_after_commit_is_retried():
    db = FakeDB()
    db.rows = [dict(r, id=i) for i, r in enumerate(_rows(5, start=9000), 1)]
    db.next_id = 100
    db.delete_drop_once = True
    want = _rows(50)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert db.delete_calls == 2 and res['saved'] == 50
    assert _keys(db) == sorted(r['rcept_no'] for r in want)


def test_empty_rows_deletes_nothing():
    db = FakeDB()
    db.rows = [dict(r, id=i) for i, r in enumerate(_rows(5), 1)]
    res = ds.store_daily_disclosures(db, DAY, [])
    assert res['want'] == 0 and db.delete_calls == 0 and len(db.day()) == 5


def test_db_outage_gives_up_quickly():
    db = FakeDB()
    db.always_fail = True
    want = _rows(250)
    res = ds.store_daily_disclosures(db, DAY, want)
    assert res['saved'] == 0 and len(res['failed']) == 250
    # 첫 넣기 3묶음 + 묶음마다(다시 넣기 1 + 한 건씩 연속 실패 한도까지 2회씩)
    assert db.insert_calls <= 3 + 3 * (1 + 2 * ds.GIVE_UP_STREAK)
