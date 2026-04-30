// comparison.js — 기업 비교 분석 페이지
// 의존: config.js (INDUSTRIES, CATS, fetchAllPages, fmtCap, chgColor, chgStr, loadingHTML, emptyHTML)

// ── 비교 상태 ──
const CMP = {
  selectedCodes: [],   // 선택된 종목코드 배열
  industry: '',        // 산업 필터 ('' = 전체)
  metric: 'revenue',  // 선택된 지표
  period: '8',         // 표시 분기 수
};

const CMP_METRICS = [
  { key: 'revenue',          label: '매출액',      unit: '억', scale: 1e8 },
  { key: 'operating_profit', label: '영업이익',    unit: '억', scale: 1e8 },
  { key: 'net_income',       label: '순이익',      unit: '억', scale: 1e8 },
  { key: 'operating_margin', label: '영업이익률',  unit: '%',  scale: 1 },
  { key: 'roe',              label: 'ROE',          unit: '%',  scale: 1 },
  { key: 'roa',              label: 'ROA',          unit: '%',  scale: 1 },
  { key: 'debt_ratio',       label: '부채비율',    unit: '%',  scale: 1 },
  { key: 'total_assets',     label: '자산총계',    unit: '억', scale: 1e8 },
  { key: 'operating_cashflow', label: '영업현금흐름', unit: '억', scale: 1e8 },
];

// Chart.js 색상 팔레트
const CMP_COLORS = [
  '#2AABEE','#2dce89','#f5365c','#fb6340','#ffd600',
  '#a259ff','#11cdef','#4a9eff','#ff6b9d','#00d4aa',
];

function pComparison() {
  return `
  <div style="display:grid;grid-template-columns:300px 1fr;gap:12px;align-items:start">

    <!-- 좌측 패널: 종목 선택 -->
    <div style="position:sticky;top:1rem;display:flex;flex-direction:column;gap:10px">

      <!-- 산업 선택 -->
      <div class="card">
        <div class="card-header"><span class="card-title">산업 선택</span></div>
        <div style="padding:.75rem;display:flex;flex-direction:column;gap:6px">
          <select class="form-select" id="cmp-industry" onchange="onCmpIndustryChange()" style="width:100%">
            <option value="">-- 산업 선택 --</option>
            ${INDUSTRIES.map(i=>`<option value="${i}">${i}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-primary" onclick="addIndustryAll()" style="width:100%">산업 전체 추가</button>
        </div>
      </div>

      <!-- 종목 검색 -->
      <div class="card">
        <div class="card-header"><span class="card-title">종목 검색</span></div>
        <div style="padding:.75rem;display:flex;flex-direction:column;gap:6px">
          <div style="position:relative">
            <input class="form-input" id="cmp-search" placeholder="종목명 검색..."
              oninput="onCmpSearch(this.value)" autocomplete="off"
              style="width:100%;box-sizing:border-box">
            <div id="cmp-dropdown" style="
              display:none;position:absolute;top:100%;left:0;right:0;z-index:100;
              background:var(--bg2);border:1px solid var(--border2);border-radius:var(--radius-sm);
              max-height:200px;overflow-y:auto;margin-top:2px"></div>
          </div>
        </div>
      </div>

      <!-- 선택된 종목 -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">선택 종목 <span id="cmp-count" style="font-size:11px;color:var(--text3)">0개</span></span>
          <button class="btn btn-sm" onclick="clearCmpStocks()">전체 해제</button>
        </div>
        <div id="cmp-selected-list" style="padding:.5rem;min-height:60px;max-height:300px;overflow-y:auto">
          <div style="padding:.5rem;text-align:center;color:var(--text3);font-size:12px">종목을 선택해주세요</div>
        </div>
        <div style="padding:.5rem .75rem;border-top:1px solid var(--border)">
          <button class="btn btn-primary" style="width:100%" onclick="runComparison()">비교 분석 실행</button>
        </div>
      </div>

      <!-- 지표/기간 설정 -->
      <div class="card">
        <div class="card-header"><span class="card-title">지표 / 기간</span></div>
        <div style="padding:.75rem;display:flex;flex-direction:column;gap:8px">
          <div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:4px">재무 지표</div>
            <select class="form-select" id="cmp-metric" onchange="CMP.metric=this.value" style="width:100%">
              ${CMP_METRICS.map(m=>`<option value="${m.key}" ${CMP.metric===m.key?'selected':''}>${m.label}</option>`).join('')}
            </select>
          </div>
          <div>
            <div style="font-size:11px;color:var(--text2);margin-bottom:4px">표시 분기</div>
            <select class="form-select" id="cmp-period" onchange="CMP.period=this.value" style="width:100%">
              <option value="4">최근 4분기</option>
              <option value="8" selected>최근 8분기</option>
              <option value="12">최근 12분기</option>
            </select>
          </div>
        </div>
      </div>

    </div>

    <!-- 우측: 차트/테이블 -->
    <div id="cmp-result">
      <div style="padding:3rem;text-align:center;color:var(--text3);font-size:13px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius)">
        <div style="font-size:24px;margin-bottom:.5rem">📊</div>
        <div>좌측에서 종목을 선택하고 비교 분석을 실행해주세요</div>
        <div style="font-size:11px;margin-top:.5rem;color:var(--text3)">산업 전체 추가 또는 개별 종목 검색으로 최대 10개까지 비교 가능합니다</div>
      </div>
    </div>

  </div>`;
}

// ── 산업 변경 → 해당 산업 종목 드롭다운 표시 ──
async function onCmpIndustryChange() {
  const ind = document.getElementById('cmp-industry')?.value;
  CMP.industry = ind;
}

// ── 산업 전체 추가 ──
async function addIndustryAll() {
  const ind = document.getElementById('cmp-industry')?.value;
  if (!ind) { toast('산업을 먼저 선택해주세요.', 'error'); return; }

  const rows = await fetchAllPages(
    sb.from('companies').select('code,name,industry').eq('industry', ind).eq('active', true)
  );

  let added = 0;
  for (const r of rows) {
    const code = (r.code || '').split('.')[0];
    if (!code) continue;
    if (CMP.selectedCodes.length >= 10) { toast('최대 10개까지 선택 가능합니다.', 'error'); break; }
    if (!CMP.selectedCodes.find(s => s.code === code)) {
      CMP.selectedCodes.push({ code, name: r.name, industry: ind });
      added++;
    }
  }
  renderCmpSelected();
  if (added > 0) toast(`${ind} ${added}개 종목 추가됨`, 'success');
}

// ── 종목 검색 자동완성 ──
let _cmpSearchTimer = null;
async function onCmpSearch(q) {
  clearTimeout(_cmpSearchTimer);
  const dd = document.getElementById('cmp-dropdown');
  if (!q || q.length < 1) { dd.style.display = 'none'; return; }

  _cmpSearchTimer = setTimeout(async () => {
    const rows = await fetchAllPages(
      sb.from('companies').select('code,name,industry')
        .ilike('name', `%${q}%`).eq('active', true).limit(20)
    );
    if (!rows.length) { dd.style.display = 'none'; return; }
    dd.innerHTML = rows.map(r => {
      const code = (r.code || '').split('.')[0];
      const already = CMP.selectedCodes.find(s => s.code === code);
      return `<div onclick="addCmpStock('${code}','${r.name}','${r.industry||''}')"
        style="padding:8px 12px;font-size:13px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;
        border-bottom:1px solid var(--border);${already?'opacity:.4;pointer-events:none':''}">
        <span>${r.name} <span style="font-size:11px;color:var(--text3)">${code}</span></span>
        <span style="font-size:11px;color:${CATS[r.industry]||'var(--text3)'}">
          ${already?'추가됨':r.industry||''}
        </span>
      </div>`;
    }).join('');
    dd.style.display = 'block';
  }, 200);
}

function addCmpStock(code, name, industry) {
  if (CMP.selectedCodes.length >= 10) { toast('최대 10개까지 선택 가능합니다.', 'error'); return; }
  if (CMP.selectedCodes.find(s => s.code === code)) return;
  CMP.selectedCodes.push({ code, name, industry });
  renderCmpSelected();
  document.getElementById('cmp-dropdown').style.display = 'none';
  document.getElementById('cmp-search').value = '';
}

function removeCmpStock(code) {
  CMP.selectedCodes = CMP.selectedCodes.filter(s => s.code !== code);
  renderCmpSelected();
}

function clearCmpStocks() {
  CMP.selectedCodes = [];
  renderCmpSelected();
}

function renderCmpSelected() {
  const el = document.getElementById('cmp-selected-list');
  const cnt = document.getElementById('cmp-count');
  if (!el) return;
  if (cnt) cnt.textContent = `${CMP.selectedCodes.length}개`;
  if (!CMP.selectedCodes.length) {
    el.innerHTML = '<div style="padding:.5rem;text-align:center;color:var(--text3);font-size:12px">종목을 선택해주세요</div>';
    return;
  }
  el.innerHTML = CMP.selectedCodes.map((s, i) => `
    <div style="display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:var(--radius-sm);background:var(--bg3);margin-bottom:4px">
      <span style="width:10px;height:10px;border-radius:50%;background:${CMP_COLORS[i%CMP_COLORS.length]};flex-shrink:0"></span>
      <span style="font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.name}</span>
      <span style="font-size:10px;color:var(--text3)">${s.code}</span>
      <button onclick="removeCmpStock('${s.code}')"
        style="background:none;border:none;color:var(--text3);cursor:pointer;padding:0 2px;font-size:14px;line-height:1">×</button>
    </div>`).join('');
}

// ── 비교 분석 실행 ──
async function runComparison() {
  if (CMP.selectedCodes.length < 1) { toast('종목을 1개 이상 선택해주세요.', 'error'); return; }

  const el = document.getElementById('cmp-result');
  el.innerHTML = loadingHTML('데이터 로드 중...');

  try {
    const codes = CMP.selectedCodes.map(s => s.code);
    const period = parseInt(CMP.period);
    const metric = CMP.metric;
    const metaDef = CMP_METRICS.find(m => m.key === metric);

    // 재무 데이터 조회
    const finRows = await fetchAllPages(
      sb.from('financials')
        .select('stock_code,corp_name,bsns_year,quarter,fs_div,' + CMP_METRICS.map(m=>m.key).join(','))
        .in('stock_code', codes)
        .eq('fs_div', 'CFS')
        .order('bsns_year', { ascending: false })
        .order('quarter', { ascending: false })
    );

    // 시장 데이터 조회 (최신 1일치)
    const { data: dateRow } = await sb.from('market_data').select('base_date').order('base_date', { ascending: false }).limit(1);
    const maxDate = dateRow?.[0]?.base_date;
    let mktMap = {};
    if (maxDate) {
      const mktRows = await fetchAllPages(
        sb.from('market_data')
          .select('stock_code,corp_name,price,price_change_rate,market_cap,per,pbr')
          .in('stock_code', codes)
          .eq('base_date', maxDate)
      );
      mktRows.forEach(r => { mktMap[r.stock_code] = r; });
    }

    // 종목별 데이터 구성
    const stockDataMap = {};
    codes.forEach(code => { stockDataMap[code] = []; });
    finRows.forEach(r => {
      if (stockDataMap[r.stock_code]) {
        stockDataMap[r.stock_code].push(r);
      }
    });

    // 공통 기간 라벨 (최신 N분기)
    const allLabels = new Set();
    codes.forEach(code => {
      stockDataMap[code].slice(0, period).forEach(r => allLabels.add(`${r.bsns_year} ${r.quarter}`));
    });
    const sortedLabels = [...allLabels].sort((a, b) => {
      const [ya, qa] = a.split(' ');
      const [yb, qb] = b.split(' ');
      return ya !== yb ? ya.localeCompare(yb) : qa.localeCompare(qb);
    }).slice(-period);

    // Chart.js 데이터셋 구성
    const datasets = CMP.selectedCodes.map((s, i) => {
      const color = CMP_COLORS[i % CMP_COLORS.length];
      const rows = stockDataMap[s.code] || [];
      const rowMap = {};
      rows.forEach(r => { rowMap[`${r.bsns_year} ${r.quarter}`] = r; });
      const data = sortedLabels.map(lbl => {
        const row = rowMap[lbl];
        if (!row) return null;
        const val = row[metric];
        return val != null ? (metaDef.scale === 1 ? val : Math.round(val / metaDef.scale)) : null;
      });
      return {
        label: s.name,
        data,
        borderColor: color,
        backgroundColor: color + '22',
        borderWidth: 2,
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.3,
        fill: false,
      };
    });

    // 주가 동향 (이동평균) 계산
    const maData = {};
    for (const s of CMP.selectedCodes) {
      const { data: priceRows } = await sb.from('market_data')
        .select('base_date,price,price_change_rate')
        .eq('stock_code', s.code)
        .order('base_date', { ascending: false })
        .limit(60);
      if (priceRows?.length) {
        const prices = priceRows.map(r => r.price).filter(Boolean);
        const latest = priceRows[0];
        const ma5  = prices.slice(0, 5).reduce((a,b)=>a+b,0) / Math.min(5, prices.length);
        const ma20 = prices.slice(0, 20).reduce((a,b)=>a+b,0) / Math.min(20, prices.length);
        const ma60 = prices.slice(0, 60).reduce((a,b)=>a+b,0) / Math.min(60, prices.length);
        maData[s.code] = {
          price: latest.price,
          chg: latest.price_change_rate,
          ma5: Math.round(ma5),
          ma20: Math.round(ma20),
          ma60: Math.round(ma60),
        };
      }
    }

    // 결과 렌더링
    el.innerHTML = `
      <!-- 주가 동향 카드 -->
      <div class="card" style="margin-bottom:1rem">
        <div class="card-header">
          <span class="card-title">주가 동향</span>
          <span style="font-size:11px;color:var(--text3)">기준: ${maxDate || '—'}</span>
        </div>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead>
              <tr>
                <th style="padding:8px 12px;text-align:left;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">종목</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">현재가</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">등락률</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">5일선</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">20일선</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">60일선</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">시총</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">PER</th>
                <th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">PBR</th>
              </tr>
            </thead>
            <tbody>
              ${CMP.selectedCodes.map((s, i) => {
                const ma = maData[s.code];
                const mkt = mktMap[s.code];
                const color = CMP_COLORS[i % CMP_COLORS.length];
                if (!ma && !mkt) return `<tr><td colspan="9" style="padding:8px 12px;color:var(--text3);font-size:12px">${s.name} — 시장 데이터 없음</td></tr>`;
                const price = ma?.price || mkt?.price;
                const chg = ma?.chg ?? mkt?.price_change_rate;
                const ma5pos  = ma && price > ma.ma5  ? 'var(--red)' : 'var(--blue)';
                const ma20pos = ma && price > ma.ma20 ? 'var(--red)' : 'var(--blue)';
                const ma60pos = ma && price > ma.ma60 ? 'var(--red)' : 'var(--blue)';
                return `<tr style="border-bottom:1px solid var(--border)">
                  <td style="padding:8px 12px">
                    <div style="display:flex;align-items:center;gap:6px">
                      <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>
                      <span style="font-weight:500">${s.name}</span>
                    </div>
                  </td>
                  <td style="padding:8px 12px;text-align:right;font-weight:500">${price ? price.toLocaleString()+'원' : '—'}</td>
                  <td style="padding:8px 12px;text-align:right;color:${chgColor(chg)};font-weight:500">${chg != null ? chgStr(chg) : '—'}</td>
                  <td style="padding:8px 12px;text-align:right;color:${ma5pos}">${ma?.ma5 ? ma.ma5.toLocaleString()+'원' : '—'}</td>
                  <td style="padding:8px 12px;text-align:right;color:${ma20pos}">${ma?.ma20 ? ma.ma20.toLocaleString()+'원' : '—'}</td>
                  <td style="padding:8px 12px;text-align:right;color:${ma60pos}">${ma?.ma60 ? ma.ma60.toLocaleString()+'원' : '—'}</td>
                  <td style="padding:8px 12px;text-align:right">${mkt?.market_cap ? fmtCap(mkt.market_cap) : '—'}</td>
                  <td style="padding:8px 12px;text-align:right">${mkt?.per ? mkt.per.toFixed(1) : '—'}</td>
                  <td style="padding:8px 12px;text-align:right">${mkt?.pbr ? mkt.pbr.toFixed(2) : '—'}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
      </div>

      <!-- 지표 선택 탭 -->
      <div class="card" style="margin-bottom:1rem">
        <div class="card-header">
          <span class="card-title">분기별 재무 비교</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            ${CMP_METRICS.map(m => `
              <button class="chip ${CMP.metric===m.key?'active':''}"
                onclick="CMP.metric='${m.key}';document.getElementById('cmp-metric').value='${m.key}';renderCmpChart('${m.key}')"
                style="font-size:11px;padding:3px 8px">${m.label}</button>
            `).join('')}
          </div>
        </div>
        <div style="padding:1rem">
          <canvas id="cmp-chart" style="max-height:380px"></canvas>
        </div>
      </div>

      <!-- 최신 분기 비교 테이블 -->
      <div class="card">
        <div class="card-header"><span class="card-title">최신 분기 재무 지표 비교</span></div>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead>
              <tr>
                <th style="padding:8px 12px;text-align:left;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">종목</th>
                ${CMP_METRICS.map(m=>`<th style="padding:8px 12px;text-align:right;font-size:11px;color:var(--text3);border-bottom:1px solid var(--border)">${m.label}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${CMP.selectedCodes.map((s, i) => {
                const rows = stockDataMap[s.code] || [];
                const latest = rows[0];
                const color = CMP_COLORS[i % CMP_COLORS.length];
                if (!latest) return `<tr><td colspan="${CMP_METRICS.length+1}" style="padding:8px 12px;color:var(--text3);font-size:12px">${s.name} — 재무 데이터 없음</td></tr>`;
                return `<tr style="border-bottom:1px solid var(--border)">
                  <td style="padding:8px 12px">
                    <div style="display:flex;align-items:center;gap:6px">
                      <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>
                      <div>
                        <div style="font-weight:500">${s.name}</div>
                        <div style="font-size:11px;color:var(--text3)">${latest.bsns_year} ${latest.quarter}</div>
                      </div>
                    </div>
                  </td>
                  ${CMP_METRICS.map(m => {
                    const v = latest[m.key];
                    if (v == null) return `<td style="padding:8px 12px;text-align:right;color:var(--text3)">—</td>`;
                    const formatted = m.scale === 1
                      ? v.toFixed(1) + m.unit
                      : fmtCap(v * 1); // 이미 원단위
                    const isHighlight = m.key === CMP.metric;
                    return `<td style="padding:8px 12px;text-align:right;${isHighlight?`color:${color};font-weight:600`:''}">${formatted}</td>`;
                  }).join('')}
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
      </div>`;

    // 차트 렌더링
    window._cmpChartDatasets = datasets;
    window._cmpChartLabels   = sortedLabels;
    renderCmpChart(metric);

  } catch(e) {
    el.innerHTML = `<div style="padding:1rem;color:var(--red)">${e.message}</div>`;
    console.error('[비교분석]', e);
  }
}

// ── Chart.js 차트 렌더링 ──
let _cmpChartInstance = null;
function renderCmpChart(metricKey) {
  const canvas = document.getElementById('cmp-chart');
  if (!canvas || !window._cmpChartDatasets) return;

  const metaDef = CMP_METRICS.find(m => m.key === metricKey) || CMP_METRICS[0];
  const labels  = window._cmpChartLabels;
  const period  = parseInt(CMP.period);

  // 선택된 지표의 data로 datasets 재구성
  const codes = CMP.selectedCodes.map(s => s.code);
  const finRows = [];  // 이미 stockDataMap에 있음 — 간단히 기존 datasets 활용
  const datasets = window._cmpChartDatasets.map((ds, i) => {
    const s = CMP.selectedCodes[i];
    return {
      ...ds,
      // data는 runComparison에서 기본 metric으로 만들어져 있음
      // 다른 metric 클릭 시 재조회 필요하므로 별도 처리
    };
  });

  if (_cmpChartInstance) { _cmpChartInstance.destroy(); _cmpChartInstance = null; }

  // 지표가 바뀌면 데이터 재조회 후 차트 재렌더
  if (metricKey !== CMP.metric || !window._cmpMetricCache?.[metricKey]) {
    // 캐시 없으면 DB에서 해당 지표 재조회
    fetchCmpMetricAndRender(metricKey, canvas, labels, metaDef);
    return;
  }

  drawCmpChart(canvas, window._cmpMetricCache[metricKey], labels, metaDef);
}

async function fetchCmpMetricAndRender(metricKey, canvas, labels, metaDef) {
  if (!window._cmpMetricCache) window._cmpMetricCache = {};
  if (window._cmpMetricCache[metricKey]) {
    drawCmpChart(canvas, window._cmpMetricCache[metricKey], labels, metaDef);
    return;
  }

  const codes = CMP.selectedCodes.map(s => s.code);
  const period = parseInt(CMP.period);
  const rows = await fetchAllPages(
    sb.from('financials')
      .select(`stock_code,bsns_year,quarter,${metricKey}`)
      .in('stock_code', codes)
      .eq('fs_div', 'CFS')
      .order('bsns_year', { ascending: false })
      .order('quarter', { ascending: false })
  );

  const stockMap = {};
  codes.forEach(c => { stockMap[c] = {}; });
  rows.forEach(r => {
    if (stockMap[r.stock_code]) {
      stockMap[r.stock_code][`${r.bsns_year} ${r.quarter}`] = r[metricKey];
    }
  });

  const datasets = CMP.selectedCodes.map((s, i) => {
    const color = CMP_COLORS[i % CMP_COLORS.length];
    const data = labels.map(lbl => {
      const v = stockMap[s.code]?.[lbl];
      return v != null ? (metaDef.scale === 1 ? v : Math.round(v / metaDef.scale)) : null;
    });
    return {
      label: s.name,
      data,
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: 2,
      pointRadius: 4,
      pointHoverRadius: 6,
      tension: 0.3,
      fill: false,
    };
  });

  window._cmpMetricCache[metricKey] = datasets;

  // 탭 active 업데이트
  document.querySelectorAll('.chip').forEach(el => {
    el.classList.toggle('active', el.textContent.trim() === metaDef.label);
  });

  drawCmpChart(canvas, datasets, labels, metaDef);
}

function drawCmpChart(canvas, datasets, labels, metaDef) {
  if (_cmpChartInstance) { _cmpChartInstance.destroy(); _cmpChartInstance = null; }

  _cmpChartInstance = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: { color: '#8b90a7', font: { size: 12 }, padding: 16, usePointStyle: true },
        },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: 'rgba(255,255,255,.1)',
          borderWidth: 1,
          titleColor: '#e8eaf0',
          bodyColor: '#8b90a7',
          callbacks: {
            label: ctx => {
              const v = ctx.parsed.y;
              if (v == null) return `${ctx.dataset.label}: —`;
              return `${ctx.dataset.label}: ${v.toLocaleString()}${metaDef.unit}`;
            }
          }
        },
      },
      scales: {
        x: {
          ticks: { color: '#555a70', font: { size: 11 } },
          grid: { color: 'rgba(255,255,255,.04)' },
        },
        y: {
          ticks: {
            color: '#555a70', font: { size: 11 },
            callback: v => v.toLocaleString() + metaDef.unit,
          },
          grid: { color: 'rgba(255,255,255,.06)' },
        },
      },
    },
  });
}

// 외부 클릭 시 드롭다운 닫기
document.addEventListener('click', e => {
  const dd = document.getElementById('cmp-dropdown');
  if (dd && !e.target.closest('#cmp-search') && !e.target.closest('#cmp-dropdown')) {
    dd.style.display = 'none';
  }
});
