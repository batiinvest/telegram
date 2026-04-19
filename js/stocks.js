// stocks.js — 종목 관리 CRUD
const INDUSTRIES = ['바이오','반도체','2차전지','로봇','뷰티','테크','조선','신재생','엔터','소비재','우주'];
let _stocksTab = 'list'; // 'list' | 'subindustry'

function pStocks() {
  const industries = ['전체', ...INDUSTRIES];
  return `
  <div class="tabs" style="margin-bottom:1rem">
    <button class="tab ${_stocksTab==='list'?'active':''}" onclick="switchStocksTab('list')">종목 목록</button>
    <button class="tab ${_stocksTab==='subindustry'?'active':''}" onclick="switchStocksTab('subindustry')">세부분야 관리</button>
  </div>

  <!-- 종목 목록 탭 -->
  <div id="stocks-list-tab" style="${_stocksTab==='list'?'':'display:none'}">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
      <input class="search-box" id="stock-q" placeholder="종목명·코드 검색..." oninput="filterStocks()" style="max-width:200px">
      <select class="form-select" id="stock-ind" onchange="filterStocks()" style="width:130px;padding:6px 10px">
        ${industries.map(i=>`<option value="${i}">${i}</option>`).join('')}
      </select>
      <select class="form-select" id="stock-level" onchange="loadStocks()" style="width:130px;padding:6px 10px">
        <option value="monitored">모니터링 종목</option>
        <option value="full">채팅방(full)</option>
        <option value="news">뉴스/공시(news)</option>
        <option value="data">데이터만(data)</option>
        <option value="all">전체</option>
      </select>
      <select class="form-select" id="stock-active" onchange="filterStocks()" style="width:110px;padding:6px 10px">
        <option value="all">전체</option>
        <option value="true">활성화</option>
        <option value="false">비활성화</option>
      </select>
      <span style="font-size:12px;color:var(--text3)" id="stock-count"></span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn btn-sm" onclick="openModal('m-stock-add')">+ 종목 추가</button>
        <button class="btn btn-sm btn-primary" id="reload-btn" onclick="requestBotReload()" title="DB 변경사항을 봇에 반영합니다">
          <svg style="width:12px;height:12px;vertical-align:middle;margin-right:3px" viewBox="0 0 16 16" fill="none"><path d="M13.5 8A5.5 5.5 0 112.5 5M2.5 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          봇 재로드
        </button>
      </div>
    </div>
    <div class="card" id="stock-list">
      <div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>
    </div>
  </div>

  <!-- 세부분야 관리 탭 -->
  <div id="stocks-sub-tab" style="${_stocksTab==='subindustry'?'':'display:none'}">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
      <select class="form-select" id="sub-industry-select" onchange="loadSubIndustryPanel()" style="width:140px;padding:6px 10px">
        ${INDUSTRIES.map(i=>`<option value="${i}">${i}</option>`).join('')}
      </select>
      <span style="font-size:12px;color:var(--text3)" id="sub-panel-count"></span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="btn btn-sm btn-primary" onclick="openAddSubIndustry()">+ 세부분야 추가</button>
        <button class="btn btn-sm" id="reload-btn-sub" onclick="requestBotReload('reload-btn-sub')" title="DB 변경사항을 봇에 반영합니다">
          <svg style="width:12px;height:12px;vertical-align:middle;margin-right:3px" viewBox="0 0 16 16" fill="none"><path d="M13.5 8A5.5 5.5 0 112.5 5M2.5 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          봇 재로드
        </button>
      </div>
    </div>
    <div id="sub-industry-panel">
      <div style="padding:2rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>
    </div>
  </div>`;
}

function switchStocksTab(tab) {
  _stocksTab = tab;
  const listTab = document.getElementById('stocks-list-tab');
  const subTab  = document.getElementById('stocks-sub-tab');
  document.querySelectorAll('.tabs .tab').forEach((t,i) => {
    t.classList.toggle('active', (i===0 && tab==='list') || (i===1 && tab==='subindustry'));
  });
  if (listTab) listTab.style.display = tab==='list' ? '' : 'none';
  if (subTab)  subTab.style.display  = tab==='subindustry' ? '' : 'none';
  if (tab === 'subindustry') loadSubIndustryPanel();
}

// ══════════════════════════════════════════
//  세부분야(sub_industry) 관리
// ══════════════════════════════════════════
let _subStocks = []; // 현재 선택된 산업의 전체 종목

async function loadSubIndustryPanel() {
  const industry = document.getElementById('sub-industry-select')?.value;
  if (!industry) return;
  const panel = document.getElementById('sub-industry-panel');
  if (!panel) return;
  panel.innerHTML = '<div style="padding:2rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>';

  // 해당 산업의 모든 종목 로드
  let all = [], page = 0;
  while (true) {
    const { data, error } = await sb.from('companies')
      .select('id,name,code,sub_industry,monitoring_level,active')
      .eq('industry', industry)
      .order('sub_industry').order('name')
      .range(page*1000, (page+1)*1000-1);
    if (error || !data?.length) break;
    all = all.concat(data);
    if (data.length < 1000) break;
    page++;
  }
  _subStocks = all;

  // sub_industry별 그룹핑
  const groups = {};
  all.forEach(s => {
    const sub = s.sub_industry || '(미분류)';
    if (!groups[sub]) groups[sub] = [];
    groups[sub].push(s);
  });

  const cnt = document.getElementById('sub-panel-count');
  if (cnt) cnt.textContent = `${all.length}개 종목 · ${Object.keys(groups).length}개 세부분야`;

  panel.innerHTML = Object.entries(groups)
    .sort(([a],[b]) => a==='(미분류)'?1:b==='(미분류)'?-1:a.localeCompare(b,'ko'))
    .map(([sub, stocks]) => `
    <div class="card" style="margin-bottom:.75rem">
      <div class="card-header" style="gap:8px">
        <span class="card-title" style="flex:1">${sub}
          <span style="font-size:11px;font-weight:400;color:var(--text3);margin-left:6px">${stocks.length}개</span>
        </span>
        ${sub !== '(미분류)' && canEdit() ? `
        <button class="btn btn-sm" onclick="openRenameSubIndustry('${industry}','${sub.replace(/'/g,"\\'")}')">이름 변경</button>
        <button class="btn btn-sm btn-primary" onclick="openAssignCompanies('${industry}','${sub.replace(/'/g,"\\'")}')">기업 편집</button>
        ` : canEdit() ? `
        <button class="btn btn-sm btn-primary" onclick="openAssignCompanies('${industry}','')">기업 배정</button>
        ` : ''}
      </div>
      <div style="padding:.5rem 1rem .75rem;display:flex;flex-wrap:wrap;gap:6px">
        ${stocks.map(s => `
        <span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:3px 10px;border-radius:100px;background:var(--bg3);border:1px solid var(--border)">
          <span style="color:var(--text)">${s.name}</span>
          <span style="color:var(--text3);font-size:10px">${s.code||''}</span>
          ${canEdit() && sub !== '(미분류)' ? `<button onclick="removeFromSubIndustry(${s.id},'${s.name.replace(/'/g,"\\'")}','${sub.replace(/'/g,"\\'")}')" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:0;font-size:13px;line-height:1;margin-left:2px" title="이 세부분야에서 제외">×</button>` : ''}
        </span>`).join('')}
        ${stocks.length === 0 ? '<span style="font-size:12px;color:var(--text3)">종목 없음</span>' : ''}
      </div>
    </div>`).join('');
}

// 세부분야 추가
function openAddSubIndustry() {
  const industry = document.getElementById('sub-industry-select')?.value;
  if (!industry) return;
  const name = prompt(`[${industry}] 새 세부분야 이름을 입력하세요:`);
  if (!name?.trim()) return;
  openAssignCompanies(industry, name.trim(), true);
}

// 세부분야 이름 변경
async function openRenameSubIndustry(industry, oldName) {
  const newName = prompt(`세부분야 이름 변경\n현재: ${oldName}\n새 이름:`, oldName);
  if (!newName?.trim() || newName.trim() === oldName) return;
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }

  // 해당 산업+세부분야의 모든 종목 업데이트
  const targets = _subStocks.filter(s => s.sub_industry === oldName);
  if (!targets.length) { toast('해당 세부분야에 종목이 없습니다.', 'error'); return; }

  const { error } = await sb.from('companies')
    .update({ sub_industry: newName.trim() })
    .eq('industry', industry)
    .eq('sub_industry', oldName);

  if (error) { toast('변경 실패: ' + error.message, 'error'); return; }
  toast(`"${oldName}" → "${newName.trim()}" 변경 완료 (${targets.length}개 종목)`, 'success');
  loadSubIndustryPanel();
}

// 기업 배정/편집 모달
let _assignIndustry = '', _assignSub = '', _assignIsNew = false;
let _allCompanies = []; // 전체 종목 캐시 (배정 모달용)

async function openAssignCompanies(industry, sub, isNew=false) {
  _assignIndustry = industry;
  _assignSub = sub;
  _assignIsNew = isNew;

  document.getElementById('assign-sub-title').textContent =
    isNew ? `[${industry}] 새 세부분야: ${sub}` : `[${industry} › ${sub || '미분류'}] 기업 편집`;

  // 전체 종목 로드 (캐시 없으면 DB 조회)
  const list = document.getElementById('assign-company-list');
  list.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>';
  openModal('m-assign-sub');

  if (!_allCompanies.length) {
    let all = [], page = 0;
    while (true) {
      const { data } = await sb.from('companies')
        .select('id,name,code,industry,sub_industry')
        .order('industry').order('name')
        .range(page*1000, (page+1)*1000-1);
      if (!data?.length) break;
      all = all.concat(data);
      if (data.length < 1000) break;
      page++;
    }
    _allCompanies = all;
  }

  // 검색창 초기화
  const searchEl = document.querySelector('#m-assign-sub input[type=text], #m-assign-sub input:not([type=checkbox])');
  if (searchEl) searchEl.value = '';

  renderAssignList('');
}

function renderAssignList(q) {
  const list = document.getElementById('assign-company-list');
  if (!list) return;
  const keyword = q.toLowerCase();

  // 전체 종목에서 검색 (현재 산업 종목 + 미분류 종목 모두 포함)
  const stocks = _allCompanies.filter(s => {
    if (!keyword) return true;
    return s.name.toLowerCase().includes(keyword) || (s.code||'').toLowerCase().includes(keyword);
  });

  // 정렬: 현재 산업 먼저, 그 다음 미분류, 그 다음 다른 산업
  stocks.sort((a, b) => {
    const aScore = a.industry === _assignIndustry ? 0 : !a.industry ? 1 : 2;
    const bScore = b.industry === _assignIndustry ? 0 : !b.industry ? 1 : 2;
    if (aScore !== bScore) return aScore - bScore;
    return a.name.localeCompare(b.name, 'ko');
  });

  list.innerHTML = stocks.map(s => {
    const inSub = s.sub_industry === _assignSub && s.industry === _assignIndustry;
    const isOtherSub = s.sub_industry && !inSub;
    const isDiffIndustry = s.industry && s.industry !== _assignIndustry;
    const isUnassigned = !s.industry && !s.sub_industry;

    return `
    <label style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);cursor:pointer">
      <input type="checkbox" value="${s.id}" ${inSub?'checked':''}
        style="width:15px;height:15px;flex-shrink:0">
      <span style="flex:1;font-size:13px">${s.name}</span>
      <span style="font-size:11px;font-family:monospace;color:var(--text3)">${s.code||''}</span>
      ${isUnassigned
        ? `<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:rgba(255,255,255,.06);color:var(--text3)">미분류</span>`
        : isDiffIndustry
        ? `<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:rgba(255,255,255,.06);color:var(--text3)">${s.industry}</span>`
        : isOtherSub
        ? `<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:rgba(251,99,64,.15);color:var(--yellow)">${s.sub_industry}</span>`
        : inSub
        ? `<span style="font-size:10px;padding:1px 6px;border-radius:100px;background:rgba(42,171,238,.12);color:var(--tg)">현재</span>`
        : ''}
    </label>`;
  }).join('') || '<div style="padding:1rem;color:var(--text3);font-size:13px;text-align:center">종목 없음</div>';
}



async function saveAssign() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const checkboxes = document.querySelectorAll('#assign-company-list input[type=checkbox]');
  const toAdd    = [];  // sub_industry + 필요시 industry도 세팅
  const toRemove = [];  // sub_industry 해제

  checkboxes.forEach(cb => {
    const id = parseInt(cb.value);
    const s  = _allCompanies.find(x => x.id === id);
    if (!s) return;
    const wasIn = s.sub_industry === _assignSub && s.industry === _assignIndustry;
    if (cb.checked && !wasIn) toAdd.push(s);
    if (!cb.checked && wasIn) toRemove.push(id);
  });

  let ok = true;

  // 추가: industry 없는 종목은 industry도 함께 세팅
  // monitoring_level이 'data'인 종목은 'news'로 자동 업그레이드
  if (toAdd.length) {
    const needIndustry      = toAdd.filter(s => !s.industry || s.industry !== _assignIndustry);
    const alreadyInIndustry = toAdd.filter(s => s.industry === _assignIndustry);

    if (needIndustry.length) {
      const { error } = await sb.from('companies')
        .update({
          industry: _assignIndustry,
          sub_industry: _assignSub,
          // data 레벨만 news로 업그레이드 (full은 유지)
          // → 일괄 업데이트라 개별 체크 불가, 조건을 분리해서 처리
        })
        .in('id', needIndustry.map(s => s.id));
      if (error) { toast('배정 실패: ' + error.message, 'error'); ok = false; }

      // data 레벨 종목만 news로 업그레이드
      const dataIds = needIndustry.filter(s => !s.monitoring_level || s.monitoring_level === 'data').map(s => s.id);
      if (dataIds.length) {
        await sb.from('companies').update({ monitoring_level: 'news', is_monitored: true }).in('id', dataIds);
      }
    }
    if (alreadyInIndustry.length) {
      const { error } = await sb.from('companies')
        .update({ sub_industry: _assignSub })
        .in('id', alreadyInIndustry.map(s => s.id));
      if (error) { toast('배정 실패: ' + error.message, 'error'); ok = false; }

      const dataIds = alreadyInIndustry.filter(s => !s.monitoring_level || s.monitoring_level === 'data').map(s => s.id);
      if (dataIds.length) {
        await sb.from('companies').update({ monitoring_level: 'news', is_monitored: true }).in('id', dataIds);
      }
    }
  }

  if (toRemove.length) {
    const { error } = await sb.from('companies')
      .update({ sub_industry: null })
      .in('id', toRemove);
    if (error) { toast('제외 실패: ' + error.message, 'error'); ok = false; }
  }

  if (ok) {
    const upgradedCount = toAdd.filter(s => !s.monitoring_level || s.monitoring_level === 'data').length;
    const msg = upgradedCount > 0
      ? `저장 완료 — 추가 ${toAdd.length}개, 제외 ${toRemove.length}개 (${upgradedCount}개 뉴스/공시 자동 업그레이드)`
      : `저장 완료 — 추가 ${toAdd.length}개, 제외 ${toRemove.length}개`;
    toast(msg, 'success');
    _allCompanies = []; // 캐시 무효화 (다음 열 때 재조회)
    closeModal('m-assign-sub');
    loadSubIndustryPanel();
  }
}

// 개별 기업 세부분야 제외 (× 버튼)
async function removeFromSubIndustry(id, name, sub) {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  if (!confirm(`"${name}"을(를) [${sub}]에서 제외할까요?\n(종목 자체는 삭제되지 않습니다)`)) return;
  const { error } = await sb.from('companies').update({ sub_industry: null }).eq('id', id);
  if (error) { toast('실패: ' + error.message, 'error'); return; }
  toast(`${name} 제외 완료`, 'info');
  loadSubIndustryPanel();
}

async function requestBotReload(btnId = 'reload-btn') {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const btn = document.getElementById(btnId);
  const origHTML = btn?.innerHTML;
  if (btn) { btn.disabled = true; btn.textContent = '전송 중...'; }
  try {
    const { error } = await sb.from('app_config')
      .update({ value: String(Date.now()) })
      .eq('key', 'reload_flag');
    if (error) throw error;
    toast('✓ 재로드 요청 전송 완료 — 봇이 1분 내 반영합니다', 'success');
  } catch(e) {
    toast('전송 실패: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origHTML; }
  }
}

let _allStocks = [];

async function loadStocks() {
  const el = document.getElementById('stock-list');
  if (!el) return;
  el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3)"><span class="loading"></span></div>';

  const level = document.getElementById('stock-level')?.value || 'monitored';
  let allData = [];
  let page = 0;
  const pageSize = 1000;

  while (true) {
    let q = sb.from('companies')
      .select('id,name,code,industry,sub_industry,sector,chat_id,keywords,monitoring_level,active,market')
      .order('name')
      .range(page * pageSize, (page + 1) * pageSize - 1);

    if (level === 'monitored') {
      q = q.in('monitoring_level', ['full', 'news']);
    } else if (level !== 'all') {
      q = q.eq('monitoring_level', level);
    }

    const { data, error } = await q;
    if (error) { el.innerHTML = `<div style="padding:1rem;color:var(--red);font-size:13px">${error.message}</div>`; return; }
    if (!data?.length) break;
    allData = allData.concat(data);
    if (data.length < pageSize) break;
    page++;
  }
  _allStocks = allData;
  filterStocks();
}

function filterStocks() {
  const q     = (document.getElementById('stock-q')?.value || '').toLowerCase();
  const ind   = document.getElementById('stock-ind')?.value || '전체';
  const act   = document.getElementById('stock-active')?.value || 'all';
  const filtered = _allStocks.filter(s => {
    const matchQ   = !q || s.name.toLowerCase().includes(q) || (s.code||'').toLowerCase().includes(q);
    const matchInd = ind === '전체' || s.industry === ind;
    const matchAct = act === 'all' || String(s.active) === act;
    return matchQ && matchInd && matchAct;
  });
  renderStocks(filtered);
}

function renderStocks(list) {
  const el = document.getElementById('stock-list');
  if (!el) return;
  const cnt = document.getElementById('stock-count');
  if (cnt) cnt.textContent = `${list.length}개`;
  if (!list.length) { el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:13px">종목 없음</div>'; return; }

  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>종목명</th><th>코드</th><th>산업</th><th>세부분야</th><th>업종</th>
      <th class="stock-col-chatid">채팅방 ID</th><th class="stock-col-keyword">키워드</th><th>모니터링</th><th>관리</th>
    </tr></thead>
    <tbody>${list.map(s => `<tr>
      <td style="font-weight:600;font-size:13px">${s.name}</td>
      <td style="font-size:12px;font-family:monospace;color:var(--text2)">${s.code||'—'}</td>
      <td><span class="badge badge-cat">${s.industry||'—'}</span></td>
      <td style="font-size:12px;color:var(--text2)">${s.sub_industry||'—'}</td>
      <td style="font-size:11px;color:var(--text2);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${s.sector||''}">${s.sector||'—'}</td>
      <td class="stock-col-chatid" style="font-size:11px;font-family:monospace;color:var(--text3);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.chat_id||'—'}</td>
      <td class="stock-col-keyword" style="font-size:12px;color:var(--text2);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.keywords||'—'}</td>
      <td>
        <span style="font-size:11px;font-weight:500;padding:3px 8px;border-radius:100px;background:${
          s.monitoring_level==='full'?'rgba(42,171,238,.15)':
          s.monitoring_level==='news'?'rgba(45,206,137,.12)':
          'rgba(255,255,255,.04)'};color:${
          s.monitoring_level==='full'?'var(--tg)':
          s.monitoring_level==='news'?'var(--green)':
          'var(--text3)'}">
          ${s.monitoring_level==='full'?'채팅방':s.monitoring_level==='news'?'뉴스/공시':'데이터만'}
        </span>
      </td>
      <td><div style="display:flex;gap:4px">
        <button class="btn btn-sm" onclick="openStockEdit(${s.id})">수정</button>
        <button class="btn btn-sm btn-danger" onclick="deleteStock(${s.id},'${s.name.replace(/'/g,"\\'")}')">삭제</button>
      </div></td>
    </tr>`).join('')}
    </tbody></table></div>`;
}

async function openStockEdit(id) {
  const s = _allStocks.find(x => x.id === id);
  if (!s) return;
  document.getElementById('se-id').value            = s.id;
  document.getElementById('se-name').value          = s.name;
  document.getElementById('se-code').value          = s.code || '';
  document.getElementById('se-industry').value      = s.industry || '';
  document.getElementById('se-sub').value           = s.sub_industry || '';
  document.getElementById('se-chatid').value        = s.chat_id || '';
  document.getElementById('se-kw').value        = s.keywords || '';
  document.getElementById('se-active').checked      = s.active !== false;
  document.getElementById('se-level').value          = s.monitoring_level || 'data';
  openModal('m-stock-edit');
}

async function saveStockEdit() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const id      = parseInt(document.getElementById('se-id').value);
  const oldData = _allStocks.find(x => x.id === id);
  const newName = document.getElementById('se-name').value.trim();
  const payload = {
    name:                newName,
    code:                document.getElementById('se-code').value.trim(),
    industry:            document.getElementById('se-industry').value.trim(),
    sub_industry:        document.getElementById('se-sub').value.trim(),
    chat_id:             document.getElementById('se-chatid').value.trim() || null,
    keywords: document.getElementById('se-kw').value.trim(),
    keywords_related:    ''.trim(),
    active:              document.getElementById('se-active').checked,
    monitoring_level:    document.getElementById('se-level').value,
    is_monitored:        ['full','news'].includes(document.getElementById('se-level').value),
  };
  if (!payload.name) { toast('종목명은 필수입니다.', 'error'); return; }

  const { error } = await sb.from('companies').update(payload).eq('id', id);
  if (error) { toast('수정 실패: ' + error.message, 'error'); return; }

  // 종목명 변경 + 채팅방 있는 경우 → 채팅방 이름 변경 제안
  const nameChanged = oldData && oldData.name !== newName;
  const hasChatId   = payload.chat_id;
  if (nameChanged && hasChatId) {
    const doRename = confirm(`채팅방 이름도 "${newName}"으로 변경할까요?\n(봇이 채팅방 관리자여야 합니다)`);
    if (doRename) {
      await renameTelegramChat(hasChatId, newName);
    }
  }

  const idx = _allStocks.findIndex(x => x.id === id);
  if (idx >= 0) _allStocks[idx] = { ..._allStocks[idx], ...payload };
  closeModal('m-stock-edit');
  filterStocks();
  toast('수정 완료', 'success');
}

async function renameTelegramChat(chatId, newTitle) {
  const { data: cfg } = await sb.from('app_config').select('value').eq('key','tg_bot_token').single();
  const token = cfg?.value;
  if (!token) { toast('봇 토큰 없음 — 설정 페이지에서 입력해주세요.', 'error'); return; }

  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/setChatTitle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, title: newTitle }),
    });
    const data = await res.json();
    if (data.ok) {
      toast(`✓ 채팅방 이름이 "${newTitle}"으로 변경됐습니다.`, 'success');
    } else {
      toast(`채팅방 이름 변경 실패: ${data.description}`, 'error');
    }
  } catch(e) {
    toast('채팅방 이름 변경 오류: ' + e.message, 'error');
  }
}

async function addStock() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const payload = {
    name:                document.getElementById('sa-name').value.trim(),
    code:                document.getElementById('sa-code').value.trim(),
    industry:            document.getElementById('sa-industry').value,
    sub_industry:        document.getElementById('sa-sub').value.trim(),
    chat_id:             document.getElementById('sa-chatid').value.trim() || null,
    keywords: document.getElementById('sa-kw').value.trim(),
    keywords_related:    ''.trim(),
    active:              true,
  };
  if (!payload.name || !payload.code) { toast('종목명과 코드는 필수입니다.', 'error'); return; }
  const { data, error } = await sb.from('companies').insert([payload]).select().single();
  if (error) { toast('추가 실패: ' + error.message, 'error'); return; }
  _allStocks.push(data);
  _allStocks.sort((a,b) => a.name.localeCompare(b.name, 'ko'));
  closeModal('m-stock-add');
  filterStocks();
  toast(`${payload.name} 추가 완료`, 'success');
}

async function deleteStock(id, name) {
  if (!canDel()) { toast('admin만 삭제 가능합니다.', 'error'); return; }
  if (!confirm(`"${name}" 종목을 삭제할까요?\n봇 알림도 중단됩니다.`)) return;
  const { error } = await sb.from('companies').delete().eq('id', id);
  if (error) { toast('삭제 실패: ' + error.message, 'error'); return; }
  _allStocks = _allStocks.filter(x => x.id !== id);
  filterStocks();
  toast(`${name} 삭제됨`, 'info');
}

