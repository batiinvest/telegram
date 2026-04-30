// pages.js — 텔레그램 채널 관련 페이지 (overview, rooms, notice, logs)
// 투자현황 → investment.js / 스크리너 → screener.js
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

  const sortedRooms = [...companyRooms].sort((a,b) => b.members - a.members);
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
    <div class="card"><div class="card-header"><span class="card-title">채팅방 멤버 순위</span></div><div class="card-body" style="padding:.75rem 1rem;max-height:400px;overflow-y:auto">
      ${sortedRooms.map((r,i)=>`<div style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px"><span style="width:16px;color:var(--text3);font-size:11px;font-weight:600">${i+1}</span><span style="flex:1">${r.name}</span><span style="color:var(--text2);font-size:12px">${(r.members||0).toLocaleString()}</span><div style="width:50px"><div class="progress"><div class="progress-fill" style="background:${CATS[r.cat]||'#888'};width:${Math.min(100,Math.round((r.members||0)/(r.max_members||1000)*100))}%"></div></div></div></div>`).join('')}
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

  const roomOptions = [...A.rooms]
    .sort((a,b) => a.name.localeCompare(b.name, 'ko'))
    .map(r => `<option value="room:${r.id}">[${r.cat}] ${r.name}</option>`)
    .join('');

  return `
  <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">새 공지 작성</span></div><div class="card-body">
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:1rem">
      <div class="form-group" style="margin:0;min-width:220px">
        <label class="form-label">발송 대상</label>
        <select class="form-select" id="i-target" onchange="onNoticeTargetChange()">
          <optgroup label="── 그룹 발송 ──">
            <option value="all">전체 (${A.rooms.length}개)</option>
            <option value="open">입장 가능</option>
            ${INDUSTRIES.map(i=>`<option value="${i}">${i}</option>`).join('')}
          </optgroup>
          <optgroup label="── 개별 채팅방 ──">
            ${roomOptions}
          </optgroup>
        </select>
      </div>
      <div class="form-group" style="margin:0">
        <label class="form-label">발송 형식</label>
        <select class="form-select" id="i-parse-mode">
          <option value="HTML">HTML</option>
          <option value="Markdown">Markdown (URL 자동 링크)</option>
        </select>
      </div>
      <div style="align-self:flex-end;font-size:12px;color:var(--text3)" id="i-target-info"></div>
    </div>
    <div class="form-group"><label class="form-label">내용</label><textarea class="form-input" id="i-content" rows="8" oninput="prev(this.value,'i-prev')"></textarea></div>
    <div class="form-group"><label class="form-label">미리보기</label><div id="i-prev" style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;font-size:13px;min-height:36px;color:var(--text2);white-space:pre-wrap"></div></div>
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
