// ══════════════════════════════════════════
//  Supabase 연결 설정
//  SB_URL, SB_KEY 두 줄만 본인 값으로 교체하세요
//  SB_KEY: Supabase > Settings > API > anon public (eyJ...로 시작하는 값)
// ══════════════════════════════════════════
const SB_URL = 'https://ngyzcpogfxbkoqkcfipv.supabase.co';
const SB_KEY = 'sb_publishable_Z1NuLTk2M2EwYzMxLWViNzktNDQ4Ni04NzI2LTgzYTNjNzNhYjcwOXhHUkpCTEdPSlNQUlZFdXVXZnc=';

const sb = supabase.createClient(SB_URL, SB_KEY, {
  auth: {
    storageKey: 'bati-auth-v2',
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
  }
});
const DB = sb.from.bind(sb);
const CATS = { '바이오':'#2AABEE','뷰티':'#f5365c','로봇':'#2dce89','2차전지':'#fb6340','신재생':'#5e72e4','소비재':'#f3a4b5','테크':'#a259ff','반도체':'#8898aa','엔터':'#ffd600','조선':'#11cdef','우주':'#4a6fa5' };


// ══════════════════════════════════════════
//  Supabase URL / Key — 여기만 직접 입력
//  (이 두 값은 공개 키라 노출되어도 괜찮습니다.
//   실제 보안은 RLS 정책이 담당합니다)
// ══════════════════════════════════════════
const SB_URL = 'https://ngyzcpogfxbkoqkcfipv.supabase.co';
const SB_KEY = 'YOUR_ANON_KEY';  // ← Supabase > Settings > API > anon public 키로 교체

const sb = supabase.createClient(SB_URL, SB_KEY, {
  auth: {
    storageKey: 'bati-auth-v2',
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
  }
});
const DB  = sb.from.bind(sb);
const CATS = { '바이오':'#2AABEE','뷰티':'#f5365c','로봇':'#2dce89','2차전지':'#fb6340','신재생':'#5e72e4','소비재':'#f3a4b5','테크':'#a259ff','반도체':'#8898aa','엔터':'#ffd600','조선':'#11cdef','우주':'#4a6fa5' };

// ── App state ──
const A = {
  user: null,     // Supabase auth user
  profile: null,  // app_users row  { role, name, email }
  rooms: [],
  config: {},     // app_config rows: { key: value }
  page: 'overview',
  cat: 'all', status: 'all', q: '',
  room: null,
};

const isAdmin  = () => A.profile?.role === 'admin';
const isEditor = () => ['admin','editor'].includes(A.profile?.role);
const canEdit  = () => isEditor();
const canDel   = () => isAdmin();

// ══════════════════════════════════════════
//  AUTH
// ══════════════════════════════════════════
async function doLogin() {
  const email = document.getElementById('login-email').value.trim();
  const pw    = document.getElementById('login-pw').value;
  const btn   = document.getElementById('login-btn');
  const err   = document.getElementById('login-err');
  err.classList.add('hidden');

  if (!email || !pw) {
    err.textContent = '이메일과 비밀번호를 입력해주세요.';
    err.classList.remove('hidden'); return;
  }

  btn.disabled = true; btn.textContent = '로그인 중...';

  try {
    const { data, error } = await sb.auth.signInWithPassword({ email, password: pw });
    console.log('[login] data:', data, 'error:', error);
    if (error) throw error;
  } catch(e) {
    console.error('[login error]', e);
    const msg = e.message || JSON.stringify(e);
    err.textContent = msg.includes('Invalid login')
      ? '이메일 또는 비밀번호가 올바르지 않습니다.'
      : msg.includes('Email not confirmed')
      ? '이메일 인증이 필요합니다. Supabase에서 Confirm email을 OFF로 설정해주세요.'
      : msg.includes('fetch')
      ? 'Supabase 연결 실패 — SB_URL, SB_KEY를 확인해주세요.'
      : msg;
    err.classList.remove('hidden');
  } finally {
    btn.disabled = false; btn.textContent = '로그인';
  }
}

async function doSignup() {
  const name  = document.getElementById('signup-name').value.trim();
  const email = document.getElementById('signup-email').value.trim();
  const pw    = document.getElementById('signup-pw').value;
  const btn   = document.getElementById('signup-btn');
  const err   = document.getElementById('signup-err');
  err.style.color = 'var(--red)';
  err.classList.add('hidden');

  if (!name || !email || pw.length < 8) {
    err.textContent = '모든 항목을 입력하세요 (비밀번호 8자 이상)';
    err.classList.remove('hidden'); return;
  }

  btn.disabled = true; btn.textContent = '가입 중...';

  try {
    const { data, error } = await sb.auth.signUp({
      email, password: pw, options: { data: { name } }
    });
    console.log('[signup] data:', data, 'error:', error);
    if (error) throw error;

    err.style.color = 'var(--green)';
    err.textContent = '가입 완료! 바로 로그인해주세요.';
    err.classList.remove('hidden');
    setTimeout(() => {
      showLogin();
      document.getElementById('login-email').value = email;
    }, 1500);
  } catch(e) {
    console.error('[signup error]', e);
    const msg = e.message || JSON.stringify(e);
    err.textContent = msg.includes('already registered')
      ? '이미 가입된 이메일입니다. 로그인을 시도해보세요.'
      : msg.includes('fetch')
      ? 'Supabase 연결 실패 — SB_URL, SB_KEY를 확인해주세요.'
      : msg.includes('Database error')
      ? 'DB 오류 — Supabase SQL Editor에서 fix_trigger.sql을 실행해주세요.'
      : msg;
    err.classList.remove('hidden');
  } finally {
    btn.disabled = false; btn.textContent = '가입하기';
  }
}

async function doLogout() {
  await sb.auth.signOut();
}

function showLogin()  { document.getElementById('login-form').classList.remove('hidden'); document.getElementById('signup-form').classList.add('hidden'); }
function showSignup() { document.getElementById('login-form').classList.add('hidden'); document.getElementById('signup-form').classList.remove('hidden'); }

// ── Auth boot: 세션 확인 후 대시보드 또는 로그인 화면 ──
async function bootAuth() {
  try {
    const { data: { session }, error } = await sb.auth.getSession();
    console.log('[boot]', session?.user?.email, error?.message);
    if (session?.user) {
      A.user = session.user;
      await Promise.all([loadProfile(), loadConfig(), loadRooms()]);
      showDashboard();
    } else {
      showLoginScreen();
    }
  } catch(e) {
    console.error('[boot error]', e);
    showLoginScreen();
  }
}

// ── 로그인/로그아웃 이벤트 감지 ──
sb.auth.onAuthStateChange((event, session) => {
  console.log('[auth event]', event, session?.user?.email);
  if (event === 'SIGNED_IN' && !A.user) {
    bootAuth();
  } else if (event === 'SIGNED_OUT') {
    A.user = null; A.profile = null;
    showLoginScreen();
  }
});

// 페이지 로드 시 실행
bootAuth();

async function loadProfile() {
  const { data } = await DB('app_users').select('*').eq('id', A.user.id).single();
  A.profile = data;
}

async function loadConfig() {
  const { data } = await DB('app_config').select('key,value');
  if (data) data.forEach(r => A.config[r.key] = r.value);
}

async function loadRooms() {
  const { data, error } = await DB('rooms').select('*').order('cat').order('name');
  if (!error) A.rooms = data;
}

function showDashboard() {
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('dashboard').classList.remove('hidden');
  // 유저 정보 표시
  const name  = A.profile?.name  || A.user.email.split('@')[0];
  const email = A.user.email;
  const role  = A.profile?.role || 'viewer';
  document.getElementById('user-name').textContent  = name;
  document.getElementById('user-email').textContent = email;
  document.getElementById('user-avatar').textContent = name.slice(0,2).toUpperCase();
  document.getElementById('badge').textContent = A.rooms.length;
  // 역할에 따라 네비 잠금
  if (!isAdmin()) {
    document.getElementById('nav-settings').classList.add('disabled');
    document.getElementById('nav-team').classList.add('disabled');
  }
  // viewer는 공지/추가 버튼 숨김
  if (!canEdit()) {
    document.getElementById('btn-notice').classList.add('hidden');
    document.getElementById('btn-add').classList.add('hidden');
    document.getElementById('sync-btn').classList.add('hidden');
  }
  go('overview');
}

function showLoginScreen() {
  document.getElementById('dashboard').classList.add('hidden');
  document.getElementById('login-screen').classList.remove('hidden');
}

// ══════════════════════════════════════════
//  TELEGRAM (토큰은 DB app_config에서)
// ══════════════════════════════════════════
const botToken = () => A.config['tg_bot_token'] || '';
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
function go(page) {
  A.page = page;
  closeSidebar();
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  const t = { overview:'전체 현황', rooms:'채팅방 관리', notice:'전체 공지', logs:'동기화 로그', bot:'봇 모니터링', botconfig:'봇 설정', team:'팀원 관리', settings:'설정' };
  document.getElementById('page-title').textContent = t[page] || '';
  draw();
}

function draw() {
  const el = document.getElementById('content');
  const pages = { overview:pOverview, rooms:pRooms, notice:pNotice, logs:pLogs, bot:pBot, botconfig:pBotConfig, team:pTeam, settings:pSettings };
  el.innerHTML = (pages[A.page] || pOverview)();
  if (A.page === 'notice') loadNotices();
  if (A.page === 'logs')   loadLogs();
  if (A.page === 'team')      loadTeam();
  if (A.page === 'bot')       loadBotStatus();
  if (A.page === 'botconfig') loadBotConfig();
}

// ── Pages ──
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
  const { data, error } = await DB('notice_history').select('*, app_users(name,email)').order('created_at',{ascending:false}).limit(30);
  if (error) { el.innerHTML=`<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
  el.innerHTML=`<div class="table-wrap"><table><thead><tr><th>시각</th><th>발송자</th><th>대상</th><th>내용</th><th>발송</th><th>성공</th></tr></thead><tbody>
    ${!data.length?'<tr><td colspan="6" class="empty-row">기록 없음</td></tr>':data.map(h=>`<tr>
      <td style="font-size:12px;color:var(--text2)">${new Date(h.created_at).toLocaleString('ko-KR')}</td>
      <td style="font-size:12px;color:var(--text2)">${h.app_users?.name||'—'}</td>
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
  const { data, error } = await DB('sync_logs').select('*, app_users(name)').order('synced_at',{ascending:false}).limit(50);
  if (error) { el.innerHTML=`<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
  el.innerHTML=`<div class="table-wrap"><table><thead><tr><th>시각</th><th>실행자</th><th>채팅방</th><th>이전</th><th>이후</th><th>변화</th></tr></thead><tbody>
    ${!data.length?'<tr><td colspan="6" class="empty-row">없음</td></tr>':data.map(l=>{const d=l.after-l.before;return`<tr>
      <td style="font-size:12px;color:var(--text2)">${new Date(l.synced_at).toLocaleString('ko-KR')}</td>
      <td style="font-size:12px;color:var(--text2)">${l.app_users?.name||'—'}</td>
      <td style="font-weight:500">${l.room_name}</td>
      <td>${(l.before||0).toLocaleString()}</td><td>${(l.after||0).toLocaleString()}</td>
      <td style="font-weight:600;color:${d>0?'var(--green)':d<0?'var(--red)':'var(--text3)'}">${d>0?'+':''}${d}</td>
    </tr>`;}).join('')}
  </tbody></table></div>`;
}

function pTeam() {
  if (!isAdmin()) return `<div style="padding:2rem;text-align:center;color:var(--text3);font-size:13px">admin만 접근 가능합니다.</div>`;
  return `
  <div class="section-header"><span class="section-title">팀원 목록</span><button class="btn btn-sm" onclick="loadTeam()">새로고침</button></div>
  <div class="card" id="team-list"><div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div></div>
  <div style="margin-top:.75rem;font-size:12px;color:var(--text3)">
    역할 안내 — <strong style="color:var(--tg)">admin</strong>: 모든 기능 (설정·삭제 포함) &nbsp;|&nbsp;
    <strong style="color:var(--green)">editor</strong>: 공지·동기화·추가·수정 &nbsp;|&nbsp;
    <strong style="color:var(--text3)">viewer</strong>: 읽기 전용
  </div>`;
}


function pBot() {
  return `
  <div class="metrics-grid" style="grid-template-columns:repeat(2,1fr)">
    <div class="metric-card" id="bot-dart-card">
      <div class="metric-label">DART 공시 봇</div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
        <span class="dot dot-gray" id="bot-dart-dot"></span>
        <span class="metric-value" style="font-size:16px" id="bot-dart-status">확인 중...</span>
      </div>
      <div class="metric-sub" id="bot-dart-time">—</div>
    </div>
    <div class="metric-card" id="bot-news-card">
      <div class="metric-label">뉴스 봇</div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
        <span class="dot dot-gray" id="bot-news-dot"></span>
        <span class="metric-value" style="font-size:16px" id="bot-news-status">확인 중...</span>
      </div>
      <div class="metric-sub" id="bot-news-time">—</div>
    </div>
    <div class="metric-card" id="bot-price-card">
      <div class="metric-label">시세 감시 봇</div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
        <span class="dot dot-gray" id="bot-price-dot"></span>
        <span class="metric-value" style="font-size:16px" id="bot-price-status">확인 중...</span>
      </div>
      <div class="metric-sub" id="bot-price-time">—</div>
    </div>
    <div class="metric-card" id="bot-sched-card">
      <div class="metric-label">스케줄러 봇</div>
      <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
        <span class="dot dot-gray" id="bot-sched-dot"></span>
        <span class="metric-value" style="font-size:16px" id="bot-sched-status">확인 중...</span>
      </div>
      <div class="metric-sub" id="bot-sched-time">—</div>
    </div>
  </div>

  <div class="section-header" style="margin-top:.5rem">
    <span class="section-title">최근 발송 기록</span>
    <button class="btn btn-sm" onclick="loadBotStatus()">새로고침</button>
  </div>
  <div class="card" id="bot-notice-card">
    <div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>
  </div>`;
}

async function loadBotStatus() {
  const bots = [
    { key:'heartbeat_dart_bot',      dotId:'bot-dart-dot',  statusId:'bot-dart-status',  timeId:'bot-dart-time',  label:'DART 공시 봇' },
    { key:'heartbeat_news_bot',      dotId:'bot-news-dot',  statusId:'bot-news-status',  timeId:'bot-news-time',  label:'뉴스 봇' },
    { key:'heartbeat_price_bot',     dotId:'bot-price-dot', statusId:'bot-price-status', timeId:'bot-price-time', label:'시세 감시 봇' },
    { key:'heartbeat_scheduler_bot', dotId:'bot-sched-dot', statusId:'bot-sched-status', timeId:'bot-sched-time', label:'스케줄러 봇' },
  ];

  for (const bot of bots) {
    const { data } = await sb.from('app_config').select('value').eq('key', bot.key).single().catch(()=>({data:null}));
    const dotEl    = document.getElementById(bot.dotId);
    const statusEl = document.getElementById(bot.statusId);
    const timeEl   = document.getElementById(bot.timeId);
    if (!dotEl || !statusEl) continue;

    if (!data?.value) {
      dotEl.className    = 'dot dot-gray';
      statusEl.textContent = '신호 없음';
      timeEl && (timeEl.textContent = '봇이 아직 연결되지 않았습니다');
      continue;
    }

    const lastBeat = new Date(data.value);
    const diffMin  = Math.floor((Date.now() - lastBeat) / 60000);
    const timeStr  = lastBeat.toLocaleString('ko-KR', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

    if (diffMin < 3) {
      dotEl.className    = 'dot dot-green';
      statusEl.textContent = '정상 가동';
      statusEl.style.color = 'var(--green)';
    } else if (diffMin < 10) {
      dotEl.className    = 'dot dot-yellow';
      statusEl.textContent = `${diffMin}분 전`;
      statusEl.style.color = 'var(--yellow)';
    } else {
      dotEl.className    = 'dot dot-red';
      statusEl.textContent = '응답 없음';
      statusEl.style.color = 'var(--red)';
    }
    timeEl && (timeEl.textContent = `마지막 신호: ${timeStr}`);
  }

  // 최근 발송 기록
  const card = document.getElementById('bot-notice-card');
  if (card) {
    const { data } = await sb.from('notice_history').select('*').order('created_at',{ascending:false}).limit(20);
    if (!data?.length) {
      card.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:13px">발송 기록 없음 (봇 연동 후 자동 기록)</div>';
    } else {
      card.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>시각</th><th>대상</th><th>내용</th><th>성공</th></tr></thead>
        <tbody>${data.map(h=>`<tr>
          <td style="font-size:12px;color:var(--text2)">${new Date(h.created_at).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td>
          <td><span class="badge badge-cat">${h.target}</span></td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${h.content}</td>
          <td style="color:var(--green)">${h.ok_count}/${h.sent_count}</td>
        </tr>`).join('')}</tbody>
      </table></div>`;
    }
  }
}

function pBotConfig() {
  if (!isAdmin()) return `<div style="padding:2rem;text-align:center;color:var(--text3);font-size:13px">admin만 접근 가능합니다.</div>`;
  return `
  <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">AI 분석 키워드</span></div><div class="card-body">
    <div class="form-group">
      <label class="form-label">AI 트리거 키워드 (쉼표로 구분)</label>
      <textarea class="form-input" id="cfg-ai-kw" rows="3" placeholder="공급계약,임상,무상증자,..."></textarea>
      <div class="form-hint">이 키워드가 공시 제목에 포함되면 Gemini AI 분석을 실행합니다.</div>
    </div>
    <div class="form-group">
      <label class="form-label">전체 중요 키워드 (쉼표로 구분)</label>
      <textarea class="form-input" id="cfg-global-kw" rows="2" placeholder="거래정지,상장폐지,부도,..."></textarea>
      <div class="form-hint">비보유 종목도 이 키워드가 있으면 무조건 알림 발송합니다.</div>
    </div>
    <button class="btn btn-primary" onclick="saveBotKeywords()">저장</button>
  </div></div>

  <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">스케줄 ON/OFF</span></div><div class="card-body">
    <div id="schedule-list"><span class="loading"></span></div>
  </div></div>

  <div style="background:linear-gradient(135deg,rgba(42,171,238,.12),rgba(42,171,238,.04));border:1px solid rgba(42,171,238,.25);border-radius:var(--radius);padding:1rem 1.25rem">
    <div style="font-size:13px;font-weight:600;color:var(--tg);margin-bottom:.5rem">봇 서버 연동 방법</div>
    <div style="font-size:12px;color:var(--text2);line-height:1.9">
      1. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">supabase_bridge.py</code> 를 봇 폴더에 복사<br>
      2. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">pip install supabase</code> 실행<br>
      3. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">.env</code> 에 <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">SB_URL</code>, <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">SB_SERVICE_KEY</code> 추가<br>
      4. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">config.py</code> 하단에 패치 코드 추가 (bot_patch_guide.py 참고)<br>
      5. 봇 재시작 → 대시보드에서 heartbeat 확인
    </div>
  </div>`;
}

async function loadBotConfig() {
  const schedules = [
    { key:'schedule_lunch',    label:'점심 브리핑 (11:30)' },
    { key:'schedule_closing',  label:'마감 브리핑 (18:30)' },
    { key:'schedule_report',   label:'네이버 리포트 (08:50, 18:00)' },
    { key:'schedule_saturday', label:'토요일 주간 랭킹 (10:00)' },
    { key:'schedule_sunday',   label:'일요일 리포트 (10:00)' },
  ];

  // 키워드 로드
  for (const cfgKey of ['ai_trigger_keywords','global_important_keywords']) {
    const { data } = await sb.from('app_config').select('value').eq('key', cfgKey).single().catch(()=>({data:null}));
    const elId = cfgKey === 'ai_trigger_keywords' ? 'cfg-ai-kw' : 'cfg-global-kw';
    const el = document.getElementById(elId);
    if (el && data?.value) el.value = data.value;
  }

  // 스케줄 토글 로드
  const schedEl = document.getElementById('schedule-list');
  if (!schedEl) return;

  const rows = await Promise.all(schedules.map(async s => {
    const { data } = await sb.from('app_config').select('value').eq('key', s.key).single().catch(()=>({data:{value:'1'}}));
    const on = (data?.value ?? '1') !== '0';
    return { ...s, on };
  }));

  schedEl.innerHTML = rows.map(r => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px">
      <span>${r.label}</span>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" ${r.on?'checked':''} onchange="toggleSchedule('${r.key}',this.checked)" style="width:16px;height:16px;cursor:pointer">
        <span style="font-size:12px;color:${r.on?'var(--green)':'var(--text3)'}">${r.on?'ON':'OFF'}</span>
      </label>
    </div>
  `).join('') + '<div style="font-size:11px;color:var(--text3);margin-top:.75rem">변경 즉시 반영됩니다. 봇은 다음 실행 사이클에서 확인합니다.</div>';
}

async function saveBotKeywords() {
  if (!isAdmin()) { toast('권한이 없습니다.', 'error'); return; }
  const aiKw     = document.getElementById('cfg-ai-kw')?.value.trim();
  const globalKw = document.getElementById('cfg-global-kw')?.value.trim();

  try {
    if (aiKw !== undefined) {
      await sb.from('app_config').update({ value: aiKw, updated_at: new Date().toISOString() }).eq('key','ai_trigger_keywords');
    }
    if (globalKw !== undefined) {
      await sb.from('app_config').update({ value: globalKw, updated_at: new Date().toISOString() }).eq('key','global_important_keywords');
    }
    toast('키워드 저장 완료 — 봇 다음 사이클에 반영됩니다', 'success');
  } catch(e) { toast('저장 실패: ' + e.message, 'error'); }
}

async function toggleSchedule(key, enabled) {
  if (!isAdmin()) { toast('권한이 없습니다.', 'error'); return; }
  try {
    await sb.from('app_config').update({ value: enabled ? '1' : '0', updated_at: new Date().toISOString() }).eq('key', key);
    toast(`스케줄 ${enabled?'활성화':'비활성화'} 완료`, 'info');
  } catch(e) { toast('변경 실패: ' + e.message, 'error'); }
}

function pSettings() {
  if (!isAdmin()) return `<div style="padding:2rem;text-align:center;color:var(--text3);font-size:13px">admin만 설정 변경 가능합니다.</div>`;
  return `
  <div class="card" style="max-width:560px;margin-bottom:1rem"><div class="card-header"><span class="card-title">Bot 토큰 (DB 저장)</span></div><div class="card-body">
    <div class="form-group">
      <label class="form-label">Telegram Bot Token</label>
      <input class="form-input" id="cfg-token" type="password" value="${A.config['tg_bot_token']||''}" placeholder="123456789:ABCdef...">
      <div class="form-hint">@BotFather에서 발급. Supabase app_config 테이블에 저장됩니다.</div>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary" onclick="saveConfig('tg_bot_token', document.getElementById('cfg-token').value.trim()).then(()=>{ A.config['tg_bot_token']=document.getElementById('cfg-token').value.trim(); })">DB에 저장</button>
      <button class="btn" onclick="testBot()">연결 테스트</button>
    </div>
    <div id="bot-result" style="margin-top:.75rem;font-size:13px"></div>
  </div></div>

  <div class="card" style="max-width:560px;margin-bottom:1rem"><div class="card-header"><span class="card-title">앱 설정</span></div><div class="card-body">
    <div class="form-group">
      <label class="form-label">대시보드 이름</label>
      <input class="form-input" id="cfg-appname" value="${A.config['app_name']||'바티인베스트 채팅방 관리'}">
    </div>
    <button class="btn btn-primary" onclick="saveConfig('app_name', document.getElementById('cfg-appname').value.trim())">저장</button>
  </div></div>

  <div class="card" style="max-width:560px"><div class="card-header"><span class="card-title">Supabase 연결 정보</span></div><div class="card-body" style="font-size:13px;color:var(--text2);line-height:1.9">
    <p>Project URL: <code style="background:var(--bg3);padding:1px 6px;border-radius:3px;font-size:12px">${SB_URL}</code></p>
    <p style="margin-top:.5rem">연결된 유저: <strong style="color:var(--text)">${A.user?.email}</strong> (${A.profile?.role})</p>
    <p style="margin-top:.5rem;font-size:12px;color:var(--text3)">URL/Key 변경이 필요하면 index.html 상단의 SB_URL, SB_KEY를 직접 수정하세요.</p>
  </div></div>`;
}

// ══════════════════════════════════════════
//  DETAIL MODAL
// ══════════════════════════════════════════
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