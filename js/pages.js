// pages.js — 페이지 렌더링 함수들
function pOverview() {
  const isFull = r => r.status === 'full' || (r.members || 0) >= (r.max_members || 1000);

  // 기업/산업 채팅방 분리
  const companyRooms  = A.rooms.filter(r => r.room_type !== 'industry');
  const industryRooms = A.rooms.filter(r => r.room_type === 'industry');

  const total  = companyRooms.length;
  const full   = companyRooms.filter(isFull).length;
  const open   = companyRooms.filter(r => !isFull(r)).length;
  const totalM = companyRooms.reduce((s,r) => s + (r.members||0), 0);

  // 산업별 집계 (기업 채팅방 기준)
  const catMap = {};
  companyRooms.forEach(r => {
    if (!catMap[r.cat]) catMap[r.cat] = { n:0, m:0, industryM:0, industryLink:'' };
    catMap[r.cat].n++;
    catMap[r.cat].m += r.members || 0;
  });
  // 산업 채팅방 인원 매핑
  industryRooms.forEach(r => {
    if (catMap[r.cat]) {
      catMap[r.cat].industryM    = r.members || 0;
      catMap[r.cat].industryLink = r.link || '';
    }
  });

  const top5 = [...companyRooms].sort((a,b) => b.members - a.members).slice(0,5);
  return `
  <div class="metrics-grid">
    <div class="metric-card"><div class="metric-label">전체 채팅방</div><div class="metric-value">${total}</div><div class="metric-sub">Supabase DB 기준</div></div>
    <div class="metric-card"><div class="metric-label">전체 멤버</div><div class="metric-value">${totalM.toLocaleString()}</div><div class="metric-sub">동기화 기준</div></div>
    <div class="metric-card"><div class="metric-label">정원 마감</div><div class="metric-value" style="color:var(--red)">${full}</div><div class="metric-sub">${total?Math.round(full/total*100):0}%</div></div>
    <div class="metric-card"><div class="metric-label">입장 가능</div><div class="metric-value" style="color:var(--green)">${open}</div><div class="metric-sub">${total?Math.round(open/total*100):0}%</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
    <div class="card"><div class="card-header"><span class="card-title">산업별 현황</span></div><div class="card-body" style="padding:.75rem 1rem">
      ${Object.entries(catMap).sort((a,b)=>b[1].m-a[1].m).map(([cat,v])=>{
        const compList = companyRooms.filter(r=>r.cat===cat).sort((a,b)=>b.members-a.members);
        const catId = 'cat-' + cat.replace(/[^a-zA-Z0-9가-힣]/g,'');
        return `
        <div style="border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:10px;padding:9px 0;cursor:pointer"
               onclick="toggleCatDetail('${catId}')">
            <span class="cat-dot" style="background:${CATS[cat]||'#888'}"></span>
            <span style="font-weight:600;font-size:13px;min-width:56px">${cat}</span>
            ${v.industryM > 0 ? `
            <span style="font-size:12px;color:var(--tg)">
              ${v.industryLink
                ? `<a href="${v.industryLink}" target="_blank" style="color:var(--tg)" onclick="event.stopPropagation()">산업채팅방</a>`
                : '산업채팅방'}
              <span style="color:var(--text2);margin-left:3px">${v.industryM.toLocaleString()}명</span>
            </span>` : '<span></span>'}
            <span style="flex:1"></span>
            <span style="font-size:12px;color:var(--text2)">기업방 <b>${v.n}</b>개</span>
            <span style="font-size:13px;font-weight:600;min-width:70px;text-align:right">${v.m.toLocaleString()}명</span>
            <span style="font-size:11px;color:var(--text3);width:14px;text-align:center" id="${catId}-icon">▶</span>
          </div>
          <div id="${catId}" style="display:none;padding:2px 0 10px 18px">
            ${compList.map(r=>`
              <div style="display:flex;align-items:center;gap:6px;padding:3px 0">
                <span style="font-size:12px;flex:1;color:var(--text1)">${r.name}</span>
                <span style="font-size:12px;color:var(--text2);min-width:52px;text-align:right">${(r.members||0).toLocaleString()}명</span>
                <span style="font-size:11px;font-weight:500;min-width:28px;text-align:center;color:${(r.members||0)>=(r.max_members||1000)?'var(--red)':'var(--green)'}">${(r.members||0)>=(r.max_members||1000)?'마감':'입장'}</span>
                ${r.link?`<a href="${r.link}" target="_blank" style="font-size:12px;color:var(--tg);text-decoration:none">→</a>`:'<span style="width:12px"></span>'}
              </div>`).join('')}
          </div>
        </div>`;
      }).join('')}
    </div></div>
    <div class="card"><div class="card-header"><span class="card-title">멤버 Top 5</span></div><div class="card-body" style="padding:.75rem 1rem">
      ${top5.map((r,i)=>`<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px"><span style="width:16px;color:var(--text3);font-size:11px;font-weight:600">${i+1}</span><span style="flex:1">${r.name}</span><span style="color:var(--text2);font-size:12px">${(r.members||0).toLocaleString()}</span><div style="width:50px"><div class="progress"><div class="progress-fill" style="background:${CATS[r.cat]||'#888'};width:${Math.min(100,Math.round((r.members||0)/(r.max_members||1000)*100))}%"></div></div></div></div>`).join('')}
    </div></div>
  </div>`;
}

function pRooms() {
  const cats = [...new Set(A.rooms.map(r=>r.cat))].sort();
  const filtered = A.rooms.filter(r => {
    const cOk = A.cat==='all'||r.cat===A.cat, sOk = A.status==='all'||r.status===A.status;
    const q = A.q.toLowerCase();
    return cOk && sOk && (!q || r.name.toLowerCase().includes(q) || (r.keywords||'').toLowerCase().includes(q));
  });
  return `
  <div class="filter-bar">
    <input class="search-box" placeholder="이름·키워드 검색..." value="${A.q}" oninput="A.q=this.value;draw()">
    <button class="chip ${A.status==='all'?'active':''}" onclick="A.status='all';draw()">전체</button>
    <button class="chip ${A.status==='open'?'active':''}" onclick="A.status='open';draw()">입장 가능</button>
    <button class="chip ${A.status==='full'?'active':''}" onclick="A.status='full';draw()">정원 마감</button>
    <span style="width:1px;height:14px;background:var(--border2);margin:0 2px"></span>
    <button class="chip ${A.cat==='all'?'active':''}" onclick="A.cat='all';draw()">전체 산업</button>
    ${cats.map(c=>`<button class="chip ${A.cat===c?'active':''}" onclick="A.cat='${c}';draw()">${c}</button>`).join('')}
  </div>
  <div style="font-size:12px;color:var(--text3);margin-bottom:.75rem">${filtered.length}개</div>
  <div class="card"><div class="table-wrap"><table>
    <thead><tr><th>채팅방</th><th>산업</th><th>멤버 수</th><th>상태</th><th>Chat ID</th><th>관리</th></tr></thead>
    <tbody>${filtered.map(r=>`<tr>
      <td><div style="display:flex;align-items:center;gap:6px"><span class="cat-dot" style="background:${CATS[r.cat]||'#888'}"></span><span style="font-weight:500">${r.name}</span>${r.code?`<span style="font-size:10px;color:var(--text3)">${r.code}</span>`:''}</div></td>
      <td>
        <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
          <span class="badge badge-cat">${r.cat}</span>
          ${r.room_type==='industry'?`<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:rgba(74,158,255,.15);color:#4a9eff">산업방</span>`:''}
        </div>
        ${r.sub_cat&&r.sub_cat!=='산업전체'?`<div style="font-size:11px;color:var(--text3);margin-top:2px">${r.sub_cat}</div>`:''}
      </td>
      <td><div>${(r.members||0).toLocaleString()}<span style="color:var(--text3)">/${r.max_members||1000}</span></div><div class="progress" style="margin-top:4px"><div class="progress-fill" style="background:${(r.members||0)/(r.max_members||1000)>.9?'var(--red)':'var(--tg)'};width:${Math.min(100,Math.round((r.members||0)/(r.max_members||1000)*100))}%"></div></div></td>
      <td><span class="badge ${r.status==='full'?'badge-full':'badge-open'}">${r.status==='full'?'정원 마감':'입장 가능'}</span></td>
      <td><span style="font-size:11px;color:var(--text3);font-family:monospace">${String(r.chat_id).slice(0,22)}</span></td>
      <td><div style="display:flex;gap:5px">
        <button class="btn btn-sm" onclick="openDetail(${r.id})">상세</button>
        ${canEdit()?`<button class="btn btn-sm" onclick="syncOne(${r.id})" title="동기화">↻</button><button class="btn btn-sm ${r.status==='full'?'btn-success':'btn-danger'}" onclick="toggleStatus(${r.id})">${r.status==='full'?'개방':'마감'}</button>`:''}
      </div></td>
    </tr>`).join('')}</tbody>
  </table></div></div>`;
}

function pNotice() {
  if (!canEdit()) return `<div style="padding:2rem;text-align:center;color:var(--text3);font-size:13px">발송 권한이 없습니다 (viewer)</div>`;
  return `
  <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">새 공지 작성</span></div><div class="card-body">
    <div class="form-group" style="max-width:220px"><label class="form-label">대상</label>
      <select class="form-select" id="i-target"><option value="all">전체 (${A.rooms.length}개)</option><option value="open">입장 가능</option><option value="바이오">바이오</option><option value="뷰티">뷰티</option><option value="로봇">로봇</option><option value="2차전지">2차전지</option><option value="테크">테크</option><option value="반도체">반도체</option><option value="신재생">신재생</option></select>
    </div>
    <div class="form-group"><label class="form-label">내용</label><textarea class="form-input" id="i-content" rows="5" oninput="prev(this.value,'i-prev')"></textarea></div>
    <div class="form-group"><label class="form-label">미리보기</label><div id="i-prev" style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;font-size:13px;min-height:36px;color:var(--text2)"></div></div>
    <div id="i-prog" class="hidden" style="font-size:12px;padding:8px;background:var(--bg3);border-radius:var(--radius-sm);color:var(--text2);margin-bottom:.75rem"></div>
    <div style="display:flex;justify-content:flex-end"><button class="btn btn-primary" id="i-btn" onclick="sendInline()">발송 + DB 저장</button></div>
  </div></div>
  <div class="section-header"><span class="section-title">발송 기록</span></div>
  <div class="card" id="notice-list"><div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div></div>`;
}

async function loadNotices() {
  const el = document.getElementById('notice-list'); if (!el) return;
  const { data, error } = await DB('notice_history').select('*').order('created_at',{ascending:false}).limit(30);
  if (error) { el.innerHTML=`<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
  el.innerHTML=`<div class="table-wrap"><table><thead><tr><th>시각</th><th>발송자</th><th>대상</th><th>내용</th><th>발송</th><th>성공</th></tr></thead><tbody>
    ${!data.length?'<tr><td colspan="6" class="empty-row">기록 없음</td></tr>':data.map(h=>`<tr>
      <td style="font-size:12px;color:var(--text2)">${new Date(h.created_at).toLocaleString('ko-KR')}</td>
      <td style="font-size:12px;color:var(--text2)">봇/대시보드</td>
      <td><span class="badge badge-cat">${h.target}</span></td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h.content.replace(/<[^>]+>/g,'').slice(0,35)}...</td>
      <td>${h.sent_count}</td><td style="color:var(--green)">${h.ok_count}</td>
    </tr>`).join('')}
  </tbody></table></div>`;
}

function pLogs() {
  return `<div class="section-header"><span class="section-title">동기화 로그 (최근 50건)</span><button class="btn btn-sm" onclick="loadLogs()">새로고침</button></div>
  <div class="card" id="log-list"><div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div></div>`;
}

async function loadLogs() {
  const el = document.getElementById('log-list'); if (!el) return;
  const { data, error } = await DB('sync_logs').select('*').order('synced_at',{ascending:false}).limit(50);
  if (error) { el.innerHTML=`<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
  el.innerHTML=`<div class="table-wrap"><table><thead><tr><th>시각</th><th>채팅방</th><th>이전</th><th>이후</th><th>변화</th></tr></thead><tbody>
    ${!data.length?'<tr><td colspan="5" class="empty-row">없음</td></tr>':data.map(l=>{const d=l.after-l.before;return`<tr>
      <td style="font-size:12px;color:var(--text2)">${new Date(l.synced_at).toLocaleString('ko-KR')}</td>
      <td style="font-weight:500">${l.room_name}</td>
      <td>${(l.before||0).toLocaleString()}</td><td>${(l.after||0).toLocaleString()}</td>
      <td style="font-weight:600;color:${d>0?'var(--green)':d<0?'var(--red)':'var(--text3)'}">${d>0?'+':''}${d}</td>
    </tr>`;}).join('')}
  </tbody></table></div>`;
}

// ── 재무 상태 ──

function toggleCatDetail(id) {
  const el = document.getElementById(id);
  const icon = document.getElementById(id + '-icon');
  if (!el) return;
  const isOpen = el.style.display !== 'none';
  el.style.display = isOpen ? 'none' : 'block';
  if (icon) icon.textContent = isOpen ? '▶' : '▼';
}

function pInvestment() {
  return `
  <div id="inv-body">
    <div style="display:flex;gap:8px;margin-bottom:1rem;flex-wrap:wrap">
      <select class="form-select" id="inv-industry" onchange="loadInvestment()" style="width:130px;padding:6px 10px">
        <option value="all">전체 산업</option>
        <option value="반도체">반도체</option>
        <option value="바이오">바이오</option>
        <option value="2차전지">2차전지</option>
        <option value="로봇">로봇</option>
        <option value="뷰티">뷰티</option>
        <option value="테크">테크</option>
        <option value="조선">조선</option>
        <option value="신재생">신재생</option>
        <option value="엔터">엔터</option>
        <option value="소비재">소비재</option>
        <option value="우주">우주</option>
      </select>
      <span style="font-size:12px;color:var(--text3);align-self:center" id="inv-date"></span>
      <button class="btn btn-sm" style="margin-left:auto" onclick="loadInvestment()">새로고침</button>
    </div>

    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:1rem" id="inv-summary">
      <div class="metric-card"><div class="metric-label">급등 (상위 5%)</div><div class="metric-value" style="color:var(--red)" id="inv-surge">—</div></div>
      <div class="metric-card"><div class="metric-label">급락 (하위 5%)</div><div class="metric-value" style="color:#4a9eff" id="inv-drop">—</div></div>
      <div class="metric-card"><div class="metric-label">산업 평균 등락률</div><div class="metric-value" id="inv-avg">—</div></div>
    </div>

    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:1rem">
      <div class="card">
        <div class="card-header"><span class="card-title">시총 Top 10</span></div>
        <div id="inv-cap-list" style="padding:.5rem 0"></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">등락률 순위</span></div>
        <div id="inv-chg-list" style="padding:.5rem 0"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">산업별 등락률</span><span style="font-size:11px;color:var(--text3)">각 산업 상위 3종목</span></div>
      <div id="inv-industry-list" style="padding:.5rem 0"></div>
    </div>
  </div>`;
}

let _invData = [];
let _invIndustryMap = {};

async function loadInvestment() {
  const industry = document.getElementById('inv-industry')?.value || 'all';

  // market_data 조회
  let query = sb.from('market_data')
    .select('stock_code,corp_name,market_cap,price,price_change_rate,per,pbr')
    .order('base_date', { ascending: false })
    .limit(3000);

  const { data: mktRaw } = await query;
  if (!mktRaw?.length) return;

  // 종목당 최신 1개
  const latest = {};
  mktRaw.forEach(r => { if (!latest[r.stock_code]) latest[r.stock_code] = r; });
  let allData = Object.values(latest);

  // 날짜 표시
  const dateEl = document.getElementById('inv-date');
  if (dateEl && mktRaw[0]) {
    // base_date가 없으면 최근 updated_at 기준
    dateEl.textContent = `기준: ${mktRaw[0].base_date || '최근'}`;
  }

  // companies 테이블에서 산업 정보 가져오기 (항상 최신으로 로드)
  const { data: compData } = await sb.from('companies').select('name,code,industry,sub_industry').eq('active', true);
  const industryMap = {};
  (compData || []).forEach(s => {
    const code = (s.code || '').split('.')[0];
    if (code) industryMap[code] = { industry: s.industry || '기타', sub_industry: s.sub_industry || '' };
  });

  // 산업 정보 붙이기
  allData = allData.map(r => ({
    ...r,
    industry:     (industryMap[r.stock_code] || {}).industry     || '기타',
    sub_industry: (industryMap[r.stock_code] || {}).sub_industry || '',
  }));

  // 산업 필터
  const filtered = industry === 'all' ? allData : allData.filter(r => r.industry === industry);

  // 등락률 있는 것만
  const withChg = filtered.filter(r => r.price_change_rate != null);

  // 요약 지표
  const sorted = [...withChg].sort((a,b) => (b.price_change_rate||0) - (a.price_change_rate||0));
  const top5pct = Math.max(1, Math.floor(sorted.length * 0.05));
  const surgeAvg = sorted.slice(0, top5pct).reduce((s,r) => s + r.price_change_rate, 0) / top5pct;
  const dropAvg  = sorted.slice(-top5pct).reduce((s,r) => s + r.price_change_rate, 0) / top5pct;
  const avg = withChg.reduce((s,r) => s + (r.price_change_rate||0), 0) / (withChg.length || 1);

  const surgeEl = document.getElementById('inv-surge');
  const dropEl  = document.getElementById('inv-drop');
  const avgEl   = document.getElementById('inv-avg');
  if (surgeEl) surgeEl.textContent = `+${surgeAvg.toFixed(2)}%`;
  if (dropEl)  dropEl.textContent  = `${dropAvg.toFixed(2)}%`;
  if (avgEl) {
    avgEl.textContent = `${avg >= 0 ? '+' : ''}${avg.toFixed(2)}%`;
    avgEl.style.color = avg >= 0 ? 'var(--red)' : '#4a9eff';
  }

  // 시총 Top 10
  const capTop = [...filtered]
    .filter(r => r.market_cap)
    .sort((a,b) => (b.market_cap||0) - (a.market_cap||0))
    .slice(0, 10);

  const capEl = document.getElementById('inv-cap-list');
  if (capEl) {
    capEl.innerHTML = capTop.map((r, i) => {
      const chg = r.price_change_rate;
      const chgColor = chg > 0 ? 'var(--red)' : chg < 0 ? '#4a9eff' : 'var(--text3)';
      const chgStr = chg != null ? `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%` : '—';
      return `<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:11px;color:var(--text3);width:14px">${i+1}</span>
          <div>
            <div style="font-size:13px;font-weight:500">${r.corp_name}</div>
            <div style="font-size:11px;color:var(--text3)">${r.industry || ''}</div>
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-size:12px;font-weight:500">${fmtCap(r.market_cap)}</div>
          <div style="font-size:11px;color:${chgColor}">${chgStr}</div>
        </div>
      </div>`;
    }).join('');
  }

  // 등락률 Top/Bottom 10
  const chgTop    = sorted.slice(0, 10);
  const chgBottom = sorted.slice(-10).reverse();
  const combined  = [...chgTop, ...chgBottom];

  const chgEl = document.getElementById('inv-chg-list');
  if (chgEl) {
    chgEl.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0">
        <div>
          <div style="font-size:11px;color:var(--text3);padding:4px 12px;font-weight:500">급등</div>
          ${chgTop.map(r => `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 12px;border-bottom:1px solid var(--border)">
            <div>
              <div style="font-size:12px;font-weight:500">${r.corp_name}</div>
              <div style="font-size:10px;color:var(--text3)">${r.industry||''}</div>
            </div>
            <span style="font-size:12px;font-weight:500;color:var(--red)">+${r.price_change_rate.toFixed(2)}%</span>
          </div>`).join('')}
        </div>
        <div style="border-left:1px solid var(--border)">
          <div style="font-size:11px;color:var(--text3);padding:4px 12px;font-weight:500">급락</div>
          ${chgBottom.map(r => `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 12px;border-bottom:1px solid var(--border)">
            <div>
              <div style="font-size:12px;font-weight:500">${r.corp_name}</div>
              <div style="font-size:10px;color:var(--text3)">${r.industry||''}</div>
            </div>
            <span style="font-size:12px;font-weight:500;color:#4a9eff">${r.price_change_rate.toFixed(2)}%</span>
          </div>`).join('')}
        </div>
      </div>`;
  }

  // 산업별 등락률
  const industries = ['반도체','바이오','2차전지','로봇','뷰티','테크','조선','신재생','엔터','소비재','우주'];
  const targetInds = industry === 'all' ? industries : [industry];
  const isSingle   = industry !== 'all';  // 단일 산업 선택 시 세부분류 표시

  const indEl = document.getElementById('inv-industry-list');
  if (indEl) {
    indEl.innerHTML = targetInds.map(ind => {
      const indStocks = withChg
        .filter(r => r.industry === ind)
        .sort((a,b) => (b.price_change_rate||0) - (a.price_change_rate||0));
      if (!indStocks.length) return '';
      const indAvg = indStocks.reduce((s,r) => s + (r.price_change_rate||0), 0) / indStocks.length;

      if (isSingle) {
        // 세부분류별 그룹핑
        const subMap = {};
        indStocks.forEach(r => {
          const sub = r.sub_industry || '기타';
          if (!subMap[sub]) subMap[sub] = [];
          subMap[sub].push(r);
        });
        // 세부분류별 평균 등락률 기준 정렬
        const subEntries = Object.entries(subMap).sort((a,b) => {
          const avgA = a[1].reduce((s,r) => s+(r.price_change_rate||0),0)/a[1].length;
          const avgB = b[1].reduce((s,r) => s+(r.price_change_rate||0),0)/b[1].length;
          return avgB - avgA;
        });

        return `<div style="padding:8px 0">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:0 12px;margin-bottom:8px">
            <span style="font-size:13px;font-weight:600">${ind}</span>
            <span style="font-size:12px;color:${indAvg>=0?'var(--red)':'#4a9eff'};font-weight:500">전체 평균 ${indAvg>=0?'+':''}${indAvg.toFixed(2)}%</span>
          </div>
          ${subEntries.map(([sub, stocks]) => {
            const subAvg = stocks.reduce((s,r) => s+(r.price_change_rate||0),0)/stocks.length;
            const icon = subAvg > 1 ? '🔥' : subAvg > 0 ? '🔺' : subAvg < 0 ? '🔹' : '⬜';
            return `<div style="padding:7px 12px;border-top:1px solid var(--border)">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px">
                <span style="font-size:12px;font-weight:500;color:var(--text1)">${icon} ${sub}</span>
                <span style="font-size:11px;color:${subAvg>=0?'var(--red)':'#4a9eff'}">${subAvg>=0?'+':''}${subAvg.toFixed(2)}%</span>
              </div>
              <div style="display:flex;gap:5px;flex-wrap:wrap">
                ${stocks.map(r => {
                  const chg = r.price_change_rate || 0;
                  const color = chg > 0 ? 'rgba(45,206,137,.12)' : chg < 0 ? 'rgba(74,158,255,.12)' : 'rgba(128,128,128,.1)';
                  const tc = chg > 0 ? 'var(--green)' : chg < 0 ? '#4a9eff' : 'var(--text3)';
                  return `<span style="font-size:11px;padding:2px 8px;border-radius:100px;background:${color};color:${tc}">${r.corp_name} ${chg>=0?'+':''}${chg.toFixed(1)}%</span>`;
                }).join('')}
              </div>
            </div>`;
          }).join('')}
        </div>`;
      } else {
        // 전체 산업 보기 — 기존 방식 (상위 3 + 하위 3)
        const top3 = indStocks.slice(0, 3);
        const bot3 = indStocks.slice(-3).reverse();
        return `<div style="padding:10px 12px;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <span style="font-size:13px;font-weight:500">${ind}</span>
            <span style="font-size:12px;color:${indAvg>=0?'var(--red)':'#4a9eff'};font-weight:500">평균 ${indAvg>=0?'+':''}${indAvg.toFixed(2)}%</span>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            ${top3.map(r => `<span style="font-size:11px;padding:2px 8px;border-radius:100px;background:rgba(45,206,137,.12);color:var(--green)">${r.corp_name} +${r.price_change_rate.toFixed(1)}%</span>`).join('')}
            ${bot3.map(r => `<span style="font-size:11px;padding:2px 8px;border-radius:100px;background:rgba(74,158,255,.12);color:#4a9eff">${r.corp_name} ${r.price_change_rate.toFixed(1)}%</span>`).join('')}
          </div>
        </div>`;
      }
    }).filter(Boolean).join('');
  }
}

function pScreener() {
  return `
  <div style="display:grid;grid-template-columns:280px 1fr;gap:1rem;align-items:start">

    <div class="card" style="position:sticky;top:1rem">
      <div class="card-header"><span class="card-title">필터 조건</span></div>
      <div style="padding:.75rem 1rem;display:flex;flex-direction:column;gap:1rem">

        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:6px">산업</div>
          <select class="form-select" id="sc-industry" style="width:100%">
            <option value="">전체</option>
            <option>바이오</option><option>반도체</option><option>2차전지</option>
            <option>로봇</option><option>뷰티</option><option>테크</option>
            <option>조선</option><option>신재생</option><option>엔터</option>
            <option>소비재</option><option>우주</option>
          </select>
        </div>

        <div>
          <div style="font-size:12px;color:var(--text2);margin-bottom:6px">시장</div>
          <select class="form-select" id="sc-market" style="width:100%">
            <option value="">전체</option>
            <option value="KOSPI">코스피</option>
            <option value="KOSDAQ">코스닥</option>
          </select>
        </div>

        <div style="border-top:1px solid var(--border);padding-top:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:.75rem">밸류에이션</div>
          ${[
            ['sc-per-min','sc-per-max','PER','0','100'],
            ['sc-pbr-min','sc-pbr-max','PBR','0','20'],
          ].map(([id1,id2,label,min,max])=>`
            <div style="margin-bottom:.75rem">
              <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text2);margin-bottom:4px">
                <span>${label}</span>
                <span id="${id1}-label" style="color:var(--text3)">0 ~ ${max}</span>
              </div>
              <div style="display:flex;gap:6px;align-items:center">
                <input type="number" class="form-input" id="${id1}" placeholder="최소" min="${min}" style="width:70px;padding:4px 8px;font-size:12px">
                <span style="color:var(--text3);font-size:12px">~</span>
                <input type="number" class="form-input" id="${id2}" placeholder="최대" max="${max}" style="width:70px;padding:4px 8px;font-size:12px">
              </div>
            </div>`).join('')}
        </div>

        <div style="border-top:1px solid var(--border);padding-top:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:.75rem">수익성</div>
          ${[
            ['sc-margin-min','sc-margin-max','영업이익률(%)'],
            ['sc-roe-min','sc-roe-max','ROE(%)'],
            ['sc-roa-min','sc-roa-max','ROA(%)'],
          ].map(([id1,id2,label])=>`
            <div style="margin-bottom:.75rem">
              <div style="font-size:12px;color:var(--text2);margin-bottom:4px">${label}</div>
              <div style="display:flex;gap:6px;align-items:center">
                <input type="number" class="form-input" id="${id1}" placeholder="최소" style="width:70px;padding:4px 8px;font-size:12px">
                <span style="color:var(--text3);font-size:12px">~</span>
                <input type="number" class="form-input" id="${id2}" placeholder="최대" style="width:70px;padding:4px 8px;font-size:12px">
              </div>
            </div>`).join('')}
        </div>

        <div style="border-top:1px solid var(--border);padding-top:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:.75rem">재무건전성</div>
          ${[
            ['sc-debt-min','sc-debt-max','부채비율(%)'],
            ['sc-cr-min','sc-cr-max','유동비율(%)'],
          ].map(([id1,id2,label])=>`
            <div style="margin-bottom:.75rem">
              <div style="font-size:12px;color:var(--text2);margin-bottom:4px">${label}</div>
              <div style="display:flex;gap:6px;align-items:center">
                <input type="number" class="form-input" id="${id1}" placeholder="최소" style="width:70px;padding:4px 8px;font-size:12px">
                <span style="color:var(--text3);font-size:12px">~</span>
                <input type="number" class="form-input" id="${id2}" placeholder="최대" style="width:70px;padding:4px 8px;font-size:12px">
              </div>
            </div>`).join('')}
        </div>

        <div style="border-top:1px solid var(--border);padding-top:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:.75rem">시가총액</div>
          <div style="display:flex;gap:6px;align-items:center">
            <input type="number" class="form-input" id="sc-cap-min" placeholder="최소(억)" style="width:90px;padding:4px 8px;font-size:12px">
            <span style="color:var(--text3);font-size:12px">~</span>
            <input type="number" class="form-input" id="sc-cap-max" placeholder="최대(억)" style="width:90px;padding:4px 8px;font-size:12px">
          </div>
        </div>

        <div style="border-top:1px solid var(--border);padding-top:.75rem">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:.75rem">프리셋</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            <button class="btn btn-sm" onclick="applyPreset('value')">가치주</button>
            <button class="btn btn-sm" onclick="applyPreset('growth')">성장주</button>
            <button class="btn btn-sm" onclick="applyPreset('quality')">우량주</button>
            <button class="btn btn-sm" onclick="applyPreset('reset')">초기화</button>
          </div>
        </div>

        <button class="btn btn-primary" onclick="runScreener()" style="width:100%">검색</button>
      </div>
    </div>

    <div>
      <div id="sc-result" style="color:var(--text3);font-size:13px;padding:2rem;text-align:center">
        조건을 설정하고 검색 버튼을 눌러주세요.
      </div>
    </div>
  </div>`;
}

function applyPreset(type) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  // 초기화
  ['sc-per-min','sc-per-max','sc-pbr-min','sc-pbr-max',
   'sc-margin-min','sc-margin-max','sc-roe-min','sc-roe-max','sc-roa-min','sc-roa-max',
   'sc-debt-min','sc-debt-max','sc-cr-min','sc-cr-max','sc-cap-min','sc-cap-max']
  .forEach(id => set(id, ''));

  if (type === 'value') {
    set('sc-per-max', '15'); set('sc-pbr-max', '1.5');
    set('sc-margin-min', '5'); set('sc-debt-max', '100');
  } else if (type === 'growth') {
    set('sc-margin-min', '15'); set('sc-roe-min', '15');
    set('sc-per-max', '50');
  } else if (type === 'quality') {
    set('sc-margin-min', '10'); set('sc-roe-min', '10');
    set('sc-debt-max', '100'); set('sc-cr-min', '150');
  }
}

async function runScreener() {
  const el = document.getElementById('sc-result');
  el.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text3)"><span class="loading"></span> 검색 중...</div>';

  const g = id => { const v = document.getElementById(id)?.value; return v ? parseFloat(v) : null; };
  const industry = document.getElementById('sc-industry')?.value || '';
  const market   = document.getElementById('sc-market')?.value   || '';

  const filters = {
    perMin: g('sc-per-min'), perMax: g('sc-per-max'),
    pbrMin: g('sc-pbr-min'), pbrMax: g('sc-pbr-max'),
    marginMin: g('sc-margin-min'), marginMax: g('sc-margin-max'),
    roeMin: g('sc-roe-min'), roeMax: g('sc-roe-max'),
    roaMin: g('sc-roa-min'), roaMax: g('sc-roa-max'),
    debtMin: g('sc-debt-min'), debtMax: g('sc-debt-max'),
    crMin: g('sc-cr-min'), crMax: g('sc-cr-max'),
    capMin: g('sc-cap-min'), capMax: g('sc-cap-max'),
  };

  // financials 최신 분기 데이터 로드
  let finRows = [], page = 0;
  while (true) {
    const { data } = await sb.from('financials').select('stock_code,operating_margin,roe,roa,debt_ratio,current_ratio,bsns_year,quarter')
      .order('bsns_year',{ascending:false}).order('quarter',{ascending:false})
      .range(page*1000,(page+1)*1000-1);
    if (!data?.length) break;
    finRows = finRows.concat(data);
    if (data.length < 1000) break;
    page++;
  }
  // 종목당 최신 1개
  const finMap = {};
  finRows.forEach(r => { if (!finMap[r.stock_code]) finMap[r.stock_code] = r; });

  // market_data 최신
  const { data: latestDate } = await sb.from('market_data').select('base_date').order('base_date',{ascending:false}).limit(1);
  const maxDate = latestDate?.[0]?.base_date;
  let mktRows = [], mp = 0;
  while (true) {
    const { data } = await sb.from('market_data').select('stock_code,corp_name,market_cap,price,price_change_rate,per,pbr,market')
      .eq('base_date', maxDate).range(mp*1000,(mp+1)*1000-1);
    if (!data?.length) break;
    mktRows = mktRows.concat(data);
    if (data.length < 1000) break;
    mp++;
  }

  // companies에서 industry 정보
  let compRows = [], cp = 0;
  while (true) {
    const { data } = await sb.from('companies').select('code,industry,sub_industry').range(cp*1000,(cp+1)*1000-1);
    if (!data?.length) break;
    compRows = compRows.concat(data);
    if (data.length < 1000) break;
    cp++;
  }
  const indMap = {};
  compRows.forEach(r => { indMap[r.code] = r.industry || ''; });

  // 데이터 합치기
  let combined = mktRows.map(m => ({
    ...m,
    industry: indMap[m.stock_code] || '',
    ...(finMap[m.stock_code] || {}),
    capEok: m.market_cap ? Math.round(m.market_cap / 1e8) : null,
  }));

  // 필터 적용
  const inRange = (val, min, max) => {
    if (val == null) return (min == null && max == null);
    if (min != null && val < min) return false;
    if (max != null && val > max) return false;
    return true;
  };

  combined = combined.filter(r => {
    if (industry && r.industry !== industry) return false;
    if (market   && r.market   !== market)   return false;
    if (!inRange(r.per,            filters.perMin,    filters.perMax))    return false;
    if (!inRange(r.pbr,            filters.pbrMin,    filters.pbrMax))    return false;
    if (!inRange(r.operating_margin, filters.marginMin, filters.marginMax)) return false;
    if (!inRange(r.roe,            filters.roeMin,    filters.roeMax))    return false;
    if (!inRange(r.roa,            filters.roaMin,    filters.roaMax))    return false;
    if (!inRange(r.debt_ratio,     filters.debtMin,   filters.debtMax))   return false;
    if (!inRange(r.current_ratio,  filters.crMin,     filters.crMax))     return false;
    if (!inRange(r.capEok,         filters.capMin,    filters.capMax))    return false;
    return true;
  });

  // 시총 내림차순 정렬
  combined.sort((a,b) => (b.market_cap||0) - (a.market_cap||0));

  if (!combined.length) {
    el.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text3)">조건에 맞는 종목이 없습니다.</div>';
    return;
  }

  const fmt = v => fmtCap(v);
  const pct = v => v != null ? v.toFixed(1)+'%' : '—';
  const num = v => v != null ? v.toFixed(1) : '—';
  const chgColor = v => v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text2)';

  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem">
      <span style="font-size:13px;font-weight:600">${combined.length}개 종목</span>
      <button class="btn btn-sm" onclick="exportScreener()">CSV 다운로드</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th>종목명</th><th>산업</th><th>시장</th><th>시가총액</th><th>현재가</th><th>등락률</th>
        <th>PER</th><th>PBR</th><th>영업이익률</th><th>ROE</th><th>ROA</th><th>부채비율</th>
      </tr></thead>
      <tbody>${combined.map(r => `<tr>
        <td style="font-weight:600;cursor:pointer;color:var(--tg)" onclick="openFinTrend('${r.stock_code}','${r.corp_name}')">${r.corp_name}</td>
        <td><span class="badge badge-cat">${r.industry||'—'}</span></td>
        <td style="font-size:11px;color:var(--text3)">${r.market||'—'}</td>
        <td style="font-size:12px">${fmt(r.market_cap)}</td>
        <td style="font-size:12px">${r.price ? r.price.toLocaleString()+'원' : '—'}</td>
        <td style="font-size:12px;color:${chgColor(r.price_change_rate)}">${r.price_change_rate != null ? (r.price_change_rate>0?'+':'')+r.price_change_rate.toFixed(2)+'%' : '—'}</td>
        <td>${num(r.per)}</td>
        <td>${num(r.pbr)}</td>
        <td style="color:${r.operating_margin>0?'var(--green)':'var(--text2)'}">${pct(r.operating_margin)}</td>
        <td>${pct(r.roe)}</td>
        <td>${pct(r.roa)}</td>
        <td>${pct(r.debt_ratio)}</td>
      </tr>`).join('')}
      </tbody>
    </table></div>`;

  window._screenerData = combined;
}

function exportScreener() {
  if (!window._screenerData?.length) return;
  const keys = ['corp_name','industry','market','capEok','price','price_change_rate','per','pbr','operating_margin','roe','roa','debt_ratio'];
  const headers = ['종목명','산업','시장','시총(억)','현재가','등락률','PER','PBR','영업이익률','ROE','ROA','부채비율'];
  const csv = [headers.join(','), ...window._screenerData.map(r => keys.map(k => r[k]??'').join(','))].join('\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,\uFEFF' + encodeURIComponent(csv);
  a.download = 'screener_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
}
