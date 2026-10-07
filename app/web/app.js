/* gh-rank 前端逻辑：榜单渲染 + 点击项目弹说明 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  period: 'daily',
  date: '',
  data: { entries: [] },
  healthTimer: null,
  refreshing: false,
  dataAge: null,
};

const PERIOD_LABEL = { daily: '本日', weekly: '本周' };

// 数据超过这么多小时就在界面上告警。必须和后端 scheduler.STALE_HOURS 对齐。
const STALE_HOURS = 12;

// ---------------------------------------------------------------- 工具

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtNum = (n) => Number(n || 0).toLocaleString('en-US');

function humanAge(hours) {
  if (hours === null || hours === undefined) return '未知';
  const h = Math.floor(hours);
  if (h < 1) return `${Math.max(1, Math.round(hours * 60))} 分钟前`;
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  const rest = h % 24;
  return rest ? `${d} 天 ${rest} 小时前` : `${d} 天前`;
}

/**
 * 数据新鲜度告警。
 *
 * 这个必须有：抓取失败时榜单会静静停在旧日期（实测 2026-10-01 那天 daily
 * 连续失败，榜单停在 09-27），用户看着"2026-09-27"完全不知道已经 4 天
 * 没更新了 —— 还以为是正常的。
 *
 * ages 是**按周期分开**的 {daily: x, weekly: y}（后端 models.days_since_last_snapshot
 * 的 period 参数），必须按当前选中的周期判断：之前后端返回的是所有周期里
 * 最新的那个数，于是 daily 停在 5 天前、weekly 刚抓的时，界面一声不吭。
 */
function renderStaleBanner(ages) {
  const banner = $('#staleBanner');
  const label = PERIOD_LABEL[state.period] || '榜单';
  // 兼容旧后端返回的单值
  const age = (ages && typeof ages === 'object') ? ages[state.period] : ages;

  if (age === null || age === undefined || age <= STALE_HOURS) {
    banner.hidden = true;
    return;
  }
  const severe = age > 48;
  banner.className = 'stale-banner' + (severe ? ' severe' : '');
  $('#staleText').innerHTML =
    `抓取失败，${label}榜已停留在 <b>${humanAge(age)}</b>的数据。` +
    `本机网络不稳定，系统会每 30 分钟自动重试，你也可以现在手动触发。`;
  banner.hidden = false;
}

// ---------------------------------------------------------------- 主题 / 字号
//
// 用户的屏幕小，所以字号默认给「大」档（1.2）。三档都持久化到 localStorage，
// 且首屏由 index.html 里的内联脚本预先套好，这里只负责响应点击。

const LS_THEME = 'ghrank.theme';
const LS_FONT = 'ghrank.fontScale';
const FONT_DEFAULT = '1.2';
const THEME_ICON = { dark: '🌙', light: '☀️' };

function lsGet(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 隐私模式，忽略 */ }
}

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

function applyTheme(theme) {
  const html = document.documentElement;
  // 切主题前先把过渡全关掉，否则会看到「页面变了、卡片还是旧色」的半截状态；
  // 强制一次重排让 no-anim 生效，改完立刻恢复，hover 动画不受影响。
  html.classList.add('no-anim');
  html.setAttribute('data-theme', theme);
  void html.offsetWidth;            // reflow
  html.classList.remove('no-anim');

  lsSet(LS_THEME, theme);
  const btn = $('#btnTheme');
  btn.textContent = THEME_ICON[theme];
  btn.title = theme === 'dark' ? '当前：夜间模式（点击切到浅色）' : '当前：浅色模式（点击切到夜间）';
}

function applyFontScale(scale) {
  document.documentElement.style.setProperty('--font-scale', scale);
  lsSet(LS_FONT, scale);
  $$('.font-ctl button').forEach((b) =>
    b.classList.toggle('active', b.dataset.fs === String(scale)));
}

function initViewControls() {
  applyTheme(lsGet(LS_THEME, 'dark'));
  applyFontScale(lsGet(LS_FONT, FONT_DEFAULT));

  $('#btnTheme').addEventListener('click', () =>
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'));

  $$('.font-ctl button').forEach((b) =>
    b.addEventListener('click', () => applyFontScale(b.dataset.fs)));

  // 快捷键：T 切主题，+ / - 调字号
  document.addEventListener('keydown', (e) => {
    // 弹窗打开时不要响应 T/+/−，免得误触
    if (!$('#modal').hidden) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.target.closest('input, textarea, select, [contenteditable]')) return;
    if (e.key === 't' || e.key === 'T') {
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    } else if (e.key === '+' || e.key === '=') {
      stepFont(1);
    } else if (e.key === '-' || e.key === '_') {
      stepFont(-1);
    }
  });
}

function stepFont(dir) {
  const steps = ['1', FONT_DEFAULT, '1.4'];
  const cur = String(lsGet(LS_FONT, FONT_DEFAULT));
  const i = Math.max(0, Math.min(steps.length - 1, steps.indexOf(cur) + dir));
  applyFontScale(steps[i]);
}

function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (bad ? ' bad' : '');
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 4200);}

async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch { /* 响应不是 JSON，用状态码兜底 */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------- 状态栏

async function loadHealth() {
  try {
    const h = await api('/api/health');

    const llm = $('#chipLlm');
    llm.className = 'chip ' + (h.llm.ready ? 'ok' : 'warn');
    llm.innerHTML = '模型 <b>' + esc(h.llm.model) + (h.llm.ready ? '' : ' 未就绪') + '</b>';

    $('#refreshHour').textContent = (h.scheduler?.jobs?.[0]?.next_run || '')
      .slice(11, 16) || '定时';

    // 数据过期必须显式告警，不能让用户对着旧日期猜
    state.dataAge = h.data_age_hours;
    renderStaleBanner(state.dataAge);

    // 抓取源探测走的是另一个接口，慢一点没关系
    api('/api/quota').then((q) => {
      const chip = $('#chipRaw');
      const qa = q.api_quota || {};

      // 探测线程整段抛异常时 q.error 有值、但 raw 仍是 null。
      // 之前只判断 raw===null || probing，没人渲染 error ——
      // 结果是前端永久显示「检测中…」，用户以为程序死了。
      if (q.raw === null || q.probing) {
        if (q.error) {
          chip.className = 'chip bad';
          chip.innerHTML = '抓取源 <b>探测失败</b>';
          chip.title = q.error;
        } else {
          // 后台还在探测：显示中性状态，不要闪红
          chip.className = 'chip';
          chip.innerHTML = '抓取源 <b>检测中…</b>';
        }
        return;
      }

      if (q.raw) {
        // raw 通就够用了，Token 有没有都无所谓
        chip.className = 'chip ok';
        chip.innerHTML = '抓取源 <b>raw 直连</b>';
        chip.title = qa.authenticated
          ? `raw 域名可用（不吃配额）｜api 兜底也可用，剩余 ${qa.remaining}/${qa.limit}`
          : `raw 域名可用（不吃配额）｜未配 Token 也不影响，api 兜底跳过`;
      } else {
        chip.className = 'chip bad';
        chip.innerHTML = '抓取源 <b>raw 不通</b>';
        chip.title = qa.remaining
          ? `raw 域名不可达。建议在 .env 配置 GITHUB_TOKEN，走 api 兜底（剩余 ${qa.remaining}/${qa.limit}）`
          : 'raw 域名不可达且无 Token，README 抓取会失败';
      }
    }).catch(() => {});

    setRefreshButton(h.refreshing);
  } catch (e) {
    console.error(e);
  }
}

function setRefreshButton(busy) {
  state.refreshing = busy;
  const btn = $('#btnRefresh');
  btn.disabled = busy;
  btn.querySelector('.spin').hidden = !busy;
  btn.querySelector('.label').textContent = busy ? '刷新中…' : '立即刷新';
}

// ---------------------------------------------------------------- 榜单

async function loadLeaderboard() {
  $('#loading').hidden = false;
  $('#loadingText').textContent = '加载中…';
  $('#empty').hidden = true;

  try {
    const qs = new URLSearchParams({ period: state.period });
    if (state.date) qs.set('date', state.date);
    state.data = await api('/api/leaderboard?' + qs);

    renderDates();
    renderList();
  } catch (e) {
    toast('加载失败：' + e.message, true);
    $('#list').innerHTML = '';
  } finally {
    $('#loading').hidden = true;
  }
}

function renderDates() {
  const sel = $('#dateSelect');
  const dates = state.data.available_dates || [];
  sel.innerHTML =
    '<option value="">最新</option>' +
    dates.map((d) => `<option value="${d}"${d === state.data.date ? ' selected' : ''}>${d}</option>`).join('');
}

function renderList() {
  const entries = state.data.entries || [];
  const list = $('#list');

  $('#listMeta').textContent = state.data.date
    ? `${state.data.entries.length} 个项目 · 快照日期 ${state.data.date}`
    : '';

  if (!entries.length) {
    list.innerHTML = '';
    $('#empty').hidden = false;
    return;
  }
  $('#empty').hidden = true;

  list.innerHTML = entries.map((e) => {
    const badges = [];
    if (e.language) badges.push(`<span class="badge lang">${esc(e.language)}</span>`);
    if (e.has_explanation) {
      badges.push('<span class="badge ai">✦ AI 说明</span>');
    } else if (e.expl_status === 'fallback') {
      badges.push('<span class="badge fb">⚠ 摘要兜底</span>');
    } else {
      badges.push('<span class="badge wait">○ 待生成</span>');
    }
    (e.topics || []).slice(0, 3).forEach((t) => badges.push(`<span class="badge">${esc(t)}</span>`));

    const unit = state.period === 'daily' ? '今日新增' : '本周新增';

    return `
    <li class="rank-item">
      <div class="rank-no">${e.rank}</div>
      <div class="rank-main">
        <button class="repo-link" data-repo="${esc(e.full_name)}">${esc(e.full_name)}</button>
        ${e.description ? `<div class="repo-desc">${esc(e.description)}</div>` : ''}
        <div class="badge-row">${badges.join('')}</div>
      </div>
      <div class="rank-stats">
        <div class="stars-today">+${fmtNum(e.period_stars)}<small>${unit}</small></div>
        <div class="stars-total">★ ${fmtNum(e.total_stars)}</div>
      </div>
    </li>`;
  }).join('');

  $$('.repo-link').forEach((btn) =>
    btn.addEventListener('click', () => openModal(btn.dataset.repo)));
}

// ---------------------------------------------------------------- 弹窗

async function openModal(fullName) {
  const modal = $('#modal');
  modal.hidden = false;
  document.body.style.overflow = 'hidden';

  $('#mRepo').textContent = fullName;
  $('#mDesc').textContent = '';
  $('#mDesc').removeAttribute('href');
  $('#mMeta').innerHTML = '';
  $('#mBody').innerHTML = '<div class="gen-state"><span class="spinner small"></span> 正在读取说明…</div>';
  $('#mGrounded').textContent = '';
  $('#readmeView').hidden = true;
  $('#btnReadme').hidden = true;
  $('#btnGithub').href = 'https://github.com/' + fullName;

  try {
    const d = await api('/api/repo/' + fullName);
    const repo = d.repo || {};
    const ex = d.explanation;

    $('#mRepo').textContent = fullName;
    if (repo.description) {
      $('#mDesc').textContent = repo.description;
    }
    $('#mMeta').innerHTML = [
      repo.language ? `<span class="badge lang">${esc(repo.language)}</span>` : '',
      `<span class="badge">★ ${fmtNum(repo.stars)}</span>`,
      `<span class="badge">⑂ ${fmtNum(repo.forks)}</span>`,
      ...(repo.topics || []).slice(0, 4).map((t) => `<span class="badge">${esc(t)}</span>`),
    ].join('');

    $('#mBody').innerHTML = renderExplanation(ex);

    if (ex && ex.status === 'ok') {
      $('#mGrounded').textContent =
        `原文回检 ${Math.round((ex.grounded_ratio || 0) * 100)}% · ${ex.model || ''}`;
    }
    if (d.readme_available) $('#btnReadme').hidden = false;
  } catch (e) {
    $('#mBody').innerHTML = `<div class="fb-notice">加载失败：${esc(e.message)}</div>`;
  }
}

function renderExplanation(ex) {
  if (!ex) {
    return `<div class="fb-notice">
      这个项目还没有生成说明。点右上角「立即刷新」，系统会抓取 README 后自动生成。
    </div>`;
  }

  const paras = String(ex.body || '')
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => `<p>${esc(p)}</p>`)
    .join('');

  const notice = ex.status === 'fallback'
    ? `<div class="fb-notice">
        ⚠ 这段是 README 原文摘要，不是 AI 生成。
        ${ex.error ? '原因：' + esc(ex.error) : ''}
      </div>`
    : '';

  const tags = (ex.tags || []).length
    ? `<div class="tag-row">${ex.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</div>`
    : '';

  return notice +
    (ex.headline ? `<div class="headline">${esc(ex.headline)}</div>` : '') +
    `<div class="explain-text">${paras}</div>` + tags;
}

function closeModal() {
  $('#modal').hidden = true;
  document.body.style.overflow = '';
}

async function toggleReadme() {
  const box = $('#readmeView');
  if (!box.hidden) { box.hidden = true; return; }
  const fullName = $('#mRepo').textContent.trim();
  box.hidden = false;
  box.textContent = '加载中…';
  try {
    const d = await api(`/api/repo/${fullName}/readme`);
    box.textContent = d.readme;
  } catch (e) {
    box.textContent = '读取失败：' + e.message;
  }
}

// ---------------------------------------------------------------- 事件

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    state.period = tab.dataset.period;
    state.date = '';
    // 告警按周期判断，所以切周期就要重算一次横幅
    renderStaleBanner(state.dataAge);
    loadLeaderboard();
  });
});

$('#dateSelect').addEventListener('change', (e) => {
  state.date = e.target.value;
  loadLeaderboard();
});

$('#btnRefresh').addEventListener('click', triggerRefresh);
$('#btnRetryNow').addEventListener('click', triggerRefresh);

function triggerRefresh() {
  setRefreshButton(true);
  api('/api/refresh', { method: 'POST' })
    .then((r) => {
      if (r.ok === false) toast(r.reason || '刷新未启动', true);
      else toast('刷新已开始，抓取和 AI 生成需要一会儿，完成后页面会自动更新');
      startPolling();
    })
    .catch((e) => {
      toast('触发失败：' + e.message, true);
      setRefreshButton(false);
    });
}

$('#btnClose').addEventListener('click', closeModal);
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
$('#btnReadme').addEventListener('click', toggleReadme);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#modal').hidden) closeModal(); });

function startPolling() {
  clearInterval(state.healthTimer);
  let ticks = 0;
  state.healthTimer = setInterval(async () => {
    ticks += 1;
    await loadHealth();
    if (!state.refreshing || ticks > 90) {
      clearInterval(state.healthTimer);
      loadLeaderboard();
    }
  }, 3000);
}

// ---------------------------------------------------------------- 启动

initViewControls();
loadHealth();
loadLeaderboard();
setInterval(loadHealth, 60000);
