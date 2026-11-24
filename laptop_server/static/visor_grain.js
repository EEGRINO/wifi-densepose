'use strict';

const VisorGrain = (() => {
 const width=422, height=236;
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

 function displayExposure(amplitude) {
  const occupied=amplitude.filter(value=>value>0).sort();
  if (!occupied.length) return 0;
  const reference=occupied[Math.floor((occupied.length-1)*.85)];
  return clamp(.56/reference,1,96);
 }

 function randomness(seed) {
  let bits=seed||1;
  return ()=>{
   bits^=bits<<13;bits^=bits>>>17;bits^=bits<<5;
   return (bits>>>0)/4294967296;
  };
 }

 function grain() {
  let signature=null, previousGain=0, amplitude=null, seed=1, exposure=0;
  let texture, pixels, textureContext;

  function setPacket(packet,gain) {
   const key=packet?.key??null;
   if (key===signature&&gain===previousGain) return;
   if (key!==signature) {
    signature=key;
    if (packet) {
     ({amplitude,seed}=values(packet.samples));
     exposure=displayExposure(amplitude);
    } else exposure=0;
   }
   previousGain=gain;
   if (!exposure) return;
   if (!texture) {
    texture=document.createElement('canvas');texture.width=width;texture.height=height;
    textureContext=texture.getContext('2d');
    pixels=textureContext.createImageData(width,height);
   }
   const random=randomness(seed), rgba=pixels.data;
   for (let y=0;y<height;y++)for (let x=0;x<width;x++) {
    const offset=(y*width+x)*4;
    const sample=clamp(amplitude[(Math.floor(x/5)+3*Math.floor(y/11))%amplitude.length]*exposure,0,1);
    const speck=random();
    const value=gain*sample*(3+44*speck**4+(speck>.962?55:0));
    rgba[offset]=value*.71;rgba[offset+1]=value*.87;rgba[offset+2]=value;rgba[offset+3]=255;
   }
   textureContext.putImageData(pixels,0,0);
  }

  function drawPerson(ctx,points,scale) {
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

  return function draw(ctx,w,h,packet,pose,gain,alpha=1) {
   setPacket(packet,gain);
   ctx.save();
   ctx.globalAlpha=alpha;
   ctx.fillStyle='#000';ctx.fillRect(0,0,w,h);
   const drawn=Boolean(exposure&&pose&&alpha>.98);
   if (exposure) {
    ctx.imageSmoothingEnabled=false;
    ctx.drawImage(texture,0,0,w,h);
    if (drawn) drawPerson(ctx,pose.points,pose.scale);
   }
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

 return {decode,values,displayExposure,grain,projectPose};
})();
