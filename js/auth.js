// auth.js — 로그인, 회원가입, 세션 관리
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
