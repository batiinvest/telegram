// pages.js — 페이지 렌더링 함수들
function pOverview() {
  const total = A.rooms.length, full = A.rooms.filter(r=>r.status==='full').length, open = A.rooms.filter(r=>r.status==='open').length;
  const totalM = A.rooms.reduce((s,r)=>s+(r.members||0),0);
  const catMap = {}; A.rooms.forEach(r=>{ if(!catMap[r.cat])catMap[r.cat]={n:0,m:0}; catMap[r.cat].n++; catMap[r.cat].m+=r.members||0; });
  const top5 = [...A.rooms].sort((a,b)=>b.members-a.members).slice(0,5);
  return `
  <div class="metrics-grid">
    <div class="metric-card"><div class="metric-label">전체 채팅방</div><div class="metric-value">${total}</div><div class="metric-sub">Supabase DB 기준</div></div>
    <div class="metric-card"><div class="metric-label">전체 멤버</div><div class="metric-value">${totalM.toLocaleString()}</div><div class="metric-sub">동기화 기준</div></div>
    <div class="metric-card"><div class="metric-label">정원 마감</div><div class="metric-value" style="color:var(--red)">${full}</div><div class="metric-sub">${total?Math.round(full/total*100):0}%</div></div>
    <div class="metric-card"><div class="metric-label">입장 가능</div><div class="metric-value" style="color:var(--green)">${open}</div><div class="metric-sub">${total?Math.round(open/total*100):0}%</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
    <div class="card"><div class="card-header"><span class="card-title">산업별 현황</span></div><div class="card-body" style="padding:.75rem 1rem">
      ${Object.entries(catMap).map(([cat,v])=>`<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px"><span class="cat-dot" style="background:${CATS[cat]||'#888'}"></span><span style="flex:1">${cat}</span><span style="color:var(--text2);font-size:12px">${v.n}개</span><span style="color:var(--text3);font-size:12px;min-width:50px;text-align:right">${v.m.toLocaleString()}명</span></div>`).join('')}
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
      <td><div><span class="badge badge-cat">${r.cat}</span></div>${r.sub_cat?`<div style="font-size:11px;color:var(--text3);margin-top:2px">${r.sub_cat}</div>`:''}</td>
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
const F = { mode: 'market', industry: '전체', q: '', sortBy: 'market_cap', sortDir: 'desc' };

