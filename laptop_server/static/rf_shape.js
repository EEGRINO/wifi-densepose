'use strict';

(() => {
 const el=name=>document.getElementById(`rf-shape-${name}`);
 const grid=el('grid'), output=el('output'), notice=el('notice');
 const selected=new Set();
 const draft=new URLSearchParams(location.hash.slice(1)).get('mask');
 let buttons=[], socket, pending='', state=null, timer, listed='', error='';
 const states={untrained:'Нужны размеченные записи формы', rejected:'Модель не прошла независимую проверку',
  recording:'Идёт запись формы', unknown:'Не хватает уверенности или свежих кадров CSI',
  estimated:'Оценка формы в одной вертикальной плоскости', empty:'Модель оценивает комнату как пустую'};

 function request(cmd, fields={}) {
  if (socket?.readyState!==WebSocket.OPEN) return;
  socket.send(JSON.stringify({cmd,...fields}));
  if (cmd==='rf_shape_status') return;
  pending=cmd;
  error='';
  if (state) renderSetup();
 }

 function draw() {
  const {estimate,cols,rows}=state;
  output.hidden=state.state!=='estimated';
  el('status').textContent=states[state.state];
  if (output.hidden) return;
  const ctx=output.getContext('2d');
  const w=output.width/cols,h=output.height/rows;
  ctx.fillStyle='#071018';ctx.fillRect(0,0,output.width,output.height);
  ctx.fillStyle='#94def5';
  for (const cell of estimate) ctx.fillRect((cell%cols)*w,Math.floor(cell/cols)*h,w-1,h-1);
 }

 function refreshSelection() {
  for (const [index,button] of buttons.entries()) button.setAttribute('aria-pressed',String(selected.has(index)));
 }

 function createGrid(cols,rows) {
  grid.style.gridTemplateColumns=`auto repeat(${cols},1fr)`;
  const axis=label=>{
   const span=document.createElement('span');
   span.className='shape-axis';
   span.textContent=label;
   span.setAttribute('aria-hidden','true');
   return span;
  };
  buttons=Array.from({length:cols*rows},(_,index)=>{
   const row=Math.floor(index/cols), column=String.fromCharCode(65+index%cols);
   const button=document.createElement('button');
   button.type='button';
   button.title=`${column}${row+1} · ${Math.round(200-(row+1)*200/rows)}–${Math.round(200-row*200/rows)} см от пола`;
   button.setAttribute('aria-label',button.title);
   button.onclick=()=>{
    if (!selected.delete(index)) selected.add(index);
    refreshSelection();
   };
   return button;
  });
  const cells=[axis(''),...Array.from({length:cols},(_,column)=>axis(String.fromCharCode(65+column)))];
  for (let row=0;row<rows;row++) cells.push(axis(row+1),...buttons.slice(row*cols,(row+1)*cols));
  grid.replaceChildren(...cells);
  refreshSelection();
 }

 function listing() {
  el('captures').replaceChildren(...state.captures.map(row=>{
   const item=document.createElement('li');
   item.textContent=`${row.role==='train'?'Обучение':'Проверка'} · ${row.cells.length?`${row.cells.length} клеток`:'Пусто'} · ${row.windows} окон`;
   const load=document.createElement('button'), remove=document.createElement('button');
   load.type=remove.type='button';
   load.textContent='Разметка';
   load.onclick=()=>{
    selected.clear();
    for (const cell of row.cells) selected.add(cell);
    refreshSelection();
   };
   remove.textContent='Удалить';
   remove.onclick=()=>request('rf_shape_delete',{id:row.id});
   item.append(load,remove);
   return item;
  }));
 }

 function renderSetup() {
  if (!grid) return;
  const open=socket.readyState===WebSocket.OPEN,busy=!!(state.recording||pending);
  el('record').disabled=!open||busy||!state.room_ready;
  el('cancel').disabled=!open||!state.can_cancel||!!pending;
  el('import').disabled=!open||busy||!state.room_ready;
  el('train').disabled=!open||busy||!state.captures.length;
  for (const button of [...buttons,...el('captures').querySelectorAll('button'),el('clear'),el('role')]) button.disabled=busy;
  const rec=state.recording;
  el('timer').textContent=rec?`${rec.phase==='waiting'?'Приготовьтесь':'Стойте спокойно'} · ${rec.left_s} с`:'';
  el('dataset').textContent=state.dataset;
  notice.textContent=error||state.message||(!state.room_ready?'Сохраните план помещения в радиовизоре.':'');
  const r=state.report;
  const pct=value=>`${Math.round(value*100)}%`;
  el('report').textContent=r?`Совпадение клеток на новых формах ${pct(r.iou)} · лучший шаблон из обучения ${pct(r.baseline)} · пустая комната ${pct(r.empty_accuracy)}`:'';
 }

 function announce(previous, next) {
  if (!grid) return;
  const before=previous?.can_cancel?previous.recording?.phase:null;
  const after=next.can_cancel?next.recording?.phase:null;
  if (before===after) return;
  const speech=window.speechSynthesis;
  const voice=speech?.getVoices().find(item=>item.localService&&/^ru(?:-|$)/i.test(item.lang));
  if (!voice) return;
  const text=after==='waiting'?'Займите положение.':after==='recording'?'Идёт запись. Стойте спокойно.':'Запись завершена.';
  const utterance=new SpeechSynthesisUtterance(text);
  utterance.voice=voice;
  speech.cancel();
  speech.speak(utterance);
 }

 if (grid) {
  el('clear').onclick=()=>{selected.clear();refreshSelection();};
  el('record').onclick=()=>request('rf_shape_record',{cells:[...selected],role:el('role').value});
  el('cancel').onclick=()=>request('rf_shape_cancel');
  el('import').onclick=()=>request('rf_shape_import_background');
  el('train').onclick=()=>request('rf_shape_train');
 }

 function connect() {
  socket=new WebSocket(`ws://${location.hostname||'127.0.0.1'}:8765/rf-shape`);
  socket.onopen=()=>{
   error='';
   request('rf_shape_status');
   timer=setInterval(()=>request('rf_shape_status'),900);
  };
  socket.onmessage=({data})=>{
   let message;
   try {message=JSON.parse(data);} catch (_) {return;}
   if (message.action===pending) pending='';
   if (message.type==='rf_shape_error') {
    error=message.message;
    if (state) renderSetup();
    return;
   }
   if (message.type!=='rf_shape') return;
   if (grid&&state?.scope!==message.scope) {
    selected.clear();
    if (!state&&draft&&/^\d+(,\d+)*$/.test(draft)) {
     for (const cell of draft.split(',').map(Number)) if (cell<message.cols*message.rows) selected.add(cell);
    }
    createGrid(message.cols,message.rows);
   }
   announce(state,message);
   state=message;
   draw();
   if (!grid) return;
   const signature=JSON.stringify(state.captures.map(row=>row.id));
   if (signature!==listed) {
    listed=signature;
    listing();
   }
   renderSetup();
  };
  socket.onclose=()=>{
   clearInterval(timer);
   pending='';
   error='Связь с сервером потеряна.';
   output.hidden=true;
   el('status').textContent=error;
   if (state) renderSetup();
   setTimeout(connect,1500);
  };
 }
 connect();
})();
