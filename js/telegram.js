// telegram.js — Telegram Bot API, 멤버 수 동기화
async function tg(method, params = {}) {
  const token = botToken();
  if (!token) throw new Error('Bot 토큰이 DB에 설정되지 않았습니다. 설정 페이지에서 입력해주세요.');
  const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(params) });
  const d = await r.json();
  if (!d.ok) throw new Error(d.description);
  return d.result;
}
const tgCount = async id => { try { return await tg('getChatMemberCount', { chat_id: id }); } catch { return null; } };
const tgSend  = (id, text) => tg('sendMessage', { chat_id: id, text, parse_mode: 'HTML' });

// ══════════════════════════════════════════
//  SYNC
// ══════════════════════════════════════════
async function syncAll() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  if (!botToken()) { toast('Bot 토큰을 설정해주세요.', 'error'); go('settings'); return; }
  const btn = document.getElementById('sync-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading"></span>동기화 중...';
  const logs = []; let n = 0;
  for (const r of A.rooms) {
    const c = await tgCount(r.chat_id);
    if (c !== null && c !== r.members) {
      logs.push({ room_id: r.id, room_name: r.name, before: r.members||0, after: c, synced_by: A.user.id });
      await DB('rooms').update({ members: c }).eq('id', r.id);
      r.members = c; n++;
    }
  }
  if (logs.length) await DB('sync_logs').insert(logs);
  btn.disabled = false; btn.innerHTML = '<svg style="width:13px;height:13px;vertical-align:middle;margin-right:4px" viewBox="0 0 16 16" fill="none"><path d="M13.5 8A5.5 5.5 0 112.5 5M2.5 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>멤버 수 동기화';
  toast(`${n}개 방 업데이트 완료`, 'success'); draw();
}

async function syncOne(id) {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const r = A.rooms.find(x => x.id === id); if (!r) return;
  const c = await tgCount(r.chat_id);
  if (c === null) { toast('조회 실패 — Chat ID 확인', 'error'); return; }
  const before = r.members || 0;
  await DB('rooms').update({ members: c }).eq('id', id);
  await DB('sync_logs').insert([{ room_id: r.id, room_name: r.name, before, after: c, synced_by: A.user.id }]);
  r.members = c; toast(`${r.name}: ${c.toLocaleString()}명`, 'success'); draw();
}

// ══════════════════════════════════════════
//  ROOM CRUD
// ══════════════════════════════════════════
