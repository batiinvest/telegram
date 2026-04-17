// detail.js — 채팅방 상세 모달, 사이드바, 유틸리티
function openDetail(id) {
  const r = A.rooms.find(x => x.id === id); if (!r) return;
  A.room = r;
  document.getElementById('d-title').textContent = r.name;
  document.getElementById('d-tg').onclick = () => r.link && window.open(r.link, '_blank');
  dtab('info', document.querySelector('#m-detail .tab'));
  openModal('m-detail');
}

function dtab(tab, el) {
  document.querySelectorAll('#m-detail .tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
  const r = A.room; if (!r) return;
  const body = document.getElementById('d-body');
  if (tab === 'info') {
    body.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:2px;margin-bottom:1rem">
        ${[['이름',r.name],['종목코드',r.code||'-'],['산업',r.cat],['세부 분야',r.sub_cat||'-'],['멤버',`${(r.members||0).toLocaleString()} / ${r.max_members||1000}`],['상태',r.status==='full'?'<span class="badge badge-full">정원 마감</span>':'<span class="badge badge-open">입장 가능</span>'],['Chat ID',`<code style="font-size:11px;background:var(--bg3);padding:2px 6px;border-radius:4px">${r.chat_id}</code>`],['링크',r.link?`<a href="${r.link}" target="_blank">${r.link.slice(0,45)}</a>`:'-'],['키워드',r.keywords?`<span style="font-size:12px;color:var(--text2)">${r.keywords}</span>`:'-']].map(([k,v])=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px"><span style="color:var(--text2)">${k}</span><span>${v}</span></div>`).join('')}
      </div>
      ${canEdit()?`<div style="display:flex;gap:8px">
        <button class="btn btn-sm" onclick="syncOne(${r.id})">멤버 수 동기화</button>
        <button class="btn btn-sm ${r.status==='full'?'btn-success':'btn-danger'}" onclick="toggleStatus(${r.id})">${r.status==='full'?'개방':'마감'}</button>
        ${canDel()?`<button class="btn btn-sm btn-danger" onclick="deleteRoom(${r.id})">삭제</button>`:''}
      </div>`:''}`;
  } else if (tab === 'edit') {
    if (!canEdit()) { body.innerHTML='<div style="padding:1rem;color:var(--text3);font-size:13px;text-align:center">수정 권한이 없습니다.</div>'; return; }
    const cats=['바이오','뷰티','로봇','2차전지','신재생','소비재','테크','반도체','엔터','조선','우주'];
    body.innerHTML=`
      <div class="form-row"><div class="form-group"><label class="form-label">이름</label><input class="form-input" id="e-name" value="${r.name}"></div><div class="form-group"><label class="form-label">종목코드</label><input class="form-input" id="e-code" value="${r.code||''}"></div></div>
      <div class="form-row"><div class="form-group"><label class="form-label">산업</label><select class="form-select" id="e-cat">${cats.map(c=>`<option ${c===r.cat?'selected':''}>${c}</option>`).join('')}</select></div><div class="form-group"><label class="form-label">세부 분야</label><input class="form-input" id="e-sub" value="${r.sub_cat||''}"></div></div>
      <div class="form-row"><div class="form-group"><label class="form-label">Chat ID</label><input class="form-input" id="e-chatid" value="${r.chat_id}"></div><div class="form-group"><label class="form-label">최대 정원</label><input class="form-input" type="number" id="e-max" value="${r.max_members||1000}"></div></div>
      <div class="form-group"><label class="form-label">초대 링크</label><input class="form-input" id="e-link" value="${r.link||''}"></div>
      <div class="form-group"><label class="form-label">키워드</label><input class="form-input" id="e-kw" value="${r.keywords||''}"></div>
      <div style="display:flex;justify-content:flex-end;margin-top:.5rem"><button class="btn btn-primary" onclick="saveEdit(${r.id})">DB에 저장</button></div>`;
  } else if (tab === 'msg') {
    if (!canEdit()) { body.innerHTML='<div style="padding:1rem;color:var(--text3);font-size:13px;text-align:center">발송 권한이 없습니다.</div>'; return; }
    body.innerHTML=`
      <div class="form-group"><label class="form-label">${r.name}에만 발송</label><textarea class="form-input" id="s-content" rows="4" oninput="prev(this.value,'s-prev')"></textarea></div>
      <div class="form-group"><label class="form-label">미리보기</label><div id="s-prev" style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 12px;font-size:13px;min-height:32px;color:var(--text2)"></div></div>
      <div style="display:flex;justify-content:flex-end"><button class="btn btn-primary" onclick="sendSingleNotice(${r.id})">발송 + DB 저장</button></div>`;
  } else if (tab === 'log') {
    body.innerHTML = `<div id="r-log"><span class="loading"></span></div>`;
    DB('sync_logs').select('*').eq('room_id', r.id).order('synced_at',{ascending:false}).limit(20)
      .then(({data}) => {
        document.getElementById('r-log').innerHTML = !data?.length ? '<div style="color:var(--text3);font-size:13px;padding:.5rem">이력 없음</div>' :
          data.map(l=>{const d=l.after-l.before;return`<div class="activity-item"><span class="activity-time">${new Date(l.synced_at).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</span><span>${(l.before||0).toLocaleString()} → <strong>${(l.after||0).toLocaleString()}</strong></span><span style="margin-left:auto;color:${d>0?'var(--green)':d<0?'var(--red)':'var(--text3)'};font-weight:600">${d>0?'+':''}${d}</span></div>`;}).join('');
      });
  }
}

// ══════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════
const prev = (v, id) => { const el = document.getElementById(id); if (el) el.innerHTML = v || '<span style="color:var(--text3)">미리보기</span>'; };
const openModal  = id => document.getElementById(id).classList.add('open');
const closeModal = id => document.getElementById(id).classList.remove('open');
document.querySelectorAll('.modal-overlay').forEach(o => o.addEventListener('click', e => { if (e.target === o) o.classList.remove('open'); }));
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const ov = document.getElementById('sidebar-overlay');
  const isOpen = sb.classList.contains('open');
  sb.classList.toggle('open', !isOpen);
  ov.classList.toggle('active', !isOpen);
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('active');
}

function toast(msg, type='info') {
  const c = document.getElementById('toasts'), t = document.createElement('div');
  t.className = `toast toast-${type}`; t.textContent = msg; c.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
