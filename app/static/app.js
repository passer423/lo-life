/* lo 上新。原生 JS，零依赖、零 CDN —— 与项目其余部分保持同一套取舍。 */
'use strict';

const SUGGESTIONS = ['JSK 和 OP 有什么区别？', '烫金和亮片可以机洗吗？', '新手入坑先买什么？', '柄图是什么意思？'];
const DIM_LABEL = { bust: '胸围', waist: '腰围', hip: '臀围', shoulder: '肩宽', length: '裙长' };

const ROUTES = {
  // 路由 key 仍叫 kb —— 这一页检索的就是知识库；界面上的标签叫「搜索」。
  // 改名只影响显示，不动 key，避免和外部链接/并行改动互相踩。
  feed: '推荐', preferences: '偏好', kb: '助手', me: '我的',
  size: '尺码助手', care: '洗护查询',
};
// 尺码/洗护是「知识」下面的子页，底栏仍高亮「知识」
const TAB_OF = { size: 'kb', care: 'kb' };

const state = { tab: 'feed', param: '', feedFilter: 'all', feedQ: '', feedSort: 'score', feedMode: 'recommend', user: null };
const RENDERERS = {};

/* 令牌放 localStorage，随请求以 Authorization: Bearer 发送。
   不用 cookie 是刻意的：本地服务的 cookie 会和同一 host 上的其他东西混在一起，
   而 localStorage 在 PWA 独立窗口里也照样可用。 */
const TOKEN_KEY = 'lo-life-token';
const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; } };
const setToken = (value) => {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (_) { /* 隐私模式下写不了，退化为每次重新登录 */ }
};

const $view = () => document.getElementById('view');
const esc = (value) => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/* ------------------------------------------------------------------ 网络 */

async function request(path, { method = 'GET', body = null } = {}) {
  const init = { method, headers: {} };
  const token = getToken();
  if (token) init.headers['Authorization'] = 'Bearer ' + token;
  if (body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try { const data = await res.json(); if (data && data.detail) detail = data.detail; } catch (_) { /* 非 JSON 错误体 */ }
    if (res.status === 401) {
      // 令牌过期或已被吊销：清掉本地状态，让界面回到"未登录"
      setToken('');
      state.user = null;
    }
    const error = new Error(detail);
    error.status = res.status;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}

async function refreshUser() {
  try {
    const data = await request('/api/auth/me');
    state.user = data.user || null;
  } catch (_) {
    state.user = null;
  }
  return state.user;
}

let toastTimer = null;
function toast(message) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = 'toast show';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 2400);
}

/* ------------------------------------------------------------------ 路由 */

function route() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  const [key, param] = raw.split('/');
  state.tab = RENDERERS[key] ? key : 'feed';
  state.param = param ? decodeURIComponent(param) : '';
  document.getElementById('title').textContent = ROUTES[state.tab];
  const activeTab = TAB_OF[state.tab] || state.tab;
  document.querySelectorAll('#tabbar button').forEach((button) => {
    button.classList.toggle('on', button.dataset.tab === activeTab);
  });
  $view().innerHTML = '<div class="loading">载入中…</div>';
  RENDERERS[state.tab]().catch((error) => {
    $view().innerHTML = '<div class="notice warn">载入失败：' + esc(error.message) + '</div>';
  });
}

document.querySelectorAll('#tabbar button').forEach((button) => {
  button.addEventListener('click', () => { location.hash = '#/' + button.dataset.tab; });
});
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------------ 偏好 */

/** 自定义标签的边界，必须与服务端 `axes.CUSTOM_MAX_LEN` 一致。 */
const CUSTOM_MAX_LEN = 20;

/**
 * 标签组件：一个"轴"（店家 / 风格 / 款式 / 自定义标签）→ 一组可多选的标签。
 *
 * 完全由后端返回的元数据驱动（key / label / hint / allow_custom / options）：
 * 服务端加一个新轴，这里不用改任何代码。`allow_custom` 为真的轴渲染成
 * 「输入框 + 回车添加」，其余轴渲染成「从选项里挑」。
 *
 * 元数据里还有 `bonus`（该轴的权重），但界面**不显示它**：用户要看的是
 * "勾这个有没有用"，不是一套内部打分。权重留在服务端参与排序即可。
 */
function createTagGroup({ axis, options, selected }) {
  const custom = !!axis.allow_custom;
  const chosen = new Set(selected || []);

  const root = document.createElement('section');
  root.className = 'tag-group';
  root.dataset.axis = axis.key;
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', axis.label);

  const head = document.createElement('div');
  head.className = 'tag-head';
  const wrap = document.createElement('div');
  wrap.className = 'tags-input';

  const adder = document.createElement('div');
  adder.className = 'tag-add';
  adder.innerHTML =
    '<input class="tag-input" type="text" maxlength="' + CUSTOM_MAX_LEN + '" ' +
      'placeholder="输入标签，回车添加" aria-label="添加自定义标签">' +
    '<button type="button" class="ghost sm">添加</button>';

  const foot = document.createElement('p');
  foot.className = 'tag-hint';
  // 不显示每个轴的权重分值：用户要的是"勾这个有没有用"，不是内部打分的数字
  foot.textContent = axis.hint || '';

  root.append(head, wrap, adder, foot);
  adder.style.display = custom ? 'flex' : 'none';

  function addFromInput() {
    const input = adder.querySelector('.tag-input');
    // 折叠空白：容忍" 缎 带 "，但归一到同一个值
    const text = input.value.split(/\s+/).filter(Boolean).join(' ');
    if (!text) return;
    if (text.length > CUSTOM_MAX_LEN) {
      toast('单个标签最多 ' + CUSTOM_MAX_LEN + ' 个字');
      return;
    }
    if (chosen.has(text)) {
      toast('已经有这个标签了');
      input.value = '';
      return;
    }
    chosen.add(text);
    input.value = '';
    render();
  }

  function render() {
    head.innerHTML = '<span class="tag-label">' + esc(axis.label) + '</span>' +
      '<span class="tag-state">' + (chosen.size ? '已选 ' + chosen.size : '未选') + '</span>' +
      (chosen.size ? '<button type="button" class="tag-clear">清空</button>' : '');
    const clear = head.querySelector('.tag-clear');
    if (clear) clear.addEventListener('click', () => { chosen.clear(); render(); });

    wrap.innerHTML = '';

    if (custom) {
      // 自定义轴没有预置选项：列出来的就是已经加进去的词，每个可单独删
      if (!chosen.size) {
        wrap.innerHTML = '<span class="hint">还没有自定义标签</span>';
        return;
      }
      chosen.forEach((value) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'tag-chip on';
        chip.innerHTML = esc(value) + '<i class="tag-x">×</i>';
        chip.setAttribute('aria-label', '移除 ' + value);
        chip.addEventListener('click', () => { chosen.delete(value); render(); });
        wrap.appendChild(chip);
      });
      return;
    }

    const list = options || [];
    if (!list.length) {
      wrap.innerHTML = '<span class="hint">content 里还没有可选项</span>';
      return;
    }
    list.forEach((option) => {
      const button = document.createElement('button');
      button.type = 'button';
      const on = chosen.has(option.value);
      // 不显示"有几条能匹配"的数字：标签是偏好，不是数据统计。
      // 因此也不再给零匹配的标签加视觉弱化——少了数字，弱化会被读成"不可选"。
      button.className = 'tag-chip' + (on ? ' on' : '');
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
      button.textContent = option.label;
      button.addEventListener('click', () => {
        if (chosen.has(option.value)) chosen.delete(option.value);
        else chosen.add(option.value);
        render();
      });
      wrap.appendChild(button);
    });
  }

  adder.querySelector('button').addEventListener('click', addFromInput);
  adder.querySelector('.tag-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();      // 别让回车把整个表单提交了
      addFromInput();
    }
  });

  render();
  return { element: root, values: () => [...chosen] };
}

function describePreferences(described) {
  if (!described || !described.length) {
    return '<div class="hint">还没设置任何偏好。上面的标签勾选后保存即可，推荐页会立刻按它排序。</div>';
  }
  return described.map((row) =>
    '<div class="pref-row"><span class="pref-label">' + esc(row.label) + '</span>' +
    '<span class="pref-values">' + esc(row.values.join('、')) + '</span></div>').join('');
}

/** 卡片上的"喜欢这家店"：直接改 shops 轴。只传 shops，其他轴不受影响。 */
async function toggleShopPreference(brand, add) {
  const current = (state.preferences && state.preferences.shops) || [];
  const next = add
    ? Array.from(new Set([...current, brand]))
    : current.filter((value) => value !== brand);
  const saved = await request('/api/preferences', { method: 'PUT', body: { shops: next } });
  state.preferences = saved.preferences;
  return saved;
}

function wireShopToggles(nodes, onChanged) {
  nodes.forEach((node) => {
    node.addEventListener('click', async (event) => {
      event.preventDefault();
      node.disabled = true;
      const add = node.dataset.inPref !== '1';
      try {
        await toggleShopPreference(node.dataset.shopBrand, add);
        toast(add ? '已加入「喜欢的店家」' : '已从偏好移除');
        await onChanged();
      } catch (error) {
        toast('操作失败：' + error.message);
        node.disabled = false;
      }
    });
  });
}

/** 自定义标签的命中回报：没有这一段，用户写了个永不命中的词只会以为功能坏了。 */
function renderMatchReport(report) {
  const unmatched = Object.values(report || {})
    .flatMap((entry) => (entry && entry.unmatched) || []);
  if (!unmatched.length) return '';
  return '<div class="notice warn">这些自定义标签在当前内容里没有匹配到：' +
    esc(unmatched.join('、')) + '。内容补上之后会自动生效。</div>';
}

RENDERERS.preferences = async function renderPreferences() {
  if (!state.user) {
    $view().innerHTML =
      '<div class="card"><h2>我的偏好</h2>' +
        '<div class="notice info">设置偏好需要登录——偏好是跟着账号走的。</div>' +
        '<a class="ghost" href="#/me">去登录 / 注册</a></div>' +
      '<div class="card"><h2>为什么要登录</h2><p class="hint">' +
        '这台机器可能不止你一个人用，也可能以后给朋友用。所以偏好按账号隔离，' +
        '而不是所有人共用一份。浏览推荐、查知识、算尺码都不需要登录。</p></div>';
    return;
  }

  const [optionsData, mine] = await Promise.all([
    request('/api/preferences/options'),
    request('/api/preferences'),
  ]);
  const axes = optionsData.axes || [];
  const saved = mine.preferences || {};

  $view().innerHTML =
    '<div class="card"><h2>我的偏好</h2>' +
      '<div class="notice info">偏好决定推荐页的排序。勾得越准排得越对。' +
        '「自定义标签」可以自由输入，会去匹配系列名 / 备注 / 品牌名 / 款式 / 风格。</div>' +
      '<div id="pref-warn">' + renderMatchReport(mine.match_report) + '</div>' +
      '<div id="tag-groups"></div>' +
      '<button class="primary" id="save-prefs">保存偏好</button>' +
      '<p class="hint" id="pref-saved"></p>' +
    '</div>' +
    '<div class="card"><h2>当前排序依据</h2><div id="pref-described">' +
      describePreferences(mine.described) + '</div>' +
      '<p class="hint">每条推荐下面都会标出它为什么排在前面。</p>' +
    '</div>';

  const host = document.getElementById('tag-groups');
  const handles = axes.map((axis) => {
    const handle = createTagGroup({
      axis,
      options: (optionsData.options || {})[axis.key] || [],
      selected: saved[axis.key] || [],
    });
    host.appendChild(handle.element);
    return handle;
  });

  if (mine.updated_at) {
    document.getElementById('pref-saved').textContent = '上次保存：' + mine.updated_at;
  }

  const save = document.getElementById('save-prefs');
  save.addEventListener('click', async () => {
    const payload = {};
    handles.forEach((handle) => { payload[handle.element.dataset.axis] = handle.values(); });
    save.disabled = true;
    try {
      const result = await request('/api/preferences', { method: 'PUT', body: payload });
      state.preferences = result.preferences;
      document.getElementById('pref-described').innerHTML = describePreferences(result.described);
      document.getElementById('pref-warn').innerHTML = renderMatchReport(result.match_report);
      document.getElementById('pref-saved').textContent = '已保存：' + result.updated_at;
      // 保存完固定回到「我的」：偏好是在那里进入的，回去能看到摘要与账号状态。
      // 提示里带上未命中的数量——否则离开这一页就看不到那条黄条了。
      const unmatched = (((result.match_report || {}).keywords || {}).unmatched || []).length;
      toast(unmatched
        ? '已保存；' + unmatched + ' 个自定义标签没匹配到内容'
        : '偏好已保存，推荐排序已更新');
      location.hash = '#/me';
    } catch (error) {
      toast('保存失败：' + error.message);
    } finally {
      save.disabled = false;
    }
  });
};

/* ------------------------------------------------------------------ 推荐 */

const FEED_FILTERS = [
  ['all', '全部'],
  ['preferred', '只看偏好店家'],
  ['7', '近 7 天'],
  ['30', '近 30 天'],
];

/* 排序和筛选是两件事：筛选决定"哪些条目出现"，排序决定"谁在前面"。
   混成一个 chip 组会让人以为"按价格"是在价格区间里过滤。 */
const FEED_SORTS = [
  ['score', '按偏好'],
  ['date', '最新'],
  ['price_asc', '价格 ↑'],
  ['price_desc', '价格 ↓'],
];

/* 三围存在**浏览器**里，不进服务端数据库。
   理由：这是本应用最私人的几个数字，而"记住它"只是为了免去每次重填。
   放 localStorage 的好处是——清除它不需要接口、删号不需要级联、备份 zip 里也不会多出
   一份身体数据；代价是换设备要重填（对一个人自用的场景，这个代价可以接受）。 */
const MEASURE_KEY = 'lo-life-measure';

function getMeasurements() {
  try {
    const raw = localStorage.getItem(MEASURE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (_) { return null; }
}

function setMeasurements(value) {
  try {
    if (value) localStorage.setItem(MEASURE_KEY, JSON.stringify(value));
    else localStorage.removeItem(MEASURE_KEY);
  } catch (_) { /* 隐私模式下写不了：降级为"这次会话不记住" */ }
}

function feedQuery() {
  const parts = ['limit=200'];
  const filter = state.feedFilter;
  if (filter === 'preferred') parts.push('only_preferred=true');
  else if (filter === '7' || filter === '30') parts.push('days=' + filter);
  if (state.feedQ) parts.push('q=' + encodeURIComponent(state.feedQ));
  if (state.feedSort && state.feedSort !== 'score') parts.push('sort=' + state.feedSort);
  return '?' + parts.join('&');
}

/* 出口再校验一次协议：后端已经过滤过（app/domain/urls.py），
   这里防的是"后端换了/直连了别的源"这种情况。两道都留着，成本几乎为零。 */
function safeUrl(value, allowMedia) {
  const text = String(value || '').trim();
  if (/^https?:\/\//i.test(text)) return text;
  if (allowMedia && /^\/media\/[^/]/.test(text) && text.indexOf('..') === -1) return text;
  return '';
}

/* 条目的 items（JSK/SK/KC）里挑一个该品牌尺码表**真的有的**品类。
   挑不到就返回 null——不拿 JSK 的表去算 KC 的码，那是错误答案而不是不精确的答案。 */
function matchingCategory(chart, items) {
  const categories = Object.keys((chart && chart.categories) || {});
  for (const name of (items || [])) {
    const wanted = String(name).trim().toLowerCase();
    const hit = categories.find((cat) => cat.toLowerCase() === wanted);
    if (hit) return hit;
  }
  return null;
}

function sizeBadges(item, charts) {
  // 商品级实测尺寸最优先：官网给的是一件衣服的真实范围（弹性抽褶、可调肩带），
  // 比任何品牌级尺码表都准，而且它不需要“核对”——它本来就是从商品页抄下来的。
  const own = item.size && Object.keys(item.size || {}).some((key) => Array.isArray(item.size[key]));
  if (own) {
    const parts = [];
    ['bust', 'waist', 'hip', 'shoulder', 'length'].forEach((dim) => {
      const bounds = item.size[dim];
      if (!Array.isArray(bounds) || bounds.length !== 2) return;
      const label = { bust: '胸', waist: '腰', hip: '臀', shoulder: '肩', length: '长' }[dim];
      const text = bounds[0] === bounds[1] ? String(bounds[0]) : bounds[0] + '–' + bounds[1];
      parts.push(label + text);
    });
    const kind = item.size.label || 'F';
    return '<span class="badge size" data-size-for="' + esc(item.id) + '" title="' +
      esc(item.size.source || '官网商品页') + '">' + esc(kind) + ' ' + esc(parts.join(' ')) + '</span>';
  }

  const chart = charts && charts[item.brand];
  if (!chart) return '<span class="badge gray">无尺码表</span>';
  // 没按官方表核对过的，只说不确定——**不显示具体码和版型**。
  // 首页把「偏小」当事实印出来，用户就会照它选大一码；猜错一次就是买错一码。
  if (chart.verified !== true) {
    return '<span class="badge gray" title="' + esc(chart.source || '还没录入官方尺码表') +
      '">尺码表未核对</span>';
  }
  const category = matchingCategory(chart, item.items);
  if (!category) return '<span class="badge gray">品类未收录</span>';
  const sizes = chart.categories[category] || [];
  const fit = chart.fit && chart.fit !== '正码'
    ? '<span class="badge gray">' + esc(chart.fit) + '</span>' : '';
  return '<span class="badge size" data-size-for="' + esc(item.id) + '">' +
      esc(category) + ' ' + esc(sizes.join('/')) + '</span>' + fit;
}

function relativeDays(age) {
  if (age < 0) return '未发售';
  if (age === 0) return '今天';
  return age + ' 天前';
}

/* 价格单独一行、自己一种样式：混在灰色 meta 里（`日期 · 几天前 · ¥价格`）会被当成
   "又一串数字"直接跳过，而价格是决定买不买的第一条信息。

   币种标签来自条目的 `currency` 字段（默认 CNY），**不靠品牌名猜**：
   猜错一次的价格标签比没有标签更糟。CNY 不标——`¥` 已经说明是人民币，
   每张国内卡片都挂一个「元」是纯噪声。 */
const CURRENCY_LABEL = { JPY: '日元', USD: '美元', EUR: '欧元', GBP: '英镑', HKD: '港币' };

function formatAmount(text) {
  // 给四位以上的数字加千分位：¥55000 读起来要停顿，¥55,000 不用。
  return String(text).replace(/\d{4,}/g, (digits) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ','));
}

function priceTag(item) {
  const text = String((item && item.price_range) || '').trim();
  if (!text) return '<span class="price none">价格未标</span>';

  // 文本里已经写了货币的（老数据「41800 日元」「USD 120」）就不再补符号和标签
  const hasCurrency = /[¥￥]|日元|JPY|RMB|USD|元|美元|港币/.test(text);
  const currency = String((item && item.currency) || 'CNY').toUpperCase();
  const label = currency === 'CNY' ? '' : (CURRENCY_LABEL[currency] || currency);
  const showLabel = !hasCurrency && label;

  return '<span class="price">' + (hasCurrency ? '' : '¥') + esc(formatAmount(text)) + '</span>' +
    (showLabel ? '<span class="cur">' + esc(label) + '</span>' : '');
}

function arrivalCard(item, charts, shops) {
  const image = safeUrl(item.image, true);
  const link = safeUrl(item.link, false);
  // 店铺页和商品页指向的东西不一样：把店铺首页标成"去商品"，点进去发现不是那条裙子
  const linkLabel = item.link_kind === 'shop' ? '去店铺' : '去商品';
  // 三级兜底，每一级的文案都不一样：
  //   有链接 → 去商品 / 去店铺；只有搜索 → 淘宝搜（**不是**详情页，不能冒充）
  const searchUrl = safeUrl(item.search_url, false);
  const buyLink = link
    ? '<a class="ghost sm" href="' + esc(link) + '" target="_blank" rel="noopener noreferrer">' + linkLabel + '</a>'
    : (searchUrl
        ? '<a class="ghost sm" href="' + esc(searchUrl) + '" target="_blank" rel="noopener noreferrer">淘宝搜</a>'
        : '');
  // 没图时不要只放一个字母：那看起来像"图片加载失败"，而不是"还没取图"。
  // 明确写「暂无图」，右下角那个「取图」按钮就是下一步该做的事。
  const initial = esc((item.brand_name || item.brand || '?').slice(0, 1));
  const thumb = image
    ? '<img src="' + esc(image) + '" loading="lazy" alt="">'
    : '<span class="ph"><b>' + initial + '</b><small>暂无图</small></span>';
  const badges = [
    item.is_new ? '<span class="badge">新</span>' : '',
    item.preferred_shop ? '<span class="badge pref">喜欢的店</span>' : '',
    item.future ? '<span class="badge gray">预告</span>' : '',
  ].join('');
  const itemTags = (item.items || []).map((name) => '<span class="tag">' + esc(name) + '</span>').join('');
  const styleTags = (item.styles || []).map((name) => '<span class="tag style">' + esc(name) + '</span>').join('');
  // 排序原因必须露出来：加权一旦不可解释，推荐就成了玄学，排错一次就没人信了
  const reasons = (item.reasons || []).length
    ? '<div class="reasons">' +
        item.reasons.map((text) => '<span class="reason">' + esc(text) + '</span>').join('') +
      '</div>'
    : '';
  // 搜索命中单独一行，和"排序原因"分开：命中的是**筛选条件**，不是加分理由
  const matched = (item.matched || []).length
    ? '<div class="reasons"><span class="reason hit">命中：' + esc(item.matched.join(' / ')) + '</span></div>'
    : '';
  // 相关知识：RAG 在这里唯一的用途——挂一段背景，不参与排序。
  // 查不到就什么都不显示，不写"暂无"占位。
  const knowledge = item.knowledge && item.knowledge.title
    ? '<div class="kn">相关知识：' + esc(item.knowledge.title) + '</div>'
    : '';
  // 未登录时不渲染操作按钮，直接引导去登录——点了才弹 401 是更差的体验
  const shopAction = state.user
    ? '<button class="ghost sm" data-shop-brand="' + esc(item.brand) +
      '" data-in-pref="' + (item.preferred_shop ? '1' : '0') + '">' +
      (item.preferred_shop ? '已在偏好' : '喜欢这家店') + '</button>'
    : '<a class="ghost sm" href="#/me">登录后可设偏好</a>';

  // 没图才给「取图」：点一下才联网，页面加载永远不抓。
  // 登记过 page_url 的直接开跑，否则就地展开一个输入框让人粘贴地址。
  const registeredPage = (shops && shops[item.brand] && shops[item.brand].page_url) || '';
  const fetchButton = image
    ? ''
    : '<button class="ghost sm" data-fetch-image="' + esc(item.id) +
      '" data-fetch-page="' + esc(registeredPage) + '">取图</button>';

  return '<article class="arrival">' +
    '<div class="thumb">' + thumb + '</div>' +
    '<div class="body">' +
      '<div class="line"><span class="brand">' + esc(item.brand_name) + '</span>' + badges + '</div>' +
      '<div class="series">' + esc(item.series) + '</div>' +
      '<div class="price-line">' + priceTag(item) + '</div>' +
      '<div class="meta">' + esc(item.release_date) + ' · ' + esc(relativeDays(item.age_days)) + '</div>' +
      '<div class="sizes">' + sizeBadges(item, charts) + '</div>' +
      reasons +
      matched +
      knowledge +
      (itemTags || styleTags ? '<div class="tags">' + itemTags + styleTags + '</div>' : '') +
      (item.note ? '<div class="note">' + esc(item.note) + '</div>' : '') +
      '<div class="actions">' +
        shopAction +
        '<a class="ghost sm" href="#/size/' + encodeURIComponent(item.brand) + '">算尺码</a>' +
        buyLink +
        fetchButton +
      '</div>' +
      (item.source ? '<div class="src">来源：' + esc(item.source) + '</div>'
                   : '<div class="src warn">未标注来源</div>') +
    '</div></article>';
}

/* 把已经存下来的尺寸套到列表上：算得出来的显示「你的码 M」，算不出来的保持原样。
   整段失败（接口 500、离线）就静默跳过——搜索和列表本身不该因为尺码挂了而不可用。 */
async function applySizes() {
  const measured = getMeasurements();
  if (!measured) return;
  const nodes = Array.from($view().querySelectorAll('[data-size-for]'));
  if (!nodes.length) return;
  const ids = Array.from(new Set(nodes.map((node) => node.dataset.sizeFor)));
  let data = null;
  try {
    data = await request('/api/feed/sizes', { method: 'POST', body: Object.assign({ ids: ids }, measured) });
  } catch (_) { return; }

  const sizes = (data && data.sizes) || {};
  nodes.forEach((node) => {
    const result = sizes[node.dataset.sizeFor];
    if (!result || result.status !== 'ok') return;
    // 区间型（真实官网数据）：徽标上本来就是「胸 91–101」这种事实，
    // 不要再换成“你的码 F”。只上色 + 把判断放进 title。
    if (result.ranges) {
      node.className = 'badge size ' +
        (result.verdict === 'too_small' ? 'warn' : (result.verdict === 'fits' ? 'on' : ''));
      node.title = [result.headline, ...(result.warnings || []), ...(result.advice || [])]
        .filter(Boolean).join(' ');
      return;
    }
    if (result.recommended) {
      node.textContent = (result.category || '') + ' 你的码 ' + result.recommended;
      node.className = 'badge size on';
      node.title = (result.warnings || []).join(' ');
    } else {
      node.textContent = (result.category || '') + ' 最大码也不够';
      node.className = 'badge size warn';
    }
  });
}

function emptyFeed(data) {
  const filters = (data && data.filters) || {};
  if (state.feedQ) return '没有搜到「' + state.feedQ + '」。换个词，或清空搜索看全部。';
  if (filters.price_min != null || filters.price_max != null) {
    return '这个价位区间内没有上新。<br><span class="hint">没标价格的条目不会参与价格筛选。</span>';
  }
  if (state.feedFilter === 'preferred') return '偏好里还没选店家。去「偏好」页勾几个。';
  return '这个时间窗口内没有上新。';
}

RENDERERS.feed = async function renderFeed() {
  /* 两种模式，同一套卡片：
       recommend —— 只列"命中你偏好 tag"的条目（默认，登录且有偏好时）
       all       —— 全部上新 + 搜索/筛选/排序
     退回规则很重要：推荐为空就**自动退回全部**，绝不给用户一个空白页。
     "你没有偏好"和"你没有符合偏好的上新"是两个完全不同的状态，只有后者才该空。 */
  let recommended = null;
  if (state.user && state.feedMode === 'recommend') {
    try {
      recommended = await request('/api/recommendations?limit=10&sort=date');
    } catch (_) {
      recommended = null;               // 401/500 都不是"没有推荐"，就走全部
    }
  }
  const mode = recommended && (recommended.items || []).length ? 'recommend' : 'all';
  state.feedMode = mode;

  const data = mode === 'recommend' ? recommended : await request('/api/feed' + feedQuery());
  state.preferences = data.preferences || state.preferences || { shops: [], styles: [], categories: [] };

  const chips = FEED_FILTERS.map(([key, label]) =>
    '<button class="chip' + (state.feedFilter === key ? ' on' : '') + '" data-filter="' + key + '">' +
    label + '</button>').join('');
  const sorts = FEED_SORTS.map(([key, label]) =>
    '<button class="chip small' + (state.feedSort === key ? ' on' : '') + '" data-sort="' + key + '">' +
    label + '</button>').join('');

  const banner = data.has_sample_data
    ? '<div class="notice warn">当前包含<b>示例数据</b>（不是真实上新）。按 content/newarrivals/_README.md 录入真实条目后，这里会自动消失。</div>'
    : '';
  const problems = (data.problems || []).length
    ? '<div class="notice warn">有 ' + data.problems.length + ' 条上新数据没读进来：' +
      esc(data.problems.slice(0, 3).join('；')) + (data.problems.length > 3 ? ' …' : '') + '</div>'
    : '';

  const described = data.described || [];
  let head = '';
  let toolbar = '';

  if (mode === 'recommend') {
    head = '<div class="notice ok"><b>为你推荐</b> · 按上新日期倒序，最近 ' + esc(data.count) + ' 条' +
      (described.length
        ? '：' + described.map((row) => esc(row.label) + ' ' + esc(row.values.join('、'))).join(' · ')
        : '') +
      ' <a href="#/preferences">调整偏好</a></div>' +
      (data.kb_ready === false
        ? '<div class="notice info">知识索引还没建，所以卡片上没有「相关知识」。</div>' : '');
    toolbar = '<div class="chips"><button class="chip" data-mode="all">看全部上新</button></div>';
  } else {
    // 全部模式下，让用户随时看得到"当前按什么在排序"
    head = !state.user
      ? '<div class="notice info">登录后可以设置偏好（喜欢的店家 / 风格 / 常穿款式），推荐会按偏好排序。</div>'
      : (described.length
          ? '<div class="notice ok">推荐依据：' + described.map((row) =>
              esc(row.label) + ' ' + esc(row.values.join('、'))).join(' · ') +
            ' <a href="#/preferences">调整</a></div>'
          : '<div class="notice info">还没设置偏好。<a href="#/preferences">去勾选标签</a>，推荐会更准。</div>');
    if (state.user && described.length) {
      toolbar = '<div class="chips"><button class="chip" data-mode="recommend">只看为你推荐</button></div>';
    }
  }

  const cards = (data.items || []).length
    ? data.items.map((item) => arrivalCard(item, data.size_charts, data.shops)).join('')
    : '<div class="empty">' + emptyFeed(data) + '</div>';

  const searching = mode === 'all'
    ? '<div class="searchbar"><input id="feed-q" type="search" enterkeyhint="search" ' +
        'placeholder="搜系列 / 品牌 / 款式，如「秋日 JSK」" value="' + esc(state.feedQ) + '">' +
        (state.feedQ ? '<button class="ghost sm" id="feed-q-clear">清空</button>' : '') +
      '</div>' +
      '<div class="chips">' + chips + '</div>' +
      '<div class="chips">' + sorts + '</div>'
    : '';

  $view().innerHTML = banner + problems + head +
    toolbar + searching +
    '<div class="arrivals">' + cards + '</div>';

  wireFeedSearch();
  $view().querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      state.feedMode = button.dataset.mode;
      RENDERERS.feed().catch((error) => toast(error.message));
    });
  });
  $view().querySelectorAll('[data-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      state.feedFilter = button.dataset.filter;
      RENDERERS.feed().catch((error) => toast(error.message));
    });
  });
  $view().querySelectorAll('[data-sort]').forEach((button) => {
    button.addEventListener('click', () => {
      state.feedSort = button.dataset.sort;
      RENDERERS.feed().catch((error) => toast(error.message));
      toast('已按' + button.textContent + '排序');
    });
  });
  const clear = document.getElementById('feed-q-clear');
  if (clear) clear.addEventListener('click', () => {
    state.feedQ = '';
    RENDERERS.feed().catch((error) => toast(error.message));
  });
  wireShopToggles([...$view().querySelectorAll('[data-shop-brand]')], () => RENDERERS.feed());
  wireFetchButtons();
  await applySizes();
};

/* 「取图」：一次点击 = 一次请求，跑完立刻重渲染。
   登记过 page_url 的直接开跑；没有的就地展开输入框，让人粘贴一次地址——
   比"去跑一个脚本"少一层门槛，也比"页面一打开就抓"安全得多。 */
function wireFetchButtons() {
  $view().querySelectorAll('[data-fetch-image]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.fetchImage;
      const registered = button.dataset.fetchPage || '';
      if (registered) { runFetch(id, registered, button); return; }

      const card = button.closest('.arrival');
      if (!card || card.querySelector('.fetch-form')) return;

      const form = document.createElement('div');
      form.className = 'fetch-form';
      form.innerHTML =
        '<input type="url" inputmode="url" autocomplete="off" ' +
          'placeholder="粘贴这条商品的淘宝详情页地址（或店铺上新列表页）">' +
        '<button class="primary sm" data-fetch-go>取</button>' +
        '<button class="ghost sm" data-fetch-cancel>取消</button>';
      button.parentElement.insertAdjacentElement('afterend', form);

      const input = form.querySelector('input');
      input.focus();
      form.querySelector('[data-fetch-go]').addEventListener('click', () => runFetch(id, input.value.trim(), button));
      form.querySelector('[data-fetch-cancel]').addEventListener('click', () => form.remove());
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        runFetch(id, input.value.trim(), button);
      });
    });
  });
}

async function runFetch(id, pageUrl, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '取图中…';
  try {
    const result = await request('/api/images/fetch', { method: 'POST', body: { id: id, page_url: pageUrl || '' } });
    toast(result.link ? '已取到图片和详情页链接' : '已取到图片');
    await RENDERERS.feed();
  } catch (error) {
    // 失败原因原样显示（403 风控 / 配不上系列 / 只允许本机…），让人知道下一步换什么输入
    toast(error.message);
    button.disabled = false;
    button.textContent = label;
  }
}

/* 搜索框：防抖 300ms，回车立即搜。
   重渲染会把输入框一起换掉，所以要把焦点和光标位置收回来——否则每打一个字
   输入框就失焦，这个搜索框等于不能用。 */
function wireFeedSearch() {
  const input = document.getElementById('feed-q');
  if (!input) return;
  let timer = null;

  const run = (caret) => {
    const value = input.value.trim();
    if (value === state.feedQ) return;
    state.feedQ = value;
    RENDERERS.feed().then(() => {
      const again = document.getElementById('feed-q');
      if (!again) return;
      again.focus();
      const pos = Math.min(caret == null ? again.value.length : caret, again.value.length);
      try { again.setSelectionRange(pos, pos); } catch (_) { /* 某些输入法下不支持 */ }
    }).catch((error) => toast(error.message));
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const caret = input.selectionStart;
    timer = setTimeout(() => run(caret), 300);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    clearTimeout(timer);
    run(input.selectionStart);
  });
}

/* ------------------------------------------------------------------ 知识 */

function formatAnswer(text) {
  // 先转义再标引用：否则用户输入里的 < > 会变成标签
  return esc(text).replace(/\[(\d+)\]/g, '<sup class="cite" data-n="$1">[$1]</sup>');
}

RENDERERS.kb = async function renderKb() {
  // 助手（聊天页）在 chat.js 里，独立文件避免和并行改动抢 app.js。
  // 加载失败时退回原来的知识库检索页，而不是整页空白。
  if (typeof window.renderAssistant === 'function') {
    return window.renderAssistant();
  }
  let status = null;
  try { status = await request('/api/kb/status'); } catch (error) { status = { ready: false, error: error.message }; }

  const banner = status.ready
    ? '<div class="notice ok">知识库就绪：' + status.chunks + ' 个片段' +
      (status.built_at ? '，建于 ' + esc(status.built_at) : '') +
      (status.llm_enabled ? '' : '。未配置模型，将以原文摘录作答') + '</div>'
    : '<div class="notice warn">知识库还没建立：' + esc(status.error || '请先运行建索引脚本') + '</div>';

  const entries =
    '<div class="entries">' +
      '<a class="entry" href="#/size"><span>📏</span>尺码助手</a>' +
      '<a class="entry" href="#/care"><span>🧺</span>洗护查询</a>' +
    '</div>';

  $view().innerHTML = banner + entries +
    '<div class="card">' +
      '<label class="field"><span>问点什么</span>' +
        '<textarea id="q" maxlength="300" placeholder="例：烫金和亮片可以机洗吗？"></textarea></label>' +
      '<div class="chips">' + SUGGESTIONS.map((text) =>
        '<button class="chip" data-suggest="' + esc(text) + '">' + esc(text) + '</button>').join('') + '</div>' +
      '<button class="primary" id="ask">查知识库</button>' +
    '</div>' +
    '<div id="result"></div>';

  const input = document.getElementById('q');
  const ask = document.getElementById('ask');

  const run = async () => {
    const question = input.value.trim();
    if (!question) { toast('先写一个问题'); return; }
    ask.disabled = true;
    ask.textContent = '检索中…';
    const box = document.getElementById('result');
    box.innerHTML = '<div class="card"><div class="loading">检索并生成中…</div></div>';
    try {
      const data = await request('/api/kb/ask?q=' + encodeURIComponent(question));
      if (!data.count) {
        // 空检索是**设计内的结果**（上游五道闸门的用途），不是错误
        box.innerHTML = '<div class="card"><div class="notice info">知识库里暂时没有这方面的内容。' +
          '这不是出错——宁可说「没有」，也不给一个看起来像答案的猜测。</div>' +
          '<div class="hint">分词：' + esc((data.terms || []).join(' / ')) + '</div></div>';
        return;
      }
      const problems = (data.problems || []).length
        ? '<div class="notice warn">答案已降级为原文摘录：' + esc(data.problems.join('；')) + '</div>' : '';
      const note = data.note && !data.used_llm
        ? '<div class="notice info">' + esc(data.note) + '</div>' : '';
      const intents = (data.subqueries || []).length
        ? '<div class="hint">已按多个意图拆分检索：' + esc(data.subqueries.join(' / ')) + '</div>' : '';
      const srcTitle = data.count > 1
        ? '来源（点答案里的编号可跳转）'
        : '来源（最相关的一条）';
      const sources = data.hits.map((hit, index) =>
        '<div class="src-block" data-src="' + (index + 1) + '">' +
          '<div class="head">[' + hit.rank + '] ' +
            esc([hit.author, hit.created_at, hit.retriever].filter(Boolean).join(' · ')) +
            ' · 相关度 ' + hit.score.toFixed(3) + '</div>' +
          '<div class="body">' + esc(hit.text) + '</div></div>').join('');

      box.innerHTML =
        '<div class="card"><h2>回答</h2><div class="answer" id="answer">' + formatAnswer(data.answer) + '</div>' +
          intents + '</div>' +
        problems + note +
        '<div class="card"><h2>' + srcTitle + '</h2>' + sources + '</div>';

      document.querySelectorAll('#answer .cite').forEach((node) => {
        node.addEventListener('click', () => {
          const target = document.querySelector('[data-src="' + node.dataset.n + '"]');
          if (!target) return;
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
          target.classList.add('flash');
          setTimeout(() => target.classList.remove('flash'), 1200);
        });
      });
    } catch (error) {
      box.innerHTML = '<div class="card"><div class="notice warn">' + esc(error.message) + '</div></div>';
    } finally {
      ask.disabled = false;
      ask.textContent = '查知识库';
    }
  };

  ask.addEventListener('click', run);
  input.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') run();
  });
  document.querySelectorAll('[data-suggest]').forEach((button) => {
    button.addEventListener('click', () => { input.value = button.dataset.suggest; run(); });
  });
};

/* ------------------------------------------------------------------ 尺码 */

function renderSizeResult(r) {
  const head = r.recommended
    ? '<div class="verdict"><span class="big">' + esc(r.recommended) + '</span> 码</div>'
    : '<div class="verdict bad">没有合适的码</div>';
  const conf = { high: '高', medium: '中', low: '低' }[r.confidence] || r.confidence;
  const rows = r.evaluated.map((item) => {
    const short = Object.keys(item.shortfall).map((dim) => DIM_LABEL[dim] + '差 ' + item.shortfall[dim]).join('、');
    const slack = Object.keys(item.slack).map((dim) => DIM_LABEL[dim] + '松 ' + item.slack[dim]).join('、');
    return '<tr><td>' + esc(item.size) + '</td><td>' + (item.ok ? '可穿' : '不够') + '</td><td>' +
      esc(short || slack || '—') + '</td></tr>';
  }).join('');
  const warnings = (r.warnings || []).map((text) => '<div class="notice warn">' + esc(text) + '</div>').join('');
  const advice = (r.advice || []).map((text) => '<li>' + esc(text) + '</li>').join('');
  const required = Object.keys(r.required)
    .map((dim) => DIM_LABEL[dim] + '需 ≥ ' + r.required[dim]).join('、');

  return '<div class="card"><h2>结论</h2>' + head +
    '<div class="kv">置信度 ' + esc(conf) + '：' +
      esc(Object.keys(r.measured).map((d) => DIM_LABEL[d] + ' ' + r.measured[d]).join(' / ')) +
    '<br>' + esc(required) + '（已含富余量与版型修正）</div>' +
    warnings +
    '<table class="tbl"><thead><tr><th>码</th><th>判断</th><th>余量</th></tr></thead><tbody>' + rows + '</tbody></table>' +
    (advice ? '<ul class="plain">' + advice + '</ul>' : '') +
    '<p class="hint">依据：' + esc(r.source || '未标注来源') +
    (r.chart_version ? '（表版本 ' + esc(r.chart_version) + '）' : '') + '</p></div>';
}

RENDERERS.size = async function renderSize() {
  const data = await request('/api/size/brands');
  const brands = data.brands || [];
  if (!brands.length) {
    $view().innerHTML = '<div class="card"><div class="notice warn">还没有任何品牌尺码表。' +
      '按 content/size/_README.md 加一个 JSON 就能用。</div></div>';
    return;
  }
  const byId = new Map(brands.map((item) => [item.brand, item]));
  const initial = byId.has(state.param) ? state.param : brands[0].brand;

  $view().innerHTML =
    '<div class="card"><h2>按品牌尺码表算</h2>' +
      '<div class="notice info">只接受真实身体尺寸（厘米）。身高体重推不出胸腰围，硬给一个数字就是伪精确。</div>' +
      '<div id="size-remember"></div>' +
      '<label class="field"><span>品牌</span><select id="size-brand">' +
        brands.map((item) => '<option value="' + esc(item.brand) + '"' +
          (item.brand === initial ? ' selected' : '') + '>' +
          esc(item.display_name) + ' · ' + esc(item.fit) + '</option>').join('') +
      '</select></label>' +
      '<label class="field"><span>品类</span><select id="size-cat"></select></label>' +
      '<div id="size-dims"></div>' +
      '<button class="primary" id="size-go">算一下</button>' +
      '<p id="size-meta" class="hint"></p>' +
    '</div><div id="size-result"></div>';

  const brandSel = document.getElementById('size-brand');
  const catSel = document.getElementById('size-cat');
  const dims = document.getElementById('size-dims');
  const meta = document.getElementById('size-meta');

  /* 记住的尺寸只存在这个浏览器里（localStorage），不上服务端。
     所以这里必须给一个**看得见的清除入口**——否则用户永远不知道
     "它记住了什么、存在哪、怎么删"。 */
  function drawRemember() {
    const box = document.getElementById('size-remember');
    const stored = getMeasurements();
    if (!stored || !Object.keys(stored).length) {
      box.innerHTML = '<div class="notice info">算过之后可以把尺寸记在这个浏览器里，' +
        '推荐页会直接标出你的码。服务端不保存。</div>';
      return;
    }
    const text = Object.keys(stored)
      .map((key) => (DIM_LABEL[key] || key) + ' ' + stored[key]).join(' / ');
    box.innerHTML = '<div class="notice ok">已记住：' + esc(text) +
      ' <button class="ghost sm" id="size-forget">清除</button></div>';
    const forget = document.getElementById('size-forget');
    if (forget) forget.addEventListener('click', () => {
      setMeasurements(null);
      Object.keys(stored).forEach((key) => {
        const input = dims.querySelector('input[name="' + key + '"]');
        if (input) input.value = '';
      });
      drawRemember();
      toast('已清除记住的尺寸');
    });
  }

  function drawDims() {
    const chart = byId.get(brandSel.value);
    const keys = ((chart && chart.category_keys) || {})[catSel.value] || [];
    const usable = keys.filter((key) => key !== 'length');   // 裙长不需要用户填
    const stored = getMeasurements() || {};
    dims.innerHTML = usable.length
      ? usable.map((key) => '<label class="field"><span>' + esc(DIM_LABEL[key] || key) +
          '（cm）</span><input name="' + esc(key) + '" type="number" step="0.5" min="1" ' +
          'inputmode="decimal" value="' + (stored[key] ? esc(stored[key]) : '') + '"></label>').join('')
      : '<div class="notice warn">这个品类还没有可判断的尺寸维度。</div>';
    meta.textContent = [chart && chart.source, chart && chart.chart_version ? '表版本 ' + chart.chart_version : '']
      .filter(Boolean).join(' · ');
  }

  function drawCategories() {
    const chart = byId.get(brandSel.value);
    catSel.innerHTML = ((chart && chart.categories) || []).map((name) =>
      '<option value="' + esc(name) + '">' + esc(name) + '</option>').join('');
    drawDims();
  }

  brandSel.addEventListener('change', drawCategories);
  catSel.addEventListener('change', drawDims);
  drawCategories();
  drawRemember();

  document.getElementById('size-go').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const payload = { brand: brandSel.value, category: catSel.value };
    dims.querySelectorAll('input[name]').forEach((input) => {
      if (input.value) payload[input.name] = Number(input.value);
    });
    const box = document.getElementById('size-result');
    box.innerHTML = '<div class="card"><div class="loading">计算中…</div></div>';
    button.disabled = true;
    try {
      box.innerHTML = renderSizeResult(await request('/api/size/recommend', { method: 'POST', body: payload }));
      // 算成功才记：填一半的尺寸记下来，下次会拿它去算别的品牌，结果不可信
      const measured = {};
      ['bust', 'waist', 'hip', 'shoulder'].forEach((key) => {
        if (payload[key]) measured[key] = payload[key];
      });
      if (Object.keys(measured).length) {
        setMeasurements(measured);
        drawRemember();
      }
    } catch (error) {
      box.innerHTML = '<div class="card"><div class="notice warn">' + esc(error.message) + '</div></div>';
    } finally {
      button.disabled = false;
    }
  });
};

/* ------------------------------------------------------------------ 洗护 */

function renderCareResult(r) {
  if (!r.known) {
    return '<div class="card"><div class="notice info"><b>' + esc(r.headline) + '</b><br>' +
      esc((r.advice || []).join(' ')) + '</div></div>';
  }
  const verdict = r.machine_washable
    ? '<div class="verdict ok">' + esc(r.headline) + '</div>'
    : '<div class="verdict bad">' + esc(r.headline) + '</div>';
  const advice = (r.advice || []).map((text) => '<li>' + esc(text) + '</li>').join('');
  const refs = (r.refs || []).length
    ? '<p class="hint">相关条目：' + esc(r.refs.join('、')) + '</p>' : '';
  return '<div class="card"><h2>结论</h2>' + verdict +
    '<ul class="plain">' + advice + '</ul>' + refs + '</div>';
}

RENDERERS.care = async function renderCare() {
  const options = await request('/api/care/options');
  const groups = [
    ['materials', '面料', options.materials || []],
    ['crafts', '工艺', options.crafts || []],
    ['notes', '情况', options.notes || []],
  ];
  const selected = { materials: new Set(), crafts: new Set(), notes: new Set() };

  const blocks = groups.map(([key, label, values]) =>
    '<div class="field"><span class="label">' + esc(label) + '</span><div class="pick" data-group="' + esc(key) + '">' +
      values.map((value) => '<button type="button" class="chip" data-value="' + esc(value) + '">' +
        esc(value) + '</button>').join('') +
      '</div></div>').join('');

  $view().innerHTML =
    '<div class="card"><h2>这件怎么洗</h2>' +
      '<div class="notice info">选面料、工艺和情况，规则匹配会给一个明确结论。' +
      '没有匹配到规则时会直接说不知道——不套用"最接近"的一条，洗坏一条裙子的代价太大。</div>' +
      blocks +
      '<button class="primary" id="care-go">给建议</button>' +
    '</div><div id="care-result"></div>';

  document.querySelectorAll('[data-group]').forEach((groupNode) => {
    const key = groupNode.dataset.group;
    groupNode.querySelectorAll('[data-value]').forEach((button) => {
      button.addEventListener('click', () => {
        const value = button.dataset.value;
        if (selected[key].has(value)) { selected[key].delete(value); button.classList.remove('sel'); }
        else { selected[key].add(value); button.classList.add('sel'); }
      });
    });
  });

  document.getElementById('care-go').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const box = document.getElementById('care-result');
    const payload = {
      materials: [...selected.materials],
      crafts: [...selected.crafts],
      notes: [...selected.notes],
    };
    if (!payload.materials.length && !payload.crafts.length && !payload.notes.length) {
      toast('先选一项');
      return;
    }
    box.innerHTML = '<div class="card"><div class="loading">匹配中…</div></div>';
    button.disabled = true;
    try {
      box.innerHTML = renderCareResult(await request('/api/care/advice', { method: 'POST', body: payload }));
    } catch (error) {
      box.innerHTML = '<div class="card"><div class="notice warn">' + esc(error.message) + '</div></div>';
    } finally {
      button.disabled = false;
    }
  });
};

/* ------------------------------------------------------------------ 我的 */

function authCard(orphanFollows) {
  const legacy = orphanFollows
    ? '<div class="notice warn">本机还有 ' + orphanFollows +
      ' 条升级前留下的关注。第一个注册（或登录）的账号会自动接手它们。</div>'
    : '';
  return '<div class="card"><h2>登录 / 注册</h2>' +
    '<div class="notice info">浏览不需要登录。登录之后才能关注品牌、设置偏好——' +
      '这两样都是跟着账号走的，所以需要知道你是谁。</div>' +
    legacy +
    '<form id="auth-form">' +
      '<label class="field"><span>用户名（3-32 个字符）</span>' +
        '<input name="username" autocomplete="username" minlength="3" maxlength="32" required></label>' +
      '<label class="field"><span>密码（至少 8 位）</span>' +
        '<input name="password" type="password" autocomplete="current-password" minlength="8" required></label>' +
      '<div class="row2">' +
        '<button class="primary" type="submit">登录</button>' +
        '<button class="ghost" type="button" id="register-btn">注册新账号</button>' +
      '</div>' +
      '<p class="hint">没有找回密码功能——忘了密码，这个账号的关注和偏好就拿不回来了，' +
        '请存进密码管理器。第一个注册的账号会接手升级前留在本机的关注列表。</p>' +
    '</form></div>';
}

function wireAuth() {
  const form = document.getElementById('auth-form');
  if (!form) return;
  const submit = async (mode) => {
    const username = form.username.value.trim();
    const password = form.password.value;
    if (!username || !password) { toast('用户名和密码都要填'); return; }
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const data = await request('/api/auth/' + mode, {
        method: 'POST', body: { username, password },
      });
      setToken(data.token);
      state.user = data.user;
      toast(mode === 'register'
        ? (data.claimed_follows ? '注册成功，已接手 ' + data.claimed_follows + ' 条旧关注' : '注册成功')
        : '登录成功');
      await RENDERERS.me();
    } catch (error) {
      toast((mode === 'register' ? '注册失败：' : '登录失败：') + error.message);
      buttons.forEach((button) => { button.disabled = false; });
    }
  };
  form.addEventListener('submit', (event) => { event.preventDefault(); submit('login'); });
  const registerBtn = document.getElementById('register-btn');
  if (registerBtn) registerBtn.addEventListener('click', () => submit('register'));
}

RENDERERS.me = async function renderMe() {
  const [feed, health] = await Promise.all([
    request('/api/feed?limit=200').catch(() => null),
    request('/api/health').catch(() => null),
  ]);
  const summary = (feed && feed.summary)
    || { total: 0, brands: 0, recent: 0, latest: '', preferences: {} };
  const described = (feed && feed.described) || [];
  const selected = Object.values(summary.preferences || {})
    .reduce((total, value) => total + (Number(value) || 0), 0);

  const account = state.user
    ? '<div class="card"><h2>账号</h2>' +
        '<div class="brand-row"><div class="grow">' +
          '<div class="name">' + esc(state.user.username) + '</div>' +
          '<div class="meta">注册于 ' + esc((state.user.created_at || '').slice(0, 10)) + '</div>' +
        '</div><button class="ghost sm" id="logout">退出登录</button></div>' +
        '<button class="ghost sm danger" id="delete-account">删除账号</button>' +
      '</div>'
    : authCard(health && health.orphan_shops);

  $view().innerHTML = account +
    (state.user
      // 身材参数卡片由 profile.js 挂载（独立文件，避免和并行改动抢 app.js）
      ? '<div class="card"><h2>身材参数</h2><div id="body-profile">' +
          '<div class="hint">载入中…</div></div></div>'
      : '') +
    (state.user
      ? '<div class="card"><h2>我的偏好</h2>' + describePreferences(described) +
          '<p><a class="ghost sm" href="#/preferences">' +
            (selected ? '去调整偏好' : '去设置偏好') + '</a></p></div>'
      : '') +
    '<div class="card"><h2>概况</h2><div class="summary">' +
      '<div><div class="num">' + summary.total + '</div><div class="lbl">上新条目</div></div>' +
      '<div><div class="num">' + summary.brands + '</div><div class="lbl">品牌</div></div>' +
      '<div><div class="num">' + selected + '</div><div class="lbl">偏好标签</div></div>' +
      '<div><div class="num">' + summary.recent + '</div><div class="lbl">近 7 天</div></div>' +
    '</div></div>' +
    '<div class="card"><h2>备份</h2>' +
      '<p class="hint">导出包含账号、偏好、知识库、尺码表、洗护规则与全部上新条目，可独立恢复。</p>' +
      '<button class="primary" id="export">导出备份 zip</button></div>' +
    '<div class="card"><h2>状态</h2><div class="status">' +
      '服务版本：' + esc(health ? health.version : '未知') + '<br>' +
      '账号数：' + esc(health ? health.users : 0) + '<br>' +
      '上新条目：' + esc(health ? health.newarrivals : 0) +
        (health && health.newarrival_problems ? '（' + health.newarrival_problems + ' 条读取失败）' : '') + '<br>' +
      '知识库片段：' + esc(health && health.kb ? health.kb.chunks : 0) + '<br>' +
      '问答条数：每次最多 ' + esc(health ? health.top_k : 1) + ' 条<br>' +
      '模型：' + (health && health.llm_enabled ? '已配置' : '未配置（原文摘录模式）') + '<br>' +
      '最新上新：' + esc(summary.latest || '—') +
    '</div></div>' +
    '<div class="card"><h2>关于</h2><p class="hint">' +
      '数据都在这台机器上，不上传任何第三方。上新条目与知识库均由你自己维护，' +
      '不包含抓取的他人内容。密码只存 scrypt 哈希，登录态是本地令牌。' +
      '问答会把你输入的问题与命中的知识片段发送给所配置的模型接口。</p></div>';

  if (state.user) {
    const logout = document.getElementById('logout');
    if (logout) {
      logout.addEventListener('click', async () => {
        try { await request('/api/auth/logout', { method: 'POST' }); } catch (_) { /* 令牌已失效也无所谓 */ }
        setToken('');
        state.user = null;
        toast('已退出登录');
        await RENDERERS.me();
      });
    }
    const remove = document.getElementById('delete-account');
    if (remove) {
      remove.addEventListener('click', async () => {
        if (!confirm('删除账号「' + state.user.username + '」？\n关注和偏好会一并删除，不可恢复。')) return;
        try {
          await request('/api/auth/me', { method: 'DELETE' });
          setToken('');
          state.user = null;
          toast('账号已删除');
          await RENDERERS.me();
        } catch (error) {
          toast('删除失败：' + error.message);
        }
      });
    }
  } else {
    wireAuth();
  }

  document.getElementById('export').addEventListener('click', () => {
    window.location.href = '/api/export';
    toast('正在打包…');
  });
};

/* ------------------------------------------------------------------ 启动 */

async function ping() {
  const dot = document.getElementById('netdot');
  try {
    await request('/api/health');
    dot.className = 'dot ok';
  } catch (_) {
    dot.className = 'dot bad';
  }
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* 离线能力不可用不影响主流程 */ });
  });
}

// 启动顺序有讲究：先确认登录态，再渲染第一页。
// 反过来的话「我的」会先闪一下登录表单，然后才变成已登录状态。
(async function boot() {
  await refreshUser();
  route();
  ping();
})();
