import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { CSS3DRenderer, CSS3DObject } from "three/addons/renderers/CSS3DRenderer.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

/* =========================================================================
   Data
   ========================================================================= */
const $ = (id) => document.getElementById(id);
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
let STATE = null;

const money = (x, dp = 0) =>
  (x < 0 ? "−$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const signedMoney = (x) => (x >= 0 ? "+" : "−") + money(Math.abs(x));
const pct = (x, dp = 1) => {
  const v = Math.abs(x * 100).toFixed(dp);
  return (Number(v) === 0 ? "" : x >= 0 ? "+" : "−") + v + "%";
};
const cls = (x) => (x >= 0 ? "up" : "down");
const num1 = (x) => String(Math.round(x * 100) / 100);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// "server": the always-on server (live prices, password-protected settings)
// "static": GitHub Pages reading data/state.json; "preview": data baked into the page
let MODE = "static";

async function loadState() {
  if (window.__STATE__) {
    MODE = "preview";
    return window.__STATE__;
  }
  for (let attempt = 0; attempt < 20; attempt++) {
    let res;
    try {
      res = await fetch("api/state", { cache: "no-store" });
    } catch {
      break;
    }
    if (res.ok) {
      MODE = "server";
      return res.json();
    }
    if (res.status !== 503) break;
    $("banner").textContent = "The city is loading market data…";
    $("banner").hidden = false;
    await new Promise((r) => setTimeout(r, 3000));
  }
  const res = await fetch("data/state.json", { cache: "no-store" });
  if (!res.ok) throw new Error(`Could not load data/state.json (${res.status})`);
  return res.json();
}

/* =========================================================================
   GitHub connection (settings saves + Run now)
   ========================================================================= */
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const gh = {
  repo: () => store.get("sc.repo"),
  token: () => store.get("sc.token"),
  connected: () => !!(store.get("sc.repo") && store.get("sc.token")),
  async call(path, opts = {}) {
    const res = await fetch(`https://api.github.com/repos/${gh.repo()}${path}`, {
      ...opts,
      headers: { Authorization: `Bearer ${gh.token()}`, Accept: "application/vnd.github+json", ...(opts.headers || {}) },
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`GitHub said ${res.status}: ${body.slice(0, 160)}`);
    }
    return res.status === 204 ? null : res.json();
  },
  async readConfig() {
    const f = await gh.call("/contents/config/bots.json");
    const text = new TextDecoder().decode(Uint8Array.from(atob(f.content.replace(/\n/g, "")), (c) => c.charCodeAt(0)));
    return { sha: f.sha, json: JSON.parse(text) };
  },
  async writeConfig(json, sha, message) {
    const bytes = new TextEncoder().encode(JSON.stringify(json, null, 2) + "\n");
    let bin = "";
    bytes.forEach((b) => (bin += String.fromCharCode(b)));
    return gh.call("/contents/config/bots.json", { method: "PUT", body: JSON.stringify({ message, content: btoa(bin), sha }) });
  },
  async runNow() {
    return gh.call("/actions/workflows/trade.yml/dispatches", { method: "POST", body: JSON.stringify({ ref: "main", inputs: { force: "true" } }) });
  },
};

const api = {
  pass: () => store.get("sc.pass"),
  unlocked: () => !!store.get("sc.pass"),
  async call(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-City-Password": api.pass() || "", ...(opts.headers || {}) },
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401) store.del("sc.pass");
      throw new Error(body.detail || `Server said ${res.status}`);
    }
    return body;
  },
};
const canSave = () => (MODE === "server" ? api.unlocked() : gh.connected());

function toast(msg, ms = 4200) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* =========================================================================
   3D city
   ========================================================================= */
const PLOTS = {
  tech: new THREE.Vector3(-13, 0, -13),
  energy: new THREE.Vector3(13, 0, -13),
  finance: new THREE.Vector3(-13, 0, 13),
  consumer: new THREE.Vector3(13, 0, 13),
};
const EXTRA_SPOTS = [new THREE.Vector3(0, 0, -26), new THREE.Vector3(26, 0, 0), new THREE.Vector3(0, 0, 26), new THREE.Vector3(-26, 0, 0)];

const canvas = $("city");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0820);
scene.fog = new THREE.Fog(0x0b0820, 70, 150);

const FRUSTUM = 68;
const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -200, 400);
camera.position.set(62, 46, 26);
camera.lookAt(0, 0, 0);

const labelRenderer = new CSS2DRenderer({ element: $("labels") });
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.enablePan = false;
controls.minZoom = 0.7;
controls.maxZoom = 3;
controls.minPolarAngle = 0.55;
controls.maxPolarAngle = 1.15;
controls.autoRotate = !REDUCED;
controls.autoRotateSpeed = 0.35;
controls.addEventListener("start", () => (controls.autoRotate = false));

const composer = new EffectComposer(renderer);
const renderPass = new RenderPass(scene, camera);
composer.addPass(renderPass);
const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.85, 0.55, 0.18);
composer.addPass(bloom);
composer.addPass(new OutputPass());

scene.add(new THREE.HemisphereLight(0x8b7cf6, 0x0b0820, 0.9));
const moon = new THREE.DirectionalLight(0xc4b5fd, 1.1);
moon.position.set(-20, 50, 30);
scene.add(moon);

// seeded random so the city looks the same on every visit
let seed = 42;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

const animated = []; // functions (t, dt) => void
const pickables = [];
const buildings = {}; // id -> { group, pos, mats, label }

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.35, ...opts });
}
function glow(color, intensity = 2) {
  return new THREE.MeshStandardMaterial({ color: 0x000000, emissive: new THREE.Color(color), emissiveIntensity: intensity });
}
function edges(mesh, color, opacity = 0.9) {
  const l = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  l.position.copy(mesh.position);
  l.rotation.copy(mesh.rotation);
  return l;
}

function windowTexture(color, cols, rows, litRatio = 0.55) {
  const c = document.createElement("canvas");
  c.width = cols * 8;
  c.height = rows * 12;
  const g = c.getContext("2d");
  g.fillStyle = "#000";
  g.fillRect(0, 0, c.width, c.height);
  for (let y = 0; y < rows; y++)
    for (let x = 0; x < cols; x++) {
      if (rand() > litRatio) continue;
      g.globalAlpha = 0.45 + rand() * 0.55;
      g.fillStyle = color;
      g.fillRect(x * 8 + 2, y * 12 + 3, 4, 6);
    }
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.magFilter = THREE.NearestFilter;
  return tex;
}

function windowedBox(w, h, d, color, base = 0x1b1546, lit = 0.55) {
  const tex = windowTexture(color, Math.max(2, Math.round(w * 1.6)), Math.max(2, Math.round(h * 1.4)), lit);
  const side = mat(base, { emissive: 0xffffff, emissiveMap: tex, emissiveIntensity: 1.3 });
  const top = mat(base);
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), [side, side, top, top, side, side]);
  m.userData.windowMat = side;
  return m;
}

function platform(w, d, h, edgeColor) {
  const g = new THREE.Group();
  const slab = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(0x17113d, { roughness: 0.8 }));
  slab.position.y = h / 2;
  g.add(slab, edges(slab, edgeColor, 0.95));
  const inner = new THREE.Mesh(new THREE.PlaneGeometry(w - 1.2, d - 1.2), mat(0x1f1850, { roughness: 0.9 }));
  inner.rotation.x = -Math.PI / 2;
  inner.position.y = h + 0.01;
  g.add(inner);
  return g;
}

function tree(x, z) {
  const g = new THREE.Group();
  const trunk = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.1, 0.5), mat(0x2a1f4a));
  trunk.position.y = 0.25;
  const top = new THREE.Mesh(new THREE.SphereGeometry(0.45 + rand() * 0.2, 8, 6), mat(0x0f5a4f, { emissive: 0x0b3b36, emissiveIntensity: 0.6 }));
  top.position.y = 0.8;
  g.add(trunk, top);
  g.position.set(x, 0.6, z);
  return g;
}

function lamp(x, z, color = 0x93c5fd) {
  const g = new THREE.Group();
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 1.6), mat(0x3b3270));
  pole.position.y = 0.8;
  const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.16, 8, 8), glow(color, 3));
  bulb.position.y = 1.65;
  g.add(pole, bulb);
  g.position.set(x, 0.6, z);
  return g;
}

/* ---------- sector buildings ---------- */
function buildTech(color) {
  const g = new THREE.Group();
  const a = windowedBox(4.2, 11, 4.2, "#7dd3fc", 0x162a4a, 0.6);
  a.position.y = 5.5;
  const b = windowedBox(3, 5, 3, "#7dd3fc", 0x162a4a, 0.6);
  b.position.y = 13.5;
  const c = windowedBox(1.8, 2.4, 1.8, "#bae6fd", 0x162a4a, 0.7);
  c.position.y = 17.2;
  const ant = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 3), mat(0x9ca3af));
  ant.position.y = 19.9;
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.22, 10, 10), glow(0xff3b6b, 4));
  tip.position.y = 21.4;
  animated.push((t) => (tip.material.emissiveIntensity = Math.sin(t * 3) > 0.2 ? 5 : 0.4));
  const side = windowedBox(2.6, 6, 2.6, "#67e8f9", 0x1a2050, 0.5);
  side.position.set(3.6, 3, 2.2);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(2.6, 0.07, 6, 48), glow(color, 2.5));
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 11.2;
  g.add(a, b, c, ant, tip, side, ring, edges(a, color, 0.5), edges(b, color, 0.5));
  return g;
}

function buildEnergy(color) {
  const g = new THREE.Group();
  const shed = windowedBox(5, 3, 3.4, "#fcd34d", 0x2a1f3d, 0.45);
  shed.position.set(-1.2, 1.5, 2);
  g.add(shed, edges(shed, color, 0.5));
  for (const [x, z, r, h] of [[-2.2, -2.2, 1.5, 3.4], [1.4, -2.6, 1.2, 2.8]]) {
    const tank = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 24), mat(0x332a55));
    tank.position.set(x, h / 2, z);
    const band = new THREE.Mesh(new THREE.TorusGeometry(r + 0.02, 0.06, 6, 32), glow(color, 2.5));
    band.rotation.x = Math.PI / 2;
    band.position.set(x, h * 0.7, z);
    g.add(tank, band);
  }
  for (const [x, z, h] of [[3, 1.2, 8], [3.9, -0.4, 6.5]]) {
    const stack = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.5, h, 12), mat(0x3a3160));
    stack.position.set(x, h / 2, z);
    const lip = new THREE.Mesh(new THREE.TorusGeometry(0.4, 0.08, 6, 16), glow(0xff7a1a, 3));
    lip.rotation.x = Math.PI / 2;
    lip.position.set(x, h, z);
    g.add(stack, lip);
    // smoke puffs
    for (let i = 0; i < 4; i++) {
      const puff = new THREE.Mesh(new THREE.SphereGeometry(0.4, 8, 6), new THREE.MeshBasicMaterial({ color: 0x6d5fa8, transparent: true, opacity: 0.35 }));
      g.add(puff);
      const off = i / 4;
      animated.push((t) => {
        const p = (t * 0.25 + off) % 1;
        puff.position.set(x + p * 1.2, h + 0.4 + p * 4, z - p * 0.6);
        puff.scale.setScalar(0.6 + p * 1.6);
        puff.material.opacity = 0.35 * (1 - p);
      });
    }
  }
  // wind turbine
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.18, 9, 8), mat(0xd6d3f0, { metalness: 0.1 }));
  pole.position.set(-3.6, 4.5, -0.2);
  const hub = new THREE.Group();
  hub.position.set(-3.6, 9, 0.1);
  for (let i = 0; i < 3; i++) {
    const blade = new THREE.Mesh(new THREE.BoxGeometry(0.22, 3.4, 0.06), mat(0xe9e5ff, { metalness: 0.1 }));
    blade.position.y = 1.7;
    const arm = new THREE.Group();
    arm.rotation.z = (i * Math.PI * 2) / 3;
    arm.add(blade);
    hub.add(arm);
  }
  hub.add(new THREE.Mesh(new THREE.SphereGeometry(0.2, 8, 8), glow(color, 2)));
  animated.push((t, dt) => (hub.rotation.z -= dt * 1.4));
  g.add(pole, hub);
  return g;
}

function buildFinance(color) {
  const g = new THREE.Group();
  const step1 = new THREE.Mesh(new THREE.BoxGeometry(8, 0.5, 6.4), mat(0x2a2358));
  step1.position.y = 0.25;
  const step2 = new THREE.Mesh(new THREE.BoxGeometry(7.2, 0.5, 5.6), mat(0x2f2860));
  step2.position.y = 0.75;
  const hall = windowedBox(6.2, 4.6, 4.2, "#fde68a", 0x2a2350, 0.35);
  hall.position.set(0, 3.3, -0.4);
  g.add(step1, step2, hall, edges(step2, color, 0.7));
  for (let i = 0; i < 6; i++) {
    const col = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.25, 4.4, 12), mat(0xe7e3ff, { metalness: 0.15, roughness: 0.4 }));
    col.position.set(-2.75 + i * 1.1, 3.2, 2.3);
    g.add(col);
  }
  const beam = new THREE.Mesh(new THREE.BoxGeometry(6.8, 0.5, 5.4), mat(0x3a3170));
  beam.position.y = 5.65;
  const tri = new THREE.Shape([new THREE.Vector2(-3.4, 0), new THREE.Vector2(3.4, 0), new THREE.Vector2(0, 1.5)]);
  const pedGeo = new THREE.ExtrudeGeometry(tri, { depth: 5.4, bevelEnabled: false });
  pedGeo.translate(0, 0, -2.7);
  const ped = new THREE.Mesh(pedGeo, mat(0x3a3170));
  ped.position.set(0, 5.9, 0);
  const trim = new THREE.Mesh(new THREE.BoxGeometry(6.9, 0.08, 0.08), glow(0xfacc15, 3));
  trim.position.set(0, 5.95, 2.72);
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1.5, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), mat(0x4a3f8a, { metalness: 0.6, roughness: 0.3 }));
  dome.position.set(0, 7.1, -0.6);
  const domeRing = new THREE.Mesh(new THREE.TorusGeometry(1.52, 0.06, 6, 40), glow(color, 2.5));
  domeRing.rotation.x = Math.PI / 2;
  domeRing.position.copy(dome.position);
  // a coin that spins over the bank
  const coin = new THREE.Mesh(new THREE.CylinderGeometry(0.9, 0.9, 0.16, 32), glow(0xfacc15, 2.2));
  coin.rotation.x = Math.PI / 2;
  coin.position.set(0, 10.4, -0.6);
  animated.push((t) => {
    coin.rotation.z = t * 1.5;
    coin.position.y = 10.4 + Math.sin(t * 1.6) * 0.25;
  });
  g.add(beam, ped, trim, dome, domeRing, coin);
  return g;
}

function buildConsumer(color) {
  const g = new THREE.Group();
  const mall = windowedBox(8.4, 3.4, 6, "#f9a8d4", 0x2d1a4a, 0.5);
  mall.position.y = 1.7;
  const upper = windowedBox(6, 2.6, 4.2, "#fbcfe8", 0x2d1a4a, 0.45);
  upper.position.set(-0.6, 4.7, -0.4);
  const sign = new THREE.Mesh(new THREE.BoxGeometry(6.4, 0.7, 0.12), glow(color, 2.8));
  sign.position.set(0, 3.0, 3.07);
  g.add(mall, upper, sign, edges(mall, color, 0.6));
  const awningColors = [0xf472b6, 0x22d3ee, 0xfacc15, 0xa78bfa];
  for (let i = 0; i < 4; i++) {
    const aw = new THREE.Mesh(new THREE.BoxGeometry(1.6, 0.12, 0.9), glow(awningColors[i], 1.6));
    aw.position.set(-3 + i * 2, 1.9, 3.4);
    aw.rotation.x = 0.35;
    g.add(aw);
  }
  // rooftop sign: a bag icon made of a box and a handle ring, slowly turning
  const bag = new THREE.Group();
  const body = new THREE.Mesh(new THREE.BoxGeometry(1.6, 1.8, 0.7), glow(0xf472b6, 1.6));
  const handle = new THREE.Mesh(new THREE.TorusGeometry(0.45, 0.08, 8, 24, Math.PI), glow(0xfbcfe8, 2.5));
  handle.position.y = 0.9;
  bag.add(body, handle);
  bag.position.set(-0.6, 7.6, -0.4);
  animated.push((t) => (bag.rotation.y = t * 0.8));
  g.add(bag);
  return g;
}

function buildGeneric(color) {
  const g = new THREE.Group();
  const a = windowedBox(4, 7, 4, "#ddd6fe", 0x231b52, 0.5);
  a.position.y = 3.5;
  g.add(a, edges(a, color, 0.6));
  return g;
}

const BUILDERS = { tech: buildTech, energy: buildEnergy, finance: buildFinance, consumer: buildConsumer };

function buildHQ() {
  const g = new THREE.Group();
  const base = new THREE.Mesh(new THREE.CylinderGeometry(4.6, 5, 0.8, 8), mat(0x221a55));
  base.position.y = 0.4;
  g.add(base, edges(base, 0xa78bfa, 0.9));
  const tex = windowTexture("#c4b5fd", 22, 26, 0.6);
  tex.wrapS = THREE.RepeatWrapping;
  const towerMat = mat(0x221a55, { emissive: 0xffffff, emissiveMap: tex, emissiveIntensity: 1.2 });
  const tower = new THREE.Mesh(new THREE.CylinderGeometry(2, 2.8, 16, 8, 1, true), towerMat);
  tower.position.y = 8.8;
  const cap = new THREE.Mesh(new THREE.CylinderGeometry(1.2, 2, 2.2, 8), mat(0x2d2470));
  cap.position.y = 17.9;
  const spire = new THREE.Mesh(new THREE.ConeGeometry(0.5, 4.2, 8), mat(0xd8d2ff, { metalness: 0.7, roughness: 0.25 }));
  spire.position.y = 21.1;
  const beacon = new THREE.Mesh(new THREE.TorusGeometry(2.9, 0.08, 6, 64), glow(0xa78bfa, 3));
  beacon.rotation.x = Math.PI / 2;
  beacon.position.y = 12;
  const beacon2 = beacon.clone();
  beacon2.scale.setScalar(0.75);
  beacon2.position.y = 15.5;
  animated.push((t) => {
    beacon.position.y = 10 + ((t * 1.5) % 7);
    beacon.material.emissiveIntensity = 3 * (1 - ((t * 1.5) % 7) / 7) + 0.4;
  });
  g.add(tower, cap, spire, beacon, beacon2);
  return g;
}

/* ---------- city layout ---------- */
function layoutCity(bots) {
  // ground and grid
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), mat(0x0d0a26, { roughness: 1, metalness: 0 }));
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);
  const grid = new THREE.GridHelper(400, 160, 0x3a2b8c, 0x1d1650);
  grid.position.y = 0.01;
  scene.add(grid);

  // central plaza + roads
  const plaza = platform(14, 14, 0.6, 0xa78bfa);
  scene.add(plaza);
  const roadMat = mat(0x120d30, { roughness: 0.9 });
  const dashMat = glow(0x6d5ad6, 1.4);
  const roads = [
    [0, -13, 4, 12, 0], [0, 13, 4, 12, 0], [-13, 0, 12, 4, 1], [13, 0, 12, 4, 1],
    [-13, -6.5, 4, 1, 0], [13, -6.5, 4, 1, 0],
  ];
  for (const [x, z, w, d, horiz] of roads.slice(0, 4)) {
    const r = new THREE.Mesh(new THREE.BoxGeometry(w, 0.3, d), roadMat);
    r.position.set(x, 0.15, z);
    scene.add(r);
    for (let i = -2; i <= 2; i++) {
      const dash = new THREE.Mesh(new THREE.BoxGeometry(horiz ? 1 : 0.15, 0.05, horiz ? 0.15 : 1), dashMat);
      dash.position.set(x + (horiz ? i * 2.2 : 0), 0.32, z + (horiz ? 0 : i * 2.2));
      scene.add(dash);
    }
  }
  // ring road loop for cars
  const loop = new THREE.CatmullRomCurve3(
    [[-21, -21], [21, -21], [21, 21], [-21, 21]].map(([x, z]) => new THREE.Vector3(x, 0.35, z)),
    true, "catmullrom", 0.05,
  );
  const loopMesh = new THREE.Mesh(new THREE.TubeGeometry(loop, 200, 0.06, 4, true), glow(0x3b82f6, 1.2));
  scene.add(loopMesh);
  for (let i = 0; i < 9; i++) {
    const car = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.4, 0.5), glow(i % 2 ? 0x22d3ee : 0xf472b6, 2.5));
    scene.add(car);
    const off = i / 9;
    const dir = i % 3 === 0 ? -1 : 1;
    animated.push((t) => {
      const u = (((dir * t * 0.02 + off) % 1) + 1) % 1;
      const p = loop.getPointAt(u);
      const tan = loop.getTangentAt(u);
      car.position.copy(p).add(new THREE.Vector3(0, 0.1, 0));
      car.rotation.y = Math.atan2(-tan.z, tan.x);
    });
  }

  // HQ
  const hq = buildHQ();
  hq.position.y = 0.6;
  scene.add(hq);
  const hqLabel = makeLabel("hq", "City Hall", "");
  hqLabel.position.set(0, 26, 0);
  scene.add(hqLabel);
  buildings.__hq = { label: hqLabel };

  bots.forEach((bot, i) => {
    const pos = (PLOTS[bot.id] || EXTRA_SPOTS[i % EXTRA_SPOTS.length]).clone();
    const color = new THREE.Color(bot.color || "#a78bfa");
    const plot = platform(12, 12, 0.6, color);
    plot.position.copy(pos);
    scene.add(plot);

    const g = (BUILDERS[bot.id] || buildGeneric)(color);
    g.position.set(pos.x, 0.6, pos.z);
    scene.add(g);

    for (let k = 0; k < 7; k++) {
      const a = rand() * Math.PI * 2;
      const r = 4.4 + rand() * 0.9;
      scene.add(tree(pos.x + Math.cos(a) * r, pos.z + Math.sin(a) * r));
    }
    scene.add(lamp(pos.x - 5.3, pos.z + 5.3, color), lamp(pos.x + 5.3, pos.z - 5.3, color));

    const light = new THREE.PointLight(color, 40, 22, 1.6);
    light.position.set(pos.x, 6, pos.z);
    scene.add(light);

    // data link from City Hall to this building
    const top = new THREE.Vector3(pos.x, 14, pos.z);
    const mid = new THREE.Vector3(pos.x / 2, 26, pos.z / 2);
    const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(0, 19, 0), mid, top);
    const lineGeo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(40));
    const line = new THREE.Line(lineGeo, new THREE.LineDashedMaterial({ color, dashSize: 0.8, gapSize: 0.6, transparent: true, opacity: 0.85 }));
    line.computeLineDistances();
    scene.add(line);
    // a packet that travels along the link
    const packet = new THREE.Mesh(new THREE.SphereGeometry(0.22, 8, 8), glow(color, 4));
    scene.add(packet);
    const poff = rand();
    animated.push((t) => packet.position.copy(curve.getPointAt((t * 0.18 + poff) % 1)));

    const mats = [];
    g.traverse((o) => {
      if (o.isMesh) {
        o.userData.botId = bot.id;
        pickables.push(o);
        const ms = Array.isArray(o.material) ? o.material : [o.material];
        ms.forEach((m) => m.emissive && mats.push([m, m.emissiveIntensity]));
      }
    });
    plot.traverse((o) => {
      if (o.isMesh) {
        o.userData.botId = bot.id;
        pickables.push(o);
      }
    });

    const height = { tech: 22.5, energy: 12, finance: 12.5, consumer: 9.5 }[bot.id] || 9;
    const label = makeLabel(bot.id, bot.name, "", bot.color);
    label.position.set(pos.x, height, pos.z);
    scene.add(label);

    buildings[bot.id] = { group: g, pos, mats, label, line, light, scale: 1 };
  });
}

function makeLabel(id, name, sub, color) {
  const el = document.createElement("div");
  el.className = "label" + (id === "hq" ? " hq" : "");
  el.style.color = color || "";
  el.innerHTML = `<span class="lname">${esc(name)}</span><span class="lsub num"></span><span class="ltick"></span>`;
  if (id !== "hq") el.addEventListener("click", () => openBuilding(id));
  const obj = new CSS2DObject(el);
  obj.userData.el = el;
  return obj;
}

/* ---------- interaction ---------- */
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let hovered = null;
let downAt = null;

function pick(ev) {
  const r = canvas.getBoundingClientRect();
  pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(pickables, false)[0];
  return hit ? hit.object.userData.botId : null;
}
canvas.addEventListener("pointermove", (ev) => {
  if (ev.pointerType !== "mouse") return;
  if (office.active) return canvas.classList.toggle("hovering", pickDude(ev));
  hovered = pick(ev);
  canvas.classList.toggle("hovering", !!hovered);
});
canvas.addEventListener("pointerdown", (ev) => (downAt = [ev.clientX, ev.clientY]));
canvas.addEventListener("pointerup", (ev) => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 6) return;
  if (office.active) {
    // poke the trader and he tells you what he's up to
    if (pickDude(ev) && !office.busy) greet();
    return;
  }
  const id = pick(ev);
  if (id) openBuilding(id);
});

/* ---------- render loop ---------- */
const focusTarget = new THREE.Vector3();
function resize() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const aspect = w / h;
  const f = aspect < 1 ? FRUSTUM * 1.25 : FRUSTUM;
  camera.left = (-f * aspect) / 2;
  camera.right = (f * aspect) / 2;
  camera.top = f / 2;
  camera.bottom = -f / 2;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
  composer.setSize(w, h);
  bloom.setSize(w, h);
  labelRenderer.setSize(w, h);
  officeCam.aspect = aspect;
  officeCam.updateProjectionMatrix();
  css3d.setSize(w, h);
  if (office.active) {
    snapPose();
    syncFlat();
  }
}
addEventListener("resize", resize);

const clock = new THREE.Clock();
function frame() {
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = clock.elapsedTime;
  const now = performance.now();
  runTweens(now);
  if (office.active) {
    officeFrame(t, dt, now);
    composer.render();
    css3d.render(cssScene, officeCam);
    requestAnimationFrame(frame);
    return;
  }
  if (!REDUCED) animated.forEach((fn) => fn(t, dt));
  for (const [id, b] of Object.entries(buildings)) {
    if (!b.group) continue;
    const want = id === hovered || id === openId ? 1.06 : 1;
    b.scale += (want - b.scale) * 0.15;
    b.group.scale.setScalar(b.scale);
    b.line.material.dashOffset = REDUCED ? 0 : -t * 2;
  }
  controls.target.lerp(focusTarget, 0.08);
  controls.update();
  composer.render();
  labelRenderer.render(scene, camera);
  requestAnimationFrame(frame);
}

/* =========================================================================
   HUD, building list, ticker tape
   ========================================================================= */
function renderHUD() {
  const t = STATE.totals;
  $("t-equity").textContent = money(t.equity);
  $("t-pnl").innerHTML = `<span class="${cls(t.pnl)}">${signedMoney(t.pnl)} (${pct(t.pnl_pct)})</span>`;
  $("t-day").innerHTML = `<span class="${cls(t.day_change)}">${signedMoney(t.day_change)}</span>`;
  const live = STATE.broker === "schwab";
  const pill = $("mode-pill");
  pill.textContent = live ? "Live · Schwab" : "Paper money";
  pill.classList.toggle("live", live);

  const mk = STATE.server?.markets?.[0];
  const mp = $("market-pill");
  if (mk) {
    mp.hidden = false;
    mp.textContent = mk.open ? "Market open" : "Market closed";
    mp.className = "pill " + (mk.open ? "on" : "paused");
    const q = STATE.server.last_quote_at ? new Date(STATE.server.last_quote_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "–";
    mp.title = `Prices updated ${q}. Bots trade ${STATE.server.trade_window_ny} New York time on market days, each on its own check interval.`;
  }

  const banner = $("banner");
  const ageH = (Date.now() - Date.parse(STATE.generated_at)) / 36e5;
  const slowFeed = (STATE.server?.markets || []).find((m) => m.feed && !m.feed.ok);
  if (STATE.server?.last_error) {
    banner.textContent = `Price feed problem: ${STATE.server.last_error}. Retrying automatically.`;
    banner.hidden = false;
  } else if (slowFeed) {
    banner.textContent = `The quick price feed isn't working (${slowFeed.feed.error}). During market hours the bots fall back to slower per-stock prices for up to 80 stocks, holdings first, and keep retrying.`;
    banner.hidden = false;
  } else if (STATE.price_source === "simulated" && MODE === "server") {
    banner.textContent = "Test mode: the server is using simulated prices (MARKET_DATA=simulated).";
    banner.hidden = false;
  } else if (STATE.price_source === "simulated") {
    banner.textContent = "Preview with simulated prices. Real prices show once the server is running.";
    banner.hidden = false;
  } else if (MODE !== "server" && ageH > 80) {
    banner.textContent = `Last update was ${Math.round(ageH / 24)} days ago. Check the Actions tab on GitHub.`;
    banner.hidden = false;
  } else banner.hidden = true;

  const hq = buildings.__hq.label.userData.el.querySelector(".lsub");
  hq.innerHTML = `${money(t.equity)} <span class="${cls(t.pnl)}">${pct(t.pnl_pct)}</span>`;

  const list = $("building-list");
  list.innerHTML = "";
  for (const b of STATE.bots) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.id = b.id;
    if (b.id === openId) btn.classList.add("active");
    btn.innerHTML = `<span class="dot" style="color:${esc(b.color)}"></span><span class="bl-name">${esc(b.name)}</span><span class="bl-pnl ${cls(b.pnl)}">${pct(b.pnl_pct)}</span>`;
    btn.addEventListener("click", () => openBuilding(b.id));
    list.appendChild(btn);
    const lb = buildings[b.id]?.label.userData.el.querySelector(".lsub");
    if (lb) lb.innerHTML = `${money(b.equity)} <span class="${cls(b.pnl)}">${pct(b.pnl_pct)}</span>${b.enabled ? "" : " · paused"}`;
    const bd = buildings[b.id];
    if (bd) bd.mats.forEach(([m, base]) => (m.emissiveIntensity = b.enabled ? base : base * 0.25));
  }

  const trades = STATE.bots
    .flatMap((b) => b.trades.slice(0, 8).map((t) => ({ ...t, bot: b.name, color: b.color })))
    .sort((a, b) => (a.date < b.date ? 1 : -1))
    .slice(0, 24);
  const tapeHTML = trades.length
    ? trades
        .map((t) => `<span><b style="color:${esc(t.color)}">${esc(t.bot)}</b> <span class="${t.side === "buy" ? "up" : "down"}">${t.side.toUpperCase()}</span> ${t.shares} ${esc(t.ticker)} @ ${money(t.price, 2)}</span>`)
        .join("")
    : "<span>No trades yet. The bots trade on their first run.</span>";
  if ($("tape").innerHTML !== tapeHTML) $("tape").innerHTML = tapeHTML;
}

/* =========================================================================
   Office: clicking a building flies into its trader's office. Each part of
   the building's details is on one of his monitors; click one to zoom in.
   ========================================================================= */
let openId = null;
let draft = null;

function bot() {
  return STATE.bots.find((b) => b.id === openId);
}

const SLEEVE_TAG = { ai: "AI", momentum: "MOM", intraday: "DAY" };
const mainName = (s) => (s.style === "intraday" ? "Day trading" : "Momentum");

const officeScene = new THREE.Scene();
officeScene.background = new THREE.Color(0x0b0820);
officeScene.fog = new THREE.Fog(0x0b0820, 8, 20);
const officeCam = new THREE.PerspectiveCamera(40, 1, 0.05, 60);
const cssScene = new THREE.Scene();
const css3d = new CSS3DRenderer();
css3d.domElement.className = "screens";
css3d.domElement.hidden = true;
$("app").insertBefore(css3d.domElement, $("labels"));

// Monitor sizes in metres, placed around the desk. A focused monitor shows its page at about 1:1.
const PX_PER_M = 760;
const MONITORS = [
  { id: "status", title: "Building", w: 1.34, h: 0.34, x: 0, y: 2.1, z: -1.02, yaw: 0, tilt: 0.12 },
  { id: "holdings", title: "Holdings & plan", w: 1.34, h: 0.8, x: 0, y: 1.47, z: -0.98, yaw: 0, tilt: 0.05 },
  { id: "trades", title: "Trades", w: 0.96, h: 0.6, x: -1.27, y: 1.2, z: -0.74, yaw: 0.5, tilt: 0.05 },
  { id: "picks", title: "Picks", w: 0.96, h: 0.6, x: -1.27, y: 1.86, z: -0.8, yaw: 0.5, tilt: 0.12 },
  { id: "settings", title: "Settings", w: 0.66, h: 0.84, x: 1.12, y: 1.62, z: -0.78, yaw: -0.5, tilt: 0.05 },
];
const NAV_ORDER = ["holdings", "trades", "picks", "settings"];

const office = {
  built: false,
  active: false,
  busy: false,
  focus: null, // monitor id, or null for the whole desk
  look: new THREE.Vector3(),
  mons: {},
  dude: null,
  dressing: null,
  anims: [], // (t, dt) => void for the current building's props
  act: null, // a short reaction (cheer, facepalm, wave, nod) that overrides the normal pose
  actUntil: 0,
  bubbleUntil: 0,
  seen: {}, // building id -> newest trade already reacted to
  cityZoom: 1,
};

/* ---------- small helpers ---------- */
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const easeInOut = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const tweens = [];
function tween(ms, fn) {
  if (REDUCED || ms <= 0) {
    fn(1);
    return Promise.resolve();
  }
  return new Promise((done) => tweens.push({ t0: performance.now(), ms, fn, done }));
}
function runTweens(now) {
  for (let i = tweens.length - 1; i >= 0; i--) {
    const tw = tweens[i];
    const k = Math.min(1, (now - tw.t0) / tw.ms);
    tw.fn(easeInOut(k));
    if (k >= 1) {
      tweens.splice(i, 1);
      tw.done();
    }
  }
}
function fade(on) {
  $("fader").classList.toggle("on", on);
  return wait(REDUCED ? 60 : 260);
}
function flyTo(pose, ms, onStep) {
  const p0 = officeCam.position.clone();
  const t0 = office.look.clone();
  return tween(ms, (k) => {
    officeCam.position.lerpVectors(p0, pose.p, k);
    office.look.lerpVectors(t0, pose.t, k);
    onStep?.(k);
  });
}
const nyTime = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }) : "");
const marketOpen = () => {
  const mk = STATE.server?.markets?.[0];
  return mk ? mk.open : null;
};
const tradingStarts = () => `${(STATE.server?.trade_window_ny || "9:45").split("–")[0]}am`;
const tradeKey = (t) => (t ? `${t.date}|${t.time}|${t.ticker}|${t.side}|${t.shares}` : "");

function obox(w, h, d, color, opts = {}) {
  return new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshStandardMaterial({ color, roughness: 0.75, metalness: 0.1, ...opts }));
}
function place(obj, x, y, z, parent) {
  obj.position.set(x, y, z);
  parent?.add(obj);
  return obj;
}
function rod(a, b, r, color) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, a.distanceTo(b), 10), new THREE.MeshStandardMaterial({ color, metalness: 0.6, roughness: 0.35 }));
  m.position.copy(a).add(b).multiplyScalar(0.5);
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), b.clone().sub(a).normalize());
  return m;
}
function canvasTexture(w, h, draw) {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/* ---------- the room: desk, chair-free furniture, window, monitors ---------- */
function skylineTexture() {
  let s = 11;
  const r = () => ((s = (s * 16807) % 2147483647) / 2147483647);
  return canvasTexture(1024, 320, (g, W, H) => {
    const sky = g.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, "#06041a");
    sky.addColorStop(1, "#24185c");
    g.fillStyle = sky;
    g.fillRect(0, 0, W, H);
    for (let x = 0; x < W; ) {
      const w = 34 + r() * 70;
      const h = 70 + r() * 210;
      g.fillStyle = "#0b0824";
      g.fillRect(x, H - h, w - 5, h);
      for (let wy = H - h + 9; wy < H - 8; wy += 13)
        for (let wx = x + 6; wx < x + w - 12; wx += 10)
          if (r() < 0.32) {
            g.fillStyle = r() < 0.7 ? "rgba(253, 224, 171, 0.7)" : "rgba(167, 139, 250, 0.75)";
            g.fillRect(wx, wy, 4, 6);
          }
      x += w;
    }
  });
}

function buildOffice() {
  office.built = true;
  officeScene.add(new THREE.HemisphereLight(0xb4a8ff, 0x140f33, 0.8));
  const key = new THREE.DirectionalLight(0xe9e4ff, 1.0);
  key.position.set(1.5, 4, 4);
  officeScene.add(key);
  office.glowLight = place(new THREE.PointLight(0x22d3ee, 1.8, 4, 1.5), 0, 1.35, -0.5, officeScene);
  // a soft lamp over the chair so the trader reads against the dark room
  place(new THREE.PointLight(0xfff1dc, 4, 4.5, 1.6), 0.4, 2.7, 1.5, officeScene);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(18, 18), new THREE.MeshStandardMaterial({ color: 0x120d2e, roughness: 0.9 }));
  floor.rotation.x = -Math.PI / 2;
  officeScene.add(floor);
  const grid = new THREE.GridHelper(18, 36, 0x3b2f7a, 0x231b4e);
  grid.position.y = 0.002;
  officeScene.add(grid);

  // window wall with the city at night
  const view = new THREE.Mesh(new THREE.PlaneGeometry(10, 3.1), new THREE.MeshBasicMaterial({ map: skylineTexture(), color: 0xb9b0e6 }));
  place(view, 0, 1.75, -2.7, officeScene);
  for (let x = -5; x <= 5; x += 1.6) place(obox(0.06, 3.1, 0.06, 0x0a0820), x, 1.75, -2.66, officeScene);
  place(obox(10, 0.08, 0.1, 0x0a0820), 0, 0.2, -2.66, officeScene);

  // desk
  const deskMat = { metalness: 0.45, roughness: 0.4 };
  place(obox(3.6, 0.06, 1.32, 0x1c1640, deskMat), -0.1, 0.75, -0.66, officeScene);
  for (const x of [-1.84, 1.64]) place(obox(0.06, 0.72, 1.2, 0x15112f, deskMat), x, 0.36, -0.66, officeScene);
  office.edgeMat = glow(0xa78bfa, 1.6);
  place(new THREE.Mesh(new THREE.BoxGeometry(3.6, 0.016, 0.016), office.edgeMat), -0.1, 0.728, -0.0, officeScene);

  // keyboard, mouse, mug
  place(obox(0.52, 0.025, 0.17, 0x2a2350), 0, 0.794, -0.2, officeScene);
  office.kbGlow = glow(0xa78bfa, 0.9);
  place(new THREE.Mesh(new THREE.BoxGeometry(0.53, 0.004, 0.18), office.kbGlow), 0, 0.781, -0.2, officeScene);
  place(obox(0.07, 0.03, 0.11, 0x2a2350), 0.42, 0.795, -0.18, officeScene);
  const mug = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.04, 0.1, 16), new THREE.MeshStandardMaterial({ color: 0xe9e4ff, roughness: 0.5 }));
  place(mug, -0.48, 0.83, -0.22, officeScene);

  // monitors on poles; each screen is a real web page laid over the 3D screen
  const poleTop = {};
  const normal = new THREE.Vector3();
  for (const m of MONITORS) {
    const g = new THREE.Group();
    g.position.set(m.x, m.y, m.z);
    g.rotation.set(-m.tilt, m.yaw, 0, "YXZ");
    place(obox(m.w + 0.05, m.h + 0.05, 0.04, 0x0d0a22, { metalness: 0.5, roughness: 0.35 }), 0, 0, -0.021, g);
    const screenMat = new THREE.MeshBasicMaterial({ color: 0x0a0720 });
    place(new THREE.Mesh(new THREE.PlaneGeometry(m.w, m.h), screenMat), 0, 0, 0.001, g);
    officeScene.add(g);
    g.updateMatrixWorld();

    // arm back to a pole behind the screen
    normal.set(0, 0, 1).applyQuaternion(g.quaternion);
    const col = m.x < -0.5 ? "L" : m.x > 0.5 ? "R" : "C";
    const back = g.position.clone().addScaledVector(normal, -0.06);
    const pole = col === "C" ? new THREE.Vector3(0, m.y, -1.28) : g.position.clone().addScaledVector(normal, -0.3).setY(m.y);
    poleTop[col] = poleTop[col] || { x: pole.x, z: pole.z, top: 0 };
    poleTop[col].top = Math.max(poleTop[col].top, m.y + 0.06);
    pole.x = poleTop[col].x;
    pole.z = poleTop[col].z;
    officeScene.add(rod(back, pole, 0.018, 0x2a2350));

    const el = document.createElement("section");
    el.className = "mon";
    el.dataset.mon = m.id;
    el.style.width = `${Math.round(m.w * PX_PER_M)}px`;
    el.style.height = `${Math.round(m.h * PX_PER_M)}px`;
    el.setAttribute("aria-label", m.title);
    el.innerHTML = `<header class="mon-bar"><i></i><b>${esc(m.title)}</b><span class="mon-hint">Click to open</span></header><div class="mon-body" tabindex="-1"></div>`;
    el.addEventListener("click", () => {
      if (office.focus !== m.id) focusMonitor(m.id);
    });
    const obj = new CSS3DObject(el);
    obj.position.copy(g.position);
    obj.quaternion.copy(g.quaternion);
    obj.scale.setScalar(1 / PX_PER_M);
    cssScene.add(obj);
    office.mons[m.id] = { ...m, group: g, el, body: el.querySelector(".mon-body"), screenMat };
  }
  for (const p of Object.values(poleTop)) {
    officeScene.add(rod(new THREE.Vector3(p.x, 0.78, p.z), new THREE.Vector3(p.x, p.top, p.z), 0.03, 0x241e48));
    place(obox(0.26, 0.02, 0.2, 0x241e48, { metalness: 0.6 }), p.x, 0.79, p.z, officeScene);
  }
}

/* ---------- the trader: a blocky figure dressed for his sector ---------- */
const OUTFITS = {
  tech: { skin: 0xb07a4f, hair: 0x1b120b, top: 0x4a5370, arm: 0x4a5370, fore: 0x4a5370, pants: 0x1f2433, shoes: 0xe5e7eb, gear: ["hood", "headphones"] },
  energy: { skin: 0xf0c8a0, hair: 0x6b3f1d, top: 0x3a3a44, arm: 0x3a3a44, fore: 0x3a3a44, pants: 0x27406b, shoes: 0x6b4423, gear: ["hardhat", "vest", "beard"] },
  finance: { skin: 0x8a5532, hair: 0x111111, top: 0x2c406e, arm: 0x2c406e, fore: 0x2c406e, pants: 0x24365e, shoes: 0x0b0b0b, gear: ["suit", "glasses"] },
  consumer: { skin: 0xd9a36c, hair: 0x3d2414, top: 0xec4899, arm: 0xec4899, fore: 0xd9a36c, pants: 0x7aa7d9, shoes: 0xf5f5f5, gear: ["cap"] },
};

function faceTexture(skin, mood) {
  const hex = `#${new THREE.Color(skin).getHexString()}`;
  const t = canvasTexture(64, 64, (g) => {
    g.fillStyle = hex;
    g.fillRect(0, 0, 64, 64);
    g.fillStyle = "#1a1030";
    if (mood === "happy") {
      g.fillRect(14, 26, 10, 3);
      g.fillRect(40, 26, 10, 3);
      g.fillRect(20, 40, 24, 4);
      g.fillRect(16, 36, 4, 5);
      g.fillRect(44, 36, 4, 5);
    } else if (mood === "sad") {
      g.fillRect(16, 22, 8, 9);
      g.fillRect(40, 22, 8, 9);
      g.fillRect(22, 42, 20, 4);
      g.fillRect(18, 45, 4, 5);
      g.fillRect(42, 45, 4, 5);
    } else {
      g.fillRect(16, 22, 8, 10);
      g.fillRect(40, 22, 8, 10);
      g.fillRect(24, 42, 16, 4);
    }
  });
  t.magFilter = THREE.NearestFilter;
  return t;
}

function buildDude(o, accent) {
  const m = (c, opts) => new THREE.MeshStandardMaterial({ color: c, roughness: 0.8, metalness: 0.05, ...opts });
  const root = new THREE.Group();
  root.position.set(0, 0, 0.45);
  // skin glows a little on its own so the colored monitor light doesn't turn it green
  const skin = m(new THREE.Color(o.skin).multiplyScalar(0.65), { emissive: new THREE.Color(o.skin).multiplyScalar(0.36) });
  const hair = m(o.hair);
  // seated legs
  for (const side of [-1, 1]) {
    place(obox(0.22, 0.2, 0.5, o.pants), side * 0.13, 0.55, -0.2, root);
    place(obox(0.2, 0.5, 0.2, o.pants), side * 0.13, 0.3, -0.42, root);
    place(obox(0.22, 0.1, 0.3, o.shoes), side * 0.13, 0.05, -0.47, root);
  }
  const hips = place(new THREE.Group(), 0, 0.5, 0, root);
  const torso = place(new THREE.Group(), 0, 0, 0, hips);
  place(obox(0.56, 0.62, 0.3, o.top), 0, 0.33, 0, torso);
  const neck = place(new THREE.Group(), 0, 0.66, 0, torso);
  neck.rotation.order = "YXZ";
  const faces = { neutral: faceTexture(o.skin, "neutral"), happy: faceTexture(o.skin, "happy"), sad: faceTexture(o.skin, "sad") };
  const faceMat = m(0xa6a6a6, { map: faces.neutral, emissiveMap: faces.neutral, emissive: 0x5c5c5c });
  // box faces: +x, -x, +y (top), -y, +z (back of the head), -z (face, toward the monitors)
  const head = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.44, 0.44), [skin, skin, hair, skin, hair, faceMat]);
  place(head, 0, 0.24, 0, neck);
  place(new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.08, 0.2), hair), 0, 0.45, 0.13, neck);

  const arms = {};
  for (const [name, side] of [["L", -1], ["R", 1]]) {
    const sh = place(new THREE.Group(), side * 0.37, 0.58, 0, torso);
    place(obox(0.17, 0.32, 0.17, o.arm), 0, -0.14, 0, sh);
    const el = place(new THREE.Group(), 0, -0.3, 0, sh);
    place(o.fore === o.skin ? new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.28, 0.16), skin) : obox(0.16, 0.28, 0.16, o.fore), 0, -0.13, 0, el);
    place(new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.12, 0.15), skin), 0, -0.31, 0, el);
    arms[name] = { sh, el };
  }

  const g = new Set(o.gear || []);
  const accentGlow = glow(accent, 0.7);
  if (g.has("hood")) place(obox(0.5, 0.24, 0.13, o.top), 0, 0.64, 0.17, torso);
  if (g.has("headphones")) {
    place(obox(0.5, 0.05, 0.09, 0x111827), 0, 0.48, 0, neck);
    for (const s of [-1, 1]) {
      place(obox(0.07, 0.17, 0.17, 0x111827), s * 0.255, 0.26, 0, neck);
      place(new THREE.Mesh(new THREE.BoxGeometry(0.012, 0.045, 0.045), accentGlow), s * 0.296, 0.26, 0.0, neck);
    }
  }
  if (g.has("hardhat")) {
    const yellow = m(0xe0a800, { roughness: 0.7 });
    place(new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.26, 0.16, 20), yellow), 0, 0.53, 0, neck);
    place(new THREE.Mesh(new THREE.CylinderGeometry(0.33, 0.33, 0.025, 24), yellow), 0, 0.46, 0, neck);
  }
  if (g.has("vest")) {
    place(obox(0.585, 0.5, 0.32, 0xf97316), 0, 0.37, 0, torso);
    const stripe = glow(0xe5e7eb, 0.9);
    for (const y of [0.22, 0.42]) place(new THREE.Mesh(new THREE.BoxGeometry(0.595, 0.04, 0.33), stripe), 0, y, 0, torso);
  }
  if (g.has("beard")) place(new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.15, 0.06), hair), 0, 0.1, -0.23, neck);
  if (g.has("suit")) {
    place(obox(0.22, 0.12, 0.04, 0xf5f5f5), 0, 0.6, -0.15, torso);
    place(obox(0.07, 0.36, 0.03, 0xdc2626), 0, 0.4, -0.165, torso);
  }
  if (g.has("glasses")) place(obox(0.38, 0.05, 0.02, 0x0b0b0b), 0, 0.27, -0.23, neck);
  if (g.has("cap")) {
    const cap = m(accent);
    place(new THREE.Mesh(new THREE.BoxGeometry(0.47, 0.12, 0.47), cap), 0, 0.5, 0, neck);
    place(new THREE.Mesh(new THREE.BoxGeometry(0.36, 0.03, 0.22), cap), 0, 0.45, 0.32, neck);
  }

  const parts = [];
  root.traverse((x) => x.isMesh && parts.push(x));
  return { root, hips, torso, neck, arms, faceMat, faces, parts, mood: "neutral", cur: null };
}

/* ---------- props that make each office different ---------- */
function buildChair(accent, parent) {
  const dark = 0x1d1838;
  place(obox(0.56, 0.08, 0.52, dark), 0, 0.46, 0.45, parent);
  const back = place(obox(0.56, 0.38, 0.08, dark), 0, 0.72, 0.76, parent);
  back.rotation.x = 0.12;
  const stripe = place(new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.38, 0.085), glow(accent, 1.3)), 0, 0.72, 0.765, parent);
  stripe.rotation.x = 0.12;
  parent.add(rod(new THREE.Vector3(0, 0.08, 0.45), new THREE.Vector3(0, 0.42, 0.45), 0.035, 0x2a2350));
  for (let i = 0; i < 5; i++) {
    const a = (i / 5) * Math.PI * 2;
    const spoke = place(obox(0.3, 0.035, 0.05, 0x2a2350), Math.cos(a) * 0.15, 0.06, 0.45 + Math.sin(a) * 0.15, parent);
    spoke.rotation.y = -a;
  }
}

function buildProps(id, accent, parent) {
  const anims = [];
  const metal = (c, o = {}) => new THREE.MeshStandardMaterial({ color: c, metalness: 0.85, roughness: 0.25, ...o });
  if (id === "tech") {
    place(obox(0.62, 1.9, 0.62, 0x14102c, { metalness: 0.5 }), 2.4, 0.95, -0.9, parent);
    const leds = [];
    for (let row = 0; row < 12; row++)
      for (let c = 0; c < 4; c++) {
        const led = place(new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.025, 0.01), glow(c % 2 ? accent : 0x4ade80, 1.8)), 2.22 + c * 0.12, 0.25 + row * 0.14, -0.585, parent);
        leds.push(led);
      }
    anims.push(() => {
      const led = leds[(Math.random() * leds.length) | 0];
      led.material.emissiveIntensity = led.material.emissiveIntensity > 0.5 ? 0.15 : 1.8;
    });
    for (const [x, z] of [[0.66, -0.36], [0.76, -0.44]]) {
      place(new THREE.Mesh(new THREE.CylinderGeometry(0.033, 0.033, 0.12, 14), metal(0x1f2937)), x, 0.84, z, parent);
      place(new THREE.Mesh(new THREE.CylinderGeometry(0.034, 0.034, 0.03, 14), glow(accent, 1.2)), x, 0.85, z, parent);
    }
  } else if (id === "energy") {
    const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.88, 24), new THREE.MeshStandardMaterial({ color: 0x9a3412, roughness: 0.55, metalness: 0.4 }));
    place(barrel, -2.35, 0.44, -0.25, parent);
    for (const y of [0.22, 0.66]) place(new THREE.Mesh(new THREE.CylinderGeometry(0.305, 0.305, 0.03, 24), metal(0x431407)), -2.35, y, -0.25, parent);
    // desk wind turbine that spins
    parent.add(rod(new THREE.Vector3(-0.78, 0.78, -0.4), new THREE.Vector3(-0.78, 1.12, -0.4), 0.008, 0xe5e7eb));
    const hub = place(new THREE.Group(), -0.78, 1.12, -0.37, parent);
    for (let i = 0; i < 3; i++) {
      const blade = place(obox(0.02, 0.16, 0.006, 0xf5f5f5), 0, 0.08, 0, new THREE.Group());
      blade.parent.rotation.z = (i / 3) * Math.PI * 2;
      hub.add(blade.parent);
    }
    anims.push((t, dt) => (hub.rotation.z -= dt * 3));
  } else if (id === "finance") {
    const gold = metal(0xf5c542);
    const bars = [[-0.08, 0], [0.08, 0], [0, 1]];
    for (const [dx, layer] of bars) place(new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.05, 0.07), gold), -0.72 + dx, 0.8 + layer * 0.05, -0.3, parent);
    place(obox(0.56, 0.62, 0.5, 0x2b2f3a, { metalness: 0.6, roughness: 0.4 }), 2.25, 0.31, -0.5, parent);
    const dial = place(new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.03, 20), gold), 2.25, 0.36, -0.24, parent);
    dial.rotation.x = Math.PI / 2;
  } else if (id === "consumer") {
    for (const [x, z, c, rot] of [[0.9, 0.62, accent, 0.3], [1.12, 0.32, 0x22d3ee, -0.2]]) {
      const bag = place(new THREE.Group(), x, 0, z, parent);
      bag.rotation.y = rot;
      place(obox(0.34, 0.42, 0.15, c, { roughness: 0.6 }), 0, 0.21, 0, bag);
      const handle = place(new THREE.Mesh(new THREE.TorusGeometry(0.08, 0.009, 6, 16, Math.PI), new THREE.MeshStandardMaterial({ color: 0x111827 })), 0, 0.42, 0, bag);
      handle.rotation.y = 0;
    }
    place(obox(0.5, 0.4, 0.42, 0xa47148, { roughness: 0.95 }), -2.25, 0.2, -0.3, parent);
    place(obox(0.36, 0.3, 0.32, 0xb7835a, { roughness: 0.95 }), -2.2, 0.55, -0.32, parent);
  }
  // a plant for everyone
  place(new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.12, 0.34, 16), new THREE.MeshStandardMaterial({ color: 0x2a2350 })), -1.95, 0.17, 0.5, parent);
  for (let i = 0; i < 6; i++) {
    const leaf = place(obox(0.07, 0.42, 0.03, 0x15803d, { roughness: 0.9 }), -1.95, 0.5, 0.5, parent);
    leaf.rotation.set(Math.sin(i) * 0.5, (i / 6) * Math.PI * 2, Math.cos(i * 2) * 0.4);
  }
  return anims;
}

function dressOffice(b) {
  const accent = new THREE.Color(b.color);
  if (office.dressing) {
    Object.values(office.dude?.faces || {}).forEach((t) => t.dispose());
    officeScene.remove(office.dressing);
    office.dressing.traverse((x) => {
      if (x.isMesh) {
        x.geometry.dispose();
        (Array.isArray(x.material) ? x.material : [x.material]).forEach((mt) => {
          mt.map?.dispose();
          mt.dispose();
        });
      }
    });
  }
  const dressing = new THREE.Group();
  office.dressing = dressing;
  // the chair and the trader swivel together around the chair post
  office.seat = place(new THREE.Group(), 0, 0, 0.45, dressing);
  const seatInner = place(new THREE.Group(), 0, 0, -0.45, office.seat);
  buildChair(accent, seatInner);
  office.anims = buildProps(b.id, accent, dressing);
  const outfit = OUTFITS[b.id] || { skin: 0xc68642, hair: 0x2b1a0e, top: accent.getHex(), arm: accent.getHex(), fore: accent.getHex(), pants: 0x1f2433, shoes: 0xe5e7eb, gear: [] };
  office.dude = buildDude(outfit, accent);
  seatInner.add(office.dude.root);
  officeScene.add(dressing);
  office.edgeMat.emissive.copy(accent);
  office.kbGlow.emissive.copy(accent);
  office.glowLight.color.copy(accent);
  for (const m of Object.values(office.mons)) {
    m.el.style.setProperty("--acc", b.color);
    m.screenMat.color.copy(accent).multiplyScalar(0.16);
  }
  document.documentElement.style.setProperty("--acc", b.color);
  $("flat-mon").style.setProperty("--acc", b.color);
  office.act = null;
  $("bubble").hidden = true;
}

/* ---------- poses ---------- */
// Each arm is aimed by direction, in the trader's own space (he faces -z, his right hand is +x):
// [upper arm direction, forearm direction].
function poseTargets(name, t) {
  const typing = () => {
    const k = 0.05 * Math.sin(t * 15);
    const cycle = Math.floor(t / 9);
    const glance = t % 9 > 5.6 && t % 9 < 7.2 ? (cycle % 2 ? 0.55 : -0.5) : 0;
    return {
      torsoX: -0.08, headX: -0.12 + 0.03 * Math.sin(t * 1.7), headY: glance, bounce: 0, spin: 0,
      L: [[0.12, -0.8, -0.55], [0.22, -0.12 + k, -1]],
      R: [[-0.12, -0.8, -0.55], [-0.22, -0.12 - k, -1]],
    };
  };
  switch (name) {
    case "relaxed":
      return {
        torsoX: 0.2, headX: 0.15, headY: 0.3 * Math.sin(t * 0.35), bounce: 0.008 * Math.sin(t * 1.4), spin: 0.12 * Math.sin(t * 0.25),
        L: [[-0.75, 0.62, 0.18], [0.92, 0.2, 0.36]],
        R: [[0.75, 0.62, 0.18], [-0.92, 0.2, 0.36]],
      };
    case "cheer": {
      const w = 0.15 * Math.sin(t * 12);
      return {
        torsoX: 0.1, headX: 0.3, headY: 0, bounce: 0.07 * Math.abs(Math.sin(t * 9)), spin: 0.35 * Math.sin(t * 5),
        L: [[-0.5 - w, 1, -0.05], [-0.3 - w, 1, -0.1]],
        R: [[0.5 + w, 1, -0.05], [0.3 + w, 1, -0.1]],
      };
    }
    case "facepalm":
      return {
        torsoX: -0.16, headX: -0.22, headY: 0.1 * Math.sin(t * 6), bounce: 0, spin: 0,
        L: [[0.12, -0.8, -0.55], [0.22, -0.1, -1]],
        R: [[-0.25, 0.1, -1], [-0.7, 0.72, 0.05]],
      };
    case "wave": {
      const base = typing();
      // swivel round to face the camera behind him, then wave
      return { ...base, spin: -2.75, torsoX: 0.05, headX: 0.12, headY: 0, L: [[-0.25, -0.9, 0.1], [-0.1, -0.6, -0.8]], R: [[0.75, 0.75, 0.05], [0.2 + 0.5 * Math.sin(t * 10), 1, 0]] };
    }
    case "nod":
      return { ...typing(), headX: -0.1 + 0.2 * Math.max(0, Math.sin(t * 8)) };
    default:
      return typing();
  }
}

const DOWN = new THREE.Vector3(0, -1, 0);
const _aim = new THREE.Quaternion();
const _inv = new THREE.Quaternion();
const _dir = new THREE.Vector3();
function aimArm(arm, [upper, fore], k) {
  _aim.setFromUnitVectors(DOWN, _dir.set(...upper).normalize());
  arm.sh.quaternion.slerp(_aim, k);
  // the forearm direction is given in the trader's space; turn it into the shoulder's own space
  _inv.copy(arm.sh.quaternion).invert();
  _aim.setFromUnitVectors(DOWN, _dir.set(...fore).normalize().applyQuaternion(_inv));
  arm.el.quaternion.slerp(_aim, k);
}

function animateDude(name, t, dt) {
  const d = office.dude;
  if (!d) return;
  const want = poseTargets(name, t);
  const k = REDUCED || !d.posed ? 1 : 1 - Math.exp(-dt * 9);
  d.posed = true;
  const c = (d.cur ||= { torsoX: want.torsoX, headX: want.headX, headY: want.headY, bounce: want.bounce, spin: want.spin });
  for (const key of ["torsoX", "headX", "headY", "bounce"]) c[key] += (want[key] - c[key]) * k;
  // the chair turns slower than the arms move
  c.spin += (want.spin - c.spin) * (REDUCED || k === 1 ? 1 : 1 - Math.exp(-dt * 4.5));
  if (office.seat) office.seat.rotation.y = c.spin;
  d.hips.position.y = 0.5 + c.bounce;
  d.torso.rotation.x = c.torsoX;
  d.neck.rotation.set(c.headX, c.headY, 0);
  aimArm(d.arms.L, want.L, k);
  aimArm(d.arms.R, want.R, k);
  const mood = name === "cheer" || name === "wave" ? "happy" : name === "facepalm" ? "sad" : "neutral";
  if (mood !== d.mood) {
    d.mood = mood;
    d.faceMat.map = d.faceMat.emissiveMap = d.faces[mood];
    d.faceMat.needsUpdate = true;
  }
}

/* ---------- what the trader says and how he reacts ---------- */
function dudeLine(b) {
  if (!b.enabled) return "I'm paused. Switch trading on in Settings and I'll get back to it.";
  const held = b.positions.map((p) => p.ticker);
  const hold = held.length ? `Holding ${held.join(", ")}.` : "Not holding anything.";
  if (marketOpen() === false) return `Market's closed. ${hold} I start again at ${tradingStarts()} New York time.`;
  const sig = (b.intraday_signals || []).find((r) => !held.includes(r.ticker));
  return `${hold} ${sig ? `Watching ${sig.ticker}.` : "Looking for a stock that's running."}`;
}
function say(text, ms = 5000) {
  const el = $("bubble");
  el.textContent = text;
  el.hidden = false;
  office.bubbleUntil = performance.now() + ms;
}
function act(name, ms) {
  office.act = name;
  office.actUntil = performance.now() + ms;
}
function greet() {
  const b = bot();
  if (!b || office.focus) return;
  act("wave", 2600);
  say(dudeLine(b));
}
function reactTo(t) {
  const r = t.reason || "";
  if (t.side === "buy") {
    act("nod", 1500);
    say(`Bought ${t.shares} ${t.ticker} at ${money(t.price, 2)}.`);
    return;
  }
  // made or lost money? Use what the shares cost when the server sends it, else read the reason
  const sign = r.match(/\(([+-])\d/);
  const mood = t.cost ? Math.sign(t.price - t.cost) : /^Take profit/.test(r) ? 1 : /^Stop/.test(r) ? -1 : sign ? (sign[1] === "+" ? 1 : -1) : 0;
  if (mood > 0) {
    act("cheer", 2600);
    say(`Sold ${t.ticker}! ${r}.`);
  } else if (mood < 0) {
    act("facepalm", 2800);
    say(`Ugh. Sold ${t.ticker}. ${r}.`);
  } else {
    act("nod", 1500);
    say(`Sold ${t.ticker}. ${r}.`);
  }
}
function checkNewTrades() {
  const b = bot();
  if (!b) return;
  const key = tradeKey(b.trades[0]);
  const seen = office.seen[b.id];
  office.seen[b.id] = key;
  if (seen === undefined || !key || key === seen) return;
  const fresh = [];
  for (const t of b.trades) {
    if (tradeKey(t) === seen) break;
    fresh.push(t);
  }
  reactTo(fresh.find((t) => t.side === "sell") || fresh[0]);
}

/* ---------- entering, leaving, focusing ---------- */
function setOfficeMode(on) {
  $("app").classList.toggle("in-office", on);
  css3d.domElement.hidden = !on;
  $("office-nav").hidden = !on;
  controls.enabled = !on;
  renderPass.scene = on ? officeScene : scene;
  renderPass.camera = on ? officeCam : camera;
  bloom.strength = on ? 0.5 : 0.85;
  bloom.threshold = on ? 0.55 : 0.18;
  if (!on) $("bubble").hidden = true;
  syncFlat();
  layoutInsets();
  canvas.classList.remove("hovering");
  canvas.setAttribute("aria-label", on ? "A trader at his desk. Click a monitor to zoom in." : "3D city of trading bots. Click a building to visit its trader.");
}

// The whole desk: every monitor, the trader and the front of the desk, kept clear of the top bar
// and the buttons at the bottom. Found by backing the camera away until all of it fits.
const OVERVIEW_DIR = new THREE.Vector3(0.12, 0.5, 1).normalize();
function overviewPose() {
  const t = new THREE.Vector3(-0.06, 1.28, -0.45);
  const pts = [new THREE.Vector3(0, 1.66, 0.45), new THREE.Vector3(0, 0.42, 0.62), new THREE.Vector3(-1.8, 0.75, 0), new THREE.Vector3(1.6, 0.75, 0)];
  for (const m of MONITORS) {
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(-m.tilt, m.yaw, 0, "YXZ"));
    for (const [sx, sy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]])
      pts.push(new THREE.Vector3((sx * m.w) / 2, (sy * m.h) / 2, 0).applyQuaternion(q).add(new THREE.Vector3(m.x, m.y, m.z)));
  }
  const H = canvas.clientHeight || 1;
  const band = layoutInsets();
  const top = 1 - (2 * band.top) / H;
  const bottom = -1 + (2 * band.bottom) / H;
  const cam = officeCam.clone();
  const fits = (d) => {
    cam.position.copy(t).addScaledVector(OVERVIEW_DIR, d);
    cam.lookAt(t);
    cam.updateMatrixWorld();
    return pts.every((p) => {
      const s = p.clone().project(cam);
      return Math.abs(s.x) < 0.96 && s.y < top && s.y > bottom;
    });
  };
  let lo = 1.5, hi = 40;
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) hi = mid;
    else lo = mid;
  }
  return { p: t.clone().addScaledVector(OVERVIEW_DIR, hi), t };
}

function monitorPose(id) {
  const m = office.mons[id];
  const n = new THREE.Vector3(0, 0, 1).applyQuaternion(m.group.quaternion);
  const tan = Math.tan(THREE.MathUtils.degToRad(officeCam.fov / 2));
  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  const pxW = m.w * PX_PER_M;
  const pxH = m.h * PX_PER_M;
  // size on screen: at most 1:1, otherwise as big as fits between the top bar and the monitor buttons
  const band = layoutInsets();
  const scale = Math.max(0.05, Math.min(1, (W - 24) / pxW, (H - band.top - band.bottom) / pxH));
  const d = H / (2 * tan * PX_PER_M * scale);
  // aim off-centre so the monitor sits in the middle of that free band
  const t = m.group.position.clone().add(new THREE.Vector3(0, (band.top - band.bottom) / 2 / (PX_PER_M * scale), 0).applyQuaternion(m.group.quaternion));
  return { p: m.group.position.clone().addScaledVector(n, d).add(t.clone().sub(m.group.position)), t };
}

// On phones the open monitor's page moves into a flat panel that fills the screen, so it stays readable.
// phones, in either direction, and other small screens
function narrow() {
  return innerWidth <= 760 || innerHeight <= 500;
}
function syncFlat() {
  const id = office.active && office.focus && !office.busy && narrow() ? office.focus : null;
  for (const m of Object.values(office.mons)) {
    const home = m.id === id ? $("flat-mon") : m.el;
    if (m.body.parentElement !== home) {
      const top = m.body.scrollTop;
      home.appendChild(m.body);
      m.body.scrollTop = top;
    }
  }
  $("flat-mon").hidden = !id;
  // the 3D pages behind the flat one are empty shells: hide them so they don't show through or take taps
  css3d.domElement.style.visibility = id ? "hidden" : "";
  if (id) $("flat-mon").querySelector(".mon-bar b").textContent = office.mons[id].title;
}

// The free band between the top bar (and banner) and the bottom buttons, in pixels from each edge.
// Monitors and the flat panel are fitted into it, so nothing ends up underneath a bar.
function layoutInsets() {
  const app = $("app");
  const y0 = app.getBoundingClientRect().top;
  const hudBottom = document.querySelector(".hud").getBoundingClientRect().bottom - y0;
  app.style.setProperty("--hud-bottom", `${Math.round(hudBottom)}px`);
  const banner = $("banner");
  const top = Math.round((office.active && !banner.hidden ? Math.max(hudBottom, banner.getBoundingClientRect().bottom - y0) : hudBottom) + 10);
  const nav = $("office-nav");
  const bottom = nav.hidden ? 120 : Math.round(app.clientHeight - (nav.getBoundingClientRect().top - y0) + 10);
  app.style.setProperty("--top-inset", `${top}px`);
  app.style.setProperty("--bottom-inset", `${bottom}px`);
  return { top, bottom };
}
// put the camera exactly where it belongs for the current screen (after a resize, or a bar changing size)
function snapPose() {
  if (!office.active || tweens.length) return;
  const pose = office.focus ? monitorPose(office.focus) : overviewPose();
  officeCam.position.copy(pose.p);
  office.look.copy(pose.t);
}
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => {
    layoutInsets();
    snapPose();
  });
  for (const el of [document.querySelector(".hud"), $("banner"), $("office-nav")]) ro.observe(el);
}

function syncMonitors() {
  for (const m of Object.values(office.mons)) {
    const on = m.id === office.focus;
    m.el.classList.toggle("focused", on);
    m.el.querySelector(".mon-hint").textContent = "Click to open";
    m.body.inert = !on;
  }
  document.querySelectorAll("#office-nav [data-mon]").forEach((btn) => btn.setAttribute("aria-pressed", String((btn.dataset.mon || null) === office.focus)));
}

async function openBuilding(id) {
  if (office.busy || !STATE.bots.some((b) => b.id === id)) return;
  if (office.active && openId === id) return focusMonitor(null);
  office.busy = true;
  try {
    if (!office.built) buildOffice();
    if (!office.active) {
      office.cityZoom = camera.zoom;
      controls.autoRotate = false;
      const pos = buildings[id]?.pos;
      if (pos && !REDUCED) {
        focusTarget.copy(pos);
        const z0 = camera.zoom;
        const z1 = Math.min(3.2, z0 * 2.6);
        await tween(520, (k) => {
          camera.zoom = z0 + (z1 - z0) * k;
          camera.updateProjectionMatrix();
        });
      }
    }
    await fade(true);
    openId = id;
    draft = structuredClone(bot().settings);
    dressOffice(bot());
    office.active = true;
    office.focus = null;
    office.seen[id] = tradeKey(bot().trades[0]);
    setOfficeMode(true);
    renderScreens();
    syncMonitors();
    renderHUD();
    const pose = overviewPose();
    office.look.copy(pose.t);
    officeCam.position.copy(pose.p).add(REDUCED ? new THREE.Vector3() : new THREE.Vector3(0.5, 1.4, 2.2));
    await fade(false);
    await flyTo(pose, 1100);
    snapPose();
  } finally {
    office.busy = false;
  }
  greet();
}

async function closeBuilding() {
  if (!office.active || office.busy) return;
  office.busy = true;
  try {
    await fade(true);
    office.active = false;
    office.focus = null;
    openId = null;
    setOfficeMode(false);
    camera.zoom = office.cityZoom;
    camera.updateProjectionMatrix();
    focusTarget.set(0, 0, 0);
    renderHUD();
    await fade(false);
  } finally {
    office.busy = false;
  }
}

async function focusMonitor(id) {
  if (!office.active || office.busy || id === office.focus) return;
  // if the keyboard was inside the monitor that's closing, hand focus to the matching bottom button
  const lost = office.focus && office.mons[office.focus].body.contains(document.activeElement);
  office.busy = true;
  try {
    office.focus = id;
    syncMonitors();
    syncFlat();
    $("bubble").hidden = true;
    if (!id) office.dude.root.visible = true;
    await flyTo(id ? monitorPose(id) : overviewPose(), id ? 650 : 750, (k) => {
      if (id && k > 0.5) office.dude.root.visible = false;
    });
  } finally {
    office.busy = false;
  }
  snapPose();
  syncFlat();
  if (id) office.mons[id].body.focus({ preventScroll: true });
  else if (lost && (document.activeElement === document.body || !document.activeElement)) document.querySelector('#office-nav [data-mon=""]').focus({ preventScroll: true });
}


function stepBuilding(dir) {
  const ids = STATE.bots.map((b) => b.id);
  openBuilding(ids[(ids.indexOf(openId) + dir + ids.length) % ids.length]);
}

$("o-back").addEventListener("click", closeBuilding);
$("o-prev").addEventListener("click", () => stepBuilding(-1));
$("o-next").addEventListener("click", () => stepBuilding(1));
document.querySelectorAll("#office-nav [data-mon]").forEach((btn) => btn.addEventListener("click", () => focusMonitor(btn.dataset.mon || null)));
addEventListener("keydown", (e) => {
  if (!office.active || !$("unlock-modal").hidden || !$("connect-modal").hidden) return;
  if (e.key === "Escape") {
    e.preventDefault();
    if (office.focus) focusMonitor(null);
    else closeBuilding();
    return;
  }
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
  if (office.focus && !typing && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
    const i = NAV_ORDER.indexOf(office.focus);
    const n = NAV_ORDER.length;
    const right = e.key === "ArrowRight";
    focusMonitor(NAV_ORDER[i < 0 ? (right ? 0 : n - 1) : (i + (right ? 1 : n - 1)) % n]);
  }
});

function pickDude(ev) {
  if (!office.dude?.root.visible) return false;
  const r = canvas.getBoundingClientRect();
  pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(pointer, officeCam);
  return raycaster.intersectObjects(office.dude.parts, false).length > 0;
}

function officeFrame(t, dt, now) {
  const b = bot();
  if (office.act && now > office.actUntil) office.act = null;
  const base = !b?.enabled || marketOpen() === false ? "relaxed" : "typing";
  animateDude(office.act || base, REDUCED ? 0 : t, dt);
  if (!REDUCED) office.anims.forEach((fn) => fn(t, dt));
  // a slow drift while looking at the whole desk; perfectly still when a monitor is open
  const drift = !office.focus && !tweens.length && !REDUCED;
  officeCam.lookAt(office.look.x + (drift ? Math.sin(t * 0.25) * 0.05 : 0), office.look.y + (drift ? Math.sin(t * 0.18) * 0.02 : 0), office.look.z);

  const bubble = $("bubble");
  if (!bubble.hidden) {
    if (now > office.bubbleUntil || office.focus || !office.dude) bubble.hidden = true;
    else {
      const p = office.dude.neck.getWorldPosition(new THREE.Vector3()).add(new THREE.Vector3(0, 0.62, 0)).project(officeCam);
      const x = ((p.x + 1) / 2) * canvas.clientWidth;
      const y = ((1 - p.y) / 2) * canvas.clientHeight;
      bubble.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) translate(-50%, -100%)`;
    }
  }
}

/* ---------- what's on each monitor ---------- */
function renderScreens(withSettings = true) {
  const b = bot();
  if (!b || !office.built) return;
  const m = office.mons;
  m.status.body.innerHTML = statusHTML(b);
  drawSpark(m.status.body.querySelector(".spark"), b);
  m.holdings.body.innerHTML = planHTML(b);
  m.trades.body.innerHTML = tradesHTML(b);
  m.picks.body.innerHTML = picksHTML(b);
  if (withSettings) {
    const sb = m.settings.body;
    const focused = sb.contains(document.activeElement) && document.activeElement.id;
    const top = sb.scrollTop;
    sb.innerHTML = settingsHTML(b);
    wireSettings(sb, b);
    sb.scrollTop = top;
    if (focused) sb.querySelector(`#${CSS.escape(focused)}`)?.focus({ preventScroll: true });
  }
  $("o-name").textContent = b.name;
}

function statusHTML(b) {
  return `<div class="st">
    <div class="st-name"><span class="eyebrow">${esc(b.sector)} · ${esc(riskName(b.settings.risk))} risk</span><h2>${esc(b.name)}</h2><span class="pill ${b.enabled ? "on" : "paused"}">${b.enabled ? "Active" : "Paused"}</span></div>
    <div class="st-fig"><span class="stat-label">Value</span><span class="big">${money(b.equity, 2)}</span></div>
    <div class="st-fig"><span class="stat-label">All-time</span><span class="mid ${cls(b.pnl)}">${signedMoney(b.pnl)} (${pct(b.pnl_pct)})</span></div>
    <div class="st-fig"><span class="stat-label">Today</span><span class="mid ${cls(b.day_change)}">${signedMoney(b.day_change)}</span></div>
    <div class="st-fig"><span class="stat-label">Cash</span><span class="mid">${money(b.cash)}</span></div>
    <svg class="spark" viewBox="0 0 320 80" preserveAspectRatio="none" role="img" aria-label="Value over time"></svg>
  </div>`;
}

function planLine(p, s) {
  const day = s.intraday;
  const m = s.momentum;
  if (p.sleeve === "intraday") {
    return `Sells at <b class="up">${money(p.avg_cost * (1 + day.take_profit_pct), 2)}</b> (+${num1(day.take_profit_pct * 100)}%) or <b class="down">${money(p.avg_cost * (1 - day.stop_pct), 2)}</b> (−${num1(day.stop_pct * 100)}%), or sooner if the run fades. Out by ${esc(day.close_out_at)} at the latest.`;
  }
  const hi = Math.max(p.high || 0, p.avg_cost);
  const trail = hi > p.avg_cost ? ` or if it slips to <b class="down">${money(hi * (1 - m.trailing_stop_pct), 2)}</b> (${num1(m.trailing_stop_pct * 100)}% off its high)` : "";
  const who = p.sleeve === "ai" ? "AI pick, held for days." : "Momentum pick, held while it stays near the top.";
  return `${who} Sells below <b class="down">${money(p.avg_cost * (1 - m.stop_loss_pct), 2)}</b> (−${num1(m.stop_loss_pct * 100)}%)${trail}.`;
}

function planHTML(b) {
  const s = b.settings;
  const open = marketOpen();
  const held = new Set(b.positions.map((p) => p.ticker));
  const status = !b.enabled
    ? `<p class="plan-note paused">Paused. Switch trading on in Settings and he gets back to work.</p>`
    : open === false
      ? `<p class="plan-note">Market's closed. He starts checking again at ${esc(tradingStarts())} New York time.</p>`
      : `<p class="plan-note on">Checking for trades every ${s.check_every_minutes} min${b.last_check ? ` · last check ${esc(nyTime(b.last_check))}` : ""}.</p>`;
  const rows = b.positions.length
    ? b.positions
        .map(
          (p) => `<div class="hold">
      <div class="hold-top"><span class="tk">${esc(p.ticker)}<span class="sleeve ${p.sleeve}">${SLEEVE_TAG[p.sleeve] || "MOM"}</span></span>
        <span class="num">${p.shares} ${p.shares === 1 ? "share" : "shares"}</span>
        <span class="num">bought ${money(p.avg_cost, 2)} · now ${money(p.price, 2)}</span>
        <span class="num gain ${cls(p.pnl_pct)}">${pct(p.pnl_pct, 2)}</span></div>
      <div class="hold-plan">${planLine(p, s)}</div></div>`,
        )
        .join("")
    : `<p class="empty">Not holding anything right now.</p>`;
  const cash = `<p class="cash-line num">Cash ${money(b.cash, 2)}${b.settling ? ` · ${money(b.settling, 2)} of it is from today's sales and can buy again next trading day` : ""}</p>`;
  const notes = b.notes.length ? `<div class="section-title">Notes</div><ul class="notes">${b.notes.slice(0, 3).map((n) => `<li>${esc(n.text)}</li>`).join("")}</ul>` : "";
  return `${status}<div class="section-title">Holding</div>${rows}${cash}<div class="section-title">Next up</div>${nextUpHTML(b, held)}${notes}`;
}

function nextUpHTML(b, held) {
  const s = b.settings;
  const items = [];
  if (s.style === "intraday") {
    const need = num1(s.intraday.entry_pct * 100);
    for (const r of (b.intraday_signals || []).filter((x) => !held.has(x.ticker)).slice(0, 4)) {
      const move = `${r.move >= 0 ? "up" : "down"} ${num1(Math.abs(r.move) * 100)}%`;
      items.push(
        r.qualifies
          ? `<li><b>${esc(r.ticker)}</b> is ${move} in ${s.intraday.lookback_minutes} min and above VWAP. <span class="up">Ready to buy</span> on the next check if there's a free slot and cash.</li>`
          : `<li><b>${esc(r.ticker)}</b> is ${move}; needs +${need}%${r.above_vwap ? "" : " and to get back above VWAP"}.</li>`,
      );
    }
  } else {
    for (const r of b.ranking.filter((x) => x.qualifies && !held.has(x.ticker)).slice(0, 3)) items.push(`<li><b>${esc(r.ticker)}</b> ranks high on momentum (${pct(r.score)}).</li>`);
  }
  const ai = (b.ai?.picks || []).map((p) => p.ticker).filter((t) => !held.has(t));
  if (ai.length) items.push(`<li>AI picks not bought yet: <b>${ai.map(esc).join(", ")}</b>. They're bought when there's cash for a whole share.</li>`);
  if (!items.length)
    items.push(marketOpen() === false ? "<li>The watch list fills in once the market opens.</li>" : `<li>Nothing is moving enough yet. He's watching all ${s.universe.length} stocks.</li>`);
  return `<ul class="next">${items.join("")}</ul>`;
}

function drawSpark(svg, b) {
  if (!svg) return;
  const h = b.history;
  if (h.length < 2) {
    svg.innerHTML = `<text x="0" y="44">Chart appears after the second day.</text>`;
    return;
  }
  const W = 320, H = 80, pad = 4;
  const vals = h.map((p) => p.equity).concat([b.contributed]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const x = (i) => (i / (h.length - 1)) * W;
  const y = (v) => H - pad - ((v - lo) / (hi - lo || 1)) * (H - pad * 2 - 12);
  const pts = h.map((p, i) => `${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const up = h[h.length - 1].equity >= b.contributed;
  const col = up ? "var(--up)" : "var(--down)";
  svg.innerHTML = `
    <defs><linearGradient id="sg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${up ? "#4ade80" : "#fb7185"}" stop-opacity="0.35"/><stop offset="1" stop-color="${up ? "#4ade80" : "#fb7185"}" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" x2="${W}" y1="${y(b.contributed)}" y2="${y(b.contributed)}" stroke="#a49fcf" stroke-dasharray="3 4" stroke-width="1" opacity="0.6" vector-effect="non-scaling-stroke"/>
    <polygon points="0,${H} ${pts} ${W},${H}" fill="url(#sg)"/>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <text x="2" y="10">${esc(h[0].date)}</text><text x="${W - 2}" y="10" text-anchor="end">${esc(h[h.length - 1].date)}</text>`;
}

const plTag = (pl) => ` <b class="pl ${cls(pl)}">${pl >= 0 ? "+" : "−"}${money(Math.abs(pl), 2)}</b>`;
function tradesHTML(b) {
  if (!b.trades.length) return `<p class="empty">No trades yet.</p>`;
  return b.trades
    .slice(0, 60)
    .map(
      (t) => `<div class="trade"><span class="side ${t.side}">${t.side.toUpperCase()}</span>
      <span class="what">${t.shares} ${esc(t.ticker)} @ ${money(t.price, 2)}<span class="sleeve ${t.sleeve}">${SLEEVE_TAG[t.sleeve] || "MOM"}</span>${t.side === "sell" && t.cost ? plTag((t.price - t.cost) * t.shares) : ""}</span>
      <span class="when">${esc(t.date)}${t.time ? " " + esc(t.time) : ""}</span><span class="why">${esc(t.reason)}</span></div>`,
    )
    .join("");
}

function picksHTML(b) {
  const ai = b.ai || {};
  let aiPart;
  if (ai.status === "ok") {
    aiPart = `${ai.market_view ? `<p class="view">“${esc(ai.market_view)}”</p>` : ""}
      ${ai.picks.length ? ai.picks.map((p) => `<div class="pick"><div class="pick-head"><b>${esc(p.ticker)}</b><span class="conf">confidence ${Math.round(p.confidence * 100)}%</span></div><p>${esc(p.reason)}</p></div>`).join("") : `<p class="empty">No AI picks right now. That part of the building is holding cash.</p>`}
      ${ai.rejected?.length ? `<p class="blocked">Blocked: ${ai.rejected.map((r) => `${esc(r.ticker)} (${esc(r.reason)})`).join(", ")}</p>` : ""}
      <p class="blocked">Picked ${esc(ai.date)} by ${esc(ai.model)}.</p>`;
  } else if (ai.status === "error") {
    aiPart = `<p class="empty">${esc(ai.error)}</p>`;
  } else aiPart = `<p class="empty">The AI sleeve picks on its first run.</p>`;

  const maxScore = Math.max(0.0001, ...b.ranking.map((r) => Math.abs(r.score)));
  const rank = b.ranking.length
    ? b.ranking
        .map(
          (r) => `<div class="rank-row ${r.qualifies ? "" : "no"}"><span>${esc(r.ticker)}</span>
        <span class="rank-bar"><i style="width:${Math.max(2, (Math.max(0, r.score) / maxScore) * 100)}%"></i></span>
        <span class="${cls(r.score)}" style="text-align:right">${pct(r.score)}</span><span class="ok" title="${r.qualifies ? "In an uptrend" : "Below trend or negative"}">${r.qualifies ? "✓" : ""}</span></div>`,
        )
        .join("")
    : `<p class="empty">Rankings appear after the first run.</p>`;

  const blocked = b.blocked_in_universe?.length
    ? `<p class="blocked">Never bought here: ${b.blocked_in_universe.map((x) => `${esc(x.ticker)} (${esc(x.reason)})`).join(", ")}</p>`
    : "";

  const main =
    b.settings.style === "intraday"
      ? `<div class="section-title">Moving right now · ✓ = up ${num1(b.settings.intraday.entry_pct * 100)}%+ in ${b.settings.intraday.lookback_minutes} min and above VWAP · re-checked every ${b.settings.check_every_minutes} min</div>${moversHTML(b)}`
      : `<div class="section-title">Momentum leaderboard · top ${b.settings.momentum.top_n} with ✓ get bought · re-checked every ${b.settings.check_every_minutes} min</div>${rank}`;
  return `${main}${blocked}<div class="section-title">AI picks · ${Math.round(b.settings.ai_share * 100)}% of this building</div>${aiPart}`;
}

function moversHTML(b) {
  const sig = b.intraday_signals || [];
  if (!sig.length) return `<p class="empty">Movers appear once the market has been open about ${b.settings.intraday.lookback_minutes} minutes.</p>`;
  const top = Math.max(0.0001, ...sig.map((r) => Math.abs(r.move)));
  return sig
    .map(
      (r) => `<div class="rank-row ${r.qualifies ? "" : "no"}"><span>${esc(r.ticker)}</span>
        <span class="rank-bar"><i style="width:${Math.max(2, (Math.max(0, r.move) / top) * 100)}%"></i></span>
        <span class="${cls(r.move)}" style="text-align:right">${pct(r.move, 2)}</span><span class="ok" title="${r.qualifies ? "Running up and above VWAP" : r.above_vwap ? "Not moving enough yet" : "Below VWAP"}">${r.qualifies ? "✓" : ""}</span></div>`,
    )
    .join("")
    + `<p class="blocked">VWAP is today's volume-weighted average price. A stock above it has been trading stronger than average today.</p>`;
}

/* ---------- settings ---------- */
const CHECKS = [[2, "2 minutes"], [3, "3 minutes"], [5, "5 minutes"], [15, "15 minutes"], [30, "30 minutes"], [60, "hour"], [390, "day"]];
const AI_EVERY = [[60, "hour"], [120, "2 hours"], [240, "4 hours"], [390, "day"], [1950, "week"]];
const LOOKBACKS = [[63, "3 months"], [126, "6 months"], [189, "9 months"], [252, "12 months"]];

const STYLES = [["intraday", "Day trading (in and out within the day)"], ["swing", "Swing (hold days to weeks)"]];

function settingsHTML(b) {
  const d = draft;
  const aiPct = Math.round(d.ai_share * 100);
  const day = d.intraday;
  const isDay = d.style === "intraday";
  const opt = (list, cur) => list.map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`).join("");
  const dayFields = `
    <div class="two">
      <div class="field"><label for="s-entry">Buy when up at least (%)</label><input id="s-entry" type="number" min="0.05" max="5" step="any" value="${num1(day.entry_pct * 100)}"></div>
      <div class="field"><label for="s-lookmin">…over the last (minutes)</label><input id="s-lookmin" type="number" min="2" max="120" step="1" value="${day.lookback_minutes}"></div>
    </div>
    <div class="two">
      <div class="field"><label for="s-tp">Take profit at (%)</label><input id="s-tp" type="number" min="0.1" max="20" step="any" value="${num1(day.take_profit_pct * 100)}"></div>
      <div class="field"><label for="s-dstop">Stop at (%)</label><input id="s-dstop" type="number" min="0.1" max="20" step="any" value="${num1(day.stop_pct * 100)}"></div>
    </div>
    <div class="two">
      <div class="field"><label for="s-maxpos">Day trades open at once</label><input id="s-maxpos" type="number" min="1" max="10" value="${day.max_positions}"></div>
      <div class="field"><label for="s-cool">Wait before re-buying (minutes)</label><input id="s-cool" type="number" min="0" max="390" step="1" value="${day.cooldown_minutes}"></div>
    </div>
    <span class="help">Buys a stock that is up this much over the last few minutes and above VWAP. Sells at the take profit, at the stop, when the run fades, and always by ${esc(day.close_out_at)} so nothing is held overnight. No new buys after ${esc(day.no_entries_after)}.</span>`;
  const swingFields = `
    <div class="two">
      <div class="field"><label for="s-topn">Momentum stocks held</label><input id="s-topn" type="number" min="1" max="10" value="${d.momentum.top_n}"></div>
      <div class="field"><label for="s-look">Momentum looks back</label><select id="s-look">${opt(LOOKBACKS, d.momentum.lookback_days)}</select></div>
    </div>`;
  return `<form class="settings" id="settings-form" novalidate>
    <div class="toggle"><input type="checkbox" id="s-enabled" ${d.enabled ? "checked" : ""}><label for="s-enabled">Trading on (untick to pause this building)</label></div>
    <div class="field"><label for="s-cash">Money in this building ($)</label>
      <input id="s-cash" type="number" min="0" step="1" value="${d.starting_cash}">
      <span class="help">Raising it adds cash on the next check; lowering it takes cash out (only uninvested cash).</span></div>
    <div class="field"><label for="s-style">Trading style</label><select id="s-style">${opt(STYLES, d.style)}</select></div>
    <div class="field"><span class="flabel">Strategy split</span>
      <div class="split"><span class="m" style="width:${100 - aiPct}%"></span><span class="a" style="width:${aiPct}%"></span></div>
      <div class="split-legend">${legendHTML(d, aiPct)}</div>
      <input id="s-ai" type="range" min="0" max="100" step="5" value="${aiPct}" aria-label="AI share"></div>
    <div class="field"><span class="flabel">Risk</span>
      <div class="risk-head"><b id="risk-name">${riskName(d.risk)}</b><span id="risk-level">${d.risk ? `${d.risk} of ${maxRisk()}` : ""}</span></div>
      <input id="s-risk" class="risk-range" type="range" min="1" max="${maxRisk()}" step="1" value="${d.risk || 3}" aria-label="Risk, from less risky to more risky">
      <div class="risk-ends"><span>Less risky</span><span>More risky</span></div>
      <span class="help" id="risk-sum">${riskSummary(d)}</span></div>
    <div class="field"><span class="flabel">Stocks this building can trade (${d.universe.length})</span>
      <div class="chips" id="s-chips">${d.universe.map((t) => `<span class="chip">${esc(t)}<button type="button" data-rm="${esc(t)}" aria-label="Remove ${esc(t)}">✕</button></span>`).join("")}</div>
      <div class="add-row"><input id="s-add" type="text" placeholder="Add ticker, e.g. IBM" maxlength="8" autocomplete="off"><button type="button" id="s-add-btn">Add</button></div>
      <span class="err" id="s-err"></span></div>
    <details class="fine" id="s-fine" ${fineOpen ? "open" : ""}><summary>Fine-tune (optional)</summary>
    <span class="help">The risk slider sets these for you. Changing one here switches the slider to Custom.</span>
    ${isDay ? dayFields : swingFields}
    <div class="two">
      <div class="field"><label for="s-check">Check for trades every</label><select id="s-check">${opt(CHECKS, d.check_every_minutes)}</select></div>
      <div class="field"><label for="s-cap">Max buys per day</label><input id="s-cap" type="number" min="1" max="500" value="${d.max_buys_per_day}"></div>
    </div>
    <span class="help">Selling is never capped, so a stop or close-out always goes through.</span>
    <div class="section-title">${isDay ? "AI picks (held for days)" : "Risk and AI picks"}</div>
    <div class="two">
      <div class="field"><label for="s-aipicks">AI stocks held</label><input id="s-aipicks" type="number" min="1" max="5" value="${d.ai.max_picks}"></div>
      <div class="field"><label for="s-aievery">AI re-picks every</label><select id="s-aievery">${opt(AI_EVERY, d.ai.review_every_minutes)}</select></div>
    </div>
    <div class="two">
      <div class="field"><label for="s-stop">Stop loss (%)</label><input id="s-stop" type="number" min="1" max="90" step="any" value="${num1(d.momentum.stop_loss_pct * 100)}"></div>
      <div class="field"><label for="s-trail">Trailing stop (%)</label><input id="s-trail" type="number" min="1" max="90" step="any" value="${num1(d.momentum.trailing_stop_pct * 100)}"></div>
    </div>
    <div class="field"><label for="s-hold">Hold at least (minutes)</label><input id="s-hold" type="number" min="0" max="10080" step="1" value="${d.min_hold_minutes}">
      <span class="help">${isDay ? "These three apply to the AI picks." : "These apply to every holding."} Stop loss sells when a stock falls this far below what the bot paid; trailing stop sells a winner that falls this far from its high. Each AI review is one Claude request, roughly 3–5¢.</span></div>
    </details>
    <div class="actions"><button type="submit" class="primary" id="s-save">${MODE === "server" ? (api.unlocked() ? "Save changes" : "Unlock to save") : gh.connected() ? "Save to GitHub" : "Save changes"}</button><button type="button" id="s-reset">Undo changes</button></div>
    <div id="s-out"></div>
  </form>`;
}

let fineOpen = false;
const riskLevels = () => STATE.risk_levels || {};
const maxRisk = () => Math.max(5, ...Object.keys(riskLevels()).map(Number));
const riskName = (level) => (level ? riskLevels()[level]?.name || `Level ${level}` : "Custom");

function applyRisk(d, level) {
  const p = riskLevels()[level];
  if (!p) return;
  Object.assign(d.intraday, p.intraday);
  Object.assign(d.momentum, p.momentum);
  d.risk = level;
}

function detectRisk(d) {
  for (const [level, p] of Object.entries(riskLevels())) {
    const same = ["intraday", "momentum"].every((sec) => Object.entries(p[sec]).every(([k, v]) => Math.abs(d[sec][k] - v) < 1e-9));
    if (same) return Number(level);
  }
  return 0;
}

function riskSummary(d) {
  if (!d.risk) return "You've set your own numbers under Fine-tune. Move the slider to go back to a preset.";
  const pc = (x) => `${num1(x * 100)}%`;
  const day = d.intraday, m = d.momentum;
  const ai = `AI picks: ${pc(m.stop_loss_pct)} stop loss, ${pc(m.trailing_stop_pct)} trailing stop.`;
  if (d.style !== "intraday") return `Holds the top ${m.top_n} momentum ${m.top_n === 1 ? "stock" : "stocks"}, sold at a ${pc(m.stop_loss_pct)} loss or after falling ${pc(m.trailing_stop_pct)} from a high.`;
  const n = day.max_positions === 1 ? "one stock at a time" : `${day.max_positions} stocks at a time`;
  return `Buys a stock up ${pc(day.entry_pct)} in ${day.lookback_minutes} min, takes profit at +${pc(day.take_profit_pct)}, sells at −${pc(day.stop_pct)}, ${n}. ${ai}`;
}

function legendHTML(d, aiPct) {
  return `<span style="color:var(--neon-2)">${mainName(d)} ${100 - aiPct}%</span><span style="color:#f9a8d4">AI picks ${aiPct}%</span>`;
}

function blockedReason(t) {
  const ex = STATE.exclusions || {};
  return (ex.blocked_tickers || {})[t] || null;
}

function wireSettings(root, b) {
  const num = (id) => Number(root.querySelector(id).value);
  const has = (id) => !!root.querySelector(id);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  // a percent box as a fraction, to the 2 decimals the box shows (0.07% -> 0.0007)
  const pctOf = (id, lo, hi) => Math.round(clamp(num(id), lo, hi) * 100) / 10000;
  const sync = () => {
    draft.enabled = root.querySelector("#s-enabled").checked;
    draft.starting_cash = Math.max(0, num("#s-cash"));
    draft.style = root.querySelector("#s-style").value;
    draft.ai_share = num("#s-ai") / 100;
    draft.ai.max_picks = clamp(Math.round(num("#s-aipicks")), 1, 5);
    draft.check_every_minutes = num("#s-check");
    draft.ai.review_every_minutes = num("#s-aievery");
    draft.min_hold_minutes = clamp(Math.round(num("#s-hold")), 0, 10080);
    draft.max_buys_per_day = clamp(Math.round(num("#s-cap")), 1, 500);
    draft.momentum.stop_loss_pct = pctOf("#s-stop", 1, 90);
    draft.momentum.trailing_stop_pct = pctOf("#s-trail", 1, 90);
    if (has("#s-topn")) {
      draft.momentum.top_n = clamp(Math.round(num("#s-topn")), 1, 10);
      draft.momentum.lookback_days = num("#s-look");
      draft.momentum.short_lookback_days = Math.round(num("#s-look") / 2);
    }
    if (has("#s-entry")) {
      const day = draft.intraday;
      day.entry_pct = pctOf("#s-entry", 0.05, 5);
      day.lookback_minutes = clamp(Math.round(num("#s-lookmin")), 2, 120);
      day.take_profit_pct = pctOf("#s-tp", 0.1, 20);
      day.stop_pct = pctOf("#s-dstop", 0.1, 20);
      day.max_positions = clamp(Math.round(num("#s-maxpos")), 1, 10);
      day.cooldown_minutes = clamp(Math.round(num("#s-cool")), 0, 390);
    }
  };
  office.syncSettings = sync;
  root.querySelector("#s-style").addEventListener("change", () => {
    sync();
    renderScreens();
  });
  const showRisk = () => {
    root.querySelector("#risk-name").textContent = riskName(draft.risk);
    root.querySelector("#risk-level").textContent = draft.risk ? `${draft.risk} of ${maxRisk()}` : "";
    root.querySelector("#risk-sum").textContent = riskSummary(draft);
  };
  root.querySelector("#s-risk").addEventListener("input", (e) => {
    sync();
    applyRisk(draft, Number(e.target.value));
    showRisk();
  });
  root.querySelector("#s-risk").addEventListener("change", () => renderScreens());
  const fine = root.querySelector("#s-fine");
  fine.addEventListener("toggle", () => (fineOpen = fine.open));
  fine.addEventListener("input", () => {
    sync();
    draft.risk = detectRisk(draft);
    showRisk();
  });
  root.querySelector("#s-ai").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    root.querySelector(".split .m").style.width = `${100 - v}%`;
    root.querySelector(".split .a").style.width = `${v}%`;
    root.querySelector(".split-legend").innerHTML = legendHTML(draft, v);
  });
  root.querySelectorAll("[data-rm]").forEach((btn) =>
    btn.addEventListener("click", () => {
      sync();
      draft.universe = draft.universe.filter((t) => t !== btn.dataset.rm);
      renderScreens();
      $("s-add")?.focus({ preventScroll: true });
    }),
  );
  const add = () => {
    const input = root.querySelector("#s-add");
    const t = input.value.trim().toUpperCase();
    const err = root.querySelector("#s-err");
    if (!/^[A-Z.\-]{1,8}$/.test(t)) return (err.textContent = "Type a ticker symbol like IBM.");
    const why = blockedReason(t);
    if (why) return (err.textContent = `${t} is on the do-not-buy list: ${why}.`);
    if (draft.universe.includes(t)) return (err.textContent = `${t} is already here.`);
    sync();
    draft.universe.push(t);
    renderScreens();
    $("s-add")?.focus();
  };
  root.querySelector("#s-add-btn").addEventListener("click", add);
  root.querySelector("#s-add").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  });
  root.querySelector("#s-reset").addEventListener("click", () => {
    draft = structuredClone(b.settings);
    renderScreens();
  });
  root.querySelector("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!e.currentTarget.checkValidity()) {
      fine.open = true;
      e.currentTarget.reportValidity();
      return;
    }
    sync();
    const out = root.querySelector("#s-out");
    if (MODE === "server") {
      if (!api.unlocked()) return openUnlock();
      const btn = root.querySelector("#s-save");
      btn.disabled = true;
      btn.textContent = "Saving…";
      // the user may switch buildings while this saves, so everything below goes by the saved building's id
      const body = structuredClone(draft);
      try {
        const saved = await api.call(`api/bots/${encodeURIComponent(body.id)}`, { method: "PUT", body: JSON.stringify(body) });
        savedSettings(b, saved);
        toast(`Saved. ${saved.name} uses the new settings from its next check.`);
      } catch (err) {
        saveFailed(body, out, btn, "Save changes", err);
      }
      return;
    }
    if (!gh.connected()) {
      const json = JSON.stringify(draft, null, 2);
      out.innerHTML = `<p class="help">Connect your repo (top right) to save straight from here. Or copy this into <b>config/bots.json</b> on GitHub, replacing the "${esc(b.id)}" building:</p>
        <pre class="json" id="s-json">${esc(json)}</pre><button type="button" id="s-copy">Copy</button>`;
      out.querySelector("#s-copy").addEventListener("click", () =>
        navigator.clipboard.writeText(json).then(() => toast("Copied."), () => {
          const r = document.createRange();
          r.selectNodeContents(out.querySelector("#s-json"));
          getSelection().removeAllRanges();
          getSelection().addRange(r);
          toast("Press Ctrl+C / Cmd+C to copy.");
        }),
      );
      return;
    }
    const btn = root.querySelector("#s-save");
    btn.disabled = true;
    btn.textContent = "Saving…";
    const body = structuredClone(draft);
    try {
      const { sha, json } = await gh.readConfig();
      const i = json.bots.findIndex((x) => x.id === body.id);
      if (i < 0) throw new Error(`No building "${body.id}" in config/bots.json`);
      json.bots[i] = body;
      await gh.writeConfig(json, sha, `Update ${body.name} settings from Stock City`);
      savedSettings(b, body);
      toast(`Saved. ${body.name} uses the new settings on its next run.`);
    } catch (err) {
      saveFailed(body, out, btn, "Save to GitHub", err);
    }
  });
}

// After a save, update that building (the copy this form was built from and the latest one from the server),
// and only redraw the Settings monitor if it still shows that building.
function savedSettings(b, saved) {
  for (const x of new Set([b, STATE.bots.find((y) => y.id === saved.id)])) if (x) Object.assign(x, { settings: structuredClone(saved), enabled: saved.enabled });
  if (openId === saved.id) {
    draft = structuredClone(saved);
    renderScreens();
  }
  renderHUD();
}
function saveFailed(body, out, btn, label, err) {
  if (openId === body.id && out.isConnected) {
    out.innerHTML = `<p class="err">Couldn't save: ${esc(err.message)}</p>`;
    btn.disabled = false;
    btn.textContent = label;
  } else toast(`Couldn't save ${body.name}: ${err.message}`);
}

/* =========================================================================
   Connect + Run now
   ========================================================================= */
function openConnect() {
  $("c-repo").value = gh.repo() || "";
  $("c-token").value = gh.token() || "";
  $("c-msg").textContent = "";
  $("connect-modal").hidden = false;
  $("c-repo").focus();
}
$("btn-connect").addEventListener("click", () => (MODE === "server" ? openUnlock() : openConnect()));

function openUnlock() {
  $("u-pass").value = "";
  $("u-msg").textContent = STATE?.server && !STATE.server.password_set ? "No password is set on the server yet. Add APP_PASSWORD in the server's settings first." : "";
  $("unlock-modal").hidden = false;
  $("u-pass").focus();
}
$("u-cancel").addEventListener("click", () => ($("unlock-modal").hidden = true));
$("u-lock").addEventListener("click", () => {
  store.del("sc.pass");
  $("u-msg").textContent = "Locked on this browser.";
  syncConnectButton();
  if (openId) {
    office.syncSettings?.();
    renderScreens();
  }
});
$("unlock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  store.set("sc.pass", $("u-pass").value);
  try {
    await api.call("api/login", { method: "POST" });
    $("unlock-modal").hidden = true;
    toast("Unlocked. You can change settings and run the bots.");
  } catch (err) {
    store.del("sc.pass");
    $("u-msg").textContent = err.message;
  }
  syncConnectButton();
  if (openId) {
    office.syncSettings?.();
    renderScreens();
  }
});
$("c-cancel").addEventListener("click", () => ($("connect-modal").hidden = true));
$("c-forget").addEventListener("click", () => {
  store.del("sc.repo");
  store.del("sc.token");
  $("c-repo").value = $("c-token").value = "";
  $("c-msg").textContent = "Forgotten on this browser.";
  syncConnectButton();
});
$("connect-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  store.set("sc.repo", $("c-repo").value.trim().replace(/^https:\/\/github.com\//, ""));
  store.set("sc.token", $("c-token").value.trim());
  $("c-msg").textContent = "Checking…";
  try {
    await gh.readConfig();
    $("c-msg").textContent = "Connected. Settings now save to your repo.";
    syncConnectButton();
    if (openId) {
      office.syncSettings?.();
      renderScreens();
    }
  } catch (err) {
    $("c-msg").textContent = `That didn't work: ${err.message}`;
  }
});
function syncConnectButton() {
  $("btn-connect").textContent = MODE === "server" ? (api.unlocked() ? "Unlocked" : "Unlock") : gh.connected() ? "Connected" : "Connect";
}
$("btn-run").addEventListener("click", async () => {
  if (MODE === "server") {
    if (!api.unlocked()) return openUnlock();
    $("btn-run").disabled = true;
    toast("Running the bots…", 20000);
    try {
      const r = await api.call("api/run", { method: "POST" });
      await refresh();
      toast(r.note || "Done.", 6000);
    } catch (err) {
      toast(`Couldn't run: ${err.message}`, 7000);
    }
    $("btn-run").disabled = false;
    return;
  }
  if (!gh.connected()) return openConnect();
  try {
    await gh.runNow();
    toast("Bots are running on GitHub. Refresh in a few minutes to see the results.", 6000);
  } catch (err) {
    toast(`Couldn't start the run: ${err.message}`, 7000);
  }
});

/* =========================================================================
   Boot
   ========================================================================= */
(async function boot() {
  try {
    STATE = await loadState();
  } catch (err) {
    $("banner").textContent = err.message;
    $("banner").hidden = false;
    return;
  }
  layoutCity(STATE.bots);
  resize();
  syncConnectButton();
  renderHUD();
  frame();
  if (MODE === "server") setInterval(refresh, 15000);
})();

async function refresh() {
  try {
    const res = await fetch("api/state", { cache: "no-store" });
    if (!res.ok) return;
    STATE = await res.json();
    renderHUD();
    if (office.active) {
      renderScreens(false);
      checkNewTrades();
    }
  } catch {}
}
