# BatiInvest Backend

한국 주식 투자 대시보드 [BatiInvest](https://bati-dzc.pages.dev)의 백엔드.
시장·재무 데이터를 수집해 Supabase에 저장하고, 텔레그램 채널로 공시·뉴스·시세 알림과
정기 브리핑을 자동 발송하는 상주 데몬입니다.

## 시스템 구성

```
┌─ 프론트엔드 ── Vanilla JS SPA (Cloudflare Pages, 별도 repo)
│                    │ 읽기 (anon key)          │ 쓰기 요청 (bot_requests 큐)
├─ DB ────────── Supabase (PostgreSQL) ◄────────┘
│                    ▲ 쓰기 (service key)
└─ 백엔드 ────── 이 repo — GCP 서버 systemd 서비스(bati_bot)로 상주
                     run_all.py 가 봇 3종 + 스케줄러 + 워치독 스레드를 기동
```

- **외부 API**: KIS(한국투자증권 시세·수급·투자의견), DART(공시·재무), 네이버(뉴스 검색·증권 리포트), yfinance(글로벌 매크로), KIND(IR자료), Google Gemini(AI 요약), Telegram Bot API
- **발송 채널**: 메인(@BatiInvestChat) + 산업방 12개 + 종목방 60여 개 + 아카이브(@batiarchive) — 매핑은 Supabase `rooms` 테이블이 단일 출처

## 모듈 구조 (2026-07 물리 분할)

### 엔트리포인트·잡
| 파일 | 역할 |
|---|---|
| `run_all.py` | 엔트리포인트 — 스케줄 등록 + 봇 스레드 기동 + 워치독 루프만 |
| `job_infra.py` | `_job` 데코레이터(휴장일/토글/결과기록/실패알림), 브로드캐스트 헬퍼, 브릿지 |
| `jobs_collect.py` | 수집·집계 잡 22개 (재무·시장·수급·신고가·공매도·매크로·요약 생성 등) |
| `jobs_briefing.py` | 점심/마감 브리핑, 리포트, KIND IR, 프로채널, 주말 랭킹, 일일 운영 요약 |
| `watchdog_flags.py` | 대시보드 수동 트리거(ts-flag), 종목 재로드+신규 백필, bot_requests 큐 |

### 봇 (상시 스레드)
| 파일 | 역할 |
|---|---|
| `main.py` | DART 공시 봇 — 평일 07~19시 매분 폴링, 중요도 4단계(urgent/major/normal/skip) 채널 라우팅 |
| `news_main.py` | 네이버 뉴스 봇 — 모니터링 종목 상시 스윕, 스팸/실질보도/중복(해시·유사도·이벤트) 필터 |
| `realtime_alert.py` | KIS 시세 감시(장중 30초) — 급등/상·하한가/VI 알림 + 텔레그램 명령어 리스너 + 스팸 모더레이션 |

### 공통 인프라
| 파일 | 역할 |
|---|---|
| `config.py` | 환경변수·상수 + 공유 컨테이너(종목/채팅방 매핑). `init_config()` 1회 로드, 재로드는 in-place 갱신 |
| `managers.py` | KIS 토큰·호출(전역 RateLimiter 15/s), 텔레그램 발송(4096 분할·429 retry_after·DRY_RUN), 시장시간/휴장일 |
| `kis_client.py` | KIS 시세 조회 + inquire-price 45초 메모리 캐시 (수집기는 stock_api 대신 이것만 import) |
| `stock_api.py` | 텔레그램 메시지 빌더(전광판·랭킹·종목 브리핑 등) + 하위호환 facade |
| `naver_report.py` | 네이버 증권 리포트 수집 → PDF 전송 + Gemini 구조화 캡션(옵션) |
| `supabase_bridge.py` | 봇 런타임용 클라이언트, app_config 5분 캐시, heartbeat, rooms/키워드 로드 |
| `db_client.py` / `db_utils.py` / `collect_utils.py` | 수집용 클라이언트 싱글톤, 페이지네이션, 배치 upsert/부분갱신 |
| `logger_config.py` | 중앙 로깅 — 민감정보 자동 마스킹 + RotatingFileHandler(10MB×5) |
| `format_utils.py` / `telegram_utils.py` | 숫자·등락률 포맷, 관리자 채팅방 조회 |

### 수집기 (스케줄/CLI 겸용)
`collect_market`(KIS 시세→market_data) · `collect_financials`(DART 재무) ·
`collect_listed_companies`(상장사 동기화) · `collect_macro`(글로벌 지수·환율·원자재) ·
`collect_insider`(지분공시) · `collect_short`(공매도) · `collect_sector_summary`(산업 일별 집계) ·
`collect_us_etf`(미국 산업 ETF) · `collect_estimates`(추정실적) · `collect_company_info`(기업개황) ·
`leading_stocks_generator`(주도주 Top50) · `market_summary_generator`(투자포인트 요약) ·
`grade.py`(실적 등급 S/A/B/관찰) · `ai_analyst.py`(Gemini 공시/리포트 요약) · `kind_ir.py`(IR자료) ·
`backfill_*.py`(과거 데이터 수동 백필) · `check_*.py`/`verify_*.py`(정합성 점검)

### 멤버십·운영
`pro_channel.py`(월정액 구독 관리) · `sms_webhook.py`+`sms_parser.py`(입금 SMS 자동 처리, Flask :5001) ·
`room_access.py`(유료방 1회 입장) · `inactive_kick.py`(미접속 강퇴, Telethon) ·
`spam_guard.py`(광고 자동삭제·차단) · `bot_requests.py`(대시보드 쓰기 요청 큐) · `bot_commands.py`(/myid 등)

## 주요 스케줄 (KST, run_all.py 등록 기준)

| 시각 | 작업 |
|---|---|
| 06:30 / 16:10 | 글로벌 매크로 수집 (아침엔 메인채널 브리핑 포함) |
| 08:50 / 18:00 | 네이버 증권 리포트 수집·발송 |
| 09:30 / 12:00 | 모니터링 종목 시세 수집 |
| 09:35~15:35 (5회) | 기관/외국인 수급 가집계 |
| 11:30 / 18:30 | 점심 / 마감 브리핑 (메인+산업방+종목방) |
| 16:30 | 52주 신고가 수집·알림 |
| 17:00 | 전체 상장사 마감 수집 + 수익률 계산 + 관심가·시장경보 알림 |
| 17:05 / 17:15 / 17:30 | 공매도 급증 / 산업 집계 / 주도주 스코어 |
| 18:15 | 종목별 수급 확정 정산 |
| 18:30 / 18:40 | 공시 기반 재무 수집·투자포인트 요약 / 추정실적 |
| 19:50 | 일일 운영 요약 → 관리자 방 (잡 성공/실패/소요시간) |
| 토 00:30~11:00 | market_data 정리, 상장사 동기화, 주간 랭킹·수급 |
| 일 10:00~10:30 | 산업 시총 리포트, 종목 기술적 진단 |

휴장일은 KIS 휴장일 API + 정적 공휴일 목록으로 자동 스킵.
대시보드의 수동 트리거는 `app_config`의 타임스탬프 플래그를 워치독(60초)이 감지해 실행.

## 실행·배포

```bash
# 서버(dart-server) — systemd 상주
sudo systemctl restart bati_bot        # 재시작
tail -f ~/logs/bati.log                # 로그

# 수동 실행 예
python3 collect_market.py --all-listed
python3 collect_market.py --backfill 90
python3 market_summary_generator.py 2026-07-04
DRY_RUN=1 python3 run_all.py           # 텔레그램 발송 없는 리허설
```

**배포 절차** (서버·로컬·GitHub 3자 동기화 주의):
1. 배포 전 서버-로컬 diff 확인 (`md5sum`, 줄바꿈은 `tr -d '\r'` 정규화)
2. 로컬 수정 → `py_compile`/pyflakes → commit + push
3. 서버 `~/staging_split`에 전체 복사 후 `python3 -c "import run_all"` 실환경 import 테스트
4. 서버 원본 `*.bak.YYYYMMDD` 백업 → scp 배포 → `py_compile` → `systemctl restart bati_bot`
5. `~/logs/bati.log`에서 봇 3종+스케줄러 기동·에러 확인

## 환경 변수 (서버 `~/.env`)

```
TELEGRAM_BOT_TOKEN=          # 텔레그램 봇
DART_API_KEY=                # DART OpenAPI
KIS_APP_KEY= / KIS_APP_SECRET=   # 한국투자증권
GOOGLE_API_KEY=              # Gemini
NAVER_ID_1..10= / NAVER_SECRET_1..10=   # 네이버 검색 (미설정 슬롯 자동 제외)
SB_URL= / SB_SERVICE_KEY=    # Supabase (service_role)
DRY_RUN=                     # 설정 시 텔레그램 발송 생략 (리허설)
```

키는 코드에 하드코딩하지 않으며, 로그에는 `logger_config`의 마스킹 필터가 적용됩니다.

## 주요 Supabase 테이블

`companies`(상장사+모니터링 레벨) · `market_data`(일별 시세·수급·상태, 모니터링 90일/전체 28일 보존) ·
`financials`(분기 재무) · `macro_data` · `sector_daily_summary` · `leading_stocks` ·
`market_investment_summary` · `daily_disclosures` · `insider_trades` · `short_selling_history` ·
`analyst_opinions` · `consensus_estimates` · `us_market` · `earnings_grade_history` ·
`rooms`(채팅방 매핑) · `app_config`(설정·플래그·heartbeat) · `notice_history`(발송 이력) ·
`pro_members` · `bot_requests`(프론트 쓰기 위임 큐)

## 운영 메모

- **heartbeat**: 봇별 생존 신호를 `app_config`에 60초마다 기록 — 대시보드에서 상태 확인
- **잡 실패 알림**: `_job` 데코레이터가 실패 시 관리자 방으로 즉시 알림(잡·일 1회) + 19:50 일일 요약
- **종목 추가/삭제**: 대시보드에서 변경 → reload_flag → 봇이 재시작 없이 in-place 반영, 신규 종목은 90일 시세·기업정보·재무 자동 백필
- **텔레그램 발송**: 4096자 자동 분할, 429는 retry_after 존중, 실패 시 공시/뉴스는 다음 폴링에서 재시도
- `dart_parser.py`(2,577줄)는 공시 상세 파싱 핵심 모듈 — 분할 보류 상태이므로 수정 시 특히 주의
