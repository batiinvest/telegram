// investment.js — 오늘의 시황 페이지
// 의존: config.js (fmtCap, chgColor, chgStr, loadingHTML)

function pInvestment() {
  return `
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;flex-wrap:wrap;gap:8px">
    <div style="font-size:12px;color:var(--text3)" id="inv-date"></div>
    <button class="btn btn-sm" onclick="loadInvestment()">🔄 새로고침</button>
  </div>

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

  <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px">📊 모니터링 종목 현황</div>
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

function mkIndexCard(label, value, chg, unit, sub) {
  const cc  = chg != null ? chgColor(chg) : 'var(--text2)';
  const cs  = chg != null ? chgStr(chg) : '—';
  const val = value != null ? Number(value).toLocaleString() + (unit||'') : '—';
  return `
  <div class="card" style="padding:12px 14px">
    <div style="font-size:11px;color:var(--text3);margin-bottom:4px">${label}</div>
    <div style="font-size:16px;font-weight:700;color:var(--text1)">${val}</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:3px">
      <div style="font-size:12px;color:${cc};font-weight:500">${cs}</div>
      ${sub ? `<div style="font-size:10px;color:var(--text3)">${sub}</div>` : ''}
    </div>
  </div>`;
}

async function loadInvestment() {
  // 1) macro_data (글로벌/환율/원자재)
  loadMacroData();

  // 2) 최신 market_data 날짜
  const { data: dateRow } = await sb.from('market_data')
    .select('base_date').order('base_date', { ascending: false }).limit(1);
  const maxDate = dateRow?.[0]?.base_date;
  const dateEl = document.getElementById('inv-date');
  if (dateEl) dateEl.textContent = maxDate ? `기준: ${maxDate}` : '';
  if (!maxDate) return;

  // 3) 모니터링 종목 시장 데이터
  const { data: mktRows, error } = await sb.from('market_data')
    .select('stock_code,corp_name,price,price_change_rate,market_cap,market')
    .eq('base_date', maxDate)
    .order('price_change_rate', { ascending: false });

  if (error || !mktRows?.length) return;

  // null 제거 후 처리
  const rows = mktRows.filter(r => r.price_change_rate != null);
  if (!rows.length) return;

  const rise   = rows.filter(r => r.price_change_rate > 0).length;
  const fall   = rows.filter(r => r.price_change_rate < 0).length;
  const avgChg = rows.reduce((s, r) => s + r.price_change_rate, 0) / rows.length;

  const summaryEl = document.getElementById('inv-summary');
  if (summaryEl) summaryEl.innerHTML = `
    <div class="metric-card"><div class="metric-label">상승 종목</div><div class="metric-value" style="color:var(--red)">${rise}개</div></div>
    <div class="metric-card"><div class="metric-label">하락 종목</div><div class="metric-value" style="color:var(--blue)">${fall}개</div></div>
    <div class="metric-card"><div class="metric-label">평균 등락률</div><div class="metric-value" style="color:${chgColor(avgChg)}">${chgStr(avgChg)}</div></div>`;

  const rankRow = (r, i) => `
    <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid var(--border)">
      <span style="width:16px;font-size:11px;color:var(--text3);font-weight:600">${i+1}</span>
      <span style="flex:1;font-size:13px;font-weight:500">${r.corp_name}</span>
      <span style="font-size:11px;color:var(--text3)">${r.market||''}</span>
      <span style="font-size:13px;font-weight:600;color:${chgColor(r.price_change_rate)}">${chgStr(r.price_change_rate)}</span>
    </div>`;

  document.getElementById('inv-surge').innerHTML = rows.slice(0, 5).map(rankRow).join('');
  document.getElementById('inv-drop').innerHTML  = [...rows].reverse().slice(0, 5).map(rankRow).join('');
}

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
