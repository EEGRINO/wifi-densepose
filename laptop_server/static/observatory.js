/**
 * RuView-style 3D «observatory»: wireframe avatar, grid, orbit camera.
 * Требует глобальный THREE (подключается с CDN в index.html).
 * Не настоящий DensePose — поза эвристически из полос CSI + Motion.
 */
(function (global) {
  'use strict';

  let scene, camera, renderer, lineGeom, lineMat, grid, amb, pointL, tmpV, joints;
  let inited = false;
  let canvasEl = null;

  const SEGMENTS = [
    ['pelvis', 'neck'],
    ['neck', 'head'],
    ['neck', 'shL'],
    ['shL', 'elL'],
    ['elL', 'hnL'],
    ['neck', 'shR'],
    ['shR', 'elR'],
    ['elR', 'hnR'],
    ['pelvis', 'hipL'],
    ['hipL', 'knL'],
    ['knL', 'ftL'],
    ['pelvis', 'hipR'],
    ['hipR', 'knR'],
    ['knR', 'ftR'],
  ];

  function setLinePositions() {
    const arr = lineGeom.attributes.position.array;
    let i = 0;
    for (let s = 0; s < SEGMENTS.length; s++) {
      const [a, b] = SEGMENTS[s];
      joints[a].toArray(arr, i);
      i += 3;
      joints[b].toArray(arr, i);
      i += 3;
    }
    lineGeom.attributes.position.needsUpdate = true;
  }

  function applyPose(bands, motion, t) {
    if (!joints || !lineGeom) return;
    const n = (i) => (bands[i] || 0);
    const low = (n(0) + n(1) + n(2) + n(3)) * 0.25;
    const mid = (n(4) + n(5) + n(6) + n(7)) * 0.25;
    const hi = (n(8) + n(9) + n(10) + n(11)) * 0.25;
    const m = Math.min(3, motion * 15 + mid * 0.02);
    const sway = Math.sin(t * 1.8) * (0.02 + m * 0.04);
    const w2 = Math.cos(t * 1.4) * (0.015 + m * 0.035);
    const asym = (hi - low) * 0.0008 * (1 + m);

    const pelY = 0.92 + sway * 0.08;
    joints.pelvis.set(0, pelY, 0);
    joints.neck.set(sway * 0.12 + asym * 0.05, pelY + 0.52 + mid * 0.0004, w2 * 0.06);
    joints.head.copy(joints.neck).add(
      tmpV.set(asym * 0.04, 0.22 + hi * 0.0003 + m * 0.02, w2 * 0.05)
    );

    const armBase = -0.35 - m * 0.5 - mid * 0.0002;
    const armSwingL = armBase + asym * 0.6 + w2 * 0.4;
    const armSwingR = -armBase - asym * 0.6 + w2 * 0.4;

    joints.shL.copy(joints.neck).add(tmpV.set(-0.18, -0.04, 0));
    joints.elL.copy(joints.shL).add(
      tmpV.set(Math.sin(armSwingL) * 0.28, -Math.cos(armSwingL) * 0.28, 0)
    );
    joints.hnL.copy(joints.elL).add(
      tmpV.set(Math.sin(armSwingL * 1.1) * 0.26, -Math.cos(armSwingL * 1.1) * 0.26, 0)
    );

    joints.shR.copy(joints.neck).add(tmpV.set(0.18, -0.04, 0));
    joints.elR.copy(joints.shR).add(
      tmpV.set(-Math.sin(armSwingR) * 0.28, -Math.cos(armSwingR) * 0.28, 0)
    );
    joints.hnR.copy(joints.elR).add(
      tmpV.set(-Math.sin(armSwingR * 1.1) * 0.26, -Math.cos(armSwingR * 1.1) * 0.26, 0)
    );

    const legS = 0.4 + m * 0.35 + low * 0.00015;
    joints.hipL.copy(joints.pelvis).add(tmpV.set(-0.12, -0.06, 0));
    joints.knL.copy(joints.hipL).add(tmpV.set(sway * 0.1, -legS * 0.55, 0));
    joints.ftL.copy(joints.knL).add(tmpV.set(asym * 0.06, -legS * 0.5, w2 * 0.08));

    joints.hipR.copy(joints.pelvis).add(tmpV.set(0.12, -0.06, 0));
    joints.knR.copy(joints.hipR).add(tmpV.set(-sway * 0.1, -legS * 0.55, 0));
    joints.ftR.copy(joints.knR).add(tmpV.set(-asym * 0.06, -legS * 0.5, w2 * 0.08));

    setLinePositions();
  }

  global.RuViewObsInit = function (canvas) {
    if (inited || !global.THREE || !canvas) return false;
    const T = global.THREE;
    tmpV = new T.Vector3();
    joints = {
      pelvis: new T.Vector3(),
      neck: new T.Vector3(),
      head: new T.Vector3(),
      shL: new T.Vector3(),
      elL: new T.Vector3(),
      hnL: new T.Vector3(),
      shR: new T.Vector3(),
      elR: new T.Vector3(),
      hnR: new T.Vector3(),
      hipL: new T.Vector3(),
      knL: new T.Vector3(),
      ftL: new T.Vector3(),
      hipR: new T.Vector3(),
      knR: new T.Vector3(),
      ftR: new T.Vector3(),
    };
    canvasEl = canvas;
    const w = canvas.clientWidth || 400;
    const h = canvas.clientHeight || 280;

    scene = new T.Scene();
    scene.background = new T.Color(0x070a12);
    scene.fog = new T.FogExp2(0x070a12, 0.045);

    camera = new T.PerspectiveCamera(42, w / h, 0.08, 80);
    camera.position.set(2.6, 1.55, 2.8);

    renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    renderer.setClearColor(0x070a12, 1);

    grid = new T.GridHelper(9, 18, 0x2a5080, 0x1a2538);
    grid.position.y = 0.01;
    scene.add(grid);

    const plane = new T.Mesh(
      new T.PlaneGeometry(14, 14),
      new T.MeshBasicMaterial({
        color: 0x0a1020,
        transparent: true,
        opacity: 0.45,
        depthWrite: false,
      })
    );
    plane.rotation.x = -Math.PI / 2;
    floor = plane;
    scene.add(plane);

    amb = new T.AmbientLight(0x335588, 0.35);
    scene.add(amb);
    pointL = new T.PointLight(0x4db2ff, 1.35, 14);
    pointL.position.set(1.8, 3.2, 1.5);
    scene.add(pointL);

    const cyan = 0x3d9eff;
    lineMat = new T.LineBasicMaterial({
      color: cyan,
      transparent: true,
      opacity: 0.95,
    });
    const pos = new Float32Array(SEGMENTS.length * 2 * 3);
    lineGeom = new T.BufferGeometry();
    lineGeom.setAttribute('position', new T.BufferAttribute(pos, 3));
    const wire = new T.LineSegments(lineGeom, lineMat);
    scene.add(wire);

    /* Светящиеся «суставы» */
    const jGeo = new T.SphereGeometry(0.045, 10, 10);
    const jMat = new T.MeshBasicMaterial({ color: 0x7ec8ff });
    ['head', 'hnL', 'hnR', 'ftL', 'ftR'].forEach((k) => {
      const m = new T.Mesh(jGeo, jMat);
      m.userData.jointKey = k;
      m.name = 'joint_' + k;
      scene.add(m);
    });

    inited = true;
    global.addEventListener('resize', onResize);
    return true;
  };

  function onResize() {
    if (!inited || !canvasEl) return;
    const parent = canvasEl.parentElement;
    const w = parent ? parent.clientWidth : 400;
    const h = Math.max(220, Math.round(w * 0.62));
    canvasEl.style.height = h + 'px';
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }

  global.RuViewObsResize = onResize;

  global.RuViewObsTick = function (bands12, motion, rssi) {
    if (!inited || !renderer || !joints) return;
    console.log("RSSI:", rssi);
    const t = global.performance.now() * 0.001;
    applyPose(bands12, motion, t);

    scene.traverse((o) => {
      if (o.userData.jointKey && joints[o.userData.jointKey]) {
        o.position.copy(joints[o.userData.jointKey]);
      }
    });

    const orbit = t * 0.11;
    const rad = 3.85;
    camera.position.x = Math.sin(orbit) * rad;
    camera.position.z = Math.cos(orbit) * rad;
    camera.position.y = 1.35 + Math.sin(t * 0.25) * 0.12;
    camera.lookAt(0, 1.05, 0);

    pointL.intensity = 1.1 + Math.min(0.9, motion * 8);
    lineMat.opacity = 0.75 + Math.min(0.25, motion * 3);

    renderer.render(scene, camera);
  };

  global.RuViewObsAvailable = function () {
    return !!global.THREE;
  };
})(window);
