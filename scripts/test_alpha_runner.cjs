/* Run the real alpha task with a deterministic, accelerated timer and no headset. */
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const window = {};
const script = fs.readFileSync(path.join(__dirname, '../static/experiment_runner.js'), 'utf8');
vm.runInNewContext(script, {window, AbortController, AbortSignal, performance, clearTimeout});
const open = 'eyes_open', closed = 'eyes_closed';

function runner(settings, mode='demo') {
  const r = new window.NeuroExperimentRunner({run:{id:'test',kind:'alpha',mode,settings}, sound:false});
  const heading = {}, timer = {};
  const panel = {dataset:{},querySelector:needle=>needle==='h3' ? heading : timer};
  r.body = {querySelector:()=>panel};
  r.pauseNode = {};
  r.progress = {};
  r.count = {};
  r.prep = async()=>{};
  // A tab pause can drain the queue. Retain events in memory for assertions.
  r.drain = async()=>{};
  r.countdown = async(seconds, update)=>update(0, 1);
  return r;
}
const starts = r => Array.from(r.pending.filter(e=>e.type==='phase_start'));
const labels = r => starts(r).map(e=>e.label);

test('uses stored counterbalanced order and repeats the interrupted phase without redrawing', async()=>{
  const r=runner({repeats:4,alpha_order:'counterbalanced',alpha_pair_first:[closed,open,open,closed]});
  const timer = r.countdown;
  let paused=false;
  r.countdown = async(seconds, update)=>{
    if (r.phase.index===2 && !paused) {
      paused=true;
      const signal = r.stepSignal;
      r.setAway('tab',true);
      r.setAway('tab',false);
      throw signal.reason;
    }
    await timer(seconds,update);
  };
  await r.alphaTask();
  assert.deepEqual(labels(r), [closed,open,open,open,closed,open,closed,closed,open]);
  assert.deepEqual(starts(r).map(e=>e.pair_index), [0,0,1,1,1,2,2,3,3]);
  const attempts=starts(r).filter(e=>e.index===2);
  assert.notEqual(attempts[0].phase_id,attempts[1].phase_id);
  assert.equal(r.pending.find(e=>e.type==='phase_end' && e.reason==='view_away').phase_id,attempts[0].phase_id);
});

test('legacy sessions retain open-first and a missing assigned plan never falls back to it', async()=>{
  const legacy=runner({repeats:2});
  await legacy.alphaTask();
  assert.deepEqual(labels(legacy),[open,closed,open,closed]);
  for (const first of [undefined,[open],[open,'unknown']]) {
    const invalid=runner({alpha_order:'counterbalanced',repeats:2,alpha_pair_first:first});
    await assert.rejects(invalid.alphaTask(),/Сохранённый порядок/);
    assert.deepEqual(labels(invalid),[]);
  }
});

test('solo live audio cannot silently turn into visual-only eye instructions', async()=>{
  const solo=runner({sound:true,repeats:2},'live');
  await assert.rejects(solo.alphaTask(),/Звуковые команды недоступны/);
  assert.deepEqual(labels(solo),[]);
  const assisted=runner({sound:false,repeats:2},'live');
  await assisted.alphaTask();
  assert.match(assisted.lastStatus,/Ведущий/);
  assert.deepEqual(labels(assisted),[open,closed,open,closed]);
});

test('open, close, and end cues use distinct, audible-pattern schedules', ()=>{
  const times=[];
  const audio={state:'running',currentTime:5,createOscillator:()=>({
    frequency:{},connect:()=>({connect(){}}),start:t=>times.push(t),stop(){}
  }),createGain:()=>({gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}})};
  const play=window.NeuroExperimentRunner.playCue;
  play(audio,'open'); assert.equal(times.length,2);
  play(audio,'closed',1); assert.equal(times.length,3);
  play(audio,'complete',2); assert.equal(times.length,6);
  assert(times.every((t,i)=>!i||t>times[i-1]));
});
