// investment.js — 오늘의 시황 페이지

// ── 전체 지표 정의 ──
const INV_ALL_METRICS = [
  { col:'sp500',   name:'S&P500',   group:'미국',   color:'#2AABEE' },
  { col:'nasdaq',  name:'나스닥',    group:'미국',   color:'#4a9eff' },
  { col:'dow',     name:'다우',      group:'미국',   color:'#a259ff' },
  { col:'kospi',   name:'코스피',    group:'한국',   color:'#2dce89' },
  { col:'kosdaq',  name:'코스닥',    group:'한국',   color:'#00d4aa' },
  { col:'kospi200',name:'코스피200', group:'한국',   color:'#ffd600' },
  { col:'usd_krw', name:'USD/KRW',  group:'환율',   color:'#fb6340' },
  { col:'jpy_krw', name:'JPY/KRW',  group:'환율',   color:'#f5365c' },
  { col:'eur_krw', name:'EUR/KRW',  group:'환율',   color:'#ffc107' },
  { col:'wti',     name:'WTI',      group:'원자재', color:'#ff6b9d' },
  { col:'gold',    name:'금',        group:'원자재', color:'#ffd700' },
  { col:'vix',     name:'VIX',      group:'기타',   color:'#8b90a7' },
  { col:'us10y',   name:'미 금리',  group:'기타',   color:'#6e7491' },
];

const INV = {
  selected: new Set(['sp500','nasdaq','kospi','kosdaq']),
  period:   7,
};

// ── 페이지 HTML ──
function pInvestment() {
  return `
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:8px">
    <div style="font-size:12px;color:var(--text3)" id="inv-date"></div>
    <button class="btn btn-sm" onclick="loadInvestment()">🔄 새로고침</button>
  </div>

  <!-- 글로벌 지수 -->
  <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">🌍 글로벌 지수</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:1.25rem" id="inv-global">
    ${['','','','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
  </div>

  <!-- 국내 시장 -->
  <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">🇰🇷 국내 시장</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:1.25rem" id="inv-domestic">
    ${['','',''].map(()=>'<div class="card" style="padding:12px 14px;min-height:70px"></div>').join('')}
  </div>

  <!-- 환율 / 원자재 -->
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

  <!-- 흐름 비교 차트 -->
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
        <label style="display:flex;align-items:center;gap:5px;cursor:pointer;padding:3px 8px;border-radius:100px;border:1px solid var(--border);font-size:12px;user-select:none;transition:all .15s"
          id="inv-lbl-${m.col}">
          <input type="checkbox" style="display:none" id="inv-chk-${m.col}"
            onchange="toggleInvMetric('${m.col}')" ${['sp500','nasdaq','kospi','kosdaq'].includes(m.col)?'checked':''}>
          <span style="width:8px;height:8px;border-radius:50%;background:${m.color};flex-shrink:0"></span>
          <span>${m.name}</span>
          <span style="font-size:10px;color:var(--text3);margin-left:2px">${m.group}</span>
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

  <!-- 전체 종목 동향 -->
  <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">📊 전체 종목 동향</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:1.25rem" id="inv-total-summary">
    ${['','','','',''].map(()=>'<div class="metric-card" style="min-height:60px"></div>').join('')}
  </div>

  <!-- 산업별 동향 -->
  <div class="card" style="margin-bottom:1.25rem">
    <div class="card-header">
      <span class="card-title">🏭 산업별 동향</span>
    </div>
    <div id="inv-industry-grid" style="padding:.5rem 0"></div>
  </div>

  <!-- 모니터링 종목 -->
  <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">⭐ 모니터링 종목</div>
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

// ── 메인 로드 ──
async function loadInvestment() {
  loadMacroData();
  loadTrendChart();

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

// ── 매크로 카드 ──
async function loadMacroData() {
  const { data } = await sb.from('macro_data')
    .select('*').order('updated_at', { ascending: false }).limit(1);
  const m = data?.[0] || {};

  document.getElementById('inv-global').innerHTML = [
    mkIndexCard('S&P 500',     m.sp500,    m.sp500_chg,   '',  'USA'),
    mkIndexCard('나스닥',       m.nasdaq,   m.nasdaq_chg,  '',  'USA'),
    mkIndexCard('다우존스',     m.dow,      m.dow_chg,     '',  'USA'),
    mkIndexCard('VIX',         m.vix,      m.vix_chg,     '',  '공포지수'),
    mkIndexCard('미 10년 금리', m.us10y,    m.us10y_chg,   '%', '국채'),
  ].join('');

  document.getElementById('inv-domestic').innerHTML = [
    mkIndexCard('코스피',      m.kospi,    m.kospi_chg,   '',  'KOSPI'),
    mkIndexCard('코스닥',      m.kosdaq,   m.kosdaq_chg,  '',  'KOSDAQ'),
    mkIndexCard('코스피200',   m.kospi200, m.kospi200_chg,'',  '선물'),
  ].join('');

  document.getElementById('inv-fx').innerHTML = [
    mkIndexCard('USD/KRW', m.usd_krw, m.usd_krw_chg, '원', '달러'),
    mkIndexCard('JPY/KRW', m.jpy_krw, m.jpy_krw_chg, '원', '100엔'),
    mkIndexCard('EUR/KRW', m.eur_krw, m.eur_krw_chg, '원', '유로'),
    mkIndexCard('CNY/KRW', m.cny_krw, m.cny_krw_chg, '원', '위안'),
  ].join('');

  document.getElementById('inv-commodity').innerHTML = [
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
    if (INV.selected.size >= 8) { toast('최대 8개까지 선택 가능합니다.', 'info'); return; }
    INV.selected.add(col);
  }
  const m = INV_ALL_METRICS.find(x => x.col === col);
  const lbl = document.getElementById('inv-lbl-' + col);
  if (lbl && m) {
    const on = INV.selected.has(col);
    lbl.style.background  = on ? m.color + '22' : '';
    lbl.style.borderColor = on ? m.color : 'var(--border)';
    lbl.style.color       = on ? m.color : '';
  }
  loadTrendChart();
}

function initInvCheckboxStyles() {
  INV_ALL_METRICS.forEach(m => {
    const lbl = document.getElementById('inv-lbl-' + m.col);
    if (!lbl) return;
    const on = INV.selected.has(m.col);
    lbl.style.background  = on ? m.color + '22' : '';
    lbl.style.borderColor = on ? m.color : 'var(--border)';
    lbl.style.color       = on ? m.color : '';
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
    const values     = rows.map(r => r[m.col]);
    const base       = values.find(v => v != null);
    const normalized = values.map(v => v != null && base ? Math.round(v / base * 10000) / 100 : null);
    return {
      label: m.name, data: normalized,
      borderColor: m.color, backgroundColor: m.color + '15',
      borderWidth: 2, pointRadius: 2, pointHoverRadius: 5,
      tension: 0.3, fill: false, spanGaps: true,
    };
  });

  if (_invTrendChart) { _invTrendChart.destroy(); _invTrendChart = null; }

  _invTrendChart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { color: '#a8adc4', font: { size: 11 }, padding: 12, usePointStyle: true } },
        tooltip: {
          backgroundColor: '#1a1d27', borderColor: 'rgba(255,255,255,.1)', borderWidth: 1,
          callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(2) ?? '—'}` }
        },
      },
      scales: {
        x: { ticks: { color: '#6e7491', font: { size: 10 }, maxTicksLimit: 10 }, grid: { color: 'rgba(255,255,255,.04)' } },
        y: {
          ticks: { color: '#6e7491', font: { size: 10 }, callback: v => v.toFixed(0) },
          grid:  { color: 'rgba(255,255,255,.06)' },
        },
      },
    },
  });
}
