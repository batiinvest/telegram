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
  const { data: compData } = await sb.from('companies').select('name,code,industry').eq('active', true);
  const industryMap = {};
  (compData || []).forEach(s => {
    const code = (s.code || '').split('.')[0];
    if (code) industryMap[code] = s.industry || '기타';
  });

  // 산업 정보 붙이기
  allData = allData.map(r => ({ ...r, industry: industryMap[r.stock_code] || '기타' }));

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

  // 산업별 등락률 (상위 3종목)
  const industries = ['반도체','바이오','2차전지','로봇','뷰티','테크','조선','신재생','엔터','소비재','우주'];
  const targetInds = industry === 'all' ? industries : [industry];

  const indEl = document.getElementById('inv-industry-list');
  if (indEl) {
    indEl.innerHTML = targetInds.map(ind => {
      const indStocks = withChg
        .filter(r => r.industry === ind)
        .sort((a,b) => (b.price_change_rate||0) - (a.price_change_rate||0));
      if (!indStocks.length) return '';
      const indAvg = indStocks.reduce((s,r) => s + (r.price_change_rate||0), 0) / indStocks.length;
      const top3   = indStocks.slice(0, 3);
      const bot3   = indStocks.slice(-3).reverse();
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
    }).filter(Boolean).join('');
  }
}
