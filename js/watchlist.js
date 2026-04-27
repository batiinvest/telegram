
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
    const chgColor = chg > 0 ? 'var(--green)' : chg < 0 ? 'var(--red)' : 'var(--text3)';

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
            ${gapTarget != null ? `<div style="font-size:11px;color:${gapTarget>0?'var(--green)':'var(--red)'}">${gapTarget>0?'▲':'▼'} ${Math.abs(gapTarget).toFixed(1)}% ${gapTarget>0?'상승여력':'하락위험'}</div>` : ''}
          </div>` : ''}
          ${w.watch_price ? `
          <div style="background:var(--bg2);border-radius:8px;padding:8px 10px">
            <div style="font-size:10px;color:var(--text3);margin-bottom:2px">관심가격</div>
            <div style="font-size:13px;font-weight:600">${w.watch_price.toLocaleString()}원</div>
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

  const inp = (field, label, placeholder='', type='text') => `
    <div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px">${label}</div>
      <input type="${type}" class="form-input" id="wl-${field}" value="${w[field]||''}"
        placeholder="${placeholder}" style="width:100%;box-sizing:border-box">
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
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
        ${inp('stock_code','종목코드','005930')}
        ${inp('corp_name','종목명','삼성전자')}
        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:4px">그룹</div>
          <select class="form-select" id="wl-group_name" style="width:100%">
            ${['관심','후보','보유중'].map(g=>`<option value="${g}" ${w.group_name===g?'selected':''}>${g}</option>`).join('')}
          </select>
        </div>
      </div>
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

      <!-- 가격 기준 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">💰 가격 기준</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px">
        ${inp('target_price','적정가 (원)','280000','number')}
        ${inp('watch_price','관심가격 (원)','170000','number')}
        ${inp('avg_price','평균 매수가 (원)','','number')}
        ${inp('quantity','보유 수량 (주)','','number')}
      </div>

      <!-- 밸류에이션 -->
      <div style="font-size:12px;font-weight:600;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:6px">📊 밸류에이션</div>
      ${ta('valuation_note','밸류에이션 근거','예: DCF 기준 적정 시총 20조, 현재 15조로 25% 할인')}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        ${inp('competitor','경쟁사','예: 할로자임, 아비타스')}
        ${inp('peer_per','업계 평균 PER','','number')}
      </div>

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
    await sb.from('watchlist').update(payload).eq('id', id);
  } else {
    await sb.from('watchlist').insert(payload);
  }

  document.getElementById('m-watchlist').remove();
  loadWatchlist();
}
