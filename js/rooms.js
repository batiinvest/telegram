// rooms.js — 채팅방 CRUD, 공지 발송, 팀원 관리
async function addRoom() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const name = document.getElementById('a-name').value.trim();
  const chatId = document.getElementById('a-chatid').value.trim();
  if (!name || !chatId) { toast('이름과 Chat ID 필수', 'error'); return; }
  const link = chatId.startsWith('@') ? `https://t.me/${chatId.replace('@','')}` : (document.getElementById('a-link').value.trim() || '');
  try {
    const { data, error } = await DB('rooms').insert([{ name, chat_id: chatId, cat: document.getElementById('a-cat').value, sub_cat: document.getElementById('a-sub').value.trim(), code: document.getElementById('a-code').value.trim(), max_members: parseInt(document.getElementById('a-max').value)||1000, link, keywords: document.getElementById('a-kw').value.trim(), members: 0, status:'open', pinned:false }]).select().single();
    if (error) throw error;
    A.rooms.push(data); A.rooms.sort((a,b) => a.name.localeCompare(b.name,'ko'));
    document.getElementById('badge').textContent = A.rooms.length;
    closeModal('m-add'); draw(); toast(`${name} 추가 완료`, 'success');
  } catch(e) { toast('추가 실패: ' + e.message, 'error'); }
}

async function toggleStatus(id) {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const r = A.rooms.find(x => x.id === id); if (!r) return;
  const s = r.status === 'full' ? 'open' : 'full';
  await DB('rooms').update({ status: s }).eq('id', id);
  r.status = s; draw(); toast(`${r.name} → ${s === 'full' ? '정원 마감' : '입장 가능'}`, 'info');
}

async function deleteRoom(id) {
  if (!canDel()) { toast('admin만 삭제 가능합니다.', 'error'); return; }
  const r = A.rooms.find(x => x.id === id);
  if (!r || !confirm(`"${r.name}" 삭제?`)) return;
  const { error } = await DB('rooms').delete().eq('id', id);
  if (error) { toast('삭제 실패: ' + error.message, 'error'); return; }
  A.rooms = A.rooms.filter(x => x.id !== id);
  document.getElementById('badge').textContent = A.rooms.length;
  closeModal('m-detail'); draw(); toast(`${r.name} 삭제됨`, 'info');
}

async function saveEdit(id) {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const p = { name: document.getElementById('e-name').value.trim(), cat: document.getElementById('e-cat').value, sub_cat: document.getElementById('e-sub').value.trim(), code: document.getElementById('e-code').value.trim(), chat_id: document.getElementById('e-chatid').value.trim(), link: document.getElementById('e-link').value.trim(), keywords: document.getElementById('e-kw').value.trim(), max_members: parseInt(document.getElementById('e-max').value)||1000 };
  if (!p.name || !p.chat_id) { toast('이름과 Chat ID 필수', 'error'); return; }
  const { error } = await DB('rooms').update(p).eq('id', id);
  if (error) { toast('수정 실패: ' + error.message, 'error'); return; }
  Object.assign(A.rooms.find(x => x.id === id), p);
  toast('수정 완료', 'success'); dtab('info', null);
}

// ══════════════════════════════════════════
//  NOTICE
// ══════════════════════════════════════════
async function doNotice(content, target, btnId, progId) {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  if (!content) { toast('내용 입력 필요', 'error'); return; }
  let targets = A.rooms;
  if (target === 'open') targets = targets.filter(r => r.status === 'open');
  else if (target !== 'all') targets = targets.filter(r => r.cat === target);
  if (!confirm(`${targets.length}개에 발송?`)) return;
  const btn = document.getElementById(btnId), prog = document.getElementById(progId);
  btn.disabled = true; prog.classList.remove('hidden');
  let ok = 0;
  for (let i = 0; i < targets.length; i++) {
    prog.innerHTML = `<span class="loading"></span>${i+1}/${targets.length} — ${targets[i].name}`;
    try { await tgSend(targets[i].chat_id, content); ok++; } catch {}
    await new Promise(r => setTimeout(r, 300));
  }
  await DB('notice_history').insert([{ target, content, sent_count: targets.length, ok_count: ok, sent_by: A.user.id }]);
  prog.innerHTML = `✓ ${ok}/${targets.length} 완료 — DB 저장됨`;
  btn.disabled = false;
  toast(`발송 완료: ${ok}/${targets.length}`, 'success');
  setTimeout(() => { prog.classList.add('hidden'); if (A.page === 'notice') loadNotices(); }, 3000);
}

const sendNoticeModal = () => doNotice(document.getElementById('n-content').value.trim(), document.getElementById('n-target').value, 'n-btn', 'n-prog');
const sendInline = () => doNotice(document.getElementById('i-content').value.trim(), document.getElementById('i-target').value, 'i-btn', 'i-prog');

async function sendSingleNotice(id) {
  const r = A.rooms.find(x => x.id === id); if (!r) return;
  const content = document.getElementById('s-content').value.trim();
  if (!content) { toast('내용 입력 필요', 'error'); return; }
  try {
    await tgSend(r.chat_id, content);
    await DB('notice_history').insert([{ target: r.name, content, sent_count: 1, ok_count: 1, sent_by: A.user.id }]);
    toast(`${r.name} 발송 완료`, 'success');
  } catch(e) { toast('실패: ' + e.message, 'error'); }
}

// ══════════════════════════════════════════
//  SETTINGS — app_config DB 저장/로드
// ══════════════════════════════════════════
async function saveConfig(key, value) {
  if (!isAdmin()) { toast('admin만 설정 변경 가능합니다.', 'error'); return; }
  const { error } = await DB('app_config').update({ value, updated_at: new Date().toISOString(), updated_by: A.user.id }).eq('key', key);
  if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  A.config[key] = value;
  toast(`설정 저장됨`, 'success');
}

async function testBot() {
  const el = document.getElementById('bot-result'); el.innerHTML = '<span class="loading"></span>테스트 중...';
  try { const b = await tg('getMe'); el.innerHTML = `<span style="color:var(--green)">✓ @${b.username} (${b.first_name})</span>`; }
  catch(e) { el.innerHTML = `<span style="color:var(--red)">✗ ${e.message}</span>`; }
}

// ══════════════════════════════════════════
//  TEAM MANAGEMENT
// ══════════════════════════════════════════
async function loadTeam() {
  const el = document.getElementById('team-list'); if (!el) return;
  const { data, error } = await DB('app_users').select('*').order('created_at');
  if (error) { el.innerHTML = `<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
  const roleLabel = { admin: '관리자', editor: '에디터', viewer: '뷰어' };
  const roleCls   = { admin: 'role-admin', editor: 'role-editor', viewer: 'role-viewer' };
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>이름</th><th>이메일</th><th>역할</th><th>마지막 로그인</th>${isAdmin()?'<th>역할 변경</th>':''}</tr></thead>
    <tbody>${data.map(u => `<tr>
      <td style="font-weight:500">${u.name || '—'}</td>
      <td style="color:var(--text2)">${u.email}</td>
      <td><span class="nav-role ${roleCls[u.role]}">${roleLabel[u.role]}</span></td>
      <td style="font-size:12px;color:var(--text3)">${u.last_login ? new Date(u.last_login).toLocaleString('ko-KR') : '없음'}</td>
      ${isAdmin() ? `<td><select style="font-size:12px;padding:3px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text)" onchange="changeRole('${u.id}',this.value)" ${u.id === A.user.id ? 'disabled' : ''}>
        <option value="admin" ${u.role==='admin'?'selected':''}>관리자</option>
        <option value="editor" ${u.role==='editor'?'selected':''}>에디터</option>
        <option value="viewer" ${u.role==='viewer'?'selected':''}>뷰어</option>
      </select></td>` : ''}
    </tr>`).join('')}</tbody>
  </table></div>`;
}

async function changeRole(userId, role) {
  if (!isAdmin()) { toast('권한 없음', 'error'); return; }
  const { error } = await DB('app_users').update({ role }).eq('id', userId);
  if (error) { toast('변경 실패: ' + error.message, 'error'); return; }
  toast('역할 변경됨', 'success');
}

// ══════════════════════════════════════════
//  NAVIGATION & RENDER
// ══════════════════════════════════════════
