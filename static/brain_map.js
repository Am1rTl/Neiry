/* Interactive 3D electrode-montage sketch. No MRI, cortical heatmap or source localisation. */
(function(root) {
  'use strict';
  const bandNames={delta:'δ · 1–4 Гц',theta:'θ · 4–8 Гц',alpha:'α · 8–13 Гц',
    beta:'β · 13–30 Гц',low_gamma:'низкая γ · 30–45 Гц'};
  const markers=[
    {name:'O1',ch:0,p:[-.31,.25,-.96]}, {name:'T3',ch:0,p:[-.95,-.15,.16]},
    {name:'O2',ch:1,p:[.31,.25,-.96]},  {name:'T4',ch:1,p:[.95,-.15,.16]},
  ];
  const palettes=['#74e5cf','#9ba9ff'];
  function finite(v){return typeof v==='number'&&Number.isFinite(v)?v:null;}
  function rotate(p,yaw,pitch){
    const [x,y,z]=p;
    const X=x*Math.cos(yaw)+z*Math.sin(yaw), Z=z*Math.cos(yaw)-x*Math.sin(yaw);
    return [X,y*Math.cos(pitch)-Z*Math.sin(pitch), y*Math.sin(pitch)+Z*Math.cos(pitch)];
  }
  function mount(canvas,data,band='alpha'){
    if(!canvas?.getContext)return;
    if(!canvas._nsMap){
      const state={yaw:-.48,pitch:-.28,band:'alpha',data:null,down:false,startX:0,startY:0};
      canvas._nsMap=state;canvas.tabIndex=0;
      canvas.style.touchAction='none';
      canvas.addEventListener('pointerdown',e=>{state.down=true;state.startX=e.clientX;state.startY=e.clientY;canvas.setPointerCapture?.(e.pointerId);});
      canvas.addEventListener('pointermove',e=>{
        if(!state.down)return;
        state.yaw+=(e.clientX-state.startX)*.009;
        state.pitch=Math.max(-1.3,Math.min(1.3,state.pitch+(e.clientY-state.startY)*.006));
        state.startX=e.clientX;state.startY=e.clientY;draw(canvas,state);
      });
      for(const event of ['pointerup','pointercancel'])canvas.addEventListener(event,()=>state.down=false);
      canvas.addEventListener('keydown',e=>{
        if(!['ArrowRight','ArrowLeft','ArrowUp','ArrowDown','Home'].includes(e.key))return;
        e.preventDefault();
        if(e.key==='Home'){state.yaw=-.48;state.pitch=-.28;}
        else if(e.key==='ArrowLeft')state.yaw-=.15;
        else if(e.key==='ArrowRight')state.yaw+=.15;
        else if(e.key==='ArrowUp')state.pitch=Math.max(-1.3,state.pitch-.15);
        else if(e.key==='ArrowDown')state.pitch=Math.min(1.3,state.pitch+.15);
        draw(canvas,state);
      });
      if(typeof ResizeObserver!=='undefined'){
        state.resize=new ResizeObserver(()=>draw(canvas,state));state.resize.observe(canvas);
      }
    }
    canvas._nsMap.band=band;
    canvas._nsMap.data=data;
    draw(canvas,canvas._nsMap);
  }
  function draw(canvas,state){
    const rect=canvas.getBoundingClientRect();
    const width=Math.max(280,Math.round(rect.width||560));
    const height=Math.max(230,Math.round(rect.height||300));
    const dpr=Math.min(2,root.devicePixelRatio||1);
    if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){
      canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
    }
    const g=canvas.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
    g.clearRect(0,0,width,height);
    const cx=width*.5,cy=height*.52,r=Math.min(height*.4,width*.28);
    const proj=p=>{
      const q=rotate(p,state.yaw,state.pitch);
      return {x:cx+q[0]*r,y:cy-q[1]*r*.98,z:q[2]};
    };
    function line(points,color,stroke=1){
      g.strokeStyle=color;g.lineWidth=stroke;g.beginPath();
      points.forEach((p,i)=>i?g.lineTo(p.x,p.y):g.moveTo(p.x,p.y));g.stroke();
    }
    const mesh=[];
    // Latitude/longitude mesh projected from a three-dimensional spheroid.
    for(let lat=-70;lat<=70;lat+=20){
      const curve=[];
      for(let lon=0;lon<=360;lon+=6){
        const la=lat*Math.PI/180,lo=lon*Math.PI/180;
        curve.push(proj([Math.cos(la)*Math.cos(lo),Math.sin(la)*1.05,Math.cos(la)*Math.sin(lo)]));
      }
      mesh.push(curve);
    }
    for(let lon=0;lon<180;lon+=22.5){
      const curve=[];
      for(let lat=-90;lat<=90;lat+=5){
        const la=lat*Math.PI/180,lo=lon*Math.PI/180;
        curve.push(proj([Math.cos(la)*Math.cos(lo),Math.sin(la)*1.05,Math.cos(la)*Math.sin(lo)]));
      }
      mesh.push(curve);
    }
    g.save();
    const fill=g.createRadialGradient(cx-r*.35,cy-r*.55,r*.1,cx,cy,r*1.12);
    fill.addColorStop(0,'rgba(73,109,144,.18)');fill.addColorStop(1,'rgba(28,48,77,.35)');
    g.fillStyle=fill;g.strokeStyle='rgba(121,171,209,.35)';g.lineWidth=1.3;
    g.beginPath();g.ellipse(cx,cy,r,r*1.05,0,0,Math.PI*2);g.fill();g.stroke();
    for(const points of mesh)line(points,'rgba(113,169,208,.12)',.8);
    g.restore();
    const band=state.data?.bands?.[state.band],available=state.data?.available&&band;
    const powers=available?[finite(band.left_relative_power),finite(band.right_relative_power)]:[null,null];
    for(let ch=0;ch<2;ch++){
      const points=markers.filter(m=>m.ch===ch).map(m=>proj(m.p));
      const active=powers[ch]!==null;
      g.shadowColor=active?palettes[ch]:'transparent';g.shadowBlur=active?10:0;
      line(points,active?palettes[ch]:'rgba(143,162,187,.35)',active?3:1.6);g.shadowBlur=0;
    }
    // Labels refer ONLY to electrode positions, never to active cortical regions.
    for(const marker of markers){
      const q=proj(marker.p),hasValue=powers[marker.ch]!==null;
      g.fillStyle=hasValue?palettes[marker.ch]:'#71839e';
      g.beginPath();g.arc(q.x,q.y,6.5,0,2*Math.PI);g.fill();
      g.strokeStyle='#08121f';g.lineWidth=2;g.stroke();
      g.font='600 12px system-ui, sans-serif';
      g.fillText(marker.name,q.x+8,q.y-7);
    }
    g.font='11px system-ui, sans-serif';g.fillStyle='#9daec4';
    g.fillText('Поворот: перетащите · стрелки · Home',12,height-13);
    const parent=canvas.closest('.pair-map-widget');
    if(parent){
      const out=parent.querySelector('[data-map-caption]');
      if(out)out.textContent=available?
        `${bandNames[state.band]||state.band}. O1–T3: ${(powers[0]*100).toFixed(1)}%; O2–T4: ${(powers[1]*100).toFixed(1)}%. Подсвечены только линии отведений, НЕ области коры.`:
        'Нет проверенного парного спектра. Электроды показаны условно, без подсветки активности.';
    }
  }
  root.NeuroBrainMap={mount,bandNames};
})(typeof window!=='undefined'?window:globalThis);
