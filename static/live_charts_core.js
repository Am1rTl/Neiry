/* Timing and scaling for real-time SDK plots. Pure functions, independently tested. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NeuroLiveChartsCore = api;
})(typeof window !== 'undefined' ? window : globalThis, function() {
  'use strict';
  function number(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }
  function appendWave(dst, packet, values, maxSeconds=70, maxCount=12000) {
    if (!packet || !Array.isArray(values) || !values.length) return 0;
    const sr = number(packet.sr);
    const t0 = number(packet.t0);
    if (!(sr > 0) || t0 === null) return 0;
    const times = Array.isArray(packet.times) && packet.times.length === values.length ? packet.times : null;
    // Min-max buckets preserve peaks without charting all 250+ raw samples/s.
    const stride = Math.max(1, Math.ceil(sr/75), Math.ceil(values.length/500));
    let appended = 0;
    for (let i=0; i<values.length; i+=stride) {
      let lo=null, hi=null;
      for (let j=i;j<Math.min(i+stride,values.length);j++) {
        const y=number(values[j]), x=times?number(times[j]):t0+j/sr;
        if (x===null || y===null) continue;
        const p={x,y};
        if (!lo || p.y<lo.y) lo=p;
        if (!hi || p.y>hi.y) hi=p;
      }
      if (!lo) continue;
      const ordered = lo.x<=hi.x ? [lo,hi] : [hi,lo];
      for (const p of ordered) {
        const last = dst.at(-1);
        if(last && p.x <= last.x + 1e-8) continue;  // no backward/duplicate x
        if(last && p.x-last.x>Math.max(0.75,3/sr)) {
          dst.push({x:last.x + Math.min(0.15,(p.x-last.x)/2), y:null});
        }
        dst.push(p); appended++;
      }
    }
    const newest = dst.at(-1)?.x;
    if(newest !== undefined) {
      let first=0;
      while(first<dst.length && dst[first].x<newest-maxSeconds) first++;
      if(first)dst.splice(0,first);
    }
    if(dst.length>maxCount)dst.splice(0,dst.length-maxCount);
    return appended;
  }
  function appendMetric(dst,time,value,maxCount=3500) {
    const x=number(time),y=number(value);
    if(x===null||y===null||dst.length&&x<=dst.at(-1).x+1e-8)return false;
    dst.push({x,y});
    if(dst.length>maxCount)dst.splice(0,dst.length-maxCount);
    return true;
  }
  function bounds(arrays, since, until, previous=null, stable=true) {
    const vals=[];
    for(const arr of arrays) for(const p of arr) {
      if(p && p.x>=since && p.x<=until && number(p.y)!==null) vals.push(p.y);
    }
    if(!vals.length)return previous;
    vals.sort((a,b)=>a-b);
    const pick = q=>vals[Math.min(vals.length-1,Math.round((vals.length-1)*q))];
    let low=stable&&vals.length>=60?pick(0.015):vals[0];
    let high=stable&&vals.length>=60?pick(0.985):vals.at(-1);
    let span=high-low;
    if(!Number.isFinite(span)||span<=0) {
      span=Math.max(Math.abs(high)*0.08,0.000001);
      low-=span; high+=span;
    } else {
      low-=span*0.12;high+=span*0.12;
    }
    if(!previous||!stable)return {min:low,max:high};
    const mix=(old,target,alpha)=>old+(target-old)*alpha;
    return {
      min:mix(previous.min,low,low<previous.min?0.55:0.16),
      max:mix(previous.max,high,high>previous.max?0.55:0.16)
    };
  }
  function windowFor(now,seconds) {
    return {min:now-seconds,max:now};
  }
  return {number,appendWave,appendMetric,bounds,windowFor};
});
