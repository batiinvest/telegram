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
      <button class="btn btn-sm" onclick="loadInvestment()">🔄 새로고침</button>
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
          <span style="font-size:12px;color:var(--text3)">기준 분기</span>
          <select class="form-select" id="inv-earnings-quarter" style="width:130px;padding:3px 8px;font-size:12px"
            onchange="loadEarningsSurge()">
            <option value="">로딩 중...</option>
          </select>
        </div>
      </div>
      <!-- 탭 + 설정 -->
      <div style="display:flex;align-items:center;gap:0;border-bottom:1px solid var(--border)">
        <button id="inv-surge-tab-rev" class="chip active" onclick="setEarningsSurgeTab('revenue')"
          style="font-size:12px;border-radius:0;padding:8px 16px;border:none;border-bottom:2px solid var(--accent)">
          📈 매출액
        </button>
        <button id="inv-surge-tab-op" class="chip" onclick="setEarningsSurgeTab('operating_profit')"
          style="font-size:12px;border-radius:0;padding:8px 16px;border:none;border-bottom:2px solid transparent">
          💰 영업이익
        </button>
        <div style="margin-left:auto;display:flex;align-items:center;gap:6px;padding:4px 12px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--text3)">QoQ</span>
          <input type="number" id="inv-surge-qoq" value="${localStorage.getItem('earnings_surge_qoq')||20}" min="0" max="500" step="5"
            style="width:48px;padding:2px 6px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;text-align:center">
          <span style="font-size:11px;color:var(--text3)">% YoY</span>
          <input type="number" id="inv-surge-yoy" value="${localStorage.getItem('earnings_surge_yoy')||20}" min="0" max="500" step="5"
            style="width:48px;padding:2px 6px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;text-align:center">
          <span style="font-size:11px;color:var(--text3)">% 이상</span>
          <button class="chip" onclick="loadEarningsSurge()" style="font-size:11px;padding:2px 8px">적용</button>
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
    loadTodayDisclosures();
    loadEarningsSurge();
  }
}

// ── 메인 로드 ──
async function loadInvestment() {
  loadMacroData();
  loadTrendChart();

  // 공시 탭이 활성화된 경우에만 로드
  if (window._invTab === 'disclosure') {
    loadTodayDisclosures();
    loadEarningsSurge();
  }

  const { data: dateRow } = await sb.from('market_data')
    .select('base_date').order('base_date', { ascending: false }).limit(1);
  const maxDate = dateRow?.[0]?.base_date;
  const dateEl = document.getElementById('inv-date');
  if (dateEl) dateEl.textContent = maxDate ? `기준: ${maxDate}` : '';
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

// ── 전체 종목 + 산업별 동향 ──
async function loadMacroData() {
  const { data } = await sb.from('macro_data')
    .select('*').order('base_date', { ascending: false }).limit(1);
  const m = data?.[0] || {};

  const globalEl = document.getElementById('inv-global');
  if (globalEl) globalEl.innerHTML = [
    mkIndexCard('S&P 500',     m.sp500,    m.sp500_chg,    '',  'USA'),
    mkIndexCard('나스닥',       m.nasdaq,   m.nasdaq_chg,   '',  'USA'),
    mkIndexCard('다우존스',     m.dow,      m.dow_chg,      '',  'USA'),
    mkIndexCard('VIX',         m.vix,      m.vix_chg,      '',  '공포지수'),
    mkIndexCard('미 10년 금리', m.us10y,    m.us10y_chg,    '%', '국채'),
  ].join('');

  const domEl = document.getElementById('inv-domestic');
  if (domEl) domEl.innerHTML = [
    mkIndexCard('코스피',    m.kospi,    m.kospi_chg,    '',  'KOSPI'),
    mkIndexCard('코스닥',    m.kosdaq,   m.kosdaq_chg,   '',  'KOSDAQ'),
    mkIndexCard('코스피200', m.kospi200, m.kospi200_chg, '',  '선물'),
  ].join('');

  const fxEl = document.getElementById('inv-fx');
  if (fxEl) fxEl.innerHTML = [
    mkIndexCard('USD/KRW', m.usd_krw, m.usd_krw_chg, '원', '달러'),
    mkIndexCard('JPY/KRW', m.jpy_krw, m.jpy_krw_chg, '원', '100엔'),
    mkIndexCard('EUR/KRW', m.eur_krw, m.eur_krw_chg, '원', '유로'),
    mkIndexCard('CNY/KRW', m.cny_krw, m.cny_krw_chg, '원', '위안'),
  ].join('');

  const commEl = document.getElementById('inv-commodity');
  if (commEl) commEl.innerHTML = [
    mkIndexCard('WTI 유가', m.wti,    m.wti_chg,    '$', '배럴'),
    mkIndexCard('금',       m.gold,   m.gold_chg,   '$', '온스'),
    mkIndexCard('천연가스',  m.gas,    m.gas_chg,    '$', 'MMBtu'),
    mkIndexCard('구리',     m.copper, m.copper_chg, '$', '파운드'),
  ].join('');
}

// ── 흐름 비교 차트 ──
let _invTrendChart = null;

function toggleInvMetric(col) {
  if (INV.selected.has(col)) {
    INV.selected.delete(col);
  } else {
    if (INV.selected.size >= 8) { return; }
    INV.selected.add(col);
  }
  const lbl = document.getElementById('inv-lbl-' + col);
  const m   = INV_ALL_METRICS.find(x => x.col === col);
  if (lbl && m) {
    lbl.style.background  = INV.selected.has(col) ? m.color + '22' : '';
    lbl.style.borderColor = INV.selected.has(col) ? m.color : 'var(--border)';
    lbl.style.color       = INV.selected.has(col) ? m.color : '';
  }
  loadTrendChart();
}

function initInvCheckboxStyles() {
  INV_ALL_METRICS.forEach(m => {
    const lbl = document.getElementById('inv-lbl-' + m.col);
    if (!lbl) return;
    if (INV.selected.has(m.col)) {
      lbl.style.background  = m.color + '22';
      lbl.style.borderColor = m.color;
      lbl.style.color       = m.color;
    }
  });
}

function setInvPeriod(period) {
  INV.period = period;
  document.querySelectorAll('[data-inv-period]').forEach(b =>
    b.classList.toggle('active', b.dataset.invPeriod === String(period)));
  loadTrendChart();
}

async function loadTrendChart() {
  const canvas = document.getElementById('inv-trend-chart');
  const empty  = document.getElementById('inv-trend-empty');
  if (!canvas) return;

  initInvCheckboxStyles();

  if (!INV.selected.size) {
    canvas.style.display = 'none';
    if (empty) { empty.style.display = 'flex'; empty.textContent = '지표를 선택해주세요.'; }
    return;
  }

  const selectedMetrics = INV_ALL_METRICS.filter(m => INV.selected.has(m.col));
  const cols = ['base_date', ...selectedMetrics.map(m => m.col)].join(',');

  const { data: rows } = await sb.from('macro_data')
    .select(cols)
    .order('base_date', { ascending: true })
    .limit(INV.period);

  if (!rows?.length) {
    canvas.style.display = 'none';
    if (empty) { empty.style.display = 'flex'; empty.textContent = '데이터 수집 중... (매일 09:00, 16:10 업데이트)'; }
    return;
  }
  canvas.style.display = 'block';
  if (empty) empty.style.display = 'none';

  const labels = rows.map(r => r.base_date);

  const datasets = selectedMetrics.map(m => {
    const values = rows.map(r => r[m.col]);
    const base   = values.find(v => v != null);
    const normalized = values.map(v => v != null && base ? Math.round(v / base * 10000) / 100 : null);
    return {
      label:           m.name,
      data:            normalized,
      borderColor:     m.color,
      backgroundColor: m.color + '15',
      borderWidth:     2,
      pointRadius:     2,
      pointHoverRadius:5,
      tension:         0.3,
      fill:            false,
      spanGaps:        true,
    };
  });

  if (_invTrendChart) { _invTrendChart.destroy(); _invTrendChart = null; }

  _invTrendChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(2)}`
          }
        }
      },
      scales: {
        x: {
          ticks: { color: 'var(--text3)', maxTicksLimit: 7, maxRotation: 0 },
          grid:  { color: 'var(--border)' },
        },
        y: {
          ticks: { color: 'var(--text3)', callback: v => v + '' },
          grid:  { color: 'var(--border)' },
        }
      }
    }
  });
}

async function loadMarketOverview(maxDate) {
  // 전체 종목 조회 — 페이지네이션 (Supabase 기본 limit 1000 우회)
  let all = [], from = 0;
  while (true) {
    const { data, error } = await sb.from('market_data')
      .select('stock_code,corp_name,price_change_rate,market_cap,market')
      .eq('base_date', maxDate)
      .order('price_change_rate', { ascending: false })
      .range(from, from + 999);
    if (error || !data?.length) break;
    all = all.concat(data);
    if (data.length < 1000) break;
    from += 1000;
  }

  const rows = (all||[]).filter(r => r.price_change_rate != null);
  if (!rows.length) return;

  // companies 전체에서 industry 매핑 (URL 길이 제한 우회)
  let industryMap = {};
  try {
    let compAll = [], from = 0;
    while (true) {
      const { data: comp } = await sb.from('companies')
        .select('code,industry')
        .range(from, from + 999);
      if (!comp?.length) break;
      comp.forEach(c => {
        const code = c.code.replace(/\.(KS|KQ)$/, '');
        if (c.industry) industryMap[code] = c.industry;
      });
      if (comp.length < 1000) break;
      from += 1000;
    }
  } catch(e) { /* industry 없이 진행 */ }

  const enriched = rows.map(r => ({
    ...r,
    industry: industryMap[r.stock_code] || r.market || '기타'
  }));

  // 전체 요약
  const rise  = enriched.filter(r => r.price_change_rate > 0).length;
  const fall  = enriched.filter(r => r.price_change_rate < 0).length;
  const flat  = enriched.length - rise - fall;
  const avg   = enriched.reduce((s,r) => s + r.price_change_rate, 0) / enriched.length;
  const top   = [...enriched].sort((a,b) => b.price_change_rate - a.price_change_rate)[0];
  const bot   = [...enriched].sort((a,b) => a.price_change_rate - b.price_change_rate)[0];

  const totalEl = document.getElementById('inv-total-summary');
  if (totalEl) totalEl.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">전체 종목</div>
      <div class="metric-value">${enriched.length}개</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">상승</div>
      <div class="metric-value" style="color:var(--red)">${rise}개</div>
      <div class="metric-sub">${(rise/enriched.length*100).toFixed(0)}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">하락</div>
      <div class="metric-value" style="color:var(--blue)">${fall}개</div>
      <div class="metric-sub">${(fall/enriched.length*100).toFixed(0)}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">보합</div>
      <div class="metric-value" style="color:var(--text3)">${flat}개</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">평균 등락률</div>
      <div class="metric-value" style="color:${chgColor(avg)}">${chgStr(avg)}</div>
    </div>`;

  // 산업별 집계
  const indMap = {};
  enriched.forEach(r => {
    const ind = r.industry || '기타';
    if (!indMap[ind]) indMap[ind] = { rise:0, fall:0, flat:0, total:0, sumChg:0, stocks:[] };
    indMap[ind].total++;
    indMap[ind].sumChg += r.price_change_rate;
    indMap[ind].stocks.push(r);
    if (r.price_change_rate > 0)      indMap[ind].rise++;
    else if (r.price_change_rate < 0) indMap[ind].fall++;
    else                               indMap[ind].flat++;
  });

  const indGrid = document.getElementById('inv-industry-grid');
  if (!indGrid) return;

  const indRows = Object.entries(indMap)
    .map(([ind, d]) => ({ ind, ...d, avg: d.sumChg / d.total }))
    .sort((a,b) => b.avg - a.avg);

  indGrid.innerHTML = indRows.map(d => {
    const ratio = d.rise / d.total;
    const barW  = Math.round(ratio * 100);
    const top3  = [...d.stocks].sort((a,b) => b.price_change_rate - a.price_change_rate).slice(0,3);
    return `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border)">
      <div style="width:80px;font-size:13px;font-weight:600;flex-shrink:0">${d.ind}</div>
      <div style="flex:1">
        <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--bg3);margin-bottom:4px">
          <div style="width:${barW}%;background:var(--red);transition:width .3s"></div>
          <div style="width:${100-barW}%;background:var(--blue)"></div>
        </div>
        <div style="display:flex;gap:6px;font-size:11px">
          <span style="color:var(--red)">▲${d.rise}</span>
          <span style="color:var(--blue)">▼${d.fall}</span>
          <span style="color:var(--text3)">${d.flat ? '━'+d.flat : ''}</span>
          <span style="margin-left:auto;color:var(--text3)">총 ${d.total}개</span>
        </div>
      </div>
      <div style="font-size:13px;font-weight:600;color:${chgColor(d.avg)};min-width:52px;text-align:right">${chgStr(d.avg)}</div>
      <div style="font-size:11px;color:var(--text3);min-width:130px;text-align:right">
        ${top3.map(s=>`<span style="margin-left:6px;color:${chgColor(s.price_change_rate)}">${s.corp_name} ${chgStr(s.price_change_rate)}</span>`).join('')}
      </div>
    </div>`;
  }).join('');
}

// ── 전체 공시 토글 ──
let _allDiscLoaded = false;

function toggleAllDisclosures() {
  const panel = document.getElementById('inv-all-disclosure');
  const btn   = document.getElementById('inv-disclosure-expand-btn');
  if (!panel) return;

  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  btn.textContent = isOpen ? '+ 전체 공시' : '− 전체 공시';

  if (!isOpen && !_allDiscLoaded) {
    _allDiscLoaded = true;
    loadAllDisclosures();
  }
}

async function loadAllDisclosures() {
  const el = document.getElementById('inv-all-disclosure-list');
  if (!el) return;

  const { data: cfg } = await sb.from('app_config')
    .select('value').eq('key', 'today_all_disclosures').single();

  if (!cfg?.value) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">전체 공시 데이터 없음 (매일 18:30 업데이트)</div>`;
    return;
  }

  let all = [];
  try { all = JSON.parse(cfg.value); } catch { }

  if (!all.length) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">오늘 공시 없음</div>`;
    return;
  }

  // 카테고리 분류
  const CATEGORIES = [
    { label: '사업보고서',  color: '#2AABEE', bg: 'rgba(42,171,238,.12)', match: '사업보고서' },
    { label: '반기보고서',  color: '#2dce89', bg: 'rgba(45,206,137,.12)', match: '반기보고서' },
    { label: '분기보고서',  color: '#fb6340', bg: 'rgba(251,99,64,.12)',  match: '분기보고서' },
    { label: '주요사항보고', color: '#ffd600', bg: 'rgba(255,214,0,.12)', match: '주요사항보고' },
    { label: '임원/주식',   color: '#a259ff', bg: 'rgba(162,89,255,.12)', match: ['임원', '주요주주'] },
    { label: '공정공시',    color: '#00d4aa', bg: 'rgba(0,212,170,.12)', match: '공정공시' },
    { label: '기타',        color: '#8b90a7', bg: 'rgba(139,144,167,.12)', match: null },
  ];

  const categorized = {};
  CATEGORIES.forEach(c => categorized[c.label] = []);

  all.forEach(d => {
    const nm = d.report_nm || '';
    let matched = false;
    for (const cat of CATEGORIES.slice(0, -1)) {
      const matches = Array.isArray(cat.match)
        ? cat.match.some(m => nm.includes(m))
        : nm.includes(cat.match);
      if (matches) {
        categorized[cat.label].push(d);
        matched = true;
        break;
      }
    }
    if (!matched) categorized['기타'].push(d);
  });

  const catHTML = CATEGORIES.map(cat => {
    const items = categorized[cat.label];
    if (!items.length) return '';
    return `
      <div style="padding:.75rem 1rem">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:12px;font-weight:600;padding:2px 8px;border-radius:100px;background:${cat.bg};color:${cat.color}">${cat.label}</span>
          <span style="font-size:11px;color:var(--text3)">${items.length}건</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px">
          ${items.map(d => `
            <div style="padding:5px 10px;background:var(--bg3);border-radius:var(--radius-sm);border:1px solid var(--border);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${d.report_nm}">
              ${d.corp_name}
            </div>`).join('')}
        </div>
      </div>`;
  }).join('');

  el.innerHTML = `
    ${catHTML}
    <div style="padding:4px 1rem 10px;font-size:11px;color:var(--text3)">총 ${all.length}건</div>`;
}

// ── 오늘 실적 공시 목록 ──
async function loadTodayDisclosures() {
  const el     = document.getElementById('inv-disclosure-list');
  const dateEl = document.getElementById('inv-disclosure-date');
  if (!el) return;

  const { data: cfg } = await sb.from('app_config')
    .select('value,description')
    .eq('key', 'today_earnings_corps')
    .single();

  if (!cfg?.value) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">
      오늘 공시 데이터 없음 (매일 18:30 업데이트)
    </div>`;
    return;
  }

  let corps = [];
  try { corps = JSON.parse(cfg.value); } catch { }

  if (!corps.length) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">오늘 실적 공시 없음</div>`;
    return;
  }

  // 날짜 표시 (description에서 추출)
  if (dateEl && cfg.description) {
    dateEl.textContent = cfg.description.replace(' 실적 공시 종목 목록', '') + ' 기준';
  }

  // 보고서 종류별 배지 색상
  const reprtColor = (nm) => {
    if (nm.includes('사업보고서')) return { bg:'rgba(42,171,238,.15)', color:'#2AABEE', label:'연간' };
    if (nm.includes('반기'))      return { bg:'rgba(45,206,137,.15)', color:'#2dce89', label:'반기' };
    if (nm.includes('분기'))      return { bg:'rgba(251,99,64,.15)',  color:'#fb6340', label:'분기' };
    return                               { bg:'rgba(139,144,167,.15)', color:'#8b90a7', label:'공시' };
  };

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;padding:.75rem 1rem">
      ${corps.map(c => {
        const badge = reprtColor(c.report_nm || '');
        return `<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--bg3);border-radius:var(--radius-sm);border:1px solid var(--border)">
          <span style="font-size:10px;padding:2px 6px;border-radius:100px;background:${badge.bg};color:${badge.color};font-weight:600;white-space:nowrap">${badge.label}</span>
          <span style="font-size:13px;font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.corp_name}</span>
        </div>`;
      }).join('')}
    </div>
    <div style="padding:4px 1rem 8px;font-size:11px;color:var(--text3)">
      총 ${corps.length}개 종목 공시 · 재무 데이터 수집 완료
    </div>`;
}

// ── 실적 급등 종목 ──
let _earningsMetric = 'revenue';

function setEarningsMetric(el, metric) {
  _earningsMetric = metric;
  document.querySelectorAll('[data-earnings]').forEach(b =>
    b.classList.toggle('active', b.dataset.earnings === metric));
  loadEarningsSurge();
}

let _earningsSurgeTab = 'revenue';

function setEarningsSurgeTab(tab) {
  _earningsSurgeTab = tab;
  const revBtn = document.getElementById('inv-surge-tab-rev');
  const opBtn  = document.getElementById('inv-surge-tab-op');
  if (revBtn) {
    revBtn.classList.toggle('active', tab === 'revenue');
    revBtn.style.borderBottom = tab === 'revenue' ? '2px solid var(--accent)' : '2px solid transparent';
  }
  if (opBtn) {
    opBtn.classList.toggle('active', tab === 'operating_profit');
    opBtn.style.borderBottom = tab === 'operating_profit' ? '2px solid var(--accent)' : '2px solid transparent';
  }
  loadEarningsSurge();
}

async function loadEarningsSurge() {
  const el = document.getElementById('inv-earnings-list');
  if (!el) return;
  el.innerHTML = `<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px"><span class="loading"></span></div>`;

  const qoqThreshold = parseFloat(document.getElementById('inv-surge-qoq')?.value || 20);
  const yoyThreshold = parseFloat(document.getElementById('inv-surge-yoy')?.value || 20);
  const metric = _earningsSurgeTab;

  try {
    localStorage.setItem('earnings_surge_qoq', qoqThreshold);
    localStorage.setItem('earnings_surge_yoy', yoyThreshold);
  } catch(e) {}

  // 분기 목록 조회 (최초 1회)
  const qSelect = document.getElementById('inv-earnings-quarter');
  if (qSelect && (qSelect.options.length === 0 || qSelect.options[0].value === '')) {
    const { data: quarters } = await sb.from('financials')
      .select('bsns_year,quarter').eq('fs_div', 'CFS')
      .order('bsns_year', { ascending: false })
      .order('quarter', { ascending: false })
      .limit(1000);
    const seen = new Set();
    const qList = (quarters||[]).filter(r => {
      const key = `${r.bsns_year}-${r.quarter}`;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 12);
    if (qList.length) {
      qSelect.innerHTML = qList.map((q,i) =>
        `<option value="${q.bsns_year}-${q.quarter}">${q.bsns_year} ${q.quarter}${i===0?' (최신)':''}</option>`
      ).join('');
    }
  }

  const selVal = qSelect?.value || '';
  let filterYear = null, filterQuarter = null;
  if (selVal) [filterYear, filterQuarter] = selVal.split('-');

  const qoqCol = metric === 'revenue' ? 'revenue_qoq' : 'op_profit_qoq';
  const yoyCol = metric === 'revenue' ? 'revenue_yoy' : 'op_profit_yoy';

  let query = sb.from('financials')
    .select('corp_name,stock_code,bsns_year,quarter,revenue,operating_profit,revenue_yoy,revenue_qoq,op_profit_yoy,op_profit_qoq')
    .eq('fs_div', 'CFS');
  if (filterYear)    query = query.eq('bsns_year', filterYear);
  if (filterQuarter) query = query.eq('quarter', filterQuarter);

  const { data: rows } = await query
    .order('bsns_year', { ascending: false })
    .order('quarter', { ascending: false })
    .limit(3000);

  if (!rows?.length) {
    el.innerHTML = `<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px">데이터 없음</div>`;
    return;
  }

  let targets = rows;
  if (!filterYear) {
    const latestMap = {};
    rows.forEach(r => { if (!latestMap[r.stock_code]) latestMap[r.stock_code] = r; });
    targets = Object.values(latestMap);
  }

  const surges = targets
    .filter(r => r[metric] != null && (
      (r[qoqCol] != null && r[qoqCol] >= qoqThreshold) ||
      (r[yoyCol] != null && r[yoyCol] >= yoyThreshold)
    ))
    .sort((a,b) => Math.max(b[qoqCol]??-999,b[yoyCol]??-999) - Math.max(a[qoqCol]??-999,a[yoyCol]??-999))
    .slice(0, 20);

  if (!surges.length) {
    el.innerHTML = `<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:12px">기준 충족 종목 없음</div>`;
    return;
  }

  // 급등 종목의 최근 4분기 + 연간 데이터 조회
  const codes = [...new Set(surges.map(r => r.stock_code))];
  const { data: histRows } = await sb.from('financials')
    .select('corp_name,stock_code,bsns_year,quarter,revenue,operating_profit')
    .eq('fs_div', 'CFS')
    .in('stock_code', codes)
    .order('bsns_year', { ascending: false })
    .order('quarter', { ascending: false })
    .limit(codes.length * 20);

  // 종목별 히스토리 맵
  const histMap = {};
  (histRows||[]).forEach(r => {
    if (!histMap[r.stock_code]) histMap[r.stock_code] = [];
    histMap[r.stock_code].push(r);
  });

  const chgBadge = (v, label, prevVal, curVal) => {
    if (v == null) return '';
    const color = v > 0 ? 'var(--red)' : 'var(--blue)';
    const fromTo = (prevVal != null && curVal != null)
      ? ` <span style="color:var(--text3);font-size:10px">(${fmtCap(prevVal)}→${fmtCap(curVal)})</span>`
      : '';
    return `<span style="font-size:11px;color:${color}">${v>0?'▲':'▼'}${Math.abs(v).toFixed(1)}% <span style="color:var(--text3);font-size:10px">${label}</span>${fromTo}</span>`;
  };

  // 연간 집계 함수
  const calcAnnual = (hist, metric) => {
    const byYear = {};
    hist.forEach(r => {
      if (!byYear[r.bsns_year]) byYear[r.bsns_year] = 0;
      byYear[r.bsns_year] += (r[metric] || 0);
    });
    return Object.entries(byYear)
      .sort(([a],[b]) => a.localeCompare(b))
      .slice(-3); // 최근 3년
  };

  const renderBars = (items, maxVal, labelFn) => {
    if (!items.length || !maxVal) return '';
    return items.map(([label, val]) => {
      const pct = Math.abs(val) / maxVal * 100;
      const color = val >= 0 ? '#2AABEE' : 'var(--red)';
      return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px;flex:1;min-width:0">
        <div style="font-size:9px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">${labelFn(label)}</div>
        <div style="width:100%;background:var(--bg3);border-radius:2px;height:32px;display:flex;align-items:flex-end">
          <div style="width:100%;background:${color};border-radius:2px;height:${Math.max(pct,3)}%;opacity:0.85"></div>
        </div>
        <div style="font-size:9px;color:var(--text2);white-space:nowrap">${fmtCap(val)}</div>
      </div>`;
    }).join('');
  };

  el.innerHTML = surges.map((r, i) => {
    const hist = histMap[r.stock_code] || [];

    // 분기 (최근 6분기)
    const qData = hist.slice(0, 6).reverse().map(h => [h.bsns_year+h.quarter, h[metric]||0]);
    const qMax  = Math.max(...qData.map(([,v]) => Math.abs(v)), 1);

    // 연간 (최근 3년)
    const yData = calcAnnual(hist, metric);
    const yMax  = Math.max(...yData.map(([,v]) => Math.abs(v)), 1);

    // 이전 분기/전년동기 값 계산
    const histSorted = [...hist].sort((a,b) =>
      a.bsns_year !== b.bsns_year ? a.bsns_year.localeCompare(b.bsns_year) : a.quarter.localeCompare(b.quarter));
    const curIdx  = histSorted.findIndex(h => h.bsns_year === r.bsns_year && h.quarter === r.quarter);
    const prevQ   = curIdx > 0 ? histSorted[curIdx - 1] : null;
    const prevY   = hist.find(h => h.bsns_year === String(parseInt(r.bsns_year)-1) && h.quarter === r.quarter);
    const curVal  = r[metric];
    const qoqPrev = prevQ ? prevQ[metric] : null;
    const yoyPrev = prevY ? prevY[metric] : null;

    // QoQ 패턴 분석
    let qoqSignal = '';
    if (curIdx >= 2 && curVal != null) {
      const vals = histSorted.slice(Math.max(0, curIdx-4), curIdx+1).map(h => h[metric]||0);
      const cur  = vals[vals.length-1];
      const p1   = vals[vals.length-2]; // 직전 분기
      const p2   = vals.length >= 3 ? vals[vals.length-3] : null; // 2분기 전
      const p3   = vals.length >= 4 ? vals[vals.length-4] : null; // 3분기 전

      // 베이스 효과: 직전 분기가 현재의 10% 미만
      if (p1 != null && Math.abs(p1) < Math.abs(cur) * 0.1 && Math.abs(cur) > 0) {
        qoqSignal = '<span title="직전 분기가 너무 작아 QoQ 수치 과장 가능" style="font-size:13px;cursor:help">⚠️</span>';
      }
      // 추세전환: 직전 2분기 이상 하락 후 이번 분기 반등
      else if (p2 != null && p1 != null && p1 < p2 && cur > p1) {
        qoqSignal = '<span title="하락 후 반등 (추세전환 신호)" style="font-size:13px;cursor:help">↩️</span>';
      }
      // 성장 가속: 3분기 연속 QoQ 성장
      else if (p2 != null && p1 != null && cur > p1 && p1 > p2 && (p3 == null || p2 > p3)) {
        qoqSignal = '<span title="3분기+ 연속 QoQ 성장 (모멘텀)" style="font-size:13px;cursor:help">🔥</span>';
      }
      // 2분기 연속 성장
      else if (p1 != null && cur > p1 && p2 != null && p1 > p2) {
        qoqSignal = '<span title="2분기 연속 QoQ 성장" style="font-size:13px;cursor:help">📈</span>';
      }
    }

    return `
    <div style="display:grid;grid-template-columns:220px 1fr 1fr;align-items:center;gap:0;padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer"
      onclick="openFinTrend('${r.stock_code}','${r.corp_name}')"
      onmouseover="this.style.background='var(--bg3)'" onmouseout="this.style.background=''">

      <!-- 종목 정보 -->
      <div style="min-width:0;padding-right:12px;border-right:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text3);font-weight:600;width:16px">${i+1}</span>
          <span style="font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.corp_name}</span>
          ${qoqSignal}
        </div>
        <div style="display:flex;align-items:baseline;gap:6px;padding-left:22px">
          <span style="font-size:14px;font-weight:700">${fmtCap(r[metric])}</span>
          <span style="font-size:10px;color:var(--text3)">${r.bsns_year} ${r.quarter}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:3px;padding-left:22px;margin-top:4px">
          ${chgBadge(r[qoqCol], 'QoQ', qoqPrev, curVal)}
          ${chgBadge(r[yoyCol], 'YoY', yoyPrev, curVal)}
        </div>
      </div>

      <!-- 분기별 차트 -->
      <div style="padding:0 12px;border-right:1px solid var(--border)">
        <div style="font-size:10px;color:var(--text3);margin-bottom:4px">분기별</div>
        <div style="display:flex;gap:3px;align-items:flex-end;height:50px">
          ${renderBars(qData, qMax, k => k.slice(2,4)+''+k.slice(4))}
        </div>
      </div>

      <!-- 연간 차트 -->
      <div style="padding:0 12px">
        <div style="font-size:10px;color:var(--text3);margin-bottom:4px">연간</div>
        <div style="display:flex;gap:4px;align-items:flex-end;height:50px">
          ${renderBars(yData, yMax, k => k+'년')}
        </div>
      </div>
    </div>`;
  }).join('') + `<div style="padding:6px 12px;font-size:11px;color:var(--text3)">
    QoQ ${qoqThreshold}% 또는 YoY ${yoyThreshold}% 이상 · 클릭 시 재무 추이
  </div>`;
}


