/* Pure UI calculations. Only already-visible history may be passed in. */
(function (root) {
    'use strict';
    function movingAverage(bars, period) {
        let sum = 0;
        return bars.flatMap((bar, i) => {
            sum += Number(bar.close);
            if (i >= period) sum -= Number(bars[i - period].close);
            return i + 1 >= period ? [{time:bar.date, value:sum / period}] : [];
        });
    }
    function sharesFromInput(value, oddLot = false) {
        if (!/^\d+$/.test(String(value))) return null;
        const input = Number(value), shares = input * (oddLot ? 1 : 100);
        return Number.isSafeInteger(shares) && shares > 0 && shares <= 1000000 ? shares : null;
    }
    function comparison(bars, dates, through) {
        const visible = bars.filter(bar => bar.date <= through);
        if (!visible.length) return [];
        const anchor = Number(visible[0].close), result = [];
        let index = 0, last = null;
        for (const date of dates) {
            if (date > through) break;
            while (index < visible.length && visible[index].date <= date) last = visible[index++];
            if (last) result.push({time:date,value:(Number(last.close) / anchor - 1) * 100});
        }
        return result;
    }
    const api = {movingAverage,sharesFromInput,comparison};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.PortfolioUI = api;
})(typeof window === 'undefined' ? {} : window);
