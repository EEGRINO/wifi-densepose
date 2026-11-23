'use strict';

(() => {
 const byId=id=>document.getElementById(id);
 const keys=['esp1','esp2'];
 const canvas=byId('room');
 const scene=RadioScene.scene(canvas), trail=RadioScene.trail();
 const samples=new Map();
 const useful=[...Array.from({length:26},(_,i)=>38+i),...Array.from({length:26},(_,i)=>1+i)];
 const estimateLabels={empty:'Пустая комната',untrained:'Нужна калибровка',model_needed:'Нужна калибровка',
  waiting_signal:'Ожидание сигнала двух ESP',missing_lines:'Не хватает радиолиний',poor_signal:'Слабый сигнал',
  model_unreliable:'Модель требует проверки',background_failed:'Проверьте калибровку пустой комнаты',
  lines_changed:'Радиолинии изменились',unknown:'Зона не определена'};
 let connection=false, localization=null, ranging=null, presence=null, layout=null, layoutId='';
 let devices={}, view='map', receiverOptions='', receivers={esp1:'',esp2:''}, pointer=null, lastHud=0;

 function text(id,value) {
  const el=byId(id);
  if (el.textContent!==value) el.textContent=value;
 }

 function bindReceivers() {
  const available=new Set(Object.values(receivers).filter(Boolean));
  for (const ip of samples.keys()) available.add(ip);
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
  canvas.dataset.orbit=String(view!=='map');
  text('stage-hint',view==='map'?'Выберите ESP для обзора с её позиции.':'Перетаскивайте сцену или нажимайте стрелки. Пунктир — геометрическая схема пути.');
  updateViewHeading();
 }

 function updateViewHeading() {
  text('view-label',view==='map'?'План помещения':`${RadioScene.names[view]} · поворот ${scene.heading()}°`);
 }

 for (const id of ['map',...keys]) byId(`${id}-view`).onclick=()=>{
  viewMode(id);
  if (view!=='map') canvas.focus({preventScroll:true});
 };
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
  byId('grid-label').hidden=!layout;
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
  else if (typeof message.udp_src==='string'&&message.udp_src&&Array.isArray(message.heights)&&message.heights.length>=64) {
   const isNew=!samples.has(message.udp_src);
   samples.set(message.udp_src,{rssi:message.rssi,heights:message.heights,
    skeleton:message.csi_primary===true?message.skeleton:null,receivedAt:now});
   if (isNew) bindReceivers();
  }
 }

 const wsBase=`ws://${location.hostname||'127.0.0.1'}:8765`;
 function connect(path,command) {
  const socket=new WebSocket(wsBase+path);
  let requestTimer;
  const query=()=>socket.send(JSON.stringify({cmd:command}));
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
  };
  socket.onmessage=({data})=>{
   let message;
   try {message=JSON.parse(data);} catch (_) {return;}
   if (message&&typeof message==='object') accept(message);
  };
  socket.onclose=()=>{
   clearInterval(requestTimer);
   connectionLabel(false);
   if (path==='/') {
    if (localization) localization.receivedAt=-Infinity;
    presence=null;
    samples.clear();
    bindReceivers();
   } else ranging=null;
   setTimeout(()=>connect(path,command),1500);
  };
 }

 function currentRadio(now) {
  const packet=samples.get(receivers[view]);
  return packet&&now-packet.receivedAt<1500?packet:null;
 }

 function posePoints(vector) {
  if (!Array.isArray(vector)||vector.length<32) return null;
  const values=vector.slice(0,32);
  if (!values.every(v=>Number.isFinite(v)&&v>=-.35&&v<=1.35)) return null;
  const points=Array.from({length:16},(_,i)=>[values[i*2],values[i*2+1]]);
  return points.some(p=>Math.hypot(p[0]-points[0][0],p[1]-points[0][1])>.04)?points:null;
 }

 function updateHud(result,cell,radio) {
  const presenceLabel={moving:'Есть движение · зона неизвестна',still:'Присутствие · зона неизвестна'};
  text('position',!connection?'Нет связи с сервером':!layout?'Нужен план помещения':cell?`Клетка ${cell.label}`:
   presenceLabel[result.state]??estimateLabels[result.state]??'Зона не определена');
  text('position-source',cell?result.source:'');
  if (view==='map') return;
  text('radio-title',`Сигнал ${RadioScene.names[view]}`);
  const ip=receivers[view];
  const strength=radio&&Number.isFinite(radio.rssi)?` · ${radio.rssi} дБм`:'';
  text('radio-status',radio?`${ip}${strength}`:ip?`${ip} · нет свежих кадров`:'Укажите IP принимающей платы');
  const values=radio?useful.map(i=>radio.heights[i]).filter(Number.isFinite):null;
  RadioScene.drawSpectrum(byId('spectrum'),values?.length?values:null,RadioScene.colors[view]);
  const points=cell&&radio?posePoints(radio.skeleton):null;
  byId('pose-panel').hidden=!points;
  if (points) RadioScene.drawPose(byId('pose'),points);
  text('pose-status',points?'Экспериментальная 2D-оценка по CSI.':!radio||!cell?'':
   'Для выбранной платы нет пригодной оценки позы.');
 }

 function frame(now) {
  const result=positionAt(now);
  const cell=layout?.cellAt(result.point)??null;
  const radio=currentRadio(now);
  trail.update(cell,now);
  scene.draw(now,devices,trail.visible(now),cell?result.point:null,radio);
  if (now-lastHud>=120) {
   updateHud(result,cell,radio);
   lastHud=now;
  }
  requestAnimationFrame(frame);
 }

 connect('/','localization_status');
 connect('/ranging','ranging_web_status');
 requestAnimationFrame(frame);
})();
