'use strict';

const RadioScene = (() => {
 const TAU = Math.PI * 2, NEAR = .08, FADE_MS = 2500;
 const colors = {router:'#c8afd7', esp1:'#73d7ff', esp2:'#ffce82'};
 const names = {router:'Роутер', esp1:'ESP A', esp2:'ESP B'};
 const bones = [[0,1],[1,2],[2,3],[3,4],[1,5],[5,6],[6,7],[1,14],[14,15],
  [15,8],[8,9],[9,10],[15,11],[11,12],[12,13]];
 const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
 const angle = v => ((v + Math.PI) % TAU + TAU) % TAU - Math.PI;
 const mix = (a, b, t) => a + (b-a)*t;
 const sameCell = (a, b) => a?.key === b?.key;
 const flat = ([x,z], y=0) => [x,y,z];

 function grid(room) {
  if (!room) return null;
  const [x0,z0,x1,z1] = RoomPlan.bounds(room);
  const width=x1-x0, depth=z1-z0;
  if (![x0,z0,x1,z1].every(Number.isFinite) || width<=0 || depth<=0) return null;
  const rectangle = (x,z,w,d) => [[x,z],[x+w,z],[x+w,z+d],[x,z+d]];
  let boundary = room.boundary;
  if (room.shape==='curved') {
   boundary = [[0,0],...Array.from({length:25},(_,i) => {
    const a=i*Math.PI/48;
    return [room.short_width+(width-room.short_width)*Math.cos(a),depth*Math.sin(a)];
   }),[0,depth]];
  } else if (room.shape!=='custom') {
   boundary=rectangle(x0,z0,width,depth);
  }
  const cutouts=room.cutouts??[];
  const rings=[boundary,...cutouts.map(([x,z,right,bottom])=>rectangle(x,z,right-x,bottom-z))];
  const size=Math.max(1,Math.ceil(Math.max(width,depth)/16));
  const cols=Math.ceil(width/size), rows=Math.ceil(depth/size);
  const cells=Array.from({length:rows*cols},(_,index)=>{
   const col=index%cols, row=Math.floor(index/cols);
   const x=x0+col*size, z=z0+row*size;
   return {key:index, label:String.fromCharCode(65+col)+(row+1), tone:(row+col)%2,
    polygon:rectangle(x,z,size,size), center:[(x+Math.min(x+size,x1))/2,(z+Math.min(z+size,z1))/2]};
  });
  function cellAt(point) {
   if (!Array.isArray(point) || point.length!==2 || !point.every(Number.isFinite)
       || !RoomPlan.contains({boundary,cutouts},...point)) return null;
   const col=clamp(Math.floor((point[0]-x0)/size),0,cols-1);
   const row=clamp(Math.floor((point[1]-z0)/size),0,rows-1);
   return cells[row*cols+col];
  }
  const labels=cells.filter(cell=>cellAt(cell.center));
  return {rings,cells,labels,size,cellAt,width,depth,center:[(x0+x1)/2,(z0+z1)/2]};
 }

 function trail() {
  let active=null;
  const fading=new Map();
  return {
   update(cell,now) {
    if (sameCell(active,cell)) return;
    if (active) fading.set(active.key,{cell:active,at:now});
    if (cell) fading.delete(cell.key);
    active=cell;
   },
   visible(now) {
    const result=[];
    for (const [key,{cell,at}] of fading) {
     const remaining=1-(now-at)/FADE_MS;
     if (remaining<=0) fading.delete(key);
     else result.push({cell,alpha:.65*remaining*remaining});
    }
    if (active) result.push({cell:active,alpha:1});
    return result;
   },
   clear() { active=null; fading.clear(); }
  };
 }

 function canvasContext(canvas) {
  const w=canvas.clientWidth,h=canvas.clientHeight;
  if (!w||!h) return null;
  const scale=Math.min(window.devicePixelRatio||1,2);
  const width=Math.round(w*scale),height=Math.round(h*scale);
  if (canvas.width!==width) canvas.width=width;
  if (canvas.height!==height) canvas.height=height;
  const ctx=canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(scale,0,0,scale,0,0);
  ctx.clearRect(0,0,w,h);
  return {ctx,w,h};
 }

 function projector({x,y,z,yaw,pitch},w,h) {
  const sin=Math.sin(yaw), cos=Math.cos(yaw), sp=Math.sin(pitch), cp=Math.cos(pitch);
  const focal=Math.min(w,h)*.8;
  const local=([px,py,pz])=>{
   const dx=px-x,dy=py-y,dz=pz-z, forward=sin*dx-cos*dz;
   return [cos*dx+sin*dz,sp*forward+cp*dy,cp*forward-sp*dy];
  };
  const screen=([a,b,d])=>[w/2+focal*a/d,h/2-focal*b/d];
  return {
   point(p) {
    const q=local(p);
    return q[2]>=NEAR?screen(q):null;
   },
   path(ctx,points,closed) {
    const src=points.map(local), clipped=[];
    if (!closed && src[0][2]>=NEAR) clipped.push(src[0]);
    for (let i=closed?0:1;i<src.length;i++) {
     const a=src[(i+src.length-1)%src.length],b=src[i];
     if ((a[2]>=NEAR)!==(b[2]>=NEAR)) {
      const t=(NEAR-a[2])/(b[2]-a[2]);
      clipped.push([mix(a[0],b[0],t),mix(a[1],b[1],t),NEAR]);
     }
     if (b[2]>=NEAR) clipped.push(b);
    }
    if (clipped.length<(closed?3:2)) return false;
    ctx.moveTo(...screen(clipped[0]));
    for (const p of clipped.slice(1)) ctx.lineTo(...screen(p));
    if (closed) ctx.closePath();
    return true;
   }
  };
 }

 function scene(canvas) {
  let layout=null, view='map', camera=null, from=null, moved=0, yaw=0, pitch=.18;
  let darkness=0, fromDarkness=0;
  const drawGrain=VisorGrain.grain();
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;

  function select(mode, devices, aim, now) {
   const node=devices[mode];
   if (mode!=='map'&&(!layout||!node)) return false;
   if (mode===view) return true;
   from=reducedMotion?null:camera;
   fromDarkness=darkness;
   moved=now;
   view=mode;
   if (node) {
    const [x,z]=node.position;
    const target=aim&&Math.hypot(aim[0]-x,aim[1]-z)> .3?aim:layout.center;
    yaw=Math.atan2(target[0]-x,z-target[1]);
    pitch=.18;
   }
   return true;
  }

  function draw(now,devices,trail,person,radio,joints,gain) {
   const surface=canvasContext(canvas);
   if (!surface||!layout) return;
   const {ctx,w,h}=surface, node=devices[view];
   const [cx,cz]=layout.center;
   const focal=Math.min(w,h)*.8;
   const top={x:cx,y:Math.max(focal*layout.width/(w*.78),focal*layout.depth/(h*.78)),z:cz,yaw:0,pitch:Math.PI/2};
   const goal=node?{x:node.position[0],y:Math.max(.15,node.height??1.15),z:node.position[1],yaw,pitch}:top;
   const t=clamp((now-moved)/800,0,1), ease=t*t*(3-2*t);
   camera=from&&t<1?{x:mix(from.x,goal.x,ease),y:mix(from.y,goal.y,ease),z:mix(from.z,goal.z,ease),
    yaw:from.yaw+angle(goal.yaw-from.yaw)*ease,pitch:mix(from.pitch,goal.pitch,ease)}:goal;
   const projection=projector(camera,w,h);
   const goalDarkness=node?1:0;
   darkness=from&&t<1?mix(fromDarkness,goalDarkness,ease):goalDarkness;
   if (darkness===1) {
    const pose=VisorGrain.projectPose(projection.point,person,joints,w,h);
    drawGrain(ctx,w,h,radio,pose,gain);
    return;
   }
   const polygon=(points,y=0)=>projection.path(ctx,points.map(p=>flat(p,y)),true);

   ctx.beginPath();
   for (const ring of layout.rings) polygon(ring);
   ctx.fillStyle='#142638';
   ctx.fill('evenodd');
   ctx.save();ctx.clip('evenodd');
   ctx.lineWidth=1;
   for (const cell of layout.cells) {
    ctx.beginPath();
    if (!polygon(cell.polygon)) continue;
    ctx.fillStyle=cell.tone?'#162d40':'#112536';ctx.fill();
    ctx.strokeStyle='#395267';ctx.stroke();
   }
   for (const {cell,alpha} of trail) {
    ctx.beginPath();
    if (!polygon(cell.polygon)) continue;
    ctx.fillStyle=`rgba(80,238,169,${alpha*.49})`;ctx.fill();
    ctx.strokeStyle=`rgba(138,255,200,${alpha*.86})`;ctx.lineWidth=2;ctx.stroke();
   }
   if (!node&&(!from||t===1)) {
    ctx.font='11px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';
    for (const cell of layout.labels) {
     const p=projection.point(flat(cell.center));
     if (p) {ctx.fillStyle='#9db5c7';ctx.fillText(cell.label,...p);}
    }
   }
   ctx.restore();
   ctx.beginPath();
   for (const ring of layout.rings) polygon(ring);
   ctx.strokeStyle='#7896ac';ctx.lineWidth=1.5;ctx.stroke();

   ctx.textAlign='left';ctx.textBaseline='middle';ctx.font='600 12px system-ui';
   for (const [key,device] of Object.entries(devices)) {
    if (key===view||!colors[key]) continue;
    const p=projection.point(flat(device.position,node?device.height??.16:0));
    if (!p||p[0]<0||p[0]>w||p[1]<0||p[1]>h) continue;
    ctx.beginPath();ctx.arc(...p,5,0,TAU);ctx.fillStyle=colors[key];ctx.fill();
    ctx.lineWidth=3;ctx.strokeStyle='#0a1721';ctx.strokeText(names[key],p[0]+9,p[1]-8);
    ctx.fillText(names[key],p[0]+9,p[1]-8);
   }
   if (darkness>0) drawGrain(ctx,w,h,radio,null,gain,darkness);
  }

  return {
   select,draw,
   setLayout(next) {layout=next;view='map';from=null;camera=null;darkness=0;},
   rotate(dx,dy) {yaw=angle(yaw+dx);pitch=clamp(pitch+dy,-1.15,1.3);},
   heading() {return (Math.round(yaw*180/Math.PI)+360)%360;}
  };
 }

 function drawSpectrum(canvas,values,color) {
  const surface=canvasContext(canvas);
  if (!surface||!values) return;
  const {ctx,w,h}=surface;
  const step=w/values.length;
  ctx.fillStyle=color;
  values.forEach((v,i)=>{
   const bar=clamp(v,0,1)*(h-10);
   ctx.fillRect(i*step,h-4-bar,Math.max(1,step-1),bar);
  });
 }

 function drawPose(canvas,points) {
  const surface=canvasContext(canvas);
  if (!surface||!points) return;
  const {ctx,w,h}=surface;
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  const x0=Math.min(...xs),y0=Math.min(...ys),width=Math.max(...xs)-x0,height=Math.max(...ys)-y0;
  const scale=Math.min((w-24)/Math.max(.03,width),(h-20)/Math.max(.03,height));
  const pose=points.map(([x,y])=>[(w-width*scale)/2+(x-x0)*scale,(h-height*scale)/2+(y-y0)*scale]);
  ctx.strokeStyle='#79d7ff';ctx.lineWidth=2;ctx.lineCap='round';ctx.beginPath();
  for (const [a,b] of bones) {ctx.moveTo(...pose[a]);ctx.lineTo(...pose[b]);}
  ctx.stroke();
  ctx.fillStyle='#d7f2ff';
  for (const p of pose) {ctx.beginPath();ctx.arc(...p,2,0,TAU);ctx.fill();}
 }

 return {grid,trail,scene,drawSpectrum,drawPose,colors,names};
})();
