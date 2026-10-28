const test = require('node:test');
const assert = require('node:assert/strict');
const {movingAverage,sharesFromInput,comparison} = require('../../frontend/js/portfolio_ui_math.js');
const bars = [10,12,14,16,18,20].map((close,i)=>({date:`2024-01-${String(i+2).padStart(2,'0')}`,close:String(close)}));

test('MA5 starts after five actual bars, MA10 remains unavailable',()=>{
    assert.deepEqual(movingAverage(bars,5),[{time:'2024-01-06',value:14},{time:'2024-01-07',value:16}]);
    assert.deepEqual(movingAverage(bars,10),[]);
});
test('MA uses trading observations, not filled-in suspension dates',()=>{
    assert.deepEqual(movingAverage([{date:'2024-01-02',close:'10'},{date:'2024-01-05',close:'14'}],2),[{time:'2024-01-05',value:12}]);
});
test('one hand always submits 100 shares, with no decimal or exponent shorthand',()=>{
    assert.equal(sharesFromInput('1'),100); assert.equal(sharesFromInput('20'),2000);
    for(const value of ['0','-1','1.5','1e2','', '10001']) assert.equal(sharesFromInput(value),null);
    assert.equal(sharesFromInput('37',true),37);
});
test('comparison anchors each stock independently and carries suspension valuations',()=>{
    assert.deepEqual(comparison([{date:'2024-01-03',close:'20'},{date:'2024-01-05',close:'22'}],['2024-01-02','2024-01-03','2024-01-04','2024-01-05'],'2024-01-04'),[{time:'2024-01-03',value:0},{time:'2024-01-04',value:0}]);
    const result=comparison([{date:'2024-01-02',close:'20'},{date:'2024-01-03',close:'22'}],['2024-01-02','2024-01-03'],'2024-01-03');
    assert.ok(Math.abs(result[1].value-10)<1e-10);
});
test('a future poison price cannot enter comparison values',()=>{
    assert.deepEqual(comparison([...bars,{date:'2099-01-01',close:'99999'}],['2024-01-02','2099-01-01'],'2024-01-02'),[{time:'2024-01-02',value:0}]);
});
