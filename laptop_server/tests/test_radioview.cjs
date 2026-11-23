const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const scripts = ['room_editor.js','radioview_scene.js'].map(name=>readFileSync(join(__dirname,'../static',name),'utf8'));
const RadioScene = runInNewContext(`${scripts.join('\n')}\nRadioScene`);

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
