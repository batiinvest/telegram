// disclosure.js — 공시 탭: 오늘 실적 공시, 전체 공시 토글
// 의존: config.js (sb)

// ── 전체 공시 토글 ──
let _allDiscLoaded = false;

function toggleAllDisclosures() {
  const panel = document.getElementById('inv-all-disclosure');
  const btn   = document.getElementById('inv-disclosure-expand-btn');
  if (!panel) return;

  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  btn.textContent = isOpen ? '+ 전체 공시' : '− 전체 공시';

  if (!isOpen && !_allDiscLoaded) {
    _allDiscLoaded = true;
    loadAllDisclosures();
  }
}

async function loadAllDisclosures() {
  const el = document.getElementById('inv-all-disclosure-list');
  if (!el) return;

  el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px"><span class="loading"></span> 공시 목록 불러오는 중...</div>`;

  const { data: cfg } = await sb.from('app_config')
    .select('value').eq('key', 'today_all_disclosures').single();

  if (!cfg?.value) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">전체 공시 데이터 없음 (매일 18:30 업데이트)</div>`;
    return;
  }

  let all = [];
  try { all = JSON.parse(cfg.value); } catch { }

  if (!all.length) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">오늘 공시 없음</div>`;
    return;
  }

  // 카테고리 분류
  const CATEGORIES = [
    { label: '사업보고서',  color: '#2AABEE', bg: 'rgba(42,171,238,.12)', match: '사업보고서' },
    { label: '반기보고서',  color: '#2dce89', bg: 'rgba(45,206,137,.12)', match: '반기보고서' },
    { label: '분기보고서',  color: '#fb6340', bg: 'rgba(251,99,64,.12)',  match: '분기보고서' },
    { label: '주요사항보고', color: '#ffd600', bg: 'rgba(255,214,0,.12)', match: '주요사항보고' },
    { label: '임원/주식',   color: '#a259ff', bg: 'rgba(162,89,255,.12)', match: ['임원', '주요주주'] },
    { label: '공정공시',    color: '#00d4aa', bg: 'rgba(0,212,170,.12)', match: '공정공시' },
    { label: '기타',        color: '#8b90a7', bg: 'rgba(139,144,167,.12)', match: null },
  ];

  const categorized = {};
  CATEGORIES.forEach(c => categorized[c.label] = []);

  all.forEach(d => {
    const nm = d.report_nm || '';
    let matched = false;
    for (const cat of CATEGORIES.slice(0, -1)) {
      const matches = Array.isArray(cat.match)
        ? cat.match.some(m => nm.includes(m))
        : nm.includes(cat.match);
      if (matches) {
        categorized[cat.label].push(d);
        matched = true;
        break;
      }
    }
    if (!matched) categorized['기타'].push(d);
  });

  const catHTML = CATEGORIES.map(cat => {
    const items = categorized[cat.label];
    if (!items.length) return '';
    return `
      <div style="padding:.75rem 1rem">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:12px;font-weight:600;padding:2px 8px;border-radius:100px;background:${cat.bg};color:${cat.color}">${cat.label}</span>
          <span style="font-size:11px;color:var(--text3)">${items.length}건</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px">
          ${items.map(d => `
            <div style="padding:5px 10px;background:var(--bg3);border-radius:var(--radius-sm);border:1px solid var(--border);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${d.report_nm}">
              ${d.corp_name}
            </div>`).join('')}
        </div>
      </div>`;
  }).join('');

  el.innerHTML = `
    ${catHTML}
    <div style="padding:4px 1rem 10px;font-size:11px;color:var(--text3)">총 ${all.length}건</div>`;
}

// ── 오늘 실적 공시 목록 ──
async function loadTodayDisclosures() {
  const el     = document.getElementById('inv-disclosure-list');
  const dateEl = document.getElementById('inv-disclosure-date');
  if (!el) return;

  // 날짜는 항상 오늘 날짜로 표시 (DB description 의존 제거)
  if (dateEl) {
    const _d = new Date();
    const today = `${_d.getFullYear()}-${String(_d.getMonth()+1).padStart(2,'0')}-${String(_d.getDate()).padStart(2,'0')}`;
    dateEl.textContent = today + ' 기준';
  }

  const { data: cfg } = await sb.from('app_config')
    .select('value,description')
    .eq('key', 'today_earnings_corps')
    .single();

  if (!cfg?.value) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">
      오늘 공시 데이터 없음 (매일 18:30 업데이트)
    </div>`;
    return;
  }

  let corps = [];
  try { corps = JSON.parse(cfg.value); } catch { }

  if (!corps.length) {
    el.innerHTML = `<div style="padding:1.25rem;text-align:center;color:var(--text3);font-size:12px">오늘 실적 공시 없음</div>`;
    return;
  }

  // 보고서 종류별 배지 색상
  const reprtColor = (nm, isAmended) => {
    const base = nm.includes('사업보고서') ? { bg:'rgba(42,171,238,.15)',  color:'#2AABEE', label:'연간' }
                : nm.includes('반기')      ? { bg:'rgba(45,206,137,.15)',  color:'#2dce89', label:'반기' }
                : nm.includes('분기')      ? { bg:'rgba(251,99,64,.15)',   color:'#fb6340', label:'분기' }
                :                            { bg:'rgba(139,144,167,.15)', color:'#8b90a7', label:'공시' };
    if (isAmended) {
      return { ...base, label: base.label + '(정정)', bg:'rgba(253,203,110,.15)', color:'#f59e0b' };
    }
    return base;
  };

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;padding:.75rem 1rem">
      ${corps.map(c => {
        const badge = reprtColor(c.report_nm || '', c.is_amended);
        return `<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--bg3);border-radius:var(--radius-sm);border:1px solid ${c.is_amended ? 'rgba(245,158,11,.3)' : 'var(--border)'}">
          <span style="font-size:10px;padding:2px 6px;border-radius:100px;background:${badge.bg};color:${badge.color};font-weight:600;white-space:nowrap">${badge.label}</span>
          <span style="font-size:13px;font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.corp_name}</span>
        </div>`;
      }).join('')}
    </div>
    <div style="padding:4px 1rem 8px;font-size:11px;color:var(--text3)">
      총 ${corps.length}개 종목 공시 · 재무 데이터 수집 완료
    </div>`;
}

