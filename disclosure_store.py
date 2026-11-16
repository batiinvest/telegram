"""
disclosure_store.py — 그날 공시 목록을 daily_disclosures에 저장 (삭제 → 묶음 삽입 → 다시 읽어 빠진 것 채우기)

job_collect_financials(18:30, 수동 트리거 포함)가 끝에서 부른다. 웹 공시 목록·18:55 종목별 저녁 요약이 이 표를 읽는다.
지키는 것: 그날 공시가 한 건도 빠지지 않는다.

왜 이렇게 하나 (2026-11-16 조사)
  - 묶음 삽입이 오류를 내도 서버엔 저장돼 있을 수 있다 — 저장은 끝났는데 연결이 끊겨 응답만 잃는 경우.
    예전 코드는 같은 묶음을 그대로 다시 넣다가 중복 키(23505)로 실패하고 '청크 유실'로 적었다.
    11-13 청크 3이 그랬다: 로그는 유실이었지만 표엔 그날 643건이 모두 있었다(같은 오류 07-23·07-31·11-16).
    그래서 오류 난 묶음을 그대로 다시 넣지 않고, 다 넣은 뒤 표를 다시 읽어 빠진 접수번호만 다시 넣는다.
  - upsert(on_conflict='base_date,rcept_no')로는 못 막는다 — 고유 인덱스 idx_daily_disc_unique가
    ON CONFLICT 대상으로 잡히지 않는다(42P10, 11-16 확인).
  - 다시 넣을 땐 묶음 → 그래도 실패하면 한 건씩. 한 건 때문에 100건을 잃지 않게 한다.
    한 건 넣기의 중복 키는 '이미 들어가 있음'이라 성공으로 친다.
  - 같은 접수번호는 한 번만 넣는다(표가 base_date+rcept_no 고유). 접수번호 없는 행은 키로 가를 수 없어 모두 두되,
    다시 읽어 확인할 수도 없으니 오류 난 묶음에 있던 것만 다시 넣는다.
  - 같은 날을 두 실행이 겹쳐 저장해도(수동 트리거·재기동 직후 재실행) 서로 지운 것을 확인 단계가 채운다.
"""
import logging
import time

from collect_utils import safe_execute
from db_utils import fetch_all_pages

log = logging.getLogger(__name__)

TABLE = 'daily_disclosures'
CHUNK = 100
GIVE_UP_STREAK = 10   # 한 건씩 넣다가 연달아 이만큼 실패하면 DB 장애로 보고 멈춘다(건마다 재시도로 오래 끌지 않게)


def dedupe_by_rcept(rows: list) -> list:
    """같은 접수번호는 처음 것만 남긴다(순서 유지). 접수번호가 없는 행은 서로 다른 공시로 보고 모두 둔다."""
    seen, out = set(), []
    for r in rows:
        rno = r.get('rcept_no')
        if rno:
            if rno in seen:
                continue
            seen.add(rno)
        out.append(r)
    return out


def is_dup_key(e) -> bool:
    """Postgres 고유 위반(23505) — 그 키가 이미 표에 있다."""
    return getattr(e, 'code', None) == '23505' or 'duplicate key' in str(e)


def _saved_rcept(sb, base_date: str) -> set:
    """그날 표에 들어가 있는 접수번호 — 페이지로 다 읽는다(실적 시즌 하루 900건 안팎)."""
    rows = fetch_all_pages(sb.table(TABLE).select('rcept_no').eq('base_date', base_date).order('id'))
    return {r['rcept_no'] for r in rows if r.get('rcept_no')}


def _insert_one_by_one(sb, rows: list) -> list:
    """한 건씩 넣는다. 반환 = 끝내 못 넣은 행."""
    failed, streak = [], 0
    for n, r in enumerate(rows):
        if streak >= GIVE_UP_STREAK:
            log.error(f"❌ [공시저장] 한 건씩 넣기 {streak}건 연속 실패 — 남은 {len(rows) - n}건 중단")
            failed.extend(rows[n:])
            break
        ok, err = False, None
        for attempt in (1, 2):
            try:
                sb.table(TABLE).insert(r).execute()
                ok = True
            except Exception as e:
                ok = is_dup_key(e)   # 이미 있음 — 앞선 시도가 응답만 잃고 저장된 경우
                err = e
            if ok:
                break
            if attempt == 1:
                time.sleep(1)
        if ok:
            streak = 0
        else:
            streak += 1
            failed.append(r)
            log.error(f"❌ [공시저장] 저장 실패 {r.get('rcept_no')} {r.get('corp_name')} · {r.get('report_nm')}: {err}")
    return failed


def _insert_missing(sb, rows: list) -> list:
    """빠진 행을 묶음으로 다시 넣고, 묶음이 실패하면 그 묶음만 한 건씩. 반환 = 끝내 못 넣은 행."""
    failed = []
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        try:
            sb.table(TABLE).insert(part).execute()
        except Exception as e:
            log.warning(f"⚠️ [공시저장] 다시 넣기 묶음 오류({len(part)}건) — 한 건씩 넣음: {e}")
            failed += _insert_one_by_one(sb, part)
    return failed


def store_daily_disclosures(sb, base_date: str, rows: list) -> dict:
    """base_date 행을 지우고 rows를 넣은 뒤, 표를 다시 읽어 빠진 것을 채운다.

    rows가 비면 아무것도 하지 않는다(지우지도 않음 — 0건이면 기존 보존은 호출부 규칙).
    삭제가 끝내 실패하면 예외를 올린다(넣기 전이라 기존 행은 그대로).
    반환: {'want': 넣을 건수(같은 접수번호 뺀 것), 'saved': 저장된 건수, 'failed': [못 넣은 접수번호],
           'retried': 다시 넣은 건수, 'verified': 표를 다시 읽어 확인했는지}
    """
    rows = dedupe_by_rcept(rows)
    out = {'want': len(rows), 'saved': 0, 'failed': [], 'retried': 0, 'verified': False}
    if not rows:
        return out

    # 지우기는 다시 해도 결과가 같아 연결 오류면 재시도한다
    safe_execute(sb.table(TABLE).delete().eq('base_date', base_date), label=TABLE + ' 삭제')

    errored = []   # 오류 난 묶음의 행 — 오류여도 저장됐을 수 있어 그대로 다시 넣지 않는다
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        try:
            sb.table(TABLE).insert(part).execute()
        except Exception as e:
            log.warning(f"⚠️ [공시저장] 묶음 {i // CHUNK} 삽입 오류({len(part)}건) — 끝에 표를 다시 읽어 빠진 것만 다시 넣음: {e}")
            errored += part

    try:
        have = _saved_rcept(sb, base_date)
        out['verified'] = True
        pending = [r for r in rows if r.get('rcept_no') and r['rcept_no'] not in have]
    except Exception as e:
        log.warning(f"⚠️ [공시저장] 저장 확인 조회 실패 — 오류 난 묶음만 다시 넣음(중복 키는 이미 있음으로): {e}")
        pending = [r for r in errored if r.get('rcept_no')]
    pending += [r for r in errored if not r.get('rcept_no')]   # 접수번호 없는 행은 확인할 수 없어 오류 난 묶음 것만

    failed = []
    if pending:
        out['retried'] = len(pending)
        log.info(f"🔁 [공시저장] 오류 난 묶음 {len(errored)}건 · 확인 결과 빠진 {len(pending)}건 다시 넣음")
        failed = _insert_missing(sb, pending)
    elif errored:
        log.info(f"✅ [공시저장] 오류 난 묶음 {len(errored)}건은 이미 저장돼 있음(응답만 잃음)")

    out['failed'] = [r.get('rcept_no') for r in failed]
    out['saved'] = len(rows) - len(failed)
    if failed:
        shown = ', '.join(str(x) for x in out['failed'][:20])
        log.error(f"❌ [공시저장] {len(failed)}건 저장 못 함 — daily_disclosures 부분 저장: {shown}")
    return out
