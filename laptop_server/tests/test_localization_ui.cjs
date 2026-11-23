// Structural UI checks with a minimal DOM; no claim of browser rendering QA.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'static', 'localization.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.attrs={};this.value='';this.textContent='';this.disabled=false;}
 setAttribute(k,v){this.attrs[k]=String(v);}
 getAttribute(k){return this.attrs[k]??null;}
 setPointerCapture(){}
 appendChild(e){this.children.push(e);return e;}
 replaceChildren(...e){this.children=e;}
 addEventListener(name,fn){this[name]=fn;}
 createSVGPoint(){return {x:0,y:0,matrixTransform(){return {x:this.x,y:this.y};}};}
 getScreenCTM(){return {inverse(){return {};}};}
}
const elements=new Map();
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
 createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag)};
const sockets=[];
class Socket{
 constructor(){this.readyState=1;this.messages=[];sockets.push(this);}
 send(s){this.messages.push(JSON.parse(s));}
}
let destination;
const context=vm.createContext({document,WebSocket:Socket,location:{hostname:'127.0.0.1',assign:value=>destination=value},
 setTimeout:()=>{},console});
const editorSource=fs.readFileSync(require('node:path').join(__dirname,'..','static','room_editor.js'),'utf8');
vm.runInContext(editorSource,context);
vm.runInContext(source,context);
const room={width:6.46,depth:2.87,cols:3,rows:2,shape:'curved',
 short_width:4.04,declared_area:16.8,sketch_area:17.05};
const state={type:'localization',room,lines:[],active_lines:[],recording:null,
 sessions:[],model_ready:false,report:null,message:'План сохранён',
 estimate:{state:'untrained',position:null,sector:null}};
context.input=state;
vm.runInContext('render(input)',context);
assert.equal(elements.get('record').disabled,true);
assert.match(elements.get('estimate').textContent,/Нужны записи/);
assert.ok(elements.get('map').children.some(e=>e.tag==='path'));
assert.ok(elements.get('map').children.some(e=>e.tag==='defs'));
assert.equal(vm.runInContext('inside(state.room,6.4,2.8)',context),false);
assert.equal(vm.runInContext('inside(state.room,1,1)',context),true);
const centers=[];
for(let i=0;i<6;i++){
 elements.get('centerPoints').onclick();
 centers.push(vm.runInContext('selected.slice()',context));
 assert.equal(vm.runInContext('inside(state.room,...selected)',context),true);
}
assert.equal(new Set(centers.map(p=>p.join(','))).size,6);
state.active_lines=['a <- 0001 / ch6','b <- 0002 / ch6'];
vm.runInContext('render(input)',context);
assert.equal(elements.get('record').disabled,false);
// A diagnostic preset must clear an old point and never start a recording itself.
const messagesBeforePreset=sockets[0].messages.length;
elements.get('diagB').onclick();
assert.equal(vm.runInContext('selected[0]',context),5.35);
assert.equal(vm.runInContext('selected[1]',context),.72);
assert.equal(elements.get('role').value,'diagnostic');
assert.equal(elements.get('delay').value,'30');
assert.equal(elements.get('seconds').value,'30');
elements.get('diagA').onclick();
assert.equal(vm.runInContext('selected[0]',context),1);
assert.equal(vm.runInContext('selected[1]',context),.92);
elements.get('diagEmpty').onclick();
assert.equal(vm.runInContext('selected',context),null);
assert.equal(elements.get('humanX').value,'');
assert.equal(elements.get('humanZ').value,'');
assert.equal(elements.get('seconds').value,'60');
assert.equal(sockets[0].messages.length,messagesBeforePreset);
elements.get('record').onclick();
const emptyMessage=sockets[0].messages.at(-1);
assert.equal(emptyMessage.cmd,'localization_record');
assert.equal(emptyMessage.x,null);
assert.equal(emptyMessage.z,null);
assert.equal(emptyMessage.role,'diagnostic');
assert.equal(emptyMessage.delay,30);
assert.equal(emptyMessage.seconds,60);
state.recording={phase:'waiting',left_s:15,windows:0};
vm.runInContext('render(input)',context);
assert.equal(elements.get('record').disabled,true);
assert.equal(elements.get('diagB').disabled,true);
elements.get('diagB').onclick();
assert.equal(vm.runInContext('selected',context),null);
assert.match(elements.get('timer').textContent,/15/);
state.recording=null;
state.estimate={state:'estimated',position:[1,1],sector:'1:1'};
vm.runInContext('render(input)',context);
assert.match(elements.get('estimate').textContent,/X 1.00/);
state.quality={blocked:false,lines:{'a <- 0001 / ch6':{
 rssi_dbm:-45,noise_dbm:-95,snr_est_db:50,packets_per_window:100,rejected_frames:0,ok:true,reasons:[]}},
 receivers:{a:{loss_ratio:0,duplicates:0,reordered:0,resets_seen:0}}};
vm.runInContext('render(input)',context);
assert.match(elements.get('qualitySummary').textContent,/в норме/);
assert.equal(elements.get('qualityLines').children.length,1);
state.quality.blocked=true;
state.quality.lines['a <- 0001 / ch6'].ok=false;
state.quality.lines['a <- 0001 / ch6'].reasons=['low_snr'];
state.estimate={state:'poor_signal',position:null};
vm.runInContext('render(input)',context);
assert.match(elements.get('estimate').textContent,/качество/);
assert.match(elements.get('qualitySummary').textContent,/блокируются/);
// Human coordinates must not depend on exact pixel clicks.
elements.get('humanX').value='3.23';
elements.get('humanZ').value='0.718';
elements.get('selectCoordinates').onclick();
assert.equal(vm.runInContext('selected[0]',context),3.23);
assert.equal(vm.runInContext('selected[1]',context),.718);
elements.get('humanX').value='6.4';
elements.get('humanZ').value='2.8';
assert.equal(elements.get('selectCoordinates').onclick(),false);
assert.equal(vm.runInContext('selected[0]',context),3.23);
// Place hardware without changing the selected human calibration point.
const personPoint=vm.runInContext('selected.slice()',context);
elements.get('mapMode').value='router';
elements.get('mapMode').onchange();
elements.get('map').click({clientX:1.5,clientY:.4});
let placed=sockets[0].messages.at(-1);
assert.equal(placed.cmd,'localization_device');
assert.equal(placed.device,'router');
assert.equal(placed.x,1.5);
assert.equal(placed.z,.4);
assert.equal(placed.height,null);
assert.deepEqual(vm.runInContext('selected.slice()',context),personPoint);
assert.equal(elements.get('record').disabled,true);
state.devices={router:{position:[1.5,.4],height:null}};
vm.runInContext('render(input)',context);
assert.ok(elements.get('map').children.some(e=>e.tag==='rect'&&e.attrs.fill==='#c299ff'));
elements.get('mapMode').value='esp1';
elements.get('mapMode').onchange();
elements.get('deviceHeight').value='1.25';
elements.get('map').click({clientX:.5,clientY:2});
placed=sockets[0].messages.at(-1);
assert.equal(placed.device,'esp1');
assert.equal(placed.height,1.25);
state.devices.esp1={position:[.5,2],height:1.25};
vm.runInContext('render(input)',context);
assert.ok(elements.get('map').children.some(e=>e.tag==='line'));
const before=sockets[0].messages.length;
elements.get('map').click({clientX:6.4,clientY:2.8});
assert.equal(sockets[0].messages.length,before);
assert.match(elements.get('error').textContent,/внутри/);
elements.get('removeDevice').onclick();
assert.equal(sockets[0].messages.at(-1).x,null);
elements.get('mapMode').value='person';
elements.get('mapMode').onchange();
assert.equal(elements.get('record').disabled,false);
state.sessions=[{position:[1,1],role:'train',windows:20}];
elements.get('mapMode').value='esp1';
elements.get('mapMode').onchange();
assert.equal(elements.get('saveDevice').disabled,true);
state.sessions=[];

// Edit a recorded plan without overwriting its layout or recordings.
state.dataset={id:'original',name:'Тест'};
state.sessions=[{position:[1,1],role:'train',windows:20}];
vm.runInContext('render(input)',context);
elements.get('editRoom').onclick();
assert.equal(elements.get('roomTools').hidden,false);
assert.equal(elements.get('saveRoomEdit').disabled,true);
const map=elements.get('map');
const handle=(kind,index)=>map.children.find(e=>e.attrs['data-plan-kind']===kind&&e.attrs['data-plan-index']===String(index));
const choose=(kind,index)=>map.pointerdown({button:0,pointerId:1,target:handle(kind,index),preventDefault(){}});
function drag(kind,index,x,z){
 choose(kind,index);
 map.pointermove({pointerId:1,clientX:x,clientY:z});
 map.pointerup({pointerId:1});
}
drag('corner',1,6.7,0);
assert.equal(vm.runInContext('plan.draft.outline[1][0]',context),6.7);
choose('wall',0);
elements.get('addBend').onclick();
drag('curve',0,3.25,.5);
elements.get('addCutout').onclick();
map.click({clientX:1,clientY:1});
map.click({clientX:2,clientY:2});
assert.equal(vm.runInContext('inside(plan.draft,1.5,1.5)',context),false);
assert.equal(vm.runInContext('inside(plan.draft,3,.1)',context),false);
assert.equal(vm.runInContext('inside(plan.draft,...RoomPlan.sectorPoint(plan.draft,0,0))',context),true);
assert.equal(elements.get('saveRoomEdit').textContent,'Сохранить как новый план');
elements.get('saveRoomEdit').onclick();
const savedPlan=sockets[0].messages.at(-1);
assert.equal(savedPlan.cmd,'localization_config');
assert.equal(savedPlan.base_dataset_id,'original');
assert.equal(savedPlan.fork_if_recorded,true);
assert.deepEqual(savedPlan.curves[0],[3.25,.5]);
assert.deepEqual(savedPlan.cutouts,[[1,1,2,2]]);
sockets[0].onmessage({data:JSON.stringify({type:'localization_error',message:'Проверьте контур'})});
assert.equal(elements.get('roomEditError').textContent,'Проверьте контур');
assert.equal(elements.get('saveRoomEdit').disabled,false);
state.room=JSON.parse(vm.runInContext('JSON.stringify(plan.draft)',context));
state.dataset={id:'copy',name:'Изменённый план'};
state.sessions=[];
vm.runInContext('render(input)',context);
assert.equal(elements.get('roomTools').hidden,true);
assert.equal(vm.runInContext('inside(state.room,1.5,1.5)',context),false);
assert.ok(map.children.some(e=>e.tag==='path'&&e.attrs['fill-rule']==='evenodd'));

// Snap a dragged point on both axes, with Alt available to bypass the guides.
elements.get('editRoom').onclick();
choose('corner',0);
map.pointermove({pointerId:1,clientX:.04,clientY:.06});
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(plan.draft.outline[0])',context)),[0,0]);
assert.equal(map.children.filter(e=>e.attrs['data-snap-guide']==='true').length,2);
map.pointermove({pointerId:1,clientX:.04,clientY:.06,altKey:true});
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(plan.draft.outline[0])',context)),[.04,.06]);
assert.equal(map.children.filter(e=>e.attrs['data-snap-guide']==='true').length,0);
map.pointerup({pointerId:1});
elements.get('cancelRoomEdit').onclick();

// Start a fresh drawing independently of the saved shape and equipment.
elements.get('newRoom').onclick();
assert.equal(vm.runInContext('plan.drawing',context),true);
assert.equal(vm.runInContext('plan.draft.outline.length',context),0);
assert.equal(vm.runInContext('plan.draft.cutouts.length',context),0);
assert.equal(elements.get('saveRoomEdit').hidden,true);
map.click({clientX:.03,clientY:.05});
map.click({clientX:4,clientY:.06});
map.click({clientX:3.95,clientY:3});
map.click({clientX:.05,clientY:2.98});
elements.get('undoRoomPoint').onclick();
assert.equal(vm.runInContext('plan.draft.outline.length',context),3);
map.click({clientX:.05,clientY:2.98});
map.click({clientX:.03,clientY:.04});
assert.equal(vm.runInContext('plan.drawing',context),false);
assert.ok(!map.children.some(e=>e.attrs.fill==='#c299ff'));
elements.get('saveRoomEdit').onclick();
const newPlan=sockets[0].messages.at(-1);
assert.equal(newPlan.new_plan,true);
assert.equal(newPlan.base_dataset_id,'copy');
assert.deepEqual(newPlan.outline,[[0,0],[4,0],[4,3],[0,3]]);
assert.deepEqual(newPlan.cutouts,[]);
state.room=JSON.parse(vm.runInContext('JSON.stringify(plan.draft)',context));
state.dataset={id:'new',name:'План'};
vm.runInContext('render(input)',context);
elements.get('newRoom').onclick();
elements.get('newPlanWidth').value='5.3';
elements.get('newPlanDepth').value='2.7';
elements.get('newPlanWidth').oninput();
elements.get('rectangleRoom').onclick();
assert.equal(vm.runInContext('plan.draft.width',context),5.3);
assert.equal(vm.runInContext('plan.draft.depth',context),2.7);
elements.get('cancelRoomEdit').onclick();
assert.equal(vm.runInContext('state.room.width',context),4);


assert.equal(newPlan.sector_mode,'auto');

// An offset outline must still have six equal grid boxes with centers inside.
const offset=[[1.7,2],[7.1,2],[7.1,5],[1.7,5]];
state.room={shape:'custom',sector_mode:'auto',cols:3,rows:2,width:7.1,depth:5,
 outline:offset,boundary:offset,curves:[null,null,null,null],cutouts:[],sketch_area:16.2};
vm.runInContext('render(input)',context);
const cells=map.children.filter(e=>e.tag==='rect'&&e.attrs['clip-path']==='url(#roomClip)');
assert.equal(cells.length,6);
assert.equal(Number(cells[0].attrs.x),1.7);
assert.equal(Number(cells[0].attrs.y),2);
assert.ok(cells.every(e=>Math.abs(Number(e.attrs.width)-1.8)<1e-8&&Number(e.attrs.height)===1.5));
assert.equal(elements.get('width').value,5.4);
assert.equal(elements.get('depth').value,3);
vm.runInContext('nextCenter=0',context);
elements.get('centerPoints').onclick();
assert.ok(Math.abs(vm.runInContext('selected[0]',context)-2.6)<1e-8);
assert.equal(vm.runInContext('selected[1]',context),2.75);
assert.equal(elements.get('cols').disabled,true);
assert.equal(elements.get('rows').disabled,true);

state.can_train=false;
state.ranging_points=3;
vm.runInContext('render(input)',context);
assert.equal(elements.get('train').disabled,false);
const beforeTraining=sockets[0].messages.length;
elements.get('train').onclick();
assert.equal(destination,'ranging.html?train=new');
assert.equal(sockets[0].messages.length,beforeTraining);
state.can_train=true;
vm.runInContext('render(input)',context);
elements.get('train').onclick();
assert.equal(sockets[0].messages.at(-1).cmd,'localization_train');
sockets[0].onmessage({data:JSON.stringify({...state,action:'localization_train'})});
assert.match(elements.get('sectorTrainStatus').textContent,/обучена/);

const commandsByButton={newDataset:'localization_dataset_new',openDataset:'localization_dataset_open',
 visitA:'localization_visit_a',visitB:'localization_visit_b',cancel:'localization_cancel',
 abStart:'localization_ab_start',abStop:'localization_ab_stop'};
for(const [control,cmd] of Object.entries(commandsByButton)){
 elements.get(control).onclick();
 assert.equal(sockets[0].messages.at(-1).cmd,cmd);
}
const buttons=[...html.matchAll(/<button\b[^>]*\bid="([^"]+)"/g)].map(match=>match[1]);
for(const id of buttons)assert.equal(typeof elements.get(id)?.onclick,'function',id);
sockets[0].onclose();
assert.match(elements.get('estimate').textContent,/Нет связи/);
assert.match(elements.get('qualitySummary').textContent,/неактуальны/);
assert.equal(vm.runInContext('state.estimate.position',context),null);
console.log('UI checks passed: curved geometry, six valid centers, recording guards, timer, stale-point removal.');
