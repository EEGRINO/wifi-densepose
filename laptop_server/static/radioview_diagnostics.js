'use strict';

const RadioDiagnostics=(()=>{
 const fallback='Журнал на диске сервера: laptop_server/logs/.';
 const fileName=/^ruview-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.jsonl$/;

 function connect(url,button,status) {
  let socket, timer;
  const latest=new Map();

  function haveSocket() {return socket?.readyState===WebSocket.OPEN;}
  function send(message) {
   if (haveSocket()) socket.send(JSON.stringify(message));
  }
  function report(event,fields) {
   const key=event==='receiver'||event==='signal'?event+fields.view:event;
   const message={cmd:'diagnostics_event',event,...fields};
   const signature=JSON.stringify(message);
   if (latest.get(key)?.signature===signature) return;
   latest.set(key,{signature,message});
   send(message);
  }
  function finish(message) {
   clearTimeout(timer);
   timer=undefined;
   button.disabled=false;
   status.textContent=message;
  }
  function open() {
   socket=new WebSocket(url);
   socket.onopen=()=>{
    for (const {message} of latest.values()) send(message);
   };
   socket.onclose=()=>{
    if (timer!==undefined) finish(fallback);
    setTimeout(open,1500);
   };
   socket.onmessage=({data})=>{
    if (timer===undefined) return;
    let response;
    try {response=JSON.parse(data);} catch (_) {return;}
    if (response?.type!=='diagnostics_snapshot') return;
    if (!response.available||typeof response.content!=='string'||!fileName.test(response.name)) {
     finish(fallback);
     return;
    }
    const href=URL.createObjectURL(new Blob([response.content],{type:'application/x-ndjson;charset=utf-8'}));
    const link=document.createElement('a');
    link.href=href;
    link.download=response.name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(()=>URL.revokeObjectURL(href),1000);
    finish('Журнал скачан.');
   };
  }
  button.onclick=()=>{
   if (!haveSocket()) {finish(fallback);return;}
   button.disabled=true;
   status.textContent='Получаем журнал…';
   timer=setTimeout(()=>finish(fallback),5000);
   send({cmd:'diagnostics_snapshot'});
  };
  open();
  return {report};
 }

 function silhouetteReason(view,cell,radio,receiver,primary,joints,drawn) {
  if (view==='map') return 'map_view';
  if (!cell) return 'no_position';
  if (!radio?.hasSignal) return 'no_iq';
  if (drawn) return 'drawn';
  if (primary&&receiver!==primary) return 'other_receiver';
  return joints?'outside_view':'no_pose';
 }

 return {connect,silhouetteReason};
})();
