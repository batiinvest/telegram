// financials.js — 재무 조회 (시장/재무제표/종합)

function fmtCap(won) {
  if (won == null || won === 0) return '—';
  const jo  = Math.floor(won / 1e12);
  const eok = Math.floor((won % 1e12) / 1e8);
  if (jo > 0 && eok > 0) return jo + '조 ' + eok.toLocaleString() + '억';
  if (jo > 0)             return jo + '조';
  return eok.toLocaleString() + '억';
}


function pFinancials() {
  const industries = ['전체', ...INDUSTRIES];
  return `
  <div class="tabs" style="margin-bottom:.75rem">
    <button class="tab fin-tab ${F.mode==='market'?'active':''}" data-mode="market" onclick="F.mode='market';loadFinancials()">시장 데이터</button>
    <button class="tab fin-tab ${F.mode==='financial'?'active':''}" data-mode="financial" onclick="F.mode='financial';loadFinancials()">재무제표</button>
    <button class="tab fin-tab ${F.mode==='combined'?'active':''}" data-mode="combined" onclick="F.mode='combined';loadFinancials()">종합</button>
  </div>

  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:1rem">
    <select class="form-select" id="fin-scope" onchange="F.scope=this.value;loadFinancials()" style="width:130px;padding:6px 10px">
      <option value="monitored" ${F.scope==='monitored'?'selected':''}>모니터링 종목</option>
      <option value="all" ${F.scope==='all'?'selected':''}>전체</option>
    </select>
    <input class="search-box" id="fin-q" placeholder="종목명 검색..." oninput="F.q=this.value;loadFinancials()" style="max-width:160px">
    <select class="form-select" id="fin-ind" onchange="F.industry=this.value;loadFinancials()" style="width:120px;padding:6px 10px">
      ${industries.map(i=>`<option value="${i}" ${F.industry===i?'selected':''}>${i}</option>`).join('')}
    </select>
    <span style="font-size:12px;color:var(--text3)" id="fin-count"></span>
    <div style="margin-left:auto;display:flex;gap:6px">
      <button class="btn btn-sm" onclick="loadFinancials()">새로고침</button>
      <button class="btn btn-sm" onclick="exportFinancials()">CSV 다운로드</button>
    </div>
  </div>

  <div class="card" id="fin-table">
    ${loadingHTML()}
  </div>`;
}

let _finData = [];

async function loadFinancials() {
  const el = document.getElementById('fin-table');
  if (!el) return;

  // 산업 필터용 companies 캐시 (없으면 로드)
  if (!window._finIndMap) {
    window._finIndMap = {};
    const rows = await fetchAllPages(sb.from('companies').select('code,industry'));
    rows.forEach(r => { if (r.code) window._finIndMap[r.code.split('.')[0]] = r.industry || ''; });
  }

  // 탭 active 상태 업데이트
  document.querySelectorAll('.fin-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.mode === F.mode);
  });

  el.innerHTML = loadingHTML();

  try {
    if (F.mode === 'market') {
      await loadMarketData(el);
    } else if (F.mode === 'financial') {
      await loadFinancialData(el);
    } else {
      await loadCombinedData(el);
    }
  } catch(e) {
    el.innerHTML = `${errorHTML(e.message)}`;
  }
}

async function loadMarketData(el) {
  // 모니터링 종목 코드 목록 로드
  let monitoredCodes = null;
  let allCodesSet = null;

  if (F.scope === 'monitored') {
    // 모니터링 종목만
    const compData = await fetchAllPages(
      sb.from('companies').select('code').in('monitoring_level', ['full','news'])
    );
    monitoredCodes = new Set(compData.map(c => c.code));
  }

  // 가장 최신 base_date 조회
  const { data: latestDate } = await sb.from('market_data')
    .select('base_date').order('base_date', { ascending: false }).limit(1);
  const maxDate = latestDate?.[0]?.base_date;

  // 최신 날짜 데이터 전체 로드
  let allMkt = [];
  if (maxDate) {
    allMkt = await fetchAllPages(
      sb.from('market_data').select('*').eq('base_date', maxDate)
    );
  }

  // scope 필터
  const data = monitoredCodes
    ? allMkt.filter(r => monitoredCodes.has(r.stock_code))
    : allMkt;

  // 종목당 최신 1개 (혹시 중복 있을 경우 대비)
  const latest = {};
  (data || []).forEach(r => {
    if (!latest[r.stock_code]) latest[r.stock_code] = r;
  });
  let rows = Object.values(latest);

  // 필터
  if (F.q) rows = rows.filter(r => r.corp_name.includes(F.q));

  // 산업 필터 — companies 테이블과 조인 불가하니 종목 목록으로 필터
  if (F.industry !== '전체') {
    const indStocks = new Set(
      Object.entries(window._finIndMap || {}).filter(([,ind]) => ind === F.industry).map(([code]) => code)
    );
    rows = rows.filter(r => indStocks.has(r.stock_code));
  }

  // 정렬
  rows.sort((a, b) => {
    const av = a[F.sortBy] ?? -Infinity;
    const bv = b[F.sortBy] ?? -Infinity;
    return F.sortDir === 'desc' ? bv - av : av - bv;
  });

  _finData = rows;
  const cnt = document.getElementById('fin-count');
  if (cnt) cnt.textContent = `${rows.length}개`;

  if (!rows.length) {
    el.innerHTML = emptyHTML();
    return;
  }

  const sortBtn = (col, label) => {
    const active = F.sortBy === col;
    const dir = active && F.sortDir === 'desc' ? '↑' : '↓';
    return `<span style="cursor:pointer;${active?'color:var(--tg)':''}" onclick="F.sortBy='${col}';F.sortDir=F.sortBy==='${col}'&&F.sortDir==='desc'?'asc':'desc';loadFinancials()">${label}${active?dir:''}</span>`;
  };

  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>종목명</th>
      <th>${sortBtn('market_cap','시가총액')}</th>
      <th>${sortBtn('price','현재가')}</th>
      <th>${sortBtn('price_change_rate','등락률')}</th>
      <th>${sortBtn('per','PER')}</th>
      <th>${sortBtn('pbr','PBR')}</th>
      <th>${sortBtn('eps','EPS')}</th>
      <th>${sortBtn('volume','거래량')}</th>
      <th>기준일</th>
    </tr></thead>
    <tbody>${rows.map(r => {
      const chg = r.price_change_rate;
      const chgColor = chg > 0 ? 'var(--red)' : chg < 0 ? '#4a9eff' : 'var(--text3)';
      const chgStr = chg != null && chg !== 0 ? `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%` : (chg === 0 ? '0.00%' : '—');
      const cap = fmtCap(r.market_cap);
      return `<tr>
        <td style="font-weight:500">${r.corp_name}</td>
        <td>${cap}</td>
        <td>${r.price ? r.price.toLocaleString() + '원' : '—'}</td>
        <td style="color:${chgColor};font-weight:500">${chgStr}</td>
        <td>${r.per != null && r.per !== 0 ? r.per.toFixed(1) : '—'}</td>
        <td>${r.pbr != null && r.pbr !== 0 ? r.pbr.toFixed(2) : '—'}</td>
        <td>${r.eps ? r.eps.toLocaleString() : '—'}</td>
        <td>${r.volume ? r.volume.toLocaleString() : '—'}</td>
        <td style="font-size:11px;color:var(--text3)">${r.base_date || '—'}</td>
      </tr>`;
    }).join('')}
    </tbody></table></div>`;
}

async function loadFinancialData(el) {
  // 종목당 최신 분기 데이터 — 전체 조회 후 stock_code 기준 최신 1개 추출
  // 모니터링 종목 코드 목록 (scope='monitored' 시)
  let monitoredCodesFin = null;
  if (F.scope === 'monitored') {
    const compData = await fetchAllPages(
      sb.from('companies').select('code').in('monitoring_level', ['full','news'])
    );
    monitoredCodesFin = new Set(compData.map(c => c.code));
  }

  const allFin = await fetchAllPages(
    sb.from('financials').select('*')
      .order('bsns_year', { ascending: false })
      .order('quarter', { ascending: false })
  );
  const data = monitoredCodesFin
    ? allFin.filter(r => monitoredCodesFin.has(r.stock_code))
    : allFin;

  // 종목당 최신 1개 (bsns_year+quarter 기준)
  const latest = {};
  (data || []).forEach(r => {
    const key = r.stock_code;
    if (!latest[key]) {
      latest[key] = r;
    } else {
      // 더 최신 데이터로 교체
      const cur = latest[key];
      if (r.bsns_year > cur.bsns_year ||
         (r.bsns_year === cur.bsns_year && r.quarter > cur.quarter)) {
        latest[key] = r;
      }
    }
  });
  let rows = Object.values(latest);

  if (F.q) rows = rows.filter(r => r.corp_name.includes(F.q));
  if (F.industry !== '전체') {
    const indStocks = new Set(
      Object.entries(window._finIndMap || {}).filter(([,ind]) => ind === F.industry).map(([code]) => code)
    );
    rows = rows.filter(r => indStocks.has(r.stock_code));
  }

  const sortCol = { 'market_cap': 'revenue', 'price': 'operating_profit' }[F.sortBy] || F.sortBy;
  rows.sort((a, b) => {
    const av = a[sortCol] ?? -Infinity;
    const bv = b[sortCol] ?? -Infinity;
    return F.sortDir === 'desc' ? bv - av : av - bv;
  });

  _finData = rows;
  const cnt = document.getElementById('fin-count');
  if (cnt) cnt.textContent = `${rows.length}개`;

  const fmt = v => fmtCap(v);  // fmtCap: 조/억 단위 자동 변환
  const pct = v => v != null ? v.toFixed(1) + '%' : '—';

  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>종목명</th><th>기간</th><th>매출액</th><th>영업이익</th>
      <th>순이익</th><th>영업이익률</th><th>ROE</th><th>ROA</th>
      <th>부채비율</th><th>자산총계</th>
    </tr></thead>
    <tbody>${rows.map(r => {
      const opColor = r.operating_profit > 0 ? 'var(--green)' : r.operating_profit < 0 ? 'var(--red)' : 'var(--text2)';
      return `<tr>
        <td style="font-weight:500;cursor:pointer;color:var(--tg)" onclick="openFinTrend('${r.stock_code}','${r.corp_name}')">${r.corp_name}</td>
        <td style="font-size:12px;color:var(--text2)">${r.bsns_year} ${r.quarter}</td>
        <td>${fmt(r.revenue)}</td>
        <td style="color:${opColor}">${fmt(r.operating_profit)}</td>
        <td>${fmt(r.net_income)}</td>
        <td>${pct(r.operating_margin)}</td>
        <td>${pct(r.roe)}</td>
        <td>${pct(r.roa)}</td>
        <td>${pct(r.debt_ratio)}</td>
        <td>${fmt(r.total_assets)}</td>
      </tr>`;
    }).join('')}
    </tbody></table></div>`;
}

async function loadCombinedData(el) {
  // 시장 + 재무 병합 (병렬 조회)
  const [allM, allF] = await Promise.all([
    fetchAllPages(
      sb.from('market_data')
        .select('stock_code,corp_name,market_cap,price,price_change_rate,per,pbr')
        .order('base_date', { ascending: false })
    ),
    fetchAllPages(
      sb.from('financials')
        .select('stock_code,revenue,operating_profit,net_income,operating_margin,roe,debt_ratio,bsns_year,quarter')
        .order('bsns_year', { ascending: false })
        .order('quarter', { ascending: false })
    ),
  ]);
  const mktRes = { data: allM };
  const finRes = { data: allF };

  const mktMap = {};
  (mktRes.data || []).forEach(r => { if (!mktMap[r.stock_code]) mktMap[r.stock_code] = r; });
  const finMap = {};
  (finRes.data || []).forEach(r => {
    const cur = finMap[r.stock_code];
    if (!cur || r.bsns_year > cur.bsns_year ||
       (r.bsns_year === cur.bsns_year && r.quarter > cur.quarter)) {
      finMap[r.stock_code] = r;
    }
  });

  const allCodes = new Set([...Object.keys(mktMap), ...Object.keys(finMap)]);
  let rows = [...allCodes].map(code => ({
    stock_code: code,
    corp_name: (mktMap[code] || finMap[code])?.corp_name || code,
    ...mktMap[code],
    ...finMap[code],
  }));

  if (F.q) rows = rows.filter(r => r.corp_name.includes(F.q));
  if (F.industry !== '전체') {
    const indStocks = new Set(
      Object.entries(window._finIndMap || {}).filter(([,ind]) => ind === F.industry).map(([code]) => code)
    );
    rows = rows.filter(r => indStocks.has(r.stock_code));
  }

  rows.sort((a,b) => ((b.market_cap||0) - (a.market_cap||0)));
  _finData = rows;
  const cnt = document.getElementById('fin-count');
  if (cnt) cnt.textContent = `${rows.length}개`;

  const fmt = v => fmtCap(v);  // fmtCap: 조/억 단위 자동 변환
  const pct = v => v != null ? v.toFixed(1)+'%' : '—';

  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>종목명</th><th>시가총액</th><th>현재가</th><th>등락률</th>
      <th>PER</th><th>PBR</th><th>매출액</th><th>영업이익</th>
      <th>영업이익률</th><th>ROE</th><th>기간</th>
    </tr></thead>
    <tbody>${rows.map(r => {
      const chg = r.price_change_rate;
      const chgColor = chg > 0 ? 'var(--red)' : chg < 0 ? '#4a9eff' : 'var(--text3)';
      return `<tr>
        <td style="font-weight:500">${r.corp_name}</td>
        <td>${fmtCap(r.market_cap)}</td>
        <td>${r.price?r.price.toLocaleString()+'원':'—'}</td>
        <td style="color:${chgColor};font-weight:500">${chg!=null?(chg>0?'+':'')+chg.toFixed(2)+'%':'—'}</td>
        <td>${r.per!=null?r.per.toFixed(1):'—'}</td>
        <td>${r.pbr!=null?r.pbr.toFixed(2):'—'}</td>
        <td>${fmt(r.revenue)}</td>
        <td>${fmt(r.operating_profit)}</td>
        <td>${pct(r.operating_margin)}</td>
        <td>${pct(r.roe)}</td>
        <td style="font-size:11px;color:var(--text3)">${r.bsns_year?r.bsns_year+' '+r.quarter:'—'}</td>
      </tr>`;
    }).join('')}
    </tbody></table></div>`;
}

async function openFinTrend(stockCode, corpName) {
  const existing = document.getElementById('m-fin-trend');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'm-fin-trend';
  overlay.className = 'modal-overlay open';
  overlay.innerHTML = `
    <div class="modal" style="width:860px;max-width:96vw;max-height:90vh;overflow-y:auto">
      <div class="modal-header">
        <span class="modal-title">${corpName} — 분기별 재무 추이</span>
        <button class="modal-close" onclick="document.getElementById('m-fin-trend').remove()">×</button>
      </div>
      <div id="fin-trend-body" style="padding:1rem">
        ${loadingHTML('로딩 중...')}
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  // 데이터 조회 (오래된→최신 순으로 정렬해야 차트가 자연스러움)
  const { data: rawData, error } = await sb.from('financials')
    .select('bsns_year,quarter,fs_div,revenue,operating_profit,net_income,operating_margin,roe,roa,debt_ratio,total_assets,total_equity,total_liabilities,operating_cashflow,revenue_yoy,revenue_qoq,op_profit_yoy,op_profit_qoq')
    .eq('stock_code', stockCode)
    .order('bsns_year', { ascending: true })
    .order('quarter', { ascending: true })
    .limit(20);

  const body = document.getElementById('fin-trend-body');
  if (error || !rawData?.length) {
    body.innerHTML = emptyHTML();
    return;
  }

  // 최신순으로 뒤집어서 테이블에 표시
  const data = [...rawData].reverse();
  const fmt = v => fmtCap(v);
  const pct = v => v != null ? v.toFixed(1) + '%' : '—';
  const chgBadge = v => {
    if (v == null) return '';
    const color = v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text3)';
    return `<span style="font-size:10px;color:${color};margin-left:4px">${v > 0 ? '▲' : '▼'}${Math.abs(v).toFixed(1)}%</span>`;
  };

  // 차트 데이터 (오래된→최신)
  const labels   = rawData.map(r => `${r.bsns_year} ${r.quarter}`);
  const revData  = rawData.map(r => r.revenue        ? Math.round(r.revenue        / 1e8) : null);
  const opData   = rawData.map(r => r.operating_profit ? Math.round(r.operating_profit / 1e8) : null);
  const netData  = rawData.map(r => r.net_income      ? Math.round(r.net_income      / 1e8) : null);
  const marginData = rawData.map(r => r.operating_margin);

  const isDark = document.documentElement.classList.contains('dark') ||
    window.matchMedia('(prefers-color-scheme: dark)').matches;
  const textColor  = isDark ? '#b0b0b0' : '#666';
  const gridColor  = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';

  body.innerHTML = `
    <div style="margin-bottom:1rem">
      <div style="display:flex;gap:8px;margin-bottom:.5rem">
        <button class="btn btn-sm active" id="chart-tab-income" onclick="switchFinChart('income')">손익</button>
        <button class="btn btn-sm" id="chart-tab-balance" onclick="switchFinChart('balance')">재무상태</button>
        <button class="btn btn-sm" id="chart-tab-margin" onclick="switchFinChart('margin')">이익률/ROE</button>
      </div>
      <div style="position:relative;height:220px">
        <canvas id="fin-chart"></canvas>
      </div>
    </div>

    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:.5rem">손익계산서</div>
    <div class="table-wrap" style="margin-bottom:1.25rem"><table>
      <thead><tr>
        <th>기간</th><th>매출액</th><th>YoY</th><th>QoQ</th><th>영업이익</th><th>영업이익률</th><th>순이익</th><th>ROE</th>
      </tr></thead>
      <tbody>${data.map(r => {
        const opColor = r.operating_profit > 0 ? 'var(--green)' : r.operating_profit < 0 ? 'var(--red)' : 'var(--text2)';
        return `<tr>
          <td style="font-weight:600">${r.bsns_year} ${r.quarter}</td>
          <td>${fmt(r.revenue)}</td>
          <td style="font-size:11px">${r.revenue_yoy != null ? chgBadge(r.revenue_yoy) : '—'}</td>
          <td style="font-size:11px">${r.revenue_qoq != null ? chgBadge(r.revenue_qoq) : '—'}</td>
          <td style="color:${opColor}">${fmt(r.operating_profit)}</td>
          <td>${pct(r.operating_margin)}</td>
          <td>${fmt(r.net_income)}</td>
          <td>${pct(r.roe)}</td>
        </tr>`;
      }).join('')}
      </tbody>
    </table></div>

    <div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:.5rem">재무상태표</div>
    <div class="table-wrap" style="margin-bottom:.75rem"><table>
      <thead><tr>
        <th>기간</th><th>자산총계</th><th>부채총계</th><th>자본총계</th><th>부채비율</th><th>영업현금흐름</th>
      </tr></thead>
      <tbody>${data.map(r => `<tr>
        <td style="font-weight:600">${r.bsns_year} ${r.quarter}</td>
        <td>${fmt(r.total_assets)}</td>
        <td>${fmt(r.total_liabilities)}</td>
        <td>${fmt(r.total_equity)}</td>
        <td>${pct(r.debt_ratio)}</td>
        <td style="color:${r.operating_cashflow > 0 ? 'var(--green)' : 'var(--red)'}">${fmt(r.operating_cashflow)}</td>
      </tr>`).join('')}
      </tbody>
    </table></div>
    <div style="font-size:11px;color:var(--text3)">${rawData[0].fs_div === 'CFS' ? '연결' : '별도'} 재무제표 기준</div>`;

  // Chart.js 로드 후 차트 생성
  const initChart = () => {
    const ctx = document.getElementById('fin-chart')?.getContext('2d');
    if (!ctx || typeof Chart === 'undefined') return;

    window._finChartData = { labels, revData, opData, netData, marginData, textColor, gridColor };
    window._finChartInst = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: '매출액(억)', data: revData, backgroundColor: 'rgba(55,138,221,0.6)', yAxisID: 'y' },
          { label: '영업이익(억)', data: opData, backgroundColor: 'rgba(45,206,137,0.7)', yAxisID: 'y' },
          { label: '순이익(억)', data: netData, backgroundColor: 'rgba(255,193,7,0.6)', yAxisID: 'y' },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: textColor, font: { size: 11 } } } },
        scales: {
          x: { ticks: { color: textColor, font: { size: 10 } }, grid: { color: gridColor } },
          y: { ticks: { color: textColor, font: { size: 10 } }, grid: { color: gridColor } }
        }
      }
    });
  };

  if (typeof Chart !== 'undefined') {
    initChart();
  } else {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
    s.onload = initChart;
    document.head.appendChild(s);
  }
}

function switchFinChart(tab) {
  const inst = window._finChartInst;
  const d = window._finChartData;
  if (!inst || !d) return;

  document.querySelectorAll('[id^=chart-tab-]').forEach(b => b.classList.remove('active'));
  document.getElementById('chart-tab-' + tab)?.classList.add('active');

  if (tab === 'income') {
    inst.data.datasets = [
      { label: '매출액(억)', data: d.revData, backgroundColor: 'rgba(55,138,221,0.6)', yAxisID: 'y' },
      { label: '영업이익(억)', data: d.opData, backgroundColor: 'rgba(45,206,137,0.7)', yAxisID: 'y' },
      { label: '순이익(억)', data: d.netData, backgroundColor: 'rgba(255,193,7,0.6)', yAxisID: 'y' },
    ];
    inst.options.scales = {
      x: { ticks: { color: d.textColor, font: { size: 10 } }, grid: { color: d.gridColor } },
      y: { ticks: { color: d.textColor, font: { size: 10 } }, grid: { color: d.gridColor } }
    };
  } else if (tab === 'balance') {
    inst.data.datasets = [
      { label: '자산(억)', data: d.revData.map((_, i) => window._finRawData?.[i]?.total_assets ? Math.round(window._finRawData[i].total_assets/1e8) : null), backgroundColor: 'rgba(55,138,221,0.5)', yAxisID: 'y' },
      { label: '부채(억)', data: d.revData.map((_, i) => window._finRawData?.[i]?.total_liabilities ? Math.round(window._finRawData[i].total_liabilities/1e8) : null), backgroundColor: 'rgba(226,75,74,0.5)', yAxisID: 'y' },
      { label: '자본(억)', data: d.revData.map((_, i) => window._finRawData?.[i]?.total_equity ? Math.round(window._finRawData[i].total_equity/1e8) : null), backgroundColor: 'rgba(45,206,137,0.5)', yAxisID: 'y' },
    ];
  } else if (tab === 'margin') {
    inst.data.datasets = [
      { label: '영업이익률(%)', data: d.marginData, backgroundColor: 'rgba(45,206,137,0.7)', type: 'line', borderColor: 'rgba(45,206,137,1)', tension: 0.3, yAxisID: 'y' },
      { label: 'ROE(%)', data: d.revData.map((_, i) => window._finRawData?.[i]?.roe), backgroundColor: 'rgba(255,193,7,0.6)', type: 'line', borderColor: 'rgba(255,193,7,1)', tension: 0.3, yAxisID: 'y' },
    ];
  }
  inst.update();
}

function exportFinancials() {
  if (!_finData.length) { toast('데이터가 없습니다.', 'error'); return; }
  const keys = Object.keys(_finData[0]);
  const csv = [keys.join(','), ..._finData.map(r =>
    keys.map(k => {
      const v = r[k];
      return v == null ? '' : typeof v === 'string' && v.includes(',') ? `"${v}"` : v;
    }).join(',')
  )].join('\n');
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `financials_${new Date().toISOString().slice(0,10)}.csv`;
  a.click(); URL.revokeObjectURL(url);
  toast('CSV 다운로드 완료', 'success');
}

