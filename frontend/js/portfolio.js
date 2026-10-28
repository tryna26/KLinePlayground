/* Shared-date portfolio UI. Account amounts/fees always come from the server. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const fmt = value => Number(value ?? 0).toLocaleString('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:2});
    const signed = value => `${Number(value) > 0 ? '+' : ''}${fmt(value)}`;
    const tone = value => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : '';
    const statusNames = {trading:'正常交易',suspended:'停牌',not_listed:'尚未上市',delisted:'已退市',download_failed:'下载失败',cache_missing:'缓存缺失',unknown:'原因未明',invalid:'行情无效',no_trades:'无有效成交',market_closed:'休市',unsupported:'暂不支持'};
    const orderNames = {pending:'待成交',filled:'已成交',cancelled:'已撤单',rejected:'已拒绝'};
    let user = null, state = null, selectedStock = null, selected = new Map(), names = new Map();
    let busy = false, pending = null, checked = null, tab = 'positions', report = null, feeRange = null;
    let quoteSeq = 0, estimateSeq = 0, searchSeq = 0, previewSeq = 0;
    let searchTimer, estimateTimer, estimateValid = false;
    let priceChart, candle, volumeChart, volume, assetChart, assetLine;
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
        const timer = setTimeout(() => controller.abort(), 30000);
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
        for (const id of ['profile','check','refresh-list','source','prepare-data','search','cash','start','end','side','quantity','exit','end-training','chart-mode']) $(id).disabled = locked;
        for (const id of ['side','quantity']) $(id).disabled = locked || state?.status === 'ended';
        $('create').disabled = locked || checked !== fingerprint() || !selected.size;
        $('advance').disabled = locked || playing || !state?.next_date || state?.status !== 'active';
        $('advance').textContent = busy ? '正在推进 / 保存…' : state?.status === 'ended' ? '训练已结束' : state?.next_date ? `下一交易日 → ${state.next_date}` : '已到区间末日';
        $('play').textContent = playing ? '⏸ 暂停播放' : '▶ 自动播放';
        $('play').disabled = !playing && (locked || !state?.next_date || state?.status !== 'active');
        $('end-training').hidden = state?.status === 'ended';
        $('submit-order').disabled = locked || !estimateValid || state?.status !== 'active';
        $('retry-panel').hidden = !pending || busy;
        $('retry').disabled = busy;
        document.querySelectorAll('[data-restore],[data-cancel],[data-remove],[data-add],[data-stock],[data-side],[data-lots],#quantity-minus,#quantity-plus,#odd-lot,#sell-all').forEach(button => {button.disabled = locked || (state?.status === 'ended' && !button.hasAttribute('data-stock'));});
        const buy = $('side').value === 'buy';
        document.querySelectorAll('[data-side]').forEach(button => {
            const active = button.dataset.side === $('side').value;
            button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
        });
        $('submit-order').textContent = `${buy?'买入':'卖出'} ${orderShares() ? `${orderShares()} 股` : ''} · 下一开盘执行`;
        $('submit-order').classList.toggle('sell-order',!buy);
        $('sell-all').hidden = buy;
        $('odd-lot-wrap').hidden = buy;
    }
    function fingerprint() {return JSON.stringify(createParams());}
    function createParams() {return {stocks:[...selected.keys()],start_date:$('start').value,end_date:$('end').value,initial_cash:$('cash').value,data_source:$('source').value};}
    function invalidate() {
        checked = null; previewSeq++;
        $('preview').textContent = '选项已改变，请重新检查股票和日期范围。';
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
            const range = row.coverage ? `核验范围 ${row.coverage.start} 至 ${row.coverage.end}` : row.reason;
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
            : '腾讯未复权日线；仅允许后端已核验历史状态、公司行为及日历的区间。范围不足会明确阻止开始。';
        $('real-data-actions').hidden = $('source').value !== 'sina';
        if (feeRange) $('source-note').textContent += ` 历史费率覆盖 ${feeRange.start} 至 ${feeRange.end}。`;
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
        $('training-list').innerHTML = data.trainings.map(row => `<div class="saved"><div><strong>${esc(row.current_date)} · ${row.config.stocks.length} 只股票 ${row.config.synthetic?'· 合成演示':'· 真实日线'}</strong><p>${esc(row.config.start_date)} 至 ${esc(row.config.end_date)} · ${row.status === 'ended'?'已结束':'自动保存'}<br>${esc(row.training_id.slice(0,8))}</p></div><button data-restore="${esc(row.training_id)}">${row.status === 'ended'?'查看报告':'继续训练'}</button></div>`).join('') || '<div class="empty">暂无组合记录。创建第一份练习吧。</div>';
        controls();
    }
    async function transact(url, body) {
        if (busy) return false;
        busy = true;
        pending = {url,body}; sessionStorage.setItem(pendingKey(), JSON.stringify(pending)); controls();
        try {
            const result = await api(url,body);
            // A retry can return an older original response. Always re-read authority.
            await openTraining(result.training_id, true);
            pending = null; sessionStorage.removeItem(pendingKey());
            message('操作已保存。');
            return true;
        } catch (error) {
            if (error.status && error.status !== 503) {
                pending = null; sessionStorage.removeItem(pendingKey());
                if (error.status === 409 && state) await openTraining(state.training_id, true);
            }
            message(error.message, true);
            stopPlayback('自动播放已暂停，请先处理提示或重试原操作。');
            return false;
        } finally {busy = false; controls();}
    }
    async function mutate(action, extra = {}) {
        if (busy || pending || !state) return false;
        return await transact(`${path()}/${action}`, {request_id:crypto.randomUUID(),expected_version:state.version,...extra});
    }
    async function openTraining(id, internal = false) {
        if (!internal && (busy || pending)) return;
        if (!internal) stopPlayback();
        if (!internal) {busy = true; message(''); controls();}
        try {
            const restored = await api(`/api/portfolio/trainings/${encodeURIComponent(id)}/restore`, {});
            if (state?.training_id !== restored.training_id || state?.current_date !== restored.current_date) historyCache.clear();
            state = restored;
            if (['synthetic-demo','synthetic-practice'].includes(state.config.data_source)) {
                const prefix=state.config.data_source==='synthetic-practice'?'练习':'合成';
                for (const [code,label] of [['600000','甲'],['000001','乙'],['600036','丙']]) names.set(code,prefix+label);
            } else {
                const catalog = await api(`/api/portfolio/options?data_source=${encodeURIComponent(state.config.data_source)}`);
                for (const row of catalog.stocks) names.set(row.stock,row.name);
            }
            selectedStock = state.config.stocks.includes(selectedStock) ? selectedStock : state.config.stocks[0];
            $('lobby').hidden = true; $('workspace').hidden = false;
            report = null; $('chart-mode').value = 'bounded';
            if (state.status === 'ended') report = await api(`${path()}/report`);
            render(); await Promise.all([drawStock(),drawComparison()]); await estimateOrder();
        } finally {if (!internal) {busy = false; controls();}}
    }
    function render() {
        const last = state.daily_assets.at(-1), pnl = Number(last.total_assets)-Number(state.account.initial_cash);
        $('date').textContent = state.current_date;
        $('session-note').textContent = `${state.status === 'ended'?'已结束':'自动保存'} · ${state.config.stocks.length} 只股票 · ${state.config.start_date} 至 ${state.config.end_date}`;
        $('simulation-note').textContent = state.synthetic ? '合成练习 · 价格、历史状态和交易日历均为人工样本。订单最早在下一公共交易日开盘尝试成交。' : `${state.config.data_source==='sina'?'新浪真实历史日线 · 已核验区间':'腾讯历史日线'} · 使用未复权成交价和历史费率；日线简化模拟无法还原实际盘口与排队。`;
        $('metrics').innerHTML = metric('总资产 / 元',fmt(last.total_assets),`累计收益 ${signed(pnl)}`,tone(pnl)) + metric('可用现金 / 元',fmt(state.account.available_cash),'所有股票共享') + metric('冻结现金 / 元',fmt(state.account.frozen_cash),'待成交买单占用') + metric('持仓市值 / 元',fmt(last.market_value),`收盘估值 ${state.current_date}`) + metric('当日收益 / 元',signed(last.daily_pnl),`已实现 ${signed(state.account.realized_pnl)}`,tone(last.daily_pnl));
        $('stock-count').textContent = `${state.config.stocks.length} 只`;
        $('stocks').innerHTML = state.quotes.map(quote => {
            const position = state.positions.find(p => p.stock === quote.stock);
            return `<button class="stock-button ${selectedStock === quote.stock?'active':''}" data-stock="${esc(quote.stock)}"><span class="stock-line"><strong>${esc(quote.stock)}</strong><span>${quote.bar?fmt(quote.bar.close):'—'}</span></span><small>${esc(names.get(quote.stock)||'沪深主板')} · ${esc(statusNames[quote.status]||quote.status)}</small><small id="change-${esc(quote.stock)}">涨跌计算中…</small><small>${position.quantity ? `持有 ${position.quantity} 股`:'未持有'}${position.valuation.stale?' · 使用历史价估值':''}</small></button>`;
        }).join('');
        for (const quote of state.quotes) updateChange(quote, state.training_id, state.current_date);
        $('report').hidden = !report;
        if (report) {
            const s = report.summary;
            $('report-summary').innerHTML = metric('总收益 / 元',signed(s.total_pnl),`收益率 ${s.return_percent}%`,tone(s.total_pnl)) + metric('最大回撤',`${s.max_drawdown_percent}%`) + metric('成交 / 总费用',`${s.fill_count} 笔`,`${fmt(s.total_fees)} 元`) + metric('已实现 / 未实现',signed(s.realized_pnl),`${signed(s.unrealized_pnl)} 元`);
            $('report-note').textContent = report.note;
        }
        renderDetails(); controls();
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
        $('quote-note').textContent = `${statusNames[quote.status] || quote.status} · ${quote.reason || '收盘行情'}${pos.valuation.stale ? ` · 估值价格日期 ${pos.valuation.price_date}` : ''}`;
        if (full) $('quote-note').textContent = `行情区间 ${state.config.start_date} 至 ${state.config.end_date}；账户估值仍截至 ${state.current_date}`;
        $('order-price').innerHTML = quote.bar ? `当前收盘 <strong class="${tone(Number(quote.bar.close)-Number(quote.bar.open))}">${fmt(quote.bar.close)}</strong> 元 / 股` : `${esc(statusNames[quote.status]||quote.status)} · 当前无可交易报价`;
        $('availability').innerHTML = `可用现金 <b>${fmt(state.account.available_cash)}</b> 元<br>当前可卖 <b>${pos.sellable_now}</b> 股 · 冻结 ${pos.frozen_shares} 股<br>下一开盘可申报卖出 <b>${pos.orderable_next_open}</b> 股<br><small>T+1：当天买入不能当天成交卖出；下一开盘再检查。</small>`;
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
            $('estimate').innerHTML = `<div class="estimate-execution">${esc(order.execute_on)} 开盘尝试${order.side==='buy'?'买入':'卖出'} ${qty} 股</div><dl><dt>参考收盘价</dt><dd>${fmt(state.quotes.find(q=>q.stock===selectedStock)?.bar?.close)} 元</dd><dt>估算手续费</dt><dd>${fmt(fees.total)} 元</dd><dt>${order.side==='buy'?'本单冻结预算':'本单冻结股数'}</dt><dd>${order.side==='buy'?`${fmt(order.frozen_cash)} 元`:`${order.frozen_shares} 股`}</dd></dl><small>佣金 ${fmt(fees.commission)} + 过户 ${fmt(fees.transfer)} + 印花税 ${fmt(fees.stamp)}</small><p class="${estimateValid?'':'order-reason'}">${esc(estimateValid ? (order.side==='buy'?'预算含价格缓冲；实际按下一开盘价和费用结算，多余冻结退回。':'卖出所得在实际成交后到账；下一开盘再次检查可卖股数。') : order.reason)}</p>`;
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
        $('quantity-label').textContent = odd ? '零股卖出数量 / 股' : '交易数量 / 手（1 手 = 100 股）';
        $('quantity').max = odd ? '1000000' : '10000';
        const quantity = orderShares();
        $('quantity-hint').textContent = quantity ? (odd ? `本单卖出 ${quantity} 股；零股须按规则整笔处理。` : `${$('quantity').value} 手 × 100 = ${quantity.toLocaleString('zh-CN')} 股`) : '请填写有效数量。';
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
        if (!state.next_date) {stopPlayback('已到训练区间末日；可结束训练并查看报告。');return false;}
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
    function renderDetails() {
        $('asset-chart').hidden = tab !== 'assets';
        document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected',String(b.dataset.tab === tab)));
        if (tab === 'positions') $('detail').innerHTML = table(['股票','持有 / 冻结','当前可卖','下一开盘可申报','成本 / 元','估值 / 元','未实现盈亏','估值日期'],state.positions.filter(p=>p.quantity).map(p=>`<tr>${td(stockLabel(p.stock))}${td(`${p.quantity} / ${p.frozen_shares}`)}${td(p.sellable_now)}${td(p.orderable_next_open)}${td(fmt(p.cost))}${td(fmt(p.market_value))}<td class="${tone(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>${td(`${p.valuation.price_date}${p.valuation.stale?' · 历史价':''}`)}</tr>`));
        if (tab === 'orders') $('detail').innerHTML = table(['股票 / 方向','数量','提交 / 执行日期','冻结现金 / 股数','估算费用','状态','说明','操作'],[...state.orders].reverse().map(o=>`<tr>${td(`${o.stock} ${o.side === 'buy'?'买入':'卖出'}`)}${td(o.quantity)}${td(`${o.submitted_on} / ${o.execute_on}`)}${td(`${fmt(o.frozen_cash)} / ${o.frozen_shares}`)}${td(fmt(o.estimated_fees.total))}${td(orderNames[o.status]||o.status)}<td class="reason">${esc(o.reason)}</td><td>${o.status === 'pending' && state.status === 'active'?`<button data-cancel="${esc(o.order_id)}">撤单</button>`:'—'}</td></tr>`));
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
    $('create-form').addEventListener('submit',run(async event=>{event.preventDefault();if(busy||pending||checked!==fingerprint())return;await transact('/api/portfolio/trainings',{request_id:crypto.randomUUID(),...createParams()});}));
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
    $('order-form').addEventListener('submit',run(async event=>{event.preventDefault();stopPlayback();if(estimateValid && orderShares())await mutate('orders',{stock:selectedStock,side:$('side').value,quantity:orderShares()});}));
    $('advance').addEventListener('click',run(advancePortfolio));
    $('play').addEventListener('click',()=>{if(playing)stopPlayback();else startPlayback();});
    $('play-speed').addEventListener('change',()=>{if(playing)$('play-status').textContent='播放速度已调整，从下一轮等待起生效；点击“暂停播放”可暂停。';});
    document.addEventListener('visibilitychange',()=>{if(document.hidden && playing)stopPlayback('页面已隐藏，自动播放暂停。');});
    window.addEventListener('pagehide',()=>{playing=false;clearTimeout(playTimer);});
    document.querySelectorAll('[data-ma]').forEach(input=>input.addEventListener('change',drawMovingAverages));
    document.querySelectorAll('[data-chart-view]').forEach(button=>button.addEventListener('click',()=>switchChartView(button.dataset.chartView)));
    $('comparison-legend').addEventListener('change',event=>{const input=event.target.closest('[data-compare]');if(input){if(input.checked)hiddenComparison.delete(input.dataset.compare);else hiddenComparison.add(input.dataset.compare);comparisonLines.get(input.dataset.compare)?.applyOptions({visible:input.checked});showComparisonInfo();}});
    $('detail').addEventListener('click',run(async event=>{const b=event.target.closest('[data-cancel]');if(b){stopPlayback();await mutate('cancel',{order_id:b.dataset.cancel});}}));
    document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;renderDetails();}));
    $('chart-mode').addEventListener('change',run(drawStock));
    $('exit').addEventListener('click',run(async()=>{if(busy||pending)return;stopPlayback();quoteSeq++;estimateSeq++;comparisonSeq++;state=null;report=null;$('workspace').hidden=true;$('lobby').hidden=false;await listTrainings();message('训练已自动保存，下次可继续。');}));
    $('end-training').addEventListener('click',()=>{if(!busy&&!pending){stopPlayback();$('end-dialog').showModal();}});
    $('keep-training').addEventListener('click',()=>$('end-dialog').close());
    $('confirm-end').addEventListener('click',run(async()=>{$('end-dialog').close();await mutate('end');}));
    $('retry').addEventListener('click',run(async()=>{if(pending&&!busy)await transact(pending.url,pending.body);}));
    run(async()=>{
        const data=await api('/api/users');
        const users=Array.isArray(data) ? data : (data.users || []);
        $('profile').innerHTML=users.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');
        const last=localStorage.getItem('currentUser');if(users.includes(last))$('profile').value=last;
        await selectProfile();
    })();
})();
