'use strict';

const VisorGrain = (() => {
 const trailDuration=2500, maxFrameGap=450;
 const useful=Array.from({length:52},(_,i)=>i<26?i+1:i+12);
 const clamp=(value,min,max)=>Math.min(max,Math.max(min,value));

 function decode(encoded) {
  if (typeof encoded!=='string'||encoded.length>172) return null;
  let wire;
  try {wire=atob(encoded);} catch (_) {return null;}
  if (!wire.length||wire.length>128||wire.length%2) return null;
  const samples=new Int8Array(wire.length);
  for (let i=0;i<samples.length;i++) samples[i]=wire.charCodeAt(i);
  return samples;
 }

 function values(samples) {
  const amplitude=new Float32Array(samples.length/2);
  let seed=2166136261;
  for (let i=0;i<samples.length;i+=2) {
   amplitude[i/2]=Math.hypot(samples[i],samples[i+1])/181.02;
   seed=Math.imul(seed^samples[i],16777619);
   seed=Math.imul(seed^samples[i+1],16777619);
  }
  return {amplitude,seed};
 }

 function normalizedIQ(samples) {
  const indexes=useful.filter(index=>2*index+1<samples.length);
  if (!indexes.length) return null;
  const carriers=indexes.map(index=>[samples[2*index+1],samples[2*index]]);
  const rms=Math.sqrt(carriers.reduce((sum,[i,q])=>sum+i*i+q*q,0)/carriers.length);
  return rms?carriers.map(([i,q])=>[i/rms,q/rms]):null;
 }

 function alignPhase(frame,reference) {
  let real=0,imag=0;
  for (let i=0;i<frame.length;i++) {
   const [x,y]=frame[i], [rx,ry]=reference[i];
   real+=x*rx+y*ry;
   imag+=y*rx-x*ry;
  }
  const strength=Math.hypot(real,imag);
  if (strength<frame.length*.25) return null;
  const cos=real/strength,sin=imag/strength;
  return frame.map(([i,q])=>[i*cos+q*sin,q*cos-i*sin]);
 }

 function history() {
  let line='', key=null, previousTime=0, previousSequence, reference=null, previous=null;
  let recorded=[];

  return {
   update(packet) {
    if (!packet||packet.key===key) return;
    const samples=normalizedIQ(packet.samples);
    if (!samples) return;
    const nextLine=JSON.stringify([packet.receiver,packet.source,packet.channel,packet.peer,packet.samples.length]);
    if (line!==nextLine) recorded=[];
    const elapsed=packet.receivedAt-previousTime;
    const sequenceReset=Number.isInteger(packet.sequence)&&Number.isInteger(previousSequence)&&
     ((packet.sequence-previousSequence)>>>0)>0x80000000;
    const continuous=line===nextLine&&elapsed>0&&elapsed<=maxFrameGap&&!sequenceReset;
    const aligned=continuous&&alignPhase(samples,reference);
    line=nextLine;key=packet.key;previousTime=packet.receivedAt;previousSequence=packet.sequence;
    if (!aligned) {
     reference=samples;
     previous=samples.map(()=>[0,0]);
     return;
    }
    const backgroundBlend=1-Math.exp(-elapsed/2700);
    const signalBlend=1-Math.exp(-elapsed/135);
    const segments=[];
    for (let i=0;i<aligned.length;i++) {
     const start=previous[i], end=[0,0];
     for (let axis=0;axis<2;axis++) {
      const delta=aligned[i][axis]-reference[i][axis];
      reference[i][axis]+=delta*backgroundBlend;
      end[axis]=start[axis]+(delta-start[axis])*signalBlend;
     }
     const shift=Math.hypot(end[0]-start[0],end[1]-start[1]);
     if (shift>=.03) segments.push({start,end,shift});
     previous[i]=end;
    }
    if (segments.length) {
     segments.sort((a,b)=>b.shift-a.shift);
     recorded.push({at:packet.receivedAt,segments:segments.slice(0,14)});
     if (recorded.length>48) recorded.shift();
    }
   },
   visible(now) {
    recorded=recorded.filter(frame=>now-frame.at<trailDuration);
    return recorded.map(frame=>({...frame,heat:clamp(1-(now-frame.at)/trailDuration,0,1)}));
   },
  };
 }

 function randomness(seed) {
  let bits=seed||1;
  return ()=>{
   bits^=bits<<13;bits^=bits>>>17;bits^=bits<<5;
   return (bits>>>0)/4294967296;
  };
 }

 function drawPerson(ctx,points,scale,samples) {
   const {amplitude,seed}=values(samples);
   const rnd=randomness(seed^0x31ac58d7);
   const signal=amplitude.reduce((sum,v)=>sum+v,0)/amplitude.length;
   const opacity=.46+clamp(signal,0,.75)*.54;
   const size=clamp(scale/195,1.05,2.8);

   function dot(x,y,alpha) {
    ctx.globalAlpha=alpha;
    const r=size*(.48+rnd()*.78);
    ctx.fillRect(x,y,r,r);
   }

   function segment(a,b,radius,density) {
    for (let i=0;i<density;i++) {
     const t=rnd(), angle=rnd()*Math.PI*2;
     const r=Math.sqrt(rnd())*radius*(.65+.35*Math.sin(t*Math.PI));
     dot(a[0]+(b[0]-a[0])*t+Math.cos(angle)*r,a[1]+(b[1]-a[1])*t+Math.sin(angle)*r,
      opacity*(1-r/(radius*1.32))*(.15+.65*rnd()));
    }
   }

   ctx.fillStyle='#a2bfd5';
   // Fill the gap between the measured shoulders and hips with irregular grains.
   const [right,left,hipR,hipL]=[points[2],points[5],points[8],points[11]];
   for (let i=0;i<1500;i++) {
    const t=rnd(), across=rnd();
    const a=[right[0]+(hipR[0]-right[0])*t,right[1]+(hipR[1]-right[1])*t];
    const b=[left[0]+(hipL[0]-left[0])*t,left[1]+(hipL[1]-left[1])*t];
    const jitter=scale*.028;
    dot(a[0]+(b[0]-a[0])*across+(rnd()-.5)*jitter,a[1]+(b[1]-a[1])*across+(rnd()-.5)*jitter,
     opacity*(.12+.5*rnd()));
   }
   for (const [a,b,radius] of [[2,3,.043],[3,4,.033],[5,6,.043],[6,7,.033],
    [8,9,.061],[9,10,.042],[11,12,.061],[12,13,.042],[1,14,.054],[14,15,.05]]) {
    segment(points[a],points[b],scale*radius,235);
   }
   const head=points[0],neck=points[1], radius=scale*.064;
   segment([head[0]-.18*(neck[0]-head[0]), head[1]-.18*(neck[1]-head[1])],neck,radius,380);
 }

 function iqPlot() {
  const track=history();
  const palette=['#3568a0','#538bcd','#60bfe4','#b8e9ed','#ffcc83','#fff0bc'];
  return function draw(ctx,w,h,packet,pose,gain,now,alpha=1) {
   track.update(packet);
   const frames=track.visible(now);
   const x=w/2,y=h/2,radius=Math.min(w,h)*.39;
   function point([i,q]) {
    const scale=radius/(.50+Math.hypot(i,q));
    return [x+i*scale,y-q*scale];
   }
   ctx.save();
   ctx.globalAlpha=alpha;
   ctx.fillStyle='#080e17';ctx.fillRect(0,0,w,h);
   ctx.strokeStyle='#232e42';ctx.lineWidth=1;
   for (const ratio of [.32,.65,1]) {
    ctx.beginPath();ctx.arc(x,y,ratio*radius,0,Math.PI*2);ctx.stroke();
   }
   ctx.strokeStyle='#34445a';
   ctx.beginPath();
   ctx.moveTo(x-radius,y);ctx.lineTo(x+radius,y);
   ctx.moveTo(x,y-radius);ctx.lineTo(x,y+radius);
   ctx.stroke();
   ctx.fillStyle='#a1b5cb';ctx.font='12px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';
   ctx.fillText('ΔI',x+radius+15,y);
   ctx.fillText('ΔQ',x,y-radius-12);
   ctx.lineCap='round';
   for (const {heat,segments} of frames) {
    const color=palette[Math.floor(heat*(palette.length-.01))];
    ctx.strokeStyle=color;
    ctx.globalAlpha=alpha*clamp(gain*heat**1.6*.74,0,1);
    ctx.lineWidth=1+heat*1.3;
    ctx.beginPath();
    for (const {start,end} of segments) {
     ctx.moveTo(...point(start));ctx.lineTo(...point(end));
    }
    ctx.stroke();
    if (heat>.94) {
     ctx.fillStyle=color;
     for (const {end} of segments) {
      ctx.beginPath();ctx.arc(...point(end),2,0,Math.PI*2);ctx.fill();
     }
    }
   }
   const drawn=Boolean(packet?.hasSignal&&pose&&alpha>.98);
   if (drawn) drawPerson(ctx,pose.points,pose.scale,packet.samples);
   ctx.restore();
   return drawn;
  };
 }

 function projectPose(project,position,joints,w,h) {
  if (!position||!joints) return null;
  const [x,z]=position;
  const floor=project([x,0,z]), top=project([x,1.75,z]);
  if (!floor||!top) return null;
  const scale=Math.min(Math.hypot(floor[0]-top[0],floor[1]-top[1]),h*1.35);
  if (scale<8||floor[0]<-scale||floor[0]>w+scale||floor[1]<0||floor[1]>h+scale) return null;
  const bottom=Math.max(...joints.map(p=>p[1])), centre=joints[15][0];
  const points=joints.map(([jx,jy])=>[floor[0]+(jx-centre)*scale*1.85,floor[1]+(jy-bottom)*scale*1.4]);
  return {points,scale};
 }

 return {decode,values,history,iqPlot,projectPose};
})();
