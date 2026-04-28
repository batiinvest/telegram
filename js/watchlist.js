
function fmtCapEok(eok) {
  // 억 단위 입력값을 보기 쉽게 변환
  if (eok == null || isNaN(eok)) return '—';
  if (eok >= 10000) {
    const jo = Math.floor(eok / 10000);
    const rem = Math.round(eok % 10000);
    return rem > 0 ? `${jo}조 ${rem.toLocaleString()}억` : `${jo}조`;
  }
  return `${eok.toLocaleString()}억`;
}

function fmtPriceKr(price) {
  // 원 단위 주가를 보기 쉽게
  if (price == null || isNaN(price)) return '—';
  if (price >= 100000000) return `${(price/100000000).toFixed(2)}억원`;
  if (price >= 10000) return `${price.toLocaleString()}원`;
  return `${price.toLocaleString()}원`;
}

function syncWlPrice(from, val) {
  const shares = window._wlShares;
  const hint = document.getElementById('wl-target-cap-hint');
  if (!shares || !val) { if (hint) hint.textContent = ''; return; }

  if (from === 'price') {
    const price = parseFloat(val);
    if (!isNaN(price)) {
      const capEok = Math.round(price * shares / 1e8);
      document.getElementById('wl-target_cap').value = capEok;
      if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtCapEok(capEok)}</span>`;
    }
  } else {
    const capEok = parseFloat(val);
    if (!isNaN(capEok)) {
      const price = Math.round(capEok * 1e8 / shares);
      document.getElementById('wl-target_price').value = price;
      if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtPriceKr(price)}</span>`;
    }
  }
}

function syncWlWatchPrice(from, val) {
  const shares = window._wlShares;
  const hint = document.getElementById('wl-watch-cap-hint');
  if (!shares || !val) { if (hint) hint.textContent = ''; return; }

  if (from === 'price') {
    const price = parseFloat(val);
    if (!isNaN(price)) {
      const capEok = Math.round(price * shares / 1e8);
      document.getElementById('wl-watch_cap').value = capEok;
      if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtCapEok(capEok)}</span>`;
    }
  } else {
    const capEok = parseFloat(val);
    if (!isNaN(capEok)) {
      const price = Math.round(capEok * 1e8 / shares);
      document.getElementById('wl-watch_price').value = price;
      if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtPriceKr(price)}</span>`;
    }
  }
}

let _wlCompanies = null;

async function searchWatchlistStock(query) {
  const dd = document.getElementById('wl-search-dropdown');
  if (!query || query.length < 1) { dd.style.display = 'none'; return; }

  // 전체 companies 캐시 로드 (최초 1회)
  if (!_wlCompanies) {
    let all = [], page = 0;
    while (true) {
      const { data } = await sb.from('companies')
        .select('code,name,industry,sub_industry,market')
        .eq('active', true)
        .range(page*1000, (page+1)*1000-1);
      if (!data?.length) break;
      all = all.concat(data);
      if (data.length < 1000) break;
      page++;
    }
    _wlCompanies = all;
  }

  const q = query.toLowerCase();
  const results = _wlCompanies.filter(c =>
    c.name?.toLowerCase().includes(q) ||
    (c.code || '').replace('.KS','').replace('.KQ','').includes(q)
  ).slice(0, 10);

  if (!results.length) { dd.style.display = 'none'; return; }

  dd.style.display = 'block';
  dd.innerHTML = results.map(c => {
    const code = (c.code||'').split('.')[0];
    return `<div onclick="selectWatchlistStock('${code}','${c.name}','${c.industry||''}','${c.market||''}')"
      style="padding:8px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px"
      onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background=''">
      <span style="font-weight:500">${c.name}</span>
      <span style="font-size:11px;color:var(--text3)">${code}</span>
      ${c.industry ? `<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:var(--bg3);color:var(--text3)">${c.industry}</span>` : ''}
      <span style="font-size:10px;color:var(--text3);margin-left:auto">${c.market||''}</span>
    </div>`;
  }).join('');
}

async function selectWatchlistStock(code, name, industry, market) {
  // 기본 정보 입력
  document.getElementById('wl-search').value = name;
  document.getElementById('wl-stock_code').value = code;
  document.getElementById('wl-corp_name').value = name;
  document.getElementById('wl-industry').value = industry;
  document.getElementById('wl-search-dropdown').style.display = 'none';

  // 시장 데이터 자동 입력
  const { data: mkt } = await sb.from('market_data')
    .select('price,price_change_rate,market_cap,per,pbr')
    .eq('stock_code', code)
    .order('base_date', { ascending: false })
    .limit(1);

  if (mkt?.[0]) {
    const m = mkt[0];
    const cc = m.price_change_rate > 0 ? 'var(--red)' : m.price_change_rate < 0 ? 'var(--blue)' : 'var(--text2)';
    document.getElementById('wl-auto-price').textContent = m.price ? m.price.toLocaleString()+'원' : '—';
    document.getElementById('wl-auto-chg').innerHTML = m.price_change_rate != null
      ? `<span style="color:${cc}">${m.price_change_rate>0?'+':''}${m.price_change_rate.toFixed(2)}%</span>` : '—';
    document.getElementById('wl-auto-cap').textContent = m.market_cap ? fmtCap(m.market_cap) : '—';
    document.getElementById('wl-auto-per').textContent = m.per ? m.per.toFixed(1) : '—';
    document.getElementById('wl-auto-pbr').textContent = m.pbr ? m.pbr.toFixed(2) : '—';

    // 주식수 = 시총 / 현재가 (계산용 저장)
    if (m.market_cap && m.price) {
      window._wlShares = Math.round(m.market_cap / m.price);
      document.getElementById('wl-shares-hint').textContent =
        `발행주식수 약 ${Math.round(window._wlShares/10000).toLocaleString()}만주 기준`;
    } else {
      window._wlShares = null;
      document.getElementById('wl-shares-hint').textContent = '';
    }
  }

  // 재무 데이터 자동 입력 (업계 평균 PER 참고용)
  const { data: fin } = await sb.from('financials')
    .select('operating_margin,roe,debt_ratio')
    .eq('stock_code', code)
    .order('bsns_year', { ascending: false })
    .order('quarter', { ascending: false })
    .limit(1);

  if (fin?.[0]) {
    const f = fin[0];
    // 밸류에이션 메모에 기본값 제안
    const memo = document.getElementById('wl-valuation_note');
    if (memo && !memo.value) {
      const hints = [];
      if (f.operating_margin != null) hints.push(`영업이익률 ${f.operating_margin.toFixed(1)}%`);
      if (f.roe != null) hints.push(`ROE ${f.roe.toFixed(1)}%`);
      if (f.debt_ratio != null) hints.push(`부채비율 ${f.debt_ratio.toFixed(1)}%`);
      if (hints.length) memo.placeholder = `최근 재무: ${hints.join(', ')}`;
    }
  }
}

// 드롭다운 외부 클릭 시 닫기
document.addEventListener('click', e => {
  const dd = document.getElementById('wl-search-dropdown');
  if (dd && !dd.contains(e.target) && e.target.id !== 'wl-search') {
    dd.style.display = 'none';
  }
});


// =============================================
//  관심종목 (Watchlist) 페이지
// =============================================

function pWatchlist() {
  return `
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
    <div style="display:flex;gap:8px;align-items:center">
      <select class="form-select" id="wl-group" onchange="loadWatchlist()" style="width:120px;padding:6px 10px">
        <option value="all">전체</option>
        <option value="관심">관심</option>
        <option value="후보">후보</option>
        <option value="보유중">보유중</option>
      </select>
      <span id="wl-count" style="font-size:12px;color:var(--text3)"></span>
    </div>
    <button class="btn btn-primary" onclick="openWatchlistModal(null)">+ 종목 추가</button>
  </div>
  <div id="wl-list"></div>`;
}

async function loadWatchlist() {
  const group  = document.getElementById('wl-group')?.value || 'all';
  const listEl = document.getElementById('wl-list');
  if (!listEl) return;
  listEl.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text3)"><span class="loading"></span></div>';

  let q = sb.from('watchlist').select('*').order('created_at', { ascending: false });
  if (group !== 'all') q = q.eq('group_name', group);
  const { data, error } = await q;
  if (error) { listEl.innerHTML = '<div style="color:var(--red);padding:1rem">로드 실패</div>'; return; }

  // 현재가 일괄 조회
  const codes = (data || []).map(r => r.stock_code);
  let priceMap = {};
  if (codes.length) {
    const { data: mkt } = await sb.from('market_data')
      .select('stock_code,price,price_change_rate,per,pbr,market_cap')
      .in('stock_code', codes)
      .order('base_date', { ascending: false });
    (mkt || []).forEach(r => { if (!priceMap[r.stock_code]) priceMap[r.stock_code] = r; });
  }

  document.getElementById('wl-count').textContent = `${(data||[]).length}개`;

  const groupColors = { '관심': '#4a9eff', '후보': '#ffc107', '보유중': 'var(--green)' };

  if (!data?.length) {
    listEl.innerHTML = '<div style="text-align:center;padding:3rem;color:var(--text3)">등록된 관심종목이 없어요.<br>+ 종목 추가 버튼을 눌러 추가해주세요.</div>';
    return;
  }

  listEl.innerHTML = data.map(w => {
    const mkt     = priceMap[w.stock_code] || {};
    const price   = mkt.price;
    const chg     = mkt.price_change_rate;
    const cap     = mkt.market_cap;
    const chgColor = chg > 0 ? 'var(--red)' : chg < 0 ? 'var(--blue)' : 'var(--text3)';

    // 목표가 괴리율
    const gapTarget = (w.target_price && price) ? ((w.target_price - price) / price * 100) : null;
    const gapWatch  = (w.watch_price  && price) ? ((price - w.watch_price)  / price * 100) : null;

    // 보유중 평가손익
    const evalProfit = (w.avg_price && w.quantity && price)
      ? (price - w.avg_price) * w.quantity : null;
    const evalRate   = (w.avg_price && price)
      ? ((price - w.avg_price) / w.avg_price * 100) : null;

    // 다음 확인 D-day
    const dday = w.next_check_date ? Math.ceil((new Date(w.next_check_date) - new Date()) / 86400000) : null;
    const ddayStr = dday != null
      ? (dday === 0 ? 'D-Day' : dday > 0 ? `D-${dday}` : `D+${Math.abs(dday)}`)
      : '';
    const ddayColor = dday != null && dday <= 3 ? 'var(--red)' : 'var(--text3)';

    return `
    <div class="card" style="margin-bottom:.75rem">
      <div style="padding:1rem">

        <!-- 헤더 -->
        <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:.875rem">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <span style="font-size:15px;font-weight:700">${w.corp_name}</span>
              <span style="font-size:11px;color:var(--text3)">${w.stock_code}</span>
              <span style="font-size:11px;padding:2px 8px;border-radius:100px;background:${groupColors[w.group_name]||'#888'}22;color:${groupColors[w.group_name]||'#888'}">${w.group_name}</span>
              ${w.industry ? `<span class="badge badge-cat">${w.industry}</span>` : ''}
            </div>
            ${w.catalyst ? `<div style="font-size:11px;color:var(--tg);margin-top:3px">⚡ ${w.catalyst}</div>` : ''}
          </div>

          <!-- 현재가 -->
          <div style="text-align:right">
            <div style="font-size:16px;font-weight:700">${price ? price.toLocaleString()+'원' : '—'}</div>
            <div style="font-size:12px;color:${chgColor}">${chg != null ? (chg>0?'+':'')+chg.toFixed(2)+'%' : ''}</div>
            ${cap ? `<div style="font-size:11px;color:var(--text3)">${fmtCap(cap)}</div>` : ''}
          </div>

          <!-- 액션 버튼 -->
          <div style="display:flex;gap:6px;margin-left:4px">
            <button class="btn btn-sm" onclick="openWatchlistModal(${w.id})">수정</button>
            <button class="btn btn-sm" style="color:var(--red)" onclick="deleteWatchlist(${w.id},'${w.corp_name}')">삭제</button>
          </div>
        </div>

        <!-- 가격 기준 -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:.875rem">
          ${w.target_price ? `
          <div style="background:var(--bg2);border-radius:8px;padding:8px 10px">
            <div style="font-size:10px;color:var(--text3);margin-bottom:2px">적정가</div>
            <div style="font-size:13px;font-weight:600">${w.target_price.toLocaleString()}원</div>
            ${(cap && w.target_price) ? `<div style="font-size:10px;color:var(--text3)">시총 ≈ ${fmtCap(w.target_price * Math.round(cap / (price||1)))}</div>` : ''}
            ${gapTarget != null ? `<div style="font-size:11px;color:${gapTarget>0?'var(--green)':'var(--red)'}">${gapTarget>0?'▲':'▼'} ${Math.abs(gapTarget).toFixed(1)}% ${gapTarget>0?'상승여력':'하락위험'}</div>` : ''}
          </div>` : ''}
          ${w.watch_price ? `
          <div style="background:var(--bg2);border-radius:8px;padding:8px 10px">
            <div style="font-size:10px;color:var(--text3);margin-bottom:2px">관심가격</div>
            <div style="font-size:13px;font-weight:600">${w.watch_price.toLocaleString()}원</div>
            ${(cap && w.watch_price) ? `<div style="font-size:10px;color:var(--text3)">시총 ≈ ${fmtCap(w.watch_price * Math.round(cap / (price||1)))}</div>` : ''}
            ${gapWatch != null ? `<div style="font-size:11px;color:${gapWatch<0?'var(--green)':'var(--text3)'}">${gapWatch<0?'✅ 관심가 도달':'현재가 -'+Math.abs(gapWatch).toFixed(1)+'%'}</div>` : ''}
          </div>` : ''}
          ${w.avg_price ? `
          <div style="background:var(--bg2);border-radius:8px;padding:8px 10px">
            <div style="font-size:10px;color:var(--text3);margin-bottom:2px">평균매수가</div>
            <div style="font-size:13px;font-weight:600">${w.avg_price.toLocaleString()}원 ${w.quantity?'× '+w.quantity.toLocaleString()+'주':''}</div>
            ${evalRate != null ? `<div style="font-size:11px;color:${evalRate>0?'var(--green)':'var(--red)'}">${evalRate>0?'+':''}${evalRate.toFixed(2)}% ${evalProfit!=null?'('+fmtCap(evalProfit*100)+')':''}</div>` : ''}
          </div>` : ''}
          ${w.per ? `
          <div style="background:var(--bg2);border-radius:8px;padding:8px 10px">
            <div style="font-size:10px;color:var(--text3);margin-bottom:2px">PER / 업계평균</div>
            <div style="font-size:13px;font-weight:600">${mkt.per?.toFixed(1)||'—'} / ${w.peer_per?.toFixed(1)||'—'}</div>
            ${(mkt.per && w.peer_per) ? `<div style="font-size:11px;color:${mkt.per<w.peer_per?'var(--green)':'var(--red)'}">${mkt.per<w.peer_per?'업계 저평가':'업계 고평가'}</div>` : ''}
          </div>` : ''}
        </div>

        <!-- 투자포인트 / 리스크 / 붕괴조건 -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:.75rem">
          <div>
            ${[w.thesis_1, w.thesis_2, w.thesis_3].filter(Boolean).length ? `
            <div style="font-size:11px;font-weight:600;color:var(--text2);margin-bottom:5px">💡 투자포인트</div>
            ${[w.thesis_1, w.thesis_2, w.thesis_3].filter(Boolean).map((t,i)=>`
              <div style="font-size:12px;color:var(--text1);padding:3px 0;display:flex;gap:6px">
                <span style="color:var(--green);font-weight:600;flex-shrink:0">${i+1}.</span><span>${t}</span>
              </div>`).join('')}` : ''}
          </div>
          <div>
            ${[w.risk_1, w.risk_2, w.risk_3].filter(Boolean).length ? `
            <div style="font-size:11px;font-weight:600;color:var(--text2);margin-bottom:5px">⚠️ 리스크</div>
            ${[w.risk_1, w.risk_2, w.risk_3].filter(Boolean).map(r=>`
              <div style="font-size:12px;color:var(--text1);padding:3px 0;display:flex;gap:6px">
                <span style="color:var(--red);flex-shrink:0">•</span><span>${r}</span>
              </div>`).join('')}` : ''}
          </div>
        </div>

        <!-- 논리 붕괴 조건 -->
        ${w.break_condition ? `
        <div style="background:rgba(226,75,74,.08);border:1px solid rgba(226,75,74,.2);border-radius:8px;padding:8px 12px;margin-bottom:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--red);margin-bottom:3px">🚫 논리 붕괴 조건</div>
          <div style="font-size:12px;color:var(--text1)">${w.break_condition}</div>
        </div>` : ''}

        <!-- 밸류에이션 메모 -->
        ${w.valuation_note ? `
        <div style="background:var(--bg2);border-radius:8px;padding:8px 12px;margin-bottom:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:3px">📊 밸류에이션 근거</div>
          <div style="font-size:12px;color:var(--text2)">${w.valuation_note}</div>
        </div>` : ''}

        <!-- 하단: 다음 확인 / 경쟁사 -->
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          ${w.next_check_date ? `
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-size:11px;color:var(--text3)">📅 ${w.next_check_date}</span>
            <span style="font-size:11px;font-weight:600;color:${ddayColor}">${ddayStr}</span>
            ${w.next_check_memo ? `<span style="font-size:11px;color:var(--text3)">— ${w.next_check_memo}</span>` : ''}
          </div>` : ''}
          ${w.competitor ? `<span style="font-size:11px;color:var(--text3)">🏢 경쟁사: ${w.competitor}</span>` : ''}
        </div>

      </div>
    </div>`;
  }).join('');
}

async function deleteWatchlist(id, name) {
  if (!confirm(`${name}을 관심종목에서 삭제할까요?`)) return;
  await sb.from('watchlist').delete().eq('id', id);
  loadWatchlist();
}

function openWatchlistModal(id) {
  const existing = document.getElementById('m-watchlist');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'm-watchlist';
  overlay.className = 'modal-overlay open';
  overlay.innerHTML = `
    <div class="modal" style="width:720px;max-width:96vw;max-height:90vh;overflow-y:auto">
      <div class="modal-header">
        <span class="modal-title">${id ? '관심종목 수정' : '관심종목 추가'}</span>
        <button class="modal-close" onclick="document.getElementById('m-watchlist').remove()">×</button>
      </div>
      <div id="wl-modal-body" style="padding:1.25rem">
        <div style="text-align:center;color:var(--text3)"><span class="loading"></span></div>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  renderWatchlistForm(id);
}

async function renderWatchlistForm(id) {
  const body = document.getElementById('wl-modal-body');
  let w = {};
  if (id) {
    const { data } = await sb.from('watchlist').select('*').eq('id', id).single();
    w = data || {};
  }

  const inp = (field, label, placeholder='', type='text', readonly=false) => `
    <div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px">${label}</div>
      <input type="${type}" class="form-input" id="wl-${field}" value="${w[field]||''}"
        placeholder="${placeholder}" ${readonly?'readonly style="width:100%;box-sizing:border-box;opacity:0.7"':'style="width:100%;box-sizing:border-box"'}>
    </div>`;
  const ta = (field, label, placeholder='') => `
    <div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px">${label}</div>
      <textarea class="form-input" id="wl-${field}" placeholder="${placeholder}"
        style="width:100%;box-sizing:border-box;height:60px;resize:vertical">${w[field]||''}</textarea>
    </div>`;

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:1rem">

      <!-- 기본 정보 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">기본 정보</div>

      <!-- 종목 검색 -->
      <div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:4px">종목 검색</div>
        <div style="position:relative">
          <input type="text" class="form-input" id="wl-search"
            placeholder="종목명 또는 코드 입력..."
            value="${w.corp_name||''}"
            oninput="searchWatchlistStock(this.value)"
            style="width:100%;box-sizing:border-box">
          <div id="wl-search-dropdown" style="display:none;position:absolute;top:100%;left:0;right:0;background:var(--bg1);border:1px solid var(--border);border-radius:8px;z-index:999;max-height:200px;overflow-y:auto;box-shadow:0 4px 12px rgba(0,0,0,.3)"></div>
        </div>
      </div>

      <!-- 자동완성된 기본 정보 -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px">
        ${inp('stock_code','종목코드','자동입력','')}
        ${inp('corp_name','종목명','자동입력','')}
        ${inp('industry','산업','자동입력','')}
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">그룹</div>
          <select class="form-select" id="wl-group_name" style="width:100%">
            ${['관심','후보','보유중'].map(g=>`<option value="${g}" ${(w.group_name||'관심')===g?'selected':''}>${g}</option>`).join('')}
          </select>
        </div>
      </div>

      <!-- 시장 데이터 자동입력 (읽기전용) -->
      <div style="background:var(--bg2);border-radius:8px;padding:10px 12px">
        <div style="font-size:11px;color:var(--text3);margin-bottom:8px">📊 시장 데이터 (자동입력)</div>
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px">
          <div><div style="font-size:10px;color:var(--text3)">현재가</div><div id="wl-auto-price" style="font-size:12px;font-weight:600">—</div></div>
          <div><div style="font-size:10px;color:var(--text3)">등락률</div><div id="wl-auto-chg" style="font-size:12px;font-weight:600">—</div></div>
          <div><div style="font-size:10px;color:var(--text3)">시총</div><div id="wl-auto-cap" style="font-size:12px;font-weight:600">—</div></div>
          <div><div style="font-size:10px;color:var(--text3)">PER</div><div id="wl-auto-per" style="font-size:12px;font-weight:600">—</div></div>
          <div><div style="font-size:10px;color:var(--text3)">PBR</div><div id="wl-auto-pbr" style="font-size:12px;font-weight:600">—</div></div>
        </div>
      </div>

      <!-- 가격 기준 (시총↔주가 연동) -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">💰 가격 기준</div>
      <div id="wl-shares-hint" style="font-size:11px;color:var(--text3);margin-top:-6px"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px">
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">적정가 (원)</div>
          <input type="number" class="form-input" id="wl-target_price" value="${w.target_price||''}"
            placeholder="예: 280000"
            oninput="syncWlPrice('price', this.value)"
            style="width:100%;box-sizing:border-box">
          <div id="wl-target-cap-hint" style="font-size:10px;color:var(--text3);margin-top:2px"></div>
        </div>
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">적정 시총</div>
          <input type="number" class="form-input" id="wl-target_cap"
            placeholder="억원 단위"
            oninput="syncWlPrice('cap', this.value)"
            style="width:100%;box-sizing:border-box">
        </div>
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">관심가격 (원)</div>
          <input type="number" class="form-input" id="wl-watch_price" value="${w.watch_price||''}"
            placeholder="매수 고려 가격"
            oninput="syncWlWatchPrice('price', this.value)"
            style="width:100%;box-sizing:border-box">
          <div id="wl-watch-cap-hint" style="font-size:10px;color:var(--text3);margin-top:2px"></div>
        </div>
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">관심 시총</div>
          <input type="number" class="form-input" id="wl-watch_cap"
            placeholder="억원 단위"
            oninput="syncWlWatchPrice('cap', this.value)"
            style="width:100%;box-sizing:border-box">
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
        ${inp('avg_price','평균 매수가 (원)','','number')}
        ${inp('quantity','보유 수량 (주)','','number')}
        ${inp('peer_per','업계 평균 PER','','number')}
      </div>

      <!-- 밸류에이션 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">📊 밸류에이션</div>
      ${ta('valuation_note','밸류에이션 근거','예: DCF 기준 적정 시총 20조, 현재 15조로 25% 할인')}
      ${inp('competitor','경쟁사','예: 할로자임, 아비타스')}

      ${inp('catalyst','⚡ 주가 상승 트리거 (예정 이벤트)','예: 2025 Q2 FDA 임상 결과 발표')}

      <!-- 투자포인트 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">💡 핵심 투자포인트</div>
      ${ta('thesis_1','투자포인트 1 (필수)','가장 핵심적인 투자 근거')}
      ${ta('thesis_2','투자포인트 2','')}
      ${ta('thesis_3','투자포인트 3','')}

      <!-- 리스크 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">⚠️ 리스크</div>
      ${ta('risk_1','리스크 1 (필수)','가장 큰 하방 리스크')}
      ${ta('risk_2','리스크 2','')}
      ${ta('risk_3','리스크 3','')}

      <!-- 논리 붕괴 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">🚫 논리 붕괴 조건</div>
      ${ta('break_condition','이 조건이 충족되면 즉시 매도 검토','예: 로슈 기술이전 계약 해지 or 경쟁 플랫폼 FDA 승인')}



      <!-- 다음 확인 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">📅 다음 확인 일정</div>
      <div style="display:grid;grid-template-columns:160px 1fr;gap:10px">
        ${inp('next_check_date','날짜','','date')}
        ${inp('next_check_memo','확인할 내용','예: 2025 Q1 실적 발표 — 마일스톤 수령 여부 확인')}
      </div>

      <div style="display:flex;gap:8px;justify-content:flex-end;padding-top:.5rem">
        <button class="btn" onclick="document.getElementById('m-watchlist').remove()">취소</button>
        <button class="btn btn-primary" onclick="saveWatchlist(${id||'null'})">저장</button>
      </div>
    </div>`;

  // ── 기존 종목 수정 시: 시장 데이터 자동 로드 + 시총 초기값 복원 ──
  if (w.stock_code) {
    // 시장 데이터 로드 (selectWatchlistStock 재활용)
    await selectWatchlistStock(w.stock_code, w.corp_name, w.industry || '', '');

    // 적정 시총 / 관심 시총 초기값 복원 (저장된 가격 기반으로 계산)
    if (w.target_price && window._wlShares) {
      const capEok = Math.round(w.target_price * window._wlShares / 1e8);
      const el = document.getElementById('wl-target_cap');
      if (el) {
        el.value = capEok;
        const hint = document.getElementById('wl-target-cap-hint');
        if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtCapEok(capEok)}</span>`;
      }
    }
    if (w.watch_price && window._wlShares) {
      const capEok = Math.round(w.watch_price * window._wlShares / 1e8);
      const el = document.getElementById('wl-watch_cap');
      if (el) {
        el.value = capEok;
        const hint = document.getElementById('wl-watch-cap-hint');
        if (hint) hint.innerHTML = `<span style="color:var(--tg)">≈ ${fmtCapEok(capEok)}</span>`;
      }
    }
  }
}

async function saveWatchlist(id) {
  const g = field => document.getElementById('wl-' + field)?.value?.trim() || null;
  const n = field => { const v = g(field); return v ? parseFloat(v) : null; };
  const i = field => { const v = g(field); return v ? parseInt(v) : null; };

  const payload = {
    stock_code:      g('stock_code'),
    corp_name:       g('corp_name'),
    group_name:      g('group_name') || '관심',
    catalyst:        g('catalyst'),
    thesis_1:        g('thesis_1'),
    thesis_2:        g('thesis_2'),
    thesis_3:        g('thesis_3'),
    risk_1:          g('risk_1'),
    risk_2:          g('risk_2'),
    risk_3:          g('risk_3'),
    break_condition: g('break_condition'),
    target_price:    n('target_price'),
    watch_price:     n('watch_price'),
    avg_price:       n('avg_price'),
    quantity:        i('quantity'),
    valuation_note:  g('valuation_note'),
    competitor:      g('competitor'),
    peer_per:        n('peer_per'),
    next_check_date: g('next_check_date') || null,
    next_check_memo: g('next_check_memo'),
    updated_at:      new Date().toISOString(),
  };

  if (!payload.stock_code || !payload.corp_name) {
    alert('종목코드와 종목명은 필수입니다.');
    return;
  }

  if (id) {
    const { error } = await sb.from('watchlist').update(payload).eq('id', id);
    if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  } else {
    const { error } = await sb.from('watchlist').insert(payload);
    if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  }

  document.getElementById('m-watchlist').remove();
  toast('저장 완료', 'success');
  loadWatchlist();
}
