#!/usr/bin/env bash
# resmon.sh — 봇(bati_bot) 프로세스·시스템 자원을 일정 간격으로 파일에 남긴다 (읽기 전용, 봇 재시작 불필요)
#
# 왜 (2026-11-17): 11-16 19:00:54~19:05:21 봇 로그가 4분 27초 끊겼다(모든 스레드 정지). 메모리(스왑·페이지
#   캐시 재읽기)인지, CPU(e2-micro 공유 코어 제한 = steal)인지, 한 스레드가 GIL을 쥐고 놓지 않은 것인지 가리려고
#   저녁 잡 시간대의 자원 상태를 15초 간격으로 남긴다.
#   · 이 스크립트도 따로 도는 프로세스라, 표본 간격(dt)이 늘어나면 '시스템 전체가 멈춘 것'(메모리·디스크·CPU),
#     dt는 그대로인데 봇 로그만 끊기고 봇 스레드 하나만 CPU를 쓰면 '봇 안의 GIL 문제'다.
#
# 사용: nohup bash scripts/resmon.sh '2026-11-19 21:10' 17:50 21:10 15 60 >/dev/null 2>&1 &
#   인자 = 끝 시각(이 시각이 지나면 스스로 끝남) · 집중 창 시작 · 집중 창 끝 · 창 안 간격(초) · 창 밖 간격(초, 0이면 창 밖은 기록 안 함)
#   멈추기: kill $(cat logs/resmon/resmon.pid)
#
# 출력 (logs/resmon/, logs/는 .gitignore):
#   YYYYMMDD.csv          표본마다 한 줄 — 시스템 메모리·스왑·PSI·CPU(steal)·디스크·봇 프로세스(RSS·스왑·폴트·디스크 읽기)·bati.log 크기
#   YYYYMMDD_threads.csv  표본마다 봇 스레드별 상태(R 실행·S 대기·D 디스크 대기)·CPU 틱·폴트·스레드 시작 틱(잡 스레드는 시작 시각으로 잡과 맞춘다)
#   YYYYMMDD_procs.csv    표본마다 RSS 10MB 이상인 모든 프로세스(cron으로 뜨는 check_alive 등 포함)
#   YYYYMMDD_wchan.csv    표본마다 봇 스레드 전부의 tid:상태:커널 대기 위치(wchan) — 멈춤이 디스크 대기인지 GIL 독점인지 가른다
#   CPU·폴트·PSI·디스크 값은 누적값 그대로 적는다 — 구간 차이는 분석 때 계산한다.
# 표본 하나에 bash 내장 명령만 쓰고 외부 명령은 stat·sleep뿐이라 부담이 거의 없다(RSS 약 4MB).

set -u
END_AT="${1:-}"
WIN_START="${2:-17:50}"
WIN_END="${3:-21:10}"
STEP_IN="${4:-15}"
STEP_OUT="${5:-60}"

BASE=/home/kjhofone
OUT="${RESMON_OUT:-$BASE/logs/resmon}"
BOTLOG="$BASE/logs/bati.log"

if [ -z "$END_AT" ]; then
    echo "끝 시각을 주세요 (예: '2026-11-19 21:10')" >&2
    exit 1
fi
END_EPOCH=$(date -d "$END_AT" +%s) || exit 1
mkdir -p "$OUT"
echo $$ > "$OUT/resmon.pid"

# 루트 디스크 이름 (예: /dev/sda1 → sda). 스왑 파일(/swapfile)도 같은 디스크에 있다.
ROOTDEV=$(findmnt -n -o SOURCE / 2>/dev/null)
ROOTDEV=${ROOTDEV#/dev/}
case "$ROOTDEV" in
    nvme*) DISK=${ROOTDEV%p[0-9]*} ;;
    *) DISK=${ROOTDEV%%[0-9]*} ;;
esac

WS=$((10#${WIN_START%%:*} * 60 + 10#${WIN_START##*:}))
WE=$((10#${WIN_END%%:*} * 60 + 10#${WIN_END##*:}))

BOTPID=""
find_bot() {
    # MainPID가 바뀌면(재시작) 다시 찾는다. ⚠️ pgrep -f run_all 은 자기 셸과도 맞으니 쓰지 않는다.
    if [ -n "$BOTPID" ] && [ -r "/proc/$BOTPID/stat" ]; then return; fi
    BOTPID=$(systemctl show bati_bot -p MainPID --value 2>/dev/null)
    [ "$BOTPID" = "0" ] && BOTPID=""
}

PSI_SOME=""; PSI_FULL=""
psi_read() {  # $1 = /proc/pressure/... → PSI_SOME, PSI_FULL (누적 마이크로초)
    local kind rest
    PSI_SOME=""; PSI_FULL=""
    while read -r kind rest; do
        case "$kind" in
            some) PSI_SOME=${rest##*total=} ;;
            full) PSI_FULL=${rest##*total=} ;;
        esac
    done < "$1"
}

MAIN_HDR="ts,hms,dt,in_win,pid,threads,rss_kb,swap_kb,utime,stime,minflt,majflt,read_bytes,\
mem_total,mem_free,mem_avail,cached,swap_cached,anon,swap_free,\
pswpin,pswpout,pgmajfault,refault_file,refault_anon,pgscan_kswapd,pgscan_direct,allocstall,\
cpu_user,cpu_nice,cpu_sys,cpu_idle,cpu_iowait,cpu_irq,cpu_softirq,cpu_steal,\
psi_mem_some,psi_mem_full,psi_cpu_some,psi_io_some,psi_io_full,\
dk_reads,dk_rsect,dk_rms,dk_writes,dk_wsect,dk_wms,dk_inflight,dk_busy_ms,dk_wait_ms,\
load1,runnable,botlog_size,botlog_mtime"

prev_ts=0
day_cur=""
while :; do
    printf -v now '%(%s)T' -1
    [ "$now" -ge "$END_EPOCH" ] && break
    printf -v hm '%(%H:%M)T' "$now"
    nowmin=$((10#${hm%%:*} * 60 + 10#${hm##*:}))
    if [ "$nowmin" -ge "$WS" ] && [ "$nowmin" -lt "$WE" ]; then in_win=1; step=$STEP_IN; else in_win=0; step=$STEP_OUT; fi
    if [ "$in_win" = 0 ] && [ "$STEP_OUT" -le 0 ]; then prev_ts=0; sleep 30; continue; fi

    printf -v day '%(%Y%m%d)T' "$now"
    printf -v hms '%(%H:%M:%S)T' "$now"
    F_MAIN="$OUT/$day.csv"; F_THR="$OUT/${day}_threads.csv"; F_PRC="$OUT/${day}_procs.csv"; F_WCH="$OUT/${day}_wchan.csv"
    if [ "$day" != "$day_cur" ]; then
        [ -s "$F_MAIN" ] || echo "$MAIN_HDR" > "$F_MAIN"
        [ -s "$F_THR" ] || echo "ts,pid,tid,state,utime,stime,minflt,majflt,starttime" > "$F_THR"
        [ -s "$F_WCH" ] || echo "ts,pid,tid:state:wchan ..." > "$F_WCH"
        [ -s "$F_PRC" ] || echo "ts,pid,comm,rss_kb" > "$F_PRC"
        day_cur=$day
    fi
    if [ "$prev_ts" -gt 0 ]; then dt=$((now - prev_ts)); else dt=0; fi
    prev_ts=$now

    # ── 봇 프로세스 ──
    find_bot
    p_thr=""; p_rss=""; p_swap=""; p_ut=""; p_st=""; p_min=""; p_maj=""; p_rb=""
    if [ -n "$BOTPID" ]; then
        while read -r k v _; do
            case "$k" in
                VmRSS:) p_rss=$v ;;
                VmSwap:) p_swap=$v ;;
                Threads:) p_thr=$v ;;
            esac
        done < "/proc/$BOTPID/status"
        if read -r -a S < "/proc/$BOTPID/stat"; then
            p_min=${S[9]}; p_maj=${S[11]}; p_ut=${S[13]}; p_st=${S[14]}
        fi
        while read -r k v; do
            [ "$k" = "read_bytes:" ] && { p_rb=$v; break; }
        done < "/proc/$BOTPID/io"
        # 스레드별 (상태 R/S/D, 누적 CPU 틱, 폴트, 시작 틱) + 커널 대기 위치(wchan) 한 줄
        #   메모리 부족 멈춤 = GIL 쥔 스레드가 D(페이지 폴트 디스크 대기), 나머지는 futex(GIL 대기)
        #   GIL 독점 = 한 스레드 R(실행) + CPU 100%, 나머지 futex
        wl=""
        {
            for t in /proc/$BOTPID/task/*; do
                read -r -a T < "$t/stat" || continue
                echo "$now,$BOTPID,${T[0]},${T[2]},${T[13]},${T[14]},${T[9]},${T[11]},${T[21]}"
                w=""; read -r w < "$t/wchan" || true
                wl="$wl ${T[0]}:${T[2]}:${w:-0}"
            done
        } >> "$F_THR"
        echo "$now,$BOTPID,${wl# }" >> "$F_WCH"
    fi

    # ── 메모리 ──
    m_total=""; m_free=""; m_avail=""; m_cached=""; m_scached=""; m_anon=""; m_sfree=""
    while read -r k v _; do
        case "$k" in
            MemTotal:) m_total=$v ;; MemFree:) m_free=$v ;; MemAvailable:) m_avail=$v ;;
            Cached:) m_cached=$v ;; SwapCached:) m_scached=$v ;; AnonPages:) m_anon=$v ;;
            SwapFree:) m_sfree=$v ;;
        esac
    done < /proc/meminfo

    v_si=""; v_so=""; v_maj=""; v_rff=""; v_rfa=""; v_sk=""; v_sd=""; v_as=0
    while read -r k v; do
        case "$k" in
            pswpin) v_si=$v ;; pswpout) v_so=$v ;; pgmajfault) v_maj=$v ;;
            workingset_refault_file) v_rff=$v ;; workingset_refault_anon) v_rfa=$v ;;
            pgscan_kswapd) v_sk=$v ;; pgscan_direct) v_sd=$v ;;
            allocstall_*) v_as=$((v_as + v)) ;;
        esac
    done < /proc/vmstat

    # ── CPU (steal = 공유 코어 제한·호스트 경합) ──
    read -r _ c_us c_ni c_sy c_id c_io c_irq c_sirq c_st _ < /proc/stat

    # ── PSI ──
    psi_read /proc/pressure/memory; ps_ms=$PSI_SOME; ps_mf=$PSI_FULL
    psi_read /proc/pressure/cpu;    ps_cs=$PSI_SOME
    psi_read /proc/pressure/io;     ps_is=$PSI_SOME; ps_if=$PSI_FULL

    # ── 디스크 ──
    d=",,,,,,,,"
    while read -r -a D; do
        if [ "${D[2]}" = "$DISK" ]; then
            d="${D[3]},${D[5]},${D[6]},${D[7]},${D[9]},${D[10]},${D[11]},${D[12]},${D[13]}"
            break
        fi
    done < /proc/diskstats

    read -r l1 _ _ rq _ < /proc/loadavg
    runnable=${rq%%/*}

    # bati.log 크기·수정 시각 — 크기가 안 늘면 봇이 아무 줄도 못 쓰고 있는 것
    lg=$(stat -c '%s,%Y' "$BOTLOG" 2>/dev/null || echo ",")

    echo "$now,$hms,$dt,$in_win,$BOTPID,$p_thr,$p_rss,$p_swap,$p_ut,$p_st,$p_min,$p_maj,$p_rb,\
$m_total,$m_free,$m_avail,$m_cached,$m_scached,$m_anon,$m_sfree,\
$v_si,$v_so,$v_maj,$v_rff,$v_rfa,$v_sk,$v_sd,$v_as,\
$c_us,$c_ni,$c_sy,$c_id,$c_io,$c_irq,$c_sirq,$c_st,\
$ps_ms,$ps_mf,$ps_cs,$ps_is,$ps_if,\
$d,$l1,$runnable,$lg" >> "$F_MAIN"

    # ── RSS 10MB 이상 프로세스 (짧게 떴다 지는 cron 파이썬도 잡히게 매 표본) ──
    {
        for p in /proc/[0-9]*; do
            read -r _ res _ < "$p/statm" || continue
            [ "${res:-0}" -ge 2560 ] || continue   # 2560쪽 × 4KB = 10MB
            read -r comm < "$p/comm" || comm="?"
            echo "$now,${p#/proc/},$comm,$((res * 4))"
        done
    } >> "$F_PRC" 2>/dev/null

    sleep "$step"
done
rm -f "$OUT/resmon.pid"
