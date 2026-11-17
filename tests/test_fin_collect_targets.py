# -*- coding: utf-8 -*-
"""재무수집 대상·집계 테스트 — DB·DART 없이(가짜 클라이언트).

지키려는 것(2026-11-13~16 3분기 재무 누락):
1. 같은 날 정정본이 올라온 회사도 원본 정기보고서로 수집 대상이 된다 — 최종본(final=True) 목록엔
   정정본만 남아, 정정 제외 규칙에 걸려 통째로 빠졌다(11-16 종근당·한국가스공사 등 7곳).
2. DART 재무 API에 아직 없는 회사(collect_one → None)를 '성공'으로 세지 않는다 — '성공 809 / 실패 0'인데
   실제 저장은 700이었다(11-13; 11-16은 1,429 중 1,081).
"""
import pandas as pd

import collect_financials as cf
import db_utils

COLS = ['corp_code', 'corp_name', 'stock_code', 'corp_cls', 'report_nm', 'rcept_no', 'rcept_dt']


def _df(rows):
    return pd.DataFrame(rows, columns=COLS)


def _r(cc, name, nm, rno, cls='Y'):
    return {'corp_code': cc, 'corp_name': name, 'stock_code': '000000', 'corp_cls': cls,
            'report_nm': nm, 'rcept_no': rno, 'rcept_dt': '20261116'}


# ── 1. 수집 대상(fin_report_rows) ─────────────────────────────────────────

def test_same_day_correction_keeps_original():
    # 종근당 11-16: 원본 001990 → 같은 날 [기재정정] 002243. 최종본 목록엔 정정본만 남는다.
    all_ = _df([_r('00992871', '종근당', '분기보고서 (2026.09)', '20261116001990'),
                _r('00992871', '종근당', '[기재정정]분기보고서 (2026.09)', '20261116002243')])
    fin = _df([_r('00992871', '종근당', '[기재정정]분기보고서 (2026.09)', '20261116002243')])
    assert list(cf.fin_report_rows(all_, fin)['rcept_no']) == ['20261116001990']
    # 예전처럼 최종본만 보면 통째로 빠진다
    assert cf.fin_report_rows(fin).empty


def test_corrections_and_other_reports_never_trigger():
    rows = _df([_r('1', 'A', '[기재정정]사업보고서 (2025.12)', 'r1'),
                _r('2', 'B', '[첨부정정]분기보고서 (2026.09)', 'r2'),
                _r('3', 'C', '감사보고서제출', 'r3'),
                _r('4', 'D', '임원ㆍ주요주주특정증권등소유상황보고서', 'r4'),
                _r('5', 'E', '반기보고서 (2026.06)', 'r5'),
                _r('6', 'F', '사업보고서 (2025.12)', 'r6')])
    assert list(cf.fin_report_rows(rows)['rcept_no']) == ['r5', 'r6']


def test_union_of_lists_dedups_by_rcept_no():
    # 두 목록에 같은 원본이 있으면 한 번만, 한쪽에만 있는 것도 들어간다. 칸(corp_cls 등)은 그대로.
    a = _df([_r('1', 'A', '분기보고서 (2026.09)', 'r1'), _r('2', 'B', '분기보고서 (2026.09)', 'r2', cls='K')])
    f = _df([_r('1', 'A', '분기보고서 (2026.09)', 'r1'), _r('3', 'C', '분기보고서 (2026.09)', 'r3', cls='N')])
    out = cf.fin_report_rows(a, f)
    assert sorted(out['rcept_no']) == ['r1', 'r2', 'r3']
    assert dict(zip(out['rcept_no'], out['corp_cls'])) == {'r1': 'Y', 'r2': 'K', 'r3': 'N'}


def test_empty_inputs():
    assert cf.fin_report_rows().empty
    assert cf.fin_report_rows(None, pd.DataFrame()).empty
    assert cf.fin_report_rows(_df([])).empty
    only_other = _df([_r('1', 'A', '감사보고서제출', 'r1')])
    assert cf.fin_report_rows(only_other).empty


# ── 2. 집계(run_by_corp_codes) ───────────────────────────────────────────

class _Q:
    """sb.table(...).select(...).in_(...).eq(...).order(...).execute() 체인 흉내."""
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        return type('R', (), {'data': self._data})()


class _SB:
    def __init__(self, companies):
        self.companies = companies

    def table(self, name):
        return _Q(self.companies if name == 'companies' else [])


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, msg))


def test_nodata_is_not_success(monkeypatch):
    comps = [{'corp_code': 'c1', 'code': '000001', 'name': '받음'},
             {'corp_code': 'c2', 'code': '000002', 'name': '아직없음'},
             {'corp_code': 'c3', 'code': '000003', 'name': '오류남'}]

    def fake_collect(dart, corp_code, stock_code, corp_name, year, quarter):
        if corp_code == 'c1':
            return {'corp_code': 'c1', 'stock_code': stock_code, 'corp_name': corp_name,
                    'bsns_year': year, 'quarter': quarter, 'fs_div': 'CFS'}
        if corp_code == 'c2':
            return None                      # DART 재무 API 013(조회된 데이타가 없습니다)
        raise RuntimeError('연결 끊김')

    saved, log = [], _Log()
    monkeypatch.setattr(cf, '_get_sb', lambda: _SB(comps))
    monkeypatch.setattr(cf, 'OpenDartReader', lambda key: object())
    monkeypatch.setattr(cf, 'collect_one', fake_collect)
    monkeypatch.setattr(cf, 'cumulative_prev_cf', lambda *a, **k: None)
    monkeypatch.setattr(cf, 'convert_to_pure_quarter', lambda row, prev: row)
    monkeypatch.setattr(cf, 'calculate_growth_rates', lambda cache, row: {})
    monkeypatch.setattr(cf.time, 'sleep', lambda s: None)
    monkeypatch.setattr(db_utils, 'fetch_all_pages', lambda q: [])
    monkeypatch.setattr(cf, 'batch_upsert', lambda sb, table, rows, conflict, **kw: saved.extend(rows) or len(rows))
    monkeypatch.setattr(cf, 'log', log)

    ok, fail = cf.run_by_corp_codes(['c1', 'c2', 'c3'], '2026', 'Q3')

    assert (ok, fail) == (1, 1)              # 예전엔 (2, 1) — 못 받은 c2까지 성공으로 셌다
    assert [r['corp_code'] for r in saved] == ['c1']
    warns = [m for lv, m in log.lines if lv == 'warning' and 'DART 재무 없음' in m]
    assert len(warns) == 1 and '아직없음' in warns[0] and '1곳' in warns[0]
    assert any('성공 1 / DART 재무 없음 1 / 실패 1' in m for lv, m in log.lines if lv == 'info')


def test_all_collected_logs_no_warning(monkeypatch):
    comps = [{'corp_code': 'c1', 'code': '000001', 'name': '받음'}]
    log = _Log()
    monkeypatch.setattr(cf, '_get_sb', lambda: _SB(comps))
    monkeypatch.setattr(cf, 'OpenDartReader', lambda key: object())
    monkeypatch.setattr(cf, 'collect_one', lambda dart, cc, sc, nm, year, quarter: {
        'corp_code': cc, 'stock_code': sc, 'corp_name': nm, 'bsns_year': year, 'quarter': quarter, 'fs_div': 'OFS'})
    monkeypatch.setattr(cf, 'convert_to_pure_quarter', lambda row, prev: row)
    monkeypatch.setattr(cf, 'calculate_growth_rates', lambda cache, row: {})
    monkeypatch.setattr(db_utils, 'fetch_all_pages', lambda q: [])
    monkeypatch.setattr(cf, 'batch_upsert', lambda *a, **k: 1)
    monkeypatch.setattr(cf, 'log', log)

    assert cf.run_by_corp_codes(['c1'], '2026', 'Q1') == (1, 0)
    assert not [m for lv, m in log.lines if lv == 'warning']
