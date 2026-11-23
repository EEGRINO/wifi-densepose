'use strict';

// Time alignment follows the measurement principle of Person-in-WiFi (ICCV 2019).
// The capture protocol and this implementation use RuView's local clock.
function createCsiPoseSync(maxGapMs = 80) {
 const retentionMs = 1200;
 const frames = [];
 let pairedThrough = -Infinity;
 return {
  add(packet, heights, receivedAt) {
   if (packet.csi_primary === false) return false;
   const at = Number.isFinite(packet.csi_received_ms) ? packet.csi_received_ms : receivedAt;
   if (Math.abs(receivedAt - at) > retentionMs) return false;
   frames.push({at, heights});
   while (frames.length && receivedAt - frames[0].at > retentionMs) frames.shift();
   return true;
  },
  pair(capturedAt) {
   if (!Number.isFinite(capturedAt)) return null;
   let best = null;
   let gap = maxGapMs + 1;
   for (const frame of frames) {
    const delta = Math.abs(capturedAt - frame.at);
    if (frame.at > pairedThrough && delta <= maxGapMs && delta < gap) {
     best = frame;
     gap = delta;
    }
   }
   if (!best) return null;
   pairedThrough = best.at;
   return {heights:best.heights, gapMs:gap};
  },
  reset() {
   frames.length = 0;
   pairedThrough = -Infinity;
  }
 };
}
