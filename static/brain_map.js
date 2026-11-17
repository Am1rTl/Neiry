/* Dependency-free 3D montage schematic. Surface shading is illustrative, not EEG data. */
(function (root) {
  'use strict';

  // x: left/right, y: superior, z: posterior. The default camera faces the occiput.
  const leads = [
    {color: '#74e5cf', temporal: {name: 'T3', point: [-.97, -.08, -.16]},
      occipital: {name: 'O1', point: [-.36, .22, .93]}},
    {color: '#a7b3ff', temporal: {name: 'T4', point: [.97, -.08, -.16]},
      occipital: {name: 'O2', point: [.36, .22, .93]}},
  ];
  const views = {oblique: [-.65, .4], rear: [0, .26], top: [0, 1.4], side: [Math.PI / 2, .16]};
  const tau = Math.PI * 2;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const hasPower = v => typeof v === 'number' && Number.isFinite(v);

  function rotate([x, y, z], yaw, pitch) {
    const X = x * Math.cos(yaw) + z * Math.sin(yaw);
    const Z = z * Math.cos(yaw) - x * Math.sin(yaw);
    return [X, y * Math.cos(pitch) - Z * Math.sin(pitch), y * Math.sin(pitch) + Z * Math.cos(pitch)];
  }

  function onSphere(point) {
    const length = Math.hypot(...point);
    return point.map(value => value / length);
  }

  function headPoint(lat, lon) {
    const y = Math.sin(lat);
    const ring = Math.cos(lat) * (1 - Math.max(0, -y) * .16);
    return [ring * Math.cos(lon), y * 1.08, ring * Math.sin(lon)];
  }

  // Actual, generic cortical surface. Only EEG bars carry measurement values.
  const model = root.NeuroBrainSurface;
  const mesh = [];
  const triangles = (model?.triangles || []).map(indices => {
    const [a,b,c] = indices.map(i => model.vertices[i]);
    const u = b.map((v,i)=>v-a[i]), v = c.map((n,i)=>n-a[i]);
    const n = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
    const length = Math.hypot(...n) || 1;
    return {indices, normal:n.map(v=>v/length), sulc:indices.reduce((sum,i)=>sum+model.sulci[i],0)/3};
  });
  if (model) {
    const normals = model.vertices.map(()=>[0,0,0]);
    for (const face of triangles) for (const i of face.indices) {
      for(let axis=0;axis<3;axis++) normals[i][axis]+=face.normal[axis];
    }
    for (const normal of normals) {
      const length=Math.hypot(...normal)||1;
      for(let axis=0;axis<3;axis++) normal[axis]/=length;
    }
    for(const face of triangles) {
      const normal=[0,1,2].map(axis=>face.indices.reduce((sum,i)=>sum+normals[i][axis],0)/3);
      const length=Math.hypot(...normal)||1;
      face.normal=normal.map(n=>n/length);
    }
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    mesh.push(Array.from({length: 81}, (_, i) => headPoint(lat * Math.PI / 180, i * tau / 80)));
  }
  for (let lon = 0; lon < tau; lon += Math.PI / 4) {
    mesh.push(Array.from({length: 51}, (_, i) => headPoint(-Math.PI / 2 + i * Math.PI / 50, lon)));
  }

  function leadCurve(lead, offset) {
    const from = onSphere(lead.temporal.point), to = onSphere(lead.occipital.point);
    const angle = Math.acos(clamp(from.reduce((sum, v, i) => sum + v * to[i], 0), -1, 1));
    return Array.from({length: 65}, (_, i) => {
      const t = i / 64, scale = Math.sin(angle);
      const a = Math.sin((1 - t) * angle) / scale, b = Math.sin(t * angle) / scale;
      return onSphere(from.map((v, axis) => a * v + b * to[axis] + (axis === 1 ? offset : 0)))
        .map(v => v * 1.025);
    });
  }

  function visibleFraction(points, fraction) {
    if (fraction >= 1) return points;
    const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
    let remaining = lengths.reduce((sum, n) => sum + n, 0) * clamp(fraction, 0, 1);
    const visible = [points[0]];
    for (const [i, length] of lengths.entries()) {
      if (length <= remaining) { visible.push(points[i + 1]); remaining -= length; }
      else {
        const part = length ? remaining / length : 0;
        const a = points[i], b = points[i + 1];
        visible.push({x: a.x + (b.x - a.x) * part, y: a.y + (b.y - a.y) * part, z: a.z + (b.z - a.z) * part});
        break;
      }
    }
    return visible;
  }

  function requestDraw(canvas, state) {
    if (state.frame !== null) return;
    state.frame = root.requestAnimationFrame(() => { state.frame = null; draw(canvas, state); });
  }

  function setView(canvas, name) {
    const state = canvas?._nsMap, view = views[name];
    if (!state || !view) return;
    [state.yaw, state.pitch] = view;
    state.pointer = null;
    syncView(canvas, name);
    requestDraw(canvas, state);
  }

  function syncView(canvas, name) {
    for (const button of canvas.closest('[data-pair-panel]')?.querySelectorAll('[data-map-view]') || []) {
      button.setAttribute('aria-pressed', String(button.dataset.mapView === name));
    }
  }

  function mount(canvas, series = []) {
    if (!canvas?.getContext) return;
    if (!canvas._nsMap) {
      const state = {yaw: views.oblique[0], pitch: views.oblique[1], series: [], pointer: null, frame: null, resize: null};
      canvas._nsMap = state;
      canvas.tabIndex = 0;
      canvas.addEventListener('pointerdown', event => {
        if (!event.isPrimary || event.button !== 0) return;
        state.pointer = [event.clientX, event.clientY];
        canvas.setPointerCapture?.(event.pointerId);
      });
      canvas.addEventListener('pointermove', event => {
        if (!state.pointer) return;
        state.yaw += (event.clientX - state.pointer[0]) * .009;
        state.pitch = clamp(state.pitch + (event.clientY - state.pointer[1]) * .006, -1.4, 1.4);
        state.pointer = [event.clientX, event.clientY];
        syncView(canvas, '');
        requestDraw(canvas, state);
      });
      for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        canvas.addEventListener(event, () => { state.pointer = null; });
      }
      canvas.addEventListener('keydown', event => {
        if (!['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Home') { setView(canvas, 'oblique'); return; }
        if (event.key === 'ArrowLeft') state.yaw -= .15;
        else if (event.key === 'ArrowRight') state.yaw += .15;
        else state.pitch = clamp(state.pitch + (event.key === 'ArrowUp' ? .15 : -.15), -1.4, 1.4);
        syncView(canvas, '');
        requestDraw(canvas, state);
      });
      state.onResize = () => requestDraw(canvas, state);
    }
    const state = canvas._nsMap;
    state.series = series;
    if (!state.observing) {
      state.observing = true;
      if (typeof root.ResizeObserver === 'function') {
        state.resize = new root.ResizeObserver(state.onResize);
        state.resize.observe(canvas);
      }
      root.addEventListener('resize', state.onResize);
    }
    // Draw the montage immediately, independently of data and network availability.
    // ResizeObserver retries when a panel mounted while hidden becomes visible.
    draw(canvas, state);
  }

  function unmount(canvas) {
    const state = canvas?._nsMap;
    if (!state) return;
    state.resize?.disconnect();
    state.resize = null;
    root.removeEventListener('resize', state.onResize);
    state.observing = false;
    if (state.frame !== null) root.cancelAnimationFrame(state.frame);
    state.frame = null;
    state.pointer = null;
  }

  function draw(canvas, state) {
    const rect = canvas.getBoundingClientRect();
    const width = Math.round(rect.width), height = Math.round(rect.height);
    if (!width || !height) return;
    const dpr = Math.min(2, root.devicePixelRatio || 1);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    }
    const g = canvas.getContext('2d');
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    const cx = width / 2, cy = height * .37;
    const radius = Math.min(height * .34, width * .32);
    const project = point => {
      const [x, y, z] = rotate(point, state.yaw, state.pitch);
      return {x: cx + x * radius, y: cy - y * radius, z};
    };
    const path = points => {
      g.beginPath();
      points.forEach((p, i) => i ? g.lineTo(p.x, p.y) : g.moveTo(p.x, p.y));
    };
    const line = (points, color, thickness, front = null) => {
      if (points.length < 2) return;
      g.strokeStyle = color; g.lineWidth = thickness;
      g.beginPath();
      for (let i = 1; i < points.length; i++) {
        const a = points[i - 1], b = points[i];
        if (front !== null && ((a.z + b.z) / 2 >= 0) !== front) continue;
        g.moveTo(a.x, a.y); g.lineTo(b.x, b.y);
      }
      g.stroke();
    };

    // A quiet reference plane keeps the form grounded without suggesting activity.
    const halo = g.createRadialGradient(cx, cy, radius * .25, cx, cy, radius * 1.5);
    halo.addColorStop(0, '#73adc214'); halo.addColorStop(1, '#73adc200');
    g.fillStyle = halo; g.fillRect(0, 0, width, height);
    g.strokeStyle = '#6e9eb61c'; g.lineWidth = 1;
    g.beginPath(); g.ellipse(cx, cy + radius * 1.15, radius * .88, radius * .16, 0, 0, tau); g.stroke();
    g.setLineDash([2, 6]);
    g.beginPath(); g.moveTo(cx, 22); g.lineTo(cx, height - 120); g.stroke();
    g.setLineDash([]);
    for (const curve of mesh) line(curve.map(project), '#83b0c016', .65, false);

    const rotated = (model?.vertices || []).map(point => rotate(point, state.yaw, state.pitch));
    const surface = triangles.map(face => ({...face,
      normal:rotate(face.normal, state.yaw, state.pitch),
      depth:face.indices.reduce((sum,i)=>sum+rotated[i][2],0)/3,
    })).sort((a,b)=>a.depth-b.depth);
    for (const face of surface) {
      const light = Math.max(0, face.normal[0]*-.35 + face.normal[1]*.45 + face.normal[2]*.82);
      const sulcus = clamp((face.sulc + .8)/2, 0, 1);
      const shade = 46 + light * 65 - sulcus * 20;
      g.fillStyle = `rgb(${Math.round(shade*.85)},${Math.round(shade*1.1)},${Math.round(shade*1.27)})`;
      path(face.indices.map(i=>({x:cx+rotated[i][0]*radius,y:cy-rotated[i][1]*radius})));
      g.closePath(); g.fill();
      g.strokeStyle=g.fillStyle;g.lineWidth=.5;g.stroke();
    }
    g.lineCap = 'round'; g.lineJoin = 'round';
    for (const curve of mesh) line(curve.map(project), '#9dc5d51c', .65, true);

    // Ears and nasion establish anatomical orientation, including in the side view.
    for (const side of [-1, 1]) {
      const ear = Array.from({length: 33}, (_, i) => [side * 1.015, -.12 + .19 * Math.cos(i*tau/32), -.08 + .105 * Math.sin(i*tau/32)]);
      line(ear.map(project), '#829dac75', 1.2);
    }
    line([[0,.18,-1.01], [0,.04,-1.2], [0,-.1,-1.01]].map(project), '#829dac85', 1.2);

    // Leads show measured pairs; small charts make even 1–5% powers readable.
    for (const lead of leads) {
      const points=leadCurve(lead,0).map(project);
      g.setLineDash([3,4]);line(points,lead.color+'40',1.5,false);
      g.setLineDash([]);line(points,lead.color+'b0',2,true);
    }
    for (let index=0;index<2;index++) {
      const w=126,h=106,x=index ? width-w-14 : 14,y=height-h-10;
      g.fillStyle='#0b1826e6';g.strokeStyle='#526c8055';g.lineWidth=1;
      g.beginPath();g.roundRect(x,y,w,h,10);g.fill();g.stroke();
      g.textAlign='left';g.textBaseline='middle';g.fillStyle=leads[index].color;
      g.font='600 10px system-ui,sans-serif';g.fillText(index?'O2–T4 · %':'O1–T3 · %',x+10,y+15);
      const series=state.series;
      const valid=series.some(b=>hasPower(b.powers?.[index]));
      if (!valid) {
        g.fillStyle='#94a8bc';g.font='10px system-ui,sans-serif';g.fillText('Нет пригодных окон',x+10,y+56);
        continue;
      }
      const step=(w-20)/Math.max(1,series.length);
      series.forEach((band,i)=>{
        const value=band.powers?.[index],bx=x+10+step*(i+.5),base=y+h-23;
        g.fillStyle='#33475b';g.fillRect(bx-4,y+38,8,45);
        if(hasPower(value)) {
          g.fillStyle=band.color;g.fillRect(bx-4,base-clamp(value,0,1)*45,8,clamp(value,0,1)*45);
        }
        g.textAlign='center';g.font='9px system-ui,sans-serif';g.fillStyle='#d4e1ec';
        g.fillText(hasPower(value)?series.length > 1 ? Math.round(value*100).toString() : (value*100).toFixed(1)+'%':'—',bx,y+29);
        g.fillStyle=band.color;g.font='11px system-ui,sans-serif';g.fillText(band.name,bx,base+13);
      });
    }

    // Keep all four measured positions readable; far-side markers use dashed rings.
    g.font = '600 12px system-ui, sans-serif'; g.textBaseline = 'middle';
    const electrodes = leads.flatMap(lead => [lead.temporal, lead.occipital].map(e => ({...e, color:lead.color, p:project(onSphere(e.point))})));
    electrodes.sort((a,b) => a.p.z - b.p.z);
    for (const e of electrodes) {
      const p = e.p, far = p.z < -.12;
      g.globalAlpha = far ? .55 : 1;
      g.fillStyle = e.color + '16'; g.strokeStyle = e.color + '75'; g.lineWidth = 1;
      g.setLineDash(far ? [2, 3] : []);
      g.beginPath(); g.arc(p.x, p.y, 10, 0, tau); g.fill(); g.stroke();
      g.setLineDash([]);
      g.fillStyle = e.color; g.beginPath(); g.arc(p.x, p.y, 4.3, 0, tau); g.fill();
      const direction = e.point[0] < 0 ? -1 : 1;
      const lx = clamp(p.x + direction * (e.name[0] === 'O' ? 29 : 37), 26, width - 26);
      const ly = clamp(p.y + (e.name[0] === 'O' ? -32 : 25), 16, height - 16);
      line([p,{x:lx,y:ly}], e.color + '70', .9);
      g.fillStyle = '#101d2c'; g.strokeStyle = e.color + '65';
      g.beginPath(); g.roundRect(lx - 19, ly - 12, 38, 24, 7); g.fill(); g.stroke();
      g.fillStyle = e.color; g.textAlign = 'center'; g.fillText(e.name, lx, ly + .5);
      g.globalAlpha = 1;
    }
  }

  root.NeuroBrainMap = {mount, unmount, setView};
})(typeof window !== 'undefined' ? window : globalThis);
