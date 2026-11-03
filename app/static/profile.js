/* 身材参数卡片。独立文件：app.js 正被并行改动，这里用 MutationObserver 自动挂载，
   调用方只需要在「我的」页里留一个 <div id="body-profile">。 */
'use strict';

(function () {
  const TOKEN_KEY = 'lo-life-token';
  /* app.js 的 getMeasurements() 是同步读 localStorage 的（列表渲染路径）。
     服务端才是权威来源，这里在读取/保存/清除后把四个围度镜像过去，
     这样「我的」页填完，尺码页和列表徽标立刻就能用同一份数字。 */
  const MEASURE_KEY = 'lo-life-measure';
  const MEASURE_KEYS = ['bust', 'waist', 'hip', 'shoulder'];

  function mirrorMeasurements(body) {
    try {
      const usable = {};
      MEASURE_KEYS.forEach((key) => {
        if (body && body[key] != null) usable[key] = body[key];
      });
      if (Object.keys(usable).length) localStorage.setItem(MEASURE_KEY, JSON.stringify(usable));
      else localStorage.removeItem(MEASURE_KEY);
    } catch (_) { /* 隐私模式写不了：不影响服务端保存 */ }
  }

  const FIELDS = [
    ['height', '身高', 'cm', '160'],
    ['weight', '体重', 'kg', '50'],
    ['bust', '胸围', 'cm', '86'],
    ['waist', '腰围', 'cm', '68'],
    ['hip', '臀围', 'cm', '90'],
    ['shoulder', '肩宽', 'cm', '38'],
  ];

  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const token = () => {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; }
  };

  function toast(message) {
    const node = document.getElementById('toast');
    if (!node) return;
    node.textContent = message;
    node.className = 'toast show';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { node.className = 'toast'; }, 2400);
  }

  function headers() {
    const base = { 'Content-Type': 'application/json; charset=utf-8' };
    return token() ? Object.assign(base, { Authorization: 'Bearer ' + token() }) : base;
  }

  function formHtml(data, body) {
    const limits = data.limits || {};
    const inputs = FIELDS.map(([key, label, unit]) => {
      const limit = limits[key] || {};
      return '<label class="body-field"><span>' + label + '</span>' +
        '<input type="number" step="0.1" inputmode="decimal" data-body="' + key + '"' +
        ' min="' + (limit.min != null ? limit.min : '') + '"' +
        ' max="' + (limit.max != null ? limit.max : '') + '"' +
        ' placeholder="' + esc(limit.label || label) + '"' +
        ' value="' + (body[key] != null ? esc(body[key]) : '') + '">' +
        '<em>' + unit + '</em></label>';
    }).join('');

    const bmi = data.bmi
      ? '<div class="hint">BMI ' + esc(data.bmi.value) + '（' + esc(data.bmi.band) + '）</div>'
      : '';

    return '<div class="body-grid">' + inputs + '</div>' +
      bmi +
      '<p class="hint">' + esc(data.note || '') + '</p>' +
      '<div class="chips">' +
        '<button class="primary" id="body-save">保存</button>' +
        '<button class="ghost sm" id="body-clear">清除</button>' +
      '</div>' +
      (data.filled
        ? '<p class="hint">保存于 ' + esc((data.body.updated_at || '').replace('T', ' ')) + '</p>'
        : '<p class="hint">还没填。填了之后助手判断合身时就不用再问你三围。</p>');
  }

  function collect(host) {
    const payload = {};
    host.querySelectorAll('[data-body]').forEach((input) => {
      const key = input.dataset.body;
      const raw = input.value.trim();
      // 空输入 = 清空该项（服务端约定 null 表示删除）
      payload[key] = raw === '' ? null : Number(raw);
    });
    return payload;
  }

  async function mount(host) {
    if (!host || host.dataset.mounted === '1') return;
    host.dataset.mounted = '1';

    if (!token()) {
      host.innerHTML = '<p class="hint">登录后才能保存身材参数——助手会用它判断合身。</p>';
      return;
    }

    let data;
    try {
      const response = await fetch('/api/profile/body', { headers: headers() });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      data = await response.json();
    } catch (error) {
      host.dataset.mounted = '0';
      host.innerHTML = '<p class="hint">读取失败：' + esc(error.message) + '</p>';
      return;
    }

    host.innerHTML = formHtml(data, data.body || {});
    if (data.filled) mirrorMeasurements(data.body || {});

    document.getElementById('body-save').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const response = await fetch('/api/profile/body', {
          method: 'PUT', headers: headers(), body: JSON.stringify(collect(host)),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || ('HTTP ' + response.status));
        mirrorMeasurements(payload.body || {});
        host.dataset.mounted = '0';
        await mount(host);
        toast('身材参数已保存');
      } catch (error) {
        toast('保存失败：' + error.message);
        button.disabled = false;
      }
    });

    document.getElementById('body-clear').addEventListener('click', async () => {
      if (!window.confirm('清除已保存的身材参数？')) return;
      const response = await fetch('/api/profile/body', { method: 'DELETE', headers: headers() });
      if (!response.ok) { toast('清除失败'); return; }
      mirrorMeasurements({});
      host.dataset.mounted = '0';
      await mount(host);
      toast('已清除');
    });
  }

  // 「我的」页是异步渲染的，渲染完才存在容器。用 MutationObserver 等它出现，
  // 比 setInterval 轮询省事，也不会在别的页面上白跑。
  function autoMount() {
    const host = document.getElementById('body-profile');
    if (host) mount(host);
  }

  function watch() {
    const view = document.getElementById('view');
    if (!view) return;
    new MutationObserver(autoMount).observe(view, { childList: true });
    autoMount();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', watch);
  } else {
    watch();
  }
})();
