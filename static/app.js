import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const canvas = document.getElementById("canvas");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x333333);
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
const controls = new OrbitControls(camera, canvas);

let mesh = null;
let fullMat = null, mat565 = null, mode565 = false, wire = false;
let fullTex = null, quantTex = null;
let reapplyToken = 0;
let loadToken = 0;
let flipV = false;      // per-model vertical orientation override (persisted)
let currentPath = null; // path of the model currently loaded
let editSkin = null;    // working skin PNG (repo-relative) for the loaded model
let editDir = null;     // working dir the skin was extracted to
let watchSource = null; // per-model file watcher (recreated on each load)

function skinUrl(path) {
  return "/api/skin?path=" + encodeURIComponent(path) + "&index=0";
}

// Quantizes an ImageData buffer in place to RGB565 precision, matching
// mdl_tool's enc565/dec565 round-trip (truncate to N bits, expand with floor).
function quantize565InPlace(data) {
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2];
    data[i] = Math.floor((r >> 3) * 255 / 31);       // R: 5 bits
    data[i + 1] = Math.floor((g >> 2) * 255 / 63);   // G: 6 bits
    data[i + 2] = Math.floor((b >> 3) * 255 / 31);   // B: 5 bits
    // alpha (data[i+3]) left as-is
  }
}

// Builds the RGB565-quantized CanvasTexture asynchronously (image load is
// async). Calls onReady(tex) once decoded; never throws synchronously.
function buildQuantizedTextureFromUrl(url, onReady) {
  const img = new Image();
  img.onload = () => {
    const canvas2d = document.createElement("canvas");
    canvas2d.width = img.naturalWidth;
    canvas2d.height = img.naturalHeight;
    const ctx = canvas2d.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const imgData = ctx.getImageData(0, 0, canvas2d.width, canvas2d.height);
    quantize565InPlace(imgData.data);
    ctx.putImageData(imgData, 0, 0);

    const tex = new THREE.CanvasTexture(canvas2d);
    tex.colorSpace = THREE.SRGBColorSpace;
    onReady(tex);
  };
  img.onerror = () => console.warn("565 quantize: failed to load image", url);
  img.src = url;
}

function applyWire() {
  const prev = scene.getObjectByName("uvwire");
  if (prev) {
    scene.remove(prev);
    prev.material.dispose();
  }
  if (wire && mesh) {
    const wm = new THREE.Mesh(
      mesh.geometry,
      new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true })
    );
    wm.name = "uvwire";
    scene.add(wm);
  }
}

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

// Default camera azimuth, in degrees measured from world +Z toward +X. These
// models face world +X (Quake forward), and the camera used to sit on +Z, so
// every model loaded in profile ("facing right"). ~110 turns the camera onto
// the model's front and a bit past it for a flattering 3/4 view. The user can
// still orbit freely afterward.
const DEFAULT_AZIMUTH_DEG = 110;

function frameCamera(geometry) {
  geometry.computeBoundingSphere();
  const s = geometry.boundingSphere;
  controls.target.copy(s.center);
  const az = (DEFAULT_AZIMUTH_DEG * Math.PI) / 180;
  const dir = new THREE.Vector3(Math.sin(az), 0, Math.cos(az));
  camera.position.copy(s.center).addScaledVector(dir, s.radius * 2.5);
  camera.near = s.radius / 100;
  camera.far = s.radius * 100;
  camera.updateProjectionMatrix();
  controls.update();
}

function showMsg(text) {
  const el = document.getElementById("msg");
  el.textContent = text;
  el.style.display = text ? "block" : "none";
}

async function load(path) {
  const myLoad = ++loadToken;
  const myReapply = reapplyToken;
  const resp = await fetch("/api/model?path=" + encodeURIComponent(path));
  const g = await resp.json();
  if (!resp.ok || !g.positions) {
    // Model couldn't be decoded (unsupported format, or a degenerate model).
    // Leave the current view in place and tell the user why.
    showMsg("Can't open this model: " + (g.error || ("HTTP " + resp.status)));
    return;
  }
  showMsg("");

  // Apply the persisted per-model vertical override, if any. The decoder's
  // default orientation is correct for most models; a stored flipV re-flips
  // the exceptions (flat props like the newspaper) the user corrected.
  let orient = { flipV: false };
  try {
    orient = await (await fetch("/api/orientation?path=" + encodeURIComponent(path))).json();
  } catch (e) {
    console.warn("orientation fetch failed", e);
  }
  flipV = !!orient.flipV;
  if (flipV) {
    for (let i = 1; i < g.uvs.length; i += 2) g.uvs[i] = 1 - g.uvs[i];
  }
  document.getElementById("flipv").textContent = "Flip V: " + (flipV ? "ON" : "OFF");
  currentPath = path;

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(g.positions, 3));
  geo.setAttribute("uv", new THREE.Float32BufferAttribute(g.uvs, 2));
  geo.rotateX(-Math.PI / 2);

  const tex = new THREE.TextureLoader().load(skinUrl(path));
  tex.flipY = true;
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide });

  // Built with map: null so toggling to 565 before quantization finishes
  // can't crash; buildQuantizedTexture() fills in .map asynchronously below.
  const q = new THREE.MeshBasicMaterial({ map: null, side: THREE.DoubleSide });

  if (mesh) {
    scene.remove(mesh);
    const prevWire = scene.getObjectByName("uvwire");
    if (prevWire) {
      scene.remove(prevWire);
      if (prevWire.material) prevWire.material.dispose();
    }
    if (mesh.geometry) mesh.geometry.dispose();
    if (fullTex) fullTex.dispose();
    if (quantTex) quantTex.dispose();
    if (fullMat) fullMat.dispose();
    if (mat565) mat565.dispose();
  }
  mesh = new THREE.Mesh(geo, mat);
  scene.add(mesh);
  frameCamera(geo);

  fullTex = tex;
  quantTex = null;
  fullMat = mat;
  mat565 = q;
  mesh.material = mode565 ? mat565 : fullMat;
  applyWire();

  document.getElementById("tex").src = skinUrl(path);

  buildQuantizedTextureFromUrl(skinUrl(path), (tex565) => {
    // Stale guard: discard this result instead of writing into a disposed
    // or unrelated material if any of the following happened while this
    // load's quantize was in flight:
    //  - a newer load() call has superseded this one (loadToken advanced), or
    //  - a hot-reload (reapplySkinFromPng) completed and installed fresher
    //    textures (reapplyToken advanced), or
    //  - mat565 was reassigned out from under this load.
    if (myLoad !== loadToken || reapplyToken !== myReapply || mat565 !== q) {
      tex565.dispose();
      return;
    }
    quantTex = tex565;
    mat565.map = tex565;
    mat565.map.needsUpdate = true;
    mat565.needsUpdate = true;
  });

  // Extract this model's skin so it can be edited, and point the left pane and
  // the file watcher at that working PNG. Editing it (externally or, later, in
  // the canvas) hot-reloads onto the model.
  try {
    const ex = await (await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    })).json();
    if (myLoad !== loadToken) return; // superseded by a newer load
    if (ex.skin) {
      editSkin = ex.skin;
      editDir = ex.dir;
      document.getElementById("tex").src =
        "/api/pngskin?file=" + encodeURIComponent(editSkin) + "&_=" + Date.now();
      subscribeWatch(editSkin);
      showEditPath(editDir);
    } else {
      editSkin = editDir = null;
      showEditPath(null);
      console.warn("extract failed", ex.error);
    }
  } catch (e) {
    editSkin = editDir = null;
    showEditPath(null);
    console.warn("extract failed", e);
  }
}

// Regenerates BOTH the full-res and 565-quantized textures from the watched
// skin PNG (hot-reload). Unlike a single-texture update, this keeps the 565
// preview in sync with what's on disk even if it's the active mode.
function reapplySkinFromPng() {
  // The watch stream emits "changed" immediately on connect (mtime vs. an
  // initial baseline of 0.0), which races the page's own initial load(). If
  // materials aren't built yet, skip this cycle; a real edit will re-fire.
  if (!fullMat || !mat565 || !editSkin) return;

  const token = ++reapplyToken;
  const mat565Ref = mat565;
  const url = "/api/pngskin?file=" + encodeURIComponent(editSkin) + "&_=" + Date.now();

  const nf = new THREE.TextureLoader().load(url);
  nf.flipY = true;
  nf.colorSpace = THREE.SRGBColorSpace;
  if (fullTex) fullTex.dispose();
  fullTex = nf;
  fullMat.map = nf;
  fullMat.map.needsUpdate = true;

  buildQuantizedTextureFromUrl(url, (tex565) => {
    // Stale guard: discard if a fresh load() (new mat565 instance) or a
    // later reapply has superseded this one.
    if (mat565 !== mat565Ref || token !== reapplyToken) {
      tex565.dispose();
      return;
    }
    if (quantTex) quantTex.dispose();
    quantTex = tex565;
    mat565.map = tex565;
    mat565.map.needsUpdate = true;
    mat565.needsUpdate = true;
  });

  document.getElementById("tex").src = url;
}

// (Re)subscribe the file watcher to the loaded model's working skin. External
// edits to that PNG fire "changed", which re-textures the model live.
function subscribeWatch(file) {
  if (watchSource) watchSource.close();
  watchSource = new EventSource("/api/watch?file=" + encodeURIComponent(file));
  watchSource.onmessage = (e) => { if (e.data === "changed") reapplySkinFromPng(); };
}

function showEditPath(dir) {
  const el = document.getElementById("editpath");
  if (el) el.textContent = dir ? "editing: " + dir : "";
}

document.getElementById("load").onclick = () =>
  load(document.getElementById("path").value);
document.getElementById("tex").src = skinUrl(document.getElementById("path").value);

// Browse: ask the backend to open a native file dialog, then load the picked
// absolute path (the field also accepts a hand-typed absolute path).
document.getElementById("browse").onclick = async () => {
  try {
    const r = await (await fetch("/api/pick")).json();
    if (r.cancelled) return;
    if (r.error) { console.warn(r.error); return; }
    if (r.path) {
      document.getElementById("path").value = r.path;
      load(r.path);
    }
  } catch (err) {
    console.warn("file picker failed", err);
  }
};

document.getElementById("mode565").onclick = (e) => {
  mode565 = !mode565;
  if (mesh) mesh.material = mode565 ? mat565 : fullMat;
  e.target.textContent = "565 preview: " + (mode565 ? "ON" : "OFF");
};
document.getElementById("wire").onclick = (e) => {
  wire = !wire;
  applyWire();
  e.target.textContent = "Wireframe: " + (wire ? "ON" : "OFF");
};

// Flip V: invert the model's texture V instantly and persist the choice for
// this path. Corrects flat props (newspaper, vases) that don't follow the
// decoder's default vertical orientation. The wireframe shares this geometry,
// so it updates for free.
document.getElementById("flipv").onclick = async (e) => {
  if (!mesh || !currentPath) return;
  flipV = !flipV;
  const uv = mesh.geometry.getAttribute("uv");
  for (let i = 0; i < uv.count; i++) uv.setY(i, 1 - uv.getY(i));
  uv.needsUpdate = true;
  e.target.textContent = "Flip V: " + (flipV ? "ON" : "OFF");
  try {
    await fetch("/api/orientation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, flipV }),
    });
  } catch (err) {
    console.warn("persist orientation failed", err);
  }
};

// Save: re-embed the edited working skin into the .MDL (backup taken at load).
document.getElementById("save").onclick = async (e) => {
  if (!currentPath) return;
  const btn = e.target;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const r = await (await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath }),
    })).json();
    if (r.ok) {
      showMsg("");
      btn.textContent = "Saved ✓";
      setTimeout(() => { btn.textContent = label; }, 1500);
    } else {
      showMsg("Save failed: " + (r.error || "unknown error"));
      btn.textContent = label;
    }
  } catch (err) {
    showMsg("Save failed: " + err);
    btn.textContent = label;
  }
  btn.disabled = false;
};

// Reveal: open the working-skin folder so it can be edited externally (macOS).
document.getElementById("reveal").onclick = async () => {
  if (!currentPath) return;
  try {
    await fetch("/api/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath }),
    });
  } catch (err) {
    console.warn("reveal failed", err);
  }
};

resize();
load(document.getElementById("path").value);
renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
