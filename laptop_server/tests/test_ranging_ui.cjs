const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
 constructor() { this.children=[]; this.value=''; this.textContent=''; this.disabled=false; }
 addEventListener(name, callback) { this[name]=callback; }
 appendChild(child) { this.children.push(child); }
 replaceChildren(...children) { this.children=children; }
 querySelectorAll() { return []; }
 reportValidity() { return true; }
}
const elements = new Map();
const get = id => {
 if (!elements.has(id)) elements.set(id,new Element());
 return elements.get(id);
};
let socket;
class Socket {
 static OPEN=1;
 constructor() { socket=this; this.readyState=1; this.messages=[]; }
 send(raw) { this.messages.push(JSON.parse(raw)); }
}
const context=vm.createContext({
 document:{getElementById:get,createElement:()=>new Element()},
 location:{hostname:'127.0.0.1', search:''}, WebSocket:Socket, URLSearchParams,
 window:{confirm:()=>true}, setInterval:()=>{}, setTimeout:()=>1, clearTimeout:()=>{},
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/ranging.js'),'utf8'), context);
const state={type:'ranging_web', dataset:{id:'test',name:'Test'},room:null,points:[
 {id:'mark',role:'test',position:[1,2]}],devices:{},lines:[],windows:{},stale:[],model_ready:true,
 signal_ready:false,can_train:true,can_validate:true,training:{points:3,required:3,recorded:3,background:true}};
const receive = value => socket.onmessage({data:JSON.stringify(value)});
receive(state);
assert.match(get('result').textContent,/сигнал/);
get('train').onclick();
const training=socket.messages.at(-1);
assert.equal(training.cmd,'ranging_web_train');
assert.equal(get('train').disabled,true);
receive(state);
assert.equal(get('train').disabled,true);
receive({type:'ranging_web_error',request_id:training.request_id,message:'Ошибка обучения'});
assert.equal(get('trainingAction').textContent,'Ошибка обучения');
assert.equal(get('train').disabled,false);
get('tolerance').valueAsNumber=130;
get('validate').onclick();
const checking=socket.messages.at(-1);
assert.equal(checking.tolerance_mm,130);
receive({...state,request_id:checking.request_id,message:'Проверка завершена'});
assert.equal(get('trainingAction').textContent,'Проверка завершена');

get('delay').valueAsNumber=5;
get('seconds').valueAsNumber=15;
for(const id of ['background','empty_check']){
 get(id).onclick();
 assert.deepEqual(socket.messages.at(-1),{cmd:'ranging_web_record',dataset_id:'test',id,delay:5,seconds:15});
}
get('cancel').onclick();
assert.equal(socket.messages.at(-1).cmd,'ranging_web_cancel');
get('x').valueAsNumber=1;
get('z').valueAsNumber=2;
get('role').value='test';
get('pointForm').onsubmit({preventDefault(){}});
assert.deepEqual(socket.messages.at(-1),{cmd:'ranging_web_add',dataset_id:'test',role:'test',x:1,z:2});

const pointButtons=get('points').children[0].children.at(-1).children[0].children;
assert.equal(pointButtons[0].disabled,true);
for (const [i,action] of ['promote','delete'].entries()){
 pointButtons[i+1].onclick();
 assert.equal(socket.messages.at(-1).cmd,'ranging_web_'+action);
 assert.equal(socket.messages.at(-1).id,'mark');
}
get('train').onclick();
socket.readyState=3;
socket.onclose();
assert.match(get('trainingAction').textContent,/прервалась/);
assert.equal(get('train').disabled,true);
console.log('Ranging UI: actions, offline training, acknowledgements, errors and reconnect.');
