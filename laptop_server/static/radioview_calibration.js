'use strict';

const RadioCalibration = (() => {
 const letters=['А','Бэ','Цэ','Дэ','Е','Эф','Гэ','Аш','И','Жэ','Ка','Эль','Эм','Эн','О','Пэ'];

 function mount(byId, canvas, scene, showMap, send) {
  const el=name=>byId(`calibration-${name}`);
  const panel=byId('calibration'), picker=el('picker');
  const selected=new Set();
  const speech=window.speechSynthesis;
  let open=false, layout=null, plan='', connected=false, status=null, pending='', mine=false, spoken='', notice='';
  let selectable=new Map();

  const run=()=>status?.guided_calibration;
  const active=()=>run()?.state==='running';
  const cells=()=>Array.from(selected,label=>selectable.get(label));
  const target=()=>active()?layout?.cellAt(status.recording?.position):null;

  function suggestCells() {
   if (selectable.size<2) return;
   const axis=layout.width>=layout.depth?0:1;
   const offset=(axis===0?layout.width:layout.depth)/4;
   for (const distance of [-offset,offset]) {
    const point=layout.center.slice();
    point[axis]+=distance;
    const candidates=Array.from(selectable.values()).filter(cell=>!selected.has(cell.label));
    const distanceSquared=cell=>(cell.center[0]-point[0])**2+(cell.center[1]-point[1])**2;
    candidates.sort((a,b)=>distanceSquared(a)-distanceSquared(b));
    selected.add(candidates[0].label);
   }
  }

  function nearestReceiver(point) {
   const a=status.devices?.esp1?.position, b=status.devices?.esp2?.position;
   if (!a||!b) return '';
   const delta=Math.hypot(point[0]-a[0],point[1]-a[1])-Math.hypot(point[0]-b[0],point[1]-b[1]);
   return delta<-.5?'A':delta>.5?'B':'';
  }

  function say(message) {
   const voice=speech?.getVoices().find(item=>item.localService&&/^ru(?:-|$)/i.test(item.lang));
   if (!voice||typeof SpeechSynthesisUtterance==='undefined') {
    el('voice-status').hidden=false;
    el('voice-status').textContent='Русский голос недоступен в браузере. Следите за красной клеткой и таймером.';
    return;
   }
   const utterance=new SpeechSynthesisUtterance(message);
   utterance.voice=voice;
   utterance.lang=voice.lang;
   const showVoiceError=()=>{
    el('voice-status').hidden=false;
    el('voice-status').textContent='Голос не воспроизвёлся. Следите за красной клеткой и таймером.';
   };
   const timeout=setTimeout(showVoiceError,4000);
   utterance.onstart=()=>{
    clearTimeout(timeout);
    el('voice-status').hidden=true;
   };
   utterance.onerror=event=>{
    clearTimeout(timeout);
    if (event.error!=='canceled'&&event.error!=='interrupted') showVoiceError();
   };
   speech.cancel();
   speech.speak(utterance);
  }

  function renderChoices() {
   picker.replaceChildren(new Option('Выберите клетку',''),
    ...Array.from(selectable.keys()).filter(label=>!selected.has(label)).map(label=>new Option(label,label)));
   el('points').replaceChildren(...Array.from(selected,label=>{
    const button=document.createElement('button');
    button.type='button';
    button.textContent=`${label} ×`;
    button.setAttribute('aria-label',`Убрать клетку ${label}`);
    button.onclick=()=>toggleCell(label);
    return button;
   }));
  }

  function toggleCell(label) {
   if (!selectable.has(label)||active()||pending) return;
   if (!selected.delete(label)) selected.add(label);
   renderChoices();
   render();
  }

  function render() {
   const guided=run(), running=active(), rec=status?.recording;
   const step=running&&rec;
   const targetCell=target();
   panel.hidden=!open;
   el('open').disabled=!layout||running||!!pending;
   el('open').setAttribute('aria-expanded',String(open));
   el('setup').hidden=running;
   el('step').hidden=!step;
   canvas.dataset.selectCells=String(open&&!running&&!pending);
   const receivers=new Set((status?.active_lines??[]).map(line=>line.split(' <- ')[0]));
   const busy=!!(rec||status?.ranging_recording||pending);
   el('start').disabled=!connected||busy||selected.size<2||receivers.size<2;
   el('stop').disabled=!connected||!!pending;
   el('train').hidden=!status?.can_train||running||!!status.model_ready;
   el('train').disabled=!connected||busy;
   const message=notice||(!running&&guided?.message)||'';
   el('status').hidden=!message;
   el('status').textContent=message;
   if (!step) return;

   const waiting=rec.phase==='waiting';
   const nearer=targetCell?nearestReceiver(rec.position):'';
   const placement=targetCell?`Встаньте в центр клетки.${nearer?` Ближе к ESP ${nearer}.`:''}`:
    guided.completed?'Оставайтесь снаружи.':'Выйдите из комнаты.';
   el('progress').textContent=`Запись ${guided.completed+1} из ${guided.total}`;
   el('target').textContent=targetCell?`Клетка ${targetCell.label}`:'Пустая комната';
   el('instruction').textContent=waiting?`${placement} Запись начнётся через`:
    targetCell?'Идёт запись. Стойте спокойно.':'Идёт запись. Оставайтесь снаружи.';
   el('timer').textContent=`${rec.left_s} с`;
   const key=`${guided.id}/${guided.completed}/${rec.phase}`;
   if (!mine||spoken===key) return;
   spoken=key;
   const name=targetCell?`${letters[targetCell.label.charCodeAt(0)-65]} ${targetCell.label.slice(1)}`:'';
   say(waiting?(targetCell?`Клетка ${name}. Встаньте в центр.${nearer?` Ближе к Эс Пэ ${nearer==='A'?'А':'Бэ'}.`:''}`:placement):
    targetCell?'Запись. Стойте спокойно.':'Запись пустой комнаты.');
  }

  function request(action, payload) {
   if (!send(action,payload)) return;
   pending=action;
   notice='';
   render();
  }

  el('open').onclick=()=>{
   open=!open;
   if (open) showMap();
   render();
  };
  picker.onchange=event=>toggleCell(event.target.value);
  canvas.addEventListener('click',event=>{
   if (open&&!active()) toggleCell(scene.mapCellAt(event.clientX,event.clientY)?.label);
  });
  el('voice').onclick=()=>say('Голосовые подсказки готовы.');
  el('start').onclick=()=>{
   mine=true;
   const points=cells().map(cell=>cell.center);
   request('localization_guided_start',{dataset_id:status.dataset.id,points});
  };
  el('stop').onclick=()=>request('localization_cancel');
  el('train').onclick=()=>request('localization_train');

  return {
   active,
   dismiss() {
    if (!open) return;
    open=false;
    render();
   },
   overlay:()=>open?{cells:active()?[target()].filter(Boolean):cells(),target:target()}:null,
   update(next,nextLayout) {
    status=next;
    if (!connected) notice='';
    connected=true;
    layout=nextLayout;
    const nextPlan=JSON.stringify(next.room);
    if (nextPlan!==plan) {
     plan=nextPlan;
     selectable=new Map((layout?.labels??[]).map(cell=>[cell.label,cell]));
     selected.clear();
     suggestCells();
     renderChoices();
    }
    if (next.action&&next.action===pending) {
     if (pending==='localization_train') notice=next.message;
     pending='';
    }
    if (active()) {
     open=true;
     showMap();
    } else if (mine&&run()&&!pending) {
     say(run().state==='done'?'Калибровка завершена.':'Калибровка остановлена.');
     mine=false;
    }
    render();
   },
   error(event) {
    if (event.action!==pending) return;
    if (pending==='localization_guided_start') mine=false;
    pending='';
    notice=event.message;
    render();
   },
   disconnect() {
    if (mine) say('Связь с сервером потеряна. Калибровка остановлена.');
    mine=false;
    pending='';
    connected=false;
    status=null;
    notice='Нет связи с сервером.';
    render();
   },
  };
 }

 return {mount};
})();
