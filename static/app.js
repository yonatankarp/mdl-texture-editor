import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const canvas = document.getElementById("canvas");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x333333);
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
const controls = new OrbitControls(camera, canvas);

// Left pane is a paint surface. The 3D model is textured directly from this
// canvas (a CanvasTexture), so brush strokes appear on the model instantly;
// strokes are also persisted to the working skin PNG for Save-to-.MDL and the
// external-editor round-trip.
const paint = document.getElementById("paint");
const pctx = paint.getContext("2d", { willReadFrequently: true });
const paintTex = new THREE.CanvasTexture(paint);
paintTex.flipY = true;
paintTex.colorSpace = THREE.SRGBColorSpace;

let mesh = null;
let fullMat = null, mat565 = null, mode565 = false, wire = false;
let quantTex = null;
let loadToken = 0;
let flipV = false;      // per-model vertical orientation override (persisted)
let currentPath = null; // path of the model currently loaded
let editSkin = null;    // working skin PNG (repo-relative) for the loaded model
let editDir = null;     // working dir the skin was extracted to
let watchSource = null; // per-model file watcher (recreated on each load)
let suppressWatchUntil = 0; // ignore our own skin-write echoes until this time

// paint state
let brushColor = document.getElementById("color").value;
let brushSize = +document.getElementById("brushsize").value;
let drawing = false, lastX = 0, lastY = 0;
const undoStack = [], redoStack = [];
const MAX_HISTORY = 30;

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
  }
}

// Rebuild the RGB565-quantized texture from the current paint canvas.
function rebuild565() {
  const c = document.createElement("canvas");
  c.width = paint.width;
  c.height = paint.height;
  const cx = c.getContext("2d");
  cx.drawImage(paint, 0, 0);
  const d = cx.getImageData(0, 0, c.width, c.height);
  quantize565InPlace(d.data);
  cx.putImageData(d, 0, 0);
  const tex = new THREE.CanvasTexture(c);
  tex.flipY = true;
  tex.colorSpace = THREE.SRGBColorSpace;
  if (quantTex) quantTex.dispose();
  quantTex = tex;
  if (mat565) { mat565.map = tex; mat565.needsUpdate = true; }
}

// Draw a skin image into the paint canvas (sizing it to the image) and refresh
// both textures. Resets undo history, since it's a fresh image.
function loadSkinIntoCanvas(url, done) {
  const img = new Image();
  img.onload = () => {
    paint.width = img.naturalWidth;
    paint.height = img.naturalHeight;
    pctx.drawImage(img, 0, 0);
    // Resizing the paint canvas changes the texture's source dimensions.
    // paintTex was created from this canvas at its original (default) size, and
    // a bare needsUpdate does NOT reallocate the GL texture for the new size —
    // it keeps sampling as solid black, so the model renders black. Disposing
    // forces a full reallocation from the now-correctly-sized canvas on the
    // next render. (quantTex sidesteps this by being recreated each rebuild.)
    paintTex.dispose();
    paintTex.needsUpdate = true;
    rebuild565();
    undoStack.length = 0;
    redoStack.length = 0;
    updateHistoryButtons();
    if (done) done();
  };
  img.onerror = () => console.warn("failed to load skin image", url);
  img.src = url;
}

// --- undo / redo ---
function snapshot() {
  return pctx.getImageData(0, 0, paint.width, paint.height);
}
function updateHistoryButtons() {
  document.getElementById("undo").disabled = undoStack.length === 0;
  document.getElementById("redo").disabled = redoStack.length === 0;
}
function pushUndo() {
  undoStack.push(snapshot());
  if (undoStack.length > MAX_HISTORY) undoStack.shift();
  redoStack.length = 0;
  updateHistoryButtons();
}
function undo() {
  if (!undoStack.length) return;
  redoStack.push(snapshot());
  pctx.putImageData(undoStack.pop(), 0, 0);
  afterEdit();
  updateHistoryButtons();
}
function redo() {
  if (!redoStack.length) return;
  undoStack.push(snapshot());
  pctx.putImageData(redoStack.pop(), 0, 0);
  afterEdit();
  updateHistoryButtons();
}
// Refresh the model textures and persist after a committed change.
function afterEdit() {
  paintTex.needsUpdate = true;
  rebuild565();
  persistSkin();
}

// Write the canvas to the working skin PNG. The file watcher would echo this
// back as a "changed" event; suppress that briefly so a self-write doesn't
// reload the canvas and wipe undo history.
function persistSkin() {
  if (!editSkin) return;
  suppressWatchUntil = Date.now() + 1500;
  fetch("/api/skin-write", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: editSkin, png: paint.toDataURL("image/png") }),
  }).catch((e) => console.warn("skin-write failed", e));
}

// --- painting ---
function canvasXY(e) {
  const r = paint.getBoundingClientRect();
  return [
    (e.clientX - r.left) / r.width * paint.width,
    (e.clientY - r.top) / r.height * paint.height,
  ];
}
paint.addEventListener("pointerdown", (e) => {
  if (!editSkin) return; // nothing loaded to edit yet
  drawing = true;
  paint.setPointerCapture(e.pointerId);
  pushUndo();
  [lastX, lastY] = canvasXY(e);
  pctx.fillStyle = brushColor;
  pctx.beginPath();
  pctx.arc(lastX, lastY, brushSize / 2, 0, Math.PI * 2);
  pctx.fill();
  paintTex.needsUpdate = true;
});
paint.addEventListener("pointermove", (e) => {
  if (!drawing) return;
  const [x, y] = canvasXY(e);
  pctx.strokeStyle = brushColor;
  pctx.lineWidth = brushSize;
  pctx.lineCap = "round";
  pctx.lineJoin = "round";
  pctx.beginPath();
  pctx.moveTo(lastX, lastY);
  pctx.lineTo(x, y);
  pctx.stroke();
  [lastX, lastY] = [x, y];
  paintTex.needsUpdate = true; // live model update during the stroke
});
function endStroke(e) {
  if (!drawing) return;
  drawing = false;
  try { paint.releasePointerCapture(e.pointerId); } catch (_) {}
  rebuild565();   // refresh 565 preview once per stroke (cheap enough)
  persistSkin();
}
paint.addEventListener("pointerup", endStroke);
paint.addEventListener("pointercancel", endStroke);

// toolbar + keyboard
document.getElementById("color").addEventListener("input", (e) => { brushColor = e.target.value; });
document.getElementById("brushsize").addEventListener("input", (e) => {
  brushSize = +e.target.value;
  document.getElementById("brushval").textContent = brushSize;
});
document.getElementById("undo").addEventListener("click", undo);
document.getElementById("redo").addEventListener("click", redo);
window.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
  else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
});

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

// Default camera azimuth, in degrees from world +Z toward +X. These models face
// world +X (Quake forward); the camera used to sit on +Z, so models loaded in
// profile. ~110 turns the camera onto the model's front and a bit past it for a
// 3/4 view. The user can still orbit freely afterward.
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

function showEditPath(dir) {
  const el = document.getElementById("editpath");
  if (el) el.textContent = dir ? "editing: " + dir : "";
}

async function load(path) {
  const myLoad = ++loadToken;
  const resp = await fetch("/api/model?path=" + encodeURIComponent(path));
  const g = await resp.json();
  if (!resp.ok || !g.positions) {
    showMsg("Can't open this model: " + (g.error || ("HTTP " + resp.status)));
    return;
  }
  showMsg("");

  // Persisted per-model vertical override (flat props load upside-down).
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
  document.getElementById("flipv").setAttribute("aria-pressed", String(flipV));
  currentPath = path;

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(g.positions, 3));
  geo.setAttribute("uv", new THREE.Float32BufferAttribute(g.uvs, 2));
  geo.rotateX(-Math.PI / 2);

  // Both materials draw from the shared paint canvas (full-color and 565).
  const mat = new THREE.MeshBasicMaterial({ map: paintTex, side: THREE.DoubleSide });
  const q = new THREE.MeshBasicMaterial({ map: quantTex, side: THREE.DoubleSide });

  if (mesh) {
    scene.remove(mesh);
    const prevWire = scene.getObjectByName("uvwire");
    if (prevWire) {
      scene.remove(prevWire);
      if (prevWire.material) prevWire.material.dispose();
    }
    if (mesh.geometry) mesh.geometry.dispose();
    if (fullMat) fullMat.dispose();
    if (mat565) mat565.dispose();
  }
  mesh = new THREE.Mesh(geo, mat);
  scene.add(mesh);
  frameCamera(geo);
  fullMat = mat;
  mat565 = q;
  mesh.material = mode565 ? mat565 : fullMat;
  applyWire();

  // Show the current skin in the paint canvas right away (the decoded skin is
  // identical to the working PNG produced by extract below).
  loadSkinIntoCanvas(skinUrl(path));

  // Extract this model's skin so edits can be saved back, and watch it so
  // external-editor changes still hot-reload into the canvas.
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
      subscribeWatch(editSkin);
      showEditPath(editDir);
    } else {
      editSkin = editDir = null;
      showEditPath(null);
      console.warn("extract failed", ex.error);
    }
  } catch (e) {
    if (myLoad !== loadToken) return;
    editSkin = editDir = null;
    showEditPath(null);
    console.warn("extract failed", e);
  }
}

// External edits to the working skin reload into the canvas. Skips our own
// skin-write echoes so painting doesn't reset its own undo history.
function reapplySkinFromPng() {
  if (!editSkin) return;
  if (Date.now() < suppressWatchUntil) return;
  loadSkinIntoCanvas("/api/pngskin?file=" + encodeURIComponent(editSkin) + "&_=" + Date.now());
}

function subscribeWatch(file) {
  if (watchSource) watchSource.close();
  watchSource = new EventSource("/api/watch?file=" + encodeURIComponent(file));
  watchSource.onmessage = (e) => { if (e.data === "changed") reapplySkinFromPng(); };
}

document.getElementById("load").onclick = () =>
  load(document.getElementById("path").value);

// Browse: native file dialog, then load the picked absolute path.
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
  e.currentTarget.setAttribute("aria-pressed", String(mode565));
};
document.getElementById("wire").onclick = (e) => {
  wire = !wire;
  applyWire();
  e.currentTarget.setAttribute("aria-pressed", String(wire));
};

// Flip V: invert the model's texture V instantly and persist per model.
document.getElementById("flipv").onclick = async (e) => {
  if (!mesh || !currentPath) return;
  flipV = !flipV;
  const uv = mesh.geometry.getAttribute("uv");
  for (let i = 0; i < uv.count; i++) uv.setY(i, 1 - uv.getY(i));
  uv.needsUpdate = true;
  e.currentTarget.setAttribute("aria-pressed", String(flipV));
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

// Reveal: open the working-skin folder to edit externally (macOS).
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
