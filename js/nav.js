// nav.js — 페이지 라우팅, 화면 전환
function go(page) {
  A.page = page;
  closeSidebar();
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  const t = { overview:'전체 현황', rooms:'채팅방 관리', notice:'전체 공지', logs:'동기화 로그', bot:'봇 모니터링', botconfig:'봇 설정', investment:'투자 현황', screener:'종목 스크리너', financials:'재무 조회', stocks:'종목 관리', team:'팀원 관리', settings:'설정' };
  document.getElementById('page-title').textContent = t[page] || '';
  draw();
}

function draw() {
  const el = document.getElementById('content');
  const pages = { overview:pOverview, rooms:pRooms, notice:pNotice, logs:pLogs, bot:pBot, botconfig:pBotConfig, investment:pInvestment, screener:pScreener, financials:pFinancials, stocks:pStocks, team:pTeam, settings:pSettings };
  el.innerHTML = (pages[A.page] || pOverview)();
  if (A.page === 'notice') loadNotices();
  if (A.page === 'logs')   loadLogs();
  if (A.page === 'team')      loadTeam();
  if (A.page === 'bot')       loadBotStatus();
  if (A.page === 'botconfig') loadBotConfig();
  if (A.page === 'stocks')    loadStocks();
  if (A.page === 'investment') loadInvestment();
  if (A.page === 'financials') loadFinancials();
}

// ── Pages ──
