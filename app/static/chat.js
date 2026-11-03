/* 助手（agent 聊天页）。独立文件，尽量不碰 app.js —— 那边有并行改动。
   由 app.js 的 RENDERERS.kb 委托调用：window.renderAssistant()。 */
'use strict';

(function () {
  const TOKEN_KEY = 'lo-life-token';
  const MAX_HISTORY = 8;          // 与服务端 writer.MAX_HISTORY_TURNS 对齐

  const state = { messages: [], busy: false };

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

  function view() { return document.getElementById('view'); }

  /* ---------------------------------------------------------------- 渲染 */

  function messageNode(message) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + message.role;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    // 回答里的 [n] 做成小上标，点不到也别扭——这里只做高亮，不做跳转
    bubble.innerHTML = esc(message.content || '')
      .replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>')
      .replace(/\n/g, '<br>');

    if (message.steps && message.steps.length) {
      const details = document.createElement('details');
      details.className = 'steps';
      const used = message.steps.filter((step) => step.kind === 'tool');
      details.innerHTML = '<summary>过程（' + used.length + ' 步）</summary>' +
        message.steps.map((step) => step.kind === 'tool'
          ? '<div class="step">· ' + esc(step.tool) + ' → ' +
            (step.empty ? '空' : (step.evidence || 0) + ' 条') +
            (step.summary ? '<span class="step-sum">' + esc(step.summary) + '</span>' : '') + '</div>'
          : '<div class="step">· ' + esc(step.kind) + '</div>').join('');
      wrap.appendChild(bubble);
      wrap.appendChild(details);
    } else {
      wrap.appendChild(bubble);
    }

    if (message.evidence && message.evidence.length) {
      const evidence = document.createElement('div');
      evidence.className = 'evidence';
      evidence.innerHTML = message.evidence.map((item) =>
        '<div class="ev"><span class="ev-n">[' + item.n + ']</span>' +
        '<span class="ev-title">' + esc(item.title) + '</span>' +
        '<div class="ev-text">' + esc(item.text) + '</div></div>').join('');
      wrap.appendChild(evidence);
    }
    return wrap;
  }

  function paintMessages(host) {
    host.innerHTML = '';
    if (!state.messages.length) {
      host.innerHTML = '<div class="hint" style="text-align:center;padding:18px 0">' +
        '问点什么。它会自己决定查上新、查知识库、算尺码还是查洗护。</div>';
      return;
    }
    state.messages.forEach((message) => host.appendChild(messageNode(message)));
    host.scrollTop = host.scrollHeight;
  }

  /* ---------------------------------------------------------------- 发送 */

  function historyPayload() {
    return state.messages
      .filter((message) => !message.error && message.content)
      .slice(-MAX_HISTORY)
      .map((message) => ({ role: message.role, content: String(message.content).slice(0, 500) }));
  }

  async function ask(question, ui) {
    if (state.busy) return;
    state.busy = true;
    ui.input.disabled = true;
    ui.send.disabled = true;

    const userMessage = { role: 'user', content: question };
    // 历史在**追加本轮问题之前**取，否则问题会被发两遍
    const history = historyPayload();
    state.messages.push(userMessage);

    const answer = { role: 'assistant', content: '', steps: [], evidence: [] };
    state.messages.push(answer);
    paintMessages(ui.list);

    let bubble = ui.list.lastElementChild.querySelector('.bubble');
    let steps = [];
    let started = performance.now();

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: Object.assign(
          { 'Content-Type': 'application/json; charset=utf-8' },
          token() ? { Authorization: 'Bearer ' + token() } : {}
        ),
        body: JSON.stringify({ message: question, max_steps: 4, history: history }),
      });

      if (!response.ok) {
        let detail = response.status + ' ' + response.statusText;
        try { const data = await response.json(); if (data.detail) detail = data.detail; } catch (_) { /* 非 JSON */ }
        throw new Error(detail);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let cut;
        while ((cut = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const line = frame.split('\n').find((item) => item.indexOf('data: ') === 0);
          if (!line) continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch (_) { continue; }

          if (event.type === 'step') {
            steps.push(event);
            if (event.kind === 'tool' && !answer.content) {
              bubble.innerHTML = '<span class="thinking">' + esc(event.tool) + '…</span>';
            }
          } else if (event.type === 'delta') {
            answer.content += event.text;
            bubble.innerHTML = esc(answer.content)
              .replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>')
              .replace(/\n/g, '<br>');
            ui.list.scrollTop = ui.list.scrollHeight;
          } else if (event.type === 'replace') {
            answer.content = event.text;
            bubble.innerHTML = esc(answer.content)
              .replace(/\[(\d+)\]/g, '<sup class="cite">[$1]</sup>')
              .replace(/\n/g, '<br>');
          } else if (event.type === 'final') {
            answer.content = event.answer;
            answer.steps = event.steps || steps;
            answer.evidence = event.evidence || [];
            answer.refused = !!event.refused;
          } else if (event.type === 'error') {
            throw new Error(event.detail || '流式失败');
          }
        }
      }

      answer.ms = Math.round(performance.now() - started);
      paintMessages(ui.list);
    } catch (error) {
      answer.error = true;
      answer.content = '出错了：' + error.message;
      paintMessages(ui.list);
    } finally {
      state.busy = false;
      ui.input.disabled = false;
      ui.send.disabled = false;
      ui.input.focus();
    }
  }

  /* ---------------------------------------------------------------- 页面 */

  async function renderAssistant() {
    const root = view();
    let status = null;
    try {
      const response = await fetch('/api/chat/status');
      status = await response.json();
    } catch (error) {
      root.innerHTML = '<div class="card"><div class="notice warn">助手状态未知：' + esc(error.message) + '</div></div>';
      return;
    }

    if (!status.enabled) {
      root.innerHTML = '<div class="card"><h2>助手还没启用</h2>' +
        '<div class="notice warn">' + esc(status.hint || '需要配置模型') + '</div>' +
        '<p class="hint">没有模型时不做"假装能聊"的壳：这个问题需要它自己决定查哪个工具，' +
        '抽取式兜底没有决策能力。</p></div>';
      return;
    }

    root.innerHTML =
      '<div class="card chat-card">' +
        '<div class="chat-head">' +
          '<span class="chat-model">' + esc(status.model) + '</span>' +
          '<button class="ghost sm" id="chat-clear">清空对话</button>' +
        '</div>' +
        '<div class="chat-list" id="chat-list"></div>' +
        '<div class="chat-input">' +
          '<textarea id="chat-text" rows="1" maxlength="300" ' +
            'placeholder="例：蕾丝和真丝能机洗吗？"></textarea>' +
          '<button class="primary" id="chat-send">发送</button>' +
        '</div>' +
        '<div class="chips chat-chips">' +
          ['蕾丝和真丝能机洗吗？', 'baby 最近有什么上新？', '烫金能机洗吗？'].map((text) =>
            '<button class="chip" data-q="' + esc(text) + '">' + esc(text) + '</button>').join('') +
        '</div>' +
      '</div>';

    const ui = {
      list: document.getElementById('chat-list'),
      input: document.getElementById('chat-text'),
      send: document.getElementById('chat-send'),
    };

    const send = () => {
      const question = ui.input.value.trim();
      if (!question) { toast('先写一个问题'); return; }
      ui.input.value = '';
      ask(question, ui);
    };

    ui.send.addEventListener('click', send);
    ui.input.addEventListener('keydown', (event) => {
      // 回车发送，Shift+回车换行（手机上回车就是换行，靠按钮发）
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        send();
      }
    });
    root.querySelectorAll('[data-q]').forEach((button) => {
      button.addEventListener('click', () => {
        ui.input.value = button.dataset.q;
        send();
      });
    });
    document.getElementById('chat-clear').addEventListener('click', () => {
      state.messages = [];
      paintMessages(ui.list);
    });

    paintMessages(ui.list);
    ui.input.focus();
  }

  window.renderAssistant = renderAssistant;
})();
