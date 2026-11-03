'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const model = { state: null, csrf: null, polling: false, busy: false, period: 1, hover: -1, clockOffset: 0, failures: 0, noticeTimer: null };
  const actionLabels = { hold: '继续观察', wait: '继续观察', observe: '继续观察', open_long: '开仓做多', open_short: '开仓做空', close: '平仓', close_long: '平多仓', close_short: '平空仓', tighten_stop: '收紧止损', update_stop: '调整止损' };
  const statusLabels = { filled: '已成交', executed: '已执行', not_executed: '仅分析', rejected: '已拒绝', pending: '等待成交', cancelled: '已撤销', canceled: '已撤销', blocked: '已阻止', accepted: '已接受', completed: '已完成', failed: '失败', waiting: '等待验证', pending_validation: '等待验证', pending_validation_samples: '验证样本不足', historical_rejected: '留出验证未通过', contract_changed: '合约变化已停止', superseded: '已被新版替代', insufficient_data: '样本不足', shadow: '影子模拟', shadow_testing: '影子模拟', promoted: '已启用', active: '使用中', rolled_back: '已回退', passed: '验证通过', evaluated: '已评估', draft: '候选方案' };
  const riskLabels = { low: '较低', medium: '中等', moderate: '中等', high: '较高', critical: '高风险', unknown: '未知' };
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const priceFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1, minimumFractionDigits: 1 });
  const compactFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1, notation: 'compact' });
  const timeFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const minuteFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit' });
  const fullFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const first = (...values) => values.find((value) => value !== undefined && value !== null && value !== '');
  const num = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  const money = (value) => num(value) === null ? '—' : numberFormat.format(Number(value));
  const price = (value) => num(value) === null ? '—' : priceFormat.format(Number(value));
  const count = (value) => num(value) === null ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(Number(value));
  const compact = (value) => num(value) === null ? '—' : compactFormat.format(Number(value));
  const array = (value) => Array.isArray(value) ? value : [];
  const text = (id, value) => { const element = $(id); if (element) element.textContent = value === null || value === undefined || value === '' ? '—' : String(value); };
  const classed = (element, name, enabled) => { if (element) element.classList.toggle(name, Boolean(enabled)); };
  const pnl = (id, value, signed = false) => {
    const element = $(id); const valueNum = num(value);
    text(id, valueNum === null ? '—' : (signed && valueNum > 0 ? '+' : '') + money(valueNum));
    classed(element, 'positive', valueNum !== null && valueNum > 0);
    classed(element, 'negative', valueNum !== null && valueNum < 0);
  };
  const el = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  };

  function dateOf(value) {
    if (value === null || value === undefined || value === '') return null;
    let input = value;
    if (typeof value === 'number') input = value > 1e16 ? value / 1e6 : value > 1e11 ? value : value * 1000;
    if (typeof input === 'string' && /^\d{4}-\d\d-\d\d \d\d:/.test(input)) input = input.replace(' ', 'T') + (/[zZ]|[+-]\d\d:\d\d$/.test(input) ? '' : '+08:00');
    if (typeof input === 'string' && /^\d{4}-\d\d-\d\dT\d\d:/.test(input) && !/[zZ]|[+-]\d\d:\d\d$/.test(input)) input += '+08:00';
    const result = new Date(input);
    return Number.isNaN(result.getTime()) ? null : result;
  }
  const time = (value, full = false) => { const date = dateOf(value); return date ? (full ? fullFormat : timeFormat).format(date) : '—'; };
  const isoTime = (item) => first(item?.timestamp, item?.created_at, item?.time, item?.datetime, item?.checked_at, item?.received_at);

  function plain(value) {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (Array.isArray(value)) return value.map(plain).filter(Boolean).join('；');
    if (value && typeof value === 'object') {
      const chosen = first(value.summary, value.reason, value.assessment, value.message, value.description, value.text, value.rationale);
      if (chosen !== undefined) return plain(chosen);
      return Object.entries(value).filter(([key]) => !['risk_level', 'action', 'strategy_version', 'status'].includes(key)).map(([, item]) => plain(item)).filter(Boolean).join('；');
    }
    return '';
  }

  function readableAI(value) {
    return plain(value).replace(/\beligible_actions\b/g, '允许的交易动作').replace(/\bexecution_allowed\b/g, '当前成交条件').replace(/\bopen_long\b/g, '开仓做多').replace(/\bopen_short\b/g, '开仓做空').replace(/\bhold\b/g, '观望').replace(/\bobserve\b/g, '观望').replace(/\bunknown\b/g, '未知').replace(/\bfalse\b/g, '不满足').replace(/\btrue\b/g, '满足');
  }

  function unpack(value) {
    if (!value || typeof value !== 'object') return value || {};
    let parsed = {};
    for (const key of ['data', 'payload', 'decision', 'result', 'content']) {
      const raw = value[key];
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) parsed = { ...parsed, ...raw };
      if (typeof raw === 'string' && raw.trim().startsWith('{')) {
        try { const candidate = JSON.parse(raw); if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) parsed = { ...parsed, ...candidate }; } catch (_) { /* A display record may contain plain text. */ }
      }
    }
    return { ...value, ...parsed };
  }

  function notify(message, type = '', persistent = false) {
    clearTimeout(model.noticeTimer);
    const notice = $('notice');
    notice.textContent = message;
    notice.className = 'notice' + (type ? ' ' + type : '');
    notice.hidden = !message;
    if (!persistent && message) model.noticeTimer = setTimeout(() => { notice.hidden = true; }, 9000);
  }

  async function fetchJSON(url, options = {}, timeout = 10000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal });
      const contentType = response.headers.get('content-type') || '';
      const result = contentType.includes('application/json') ? await response.json() : {};
      if (!response.ok || result.ok === false) {
        const failure = new Error(plain(first(result.message, result.error, result.reason)) || `服务返回 ${response.status}`);
        failure.status = response.status;
        throw failure;
      }
      return result;
    } finally { clearTimeout(timer); }
  }

  async function bootstrap() {
    const result = await fetchJSON('/api/bootstrap');
    model.csrf = result.csrf_token;
    if (!model.csrf) throw new Error('本地服务校验尚未就绪，请稍后重试。');
  }

  async function poll() {
    if (model.polling) return;
    model.polling = true;
    try {
      const state = await fetchJSON('/api/state');
      model.state = state;
      const serverDate = dateOf(state.server_time);
      if (serverDate) model.clockOffset = serverDate.getTime() - Date.now();
      model.failures = 0;
      $('connection-status').className = 'connection connected';
      $('connection-status').lastElementChild.textContent = '本地服务已连接';
      render(state);
    } catch (error) {
      model.failures += 1;
      $('connection-status').className = 'connection disconnected';
      $('connection-status').lastElementChild.textContent = '正在重连';
      text('runtime-label', '本地服务连接中断');
      text('runtime-description', '正在自动重试；页面保留最后一次数据，请留意行情时效。');
      classed($('runtime-dot'), 'error', true);
      classed($('runtime-dot'), 'live', false);
      text('footer-status', '服务未连接 · 显示最后一次收到的数据');
      updateControls();
      if (model.failures === 1) notify('暂时无法连接本地服务，正在自动重试。', 'error', true);
    } finally {
      model.polling = false;
      setTimeout(poll, 2000);
    }
  }

  async function control(action) {
    if (model.busy) return;
    model.busy = true;
    updateControls();
    if (action === 'close') $('close-confirm').hidden = true;
    const messages = { start: '正在启动自动模拟…', pause: '正在暂停新开仓…', close: '正在提交模拟平仓…', analyze: '正在请求AI分析…', review: '正在生成交易复盘…', check: '正在读取连接与配置状态…' };
    notify(messages[action] || '正在处理…');
    try {
      if (!model.csrf) await bootstrap();
      const requestOptions = {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': model.csrf }, body: JSON.stringify({ action }),
      };
      let result;
      try { result = await fetchJSON('/api/control', requestOptions, action === 'check' ? 100000 : 45000); }
      catch (error) {
        // A rejected CSRF request has not reached control(); restart recovery is safe.
        if (error.status !== 403) throw error;
        await bootstrap();
        requestOptions.headers['X-CSRF-Token'] = model.csrf;
        result = await fetchJSON('/api/control', requestOptions, action === 'check' ? 100000 : 45000);
      }
      const defaults = { start: '自动模拟已启动，开仓仍需满足策略与风控条件。', pause: '已暂停新开仓，持仓风控继续运行。', close: '模拟平仓请求已处理，请查看成交与运行记录。', analyze: '分析请求已提交。', review: '复盘请求已提交。', check: '状态检查已完成。' };
      const resultStatus = first(result.status, result.result?.status);
      const isBlocked = ['failed', 'blocked', 'rejected'].includes(resultStatus);
      let message = plain(first(result.message, result.reason, result.result?.message, result.result?.reason)) || defaults[action];
      if (action === 'check' && typeof result.model_key_configured === 'boolean') message += result.model_key_configured ? ' 模型密钥已配置。' : ' 模型密钥尚未配置。';
      notify(message, isBlocked ? 'error' : 'success');
      const state = await fetchJSON('/api/state');
      model.state = state; render(state);
    } catch (error) {
      notify(error.name === 'AbortError' ? '请求仍未返回。请查看运行记录，确认处理状态后再操作。' : error.message || '请求失败，请稍后重试。', 'error', true);
    } finally { model.busy = false; updateControls(); }
  }

  function updateControls() {
    const ready = Boolean(model.state) && model.failures === 0;
    const runtime = model.state?.runtime || {};
    const running = Boolean(first(runtime.running, runtime.auto_trading_enabled, false));
    $('start-button').disabled = !ready || model.busy || running;
    $('pause-button').disabled = !ready || model.busy || !running;
    $('check-button').disabled = model.busy;
    $('analyze-button').disabled = !ready || model.busy || runtime.model_busy === true || ['analyzing', 'running'].includes(model.state?.ai?.status);
    $('review-button').disabled = !ready || model.busy || runtime.model_busy === true || model.state?.ai?.review_running === true;
    $('close-button').disabled = !ready || model.busy || !model.state?.position;
    $('confirm-close').disabled = model.busy;
    text('start-button', running ? '自动模拟运行中' : '▶ 启动自动模拟');
  }

  function render(state) {
    const account = state.account || {};
    const runtime = state.runtime || {};
    const market = state.market || {};
    const quote = market.quote || {};
    const calendar = state.calendar || {};
    const running = Boolean(first(runtime.running, runtime.auto_trading_enabled, false));
    const open = Boolean(first(calendar.session_open, market.session?.open, market.session?.is_open, false));
    text('runtime-label', running ? '自动模拟运行中' : '自动模拟已暂停');
    text('runtime-description', running ? (open ? '按策略评估交易机会，独立风控持续检查持仓' : '当前休市，保留持仓并等待下一交易时段') : '暂停新开仓，已有持仓仍接受程序风控检查');
    if (account.entry_blocked) text('runtime-description', plain(first(account.entry_block_reason, account.block_reason)) || '风控限制已触发，暂停新开仓');
    classed($('runtime-dot'), 'live', running && !account.entry_blocked);
    classed($('runtime-dot'), 'warning', !running || Boolean(account.entry_blocked));
    classed($('runtime-dot'), 'error', Boolean(runtime.last_error));
    text('session-label', first(calendar.session_label, calendar.session, market.session?.label, typeof market.session === 'string' ? market.session : null, open ? '交易时段' : '休市'));
    if (runtime.last_error) notify(plain(runtime.last_error), 'error', true);
    pnl('metric-equity', first(account.equity, account.balance, account.net_equity));
    $('metric-equity').classList.remove('positive', 'negative');
    text('metric-initial', money(first(account.initial_balance, account.initial_balance_cny)));
    pnl('metric-daily', first(account.daily_pnl, account.day_pnl), true);
    text('metric-daily-limit', num(first(account.daily_loss_limit, account.daily_loss_stop_new_entries_cny)) === null ? '—' : '−' + money(first(account.daily_loss_limit, account.daily_loss_stop_new_entries_cny)));
    text('metric-margin', money(first(account.margin, account.margin_used)));
    pnl('metric-unrealized', first(account.unrealized_pnl, account.floating_pnl), true);
    text('metric-available', money(first(account.available, account.available_funds)));
    text('metric-fees', money(first(account.total_fees, account.fees_paid, account.fees)));
    text('contract-symbol', first(market.symbol, quote.symbol, quote.instrument_id, '等待合约'));
    text('last-price', price(quote.last_price));
    const previousClose = num(first(quote.pre_close, quote.pre_settlement, quote.previous_close));
    const latest = num(quote.last_price);
    if (previousClose !== null && latest !== null) {
      const change = latest - previousClose;
      text('price-change', (change > 0 ? '+' : '') + price(change) + '  /  ' + (change > 0 ? '+' : '') + (change / previousClose * 100).toFixed(2) + '%');
      classed($('last-price'), 'positive', change > 0); classed($('last-price'), 'negative', change < 0);
      classed($('price-change'), 'positive', change > 0); classed($('price-change'), 'negative', change < 0);
    } else {
      text('price-change', latest !== null ? '实际月份合约行情' : '等待行情快照');
      $('last-price').classList.remove('positive', 'negative'); $('price-change').classList.remove('positive', 'negative');
    }
    text('bid-price', price(quote.bid_price1)); text('ask-price', price(quote.ask_price1));
    text('quote-volume', compact(quote.volume)); text('open-interest', compact(quote.open_interest));
    const feedStatus = String(market.status || 'waiting');
    const feedLive = ['live', 'connected', 'ready', 'streaming'].includes(feedStatus);
    const feedError = ['failed', 'error', 'disconnected'].includes(feedStatus);
    const feedName = feedLive ? (open ? '行情已连接' : '已连接 · 休市') : feedError ? '行情连接异常' : ({ starting: '行情连接中', connecting: '行情连接中', snapshot: '行情快照', stale: '行情已过时', closed: '休市快照', waiting: '等待行情' }[feedStatus] || '等待行情');
    $('feed-badge').className = 'badge ' + (feedError ? 'badge-red' : feedLive && open ? 'badge-green' : 'badge-neutral');
    text('feed-badge', feedName);
    if (market.error) $('feed-badge').title = plain(market.error);
    text('quote-timestamp', quote.datetime ? '行情时间 ' + time(quote.datetime) : '尚未收到行情');
    renderAI(state.ai || {}, calendar, runtime);
    renderPosition(state.position, account);
    renderTables(state);
    renderStrategy(state.strategy || {});
    renderReviews(state.reviews);
    renderAssumptions(state.cost_assumptions || {}, account, state.risk_limits || {});
    renderEvents(state.events);
    text('last-sync', '已同步 ' + time(state.server_time));
    text('footer-status', '账户数据保存在本机 · 最近同步 ' + time(state.server_time));
    updateControls(); updateClock(); drawChart(); drawSparkline();
  }

  function renderAI(ai, calendar, runtime) {
    const latestDecisions = array(ai.decisions).filter((record) => unpack(record).action);
    const lastRaw = ai.last_decision && unpack(ai.last_decision).action ? ai.last_decision : latestDecisions[0];
    const last = lastRaw ? unpack(lastRaw) : null;
    const statusMap = { analyzing: '分析中', running: '分析中', ready: '就绪', idle: '等待下一次', paused: '已暂停', error: '调用异常', failed: '调用异常', budget_exhausted: '今日预算已满', budget_blocked: '预算受限', waiting: '等待分析', waiting_market: '等待行情', market_closed: '休市等待' };
    const status = ai.status || (last ? 'idle' : 'waiting');
    text('ai-status', statusMap[status] || status);
    $('ai-status').className = 'badge ' + (['error', 'failed'].includes(status) || /失败|异常/.test(status) ? 'badge-red' : ['budget_exhausted', 'budget_blocked'].includes(status) || /预算/.test(status) ? 'badge-amber' : runtime.model_busy || ['analyzing', 'running'].includes(status) ? 'badge-blue loading-pulse' : 'badge-neutral');
    text('decision-action', last ? actionLabels[last.action] || plain(last.action) || '继续观察' : '等待分析');
    const action = last?.action || '';
    classed($('decision-action'), 'positive', action === 'open_long'); classed($('decision-action'), 'negative', action === 'open_short');
    const matchingRecord = last?.request_id ? latestDecisions.find((record) => record.request_id === last.request_id) : latestDecisions[0];
    text('decision-time', last ? time(first(isoTime(last), isoTime(matchingRecord))) : '—');
    text('decision-reason', last ? readableAI(first(last.reason, last.rationale, last.message)) || '本次未提供判断理由。' : '启动自动模拟后，AI 将结合行情、持仓与风险信息作出判断。');
    const execution = last?.execution;
    const executionElement = $('decision-execution');
    executionElement.hidden = !execution;
    executionElement.replaceChildren();
    if (execution) {
      executionElement.append(el('span', 'badge ' + (execution.status === 'executed' || execution.status === 'filled' ? 'badge-green' : execution.status === 'blocked' || execution.status === 'rejected' ? 'badge-amber' : 'badge-neutral'), statusLabels[execution.status] || '仅分析'));
      if (execution.reason) executionElement.append(el('span', '', plain(execution.reason)));
    }
    const risk = String(first(last?.risk_level, last?.risk?.level, 'unknown')).toLowerCase();
    text('risk-level', last ? riskLabels[risk] || '未知' : '待评估');
    $('risk-level').className = 'risk-tag ' + (['low', 'medium', 'high'].includes(risk) ? risk : risk === 'moderate' ? 'medium' : risk === 'critical' ? 'high' : '');
    text('closure-assessment', last ? readableAI(first(last.closure_assessment, last.overnight_risk, last.risk?.closure_assessment)) || '本次未提供休市风险结论，相关风险信息仍需留意。' : '允许隔夜、跨周末和长假持仓，休市风险纳入 AI 判断。');
    const riskDetails = $('risk-details'); riskDetails.replaceChildren();
    if (num(calendar.closure_hours) !== null && calendar.closure_hours > 0) riskDetails.append(el('span', '', '预计休市 ' + Number(calendar.closure_hours).toFixed(1) + ' 小时'));
    if (calendar.is_weekend_or_holiday) riskDetails.append(el('span', '', '周末 / 长假风险'));
    if (calendar.known_events_status === 'unknown' || calendar.known_events_status === 'unavailable') riskDetails.append(el('span', '', '事件信息未知'));
    if (calendar.calendar_verified === false || calendar.exchange_calendar_verified === false) riskDetails.append(el('span', '', '交易日历待核验'));
    const usage = ai.usage || {};
    const spent = num(first(usage.spent_usd, usage.cost_usd, usage.actual_cost_usd, usage.total_cost_usd, usage.used_usd));
    const reserved = num(first(usage.reserved_or_uncertain_usd, usage.reserved_usd, usage.reserved_cost_usd, 0));
    const accounted = num(first(usage.accounted_usd, spent === null ? null : spent + (reserved || 0)));
    const budget = num(first(usage.daily_budget_usd, usage.budget_usd, usage.limit_usd));
    text('model-cost', (accounted === null ? '—' : '$' + accounted.toFixed(4)) + ' / ' + (budget === null ? '—' : '$' + budget.toFixed(2)));
    $('model-cost').title = reserved > 0 ? '包含已预留或费用待确认的额度，平台账单为准。' : '按配置的模型标准价格计账，平台账单为准。';
    $('budget-progress').style.width = accounted !== null && budget > 0 ? Math.min(100, accounted / budget * 100) + '%' : '0%';
    const tokens = num(first(usage.total_tokens, (num(usage.input_tokens) !== null && num(usage.output_tokens) !== null) ? Number(usage.input_tokens) + Number(usage.output_tokens) : null));
    const requests = num(first(usage.requests, usage.request_count, usage.calls));
    text('model-token-count', (tokens === null ? '暂无调用用量' : count(tokens) + ' token' + (requests === null ? '' : ' · ' + count(requests) + ' 次调用')) + (reserved > 0 ? ' · 含预留' : ''));
    text('model-name', first(ai.model_name, ai.model) === 'gpt-6-luna' || !first(ai.model_name, ai.model) ? 'GPT-6 Luna' : first(ai.model_name, ai.model));
  }

  function renderPosition(position, account) {
    const container = $('position-content'); container.replaceChildren();
    const quantity = position ? first(position.quantity, position.volume, position.lots, 1) : 0;
    text('position-count', count(quantity) + ' / ' + count(first(model.state?.risk_limits?.max_position_contracts, account.max_position_contracts, 1)) + ' 手');
    if (!position) {
      const empty = el('div', 'empty-inline'); empty.append(el('span', 'empty-icon', '◇'));
      const copy = el('div'); copy.append(el('strong', '', '当前空仓'), el('p', '', '等待满足策略与风控条件的交易机会'));
      empty.append(copy); container.append(empty); $('close-confirm').hidden = true; return;
    }
    const grid = el('div', 'position-grid');
    const contract = el('div'); const heading = el('div', 'position-contract', first(position.symbol, position.contract, position.instrument_id, '—'));
    const direction = String(first(position.direction, position.side, position.action, '')).toLowerCase();
    const long = ['long', 'buy', 'open_long', '1'].includes(direction);
    heading.append(el('span', 'position-direction ' + (long ? 'positive' : 'negative'), long ? '多仓 ' + count(quantity) + '手' : '空仓 ' + count(quantity) + '手'));
    contract.append(heading, el('div', 'position-meta', '开仓 ' + time(first(position.opened_at, position.entry_time, position.open_time), true)));
    grid.append(contract);
    const field = (label, value, className = '') => { const item = el('div', 'position-field'); item.append(el('span', '', label), el('strong', className, value)); grid.append(item); };
    field('开仓均价', price(first(position.entry_price, position.open_price, position.average_price)));
    field('止损 / 止盈', price(first(position.stop_loss, position.stop_price)) + ' / ' + price(first(position.take_profit, position.target_price)));
    const unrealized = num(first(position.unrealized_pnl, account.unrealized_pnl));
    field('浮动盈亏', (unrealized > 0 ? '+' : '') + money(unrealized), unrealized > 0 ? 'positive' : unrealized < 0 ? 'negative' : '');
    field('持仓策略版本', first(position.strategy_version, position.version, '—'));
    container.append(grid);
    const reason = plain(first(position.entry_reason, position.reason));
    if (reason) container.append(el('p', 'position-meta', '开仓理由：' + reason));
  }

  function emptyTable(body, columns, message) {
    body.replaceChildren(); const row = el('tr'); const cell = el('td', 'empty-cell', message); cell.colSpan = columns; row.append(cell); body.append(row);
  }
  function cell(value, className = '', secondary = null) {
    const element = el('td', className, value);
    if (secondary !== null && secondary !== undefined && secondary !== '') element.append(el('span', 'table-secondary', secondary));
    return element;
  }

  function renderTables(state) {
    const trades = array(first(state.trades, state.history?.trades));
    const orders = array(first(state.orders, state.history?.orders));
    const decisions = array(first(state.ai?.decisions, state.decisions)).filter((record) => unpack(record).action);
    text('trades-count', trades.length); text('orders-count', orders.length); text('decisions-count', decisions.length);
    const tradeBody = $('trades-body'); tradeBody.replaceChildren();
    if (!trades.length) emptyTable(tradeBody, 6, '暂无成交记录 · 模拟成交后会显示价格与费用明细');
    for (const raw of trades.slice(0, 70)) {
      const trade = unpack(raw); const row = el('tr');
      const direction = first(trade.action, trade.direction, trade.side, '');
      const closed = String(trade.action || '').startsWith('close');
      const net = closed ? num(first(trade.net_pnl, trade.net_profit, trade.pnl, trade.realized_pnl)) : null;
      row.append(cell(time(isoTime(trade), true)), cell(first(trade.symbol, trade.contract, '—'), '', actionLabels[direction] || ({ buy: '买入', sell: '卖出', long: '多仓', short: '空仓', open: '开仓', close_today: '平今', close_previous: '平历史仓' }[direction] || direction)), cell(price(first(trade.price, trade.fill_price, trade.exit_price)) + ' / ' + count(first(trade.quantity, trade.volume, trade.lots, 1)) + '手', 'number'));
      const cost = first(trade.total_fees, trade.fees?.total, num(trade.fees), trade.fee, trade.commission);
      const feeParts = [];
      if (num(trade.fees?.exchange_fee_assumption) !== null) feeParts.push('手续费 ' + money(trade.fees.exchange_fee_assumption));
      if (num(trade.fees?.broker_commission_assumption) !== null) feeParts.push('佣金 ' + money(trade.fees.broker_commission_assumption));
      if (num(trade.slippage_cost) !== null) feeParts.push('滑点影响 ' + money(trade.slippage_cost));
      row.append(cell(money(cost), 'number', feeParts.join(' / ') || null));
      row.append(cell(net === null ? '—' : (net > 0 ? '+' : '') + money(net), 'number ' + (net > 0 ? 'positive' : net < 0 ? 'negative' : ''), closed ? '含开平仓费用' : '开仓费用已计入账户'));
      row.append(cell(first(trade.strategy_version, '—'), '', first(trade.assumption_version, trade.cost_version, trade.cost_assumption_version, '成本假设版本待记录')));
      row.title = plain(first(trade.reason, trade.exit_reason, trade.entry_reason)); tradeBody.append(row);
    }
    const orderBody = $('orders-body'); orderBody.replaceChildren();
    if (!orders.length) emptyTable(orderBody, 5, '暂无委托记录');
    for (const raw of orders.slice(0, 70)) {
      const order = unpack(raw); const row = el('tr'); const status = order.status || '';
      row.append(cell(time(isoTime(order), true)), cell(first(order.symbol, order.contract, '—'), '', actionLabels[order.action] || order.action || '—'));
      const statusCell = cell(''); statusCell.append(el('span', 'badge ' + (['rejected', 'failed', 'blocked'].includes(status) ? 'badge-amber' : status === 'filled' ? 'badge-green' : 'badge-neutral'), statusLabels[status] || status || '—')); row.append(statusCell);
      row.append(cell(price(first(order.fill_price, order.price, order.limit_price)) + ' / ' + count(first(order.quantity, order.volume, 1)) + '手', 'number'), cell(plain(first(order.reason, order.message, order.reject_reason)) || '—', 'table-reason'));
      orderBody.append(row);
    }
    const decisionBody = $('decisions-body'); decisionBody.replaceChildren();
    if (!decisions.length) emptyTable(decisionBody, 5, '暂无AI决策记录');
    for (const raw of decisions.slice(0, 70)) {
      const decision = unpack(raw); const row = el('tr');
      row.append(cell(time(isoTime(decision), true)), cell(actionLabels[decision.action] || decision.action || '—'), cell(riskLabels[decision.risk_level] || '未知'), cell(readableAI(first(decision.reason, decision.message)) || '—', 'table-reason'), cell(first(decision.strategy_version, '—'))); decisionBody.append(row);
    }
  }

  function renderStrategy(strategy) {
    const current = strategy.current || strategy.active || {};
    text('strategy-version', first(current.version, current.id, strategy.current_version, '等待策略'));
    text('strategy-name', first(current.name, strategy.name, current.version ? '顺势波段策略' : '等待策略信息'));
    const parameters = current.parameters || current.params || {};
    let description = plain(first(current.description, current.summary, strategy.description));
    if (!description && Object.keys(parameters).length) {
      const parts = [];
      const fast = first(parameters.fast_ma, parameters.fast_period, parameters.trend_fast);
      const slow = first(parameters.slow_ma, parameters.slow_period, parameters.trend_slow);
      const atr = first(parameters.atr_multiplier, parameters.stop_atr_multiple, parameters.stop_atr_multiplier);
      if (fast !== undefined && slow !== undefined) parts.push('趋势均线 ' + fast + ' / ' + slow);
      if (atr !== undefined) parts.push('波动止损 ' + atr + ' 倍');
      description = parts.join(' · ');
    }
    text('strategy-description', description || '新策略经独立验证和并行模拟后才会启用；既有持仓保留开仓时的管理计划。');
    const container = $('strategy-candidates'); container.replaceChildren();
    const candidates = array(first(strategy.candidates, strategy.pending_candidates));
    if (!candidates.length) container.append(el('p', 'empty-description', plain(first(strategy.message, strategy.wait_reason)) || '暂无候选策略，等待数据积累。'));
    for (const raw of candidates.slice(0, 4)) {
      const candidate = unpack(raw); const row = el('div', 'candidate-row'); const copy = el('div');
      copy.append(el('strong', '', first(candidate.version, candidate.name, candidate.id, '候选策略')));
      const stats = [];
      const sampleCount = first(candidate.trade_count, candidate.trades, candidate.sample_size, candidate.validation?.trade_count, candidate.metrics?.candidate?.trades);
      if (num(sampleCount) !== null) stats.push('验证交易 ' + count(sampleCount) + ' 笔');
      const shadowCount = first(candidate.shadow_trades, candidate.shadow_trade_count, candidate.shadow?.candidate?.trades);
      if (num(shadowCount) !== null) stats.push('影子交易 ' + count(shadowCount) + ' 笔');
      const reason = plain(first(candidate.reason, candidate.message, candidate.validation?.reason, candidate.description));
      copy.append(el('p', '', [reason, stats.join(' · ')].filter(Boolean).join(' · ') || '等待足够数据完成独立验证'));
      const status = first(candidate.status, candidate.stage, 'waiting');
      row.append(copy, el('span', 'badge ' + (status === 'promoted' || status === 'passed' ? 'badge-green' : status === 'rejected' || status === 'rolled_back' ? 'badge-amber' : 'badge-neutral'), statusLabels[status] || '验证中')); container.append(row);
    }
    text('strategy-state', plain(first(strategy.last_review?.message, strategy.last_review?.reason, strategy.last_event, strategy.last_update_message, strategy.rollback_reason)) || '仓位、亏损上限与成本假设独立于策略优化。');
  }

  function renderReviews(input) {
    const reviews = Array.isArray(input) ? input : input?.latest ? [input.latest] : input && typeof input === 'object' && Object.keys(input).length ? [input] : [];
    const container = $('review-content'); container.replaceChildren(); container.classList.toggle('has-review', reviews.length > 0);
    if (!reviews.length) {
      container.append(el('div', 'review-empty-icon', '▧'), el('strong', '', '等待第一份交易复盘'), el('p', '', '收盘后汇总成交、费用、风险与策略表现，形成可追溯的改进记录。')); return;
    }
    const review = unpack(reviews[0]);
    container.append(el('div', 'review-date', first(review.trading_day, review.date, time(isoTime(review), true))));
    const summary = plain(first(review.summary, review.review, review.analysis, review.message, review.content, review));
    container.append(el('div', 'review-summary', summary || '复盘已记录，等待内容同步。'));
    for (const [key, label] of [['trading_review', '交易表现'], ['risk_review', '风险复盘']]) {
      if (review[key]) { container.append(el('h3', 'review-subheading', label), el('div', 'review-summary', plain(review[key]))); }
    }
    const suggestions = array(first(review.improvement_focus, review.suggestions, review.improvements, review.lessons, review.recommendations));
    if (suggestions.length) { const list = el('ul', 'review-list'); for (const suggestion of suggestions.slice(0, 5)) list.append(el('li', '', plain(suggestion))); container.append(list); }
  }

  function renderAssumptions(cost, account, risk) {
    const limits = $('risk-limits'); limits.replaceChildren();
    const addLimit = (label, value) => { const item = el('div'); item.append(el('span', '', label), el('strong', '', value)); limits.append(item); };
    addLimit('最大持仓', count(first(risk.max_position_contracts, account.max_position_contracts, 1)) + ' 手');
    addLimit('单笔计划风险', money(first(risk.planned_risk_per_trade_cny, account.planned_risk_per_trade_cny, account.risk_per_trade_limit)) + ' 元');
    addLimit('当日停止开仓线', money(first(risk.daily_loss_stop_new_entries_cny, account.daily_loss_limit, account.daily_loss_stop_new_entries_cny)) + ' 元');
    const container = $('cost-details'); container.replaceChildren();
    const hasCost = cost && typeof cost === 'object' && Object.keys(cost).length > 0;
    if (!hasCost) { container.append(el('p', 'empty-description', '正在读取费用、滑点与保证金假设。')); return; }
    const rates = cost.fee_rates || cost.exchange_fee_rates || {};
    const add = (label, value, note = '') => { const row = el('div', 'cost-row'); const labelEl = el('span', '', label); if (note) labelEl.append(el('span', 'cost-note', ' · ' + note)); row.append(labelEl, el('strong', '', value)); container.append(row); };
    const rateText = (value) => num(value) === null ? '—' : '成交金额的万分之 ' + (Number(value) * 10000).toFixed(2);
    add('费用假设版本', first(cost.assumption_version, cost.version, '未标注'));
    if (Object.keys(rates).length) {
      add('开仓手续费假设', rateText(rates.open));
      add('当天平仓手续费假设', rateText(first(rates.close_today, rates.close_intraday)));
      add('历史仓位平仓手续费假设', rateText(first(rates.close_previous, rates.close_overnight)));
    }
    if (num(first(cost.broker_commission_cny_per_side, cost.commission_per_side)) !== null) add('附加佣金', money(first(cost.broker_commission_cny_per_side, cost.commission_per_side)) + ' 元 / 手 / 边');
    if (num(cost.slippage_ticks) !== null) add('额外不利滑点', cost.slippage_ticks + ' 跳 / 边', '价差已计入成交价');
    if (num(cost.margin_rate) !== null) add('保证金假设比例', (Number(cost.margin_rate) * 100).toFixed(0) + '%');
    if (cost.description) add('假设说明', plain(cost.description));
  }

  function renderEvents(input) {
    const events = array(input); const container = $('events-list'); container.replaceChildren();
    if (!events.length) { container.append(el('p', 'empty-description', '暂无运行记录。')); return; }
    for (const raw of events.slice(0, 30)) {
      const event = unpack(raw); const kind = String(first(event.kind, event.level, event.type, ''));
      const style = /fail|error|reject/.test(kind) ? 'error' : /risk|warn|block/.test(kind) ? 'warning' : '';
      const row = el('div', 'event-item ' + style); row.append(el('i'), el('p', '', readableAI(first(event.message, event.reason, event.description, event.title)) || statusLabels[kind] || kind || '运行状态更新'), el('time', '', time(isoTime(event)))); container.append(row);
    }
  }

  function updateClock() {
    const now = new Date(Date.now() + model.clockOffset);
    text('server-clock', timeFormat.format(now));
    const state = model.state;
    if (!state) return;
    const quoteDate = dateOf(state.market?.quote?.datetime);
    const age = quoteDate ? (now.getTime() - quoteDate.getTime()) / 1000 : null;
    const open = Boolean(first(state.calendar?.session_open, state.market?.session?.open, state.market?.session?.is_open, false));
    if (age !== null) text('quote-age', !open ? '休市行情快照' : age < -2 ? '行情时钟待校验' : age <= 10 ? '行情年龄 ' + Math.max(0, age).toFixed(1) + ' 秒' : '行情已过时 · ' + (age < 60 ? Math.floor(age) + ' 秒' : Math.floor(age / 60) + ' 分钟'));
    else text('quote-age', '数据时效待确认');
    $('freshness-dot').className = 'status-dot' + (age !== null && age >= -2 && age <= 10 && open ? ' live' : age !== null && open ? ' warning' : '');
    const nextDate = dateOf(state.ai?.next_analysis_at);
    const running = Boolean(first(state.runtime?.running, state.runtime?.auto_trading_enabled, false));
    if (state.runtime?.model_busy || ['analyzing', 'running'].includes(state.ai?.status)) text('next-analysis', '模型任务进行中');
    else if (!running) text('next-analysis', '自动模拟已暂停');
    else if (!open) text('next-analysis', '等待交易时段');
    else if (nextDate) { const seconds = Math.max(0, Math.ceil((nextDate.getTime() - now.getTime()) / 1000)); text('next-analysis', seconds > 0 ? seconds + ' 秒后' : '等待本地信号检查'); }
    else text('next-analysis', '等待本地交易信号');
  }

  function validBars() {
    const market = model.state?.market || {};
    const raw = model.period === 5 ? first(market.bars_5m, market.bars5, []) : first(market.bars_1m, market.bars1, []);
    return array(raw).filter((bar) => ['open', 'high', 'low', 'close'].every((key) => num(bar[key]) !== null && Number(bar[key]) > 0)).slice(-100);
  }

  function canvasContext(canvas) {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(box.width * ratio) || canvas.height !== Math.round(box.height * ratio)) {
      canvas.width = Math.round(box.width * ratio); canvas.height = Math.round(box.height * ratio);
    }
    const ctx = canvas.getContext('2d'); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, box.width, box.height);
    return { ctx, width: box.width, height: box.height };
  }

  function drawChart() {
    const view = canvasContext($('market-chart')); if (!view) return;
    const { ctx, width, height } = view; const bars = validBars();
    $('chart-empty').hidden = bars.length > 0;
    text('chart-bar-count', bars.length ? bars.length + ' 根K线' : '等待K线');
    const left = 10; const right = width < 400 ? 51 : 62; const top = 15; const bottom = 29;
    const chartWidth = Math.max(1, width - left - right); const plotBottom = height - bottom; const priceHeight = (plotBottom - top) * 0.76;
    const volumeTop = top + priceHeight + 15; const volumeHeight = Math.max(12, plotBottom - volumeTop);
    ctx.font = '9px "Segoe UI", sans-serif'; ctx.textBaseline = 'middle';
    ctx.strokeStyle = '#edf1f7'; ctx.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) { const y = top + index / 4 * priceHeight; ctx.beginPath(); ctx.moveTo(left, y + 0.5); ctx.lineTo(left + chartWidth, y + 0.5); ctx.stroke(); }
    ctx.fillStyle = '#b2bfd0'; ctx.font = '8px "Segoe UI", sans-serif'; ctx.fillText('VOL', left, volumeTop - 3);
    if (!bars.length) { $('chart-tooltip').hidden = true; return; }
    let high = Math.max(...bars.map((bar) => Number(bar.high))); let low = Math.min(...bars.map((bar) => Number(bar.low)));
    const padding = Math.max((high - low) * 0.12, Number(model.state?.market?.quote?.price_tick) || 0.2) * 2;
    high += padding; low -= padding;
    const yAt = (value) => top + (high - value) / (high - low) * priceHeight;
    ctx.fillStyle = '#a9b6c9'; ctx.font = '9px "Segoe UI", sans-serif'; ctx.textAlign = 'left';
    for (let index = 0; index <= 4; index += 1) { const value = high - index / 4 * (high - low); ctx.fillText(value.toFixed(1), left + chartWidth + 9, top + index / 4 * priceHeight); }
    const step = chartWidth / Math.max(bars.length, 20); const bodyWidth = Math.max(2, Math.min(11, step * 0.64));
    const xAt = (index) => left + step * (index + 0.5);
    const maxVolume = Math.max(1, ...bars.map((bar) => Number(bar.volume) || 0));
    bars.forEach((bar, index) => {
      const color = Number(bar.close) >= Number(bar.open) ? '#d86770' : '#399c84'; const x = xAt(index);
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, yAt(Number(bar.high))); ctx.lineTo(Math.round(x) + 0.5, yAt(Number(bar.low))); ctx.stroke();
      const bodyTop = yAt(Math.max(Number(bar.open), Number(bar.close))); const bodyHeight = Math.max(1, Math.abs(yAt(Number(bar.open)) - yAt(Number(bar.close))));
      ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
      const volHeight = Math.max(1, ((Number(bar.volume) || 0) / maxVolume) * volumeHeight);
      ctx.globalAlpha = 0.28; ctx.fillRect(x - bodyWidth / 2, plotBottom - volHeight, bodyWidth, volHeight); ctx.globalAlpha = 1;
    });
    ctx.font = '8px "Segoe UI", sans-serif'; ctx.fillStyle = '#aab7ca'; ctx.textAlign = 'center';
    const labelEvery = Math.max(1, Math.ceil(bars.length / (width < 400 ? 4 : 6)));
    bars.forEach((bar, index) => { if (index % labelEvery === 0) { const date = dateOf(first(bar.datetime, bar.timestamp, bar.time)); if (date) ctx.fillText(minuteFormat.format(date), xAt(index), height - 11); } });
    const quotePrice = num(model.state?.market?.quote?.last_price);
    if (quotePrice !== null && quotePrice >= low && quotePrice <= high) {
      const y = yAt(quotePrice); ctx.strokeStyle = '#9badc53d'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + chartWidth, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#8b9db9'; ctx.fillRect(left + chartWidth + 3, y - 9, right - 6, 18); ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.font = '8px "Segoe UI", sans-serif'; ctx.fillText(quotePrice.toFixed(1), left + chartWidth + right / 2, y);
    }
    if (model.hover >= 0 && model.hover < bars.length) {
      const bar = bars[model.hover]; const x = xAt(model.hover); const y = yAt(Number(bar.close)); ctx.strokeStyle = '#879ab7'; ctx.lineWidth = 0.7; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, plotBottom); ctx.moveTo(left, y); ctx.lineTo(left + chartWidth, y); ctx.stroke(); ctx.setLineDash([]);
      const tooltip = $('chart-tooltip'); tooltip.hidden = false;
      tooltip.textContent = time(first(bar.datetime, bar.timestamp, bar.time), true) + '\n开 ' + price(bar.open) + '　高 ' + price(bar.high) + '　低 ' + price(bar.low) + '　收 ' + price(bar.close) + '\n成交量 ' + count(bar.volume);
    } else $('chart-tooltip').hidden = true;
  }

  function drawSparkline() {
    const view = canvasContext($('equity-sparkline')); if (!view) return;
    const { ctx, width, height } = view;
    const points = array(model.state?.equity_curve).map((point) => num(typeof point === 'number' ? point : first(point.equity, point.balance, point.value))).filter((point) => point !== null).slice(-80);
    if (!points.length) return;
    if (points.length === 1) { ctx.strokeStyle = '#90a8d5'; ctx.lineWidth = 1.3; ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke(); return; }
    const high = Math.max(...points); const low = Math.min(...points); const span = Math.max(1, high - low);
    ctx.strokeStyle = '#90a8d5'; ctx.lineWidth = 1.3; ctx.beginPath();
    points.forEach((value, index) => { const x = points.length === 1 ? 0 : index / (points.length - 1) * width; const y = 4 + (high - value) / span * (height - 8); if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
    ctx.stroke();
  }

  document.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => control(button.dataset.action)));
  $('close-button').addEventListener('click', () => { $('close-confirm').hidden = false; $('confirm-close').focus(); });
  $('cancel-close').addEventListener('click', () => { $('close-confirm').hidden = true; $('close-button').focus(); });
  $('confirm-close').addEventListener('click', () => control('close'));
  document.querySelectorAll('[data-period]').forEach((button) => button.addEventListener('click', () => {
    model.period = Number(button.dataset.period); model.hover = -1;
    document.querySelectorAll('[data-period]').forEach((item) => { const active = item === button; item.classList.toggle('selected', active); item.setAttribute('aria-pressed', String(active)); }); drawChart();
  }));
  document.querySelectorAll('[data-table]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('[data-table]').forEach((item) => { const selected = item === button; item.classList.toggle('selected', selected); item.setAttribute('aria-selected', String(selected)); $(item.dataset.table + '-view').hidden = !selected; });
  }));
  document.querySelectorAll('.nav-link').forEach((link) => link.addEventListener('click', () => { document.querySelectorAll('.nav-link').forEach((item) => item.classList.toggle('active', item === link)); }));
  $('market-chart').addEventListener('mousemove', (event) => {
    const canvas = $('market-chart'); const box = canvas.getBoundingClientRect(); const right = box.width < 400 ? 51 : 62; const bars = validBars();
    const step = Math.max(1, box.width - 10 - right) / Math.max(bars.length, 20);
    const index = Math.floor((event.clientX - box.left - 10) / step);
    model.hover = index >= 0 && index < bars.length ? index : -1; drawChart();
  });
  $('market-chart').addEventListener('mouseleave', () => { model.hover = -1; drawChart(); });
  const resizeObserver = new ResizeObserver(() => { drawChart(); drawSparkline(); });
  resizeObserver.observe($('chart-wrap')); resizeObserver.observe($('equity-sparkline'));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { drawChart(); updateClock(); } });
  setInterval(updateClock, 1000);
  bootstrap().catch(() => { /* State polling remains useful while bootstrap recovers. */ });
  updateClock(); drawChart(); poll();
})();
