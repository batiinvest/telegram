// bots.js — 봇 모니터링, 봇 설정



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
    { key:'heartbeat_dart_bot',      dotId:'bot-dart-dot',  statusId:'bot-dart-status',  timeId:'bot-dart-time' },
    { key:'heartbeat_news_bot',      dotId:'bot-news-dot',  statusId:'bot-news-status',  timeId:'bot-news-time' },
    { key:'heartbeat_price_bot',     dotId:'bot-price-dot', statusId:'bot-price-status', timeId:'bot-price-time' },
    { key:'heartbeat_scheduler_bot', dotId:'bot-sched-dot', statusId:'bot-sched-status', timeId:'bot-sched-time' },
  ];

  // app_config 전체를 한 번에 조회 (single() 오류 방지)
  const { data: cfgAll, error: cfgErr } = await sb.from('app_config').select('key,value').in('key', bots.map(b=>b.key));

  const cfgMap = {};
  (cfgAll || []).forEach(r => cfgMap[r.key] = r.value);

  for (const bot of bots) {
    const dotEl    = document.getElementById(bot.dotId);
    const statusEl = document.getElementById(bot.statusId);
    const timeEl   = document.getElementById(bot.timeId);
    if (!dotEl || !statusEl) continue;

    const val = cfgMap[bot.key];

    if (!val) {
      dotEl.className      = 'dot dot-gray';
      statusEl.textContent = '신호 없음';
      statusEl.style.color = 'var(--text3)';
      if (timeEl) timeEl.textContent = 'add_bot_config.sql 실행 후 봇 재시작 필요';
      continue;
    }

    const lastBeat = new Date(val);
    const diffMin  = Math.floor((Date.now() - lastBeat) / 60000);
    const timeStr  = lastBeat.toLocaleString('ko-KR', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});

    if (diffMin < 5) {
      dotEl.className      = 'dot dot-green';
      statusEl.textContent = '정상 가동';
      statusEl.style.color = 'var(--green)';
    } else if (diffMin < 30) {
      dotEl.className      = 'dot dot-yellow';
      statusEl.textContent = `${diffMin}분 전 신호`;
      statusEl.style.color = 'var(--yellow)';
    } else {
      dotEl.className      = 'dot dot-red';
      statusEl.textContent = '응답 없음';
      statusEl.style.color = 'var(--red)';
    }
    if (timeEl) timeEl.textContent = `마지막 신호: ${timeStr}`;
  }

  // 최근 발송 기록
  const card = document.getElementById('bot-notice-card');
  if (!card) return;

  try {
    const { data, error } = await sb.from('notice_history').select('*').order('created_at',{ascending:false}).limit(20);
    if (error) throw error;
    if (!data || !data.length) {
      card.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--text3);font-size:13px">발송 기록 없음 — 봇 연동 후 자동으로 기록됩니다.</div>';
      return;
    }
    card.innerHTML = '<div class="table-wrap"><table><thead><tr><th>시각</th><th>대상</th><th>내용</th><th>성공</th></tr></thead><tbody>' +
      data.map(h => '<tr>' +
        '<td style="font-size:12px;color:var(--text2)">' + new Date(h.created_at).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) + '</td>' +
        '<td><span class="badge badge-cat">' + (h.target||'—') + '</span></td>' +
        '<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">' + (h.content||'') + '</td>' +
        '<td style="color:var(--green)">' + (h.ok_count||0) + '/' + (h.sent_count||0) + '</td>' +
      '</tr>').join('') +
    '</tbody></table></div>';
  } catch(e) {
    card.innerHTML = '<div style="padding:1rem;color:var(--red);font-size:13px">조회 실패: ' + e.message + '</div>';
  }
}

function pBotConfig() {
  if (!isAdmin()) return `<div style="padding:2rem;text-align:center;color:var(--text3);font-size:13px">admin만 접근 가능합니다.</div>`;
  return `
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem">
    <div class="tabs" style="margin-bottom:0" id="botcfg-tabs">
      <button class="tab active" onclick="switchBotCfgTab('keywords',this)">키워드 설정</button>
      <button class="tab" onclick="switchBotCfgTab('news-filter',this)">뉴스 필터</button>
      <button class="tab" onclick="switchBotCfgTab('dart-level',this)">공시 등급</button>
      <button class="tab" onclick="switchBotCfgTab('schedule',this)">스케줄</button>
      <button class="tab" onclick="switchBotCfgTab('news-terms',this)">산업별 검색어</button>
    </div>
    <button class="btn btn-sm btn-primary" id="botcfg-reload-btn" onclick="requestBotReload('botcfg-reload-btn')" title="저장한 설정을 봇에 즉시 반영합니다">
      <svg style="width:12px;height:12px;vertical-align:middle;margin-right:3px" viewBox="0 0 16 16" fill="none"><path d="M13.5 8A5.5 5.5 0 112.5 5M2.5 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      봇 재로드
    </button>
  </div>

  <!-- 키워드 설정 탭 -->
  <div id="botcfg-keywords">
    <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">AI 분석 키워드</span></div><div class="card-body">
      <div class="form-group">
        <label class="form-label">AI 트리거 키워드 <span style="font-size:11px;color:var(--text3)">(쉼표로 구분)</span></label>
        <textarea class="form-input" id="cfg-ai-kw" rows="3" placeholder="공급계약,임상,무상증자,..."></textarea>
        <div class="form-hint">이 키워드가 공시 제목에 포함되면 Gemini AI 분석을 실행합니다.</div>
      </div>
      <div class="form-group">
        <label class="form-label">전체 중요 키워드 <span style="font-size:11px;color:var(--text3)">(쉼표로 구분)</span></label>
        <textarea class="form-input" id="cfg-global-kw" rows="2" placeholder="거래정지,상장폐지,부도,..."></textarea>
        <div class="form-hint">비보유 종목도 이 키워드가 있으면 무조건 알림 발송합니다.</div>
      </div>
      <button class="btn btn-primary" onclick="saveBotKeywords()">저장</button>
    </div></div>
  </div>

  <!-- 뉴스 필터 탭 -->
  <div id="botcfg-news-filter" style="display:none">
    <div class="card" style="margin-bottom:1rem">
      <div class="card-header">
        <span class="card-title">스팸 패턴 <span style="font-size:11px;font-weight:400;color:var(--text3)">— 제목에 포함 시 발송 차단</span></span>
      </div>
      <div class="card-body">
        <div class="form-group">
          <label class="form-label">패턴 목록 <span style="font-size:11px;color:var(--text3)">(한 줄에 하나씩, 정규식 가능)</span></label>
          <textarea class="form-input" id="cfg-spam-patterns" rows="10" placeholder="매수.*위&#10;급등.*예고&#10;순매수.*위"></textarea>
          <div class="form-hint">예: <code>매수.*위</code> → "매수 1위", "매수 3위" 등 모두 차단</div>
        </div>
        <button class="btn btn-primary" onclick="saveNewsFilter('news_spam_patterns', 'cfg-spam-patterns', '\\n')">저장</button>
      </div>
    </div>

    <div class="card" style="margin-bottom:1rem">
      <div class="card-header">
        <span class="card-title">실질 보도 키워드 <span style="font-size:11px;font-weight:400;color:var(--text3)">— 하나도 없으면 발송 안 함</span></span>
      </div>
      <div class="card-body">
        <div class="form-group">
          <label class="form-label">키워드 목록 <span style="font-size:11px;color:var(--text3)">(쉼표로 구분)</span></label>
          <textarea class="form-input" id="cfg-meaningful-kw" rows="6" placeholder="계약,수주,실적,임상,특허,인수..."></textarea>
          <div class="form-hint">이 중 하나라도 제목/본문에 있어야 발송합니다. 없으면 단순 언급으로 판단해 스킵.</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-primary" onclick="saveNewsFilter('news_meaningful_keywords', 'cfg-meaningful-kw', ',')">저장</button>
          <span style="font-size:11px;color:var(--text3)">저장 후 봇 재로드 버튼을 눌러주세요.</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 공시 등급 탭 -->
  <div id="botcfg-dart-level" style="display:none">
    <div style="font-size:12px;color:var(--text2);margin-bottom:1rem;padding:10px 14px;background:var(--bg3);border-radius:var(--radius-sm);border:1px solid var(--border)">
      공시 제목에 키워드가 포함되면 해당 등급으로 분류됩니다. 쉼표로 구분하며 저장 후 봇 재로드 시 반영됩니다.<br>
      <b style="color:var(--red)">긴급</b> → 메인+산업+기업 &nbsp;|&nbsp; <b style="color:var(--green)">중요</b> → 산업+기업 &nbsp;|&nbsp; <b style="color:var(--text2)">일반</b> → 산업+기업 &nbsp;|&nbsp; <b style="color:var(--text3)">잡공시</b> → 기업채널만
    </div>

    <div class="card" style="margin-bottom:.75rem">
      <div class="card-header">
        <span class="card-title" style="color:var(--red)">🚨 긴급 키워드</span>
        <span style="font-size:11px;color:var(--text3)">메인 + 산업 + 기업채널</span>
      </div>
      <div class="card-body">
        <textarea class="form-input" id="cfg-dart-urgent" rows="3"
          placeholder="거래정지,횡령,배임,상장폐지,불성실,공개매수,영업정지"></textarea>
        <div style="display:flex;gap:8px;margin-top:.75rem;align-items:center">
          <button class="btn btn-primary btn-sm" onclick="saveDartLevel('dart_urgent','cfg-dart-urgent')">저장</button>
          <span style="font-size:11px;color:var(--text3)">우선순위 가장 높음 — 중요/잡공시 키워드와 중복 시 긴급 우선</span>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:.75rem">
      <div class="card-header">
        <span class="card-title" style="color:var(--green)">📈 중요 키워드</span>
        <span style="font-size:11px;color:var(--text3)">산업 + 기업채널</span>
      </div>
      <div class="card-body">
        <textarea class="form-input" id="cfg-dart-major" rows="4"
          placeholder="공급계약,수주,잠정실적,무상증자,유상증자,최대주주변경,합병,분할,인수,전환사채,소송,특허,임상,사업보고서"></textarea>
        <div style="margin-top:.75rem">
          <button class="btn btn-primary btn-sm" onclick="saveDartLevel('dart_major','cfg-dart-major')">저장</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span class="card-title" style="color:var(--text3)">📊 잡공시 키워드</span>
        <span style="font-size:11px;color:var(--text3)">기업채널만 (산업/메인 발송 안 함)</span>
      </div>
      <div class="card-body">
        <textarea class="form-input" id="cfg-dart-skip" rows="4"
          placeholder="소유상황보고,기업설명회,IR개최,감사보고서,주주총회소집,의결권대리,증권발행실적,투자설명서,자기주식취득결과,자기주식처분결과"></textarea>
        <div style="margin-top:.75rem">
          <button class="btn btn-primary btn-sm" onclick="saveDartLevel('dart_skip','cfg-dart-skip')">저장</button>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:.75rem">
      <div class="card-header">
        <span class="card-title">🚫 기업 블랙리스트 <span style="font-size:11px;font-weight:400;color:var(--text3)">— 모든 채널 차단</span></span>
      </div>
      <div class="card-body">
        <div class="form-group">
          <label class="form-label">기업명 <span style="font-size:11px;color:var(--text3)">(쉼표로 구분)</span></label>
          <textarea class="form-input" id="cfg-dart-blacklist" rows="3" placeholder="삼성전자,SK하이닉스,..."></textarea>
          <div class="form-hint">여기 입력된 기업의 공시는 기업채널 포함 모든 채널에 발송하지 않습니다.</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-primary btn-sm" onclick="saveDartLevel('dart_blacklist','cfg-dart-blacklist')">저장</button>
          <span style="font-size:11px;color:var(--text3)">저장 후 봇 재로드 버튼을 눌러주세요.</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">채널 라우팅 기준</span></div>
      <div class="card-body" style="font-size:12px;color:var(--text2);line-height:2">
        <div style="display:grid;grid-template-columns:auto 1fr;gap:4px 16px">
          <span style="color:var(--red);font-weight:600">🚨 긴급</span><span>거래정지·횡령·배임·상장폐지·불성실 → <b>메인 + 산업 + 기업채널</b></span>
          <span style="color:var(--green);font-weight:600">📈 중요</span><span>공급계약·수주·실적·증자·합병 등 → <b>산업 + 기업채널</b></span>
          <span style="color:var(--text2)">📄 일반</span><span>그 외 공시 → <b>산업 + 기업채널</b></span>
          <span style="color:var(--text3)">📊 잡공시</span><span>소유상황·IR·감사보고서 등 → <b>기업채널만</b></span>
          <span style="color:var(--red)">🚫 블랙리스트</span><span>지정 기업 → <b>모든 채널 차단</b></span>
        </div>
      </div>
    </div>
  </div>

  <!-- 스케줄 탭 -->
  <div id="botcfg-schedule" style="display:none">
    <div class="card" style="margin-bottom:1rem"><div class="card-header"><span class="card-title">스케줄 ON/OFF</span></div><div class="card-body">
      <div id="schedule-list"><span class="loading"></span></div>
    </div></div>
  </div>

  <!-- 산업별 검색어 탭 -->
  <div id="botcfg-news-terms" style="display:none">
    <div class="card" style="margin-bottom:1rem"><div class="card-header">
      <span class="card-title">산업별 뉴스 검색어</span>
      <span style="font-size:11px;color:var(--text3)">쉼표로 구분 — 저장 즉시 봇 다음 사이클에 반영</span>
    </div><div class="card-body">
      <div id="news-terms-list"><span class="loading"></span></div>
    </div></div>
  </div>

  <div style="background:linear-gradient(135deg,rgba(42,171,238,.12),rgba(42,171,238,.04));border:1px solid rgba(42,171,238,.25);border-radius:var(--radius);padding:1rem 1.25rem">
    <div style="font-size:13px;font-weight:600;color:var(--tg);margin-bottom:.5rem">봇 서버 연동 방법</div>
    <div style="font-size:12px;color:var(--text2);line-height:1.9">
      1. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">supabase_bridge.py</code> 를 봇 폴더에 복사<br>
      2. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">pip install supabase</code> 실행<br>
      3. <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">.env</code> 에 <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">SB_URL</code>, <code style="background:var(--bg3);padding:1px 6px;border-radius:3px">SB_SERVICE_KEY</code> 추가<br>
      4. 봇 재시작 → 대시보드에서 heartbeat 확인
    </div>
  </div>`;
}

function switchBotCfgTab(tab, el) {
  ['keywords','news-filter','dart-level','schedule','news-terms'].forEach(t => {
    document.getElementById(`botcfg-${t}`).style.display = t === tab ? '' : 'none';
  });
  document.querySelectorAll('#botcfg-tabs .tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');

  if (tab === 'schedule') loadSchedules();
  if (tab === 'news-terms') loadNewsTerms();
  if (tab === 'news-filter') loadNewsFilter();
  if (tab === 'dart-level') loadDartLevel();
}

async function loadBotConfig() {
  // 키워드 탭 로드
  const allKeys = ['ai_trigger_keywords', 'global_important_keywords'];
  const { data: cfgRows } = await sb.from('app_config').select('key,value').in('key', allKeys);
  const cfg = {};
  (cfgRows || []).forEach(r => cfg[r.key] = r.value);

  const aiEl = document.getElementById('cfg-ai-kw');
  const glEl = document.getElementById('cfg-global-kw');
  if (aiEl && cfg['ai_trigger_keywords']) aiEl.value = cfg['ai_trigger_keywords'];
  if (glEl && cfg['global_important_keywords']) glEl.value = cfg['global_important_keywords'];

  // 스케줄은 탭 전환 시 로드
  loadSchedules();
}

async function loadSchedules() {
  const schedules = [
    { key:'schedule_lunch',    label:'점심 브리핑 (11:30)' },
    { key:'schedule_closing',  label:'마감 브리핑 (18:30)' },
    { key:'schedule_report',   label:'네이버 리포트 (08:50, 18:00)' },
    { key:'schedule_saturday', label:'토요일 주간 랭킹 (10:00)' },
    { key:'schedule_sunday',   label:'일요일 리포트 (10:00)' },
  ];
  const { data: cfgRows } = await sb.from('app_config').select('key,value').in('key', schedules.map(s => s.key));
  const cfg = {};
  (cfgRows || []).forEach(r => cfg[r.key] = r.value);

  const schedEl = document.getElementById('schedule-list');
  if (!schedEl) return;
  schedEl.innerHTML = schedules.map(s => {
    const on = (cfg[s.key] ?? '1') !== '0';
    return `<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);font-size:13px">
      <span>${s.label}</span>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <span style="font-size:12px;color:${on?'var(--green)':'var(--text3)'}">${on?'ON':'OFF'}</span>
        <input type="checkbox" ${on?'checked':''} onchange="toggleSchedule('${s.key}',this.checked)" style="width:16px;height:16px;cursor:pointer">
      </label>
    </div>`;
  }).join('') +
  '<div style="font-size:11px;color:var(--text3);margin-top:.75rem">변경 즉시 반영됩니다. 봇은 다음 사이클에서 확인합니다.</div>';
}

async function loadDartLevel() {
  const keys = ['dart_urgent', 'dart_major', 'dart_skip', 'dart_blacklist'];
  const { data } = await sb.from('app_config').select('key,value').in('key', keys);
  const map = {};
  (data || []).forEach(r => map[r.key] = r.value);

  const urgentEl = document.getElementById('cfg-dart-urgent');
  const majorEl  = document.getElementById('cfg-dart-major');
  const skipEl   = document.getElementById('cfg-dart-skip');
  const blEl     = document.getElementById('cfg-dart-blacklist');
  if (urgentEl && map['dart_urgent'])    urgentEl.value = map['dart_urgent'];
  if (majorEl  && map['dart_major'])     majorEl.value  = map['dart_major'];
  if (skipEl   && map['dart_skip'])      skipEl.value   = map['dart_skip'];
  if (blEl     && map['dart_blacklist']) blEl.value     = map['dart_blacklist'];
}

async function saveDartLevel(key, elId) {
  if (!isAdmin()) { toast('admin만 수정 가능합니다.', 'error'); return; }
  const el = document.getElementById(elId);
  if (!el) return;
  const value = el.value.trim();
  const { error } = await sb.from('app_config')
    .update({ value, updated_at: new Date().toISOString() })
    .eq('key', key);
  if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  toast('저장 완료 — 봇 재로드 후 반영됩니다', 'success');
}
async function loadNewsFilter() {
  const keys = ['news_spam_patterns', 'news_meaningful_keywords'];
  const { data } = await sb.from('app_config').select('key,value').in('key', keys);
  const map = {};
  (data || []).forEach(r => map[r.key] = r.value);

  const spamEl = document.getElementById('cfg-spam-patterns');
  const kwEl   = document.getElementById('cfg-meaningful-kw');
  if (spamEl && map['news_spam_patterns'])       spamEl.value = map['news_spam_patterns'];
  if (kwEl   && map['news_meaningful_keywords']) kwEl.value   = map['news_meaningful_keywords'];
}

async function saveNewsFilter(key, elId, separator) {
  if (!isAdmin()) { toast('admin만 수정 가능합니다.', 'error'); return; }
  const el = document.getElementById(elId);
  if (!el) return;
  const value = el.value.trim();
  const { error } = await sb.from('app_config')
    .update({ value, updated_at: new Date().toISOString() })
    .eq('key', key);
  if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  toast('저장 완료 — 봇 재로드 후 반영됩니다', 'success');
}

const NEWS_INDUSTRIES = ['2차전지','반도체','로봇','조선','뷰티','엔터','신재생','바이오','테크','소비재','우주'];

async function loadNewsTerms() {
  const el = document.getElementById('news-terms-list');
  if (!el) return;

  const keys = NEWS_INDUSTRIES.map(i => `news_terms_${i}`);
  const { data } = await sb.from('app_config').select('key,value').in('key', keys);
  const map = {};
  (data || []).forEach(r => { map[r.key] = r.value; });

  el.innerHTML = NEWS_INDUSTRIES.map(ind => {
    const key = `news_terms_${ind}`;
    const val = map[key] || '';
    return `<div style="margin-bottom:.75rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <label class="form-label" style="margin:0;font-size:12px;font-weight:600">${ind}</label>
        <button class="btn btn-sm" onclick="saveNewsTerm('${key}', document.getElementById('nt-${ind}').value)">저장</button>
      </div>
      <input class="form-input" id="nt-${ind}" value="${val}" placeholder="${ind} 관련 뉴스 검색어 (쉼표 구분)" style="font-size:12px">
    </div>`;
  }).join('');
}

async function saveNewsTerm(key, value) {
  if (!isAdmin()) { toast('admin만 수정 가능합니다.', 'error'); return; }
  const { error } = await sb.from('app_config').upsert(
    { key, value: value.trim(), updated_at: new Date().toISOString() },
    { onConflict: 'key' }
  );
  if (error) { toast('저장 실패: ' + error.message, 'error'); return; }
  toast(`저장 완료 — 봇 다음 사이클에 반영됩니다`, 'success');
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
