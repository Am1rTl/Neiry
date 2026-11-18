/* Numerical contract for the old-server raw PSD adapter. Run: node --test scripts/test_paired_panel.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const sandbox = {window:{}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/paired_panel.js'),'utf8'), sandbox);
const convert = sandbox.window.NeuroPairedPanel.rawPowerFromSpectrum;
function spectrum() {
  const freqs=Array.from({length:1201},(_,i)=>i/12);
  const left=freqs.map(()=>0),right=freqs.map(()=>0);
  left[120]=1;left[240]=3;right[36]=5;
  return {freqs,psd_per_channel:[left,right],channels:['O1-T3','O2-T4']};
}
test('known spectral lines integrate to correct per-lead relative powers',()=>{
 const result=convert(spectrum());
 assert(result.available);assert.equal(result.status,'raw');
 assert(Math.abs(result.bands.alpha.left_relative_power-.25)<1e-12);
 assert(Math.abs(result.bands.beta.left_relative_power-.75)<1e-12);
 assert(Math.abs(result.bands.delta.right_relative_power-1)<1e-12);
 assert.equal(result.channels[0].window_seconds,12);
 assert.equal(result.bands.alpha.coherence_msc,undefined);
});
test('swapped channels are reordered by confirmed montage',()=>{
 const spec=spectrum();spec.channels.reverse();spec.psd_per_channel.reverse();
 assert(Math.abs(convert(spec).bands.alpha.left_relative_power-.25)<1e-12);
});
test('flat PSD never produces invented power',()=>{
 const spec=spectrum();spec.psd_per_channel=spec.psd_per_channel.map(psd=>psd.map(()=>0));
 const result=convert(spec);assert.equal(result.available,false);assert.equal(result.bands.alpha.left_relative_power,null);
});
test('malformed channel does not hide the other usable channel',()=>{
 const spec=spectrum();spec.psd_per_channel[0][100]=NaN;
 const result=convert(spec);assert(result.available);assert.equal(result.bands.alpha.left_relative_power,null);
 assert.equal(result.bands.delta.right_relative_power,1);
});
test('negative, truncated and unsupported PSD are rejected',()=>{
 let spec=spectrum();spec.psd_per_channel[0][100]=-1;
 assert.equal(convert(spec).bands.alpha.left_relative_power,null);
 spec=spectrum();spec.channels=['Fp1','Fp2'];assert.equal(convert(spec),null);
 spec=spectrum();spec.freqs=spec.freqs.slice(0,200);assert.equal(convert(spec),null);
});
test('short or irregular windows are rejected',()=>{
 let spec=spectrum();spec.freqs=spec.freqs.map(f=>f*3);assert.equal(convert(spec),null);
 spec=spectrum();spec.freqs[100]+=.005;assert.equal(convert(spec),null);
});
