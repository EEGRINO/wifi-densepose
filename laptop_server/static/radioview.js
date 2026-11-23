'use strict';

(() => {
 const byId=id=>document.getElementById(id);
 const keys=['esp1','esp2'];
 const canvas=byId('room');
 const viewport=byId('viewport');
 const scene=RadioScene.scene(canvas), trail=RadioScene.trail();
 let rawSamples=new Map();
 const estimateLabels={empty:'Пустая комната',untrained:'Нужна калибровка',model_needed:'Нужна калибровка',
  waiting_signal:'Ожидание сигнала двух ESP',missing_lines:'Не хватает радиолиний',poor_signal:'Слабый сигнал',
  model_unreliable:'Модель требует проверки',background_failed:'Проверьте калибровку пустой комнаты',
  lines_changed:'Радиолинии изменились',unknown:'Зона не определена'};
 let connection=false, localization=null, ranging=null, presence=null, layout=null, layoutId='';
 let devices={}, view='map', receiverOptions='', receivers={esp1:'',esp2:''}, pointer=null, lastHud=0, primaryIp='';

 function text(id,value) {
  const el=byId(id);
  if (el.textContent!==value) el.textContent=value;
 }

 function bindReceivers() {
  const available=new Set(Object.values(receivers).filter(Boolean));
  for (const ip of rawSamples.keys()) available.add(ip);
  for (const line of localization?.active_lines??[]) available.add(line.split(' <- ')[0]);
  const values=[...available].sort();
  const signature=JSON.stringify([values,receivers]);
  if (signature===receiverOptions) return;
  receiverOptions=signature;
  for (const key of keys) {
   const select=byId(`${key}-ip`), other=keys.find(k=>k!==key);
   const opts=[new Option('Выберите IP','')];
   for (const ip of values) {
    const option=new Option(ip,ip);
    option.disabled=receivers[other]===ip;
    opts.push(option);
   }
   select.replaceChildren(...opts);
   select.value=receivers[key];
   select.disabled=!values.length;
  }
 }

 function storageKey() {return `radioview.receivers.${localization?.dataset?.id??'original'}`;}

 for (const key of keys) byId(`${key}-ip`).onchange=event=>{
  receivers[key]=event.target.value;
  try {localStorage.setItem(storageKey(),JSON.stringify(receivers));} catch (_) {}
  bindReceivers();
 };

 function viewMode(next,now=performance.now()) {
  const aim=positionAt(now).point;
  if (!scene.select(next,devices,aim,now)) return;
  view=next;
  for (const key of ['map',...keys]) byId(`${key}-view`).setAttribute('aria-pressed',String(key===view));
  byId('radio-panel').hidden=view==='map';
  byId('full-view').hidden=view==='map';
  canvas.dataset.orbit=String(view!=='map');
  text('stage-hint',view==='map'?'Выберите ESP для обзора с её позиции.':'Поворот на 360° мышью или стрелками');
  byId('grid-label').hidden=!layout||view!=='map';
  updateViewHeading();
 }

 function updateViewHeading() {
  text('view-label',view==='map'?'План помещения':`${RadioScene.names[view]} · поворот ${scene.heading()}°`);
 }

 for (const id of ['map',...keys]) byId(`${id}-view`).onclick=()=>{
  viewMode(id);
  if (view!=='map') canvas.focus({preventScroll:true});
 };
 function expanded(value) {
  viewport.dataset.expanded=String(value);
  byId('exit-full-view').hidden=!value;
  (value?canvas:byId('full-view')).focus({preventScroll:true});
 }
 byId('full-view').onclick=async()=>{
  if (document.fullscreenEnabled&&viewport.requestFullscreen) {
   try {await viewport.requestFullscreen();} catch (_) {}
  }
  expanded(true);
 };
 byId('exit-full-view').onclick=()=>{
  if (document.fullscreenElement===viewport) document.exitFullscreen().catch(()=>{});
  expanded(false);
 };
 document.addEventListener('fullscreenchange',()=>{
  if (!document.fullscreenElement) expanded(false);
 });
 document.addEventListener('keydown',event=>{
  if (event.key==='Escape'&&viewport.dataset.expanded==='true'&&!document.fullscreenElement) expanded(false);
 });
 canvas.addEventListener('pointerdown',event=>{
  if (view==='map'||pointer||event.button!==0) return;
  pointer={id:event.pointerId,x:event.clientX,y:event.clientY};
  canvas.setPointerCapture(event.pointerId);
 });
 canvas.addEventListener('pointermove',event=>{
  if (pointer?.id!==event.pointerId||view==='map') return;
  scene.rotate((pointer.x-event.clientX)*.008,(event.clientY-pointer.y)*.008);
  pointer.x=event.clientX;pointer.y=event.clientY;
  updateViewHeading();
 });
 canvas.addEventListener('lostpointercapture',()=>{pointer=null;});
 canvas.addEventListener('keydown',event=>{
  if (view==='map') return;
  const direction={ArrowLeft:[-.18,0],ArrowRight:[.18,0],ArrowUp:[0,-.14],ArrowDown:[0,.14]}[event.key];
  if (!direction) return;
  event.preventDefault();
  scene.rotate(...direction);
  updateViewHeading();
 });

 function setLocalization(data,now) {
  const changedDataset=data.dataset?.id!==localization?.dataset?.id;
  localization={...data,receivedAt:now};
  const nextLayoutId=JSON.stringify([data.dataset?.id,data.room]);
  devices=data.devices??{};
  if (nextLayoutId!==layoutId) {
   layout=RadioScene.grid(data.room);
   layoutId=nextLayoutId;
   trail.clear();
   scene.setLayout(layout);
   viewMode('map',now);
  }
  if (changedDataset) {
   let saved={};
   try {saved=JSON.parse(localStorage.getItem(storageKey()))??{};} catch (_) {}
   receivers={esp1:typeof saved.esp1==='string'?saved.esp1:'',esp2:typeof saved.esp2==='string'?saved.esp2:''};
   if (receivers.esp2===receivers.esp1) receivers.esp2='';
  }
  for (const key of keys) byId(`${key}-view`).disabled=!layout||!devices[key];
  if (view!=='map'&&!devices[view]) viewMode('map',now);
  byId('no-plan').hidden=!!layout;
  byId('grid-label').hidden=!layout||view!=='map';
  text('grid-label',layout?`Клетка ${layout.size} × ${layout.size} м`:'');
  bindReceivers();
 }

 function positionAt(now) {
  const loc=now-(localization?.receivedAt??-Infinity)<2700?localization:null;
  const estimate=loc?.estimate;
  if (estimate?.state==='estimated'&&layout?.cellAt(estimate.position)) {
   return {point:estimate.position,state:'estimated',source:'Локализация по CSI'};
  }
  if (loc?.model_ready||estimate?.mode==='ab') return {state:estimate?.state??'unknown'};
  if (loc&&ranging?.dataset?.id===loc.dataset?.id&&now-ranging.receivedAt<2700&&ranging.model_ready) {
   const point=ranging.estimate?.position;
   if (layout?.cellAt(point)) return {point,state:'estimated',source:'Оценка по точкам дальности'};
   return {state:ranging.live_state};
  }
  if (now-(presence?.receivedAt??-Infinity)<2500&&presence.active_links>0) {
   if (presence.state==='empty') return {state:'empty'};
   if (presence.state==='moving'||presence.state==='still') return {state:presence.state};
  }
  return {state:loc?estimate?.state??'untrained':'unknown'};
 }

 function accept(message) {
  const now=performance.now();
  if (message.type==='localization') setLocalization(message,now);
  else if (message.type==='ranging_web') ranging={...message,receivedAt:now};
  else if (message.type==='presence') presence={...message,receivedAt:now};
  else if (message.type==='status') primaryIp=message.csi_primary_env||message.csi_primary_ip||primaryIp;
  else if (message.type==='visor_csi'&&Array.isArray(message.frames)) setRawSamples(message.frames,now);
  else if (message.csi_primary===true&&typeof message.udp_src==='string') primaryIp=message.udp_src;
 }

 function setRawSamples(frames,now) {
  const next=new Map();
  for (const packet of frames) {
   if (!packet) continue;
   const ip=packet.receiver;
   const age=packet.age_ms;
   if (typeof ip!=='string'||!ip||!Number.isFinite(age)||age<0||age>1000) continue;
   const current=next.get(ip);
   // Prefer the frame type used for camera calibration, then the newest packet of that type.
   if (current&&((!current.peer&&packet.peer)||(packet.peer===current.peer&&age>=current.age_ms))) continue;
   const key=JSON.stringify([ip,packet.source,packet.channel,packet.sequence,packet.timestamp_us,packet.iq]);
   const previous=rawSamples.get(ip);
   const samples=previous?.key===key?previous.samples:VisorGrain.decode(packet.iq);
   if (samples) next.set(ip,{...packet,key,samples,receivedAt:now-age});
  }
  rawSamples=next;
  bindReceivers();
 }

 const wsBase=`ws://${location.hostname||'127.0.0.1'}:8765`;
 function connect(path,command) {
  const socket=new WebSocket(wsBase+path);
  let requestTimer, lastRequest=-Infinity, pending=false;
  function query() {
   lastRequest=performance.now();
   pending=true;
   socket.send(JSON.stringify({cmd:command}));
  }
  function connectionLabel(live) {
   if (path!=='/') return;
   connection=live;
   byId('connection').dataset.live=String(live);
   text('connection',live?'Сервер подключён':'Связь потеряна · переподключение');
  }
  socket.onopen=()=>{
   connectionLabel(true);
   query();
   if (path==='/ranging') requestTimer=setInterval(query,850);
   if (path==='/visor') requestTimer=setInterval(()=>{
    const elapsed=performance.now()-lastRequest;
    if (elapsed>=1000||(!pending&&view!=='map')) query();
   },70);
  };
  socket.onmessage=({data})=>{
   let message;
   try {message=JSON.parse(data);} catch (_) {return;}
   if (message?.type==='visor_csi') pending=false;
   if (message&&typeof message==='object') accept(message);
  };
  socket.onclose=()=>{
   clearInterval(requestTimer);
   connectionLabel(false);
   if (path==='/') {
    if (localization) localization.receivedAt=-Infinity;
    presence=null;
   } else if (path==='/ranging') ranging=null;
   else {
    rawSamples.clear();
    bindReceivers();
   }
   setTimeout(()=>connect(path,command),1500);
  };
 }

 function currentRadio(now) {
  const packet=rawSamples.get(receivers[view]);
  return packet&&now-packet.receivedAt<1100?packet:null;
 }

 function posePoints(vector) {
  if (!Array.isArray(vector)||vector.length<32) return null;
  const values=vector.slice(0,32);
  if (!values.every(v=>Number.isFinite(v)&&v>=-.35&&v<=1.35)) return null;
  const points=Array.from({length:16},(_,i)=>[values[i*2],values[i*2+1]]);
  return points.some(p=>Math.hypot(p[0]-points[0][0],p[1]-points[0][1])>.04)?points:null;
 }

 function updateHud(result,cell,radio,joints) {
  const presenceLabel={moving:'Есть движение · зона неизвестна',still:'Присутствие · зона неизвестна'};
  text('position',!connection?'Нет связи с сервером':!layout?'Нужен план помещения':cell?`Клетка ${cell.label}`:
   presenceLabel[result.state]??estimateLabels[result.state]??'Зона не определена');
  text('position-source',cell?result.source:'');
  if (view==='map') return;
  text('radio-title',`Сигнал ${RadioScene.names[view]}`);
  const ip=receivers[view];
  const strength=radio&&Number.isFinite(radio.rssi)?` · ${radio.rssi} дБм`:'';
  text('radio-status',radio?`${ip}${strength} · канал ${radio.channel}`:ip?`${ip} · нет свежих кадров I/Q`:'Укажите IP принимающей платы');
  const values=radio?VisorGrain.values(radio.samples).amplitude:null;
  RadioScene.drawSpectrum(byId('spectrum'),values,RadioScene.colors[view]);
  byId('pose-panel').hidden=!joints;
  if (joints) RadioScene.drawPose(byId('pose'),joints);
  const awaitingCalibration=!!radio&&!radio.peer&&ip===primaryIp&&!radio.pose;
  byId('pose-calibration').hidden=!awaitingCalibration;
  text('pose-status',joints?'Силуэт по экспериментальной 2D-оценке.':!radio?'':!cell?
   'Для размещения силуэта нужна оценка положения.':awaitingCalibration?'Нужна калибровка позы.':
   'Для этого потока доступно только зерно CSI.');
 }

 function frame(now) {
  const result=positionAt(now);
  const cell=layout?.cellAt(result.point)??null;
  const radio=currentRadio(now);
  const joints=cell&&radio?posePoints(radio.pose):null;
  trail.update(cell,now);
  scene.draw(now,devices,trail.visible(now),cell?result.point:null,radio,joints);
  if (now-lastHud>=120) {
   updateHud(result,cell,radio,joints);
   lastHud=now;
  }
  requestAnimationFrame(frame);
 }

 connect('/','localization_status');
 connect('/ranging','ranging_web_status');
 connect('/visor','visor_snapshot');
 requestAnimationFrame(frame);
})();
