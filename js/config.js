// config.js — Supabase 연결, 전역 상수, 앱 상태
// ══════════════════════════════════════════
//  Supabase 연결 설정
//  SB_URL, SB_KEY 두 줄만 본인 값으로 교체하세요
//  SB_KEY: Supabase > Settings > API > anon public (eyJ...로 시작하는 값)
// ══════════════════════════════════════════
const SB_URL = 'https://ngyzcpogfxbkoqkcfipv.supabase.co';
const SB_KEY = 'sb_publishable_Z1NulIB63zzJABeC4eWLFw_UOyXCosq';               // ← 교체

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

// ── 재무 조회 상태 ──
const F = { mode: 'market', industry: '전체', q: '', sortBy: 'market_cap', sortDir: 'desc' };
