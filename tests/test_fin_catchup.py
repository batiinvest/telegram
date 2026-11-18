# -*- coding: utf-8 -*-
"""재무 보충(늦게 뜬 재무) 테스트 — DB·DART 없이(가짜 클라이언트).

지키려는 것(2026-11-12~16 3분기 139곳 빈칸):
1. 매일 18:30 재무수집은 그날 목록을 한 번만 본다 — 그날 저녁 DART 재무 API에 아직 없던 회사(013)와
   18:30 뒤에 뜬 보고서는 다시 보지 않았다 → 07:30 보충이 최근 7일(어제까지)을 다시 본다.
2. 보충은 financials에 그 분기가 **없는** 상장(Y/K)·비스팩·active 회사만 받는다(있는 분기는 건드리지 않음).
3. 보고서 이름 → 분기 규칙은 18:30 잡과 같다(3·6·9·12월만).
"""
import datetime

import pandas as pd

import collect_financials as cf
import db_utils

COLS = ['corp_code', 'corp_name', 'stock_code', 'corp_cls', 'report_nm', 'rcept_no', 'rcept_dt']


def _df(rows):
    return pd.DataFrame(rows, columns=COLS)


def _r(cc, name, nm, rno, cls='Y'):
    return {'corp_code': cc, 'corp_name': name, 'stock_code': '000000', 'corp_cls': cls,
            'report_nm': nm, 'rcept_no': rno, 'rcept_dt': rno[:8]}


# ── 1. 보고서 이름 → 분기 ────────────────────────────────────────────────

def test_report_quarter():
    assert cf.report_quarter('분기보고서 (2026.09)') == ('2026', 'Q3')
    assert cf.report_quarter('반기보고서 (2026.06)') == ('2026', 'Q2')
    assert cf.report_quarter('분기보고서 (2026.03)') == ('2026', 'Q1')
    assert cf.report_quarter('사업보고서 (2025.12)') == ('2025', 'Q4')
    assert cf.report_quarter('[기재정정]분기보고서 (2026.09)') == ('2026', 'Q3')   # 정정 여부는 fin_report_rows가 거른다
    assert cf.report_quarter('분기보고서 (2026.08)') == (None, None)              # 3·6·9·12월 아님
    assert cf.report_quarter('감사보고서제출') == (None, None)
    assert cf.report_quarter(None) == (None, None)


def test_report_quarter_other_fiscal_year_end():
    # 3월 결산(메이슨캐피탈 등): DART는 4~9월 반기를 '2026 반기'로 둔다 — 달로 3분기를 찾으면 늘 없음(013)
    assert cf.report_quarter('반기보고서 (2026.09)') == (None, None)
    assert cf.report_quarter('분기보고서 (2026.06)') == (None, None)
    assert cf.report_quarter('분기보고서 (2026.12)') == (None, None)
    assert cf.report_quarter('사업보고서 (2026.03)') == (None, None)
    # 9월 결산
    assert cf.report_quarter('사업보고서 (2026.09)') == (None, None)
    assert cf.report_quarter('반기보고서 (2027.03)') == (None, None)


# ── 2. 보충 후보 ─────────────────────────────────────────────────────────

def test_catchup_candidates_filters_and_groups():
    df = _df([
        _r('B', '나', '반기보고서 (2026.06)', '20261112000200', cls='K'),
        _r('A', '가', '분기보고서 (2026.09)', '20261113000100'),
        _r('C', '코넥스', '분기보고서 (2026.09)', '20261113000300', cls='N'),     # 상장(Y/K) 아님
        _r('D', '비활성', '분기보고서 (2026.09)', '20261113000400'),               # companies.active 아님
        _r('E', '에이스팩1호', '분기보고서 (2026.09)', '20261113000500', cls='K'),  # 스팩
        _r('F', '정정만', '[기재정정]분기보고서 (2026.09)', '20261113000600'),     # 정정본은 트리거 아님
        _r('G', '결산8월', '분기보고서 (2026.08)', '20261113000700'),              # 3·6·9·12월 아님
        _r('J', '3월결산', '반기보고서 (2026.09)', '20261112000750', cls='K'),     # 결산월이 다른 회사 — 토요일 백필 몫
        _r('H', '연간', '사업보고서 (2025.12)', '20261111000800'),
        _r('A', '가', '분기보고서 (2026.09)', '20261116000900'),                   # 같은 회사·분기 다시 — 한 번만
        _r('I', '다', '분기보고서 (2026.09)', '20261112001000'),
    ])
    active = {'A', 'B', 'C', 'E', 'F', 'G', 'H', 'I', 'J'}
    out = cf.catchup_candidates(df, active)
    assert out == {('2025', 'Q4'): ['H'], ('2026', 'Q2'): ['B'], ('2026', 'Q3'): ['I', 'A']}
    # 오래된 공시가 든 분기가 먼저(상한에 걸리면 앞에서부터 받는다)
    assert list(out) == [('2025', 'Q4'), ('2026', 'Q2'), ('2026', 'Q3')]


def test_catchup_candidates_empty():
    assert cf.catchup_candidates(None, {'A'}) == {}
    assert cf.catchup_candidates(pd.DataFrame(), {'A'}) == {}     # 공시 없는 날(칸 없는 빈 목록)
    assert cf.catchup_candidates(_df([_r('A', '가', '감사보고서제출', 'r1')]), {'A'}) == {}


# ── 3. run_catchup: 없는 분기만, 기간·상한 ───────────────────────────────

class _Q:
    """sb.table(...).select(...).in_(...).eq(...).order(...).execute() — 걸린 조건대로 행을 거른다."""
    def __init__(self, rows):
        self._rows, self._f = rows, []

    def select(self, *a, **k):
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r, c=col, v=vals: r.get(c) in v)
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return type('R', (), {'data': [r for r in self._rows if all(f(r) for f in self._f)]})()


class _SB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


class _Dart:
    def __init__(self, df):
        self.df, self.calls = df, []

    def list(self, **kw):
        self.calls.append(kw)
        return self.df


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, msg))


def _setup(monkeypatch, df, companies, financials):
    dart, calls, log = _Dart(df), [], _Log()
    monkeypatch.setattr(cf, 'OpenDartReader', lambda key: dart)
    monkeypatch.setattr(cf, '_get_sb', lambda: _SB({'companies': companies, 'financials': financials}))
    monkeypatch.setattr(db_utils, 'fetch_all_pages', lambda q: q.execute().data)
    monkeypatch.setattr(cf, 'log', log)

    def fake_run(ccs, y, q):
        calls.append((list(ccs), y, q))
        return len(ccs) - (1 if 'NODATA' in ccs else 0), 0     # 'NODATA'는 DART 재무 아직 없음(성공·실패 둘 다 아님)
    monkeypatch.setattr(cf, 'run_by_corp_codes', fake_run)
    return dart, calls, log


def _co(*ccs, active=True):
    return [{'corp_code': c, 'code': c, 'active': active} for c in ccs]


def _fin(cc, y, q, fs='CFS'):
    return {'corp_code': cc, 'bsns_year': y, 'quarter': q, 'fs_div': fs}


def test_run_catchup_only_missing_quarters(monkeypatch):
    df = _df([
        _r('HAVE', '있음', '분기보고서 (2026.09)', '20261113000100'),
        _r('MISS', '없음', '분기보고서 (2026.09)', '20261116000200'),
        _r('NODATA', '아직', '분기보고서 (2026.09)', '20261116000300'),
        _r('Q2LATE', '반기늦음', '반기보고서 (2026.06)', '20261112000400', cls='K'),
        _r('OLDQ', '다른분기만', '분기보고서 (2026.09)', '20261114000500'),
        _r('OFF', '비활성', '분기보고서 (2026.09)', '20261116000600'),
    ])
    companies = _co('HAVE', 'MISS', 'NODATA', 'Q2LATE', 'OLDQ') + _co('OFF', active=False)
    financials = [_fin('HAVE', '2026', 'Q3', 'OFS'),        # 별도만 있어도 '있음'
                  _fin('OLDQ', '2026', 'Q2'),               # 다른 분기만 있으면 이 분기는 없음
                  _fin('Q2LATE', '2025', 'Q2')]
    dart, calls, log = _setup(monkeypatch, df, companies, financials)

    res = cf.run_catchup(today=datetime.date(2026, 11, 18))

    # 기간 = 7일 전 ~ 어제(오늘 공시는 18:30 재무수집 몫), 정기공시 전체(원본·정정 모두 받아 원본만 고른다)
    assert dart.calls == [{'start': '2026-11-11', 'end': '2026-11-17', 'kind': 'A', 'final': False}]
    assert calls == [(['Q2LATE'], '2026', 'Q2'), (['OLDQ', 'MISS', 'NODATA'], '2026', 'Q3')]
    assert res == {'reports': 5, 'missing': 4, 'tried': 4,
                   'quarters': {('2026', 'Q2'): (1, 0), ('2026', 'Q3'): (2, 0)}}
    assert any('정기보고서 상장사 5곳' in m and '재무 없음 4곳' in m for lv, m in log.lines if lv == 'info')


def test_run_catchup_nothing_missing(monkeypatch):
    df = _df([_r('A', '가', '분기보고서 (2026.09)', '20261116000100')])
    _, calls, log = _setup(monkeypatch, df, _co('A'), [_fin('A', '2026', 'Q3')])
    res = cf.run_catchup(today=datetime.date(2026, 11, 18))
    assert calls == []
    assert res == {'reports': 1, 'missing': 0, 'tried': 0, 'quarters': {}}
    assert not [m for lv, m in log.lines if lv == 'warning']


def test_run_catchup_no_filings(monkeypatch):
    _, calls, _ = _setup(monkeypatch, pd.DataFrame(), _co('A'), [])
    assert cf.run_catchup(today=datetime.date(2026, 11, 22)) == {'reports': 0, 'missing': 0, 'tried': 0, 'quarters': {}}
    assert calls == []


def test_run_catchup_cap_oldest_first(monkeypatch):
    # 대상이 상한을 넘으면(표가 비는 사고 등) 오래된 공시부터 상한만큼 — 나머지는 다음 날
    df = _df([_r('Q3A', '가', '분기보고서 (2026.09)', '20261116000100'),
              _r('Q2A', '나', '반기보고서 (2026.06)', '20261111000200'),
              _r('Q3B', '다', '분기보고서 (2026.09)', '20261116000300')])
    _, calls, log = _setup(monkeypatch, df, _co('Q3A', 'Q2A', 'Q3B'), [])
    res = cf.run_catchup(max_corps=2, today=datetime.date(2026, 11, 18))
    assert calls == [(['Q2A'], '2026', 'Q2'), (['Q3A'], '2026', 'Q3')]
    assert res['missing'] == 3 and res['tried'] == 2
    assert any('상한 2곳만' in m for lv, m in log.lines if lv == 'warning')
