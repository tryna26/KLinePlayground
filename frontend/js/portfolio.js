/* Shared-date portfolio UI. Account amounts/fees always come from the server. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const fmt = value => Number(value ?? 0).toLocaleString('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:2});
    const shares = value => Number(value ?? 0).toLocaleString('zh-CN');
    const signed = value => `${Number(value) > 0 ? '+' : ''}${fmt(value)}`;
    const tone = value => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : '';
    const statusNames = {trading:'正常交易',suspended:'停牌',not_listed:'尚未上市',delisted:'已退市',download_failed:'下载失败',cache_missing:'缓存缺失',unknown:'原因未明',invalid:'行情无效',no_trades:'无有效成交',market_closed:'休市',unsupported:'暂不支持'};
    const orderNames = {pending:'待成交',filled:'已成交',cancelled:'已撤单',rejected:'已拒绝'};
    let user = null, state = null, selectedStock = null, selected = new Map(), names = new Map();
    let busy = false, pending = null, checked = null, tab = 'positions', report = null, feeRange = null;
    let quoteSeq = 0, estimateSeq = 0, searchSeq = 0, previewSeq = 0;
    let searchTimer, estimateTimer, estimateValid = false;
    let priceChart, candle, volumeChart, volume, assetChart, assetLine, reportChart, reportLine;
    let reviewOpen = false;
    let chartRows = [], maSeries = new Map(), maData = new Map();
    let comparisonChart, comparisonLines = new Map(), comparisonSeq = 0;
    let historyCache = new Map(), playing = false, playTimer = null, playGeneration = 0;
    const comparisonPoints = new Map(), hiddenComparison = new Set();
    let chartView = 'single';
    const maColors = {5:'#bd7e15',10:'#446fcd',20:'#9d53a6',30:'#168378',60:'#b45536'};
    const comparisonColors = ['#d4594f','#477ed3','#ac61b5','#c38b22','#238d84','#ba5c86','#797a36','#665db8'];
    const pendingKey = () => `portfolio.pending.${user}`;
    const path = () => `/api/portfolio/trainings/${state.training_id}`;
    const stockLabel = code => `${code} ${names.get(code) || ''}`.trim();

    function message(text, error = false) {
        $('message').textContent = text;
        $('message').classList.toggle('error', error);
        $('message').hidden = !text;
    }
    async function api(url, data) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 180000);
        try {
            const response = await fetch(url, {method:data === undefined ? 'GET':'POST',credentials:'same-origin',
                headers:data === undefined ? {}:{'Content-Type':'application/json'},
                body:data === undefined ? undefined:JSON.stringify(data),signal:controller.signal});
            const result = await response.json();
            if (!response.ok) {
                const error = new Error(result.error || `请求失败（${response.status}）`);
                error.status = response.status;
                throw error;
            }
            return result;
        } catch (error) {
            if (!error.status) error.message = '无法确认请求结果，请检查本地服务是否仍在运行。';
            throw error;
        } finally { clearTimeout(timer); }
    }
    const run = fn => async event => { try { await fn(event); } catch (error) { message(error.message, true); } };
    const metric = (label, value, detail = '', cls = '') => `<div class="metric"><span>${esc(label)}</span><strong class="${cls}">${esc(value)}</strong>${detail ? `<small>${esc(detail)}</small>`:''}</div>`;
    function controls() {
        const locked = busy || !!pending;
        for (const id of ['profile','check','refresh-list','source','prepare-data','search','cash','start','end','setup-mode','random-count','random-days','random-sector','side','quantity','exit','end-training','reset-training','confirm-reset','chart-mode','report-new','report-review']) $(id).disabled = locked;
        for (const id of ['side','quantity']) $(id).disabled = locked || state?.status === 'ended';
        $('create').disabled = locked || ($('setup-mode').value !== 'random' && !selected.size);
        $('check').hidden = $('setup-mode').value === 'random';
        $('advance').disabled = locked || playing || !state?.next_date || state?.status !== 'active';
        $('advance').textContent = busy ? '正在推进 / 保存…' : state?.status === 'ended' ? '训练已结束' : state?.next_date ? `下一交易日 → ${state.next_date}` : '已到区间末日';
        $('play').textContent = playing ? '⏸ 暂停播放' : '▶ 自动播放';
        $('play').disabled = !playing && (locked || !state?.next_date || state?.status !== 'active');
        $('end-training').hidden = state?.status === 'ended';
        $('submit-order').disabled = locked || !estimateValid || state?.status !== 'active';
        $('retry-panel').hidden = !pending || busy;
        $('retry').disabled = busy;
        document.querySelectorAll('[data-restore],[data-cancel],[data-edit-order],[data-remove],[data-add],[data-stock],[data-side],[data-lots],#quantity-minus,#quantity-plus,#odd-lot,#sell-all').forEach(button => {button.disabled = locked || (state?.status === 'ended' && !button.hasAttribute('data-stock') && !button.hasAttribute('data-restore'));});
        const buy = $('side').value === 'buy';
        document.querySelectorAll('[data-side]').forEach(button => {
            const active = button.dataset.side === $('side').value;
            button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
        });
        $('submit-order').textContent = state?.status === 'ended' ? '训练已结束' : '提交委托';
        $('sell-all').hidden = buy;
        $('odd-lot-wrap').hidden = buy;
        $('lobby-notes').hidden = !!state;
        $('session-notes').hidden = !state;
        $('report-note').hidden = !report;
    }
    function fingerprint() {return JSON.stringify(createParams());}
    function createParams() {return {stocks:[...selected.keys()],start_date:$('start').value,end_date:$('end').value,initial_cash:$('cash').value,data_source:$('source').value,
        mode:$('setup-mode').value,stock_count:Number($('random-count').value),training_days:Number($('random-days').value),sector:$('random-sector').value};}
    function invalidate() {
        checked = null; previewSeq++;
        $('preview').textContent = '选择完成后直接开始，系统会自动加载所选日期的行情。';
        controls();
    }
    function renderSelected() {
        $('selected-count').textContent = `${selected.size} / 20`;
        $('selected').innerHTML = [...selected].map(([code,name]) => `<button class="chip" type="button" data-remove="${esc(code)}" aria-label="移除 ${esc(code)}">${esc(code)} ${esc(name)} ×</button>`).join('') || '<span class="muted">尚未选择股票</span>';
        controls();
    }
    async function search() {
        const seq = ++searchSeq, source = $('source').value;
        const data = await api(`/api/portfolio/options?data_source=${encodeURIComponent(source)}&q=${encodeURIComponent($('search').value.trim())}`);
        if (seq !== searchSeq) return;
        $('search-results').innerHTML = data.stocks.map(row => {
            names.set(row.stock, row.name);
            const range = row.coverage ? `数据范围 ${row.coverage.start} 至 ${row.coverage.end}` : row.reason;
            return `<button class="search-result" type="button" data-add="${esc(row.stock)}"><span>${esc(row.stock)} <strong>${esc(row.name)}</strong><small>${esc(range)}</small></span><span>${selected.has(row.stock)?'已选':'＋'}</span></button>`;
        }).join('') || '<div class="empty">未找到结果。也可输入完整六位代码检查支持情况。</div>';
        controls();
        return data;
    }
    async function sourceChanged() {
        clearTimeout(searchTimer);
        selected.clear(); checked = null; $('search').value = '';
        $('source-note').textContent = $('source').value === 'synthetic-practice'
            ? '多月合成练习：2024-01-02 至 2024-06-28。价格和交易日历均为人工生成，仅供均线和自动播放练习。'
            : $('source').value === 'synthetic-demo'
            ? '合成演示：2024-01-02 至 2024-01-10。甲/乙/丙的价格、停牌和上市退市日期均为人工样本。'
            : $('source').value === 'sina'
            ? '真实未复权日线：首批支持浦发银行、平安银行、招商银行，2024-01-02 至 2024-06-12（105 个交易日）。已复核历史状态、分红配股及公共日历；其他股票和日期暂未开放。'
            : '与单股训练共用日线数据，可自由选择股票和日期；起止日期自动对齐区间内交易日。简化练习采用固定模拟费率，不计分红送转；缺失行情不成交。';
        $('real-data-actions').hidden = $('source').value !== 'sina';
        if ($('source').value === 'synthetic-demo') { $('start').value = '2024-01-02'; $('end').value = '2024-01-10'; }
        if ($('source').value === 'synthetic-practice') { $('start').value = '2024-01-02'; $('end').value = '2024-06-28'; }
        invalidate(); renderSelected(); const data = await search();
        if (!data) return;
        if ($('source').value === 'sina') {
            $('start').value = data.suggested_range?.start || '2024-01-02';
            $('end').value = data.suggested_range?.end || '2024-06-12';
            $('real-data-status').textContent = data.updated_at ? `已接入 · 最近核验 ${new Date(data.updated_at).toLocaleString('zh-CN')}` : '尚未接入，请先点击上方按钮下载并核验真实数据。';
            invalidate();
        }
    }
    async function prepareData() {
        if (busy || pending) return;
        busy = true; controls();
        $('real-data-status').textContent = '正在获取真实日线、交易日历和公司资料并核验，请稍候…';
        try {
            const result = await api('/api/portfolio/prepare-data', {});
            await sourceChanged();
            $('real-data-status').textContent = `${result.note} 最近核验 ${new Date(result.updated_at).toLocaleString('zh-CN')}`;
            message('真实数据已就绪。选择股票，再点击“检查数据范围”和“开始组合训练”。');
        } catch(error) {
            $('real-data-status').textContent = '本次更新未完成；已接入的数据仍可使用。';
            throw error;
        } finally {busy = false; controls();}
    }
    async function checkData() {
        if (!$('create-form').reportValidity()) return;
        if (!selected.size) throw new Error('请先选择至少一只股票。');
        const params = createParams(), key = fingerprint(), seq = ++previewSeq;
        busy = true; controls(); $('preview').textContent = '正在检查历史状态、公司行为、日历和行情…';
        try {
            const result = await api('/api/portfolio/preview', params);
            if (seq !== previewSeq || key !== fingerprint()) return;
            checked = result.supported ? key : null;
            $('preview').innerHTML = result.stocks.map(row => `<div class="${row.supported?'':'negative'}">${row.supported?'✓':'×'} ${esc(row.stock)}：${esc(row.reason)}</div>`).join('') + (result.supported ? '<div>区间检查通过，可以开始。</div>':'');
        } finally {busy = false; controls();}
    }
    async function listTrainings() {
        const data = await api('/api/portfolio/trainings');
        const group = (ended, title) => {
            const rows = data.trainings.filter(row => (row.status === 'ended') === ended);
            return rows.length ? `<h3 class="list-group-title">${title}<span>${rows.length}</span></h3>${rows.map(row => `<div class="saved"><div><span class="pill ${ended?'':'in-progress'}">${ended?'已完成':'进行中'}</span><strong>${row.config.stocks.length} 只股票 · ${esc(row.current_date)}</strong><p>${esc(row.config.start_date)} 至 ${esc(row.config.end_date)}<br>${row.config.synthetic?'合成练习':'历史日线'} · ${esc(row.training_id.slice(0,8))}</p></div><button class="${ended?'':'primary'}" data-restore="${esc(row.training_id)}">${ended?'查看总结':'继续训练 →'}</button></div>`).join('')}` : '';
        };
        $('training-list').innerHTML = group(false,'待继续') + group(true,'已完成的训练') || '<div class="empty">还没有组合训练记录。<br>从左侧新建一局，进度会自动保存在这里。</div>';
        controls();
    }
    async function transact(url, body) {
        if (busy) return false;
        busy = true;
        pending = {url,body}; sessionStorage.setItem(pendingKey(), JSON.stringify(pending)); controls();
        try {
            const result = await api(url,body);
            if (url.endsWith('/reset')) {
                clearTimeout(estimateTimer);
                historyCache.clear(); reviewOpen = false; selectedStock = null; tab = 'positions';
                $('side').value = 'buy'; $('odd-lot').checked = false; $('quantity').value = '1';
            }
            // A retry can return an older original response. Always re-read authority.
            await openTraining(result.training_id, true);
            pending = null; sessionStorage.removeItem(pendingKey());
            message(url.endsWith('/reset') ? '重置已保存，当前训练进度已更新。' : state.status === 'ended' ? '训练已结束，总结已保存。' : '操作已保存。');
        } catch (error) {
            if (error.status && error.status !== 503) {
                pending = null; sessionStorage.removeItem(pendingKey());
                if (error.status === 409 && state) await openTraining(state.training_id, true);
            }
            message(error.message, true);
            stopPlayback('自动播放已暂停，请先处理提示或重试原操作。');
            return false;
        } finally {busy = false; controls();}
        // Ending is a separate, persisted request after the previous lock is released.
        if (state.status === 'active' && !state.next_date) return await finishAtRangeEnd();
        return true;
    }
    async function mutate(action, extra = {}) {
        if (busy || pending || !state) return false;
        return await transact(`${path()}/${action}`, {request_id:crypto.randomUUID(),expected_version:state.version,...extra});
    }
    async function finishAtRangeEnd() {
        if (busy || pending || state?.status !== 'active' || state.next_date) return false;
        stopPlayback('训练区间已走完，正在生成总结…');
        return await mutate('end');
    }
    async function openTraining(id, internal = false) {
        if (!internal && (busy || pending)) return;
        if (!internal) stopPlayback();
        if (!internal) {busy = true; message(''); controls();}
        try {
            const restored = await api(`/api/portfolio/trainings/${encodeURIComponent(id)}/restore`, {});
            const openingReport = restored.status === 'ended' && (state?.training_id !== id || state?.status !== 'ended' || !report);
            if (state?.training_id !== id || openingReport) reviewOpen = false;
            if (state?.training_id !== restored.training_id || state?.current_date !== restored.current_date) historyCache.clear();
            state = restored;
            if (['synthetic-demo','synthetic-practice'].includes(state.config.data_source)) {
                const prefix=state.config.data_source==='synthetic-practice'?'练习':'合成';
                for (const [code,label] of [['600000','甲'],['000001','乙'],['600036','丙']]) names.set(code,prefix+label);
            } else {
                const catalogs = await Promise.all(state.config.stocks.map(code=>api(`/api/portfolio/options?data_source=${encodeURIComponent(state.config.data_source)}&q=${encodeURIComponent(code)}`)));
                for (const catalog of catalogs) for (const row of catalog.stocks) names.set(row.stock,row.name);
            }
            selectedStock = state.config.stocks.includes(selectedStock) ? selectedStock : state.config.stocks[0];
            $('lobby').hidden = true; $('workspace').hidden = false;
            report = null; $('chart-mode').value = 'bounded';
            if (state.status === 'ended') report = await api(`${path()}/report`);
            render(); await Promise.all([drawStock(),drawComparison()]); await estimateOrder();
            if (report) drawReportEquity();
            if (openingReport) {window.scrollTo(0,0);$('report').focus({preventScroll:true});}
        } finally {if (!internal) {busy = false; controls();}}
        if (!internal) await finishAtRangeEnd();
    }
    function render() {
        const last = state.daily_assets.at(-1), pnl = Number(last.total_assets)-Number(state.account.initial_cash);
        $('date').textContent = state.current_date;
        $('session-note').textContent = `${state.status === 'ended'?'已结束':'自动保存'} · ${state.config.stocks.length} 只股票 · ${state.config.start_date} 至 ${state.config.end_date}${state.synthetic?' · 合成练习':''}`;
        $('session-eyebrow').textContent = report ? '训练完成 · 组合复盘' : '共同时间 · 收盘后决策';
        $('simulation-note').textContent = state.synthetic ? '合成练习 · 价格、历史状态和交易日历均为人工样本。订单最早在下一公共交易日开盘尝试成交。' : state.config.practice
            ? `${state.config.data_source} 历史日线 · 自由选股${state.config.mode==='random'?' · 随机盲盒':''} · 未复权价格、固定模拟费率；不计分红送转，缺失行情不成交。日线无法还原实际盘口与排队。`
            : `${state.config.data_source} 历史日线 · 使用未复权成交价和历史费率；日线简化模拟无法还原实际盘口与排队。`;
        $('metrics').innerHTML = metric('总资产 / 元',fmt(last.total_assets),`累计收益 ${signed(pnl)}`,tone(pnl)) + metric('可用现金 / 元',fmt(state.account.available_cash),'所有股票共享') + metric('冻结现金 / 元',fmt(state.account.frozen_cash),'待成交买单占用') + metric('持仓市值 / 元',fmt(last.market_value),`收盘估值 ${state.current_date}`) + metric('当日收益 / 元',signed(last.daily_pnl),`已实现 ${signed(state.account.realized_pnl)}`,tone(last.daily_pnl));
        $('stock-count').textContent = `${state.config.stocks.length} 只`;
        $('stocks').innerHTML = state.quotes.map(quote => {
            const position = state.positions.find(p => p.stock === quote.stock);
            return `<button class="stock-button ${selectedStock === quote.stock?'active':''}" data-stock="${esc(quote.stock)}"><span class="stock-line"><strong>${esc(quote.stock)}</strong><span>${quote.bar?fmt(quote.bar.close):'—'}</span></span><small>${esc(names.get(quote.stock)||'沪深主板')} · ${esc(statusNames[quote.status]||quote.status)}</small><small id="change-${esc(quote.stock)}">涨跌计算中…</small><span class="stock-holding ${position.quantity?'has-position':''}"><span>持仓</span><strong>${shares(position.quantity)} <span>股</span></strong></span><small>当前可卖 ${shares(position.sellable_now)} 股${position.frozen_shares?` · 冻结 ${shares(position.frozen_shares)} 股`:''}</small>${position.valuation.stale?'<small>使用历史价估值</small>':''}</button>`;
        }).join('');
        for (const quote of state.quotes) updateChange(quote, state.training_id, state.current_date);
        $('report').hidden = !report;
        $('review-workspace').hidden = !!report && !reviewOpen;
        $('review-range').hidden = !report;
        if (report) renderReport();
        renderDetails(); controls();
    }
    function renderReport() {
        const s = report.summary, account = report.account;
        const held = account.positions.filter(p => p.quantity > 0);
        $('report-period').textContent = `${state.config.start_date} — ${s.valuation_date} · ${s.trading_days} 个交易日 · ${state.config.stocks.length} 只股票${state.synthetic?' · 合成练习':''}`;
        $('report-return').textContent = `${signed(s.return_percent)}%`;
        $('report-return').className = tone(s.total_pnl);
        $('report-verdict').textContent = !s.fill_count ? '本次没有成交，完成了一次持币观察。' : Number(s.total_pnl) > 0 ? '本次组合取得正收益，回看盈利来自哪些选择。' : Number(s.total_pnl) < 0 ? '本次组合出现亏损，从回撤和各股贡献开始复盘。' : '本次组合盈亏持平，回看交易与费用的影响。';
        $('report-capital').textContent = `¥${fmt(s.initial_cash)} → ¥${fmt(s.final_assets)}`;
        $('report-pnl').textContent = `累计盈亏 ${signed(s.total_pnl)} 元 · 可用现金 ${fmt(account.account.available_cash)} 元`;
        $('report-summary').innerHTML = metric('最大回撤',`${fmt(s.max_drawdown_percent)}%`,'按每日收盘总资产计算') + metric('成交笔数',`${s.fill_count} 笔`,`卖出 ${s.sell_fill_count} 笔`) + metric('交易费用',`¥${fmt(s.total_fees)}`,'已计入总收益，不重复扣减') + metric('盈利卖出占比',s.sell_win_rate_percent === null ? '—' : `${fmt(s.sell_win_rate_percent)}%`,s.sell_fill_count ? `${s.winning_sell_count} / ${s.sell_fill_count} 笔卖出盈利` : '尚无卖出成交，不计算胜率');
        const notes = [
            `已实现盈亏 ${signed(s.realized_pnl)} 元，持仓浮动盈亏 ${signed(s.unrealized_pnl)} 元。${held.length ? `结束时仍持有 ${held.length} 只股票，按收盘估值计入资产。` : '结束时没有持仓。'}`,
            `本次最大回撤 ${fmt(s.max_drawdown_percent)}%。可以结合资产曲线，回看回撤发生时的持仓和调仓。`,
            s.fill_count ? `共成交 ${s.fill_count} 笔，费用占初始资金的 ${fmt(Number(s.total_fees)/Number(s.initial_cash)*100)}%。回看每次换手是否符合原先的交易理由。` : '本次没有实际成交。下一次可以先写下入场条件，再观察条件是否出现。'
        ];
        const stale = held.filter(p => p.valuation.stale);
        if (stale.length) notes.push(`${stale.length} 只持仓在结束日没有有效收盘价，沿用最近历史价估值；各股表格已标明日期。`);
        $('report-insights').innerHTML = notes.map(note=>`<li>${esc(note)}</li>`).join('');
        $('report-stocks').innerHTML = table(['股票','成交','已实现 / 元','浮动盈亏 / 元','总贡献 / 元','结束持仓'],[...report.stock_breakdown].sort((a,b)=>Number(b.total_pnl)-Number(a.total_pnl)).map(row=>`<tr>${td(stockLabel(row.stock))}${td(`${row.fill_count} 笔`)}<td class="${tone(row.realized_pnl)}">${signed(row.realized_pnl)}</td><td class="${tone(row.unrealized_pnl)}">${signed(row.unrealized_pnl)}</td><td class="${tone(row.total_pnl)}"><strong>${signed(row.total_pnl)}</strong></td><td>${esc(row.quantity)} 股${row.stale && row.quantity ? `<small>沿用 ${esc(row.price_date)} 估值</small>`:''}</td></tr>`));
        $('report-note').textContent = `${report.note} 盈利卖出占比按每笔卖出成交统计，未卖出的持仓不计入该比例。`;
        $('report-review').textContent = reviewOpen ? '收起 K 线与交易明细' : '查看 K 线与交易明细';
        $('report-review').setAttribute('aria-expanded',String(reviewOpen));
    }
    function drawReportEquity() {
        if (!window.LightweightCharts || !report) return;
        if (!reportChart) {
            reportChart = makeChart($('report-equity'));
            reportLine = reportChart.addSeries(LightweightCharts.AreaSeries,{lineColor:'#2465ba',topColor:'#2465ba30',bottomColor:'#2465ba03',lineWidth:2});
        }
        reportLine.setData(report.account.daily_assets.map(a=>({time:a.date,value:Number(a.total_assets)})));
        reportChart.timeScale().fitContent();
    }
    async function updateChange(quote, id, date) {
        try {
            const bars = await visibleHistory(quote.stock);
            if (state?.training_id !== id || state?.current_date !== date) return;
            const cell = $(`change-${quote.stock}`);
            const previous = bars.filter(bar => bar.date < date).at(-1);
            if (!cell) return;
            const change = quote.bar && previous ? (Number(quote.bar.close)/Number(previous.close)-1)*100 : null;
            cell.textContent = change === null ? '当天涨跌 —（无当日或前收盘价）' : `当天涨跌 ${signed(change)}%`;
            cell.className = change === null ? '' : tone(change);
        } catch (error) {message(error.message,true);}
    }
    async function visibleHistory(stock) {
        const key = `${state.training_id}/${state.current_date}/${stock}`;
        if (!historyCache.has(key)) {
            const request = api(`${path()}/history/${stock}`).then(data => data.bars).catch(error => {historyCache.delete(key);throw error;});
            historyCache.set(key,request);
        }
        return await historyCache.get(key);
    }
    function makeChart(node) {
        const result = LightweightCharts.createChart(node,{autoSize:true,layout:{background:{color:'#ffffff'},textColor:'#748378',fontSize:11},grid:{vertLines:{color:'#f0f4f1'},horzLines:{color:'#f0f4f1'}},rightPriceScale:{borderColor:'#e6eee8'},timeScale:{borderColor:'#e6eee8',timeVisible:false},localization:{locale:'zh-CN'}});
        return result;
    }
    function ensureCharts() {
        if (priceChart || !window.LightweightCharts) return;
        priceChart = makeChart($('price-chart')); volumeChart = makeChart($('volume-chart'));
        candle = priceChart.addSeries(LightweightCharts.CandlestickSeries,{upColor:'#d45d58',downColor:'#298563',borderVisible:false,wickUpColor:'#d45d58',wickDownColor:'#298563'});
        volume = volumeChart.addSeries(LightweightCharts.HistogramSeries,{priceFormat:{type:'volume'}});
        for (const [period,color] of Object.entries(maColors)) {
            maSeries.set(Number(period),priceChart.addSeries(LightweightCharts.LineSeries,{color,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}));
        }
        let movingCrosshair = false;
        for (const [from,to,toSeries,isVolume] of [[priceChart,volumeChart,volume,true],[volumeChart,priceChart,candle,false]]) {
            from.subscribeCrosshairMove(param => {
                if (movingCrosshair || !state || $('workspace').hidden) return;
                const bar = param.point && chartRows.find(row => row.date === chartDate(param.time));
                showBarInfo(bar || chartRows.at(-1),!!bar);
                movingCrosshair = true;
                try {
                    // setData/resize may briefly leave the other pane without
                    // this time point. Do not ask the library to locate it yet.
                    if (bar && to.timeScale().timeToCoordinate(bar.date)!==null && toSeries.data().some(point=>chartDate(point.time)===bar.date)) {
                        to.setCrosshairPosition(Number(isVolume?bar.volume:bar.close),bar.date,toSeries);
                    } else to.clearCrosshairPosition();
                }
                finally {movingCrosshair=false;}
            });
        }
        let syncing = false;
        for (const [a,b] of [[priceChart,volumeChart],[volumeChart,priceChart]]) a.timeScale().subscribeVisibleLogicalRangeChange(range => {if(range && !syncing){syncing=true;try{b.timeScale().setVisibleLogicalRange(range);}finally{syncing=false;}}});
    }
    async function drawStock() {
        if (!state || !selectedStock) return;
        const seq = ++quoteSeq, stock = selectedStock;
        estimateSeq++;
        estimateValid = false; controls();
        $('estimate').textContent = state.status === 'ended' ? '训练已结束，可查看报告及完整区间走势。' : '正在加载当前股票…';
        const full = report && $('chart-mode').value === 'full';
        $('chart-title').textContent = `${stockLabel(stock)} · ${full?'完整区间复盘':'日 K 线'}`;
        $('chart-foot').textContent = full ? '已结束：完整区间仅用于复盘，不改变账户日期、现金或报告估值。' : '红涨绿跌 · 仅使用截至公共日期的行情；无未来指标或复权因子。';
        $('order-stock').textContent = stock;
        const quote = state.quotes.find(q => q.stock === stock), pos = state.positions.find(p => p.stock === stock);
        $('quote-note').textContent = `${statusNames[quote.status] || quote.status}${quote.status !== 'trading' && quote.reason ? ` · ${quote.reason}` : ''}${pos.valuation.stale ? ` · 估值价格日期 ${pos.valuation.price_date}` : ''}`;
        if (full) $('quote-note').textContent = `完整区间 · ${state.config.start_date} 至 ${state.config.end_date}`;
        $('order-price').innerHTML = quote.bar ? `当前收盘 <strong class="${tone(Number(quote.bar.close)-Number(quote.bar.open))}">${fmt(quote.bar.close)}</strong> 元 / 股` : `${esc(statusNames[quote.status]||quote.status)} · 当前无可交易报价`;
        $('order-position').innerHTML = `<div><span>持仓总股数</span><strong>${shares(pos.quantity)} <small>股</small></strong></div><div><span>当前可卖 / 股</span><b>${shares(pos.sellable_now)}</b><span>委托冻结 / 股</span><b>${shares(pos.frozen_shares)}</b></div>`;
        $('availability').innerHTML = `可用现金 <b>${fmt(state.account.available_cash)}</b> 元<br>当前可卖 <b>${pos.sellable_now}</b> 股 · 冻结 ${pos.frozen_shares} 股<br>下一开盘可申报卖出 <b>${pos.orderable_next_open}</b> 股`;
        ensureCharts(); chartRows=[]; candle?.setData([]); volume?.setData([]);
        for (const series of maSeries.values()) series.setData([]);
        const rows = full ? report.history[stock] : await visibleHistory(stock);
        if (seq !== quoteSeq) return;
        $('chart-empty').hidden = !!rows.length;
        if (!priceChart) {message('图表库加载失败，请刷新页面。',true);return;}
        chartRows = rows;
        candle.setData(rows.map(bar => ({time:bar.date,open:Number(bar.open),high:Number(bar.high),low:Number(bar.low),close:Number(bar.close)})));
        volume.setData(rows.map(bar => ({time:bar.date,value:Number(bar.volume),color:Number(bar.close)>=Number(bar.open)?'#d45d5870':'#29856370'})));
        drawMovingAverages();
        priceChart.timeScale().fitContent(); volumeChart.timeScale().fitContent();
    }
    async function estimateOrder() {
        const seq = ++estimateSeq;
        estimateValid = false; controls();
        if (!state || state.status !== 'active') { $('estimate').textContent='训练已结束，可查看报告及完整区间走势。'; return; }
        if (!state.next_date) { $('estimate').textContent='已到所选区间末日，可结束并生成报告。'; return; }
        const qty = orderShares();
        updateQuantityHint();
        if (!qty) { $('estimate').textContent=$('odd-lot').checked ? '请输入正整数股数。' : '请输入正整数手数；1 手 = 100 股，最低 1 手。'; return; }
        $('estimate').textContent = '正在按历史费率估算…';
        const version = state.version;
        try {
            const result = await api(`${path()}/estimate`,{stock:selectedStock,side:$('side').value,quantity:qty});
            if (seq !== estimateSeq || version !== state.version) return;
            const order = result.order, fees = order.estimated_fees;
            estimateValid = order.status === 'pending' && result.version === state.version;
            $('estimate').innerHTML = `<div class="estimate-execution">${esc(order.execute_on)} 开盘 · ${order.side==='buy'?'买入':'卖出'} ${qty} 股</div><dl><dt>参考收盘价</dt><dd>${fmt(state.quotes.find(q=>q.stock===selectedStock)?.bar?.close)} 元</dd><dt>估算手续费</dt><dd>${fmt(fees.total)} 元</dd><dt>${order.side==='buy'?'本单冻结预算':'本单冻结股数'}</dt><dd>${order.side==='buy'?`${fmt(order.frozen_cash)} 元`:`${order.frozen_shares} 股`}</dd></dl><small>佣金 ${fmt(fees.commission)} + 过户 ${fmt(fees.transfer)} + 印花税 ${fmt(fees.stamp)}</small>${estimateValid ? '' : `<p class="order-reason">${esc(order.reason)}</p>`}`;
        } catch (error) { if (seq === estimateSeq) $('estimate').textContent = error.message; }
        finally {controls();}
    }
    function chartDate(time) {
        if (typeof time === 'string') return time;
        return time?.year ? `${time.year}-${String(time.month).padStart(2,'0')}-${String(time.day).padStart(2,'0')}` : null;
    }
    function drawMovingAverages() {
        for (const [period,series] of maSeries) {
            const data = PortfolioUI.movingAverage(chartRows,period);
            maData.set(period,new Map(data.map(point=>[point.time,point.value])));
            series.setData(data);
            series.applyOptions({visible:document.querySelector(`[data-ma="${period}"]`).checked});
        }
        showBarInfo(chartRows.at(-1));
    }
    function showBarInfo(bar, hovered = false) {
        if (!bar) {
            $('hover-info').textContent = '截至当前日期暂无有效日线。';
            $('ma-values').textContent = '均线需要有效收盘数据。';
            return;
        }
        const index = chartRows.indexOf(bar), previous = chartRows[index-1];
        const change = previous ? (Number(bar.close)/Number(previous.close)-1)*100 : null;
        $('hover-info').innerHTML = `<strong>${esc(bar.date)} ${hovered?'· 光标所指':'· 最新有效日线'}</strong>${[['开盘',bar.open],['最高',bar.high],['最低',bar.low],['收盘',bar.close]].map(([label,value])=>`<span>${label} <b class="${previous?tone(Number(value)-Number(previous.close)):''}">${fmt(value)}</b></span>`).join('')}<span>成交量 <b>${Number(bar.volume).toLocaleString('zh-CN')} 股</b></span><span>涨跌 <b class="${tone(change)}">${change===null?'—':`${signed(change)}%`}</b></span>`;
        $('ma-values').innerHTML = [...maSeries.keys()].filter(period=>document.querySelector(`[data-ma="${period}"]`).checked).map(period=>{
            const value = maData.get(period)?.get(bar.date);
            return `<span style="color:${maColors[period]}">MA${period} <b>${value===undefined?`—（不足 ${period} 日）`:fmt(value)}</b></span>`;
        }).join('');
    }
    async function drawComparison() {
        if (!state || !window.LightweightCharts) return;
        const seq = ++comparisonSeq, current = state;
        const histories = await Promise.all(current.config.stocks.map(async code=>[code,await visibleHistory(code)]));
        if (seq !== comparisonSeq || state?.training_id !== current.training_id || state?.current_date !== current.current_date) return;
        if (!comparisonChart) {
            comparisonChart = makeChart($('comparison-chart'));
            comparisonChart.subscribeCrosshairMove(param=>showComparisonInfo(chartDate(param.time)));
        }
        for (const line of comparisonLines.values()) comparisonChart.removeSeries(line);
        comparisonLines.clear(); comparisonPoints.clear();
        const dates = current.daily_assets.map(asset=>asset.date);
        const entries = histories.map(([code,bars],index)=>({code,label:stockLabel(code),color:comparisonColors[index%comparisonColors.length],points:PortfolioUI.comparison(bars,dates,current.current_date)}));
        entries.push({code:'account',label:'组合账户（含现金 / 费用）',color:'#203b35',points:current.daily_assets.map(asset=>({time:asset.date,value:(Number(asset.total_assets)/Number(current.account.initial_cash)-1)*100}))});
        for (const entry of entries) {
            const line = comparisonChart.addSeries(LightweightCharts.LineSeries,{color:entry.color,lineWidth:entry.code==='account'?3:2,priceFormat:{type:'percent',precision:2,minMove:.01},priceLineVisible:false,lastValueVisible:false,visible:!hiddenComparison.has(entry.code)});
            line.setData(entry.points); comparisonLines.set(entry.code,line); comparisonPoints.set(entry.code,entry);
        }
        $('comparison-legend').innerHTML = entries.map(entry=>`<label style="--series-color:${entry.color}"><input type="checkbox" data-compare="${esc(entry.code)}" ${hiddenComparison.has(entry.code)?'':'checked'}><span></span>${esc(entry.label)}</label>`).join('');
        showComparisonInfo(current.current_date);
        comparisonChart.timeScale().fitContent();
    }
    function showComparisonInfo(date) {
        if (!state) return;
        date = date || state.current_date;
        $('comparison-info').innerHTML = `<strong>${esc(date)}</strong> ` + [...comparisonPoints.values()].filter(entry=>!hiddenComparison.has(entry.code)).map(entry=>{
            const point = entry.points.find(point=>point.time===date);
            return `<span style="color:${entry.color}">${esc(entry.label)} <b>${point?`${signed(point.value)}%`:'—'}</b></span>`;
        }).join('');
    }
    function switchChartView(view) {
        chartView = view;
        $('single-view').hidden = view !== 'single';
        $('comparison-view').hidden = view !== 'comparison';
        document.querySelectorAll('[data-chart-view]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.chartView===view)));
        requestAnimationFrame(()=>{
            if(chartView==='single'){priceChart?.timeScale().fitContent();volumeChart?.timeScale().fitContent();}
            else comparisonChart?.timeScale().fitContent();
        });
    }
    function orderShares() {return PortfolioUI.sharesFromInput($('quantity').value,$('side').value==='sell' && $('odd-lot').checked);}
    function updateQuantityHint() {
        const odd = $('side').value==='sell' && $('odd-lot').checked;
        $('quantity-label').textContent = odd ? '零股卖出数量 / 股' : '委托数量 / 手';
        $('quantity').max = odd ? '1000000' : '10000';
        const quantity = orderShares();
        $('quantity-hint').textContent = quantity ? (odd ? `本单卖出 ${quantity} 股` : `${$('quantity').value} 手 × 100 = ${quantity.toLocaleString('zh-CN')} 股`) : '请填写有效数量。';
    }
    function quantityChanged() {
        stopPlayback('已暂停，方便你调整订单。');
        estimateSeq++; estimateValid=false; updateQuantityHint();
        $('estimate').textContent='正在更新估算…'; controls(); clearTimeout(estimateTimer);
        estimateTimer=setTimeout(run(estimateOrder),200);
    }
    function stopPlayback(note = '已暂停；切换股票不会改变日期。') {
        playing=false; playGeneration++; clearTimeout(playTimer); playTimer=null;
        $('play-status').textContent=note; controls();
    }
    async function advancePortfolio() {
        if (!state || busy || pending || state.status!=='active' || !state.next_date) return false;
        const before=state.orders.filter(order=>order.status==='pending').map(order=>order.order_id);
        const ok=await mutate('advance',{expected_date:state.current_date});
        const rejected=state?.orders.filter(order=>before.includes(order.order_id)&&order.status==='rejected').map(order=>`${order.stock}：${order.reason}`)||[];
        if (rejected.length) {
            stopPlayback('已暂停：有订单未能成交，请查看订单说明。');
            message(`日期已推进；${rejected.join('；')}`,true); return false;
        }
        if (!ok) {stopPlayback('自动播放已暂停，请处理页面提示。');return false;}
        if (state.status === 'ended' || !state.next_date) {stopPlayback('训练已结束，总结已保存。');return false;}
        return true;
    }
    function startPlayback() {
        if (busy || pending || !state?.next_date || state.status!=='active') return;
        playing=true; const generation=++playGeneration;
        $('play-status').textContent='自动推进中；按提交顺序处理已有订单。点击“暂停播放”随时暂停。'; controls();
        const tick=async()=>{
            if (!playing || generation!==playGeneration) return;
            try {
                const ok=await advancePortfolio();
                if (ok && playing && generation===playGeneration) playTimer=setTimeout(tick,Number($('play-speed').value));
            } catch(error) {stopPlayback('遇到问题，自动播放已暂停。');message(error.message,true);}
        };
        playTimer=setTimeout(tick,Number($('play-speed').value));
    }
    function table(headers,rows) {return rows.length ? `<table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>` : '<div class="empty">暂无记录</div>';}
    const td = value => `<td>${esc(value)}</td>`;
    function orderActions(order) {
        return order.status === 'pending' && state.status === 'active' ? `<div class="order-actions"><button type="button" data-edit-order="${esc(order.order_id)}">撤单并修改</button><button type="button" data-cancel="${esc(order.order_id)}">撤单</button></div>` : '—';
    }
    function renderPendingOrders() {
        const orders = state.orders.filter(order => order.status === 'pending');
        const expanded = new Set([...$('pending-orders').querySelectorAll('details[open]')].map(node => node.dataset.orderGroup));
        const groups = new Map();
        for (const order of [...orders].reverse()) {
            const key = JSON.stringify([state.training_id,order.stock,order.side,order.execute_on]);
            if (!groups.has(key)) groups.set(key,{key,stock:order.stock,side:order.side,execute_on:order.execute_on,quantity:0,orders:[]});
            const group = groups.get(key);
            group.quantity += order.quantity;
            group.orders.push(order);
        }
        $('pending-orders').hidden = !orders.length || state.status !== 'active';
        $('pending-orders').innerHTML = `<h3>待成交委托 <span>${orders.length} 笔 · ${groups.size} 组</span></h3>${[...groups.values()].map(group => {
            const multiple = group.orders.length > 1;
            const details = multiple ? `<details class="pending-order-details" data-order-group="${esc(group.key)}"${expanded.has(group.key)?' open':''}><summary>查看 ${group.orders.length} 笔明细 / 撤改单</summary>${group.orders.map((order,index) => `<div class="pending-order-item"><p>第 ${index+1} 笔 <b>${shares(order.quantity)} 股</b></p>${orderActions(order)}</div>`).join('')}</details>` : orderActions(group.orders[0]);
            return `<div class="pending-order"><strong>${esc(stockLabel(group.stock))}</strong><p class="pending-order-total">${group.side==='buy'?'买入':'卖出'}${multiple?'合计':''} <b>${shares(group.quantity)} 股</b>${multiple?` · ${group.orders.length} 笔`:''}</p><p>${esc(group.execute_on)} 开盘</p>${details}</div>`;
        }).join('')}`;
    }
    async function handleOrderAction(event) {
        const button = event.target.closest('[data-cancel],[data-edit-order]');
        if (!button || busy || pending) return;
        const editing = button.hasAttribute('data-edit-order');
        const order = state.orders.find(order => order.order_id === (button.dataset.editOrder || button.dataset.cancel));
        if (!order || order.status !== 'pending' || state.status !== 'active') return;
        stopPlayback();
        const ok = await mutate('cancel', {order_id:order.order_id});
        if (!ok || !state.orders.some(row => row.order_id === order.order_id && row.status === 'cancelled')) return;
        if (!editing) {message('委托已撤销，冻结资金或股数已释放。');return;}
        selectedStock = order.stock;
        $('side').value = order.side;
        $('odd-lot').checked = order.side === 'sell' && order.quantity % 100 !== 0;
        $('quantity').value = String($('odd-lot').checked ? order.quantity : order.quantity / 100);
        render();
        await drawStock();
        await estimateOrder();
        message('原委托已撤销，已填回原股票、方向和数量。调整后请重新点击“提交委托”。');
        $('quantity').scrollIntoView({behavior:'smooth',block:'center'});
        $('quantity').focus({preventScroll:true});
    }
    function renderDetails() {
        renderPendingOrders();
        $('asset-chart').hidden = tab !== 'assets';
        document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected',String(b.dataset.tab === tab)));
        if (tab === 'positions') $('detail').innerHTML = table(['股票','持仓 / 股','冻结 / 股','当前可卖 / 股','下一开盘可申报 / 股','成本 / 元','估值 / 元','未实现盈亏','估值日期'],state.positions.filter(p=>p.quantity).map(p=>`<tr>${td(stockLabel(p.stock))}<td class="holding-quantity">${shares(p.quantity)}</td>${td(shares(p.frozen_shares))}${td(shares(p.sellable_now))}${td(shares(p.orderable_next_open))}${td(fmt(p.cost))}${td(fmt(p.market_value))}<td class="${tone(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>${td(`${p.valuation.price_date}${p.valuation.stale?' · 历史价':''}`)}</tr>`));
        if (tab === 'orders') $('detail').innerHTML = table(['股票 / 方向','数量 / 股','提交 / 执行日期','冻结现金 / 股数','估算费用','状态','说明','操作'],[...state.orders].reverse().map(o=>`<tr>${td(`${stockLabel(o.stock)} ${o.side === 'buy'?'买入':'卖出'}`)}${td(shares(o.quantity))}${td(`${o.submitted_on} / ${o.execute_on}`)}${td(`${fmt(o.frozen_cash)} / ${o.frozen_shares}`)}${td(fmt(o.estimated_fees.total))}${td(orderNames[o.status]||o.status)}<td class="reason">${esc(o.reason)}</td><td>${orderActions(o)}</td></tr>`));
        if (tab === 'fills') $('detail').innerHTML = table(['股票 / 方向','成交日','数量','价格','成交金额','佣金','过户费','印花税','费用合计','已实现盈亏'],[...state.fills].reverse().map(f=>`<tr>${td(`${f.stock} ${f.side === 'buy'?'买入':'卖出'}`)}${td(f.date)}${td(f.quantity)}${td(fmt(f.price))}${td(fmt(f.gross))}${td(fmt(f.fees.commission))}${td(fmt(f.fees.transfer))}${td(fmt(f.fees.stamp))}${td(fmt(f.fees.total))}${td(signed(f.realized_pnl))}</tr>`));
        if (tab === 'assets') {
            $('detail').innerHTML = table(['日期','现金','持仓市值','总资产','当日收益'],state.daily_assets.map(a=>`<tr>${td(a.date)}${td(fmt(a.cash))}${td(fmt(a.market_value))}${td(fmt(a.total_assets))}${td(signed(a.daily_pnl))}</tr>`));
            if (!assetChart && window.LightweightCharts) {assetChart = makeChart($('asset-chart'));assetLine = assetChart.addSeries(LightweightCharts.LineSeries,{color:'#24775a',lineWidth:2});}
            assetLine?.setData(state.daily_assets.map(a=>({time:a.date,value:Number(a.total_assets)})));assetChart?.timeScale().fitContent();
        }
        controls();
    }
    async function selectProfile() {
        if (busy || pending) return;
        stopPlayback();
        user = $('profile').value;
        if (!user) {message('请先在单股票入口创建本地用户。',true);return;}
        busy = true; controls();
        try {
            await api('/api/portfolio/session',{user});
            localStorage.setItem('currentUser',user);
            state=null;report=null;selectedStock=null;quoteSeq++;estimateSeq++;
            $('workspace').hidden=true;$('lobby').hidden=false;
            pending = JSON.parse(sessionStorage.getItem(pendingKey()) || 'null');
            const options = await api('/api/portfolio/options');
            feeRange = options.fee_range;
            $('source').innerHTML = options.sources.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
            await sourceChanged();await listTrainings();
            message(pending?'有尚未确认的操作，请使用“重试原操作”。':'');
        } finally {busy=false;controls();}
    }
    $('source').addEventListener('change',run(sourceChanged));
    function modeChanged() {
        const random = $('setup-mode').value === 'random';
        $('random-options').hidden = !random; $('specified-options').hidden = random;
        for (const id of ['random-count','random-days']) $(id).required = random;
        invalidate();
    }
    $('setup-mode').addEventListener('change',modeChanged);
    for (const id of ['random-count','random-days','random-sector']) $(id).addEventListener('input',invalidate);
    $('prepare-data').addEventListener('click',run(prepareData));
    for (const id of ['cash','start','end']) $(id).addEventListener('input',invalidate);
    $('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(run(search),200);});
    $('search-results').addEventListener('click',run(async event=>{
        const button=event.target.closest('[data-add]');if(!button || busy || pending)return;
        if(selected.size >= 20)throw new Error('每次训练最多选择 20 只股票。');
        selected.set(button.dataset.add,names.get(button.dataset.add)||'');invalidate();renderSelected();await search();
    }));
    $('selected').addEventListener('click',event=>{const b=event.target.closest('[data-remove]');if(b && !busy && !pending){selected.delete(b.dataset.remove);invalidate();renderSelected();}});
    $('check').addEventListener('click',run(checkData));
    async function createTraining() {
        if(busy||pending||!$('create-form').reportValidity())return;
        message('正在准备股票和行情，请稍候…');
        const ok = await transact('/api/portfolio/trainings',{request_id:crypto.randomUUID(),...createParams()});
        if (ok) {sessionStorage.removeItem('portfolio.setup');window.scrollTo(0,0);}
    }
    $('create-form').addEventListener('submit',run(async event=>{event.preventDefault();await createTraining();}));
    $('profile').addEventListener('change',run(selectProfile));
    $('refresh-list').addEventListener('click',run(listTrainings));
    $('training-list').addEventListener('click',run(async event=>{const b=event.target.closest('[data-restore]');if(b)await openTraining(b.dataset.restore);}));
    $('stocks').addEventListener('click',run(async event=>{const b=event.target.closest('[data-stock]');if(!b||busy||pending)return;selectedStock=b.dataset.stock;document.querySelectorAll('[data-stock]').forEach(x=>x.classList.toggle('active',x===b));await drawStock();await estimateOrder();}));
    $('quantity').addEventListener('input',quantityChanged);
    $('odd-lot').addEventListener('change',()=>{$('quantity').value='1';quantityChanged();});
    document.querySelectorAll('[data-side]').forEach(button=>button.addEventListener('click',()=>{const old=$('side').value;$('side').value=button.dataset.side;if(old!==$('side').value){$('odd-lot').checked=false;$('quantity').value='1';}quantityChanged();}));
    document.querySelectorAll('[data-lots]').forEach(button=>button.addEventListener('click',()=>{$('odd-lot').checked=false;$('quantity').value=button.dataset.lots;quantityChanged();}));
    for(const [id,step] of [['quantity-minus',-1],['quantity-plus',1]]) $(id).addEventListener('click',()=>{$('quantity').value=String(Math.max(1,(Number($('quantity').value)||1)+step));quantityChanged();});
    $('sell-all').addEventListener('click',()=>{const quantity=state?.positions.find(p=>p.stock===selectedStock)?.orderable_next_open||0;$('odd-lot').checked=quantity%100!==0;$('quantity').value=String($('odd-lot').checked?quantity:quantity/100);quantityChanged();});
    $('order-form').addEventListener('submit',run(async event=>{event.preventDefault();stopPlayback();if(estimateValid && orderShares()){const ok=await mutate('orders',{stock:selectedStock,side:$('side').value,quantity:orderShares()});if(ok)message('委托已保存，可在下单区下方撤单或撤单并修改；推进到下一交易日后尝试成交。');}}));
    $('advance').addEventListener('click',run(advancePortfolio));
    $('play').addEventListener('click',()=>{if(playing)stopPlayback();else startPlayback();});
    $('play-speed').addEventListener('change',()=>{if(playing)$('play-status').textContent='播放速度已调整，从下一轮等待起生效；点击“暂停播放”可暂停。';});
    document.addEventListener('visibilitychange',()=>{if(document.hidden && playing)stopPlayback('页面已隐藏，自动播放暂停。');});
    window.addEventListener('pagehide',()=>{playing=false;clearTimeout(playTimer);});
    document.querySelectorAll('[data-ma]').forEach(input=>input.addEventListener('change',drawMovingAverages));
    document.querySelectorAll('[data-chart-view]').forEach(button=>button.addEventListener('click',()=>switchChartView(button.dataset.chartView)));
    $('comparison-legend').addEventListener('change',event=>{const input=event.target.closest('[data-compare]');if(input){if(input.checked)hiddenComparison.delete(input.dataset.compare);else hiddenComparison.add(input.dataset.compare);comparisonLines.get(input.dataset.compare)?.applyOptions({visible:input.checked});showComparisonInfo();}});
    $('detail').addEventListener('click',run(handleOrderAction));
    $('pending-orders').addEventListener('click',run(handleOrderAction));
    document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;renderDetails();}));
    $('chart-mode').addEventListener('change',run(drawStock));
    async function backToLobby(newTraining = false) {
        if(busy||pending)return;
        const ended = state?.status === 'ended';
        stopPlayback();quoteSeq++;estimateSeq++;comparisonSeq++;state=null;report=null;reviewOpen=false;
        $('workspace').hidden=true;$('lobby').hidden=false;
        await listTrainings();message(ended?'总结已保存，可从“已完成的训练”随时查看。':'训练已自动保存，下次可继续。');
        window.scrollTo(0,0);
        if (newTraining) $('setup-mode').focus({preventScroll:true});
    }
    $('exit').addEventListener('click',run(()=>backToLobby()));
    $('report-new').addEventListener('click',run(()=>backToLobby(true)));
    $('report-review').addEventListener('click',run(async()=>{
        reviewOpen = !reviewOpen;render();
        if (reviewOpen) {await Promise.all([drawStock(),drawComparison()]);$('review-workspace').scrollIntoView({behavior:'smooth',block:'start'});}
    }));
    $('end-training').addEventListener('click',()=>{if(!busy&&!pending){stopPlayback();$('end-dialog').showModal();}});
    $('keep-training').addEventListener('click',()=>$('end-dialog').close());
    $('confirm-end').addEventListener('click',run(async()=>{$('end-dialog').close();await mutate('end');}));
    $('reset-training').addEventListener('click',()=>{
        if(busy || pending || !state)return;
        stopPlayback();
        $('reset-description').textContent = `将回到 ${state.config.start_date}，恢复 ${fmt(state.account.initial_cash)} 元初始资金。`;
        $('reset-dialog').showModal();
    });
    $('keep-progress').addEventListener('click',()=>$('reset-dialog').close());
    $('confirm-reset').addEventListener('click',run(async()=>{
        $('reset-dialog').close();
        const ok = await mutate('reset');
        if(ok)window.scrollTo(0,0);
    }));
    $('retry').addEventListener('click',run(async()=>{if(pending&&!busy)await transact(pending.url,pending.body);}));
    run(async()=>{
        const data=await api('/api/users');
        const users=Array.isArray(data) ? data : (data.users || []);
        $('profile').innerHTML=users.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');
        const draft = new URLSearchParams(location.search).get('setup') === '1' ? JSON.parse(sessionStorage.getItem('portfolio.setup') || 'null') : null;
        const last=draft?.user || localStorage.getItem('currentUser');if(users.includes(last))$('profile').value=last;
        await selectProfile();
        if (draft && users.includes(draft.user) && !pending) {
            if ([...$('source').options].some(option=>option.value===draft.data_source)) $('source').value=draft.data_source;
            await sourceChanged();
            $('setup-mode').value=draft.mode; $('cash').value=draft.initial_cash;
            $('start').value=draft.start_date; $('end').value=draft.end_date;
            $('random-count').value=draft.stock_count; $('random-days').value=draft.training_days;
            $('random-sector').value=draft.sector;
            selected = new Map(draft.stocks.map(code=>[code,names.get(code)||'']));
            modeChanged(); renderSelected();
            history.replaceState(null,'','portfolio.html');
            await createTraining();
        }
    })();
})();
