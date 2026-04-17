// financials.js — 재무 조회 (시장/재무제표/종합)

function fmtCap(won) {
  if (won == null || won === 0) return '—';
  const jo  = Math.floor(won / 1e12);
  const eok = Math.floor((won % 1e12) / 1e8);
  if (jo > 0 && eok > 0) return jo + '조 ' + eok.toLocaleString() + '억';
  if (jo > 0)             return jo + '조';
  return eok.toLocaleString() + '억';
}

const F = { mode: 'market', industry: '전체', q: '', sortBy: 'market_cap', sortDir: 'desc' };

function pFinancials() {
  const industries = ['전체','바이오','반도체','2차전지','로봇','뷰티','테크','조선','신재생','엔터','소비재','우주'];
  return `
  <div class="tabs" style="margin-bottom:.75rem">
    <button class="tab fin-tab ${F.mode==='market'?'active':''}" data-mode="market" onclick="F.mode='market';loadFinancials()">시장 데이터</button>
    <button class="tab fin-tab ${F.mode==='financial'?'active':''}" data-mode="financial" onclick="F.mode='financial';loadFinancials()">재무제표</button>
    <button class="tab fin-tab ${F.mode==='combined'?'active':''}" data-mode="combined" onclick="F.mode='combined';loadFinancials()">종합</button>
  </div>

  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:1rem">
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
    <div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>
  </div>`;
}

let _finData = [];

async function loadFinancials() {
  const el = document.getElementById('fin-table');
  if (!el) return;

  // 탭 active 상태 업데이트
  document.querySelectorAll('.fin-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.mode === F.mode);
  });

  el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>';

  try {
    if (F.mode === 'market') {
      await loadMarketData(el);
    } else if (F.mode === 'financial') {
      await loadFinancialData(el);
    } else {
      await loadCombinedData(el);
    }
  } catch(e) {
    el.innerHTML = `<div style="padding:1rem;color:var(--red);font-size:13px">${e.message}</div>`;
  }
}

async function loadMarketData(el) {
  // 오늘 날짜 기준 최신 데이터
  const { data, error } = await sb.from('market_data')
    .select('*')
    .order('base_date', { ascending: false })
    .limit(2000);
  if (error) throw error;

  // 종목당 최신 1개만 (base_date 기준)
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
      (_allStocks || []).filter(s => s.industry === F.industry).map(s => s.code?.split('.')[0])
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
    el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:13px">데이터 없음</div>';
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
  const { data, error } = await sb.from('financials')
    .select('*')
    .order('bsns_year', { ascending: false })
    .order('quarter', { ascending: false })
    .limit(5000);
  if (error) throw error;

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
      (_allStocks || []).filter(s => s.industry === F.industry).map(s => s.code?.split('.')[0])
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

  const fmt = v => v != null ? (v / 1e8).toFixed(0) + '억' : '—';
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
        <td style="font-weight:500">${r.corp_name}</td>
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
  // 시장 + 재무 병합
  const [mktRes, finRes] = await Promise.all([
    sb.from('market_data').select('stock_code,corp_name,market_cap,price,price_change_rate,per,pbr').order('base_date',{ascending:false}).limit(2000),
    sb.from('financials').select('stock_code,revenue,operating_profit,net_income,operating_margin,roe,debt_ratio,bsns_year,quarter').order('bsns_year',{ascending:false}).order('quarter',{ascending:false}).limit(5000),
  ]);

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
      (_allStocks || []).filter(s => s.industry === F.industry).map(s => s.code?.split('.')[0])
    );
    rows = rows.filter(r => indStocks.has(r.stock_code));
  }

  rows.sort((a,b) => ((b.market_cap||0) - (a.market_cap||0)));
  _finData = rows;
  const cnt = document.getElementById('fin-count');
  if (cnt) cnt.textContent = `${rows.length}개`;

  const fmt = v => v != null ? (v/1e8).toFixed(0)+'억' : '—';
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

