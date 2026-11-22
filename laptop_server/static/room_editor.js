'use strict';

const RoomPlan = (() => {
 const steps = 20, epsilon = 1e-8;
 const between = (a, b, t=.5) => a.map((v, axis) => v+(b[axis]-v)*t);
 const edges = points => points.map((p,i) => [p,points[(i+1)%points.length]]);
 const rounded = value => Math.round(value*1000)/1000;

 function boundary(outline, curves) {
  return edges(outline).flatMap(([a,b],i) => {
   const c = curves[i];
   if(!c)return [a];
   return Array.from({length:steps},(_,step) => {
    const t = step/steps, u = 1-t;
    return a.map((v, axis) => u*u*v+2*u*t*c[axis]+t*t*b[axis]);
   });
  });
 }

 function bounds(room) {
  if(room.shape!=='custom')return [0,0,room.width,room.depth];
  const xs=room.boundary.map(p=>p[0]), zs=room.boundary.map(p=>p[1]);
  return [Math.min(...xs),Math.min(...zs),Math.max(...xs),Math.max(...zs)];
 }

 function updateBounds(room) {
  room.boundary=boundary(room.outline,room.curves);
  const [left,top,right,bottom]=bounds(room);
  [room.width,room.depth]=[right,bottom].map(v=>Number(v.toFixed(8)));
  if(room.sector_mode==='auto'){
   [room.cols,room.rows]=[right-left,bottom-top].map(v=>Math.max(1,Math.min(10,Math.ceil(Number(v.toFixed(8))/2))));
  }
  return room;
 }

 function editable(room) {
  if(room.shape==='custom')return JSON.parse(JSON.stringify(room));
  const {width,depth,cols,rows,short_width:short} = room;
  let outline = [[0,0],[width,0],[width,depth],[0,depth]];
  let curves = [null,null,null,null];
  if(room.shape==='curved'){
   const radius = width-short, mid = Math.SQRT1_2, tangent = Math.SQRT2-1;
   outline = [[0,0],[width,0],[short+radius*mid,depth*mid],[short,depth],[0,depth]];
   curves = [null,[width,depth*tangent],[short+radius*tangent,depth],null,null];
  }
  return updateBounds({shape:'custom',cols,rows,outline,curves,cutouts:[],sector_mode:room.sector_mode});
 }

 function scale(room,width,depth) {
  const draft=editable(room);
  const [left,top,right,bottom]=bounds(draft);
  const scaleX=Math.abs(width-(right-left))<epsilon?1:width/(right-left);
  const scaleZ=Math.abs(depth-(bottom-top))<epsilon?1:depth/(bottom-top);
  const point=([x,z])=>[left+(x-left)*scaleX,top+(z-top)*scaleZ];
  draft.outline = draft.outline.map(point);
  draft.curves = draft.curves.map(c=>c&&point(c));
  draft.cutouts = draft.cutouts.map(([x0,z0,x1,z1])=>[x0*scaleX,z0*scaleZ,x1*scaleX,z1*scaleZ]);
  return updateBounds(draft);
 }

 function payload({outline,curves,cutouts,sector_mode='manual'}) {
  return {outline,curves,cutouts,sector_mode,shape:'custom',fork_if_recorded:true};
 }

 function contains(room,x,z) {
  if(room.cutouts.some(([x0,z0,x1,z1])=>x>=x0-epsilon&&x<=x1+epsilon&&z>=z0-epsilon&&z<=z1+epsilon))return false;
  let result = false;
  for(const [a,b] of edges(room.boundary)){
   const [ax,az]=a, [bx,bz]=b;
   const cross = (bx-ax)*(z-az)-(bz-az)*(x-ax);
   if(Math.abs(cross)<=epsilon&&x>=Math.min(ax,bx)-epsilon&&x<=Math.max(ax,bx)+epsilon
       &&z>=Math.min(az,bz)-epsilon&&z<=Math.max(az,bz)+epsilon)return true;
   if((az>z)!==(bz>z)&&x<ax+(z-az)*(bx-ax)/(bz-az))result=!result;
  }
  return result;
 }

 function path(room) {
  const ring = points => 'M'+points.map(p=>p.join(' ')).join('L')+'Z';
  return ring(room.boundary)+room.cutouts.map(([x0,z0,x1,z1])=>ring([[x0,z0],[x1,z0],[x1,z1],[x0,z1]])).join('');
 }

 function sectorPoint(room,col,row) {
  const [x0,z0,x1,z1]=bounds(room);
  const w=(x1-x0)/room.cols, h=(z1-z0)/room.rows;
  const left=x0+col*w, right=left+w, top=z0+row*h, bottom=top+h;
  const center=[left+w/2,top+h/2];
  if(contains(room,...center))return center;
  const walls=edges(room.boundary);
  const levels=[top,bottom,...room.boundary.map(p=>p[1]),...room.cutouts.flatMap(r=>[r[1],r[3]])];
  for(const [a,b] of walls)for(const x of [left,right]){
   if((a[0]>x)!==(b[0]>x))levels.push(a[1]+(b[1]-a[1])*(x-a[0])/(b[0]-a[0]));
  }
  const sorted=levels.filter(z=>z>=top&&z<=bottom).sort((a,b)=>a-b);
  let best=null, distance=Infinity;
  for(let i=1;i<sorted.length;i++){
   if(sorted[i]-sorted[i-1]<=epsilon)continue;
   const z=(sorted[i]+sorted[i-1])/2;
   const stops=[left,right];
   for(const [a,b] of walls){
    if((a[1]>z)!==(b[1]>z))stops.push(a[0]+(b[0]-a[0])*(z-a[1])/(b[1]-a[1]));
   }
   for(const [x0,z0,x1,z1] of room.cutouts)if(z>=z0&&z<=z1)stops.push(x0,x1);
   const xs=stops.filter(x=>x>=left&&x<=right).sort((a,b)=>a-b);
   for(let j=1;j<xs.length;j++){
    if(xs[j]-xs[j-1]<=epsilon)continue;
    const x=(xs[j]+xs[j-1])/2, d=Math.hypot(x-center[0],z-center[1]);
    if(d<distance&&contains(room,x,z)){best=[x,z];distance=d;}
   }
  }
  return best;
 }

 function editor({getState,send,draw,refresh,$}) {
  const map=$('map');
  let draft=null, base=null, original='', selected=null, cutting=false, first=null, drag=null, pending=false;
  let fresh=false, drawing=false, cursor=null, guides=null;

  function reset() {
   draft=base=selected=first=drag=null;
   cutting=pending=fresh=drawing=false;
   cursor=guides=null;
  }

  function sync(state,online) {
   if(draft&&(base.id!==(state.dataset||{}).id||base.room!==JSON.stringify(state.room)))reset();
   $('editRoom').disabled=!online||!state.room||!!state.recording;
   $('editRoom').hidden=!!draft;
   $('newRoom').hidden=!!draft;
   $('newRoom').disabled=!online||!!state.recording;
   $('roomTools').hidden=!draft;
   $('planSettings').hidden=!!draft;
   $('planSelection').hidden=!!draft;
   map.setAttribute('data-editing',draft?'true':'false');
   map.setAttribute('data-drawing',drawing?'true':'false');
   $('drawRoomTools').hidden=!drawing;
   $('shapeRoomTools').hidden=drawing;
   $('saveRoomEdit').hidden=drawing;
   if(!draft)return;
   const edge=selected&&(selected.kind==='wall'||selected.kind==='curve');
   const blocked=pending||!online||!!state.recording;
   $('closeRoomOutline').disabled=blocked||draft.outline.length<3;
   $('undoRoomPoint').disabled=blocked||!draft.outline.length;
   $('rectangleRoom').disabled=blocked||!fieldSizes().every(v=>v>=1&&v<=50);
   $('addCorner').disabled=blocked||!edge||draft.outline.length>=32;
   $('addBend').disabled=blocked||!edge||!!draft.curves[selected.index];
   $('removeShape').disabled=blocked||!selected||selected.kind==='wall'||(selected.kind==='corner'&&draft.outline.length<=3);
   $('addCutout').disabled=blocked||draft.cutouts.length>=12;
   $('addCutout').textContent=cutting?'Отменить вырез':'Добавить вырез / колонну';
   $('saveRoomEdit').disabled=blocked||drawing||cutting||(!fresh&&original===JSON.stringify(payload(draft)));
   $('saveRoomEdit').textContent=fresh?'Создать план':state.sessions.length?'Сохранить как новый план':'Сохранить изменения';
   $('cancelRoomEdit').disabled=pending;
   $('roomEditHint').textContent=pending?'Сохраняем план…':
    drawing?(draft.outline.length?'Ставьте углы по порядку. Нажмите первый угол или кнопку «Замкнуть контур».':'Поставьте первый угол на сетке или создайте прямоугольник по размерам.'):
    cutting?(first?'Выберите противоположный угол выреза.':'Укажите первый угол выреза внутри плана.'):
    'Перетаскивайте углы. Нажмите на стену, чтобы добавить угол или изгиб.';
   updateSize();
  }

  $('editRoom').onclick=()=>{
   const state=getState();
   if(!state?.room||state.recording)return;
   draft=editable(state.room);
   base={id:(state.dataset||{}).id,room:JSON.stringify(state.room)};
   original=JSON.stringify(payload(draft));
   $('roomEditError').textContent='';
   refresh();
  };
  $('newRoom').onclick=()=>{
   const state=getState();
   if(!state||state.recording)return;
   reset();
   base={id:(state.dataset||{}).id,room:JSON.stringify(state.room)};
   fresh=drawing=true;
   $('newPlanWidth').value='8';
   $('newPlanDepth').value='6';
   draft={shape:'custom',sector_mode:'auto',cols:3,rows:2,width:8,depth:6,outline:[],curves:[],cutouts:[]};
   $('roomEditError').textContent='';
   refresh();
  };

  function fieldSizes() {
   return ['newPlanWidth','newPlanDepth'].map(id=>Number($(id).value));
  }

  function updateSize() {
   const [x0,z0,x1,z1]=drawing?[0,0,draft.width,draft.depth]:bounds(draft);
   const size=(x1-x0).toFixed(2)+' × '+(z1-z0).toFixed(2)+' м';
   $('roomEditSize').textContent=drawing?'Поле: '+size:'Размеры: '+size+' · сетка '+draft.cols+' × '+draft.rows;
  }

  function resizeField() {
   if(!drawing)return;
   [draft.width,draft.depth]=fieldSizes().map((v,i)=>Math.max(
    v>=1&&v<=50?v:(i===0?draft.width:draft.depth),...draft.outline.map(p=>p[i])));
   cursor=guides=null;
   refresh();
  }

  function closeOutline() {
   if(draft.outline.length<3)return;
   draft.curves=draft.outline.map(()=>null);
   drawing=false;
   cursor=guides=null;
   changed();
  }

  $('newPlanWidth').oninput=$('newPlanDepth').oninput=resizeField;
  $('undoRoomPoint').onclick=()=>{
   draft.outline.pop();
   $('roomEditError').textContent='';
   resizeField();
  };
  $('closeRoomOutline').onclick=closeOutline;
  $('rectangleRoom').onclick=()=>{
   if($('rectangleRoom').disabled)return;
   const [w,h]=fieldSizes();
   draft.outline=[[0,0],[w,0],[w,h],[0,h]];
   closeOutline();
  };

  $('cancelRoomEdit').onclick=()=>{reset();refresh();};
  $('addCorner').onclick=()=>{
   const i=selected.index, a=draft.outline[i], b=draft.outline[(i+1)%draft.outline.length], c=draft.curves[i];
   const firstControl=c?between(a,c):null, secondControl=c?between(c,b):null;
   draft.outline.splice(i+1,0,c?between(firstControl,secondControl):between(a,b));
   draft.curves.splice(i,1,firstControl,secondControl);
   selected={kind:'corner',index:i+1};
   changed();
  };
  $('addBend').onclick=()=>{
   const i=selected.index;
   draft.curves[i]=between(draft.outline[i],draft.outline[(i+1)%draft.outline.length]);
   selected={kind:'curve',index:i};
   changed();
  };
  $('removeShape').onclick=()=>{
   const {kind,index}=selected;
   if(kind==='cutout')draft.cutouts.splice(index,1);
   if(kind==='curve')draft.curves[index]=null;
   if(kind==='corner'){
    draft.curves[(index+draft.curves.length-1)%draft.curves.length]=null;
    draft.curves.splice(index,1);
    draft.outline.splice(index,1);
   }
   selected=null;
   changed();
  };
  $('addCutout').onclick=()=>{cutting=!cutting;first=selected=null;refresh();};
  $('saveRoomEdit').onclick=()=>{
   if($('saveRoomEdit').disabled)return;
   pending=true;
   $('roomEditError').textContent='';
   send('localization_config',{...payload(draft),width:draft.width,depth:draft.depth,
     cols:draft.cols,rows:draft.rows,base_dataset_id:base.id,declared_area:null,new_plan:fresh});
   refresh();
  };

  function changed() {
   updateBounds(draft);
   $('roomEditError').textContent='';
   refresh();
  }

  function point(ev,matrix=map.getScreenCTM()?.inverse()) {
   if(!matrix)return null;
   const svgPoint=map.createSVGPoint();
   svgPoint.x=ev.clientX;svgPoint.y=ev.clientY;
   const p=svgPoint.matrixTransform(matrix);
   return [p.x,p.y].map(v=>rounded(Math.max(0,Math.min(50,v))));
  }

  function aligned(p,ev,matrix,anchors) {
   guides=null;
   if(ev.altKey||$('snapPlan').checked===false)return p;
   const tolerance=[Math.hypot(matrix.a,matrix.c),Math.hypot(matrix.b,matrix.d)]
    .map(scale=>Math.min(.3,10*(scale||.01)));
   guides=p.map((value,axis)=>{
    let best=null, distance=tolerance[axis];
    for(const anchor of anchors){
     const delta=Math.abs(anchor[axis]-value);
     if(delta<=distance){best=anchor[axis];distance=delta;}
    }
    return best;
   });
   return p.map((value,axis)=>guides[axis]??value);
  }

  function drawingPoint(ev) {
   const matrix=map.getScreenCTM()?.inverse();
   if(!matrix)return null;
   const [x,z]=point(ev,matrix);
   return aligned([Math.min(draft.width,x),Math.min(draft.depth,z)],ev,matrix,[...draft.outline,[0,0]]);
  }

  function addPoint(ev) {
   const p=drawingPoint(ev);
   if(!p)return;
   const near=q=>Math.hypot(q[0]-p[0],q[1]-p[1])<.01;
   if(draft.outline.length>=3&&near(draft.outline[0])){closeOutline();return;}
   if(draft.outline.some(near))return;
   if(draft.outline.length>=32){
    $('roomEditError').textContent='Достигнут предел в 32 угла. Замкните контур или уберите точку.';
    return;
   }
   draft.outline.push(p);
   cursor=guides=null;
   $('roomEditError').textContent='';
   refresh();
  }

  function click(ev) {
   if(!draft)return false;
   if(pending)return true;
   if(drawing){addPoint(ev);return true;}
   if(!cutting)return true;
   const p=point(ev);
   if(!p||!contains(draft,...p)){
    $('roomEditError').textContent='Выберите угол выреза внутри комнаты.';
    return true;
   }
   if(!first){first=p;refresh();return true;}
   const x0=Math.min(p[0],first[0]), x1=Math.max(p[0],first[0]);
   const z0=Math.min(p[1],first[1]), z1=Math.max(p[1],first[1]);
   if(x1-x0<.05||z1-z0<.05){
    $('roomEditError').textContent='Выберите вырез размером хотя бы 5 × 5 см.';
    return true;
   }
   draft.cutouts.push([x0,z0,x1,z1]);
   selected={kind:'cutout',index:draft.cutouts.length-1,corner:2};
   cutting=false;first=null;
   changed();
   return true;
  }

  function target(ev) {
   const kind=ev.target?.getAttribute('data-plan-kind');
   if(!kind)return null;
   return {kind,index:Number(ev.target.getAttribute('data-plan-index')),
    corner:Number(ev.target.getAttribute('data-plan-corner'))};
  }

  map.addEventListener('pointerdown',ev=>{
   if(!draft||drawing||cutting||pending||drag||ev.button!==0)return;
   selected=target(ev);
   const matrix=map.getScreenCTM()?.inverse();
   if(selected&&selected.kind!=='wall'&&matrix){
    drag={id:ev.pointerId,matrix,width:draft.width,depth:draft.depth};
    map.setPointerCapture(ev.pointerId);
    ev.preventDefault();
   }
   refresh();
  });
  function moveHandle([x,z]) {
   const {kind,index,corner}=selected;
   if(kind==='corner')draft.outline[index]=[x,z];
   if(kind==='curve')draft.curves[index]=[x,z];
   if(kind==='cutout'){
    const rect=draft.cutouts[index];
    if(corner===0){rect[0]=Math.min(x,rect[2]-.05);rect[1]=Math.min(z,rect[3]-.05);}
    else{rect[2]=Math.max(x,rect[0]+.05);rect[3]=Math.max(z,rect[1]+.05);}
   }
  }

  map.addEventListener('pointermove',ev=>{
   if(drawing){cursor=drawingPoint(ev);draw();return;}
   if(!drag||drag.id!==ev.pointerId)return;
   let p=point(ev,drag.matrix);
   if(selected.kind==='corner'){
    const anchors=draft.outline.filter((_,i)=>i!==selected.index);
    p=aligned(p,ev,drag.matrix,[...anchors,[0,0]]);
   }
   moveHandle(p);
   updateBounds(draft);
   updateSize();
   draw();
  });
  map.addEventListener('pointerleave',()=>{
   if(!drawing)return;
   cursor=guides=null;
   draw();
  });
  function finishDrag(ev) {
   if(!drag||drag.id!==ev.pointerId)return;
   drag=guides=null;
   changed();
  }
  for(const event of ['pointerup','pointercancel','lostpointercapture'])map.addEventListener(event,finishDrag);
  map.addEventListener('keydown',ev=>{
   if(!draft||pending||cutting)return;
   const handle=target(ev);
   if(!handle||!['Enter',' ','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(ev.key))return;
   ev.preventDefault();
   selected=handle;
   if(ev.key.startsWith('Arrow')&&handle.kind!=='wall'){
    const p=handle.kind==='corner'?draft.outline[handle.index]:handle.kind==='curve'?draft.curves[handle.index]:
     draft.cutouts[handle.index].slice(handle.corner,handle.corner+2);
    const step=ev.shiftKey ? .1 : .01, dx=ev.key==='ArrowLeft'?-step:ev.key==='ArrowRight'?step:0;
    const dz=ev.key==='ArrowUp'?-step:ev.key==='ArrowDown'?step:0;
    moveHandle([p[0]+dx,p[1]+dz].map(v=>rounded(Math.max(0,Math.min(50,v)))));
    updateBounds(draft);
   }
   refresh();
   const query='circle[data-plan-kind="'+handle.kind+'"][data-plan-index="'+handle.index+'"][data-plan-corner="'+handle.corner+'"]';
   map.querySelector(query)?.focus();
  });

  function drawGuides(svg) {
   if(!guides)return;
   const [x,z]=guides;
   const [left,top,width,height]=map.getAttribute('viewBox').split(' ').map(Number);
   const style={stroke:'#58e3cd','stroke-width':1,'stroke-dasharray':'4 4','vector-effect':'non-scaling-stroke',
    'pointer-events':'none','data-snap-guide':true};
   if(x!==null)svg('line',{...style,x1:x,x2:x,y1:top,y2:top+height});
   if(z!==null)svg('line',{...style,x1:left,x2:left+width,y1:z,y2:z});
  }

  function drawSketch(svg) {
   const {width,depth,outline}=draft, size=Math.max(width,depth);
   const grid=size<=12 ? .5 : size<=25 ? 1 : 2, radius=size*.012;
   svg('rect',{x:0,y:0,width,height:depth,fill:'#101f31',stroke:'#496780','stroke-width':.02});
   const gridLine={stroke:'#28435b','stroke-width':1,'vector-effect':'non-scaling-stroke'};
   for(let x=grid;x<width;x+=grid)svg('line',{...gridLine,x1:x,x2:x,y1:0,y2:depth});
   for(let z=grid;z<depth;z+=grid)svg('line',{...gridLine,x1:0,x2:width,y1:z,y2:z});
   svg('text',{x:0,y:-radius*2,fill:'#9bb3cb','font-size':radius*1.8},'Клетка '+grid+' м');
   if(outline.length)svg('polyline',{points:outline.map(p=>p.join(',')).join(' '),fill:'none',
    stroke:'#80d4ff','stroke-width':2,'vector-effect':'non-scaling-stroke'});
   if(cursor&&outline.length){
    const [x,z]=outline.at(-1);
    svg('line',{x1:x,y1:z,x2:cursor[0],y2:cursor[1],stroke:'#8eb1cf','stroke-width':1.5,
     'stroke-dasharray':'5 4','vector-effect':'non-scaling-stroke'});
   }
   drawGuides(svg);
   outline.forEach(([x,z],i)=>svg('circle',{cx:x,cy:z,r:i===0?radius*1.25:radius,
    fill:i===0?'#58e3cd':'#72d4ff'}));
   if(cursor)svg('circle',{cx:cursor[0],cy:cursor[1],r:radius*.75,fill:'#a7ceee'});
  }

  function overlay(svg) {
   if(drawing){drawSketch(svg);return;}
   drawGuides(svg);
   const radius=Math.max(draft.width,draft.depth,1)*.015;
   function handle(p,kind,index,label,corner=0) {
    const active=selected?.kind===kind&&selected.index===index&&(selected.corner??0)===corner;
    const fill=kind==='curve'?'#cf9bff':kind==='cutout'?'#ffbb77':kind==='wall'?'#192e45':'#72d4ff';
    svg('circle',{cx:p[0],cy:p[1],r:kind==='wall'?radius*.7:radius,fill,stroke:active?'#fff':'#6f8aa4',
     'stroke-width':active?2:1,'vector-effect':'non-scaling-stroke',
     'data-plan-kind':kind,'data-plan-index':index,'data-plan-corner':corner,
     tabindex:0,role:'button','aria-label':label});
   }

   for(const [i,[a,b]] of edges(draft.outline).entries()){
    const mid=between(a,b), c=draft.curves[i];
    const d='M'+a.join(' ')+(c?'Q'+c.join(' ')+' ':'L')+b.join(' ');
    if(selected?.index===i&&(selected.kind==='wall'||selected.kind==='curve'))
     svg('path',{d,fill:'none',stroke:'#72d4ff','stroke-width':2,'vector-effect':'non-scaling-stroke'});
    svg('path',{d,fill:'none',stroke:'transparent','stroke-width':16,'pointer-events':'stroke',
     'vector-effect':'non-scaling-stroke','data-plan-kind':'wall','data-plan-index':i});
    if(c){
     svg('line',{x1:mid[0],y1:mid[1],x2:c[0],y2:c[1],stroke:'#c398f5','stroke-width':.02,'stroke-dasharray':'.06 .04'});
     handle(c,'curve',i,'Изгиб стены '+(i+1));
    }else handle(mid,'wall',i,'Стена '+(i+1));
   }
   draft.outline.forEach((p,i)=>handle(p,'corner',i,'Угол '+(i+1)));
   draft.cutouts.forEach(([x0,z0,x1,z1],i)=>{
    handle([x0,z0],'cutout',i,'Первый угол выреза '+(i+1));
    handle([x1,z1],'cutout',i,'Второй угол выреза '+(i+1),2);
   });
   if(first)svg('circle',{cx:first[0],cy:first[1],r:radius,fill:'#ffbb77'});
  }

  return {
   get draft(){return draft;},
   get drawing(){return drawing;},
   get fresh(){return fresh;},
   dimensions(room){return drag?[drag.width,drag.depth]:[room.width,room.depth];},
   sync,click,overlay,
   error(message){
    if(!pending)return false;
    pending=false;
    $('roomEditError').textContent=message;
    refresh();
    return true;
   },
   disconnected(){
    if(!pending)return;
    pending=false;
    $('roomEditError').textContent='Связь прервалась. После подключения проверьте, сохранился ли план.';
   },
  };
 }

 return {contains,path,payload,scale,bounds,sectorPoint,editor};
})();
