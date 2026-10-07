import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
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
composer.addPass(new RenderPass(scene, camera));
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
  if (id !== "hq") el.addEventListener("click", () => openPanel(id));
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
  hovered = pick(ev);
  canvas.classList.toggle("hovering", !!hovered);
});
canvas.addEventListener("pointerdown", (ev) => (downAt = [ev.clientX, ev.clientY]));
canvas.addEventListener("pointerup", (ev) => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 6) return;
  const id = pick(ev);
  if (id) openPanel(id);
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
}
addEventListener("resize", resize);

const clock = new THREE.Clock();
function frame() {
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = clock.elapsedTime;
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
  if (STATE.server?.last_error) {
    banner.textContent = `Price feed problem: ${STATE.server.last_error}. Retrying automatically.`;
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
    btn.addEventListener("click", () => openPanel(b.id));
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
   Building panel
   ========================================================================= */
let openId = null;
let tab = "holdings";
let draft = null;

function bot() {
  return STATE.bots.find((b) => b.id === openId);
}

function openPanel(id) {
  openId = id;
  draft = structuredClone(bot().settings);
  const b = bot();
  const panel = $("panel");
  panel.hidden = false;
  panel.style.setProperty("--bot-color", b.color);
  focusTarget.copy(buildings[id]?.pos || new THREE.Vector3()).multiplyScalar(0.55);
  controls.autoRotate = false;
  document.querySelectorAll("#building-list button").forEach((x) => x.classList.toggle("active", x.dataset.id === id));
  renderPanel();
}
function closePanel() {
  openId = null;
  $("panel").hidden = true;
  focusTarget.set(0, 0, 0);
  document.querySelectorAll("#building-list button").forEach((x) => x.classList.remove("active"));
}
$("p-close").addEventListener("click", closePanel);
addEventListener("keydown", (e) => e.key === "Escape" && closePanel());
document.querySelectorAll(".tabs button").forEach((btn) =>
  btn.addEventListener("click", () => {
    tab = btn.dataset.tab;
    renderPanel();
  }),
);

const SLEEVE_TAG = { ai: "AI", momentum: "MOM", intraday: "DAY" };
const mainName = (s) => (s.style === "intraday" ? "Day trading" : "Momentum");

function renderPanel() {
  const b = bot();
  if (!b) return;
  $("p-sector").textContent = `${b.sector} · ${Math.round((1 - b.settings.ai_share) * 100)}/${Math.round(b.settings.ai_share * 100)} ${mainName(b.settings).toLowerCase()}/AI`;
  $("p-name").textContent = b.name;
  const st = $("p-status");
  st.textContent = b.enabled ? "Active" : "Paused";
  st.className = "pill " + (b.enabled ? "on" : "paused");
  $("p-equity").textContent = money(b.equity, 2);
  $("p-pnl").innerHTML = `<span class="${cls(b.pnl)}">${pct(b.pnl_pct)}</span>`;
  $("p-day").innerHTML = `<span class="${cls(b.day_change)}">${signedMoney(b.day_change)}</span>`;
  $("p-cash").textContent = money(b.cash);
  drawSpark(b);
  document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", String(x.dataset.tab === tab)));
  const body = $("tab-body");
  body.innerHTML = { holdings: holdingsHTML, trades: tradesHTML, picks: picksHTML, settings: settingsHTML }[tab](b);
  if (tab === "settings") wireSettings(body, b);
}

function drawSpark(b) {
  const svg = $("p-spark");
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

function holdingsHTML(b) {
  if (!b.positions.length) return `<p class="empty">No holdings right now. Cash: ${money(b.cash, 2)}.</p>${settlingHTML(b)}`;
  const rows = b.positions
    .map(
      (p) => `<tr><td class="tk">${esc(p.ticker)}<span class="sleeve ${p.sleeve}">${SLEEVE_TAG[p.sleeve] || "MOM"}</span></td>
      <td>${p.shares}</td><td>${money(p.price, 2)}</td><td>${money(p.value)}</td><td class="${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</td></tr>`,
    )
    .join("");
  return `<table><thead><tr><th>Stock</th><th>Shares</th><th>Price</th><th>Value</th><th>Gain</th></tr></thead>
    <tbody>${rows}<tr><td class="tk">Cash</td><td></td><td></td><td>${money(b.cash)}</td><td></td></tr></tbody></table>
    ${settlingHTML(b)}
    ${b.notes.length ? `<div class="section-title">Bot notes</div><ul class="notes">${b.notes.slice(0, 6).map((n) => `<li><span class="num">${esc(n.date)}</span> ${esc(n.text)}</li>`).join("")}</ul>` : ""}`;
}

function settlingHTML(b) {
  if (!b.settling) return "";
  return `<p class="blocked">${money(b.settling, 2)} of the cash is from today's sales and can buy again once it settles next trading day. Spendable now: ${money(Math.max(0, b.cash - b.settling), 2)}.</p>`;
}

function tradesHTML(b) {
  if (!b.trades.length) return `<p class="empty">No trades yet.</p>`;
  return b.trades
    .slice(0, 60)
    .map(
      (t) => `<div class="trade"><span class="side ${t.side}">${t.side.toUpperCase()}</span>
      <span class="what">${t.shares} ${esc(t.ticker)} @ ${money(t.price, 2)}<span class="sleeve ${t.sleeve}">${SLEEVE_TAG[t.sleeve] || "MOM"}</span></span>
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
      <div class="field"><label for="s-entry">Buy when up at least (%)</label><input id="s-entry" type="number" min="0.05" max="5" step="0.05" value="${num1(day.entry_pct * 100)}"></div>
      <div class="field"><label for="s-lookmin">…over the last (minutes)</label><input id="s-lookmin" type="number" min="2" max="120" step="1" value="${day.lookback_minutes}"></div>
    </div>
    <div class="two">
      <div class="field"><label for="s-tp">Take profit at (%)</label><input id="s-tp" type="number" min="0.1" max="20" step="0.1" value="${num1(day.take_profit_pct * 100)}"></div>
      <div class="field"><label for="s-dstop">Stop at (%)</label><input id="s-dstop" type="number" min="0.1" max="20" step="0.1" value="${num1(day.stop_pct * 100)}"></div>
    </div>
    <div class="two">
      <div class="field"><label for="s-maxpos">Day trades open at once</label><input id="s-maxpos" type="number" min="1" max="10" value="${day.max_positions}"></div>
      <div class="field"><label for="s-cool">Wait before re-buying (minutes)</label><input id="s-cool" type="number" min="0" max="390" step="5" value="${day.cooldown_minutes}"></div>
    </div>
    <span class="help">Buys a stock that is up this much over the last few minutes and above VWAP. Sells at the take profit, at the stop, when the run fades, and always by ${esc(day.close_out_at)} so nothing is held overnight. No new buys after ${esc(day.no_entries_after)}.</span>`;
  const swingFields = `
    <div class="two">
      <div class="field"><label for="s-topn">Momentum stocks held</label><input id="s-topn" type="number" min="1" max="10" value="${d.momentum.top_n}"></div>
      <div class="field"><label for="s-look">Momentum looks back</label><select id="s-look">${opt(LOOKBACKS, d.momentum.lookback_days)}</select></div>
    </div>`;
  return `<form class="settings" id="settings-form">
    <div class="toggle"><input type="checkbox" id="s-enabled" ${d.enabled ? "checked" : ""}><label for="s-enabled">Trading on (untick to pause this building)</label></div>
    <div class="field"><label for="s-cash">Money in this building ($)</label>
      <input id="s-cash" type="number" min="0" step="100" value="${d.starting_cash}">
      <span class="help">Raising it adds cash on the next check; lowering it takes cash out (only uninvested cash).</span></div>
    <div class="field"><label for="s-style">Trading style</label><select id="s-style">${opt(STYLES, d.style)}</select></div>
    <div class="field"><span class="flabel">Strategy split</span>
      <div class="split"><span class="m" style="width:${100 - aiPct}%"></span><span class="a" style="width:${aiPct}%"></span></div>
      <div class="split-legend">${legendHTML(d, aiPct)}</div>
      <input id="s-ai" type="range" min="0" max="100" step="5" value="${aiPct}" aria-label="AI share"></div>
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
      <div class="field"><label for="s-stop">Stop loss (%)</label><input id="s-stop" type="number" min="1" max="90" step="1" value="${Math.round(d.momentum.stop_loss_pct * 100)}"></div>
      <div class="field"><label for="s-trail">Trailing stop (%)</label><input id="s-trail" type="number" min="1" max="90" step="1" value="${Math.round(d.momentum.trailing_stop_pct * 100)}"></div>
    </div>
    <div class="field"><label for="s-hold">Hold at least (minutes)</label><input id="s-hold" type="number" min="0" max="10080" step="15" value="${d.min_hold_minutes}">
      <span class="help">${isDay ? "These three apply to the AI picks." : "These apply to every holding."} Stop loss sells when a stock falls this far below what the bot paid; trailing stop sells a winner that falls this far from its high. Each AI review is one Claude request, roughly 3–5¢.</span></div>
    <div class="field"><span class="flabel">Stocks this building can trade</span>
      <div class="chips" id="s-chips">${d.universe.map((t) => `<span class="chip">${esc(t)}<button type="button" data-rm="${esc(t)}" aria-label="Remove ${esc(t)}">✕</button></span>`).join("")}</div>
      <div class="add-row"><input id="s-add" type="text" placeholder="Add ticker, e.g. IBM" maxlength="8" autocomplete="off"><button type="button" id="s-add-btn">Add</button></div>
      <span class="err" id="s-err"></span></div>
    <div class="actions"><button type="submit" class="primary" id="s-save">${MODE === "server" ? (api.unlocked() ? "Save changes" : "Unlock to save") : gh.connected() ? "Save to GitHub" : "Save changes"}</button><button type="button" id="s-reset">Undo changes</button></div>
    <div id="s-out"></div>
  </form>`;
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
    draft.momentum.stop_loss_pct = clamp(num("#s-stop"), 1, 90) / 100;
    draft.momentum.trailing_stop_pct = clamp(num("#s-trail"), 1, 90) / 100;
    if (has("#s-topn")) {
      draft.momentum.top_n = clamp(Math.round(num("#s-topn")), 1, 10);
      draft.momentum.lookback_days = num("#s-look");
      draft.momentum.short_lookback_days = Math.round(num("#s-look") / 2);
    }
    if (has("#s-entry")) {
      const day = draft.intraday;
      day.entry_pct = clamp(num("#s-entry"), 0.05, 5) / 100;
      day.lookback_minutes = clamp(Math.round(num("#s-lookmin")), 2, 120);
      day.take_profit_pct = clamp(num("#s-tp"), 0.1, 20) / 100;
      day.stop_pct = clamp(num("#s-dstop"), 0.1, 20) / 100;
      day.max_positions = clamp(Math.round(num("#s-maxpos")), 1, 10);
      day.cooldown_minutes = clamp(Math.round(num("#s-cool")), 0, 390);
    }
  };
  root.querySelector("#s-style").addEventListener("change", () => {
    sync();
    renderPanel();
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
      renderPanel();
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
    renderPanel();
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
    renderPanel();
  });
  root.querySelector("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    sync();
    const out = root.querySelector("#s-out");
    if (MODE === "server") {
      if (!api.unlocked()) return openUnlock();
      const btn = root.querySelector("#s-save");
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        const saved = await api.call(`api/bots/${encodeURIComponent(draft.id)}`, { method: "PUT", body: JSON.stringify(draft) });
        b.settings = saved;
        b.enabled = saved.enabled;
        draft = structuredClone(saved);
        renderHUD();
        renderPanel();
        toast(`Saved. ${saved.name} uses the new settings from its next check.`);
      } catch (err) {
        out.innerHTML = `<p class="err">Couldn't save: ${esc(err.message)}</p>`;
        btn.disabled = false;
        btn.textContent = "Save changes";
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
    try {
      const { sha, json } = await gh.readConfig();
      const i = json.bots.findIndex((x) => x.id === draft.id);
      if (i < 0) throw new Error(`No building "${draft.id}" in config/bots.json`);
      json.bots[i] = draft;
      await gh.writeConfig(json, sha, `Update ${draft.name} settings from Stock City`);
      b.settings = structuredClone(draft);
      b.enabled = draft.enabled;
      renderHUD();
      renderPanel();
      toast(`Saved. ${draft.name} uses the new settings on its next run.`);
    } catch (err) {
      out.innerHTML = `<p class="err">Couldn't save: ${esc(err.message)}</p>`;
      btn.disabled = false;
      btn.textContent = "Save to GitHub";
    }
  });
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
  if (openId) renderPanel();
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
  if (openId) renderPanel();
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
    if (openId) renderPanel();
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
    if (openId && tab !== "settings") renderPanel();
  } catch {}
}
