/* Rotate a 3D schematic of the measured electrode montage. */
(function (root) {
  'use strict';

  const leads = [
    {color: '#74e5cf', temporal: {name: 'T3', point: [-.95, -.15, .16]},
      occipital: {name: 'O1', point: [-.31, .25, -.92]}},
    {color: '#9ba9ff', temporal: {name: 'T4', point: [.95, -.15, .16]},
      occipital: {name: 'O2', point: [.31, .25, -.92]}},
  ];
  const initialPitch = -.28;

  function rotate([x, y, z], yaw, pitch) {
    const X = x * Math.cos(yaw) + z * Math.sin(yaw);
    const Z = z * Math.cos(yaw) - x * Math.sin(yaw);
    return [X, y * Math.cos(pitch) - Z * Math.sin(pitch), y * Math.sin(pitch) + Z * Math.cos(pitch)];
  }

  function headPoint(latitude, longitude) {
    const y = Math.sin(latitude);
    const ring = Math.cos(latitude) * (1 - Math.max(0, -y) * .15);
    return [ring * Math.cos(longitude), y * 1.08, ring * Math.sin(longitude)];
  }

  const mesh = [];
  const radians = Math.PI / 180;
  for (let lat = -70; lat <= 70; lat += 20) {
    const curve = [];
    for (let lon = 0; lon <= 360; lon += 6) curve.push(headPoint(lat * radians, lon * radians));
    mesh.push(curve);
  }
  for (let lon = 0; lon < 360; lon += 30) {
    const curve = [];
    for (let lat = -90; lat <= 90; lat += 6) curve.push(headPoint(lat * radians, lon * radians));
    mesh.push(curve);
  }

  function onSphere(point) {
    const length = Math.hypot(...point);
    return point.map(value => value / length);
  }

  function leadCurve(lead, offset) {
    const from = onSphere(lead.temporal.point);
    const to = onSphere(lead.occipital.point);
    const dot = from.reduce((total, value, axis) => total + value * to[axis], 0);
    const angle = Math.acos(Math.max(-1, Math.min(1, dot)));
    const scale = Math.sin(angle);
    const points = [];
    for (let i = 0; i <= 32; i++) {
      const t = i / 32;
      const a = Math.sin((1 - t) * angle) / scale;
      const b = Math.sin(t * angle) / scale;
      const point = from.map((value, axis) => a * value + b * to[axis]);
      point[1] += offset;
      points.push(point);
    }
    return points;
  }

  function visibleFraction(points, fraction) {
    if (fraction >= 1) return points;
    // Use projected distance so, for example, 50% fills half the visible track at any angle.
    const lengths = points.slice(1).map((point, index) => {
      const start = points[index];
      return Math.hypot(point.x - start.x, point.y - start.y);
    });
    let remaining = lengths.reduce((total, length) => total + length, 0) * fraction;
    const visible = [points[0]];
    for (const [index, length] of lengths.entries()) {
      const start = points[index];
      const end = points[index + 1];
      if (remaining >= length) {
        visible.push(end);
        remaining -= length;
      } else {
        const part = remaining / length;
        visible.push({x: start.x + (end.x - start.x) * part, y: start.y + (end.y - start.y) * part});
        break;
      }
    }
    return visible;
  }

  function mount(canvas, series) {
    if (!canvas?.getContext) return;
    if (!canvas._nsMap) {
      const state = {yaw: 0, pitch: initialPitch, series: [], pointer: null};
      canvas._nsMap = state;
      canvas.tabIndex = 0;
      canvas.style.touchAction = 'none';
      canvas.addEventListener('pointerdown', event => {
        state.pointer = [event.clientX, event.clientY];
        canvas.setPointerCapture?.(event.pointerId);
      });
      canvas.addEventListener('pointermove', event => {
        if (!state.pointer) return;
        state.yaw += (event.clientX - state.pointer[0]) * .009;
        state.pitch = Math.max(-1.3, Math.min(1.3, state.pitch + (event.clientY - state.pointer[1]) * .006));
        state.pointer = [event.clientX, event.clientY];
        draw(canvas, state);
      });
      for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        canvas.addEventListener(event, () => { state.pointer = null; });
      }
      canvas.addEventListener('keydown', event => {
        if (!['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Home') {
          state.yaw = 0;
          state.pitch = initialPitch;
        } else if (event.key === 'ArrowLeft') state.yaw -= .15;
        else if (event.key === 'ArrowRight') state.yaw += .15;
        else if (event.key === 'ArrowUp') state.pitch = Math.max(-1.3, state.pitch - .15);
        else state.pitch = Math.min(1.3, state.pitch + .15);
        draw(canvas, state);
      });
    }

    const state = canvas._nsMap;
    state.series = series;
    if (!state.resize && typeof ResizeObserver !== 'undefined') {
      state.resize = new ResizeObserver(() => draw(canvas, state));
      state.resize.observe(canvas);
    }
    draw(canvas, state);
  }

  function unmount(canvas) {
    const state = canvas?._nsMap;
    state?.resize?.disconnect();
    if (state) state.resize = null;
  }

  function draw(canvas, state) {
    const rect = canvas.getBoundingClientRect();
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);
    if (!width || !height) return;
    const dpr = Math.min(2, root.devicePixelRatio || 1);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const g = canvas.getContext('2d');
    if (!g) return;

    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, width, height);
    const cx = width * .5;
    const cy = height * .51;
    const radius = Math.min(height * .414, width * .39);
    const project = point => {
      const [x, y] = rotate(point, state.yaw, state.pitch);
      return {x: cx + x * radius, y: cy - y * radius};
    };
    const line = (points, color, thickness) => {
      g.beginPath();
      g.strokeStyle = color;
      g.lineWidth = thickness;
      points.forEach((point, index) => index ? g.lineTo(point.x, point.y) : g.moveTo(point.x, point.y));
      g.stroke();
    };

    const fill = g.createRadialGradient(cx - radius * .37, cy - radius * .45, radius * .06,
                                        cx, cy, radius * 1.1);
    fill.addColorStop(0, '#2a3e55');
    fill.addColorStop(.63, '#16263a');
    fill.addColorStop(1, '#101c2d');
    g.fillStyle = fill;
    g.strokeStyle = '#39536d';
    g.lineWidth = 1.3;
    g.beginPath();
    g.ellipse(cx, cy, radius, radius * 1.08, 0, 0, Math.PI * 2);
    g.fill();
    g.stroke();
    for (const curve of mesh) line(curve.map(project), 'rgba(119,165,203,.15)', .8);

    const allBands = state.series.length > 1;
    const trackWidth = allBands ? 3 : 7;
    const laneSpacing = .058;
    g.lineCap = 'round';
    leads.forEach((lead, index) => {
      const hasPower = state.series.some(band => band.powers[index] !== null);
      if (!hasPower) {
        line(leadCurve(lead, 0).map(project), '#516178', 2);
        return;
      }
      const tracks = state.series.flatMap((band, lane) => {
        const power = band.powers[index];
        if (power === null) return [];
        const offset = (lane - (state.series.length - 1) / 2) * laneSpacing;
        return [{points: leadCurve(lead, offset).map(project), power, color: band.color}];
      });
      for (const track of tracks) line(track.points, '#42546b', trackWidth);
      for (const track of tracks) {
        if (track.power > 0) line(visibleFraction(track.points, track.power), track.color, trackWidth);
      }
    });

    g.font = '650 12px system-ui, sans-serif';
    g.textBaseline = 'middle';
    leads.forEach((lead, index) => {
      const active = state.series.some(band => band.powers[index] !== null);
      for (const electrode of [lead.temporal, lead.occipital]) {
        const point = project(onSphere(electrode.point));
        g.fillStyle = active ? lead.color : '#9aaac0';
        g.beginPath();
        g.arc(point.x, point.y, 5, 0, 2 * Math.PI);
        g.fill();
        g.strokeStyle = '#08121f';
        g.lineWidth = 2;
        g.stroke();
        const outward = point.x >= cx;
        const labelWidth = g.measureText(electrode.name).width;
        const x = outward ? Math.min(point.x + 9, width - labelWidth - 6)
                          : Math.max(6, point.x - labelWidth - 9);
        g.strokeStyle = '#0e1827';
        g.lineWidth = 3;
        g.strokeText(electrode.name, x, point.y - 8);
        g.fillText(electrode.name, x, point.y - 8);
      }
    });
  }

  root.NeuroBrainMap = {mount, unmount};
})(typeof window !== 'undefined' ? window : globalThis);
