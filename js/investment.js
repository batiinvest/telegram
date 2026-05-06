// investment.js — 오늘의 시황 페이지

// ── 전체 지표 정의 ──
const INV_ALL_METRICS = [
  { col:'sp500',   name:'S&P500',   group:'미국',   color:'#2AABEE' },  // 하늘파랑
  { col:'nasdaq',  name:'나스닥',    group:'미국',   color:'#ff6b35' },  // 주황
  { col:'dow',     name:'다우',      group:'미국',   color:'#a259ff' },  // 보라
  { col:'kospi',   name:'코스피',    group:'한국',   color:'#2dce89' },  // 초록
  { col:'kosdaq',  name:'코스닥',    group:'한국',   color:'#ffd600' },  // 노랑
  { col:'kospi200',name:'코스피200', group:'한국',   color:'#00d4aa' },  // 청록
  { col:'usd_krw', name:'USD/KRW',  group:'환율',   color:'#f5365c' },  // 빨강
  { col:'jpy_krw', name:'JPY/KRW',  group:'환율',   color:'#fb6340' },  // 주황빨강
  { col:'eur_krw', name:'EUR/KRW',  group:'환율',   color:'#ffc107' },  // 황금
  { col:'wti',     name:'WTI',      group:'원자재', color:'#8b5cf6' },  // 연보라
  { col:'gold',    name:'금',        group:'원자재', color:'#f59e0b' },  // 금색
  { col:'vix',     name:'VIX',      group:'기타',   color:'#64748b' },  // 회청
  { col:'us10y',   name:'미 금리',  group:'기타',   color:'#94a3b8' },  // 연회
];

const INV = {
  selected: new Set(['sp500','nasdaq','kospi','kosdaq']),
  period:   7,
};

// ── 페이지 HTML ──
function pInvestment() {
  window._invTab = window._invTab || 'market';
  return `
  <!-- 탭 헤더 -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:8px">
    <div style="display:flex;gap:6px">
      <button class="chip ${window._invTab==='market'?'active':''}" onclick="setInvTab('market')">📊 시황</button>
      <button class="chip ${window._invTab==='disclosure'?'active':''}" onclick="setInvTab('disclosure')">📋 공시</button>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <div style="font-size:12px;color:var(--text3)" id="inv-date"></div>
      <button class="btn btn-sm" onclick="refreshInvestment()">🔄 새로고침</button>
    </div>
  </div>

  <!-- 시황 탭 -->
  <div id="inv-tab-market" style="display:${window._invTab==='market'?'block':'none'}">
    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">🌍 글로벌 지수</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:1.25rem" id="inv-global">
      ${['','','','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
    </div>

    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">🇰🇷 국내 시장</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:1.25rem" id="inv-domestic">
      ${['','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.25rem">
      <div>
        <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">💱 환율</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px" id="inv-fx">
          ${['','','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
        </div>
      </div>
      <div>
        <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">🛢️ 원자재</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px" id="inv-commodity">
          ${['','','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:1.25rem">
      <div class="card-header" style="flex-wrap:wrap;gap:8px;align-items:flex-start">
        <div>
          <span class="card-title">📈 흐름 비교</span>
          <div style="font-size:11px;color:var(--text3);margin-top:2px">시작일 = 100 기준 정규화 · 원하는 지표를 선택해 비교</div>
        </div>
        <div style="display:flex;gap:4px;margin-left:auto">
          ${[{d:7,l:'1주'},{d:30,l:'1달'},{d:90,l:'3달'}].map(({d,l})=>`
            <button class="chip ${d===7?'active':''}" data-inv-period="${d}"
              onclick="setInvPeriod(${d})" style="font-size:11px;padding:2px 8px">${l}</button>
          `).join('')}
        </div>
      </div>
      <div style="padding:.75rem 1rem;border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px" id="inv-metric-checks">
        ${INV_ALL_METRICS.map(m => `
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;padding:3px 8px;border-radius:100px;border:1px solid var(--border);font-size:12px;user-select:none"
            id="inv-lbl-${m.col}">
            <input type="checkbox" style="display:none" id="inv-chk-${m.col}"
              onchange="toggleInvMetric('${m.col}')" ${['sp500','nasdaq','kospi','kosdaq'].includes(m.col)?'checked':''}>
            <span style="width:8px;height:8px;border-radius:50%;background:${m.color};flex-shrink:0"></span>
            <span>${m.name}</span>
            <span style="font-size:10px;color:var(--text3)">${m.group}</span>
          </label>
        `).join('')}
      </div>
      <div style="padding:1rem;position:relative;height:280px">
        <canvas id="inv-trend-chart"></canvas>
        <div id="inv-trend-empty" style="display:none;position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:13px">
          데이터 수집 중... (매일 09:00, 16:10 업데이트)
        </div>
      </div>
    </div>

    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">📊 전체 종목 동향</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:1rem" id="inv-total-summary"></div>
    <div id="inv-industry-grid" style="margin-bottom:1.25rem"></div>

    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">⭐ 모니터링 종목 현황</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:1rem" id="inv-summary"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="card">
        <div class="card-header"><span class="card-title">🔴 급등 Top 5</span></div>
        <div id="inv-surge" style="padding:.5rem 0"></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">🔵 급락 Top 5</span></div>
        <div id="inv-drop" style="padding:.5rem 0"></div>
      </div>
    </div>
  </div>

  <!-- 공시 탭 -->
  <div id="inv-tab-disclosure" style="display:${window._invTab==='disclosure'?'block':'none'}">

    <!-- 오늘 실적 공시 -->
    <div class="card" style="margin-bottom:1.25rem">
      <div class="card-header">
        <span class="card-title">📋 오늘 실적 공시 종목</span>
        <span id="inv-disclosure-date" style="font-size:11px;color:var(--text3);margin-left:8px"></span>
        <button id="inv-disclosure-expand-btn" class="btn btn-sm" style="margin-left:auto;font-size:12px"
          onclick="toggleAllDisclosures()">+ 전체 공시</button>
      </div>
      <div id="inv-disclosure-list" style="padding:.5rem 0">
        <div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px"><span class="loading"></span></div>
      </div>
      <!-- 전체 공시 펼침 영역 -->
      <div id="inv-all-disclosure" style="display:none;border-top:1px solid var(--border)">
        <div id="inv-all-disclosure-list" style="padding:.5rem 0">
          <div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px"><span class="loading"></span></div>
        </div>
      </div>
    </div>

    <!-- 실적 급등 종목 -->
    <div class="card" style="margin-bottom:1.25rem">
      <div class="card-header" style="flex-wrap:wrap;gap:8px;align-items:center">
        <span class="card-title">🚀 실적 급등 종목</span>
        <div style="display:flex;align-items:center;gap:6px;margin-left:auto;flex-wrap:wrap">
          <div style="display:flex;gap:4px">
            <button class="chip active" data-surge-grade="all"   onclick="setSurgeGrade(this,'all')"  style="font-size:12px">전체</button>
            <button class="chip"        data-surge-grade="S"    onclick="setSurgeGrade(this,'S')"   style="font-size:12px">S급</button>
            <button class="chip"        data-surge-grade="A"    onclick="setSurgeGrade(this,'A')"   style="font-size:12px">A급</button>
            <button class="chip"        data-surge-grade="B"    onclick="setSurgeGrade(this,'B')"   style="font-size:12px">B급</button>
            <button class="chip"        data-surge-grade="관찰"    onclick="setSurgeGrade(this,'관찰')"   style="font-size:12px">관찰</button>
          </div>
          <select class="form-select" id="inv-earnings-quarter" style="width:130px;padding:3px 8px;font-size:12px"
            onchange="loadEarningsSurge()">
            <option value="">로딩 중...</option>
          </select>
        </div>
      </div>
      <div id="inv-earnings-list" style="padding:.5rem 0">
        <div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px"><span class="loading"></span></div>
      </div>
    </div>
  </div>`;
}




// ── 지수 카드 ──
function mkIndexCard(label, value, chg, unit, sub) {
  const cc  = chg != null ? chgColor(chg) : 'var(--text2)';
  const cs  = chg != null ? chgStr(chg) : '—';
  const val = value != null ? Number(value).toLocaleString() + (unit||'') : '—';
  return `
  <div class="card" style="padding:12px 14px">
    <div style="font-size:11px;color:var(--text2);margin-bottom:4px">${label}</div>
    <div style="font-size:16px;font-weight:700;color:var(--text1)">${val}</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:3px">
      <div style="font-size:12px;color:${cc};font-weight:500">${cs}</div>
      ${sub ? `<div style="font-size:10px;color:var(--text2)">${sub}</div>` : ''}
    </div>
  </div>`;
}

// ── 탭 전환 ──
function setInvTab(tab) {
  window._invTab = tab;
  document.querySelectorAll('.chip[onclick*="setInvTab"]').forEach(b =>
    b.classList.toggle('active', b.textContent.includes(tab === 'market' ? '시황' : '공시')));
  document.getElementById('inv-tab-market').style.display     = tab === 'market'     ? 'block' : 'none';
  document.getElementById('inv-tab-disclosure').style.display = tab === 'disclosure' ? 'block' : 'none';
  if (tab === 'disclosure') {
    _allDiscLoaded = false;  // 탭 재진입 시 전체공시 재로드 허용
    loadTodayDisclosures();
    loadEarningsSurge();
  }
}

// ── 새로고침 — 공시 탭이면 백엔드 수집 트리거 후 로드 ──
async function refreshInvestment() {
  if (window._invTab === 'disclosure') {
    // run_disclosure_flag 업데이트 → 봇이 1분 내 job_collect_financials 실행
    try {
      await sb.from('app_config').upsert({
        key: 'run_disclosure_flag',
        value: String(Date.now()),
        description: '대시보드 공시수집 수동 트리거'
      }, { onConflict: 'key' });
      toast('📡 공시 수집 요청 완료 — 봇이 1분 내 업데이트합니다', 'info');
    } catch(e) {
      toast('트리거 전송 실패: ' + e.message, 'error');
    }
    // DB 업데이트 대기 후 화면 갱신 (60초 후 자동 리로드)
    setTimeout(() => {
      if (window._invTab === 'disclosure') {
        _allDiscLoaded = false;
        loadTodayDisclosures();
        loadEarningsSurge();
        toast('✓ 공시 목록 새로고침 완료', 'success');
      }
    }, 62000);
  }
  // 항상 현재 화면은 즉시 갱신
  loadInvestment();
}

// ── 메인 로드 ──
async function loadInvestment() {
  // 시황 탭 로드 (market-overview.js)
  loadMacroData();
  loadTrendChart();

  // 우상단 날짜 — 오늘 날짜 즉시 표시 (market_data 조회 전에도 보임)
  const _today = new Date();
  const todayStr = `${_today.getFullYear()}-${String(_today.getMonth()+1).padStart(2,'0')}-${String(_today.getDate()).padStart(2,'0')}`;
  const dateEl = document.getElementById('inv-date');
  if (dateEl) dateEl.textContent = `기준: ${todayStr}`;

  // 공시 탭이 활성화된 경우에만 로드
  if (window._invTab === 'disclosure') {
    _allDiscLoaded = false;  // 새로고침 시 전체공시 재로드 허용
    loadTodayDisclosures();
    loadEarningsSurge();
  }

  const { data: dateRow } = await sb.from('market_data')
    .select('base_date').order('base_date', { ascending: false }).limit(1);
  const maxDate = dateRow?.[0]?.base_date;
  if (!maxDate) return;

  // 전체 종목 + 산업별 동향
  loadMarketOverview(maxDate);

  // 모니터링 종목
  const { data: mktRows } = await sb.from('market_data')
    .select('stock_code,corp_name,price,price_change_rate,market_cap,market')
    .eq('base_date', maxDate)
    .order('price_change_rate', { ascending: false });

  if (!mktRows?.length) return;
  const rows = mktRows.filter(r => r.price_change_rate != null);
  if (!rows.length) return;

  const rise   = rows.filter(r => r.price_change_rate > 0).length;
  const fall   = rows.filter(r => r.price_change_rate < 0).length;
  const avgChg = rows.reduce((s,r) => s + r.price_change_rate, 0) / rows.length;

  document.getElementById('inv-summary').innerHTML = `
    <div class="metric-card"><div class="metric-label">상승</div><div class="metric-value" style="color:var(--red)">${rise}개</div></div>
    <div class="metric-card"><div class="metric-label">하락</div><div class="metric-value" style="color:var(--blue)">${fall}개</div></div>
    <div class="metric-card"><div class="metric-label">평균 등락률</div><div class="metric-value" style="color:${chgColor(avgChg)}">${chgStr(avgChg)}</div></div>`;

  const rankRow = (r, i) => `
    <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid var(--border)">
      <span style="width:16px;font-size:11px;color:var(--text3);font-weight:600">${i+1}</span>
      <span style="flex:1;font-size:13px;font-weight:500">${r.corp_name}</span>
      <span style="font-size:11px;color:var(--text3)">${r.market||''}</span>
      <span style="font-size:13px;font-weight:600;color:${chgColor(r.price_change_rate)}">${chgStr(r.price_change_rate)}</span>
    </div>`;

  document.getElementById('inv-surge').innerHTML = rows.slice(0,5).map(rankRow).join('');
  document.getElementById('inv-drop').innerHTML  = [...rows].reverse().slice(0,5).map(rankRow).join('');
}


// ── 시황/공시/급등 로직은 분리된 파일에서 로드 ──
// market-overview.js : loadMacroData, loadTrendChart, loadMarketOverview
// disclosure.js      : loadTodayDisclosures, loadAllDisclosures, toggleAllDisclosures
// earnings-surge.js  : loadEarningsSurge, renderSurgeList, setSurgeGrade 등
