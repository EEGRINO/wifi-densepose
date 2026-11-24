'use strict';

(() => {
 const byId=id=>document.getElementById(id);
 const keys=['esp1','esp2'];
 const canvas=byId('room');
 const viewport=byId('viewport');
 const gainControl=byId('iq-trail-gain');
 const scene=RadioScene.scene(canvas), trail=RadioScene.trail();
 const poseSmoothing=RadioScene.poseSmoother();
 let rawSamples=new Map();
 const estimateLabels={empty:'Пустая комната',untrained:'Нужна калибровка',model_needed:'Нужна калибровка',
  waiting_signal:'Ожидание сигнала двух ESP',missing_lines:'Не хватает радиолиний',poor_signal:'Слабый сигнал',
  model_unreliable:'Модель требует проверки',background_failed:'Проверьте калибровку пустой комнаты',
  lines_changed:'Радиолинии изменились',unknown:'Зона не определена'};
 let connection=false, visorConnection=false, localization=null, ranging=null, presence=null, layout=null, layoutId='';
 let devices={}, view='map', receiverOptions='', receivers={esp1:'',esp2:''}, pointer=null, lastHud=0, primaryIp='';
 let requestedView={'#esp-a':'esp1','#esp-b':'esp2'}[location.hash];
 let localizationSocket=null;
 const calibration=typeof RadioCalibration==='undefined'?null:
  RadioCalibration.mount(byId,canvas,scene,()=>viewMode('map'),(cmd,fields)=>{
   if (localizationSocket?.readyState!==WebSocket.OPEN) return false;
   localizationSocket.send(JSON.stringify({cmd,...fields}));
   return true;
  });

 function text(id,value) {
  const el=byId(id);
  if (el.textContent!==value) el.textContent=value;
 }

 gainControl.oninput=()=>text('iq-trail-gain-value',`×${Number(gainControl.value).toLocaleString('ru-RU')}`);

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
  if (next!=='map'&&calibration?.active()) return;
  const aim=positionAt(now).point;
  if (!scene.select(next,devices,aim,now)) return;
  if (next!=='map') calibration?.dismiss();
  view=next;
  for (const key of ['map',...keys]) byId(`${key}-view`).setAttribute('aria-pressed',String(key===view));
  byId('radio-panel').hidden=view==='map';
  byId('full-view').hidden=view==='map';
  canvas.dataset.orbit=String(view!=='map');
  text('stage-hint',view==='map'?'Выберите ESP для обзора с её позиции.':'След изменений CSI на плоскости I/Q');
  byId('grid-label').hidden=!layout||view!=='map';
  updateViewHeading();
 }

 function updateViewHeading() {
  text('view-label',view==='map'?'План помещения':`${RadioScene.names[view]} · поворот ${scene.heading()}°`);
 }

 for (const id of ['map',...keys]) byId(`${id}-view`).onclick=()=>{
  requestedView=null;
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
   if (data.guided_calibration?.state==='running'&&!saved.esp1&&!saved.esp2) {
    saved=receivers;
    try {localStorage.setItem(storageKey(),JSON.stringify(saved));} catch (_) {}
   }
   receivers={esp1:typeof saved.esp1==='string'?saved.esp1:'',esp2:typeof saved.esp2==='string'?saved.esp2:''};
   if (receivers.esp2===receivers.esp1) receivers.esp2='';
  }
  for (const key of keys) byId(`${key}-view`).disabled=!layout||!devices[key];
  if (view!=='map'&&!devices[view]) viewMode('map',now);
  byId('no-plan').hidden=!!layout;
  byId('grid-label').hidden=!layout||view!=='map';
  text('grid-label',layout?`Клетка ${layout.size} × ${layout.size} м`:'');
  bindReceivers();
  calibration?.update(data,layout);
  if (requestedView&&layout&&devices[requestedView]&&!calibration?.active()) {
   viewMode(requestedView,now);
   requestedView=null;
  }
 }

 function rangingHint(state) {
  if (state.can_train&&!state.model_ready) return 'Записи готовы. Нажмите «Обучить по записям» на странице расстояний.';
  if (state.live_state==='model_needed') return 'На странице расстояний запишите фон и минимум три точки обучения.';
  if (state.live_state==='waiting_signal') return 'Модели нужны пригодные кадры с обучающих радиолиний.';
  if (state.live_state==='lines_changed') return 'Проверьте IP плат и канал роутера, использованные при записи точек.';
  if (state.live_state==='background_failed') return 'Контроль пустой комнаты показал ложные оценки. Проверьте записи фона.';
  if (state.live_reason==='near_background') return 'Сигнал близок к записи пустой комнаты.';
  if (state.live_reason==='outside_calibration') return 'Сигнал вне обученных точек. Проверьте, загорается ли клетка, если встать на записанную отметку.';
  return '';
 }

 function positionAt(now) {
  const loc=now-(localization?.receivedAt??-Infinity)<2700?localization:null;
  const estimate=loc?.estimate;
  if (estimate?.state==='estimated'&&layout?.cellAt(estimate.position)) {
   return {point:estimate.position,state:'estimated',source:'Локализация по CSI',model:estimate.mode==='ab'?'ab':'sectors'};
  }
  if (estimate?.mode==='ab'||estimate?.state==='empty') return {state:estimate.state,model:estimate.mode==='ab'?'ab':'sectors'};
  let detail='';
  if (loc&&ranging?.dataset?.id===loc.dataset?.id&&now-ranging.receivedAt<2700) {
   const point=ranging.estimate?.position;
   if (ranging.model_ready&&ranging.live_state==='estimated'&&layout?.cellAt(point)) {
    return {point,state:'estimated',source:'Оценка по точкам дальности',model:'ranging'};
   }
   detail=rangingHint(ranging);
   if (ranging.model_ready&&!loc.model_ready) return {state:ranging.live_state,detail,model:'ranging'};
  }
  if (loc?.model_ready) return {state:estimate?.state??'unknown',detail,model:'sectors'};
  if (now-(presence?.receivedAt??-Infinity)<2500&&presence.active_links>0) {
   if (presence.state==='empty') return {state:'empty',detail};
   if (presence.state==='moving'||presence.state==='still') return {state:presence.state,detail};
  }
  return {state:loc?estimate?.state??'untrained':'unknown',detail};
 }

 function accept(message) {
  const now=performance.now();
  if (message.type==='localization') setLocalization(message,now);
  else if (message.type==='localization_error') calibration?.error(message);
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
   const key=JSON.stringify([ip,packet.source,packet.channel,packet.sequence,packet.timestamp_us,packet.iq]);
   const previous=rawSamples.get(ip);
   const samples=previous?.key===key?previous.samples:VisorGrain.decode(packet.iq);
   if (!samples) continue;
   const hasSignal=previous?.key===key?previous.hasSignal:samples.some(value=>value!==0);
   const current=next.get(ip);
   // Prefer usable I/Q, then the frame type used for camera calibration.
   const keepCurrent=current&&(hasSignal!==current.hasSignal?current.hasSignal:
    (!current.peer&&packet.peer)||(packet.peer===current.peer&&age>=current.age_ms));
   if (!keepCurrent) next.set(ip,{...packet,key,samples,hasSignal,receivedAt:now-age});
  }
  rawSamples=next;
  bindReceivers();
 }

 const wsBase=`ws://${location.hostname||'127.0.0.1'}:8765`;
 const diagnostic=typeof RadioDiagnostics==='undefined'?null:
  RadioDiagnostics.connect(wsBase+'/diagnostics',byId('download-log'),byId('log-status'));
 function connect(path,command) {
  const socket=new WebSocket(wsBase+path);
  if (path==='/') localizationSocket=socket;
  let requestTimer, lastRequest=-Infinity, pending=false;
  function query() {
   lastRequest=performance.now();
   pending=true;
   socket.send(JSON.stringify({cmd:command}));
  }
  function connectionLabel(live) {
   if (path==='/visor') visorConnection=live;
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
    calibration?.disconnect();
   } else if (path==='/ranging') ranging=null;
   else {
    rawSamples.clear();
    bindReceivers();
   }
   setTimeout(()=>connect(path,command),1500);
  };
 }

 function currentRadio(now,key=view) {
  const packet=rawSamples.get(receivers[key]);
  return packet&&now-packet.receivedAt<1100?packet:null;
 }

 const wristJoints=[4,7];
 function wristVerified(wrists,index) {
  return Array.isArray(wrists)&&wrists.includes(index);
 }

 function posePoints(vector, wrists) {
  if (!Array.isArray(vector)||vector.length<32) return null;
  const points=Array.from({length:16},(_,i)=>{
   if (wristJoints.includes(i)&&!wristVerified(wrists,i)) return null;
   const point=vector.slice(i*2,i*2+2);
   return point.every(v=>Number.isFinite(v)&&v>=-.35&&v<=1.35)?point:null;
  });
  const visible=points.filter(Boolean);
  return visible.length>=2&&visible.some(p=>Math.hypot(p[0]-visible[0][0],p[1]-visible[0][1])>.04)?points:null;
 }

 function updateHud(result,cell,radio,joints) {
  const presenceLabel={moving:'Есть движение · зона неизвестна',still:'Присутствие · зона неизвестна'};
  const calibrating=calibration?.active();
  for (const key of keys) byId(`${key}-view`).disabled=!layout||!devices[key]||calibrating;
  text('position',!connection?'Нет связи с сервером':!layout?'Нужен план помещения':calibrating?'Идёт калибровка':cell?`Клетка ${cell.label}`:
   presenceLabel[result.state]??estimateLabels[result.state]??'Зона не определена');
  text('position-source',cell?result.source:layout&&!calibrating?result.detail??'':'');
  if (view==='map') return;
  text('radio-title',`Сигнал ${RadioScene.names[view]}`);
  const ip=receivers[view];
  const strength=radio&&Number.isFinite(radio.rssi)?` · ${radio.rssi} дБм`:'';
  const frameStatus=radio?.hasSignal?` · получено ${radio.samples.length/2} отсчётов I/Q`:' · нулевой кадр I/Q';
  text('radio-status',radio?`${ip}${strength} · канал ${radio.channel}${frameStatus}`:ip?`${ip} · нет свежих кадров I/Q`:'Укажите IP принимающей платы');
  const values=radio?VisorGrain.values(radio.samples).amplitude:null;
  RadioScene.drawSpectrum(byId('spectrum'),values,RadioScene.colors[view]);
  byId('pose-panel').hidden=!joints;
  const awaitingCalibration=radio?.hasSignal&&!radio.peer&&ip===primaryIp&&!radio.pose;
  const unverifiedWrists=joints&&wristJoints.some(i=>Number.isFinite(radio?.pose?.[i*2+1])&&
   !wristVerified(radio.pose_wrists,i));
  byId('pose-calibration').hidden=!awaitingCalibration&&!unverifiedWrists;
  let poseStatus='';
  if (joints) {
   poseStatus=`Оценка по CSI · ${joints.filter(Boolean).length} точек`;
   if (unverifiedWrists) poseStatus+=' · положение запястий не подтверждено';
  }
  else if (radio?.hasSignal) {
   if (primaryIp&&ip!==primaryIp) poseStatus=`Для оценки позы выберите основной ESP с IP ${primaryIp}.`;
   else if (awaitingCalibration) poseStatus='Нужна калибровка позы.';
   else poseStatus='Ожидание пригодной оценки позы.';
  }
  text('pose-status',poseStatus);
 }

 function reportDiagnostics(now,result,cell,radio,joints,drawn) {
  if (!diagnostic) return;
  diagnostic.report('view',{view,receiver:receivers[view]});
  for (const key of keys) {
   const receiver=receivers[key];
   diagnostic.report('receiver',{view:key,receiver});
   const packet=currentRadio(now,key);
   const state=packet?.hasSignal?'ready':!visorConnection?'disconnected':
    !receiver?'no_receiver':packet?'zero_iq':'no_iq';
   diagnostic.report('signal',{view:key,receiver,state,iq_count:packet?.samples.length/2||0});
  }
  const state=!connection?'disconnected':!layout?'no_layout':result.state;
  diagnostic.report('cell',{view,state,cell:cell?.label,model:result.model});
  const reason=RadioDiagnostics.silhouetteReason(view,cell,radio,receivers[view],primaryIp,joints,drawn);
  diagnostic.report('silhouette',{view,receiver:receivers[view],reason});
 }

 function frame(now) {
  const result=positionAt(now);
  const calibrating=calibration?.active();
  const cell=calibrating?null:(layout?.cellAt(result.point)??null);
  const radio=currentRadio(now);
  const target=!calibrating&&radio?.hasSignal?posePoints(radio.pose,radio.pose_wrists):null;
  const poseSource=target?JSON.stringify([layoutId,view,radio.receiver,radio.source,radio.channel]):null;
  const joints=poseSmoothing.update(target,poseSource,now);
  const roomJoints=cell&&joints?.every(Boolean)?joints:null;
  trail.update(cell,now);
  const drawn=scene.draw(now,devices,calibrating?[]:trail.visible(now),cell?result.point:null,radio,roomJoints,
   gainControl.valueAsNumber,calibration?.overlay());
  if (now-lastHud>=120) {
   updateHud(result,cell,radio,joints);
   reportDiagnostics(now,result,cell,radio,joints,drawn);
   lastHud=now;
  }
  if (joints) RadioScene.drawPose(byId('pose'),joints);
  requestAnimationFrame(frame);
 }

 connect('/','localization_status');
 connect('/ranging','ranging_web_status');
 connect('/visor','visor_snapshot');
 requestAnimationFrame(frame);
})();
