const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const scripts = ['room_editor.js','visor_grain.js','radioview_scene.js'].map(name=>readFileSync(join(__dirname,'../static',name),'utf8'));
const {RadioScene,VisorGrain}=runInNewContext(`${scripts.join('\n')}\n({RadioScene,VisorGrain})`,{atob});

test('uses a valid ranging estimate when the sector model cannot see its trained lines', () => {
 const elements=new Map(), sockets=new Map();
 const get=id=>{
  if (!elements.has(id)) elements.set(id,{dataset:{},addEventListener(){},setAttribute(){},replaceChildren(){}});
  return elements.get(id);
 };
 let draw;
 class Socket {
  constructor(url) {sockets.set(new URL(url).pathname,this);}
  send() {}
 }
 const scene={setLayout(){},select(){return true;},draw(){}};
 runInNewContext(readFileSync(join(__dirname,'../static/radioview.js'),'utf8'),{
  RadioScene:{...RadioScene,scene:()=>scene}, VisorGrain,
  document:{getElementById:get,addEventListener(){}}, location:{hostname:'127.0.0.1'},
  WebSocket:Socket, Option:class {}, setInterval(){}, performance:{now:()=>100},
  requestAnimationFrame:callback=>{draw=callback;},
 });
 const send=(path,message)=>sockets.get(path).onmessage({data:JSON.stringify(message)});
 sockets.get('/').onopen();
 const sectors={type:'localization',dataset:{id:'test'},room:{width:6,depth:4},devices:{},model_ready:true,
  estimate:{state:'missing_lines'}};
 const distance={type:'ranging_web',dataset:sectors.dataset,model_ready:true,live_state:'estimated',estimate:{position:[3.4,2.5]}};
 send('/',sectors);
 send('/ranging',distance);
 draw(240);
 assert.equal(get('position').textContent,'Клетка D3');
 assert.equal(get('position-source').textContent,'Оценка по точкам дальности');
 send('/',{...sectors,estimate:{state:'ab_present_unknown',mode:'ab'}});
 draw(380);
 assert.equal(get('position').textContent,'Зона не определена');
 send('/',{...sectors,estimate:{state:'empty'}});
 draw(520);
 assert.equal(get('position').textContent,'Пустая комната');
 send('/',sectors);
 send('/ranging',{...distance,live_state:'waiting_signal'});
 draw(670);
 assert.equal(get('position').textContent,'Не хватает радиолиний');
 send('/ranging',{...distance,dataset:{id:'another-plan'}});
 draw(810);
 assert.equal(get('position').textContent,'Не хватает радиолиний');
});

test('lays out square cells, clips the outline and leaves cutouts unoccupied', () => {
 const grid = RadioScene.grid({shape:'custom',boundary:[[1,1],[8,1],[8,4.1],[1,4.1]],cutouts:[[2,2,3,3]]});
 assert.equal(grid.size,1);
 assert.equal(grid.cellAt([1.6,1.3]).label,'A1');
 assert.equal(grid.cellAt([8,4.1]).label,'G4');
 assert.equal(grid.cellAt([1.9,4.4]),null);
 assert.equal(grid.cellAt([2.5,2.5]),null);
 const first=grid.cells[0].polygon;
 assert.equal(first[1][0]-first[0][0],first[2][1]-first[1][1]);
 const curved=RadioScene.grid({shape:'curved',width:6,depth:4,short_width:3});
 assert.equal(curved.cellAt([6,4]),null);
 assert.equal(curved.cellAt([2,3]).label,'C4');
 const large=RadioScene.grid({width:50,depth:50});
 assert.ok(large.cells.length<=256);
});

test('keeps the active cell lit, fades earlier cells and clears on a layout change', () => {
 const grid=RadioScene.grid({width:4,depth:3});
 const first=grid.cellAt([.3,.3]), second=grid.cellAt([1.4,.3]);
 const trail=RadioScene.trail();
 trail.update(first,0);
 assert.equal(trail.visible(1000)[0].alpha,1);
 trail.update(second,1200);
 const [old,current]=trail.visible(1300);
 assert.equal(old.cell,first);
 assert.equal(current.cell,second);
 assert.equal(current.alpha,1);
 assert.ok(trail.visible(2300)[0].alpha<old.alpha);
 assert.equal(trail.visible(3700).length,1);
 trail.update(null,4000);
 assert.ok(trail.visible(4100)[0].alpha<1);
 trail.clear();
 assert.equal(trail.visible(4200).length,0);
});

test('decodes raw signed I/Q and changes visual texture when the raw signal changes', () => {
 const iq=VisorGrain.decode(Buffer.from([128,0,255,127]).toString('base64'));
 assert.deepEqual(Array.from(iq),[-128,0,-1,127]);
 const original=VisorGrain.values(iq);
 assert.ok(original.amplitude[0] > original.amplitude[1]);
 iq[0]=127;
 assert.notEqual(original.seed,VisorGrain.values(iq).seed);
 assert.equal(VisorGrain.decode('!!!'),null);
 assert.equal(VisorGrain.decode('AA=='),null);
});

test('gives weak nonzero I/Q a visible exposure and keeps zero data black', () => {
 const weak=VisorGrain.values(new Int8Array([0,0,0,0,1,0,-1,0,0,1,1,1])).amplitude;
 const before=Array.from(weak);
 const exposure=VisorGrain.displayExposure(weak);
 assert.ok(exposure>10);
 assert.ok(weak[2]*exposure>=.5);
 assert.equal(weak[0]*exposure,0);
 assert.deepEqual(Array.from(weak),before);
 assert.equal(VisorGrain.displayExposure(new Float32Array(64)),0);
});

test('requires a position and live pose to project a silhouette and hides it behind the view', () => {
 const points=Array.from({length:16},(_,i)=>[.4+i*.01,.2+i*.03]);
 const forward=([x,y])=>[200+x,320-y*100];
 assert.equal(VisorGrain.projectPose(forward,null,points,600,450),null);
 assert.equal(VisorGrain.projectPose(forward,[3,4],null,600,450),null);
 assert.equal(VisorGrain.projectPose(()=>null,[3,4],points,600,450),null);
 const projected=VisorGrain.projectPose(forward,[3,4],points,600,450);
 assert.equal(projected.points.length,16);
 assert.equal(projected.points[15][1],320);
});
