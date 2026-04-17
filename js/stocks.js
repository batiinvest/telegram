// stocks.js — 종목 관리 CRUD
function pStocks() {
  const industries = ['전체','바이오','반도체','2차전지','로봇','뷰티','테크','조선','신재생','엔터','소비재','우주'];
  return `
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
  </div>`;
}

async function requestBotReload() {
  if (!canEdit()) { toast('권한이 없습니다.', 'error'); return; }
  const btn = document.getElementById('reload-btn');
  if (btn) { btn.disabled = true; btn.textContent = '전송 중...'; }
  try {
    const { error } = await sb.from('app_config').upsert({
      key: 'reload_flag',
      value: String(Date.now()),
      description: '봇 종목 데이터 재로드 요청',
    }, { onConflict: 'key' });
    if (error) throw error;
    toast('✓ 재로드 요청 전송 완료 — 봇이 1분 내 반영합니다', 'success');
  } catch(e) {
    toast('전송 실패: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<svg style="width:12px;height:12px;vertical-align:middle;margin-right:3px" viewBox="0 0 16 16" fill="none"><path d="M13.5 8A5.5 5.5 0 112.5 5M2.5 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>봇 재로드'; }
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
      .select('id,name,code,industry,sub_industry,chat_id,keywords,monitoring_level,active,market')
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
      <th>종목명</th><th>코드</th><th>산업</th><th>세부분야</th>
      <th>채팅방 ID</th><th>추가 키워드</th><th>알림</th><th>관리</th>
    </tr></thead>
    <tbody>${list.map(s => `<tr>
      <td style="font-weight:500">${s.name}</td>
      <td style="font-size:12px;font-family:monospace;color:var(--text2)">${s.code||'—'}</td>
      <td><span class="badge badge-cat">${s.industry||'—'}</span></td>
      <td style="font-size:12px;color:var(--text2)">${s.sub_industry||'—'}</td>
      <td style="font-size:11px;font-family:monospace;color:var(--text3)">${s.chat_id||'—'}</td>
      <td style="font-size:12px;color:var(--text2);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.keywords||'—'}</td>
      <td>
        <span style="font-size:11px;padding:2px 7px;border-radius:100px;margin-right:4px;background:${
          s.monitoring_level==='full'?'rgba(42,171,238,.15)':
          s.monitoring_level==='news'?'rgba(45,206,137,.12)':
          'rgba(255,255,255,.05)'};color:${
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
    keywords_additional: document.getElementById('se-kw').value.trim(),
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
    keywords_additional: document.getElementById('sa-kw').value.trim(),
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

