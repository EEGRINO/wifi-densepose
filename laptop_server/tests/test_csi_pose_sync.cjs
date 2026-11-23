const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const source = readFileSync(join(__dirname, '../static/csi_pose_sync.js'), 'utf8');
const createCsiPoseSync = runInNewContext(`${source}\ncreateCsiPoseSync`);

test('aligns by server receipt time despite delay delivering CSI or estimating pose', () => {
 const sync = createCsiPoseSync();
 sync.add({csi_received_ms: 980}, [1], 1050);
 sync.add({csi_received_ms: 1004}, [2], 1070);
 sync.add({csi_received_ms: 1000, csi_primary: false}, [3], 1080);
 sync.add({csi_received_ms: 1120}, [4], 1125);
 const pair = sync.pair(1000);
 assert.equal(pair.heights[0], 2);
 assert.equal(pair.gapMs, 4);
 assert.equal(sync.pair(1009), null);
});

test('uses client receipt for older servers and resets after a reconnection', () => {
 const sync = createCsiPoseSync();
 assert.equal(sync.add({csi_received_ms: 400}, [1], 1701), false);
 sync.add({}, [2], 1710);
 assert.equal(sync.pair(1629), null);
 assert.equal(sync.pair(1630).gapMs, 80);
 sync.reset();
 assert.equal(sync.pair(1710), null);
 sync.add({}, [3], 1700);
 assert.equal(sync.pair(1700).heights[0], 3);
});
