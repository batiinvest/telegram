"""
collect_utils.py — 수집 스크립트 공통 유틸
────────────────────────────────────────────
collect_*.py / backfill_*.py 에서 반복되는 패턴을 통합합니다.

포함:
  - fetch_all_pages()  : 인라인 while True: .range() 패턴 통합 (db_utils 위임)
  - batch_upsert()     : for i in range(0, len(records), N): 패턴 통합
  - require_env()      : 필수 환경변수 검증
"""

import logging
import os

log = logging.getLogger(__name__)


# ── 페이지네이션 ──────────────────────────────────────────────────────────────

def fetch_all_pages(query_builder, page_size: int = 1000) -> list:
    """
    Supabase 전체 페이지 조회.
    db_utils.fetch_all_pages() 에 위임 (단일 진입점).

    사용법:
        from collect_utils import fetch_all_pages
        rows = fetch_all_pages(sb.from_('market_data').select('*').eq('col', val))
    """
    from db_utils import fetch_all_pages as _fap
    return _fap(query_builder, page_size)


# ── 배치 upsert ───────────────────────────────────────────────────────────────

def batch_upsert(sb, table: str, records: list,
                 conflict_col: str, chunk: int = 100) -> int:
    """
    레코드 리스트를 청크 단위로 upsert.
    collect_*.py 전반의 `for i in range(0, len(records), N):` 패턴 통합.

    Returns:
        성공 upsert 건수
    """
    if not records:
        return 0
    total = 0
    for i in range(0, len(records), chunk):
        batch = records[i:i + chunk]
        try:
            sb.table(table).upsert(batch, on_conflict=conflict_col).execute()
            total += len(batch)
        except Exception as e:
            log.error(f"[collect_utils] batch_upsert 오류 ({table} chunk {i}): {e}")
    return total


# ── 부분 컬럼 배치 갱신 ────────────────────────────────────────────────────────

def batch_update_existing(sb, table: str, records: list,
                          key_cols: tuple = ('stock_code', 'base_date'),
                          conflict_col: str = 'stock_code,base_date',
                          chunk: int = 500) -> int:
    """
    존재하는 행만 부분 컬럼 upsert (행 단위 update 루프의 배치 대체).

    upsert는 미존재 키에 스켈레톤 행(나머지 컬럼 null)을 만들어 버리므로,
    존재 키를 먼저 조회해 걸러낸 뒤 upsert 한다. records의 각 dict는
    key_cols를 포함하고 컬럼 구성이 동일해야 한다(PostgREST 벌크 제약).

    Returns:
        갱신(upsert) 건수 — 미존재로 스킵된 행은 제외
    """
    if not records:
        return 0
    k_code, k_date = key_cols
    by_date: dict = {}
    for r in records:
        by_date.setdefault(r[k_date], []).append(r)

    updatable = []
    for d, rows in by_date.items():
        codes = [r[k_code] for r in rows]
        existing = set()
        for i in range(0, len(codes), 500):
            try:
                res = sb.table(table).select(k_code) \
                        .eq(k_date, d).in_(k_code, codes[i:i + 500]).execute()
                existing.update(x[k_code] for x in (res.data or []))
            except Exception as e:
                log.error(f"[collect_utils] batch_update_existing 존재조회 오류 ({table} {d}): {e}")
        updatable.extend(r for r in rows if r[k_code] in existing)

    skipped = len(records) - len(updatable)
    if skipped:
        log.info(f"[collect_utils] {table} 미존재 {skipped}건 스킵 (스켈레톤 행 생성 방지)")
    # 존재 행만 부분 컬럼 UPDATE.
    # ⚠️ upsert(on_conflict)는 INSERT 후보의 NOT NULL(corp_name 등)을 충돌판정보다
    #   먼저 검사해 부분 갱신이 23502로 실패하므로, 행 단위 실제 UPDATE로 처리.
    updated = 0
    for r in updatable:
        vals = {c: v for c, v in r.items() if c not in (k_code, k_date)}
        if not vals:
            continue
        try:
            sb.table(table).update(vals) \
                .eq(k_code, r[k_code]).eq(k_date, r[k_date]).execute()
            updated += 1
        except Exception as e:
            log.error(f"[collect_utils] batch_update_existing UPDATE 오류 ({table} {r[k_code]} {r[k_date]}): {e}")
    return updated


# ── 환경변수 검증 ─────────────────────────────────────────────────────────────

def require_env(*names: str) -> dict:
    """
    필수 환경변수가 모두 설정됐는지 확인.
    없으면 RuntimeError 발생.

    Returns:
        {name: value} dict
    """
    result = {}
    missing = []
    for name in names:
        val = os.getenv(name, "")
        if not val:
            missing.append(name)
        else:
            result[name] = val
    if missing:
        raise RuntimeError(f"필수 환경변수 미설정: {', '.join(missing)}")
    return result
