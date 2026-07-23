import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createMeshEditor } from "./meshedit.js";

const canvas = document.getElementById("canvas");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x333333);
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
window.__camera = camera; // debug/test handle, like window.__model
const controls = new OrbitControls(camera, canvas);

// Vertex editing in Wireframe mode (issue #22). Callbacks reference functions
// declared below (hoisted); they only run on user gestures after load.
const meshEditor = createMeshEditor({
  scene, camera, canvas, controls,
  onDragStart: () => {
    if (animPlaying) {
      animPlaying = false;
      updateAnimUi();
    }
  },
  onCommit: (entry) => pushHistory({ kind: "vertex", ...entry }),
  persist: () => persistVertices(),
});

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
let editSkin = null;    // working skin PNG (repo-relative) currently being edited
let editDir = null;     // working dir the skin was extracted to
let skinFiles = [];     // working skin files (skin0.png, skin1.png, ...)
let currentSkinIndex = 0;
let watchSource = null; // per-model file watcher (recreated on each load)
let suppressWatchUntil = 0; // ignore our own skin-write echoes until this time
let animFrames = [];    // non-indexed position arrays (one per frame)
let animPlaying = false;
let animFps = 8;
let animFrame = 0;
let lastAnimMs = 0;
let paperImageData = null;
let paperImageName = "";

// paint state
let brushColor = document.getElementById("color").value;
let brushSize = +document.getElementById("brushsize").value;
let eraserColor = document.getElementById("erasecolor").value;
const TOOLS = ["brush", "eraser", "fill", "pick"];
let currentTool = "brush";
let prevTool = "brush";  // tool to restore after a one-shot eyedropper pick
let drawing = false, lastX = 0, lastY = 0;
const undoStack = [], redoStack = [];
const MAX_HISTORY = 30;

function skinUrl(path, index) {
  return "/api/skin?path=" + encodeURIComponent(path) + "&index=" + encodeURIComponent(index);
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
// both textures. Resets undo history, since it's a fresh image. `isCurrent`, if
// given, is re-checked when the image finishes decoding: image loads are async,
// so a superseded load whose request was already in flight must not clobber the
// current model's canvas when its onload finally fires.
function loadSkinIntoCanvas(url, done, isCurrent) {
  const img = new Image();
  img.onload = () => {
    if (isCurrent && !isCurrent()) return;
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
// One unified history: paint entries carry the pre-edit canvas snapshot,
// vertex entries carry the vertex's delta before/after the drag. A single
// Ctrl+Z therefore undoes whatever the user did last, regardless of kind.
function pushUndo() {
  pushHistory({ kind: "paint", img: snapshot() });
}
function pushHistory(entry) {
  undoStack.push(entry);
  if (undoStack.length > MAX_HISTORY) undoStack.shift();
  redoStack.length = 0;
  updateHistoryButtons();
}
function undo() {
  if (!undoStack.length) return;
  const entry = undoStack.pop();
  if (entry.kind === "paint") {
    redoStack.push({ kind: "paint", img: snapshot() });
    pctx.putImageData(entry.img, 0, 0);
    afterEdit();
  } else {
    redoStack.push(entry);
    applyVertexHistory(entry.vi, entry.prev);
  }
  updateHistoryButtons();
}
function redo() {
  if (!redoStack.length) return;
  const entry = redoStack.pop();
  if (entry.kind === "paint") {
    undoStack.push({ kind: "paint", img: snapshot() });
    pctx.putImageData(entry.img, 0, 0);
    afterEdit();
  } else {
    undoStack.push(entry);
    applyVertexHistory(entry.vi, entry.next);
  }
  updateHistoryButtons();
}
function applyVertexHistory(vi, delta) {
  meshEditor.setDelta(vi, delta);
  persistVertices();
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

// Persist the cumulative vertex-delta map to the working dir; Save applies it
// to the .MDL during the rebuild (backup + deltas), like the working skins.
function persistVertices() {
  if (!currentPath) return;
  fetch("/api/vertices-write", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: currentPath, deltas: meshEditor.getDeltasObject() }),
  }).catch((e) => console.warn("vertices-write failed", e));
}

function activeColor() {
  return currentTool === "eraser" ? eraserColor : brushColor;
}
function setTool(tool) {
  if (tool === "pick" && currentTool !== "pick") prevTool = currentTool;
  currentTool = tool;
  for (const t of TOOLS) {
    document.getElementById("tool-" + t)
      .setAttribute("aria-pressed", String(t === tool));
  }
}

function hexToRgb(hex) {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

// Iterative 4-connected flood fill of the exact-color region under (x, y) with
// the primary brush color. Recursion would blow the JS stack on large skins, so
// this uses an explicit coordinate stack. Snapshots for undo only when a change
// will actually happen. Returns true if any pixel changed.
function floodFill(x, y) {
  const w = paint.width, h = paint.height;
  if (x < 0 || y < 0 || x >= w || y >= h) return false;
  const img = pctx.getImageData(0, 0, w, h);
  const data = img.data;
  const idx = (px, py) => (py * w + px) * 4;
  const s = idx(x, y);
  const tr = data[s], tg = data[s + 1], tb = data[s + 2], ta = data[s + 3];
  const [fr, fg, fb] = hexToRgb(brushColor);
  const fa = 255;
  if (tr === fr && tg === fg && tb === fb && ta === fa) return false; // no-op guard
  pushUndo();
  const stack = [x, y]; // flat pairs to limit allocations
  while (stack.length) {
    const cy = stack.pop(), cx = stack.pop();
    if (cx < 0 || cy < 0 || cx >= w || cy >= h) continue;
    const i = idx(cx, cy);
    if (data[i] !== tr || data[i + 1] !== tg || data[i + 2] !== tb || data[i + 3] !== ta) continue;
    data[i] = fr; data[i + 1] = fg; data[i + 2] = fb; data[i + 3] = fa;
    stack.push(cx + 1, cy, cx - 1, cy, cx, cy + 1, cx, cy - 1);
  }
  pctx.putImageData(img, 0, 0);
  return true;
}

function pickColor(x, y) {
  const w = paint.width, h = paint.height;
  if (x < 0 || y < 0 || x >= w || y >= h) return;
  const d = pctx.getImageData(x, y, 1, 1).data;
  const hex = "#" + [d[0], d[1], d[2]]
    .map((v) => v.toString(16).padStart(2, "0")).join("");
  brushColor = hex;
  document.getElementById("color").value = hex;
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
  if (currentTool === "pick") {
    const [ex, ey] = canvasXY(e);
    pickColor(Math.floor(ex), Math.floor(ey));
    setTool(prevTool);
    return;
  }
  if (currentTool === "fill") {
    const [fx, fy] = canvasXY(e);
    if (floodFill(Math.floor(fx), Math.floor(fy))) afterEdit();
    return;
  }
  drawing = true;
  paint.setPointerCapture(e.pointerId);
  pushUndo();
  [lastX, lastY] = canvasXY(e);
  pctx.fillStyle = activeColor();
  pctx.beginPath();
  pctx.arc(lastX, lastY, brushSize / 2, 0, Math.PI * 2);
  pctx.fill();
  paintTex.needsUpdate = true;
});
paint.addEventListener("pointermove", (e) => {
  if (!drawing) return;
  const [x, y] = canvasXY(e);
  pctx.strokeStyle = activeColor();
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
document.getElementById("erasecolor").addEventListener("input", (e) => { eraserColor = e.target.value; });
for (const t of TOOLS) {
  document.getElementById("tool-" + t).addEventListener("click", () => setTool(t));
}
document.getElementById("brushsize").addEventListener("input", (e) => {
  brushSize = +e.target.value;
  document.getElementById("brushval").textContent = brushSize;
});
document.getElementById("undo").addEventListener("click", undo);
document.getElementById("redo").addEventListener("click", redo);
window.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (k === "y" || (k === "z" && e.shiftKey)) { e.preventDefault(); redo(); }
    return;
  }
  if (e.altKey) return;
  if (!editSkin) return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
  if (drawing) return;
  const map = { b: "brush", e: "eraser", g: "fill", i: "pick" };
  const tool = map[e.key.toLowerCase()];
  if (tool) { e.preventDefault(); setTool(tool); }
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
  meshEditor.setWire(wire);
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
  if (!el) return;
  // The element is direction:rtl so it ellipsizes from the left, keeping the
  // filename (end of the path) visible; the "editing:" prefix anchors the
  // string LTR so the path still renders in order. Full path in the tooltip.
  el.textContent = dir ? "editing: " + dir : "";
  el.title = dir || "";
}

function updateSkinUi() {
  const sel = document.getElementById("skinselect");
  const addBtn = document.getElementById("skinadd");
  const removeBtn = document.getElementById("skinremove");
  sel.innerHTML = "";
  for (let i = 0; i < skinFiles.length; i++) {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = "Skin " + i;
    sel.appendChild(opt);
  }
  sel.disabled = skinFiles.length <= 1;
  addBtn.disabled = !currentPath || skinFiles.length === 0;
  removeBtn.disabled = !currentPath || skinFiles.length <= 1;
  if (skinFiles.length) {
    currentSkinIndex = Math.max(0, Math.min(currentSkinIndex, skinFiles.length - 1));
    sel.value = String(currentSkinIndex);
  }
}

function loadActiveSkinFromDisk() {
  if (!editSkin) return;
  loadSkinIntoCanvas("/api/pngskin?file=" + encodeURIComponent(editSkin) + "&_=" + Date.now());
}

function setActiveSkin(index, fromDisk = true) {
  if (!skinFiles.length) {
    editSkin = null;
    currentSkinIndex = 0;
    updateSkinUi();
    return;
  }
  currentSkinIndex = Math.max(0, Math.min(index, skinFiles.length - 1));
  editSkin = skinFiles[currentSkinIndex];
  subscribeWatch(editSkin);
  updateSkinUi();
  if (fromDisk) loadActiveSkinFromDisk();
}

function updateAnimUi() {
  const slider = document.getElementById("animframe");
  const label = document.getElementById("animlabel");
  const play = document.getElementById("animplay");
  const max = Math.max(0, animFrames.length - 1);
  slider.max = String(max);
  slider.value = String(Math.min(animFrame, max));
  label.textContent = "frame " + Math.min(animFrame, max) + "/" + max;
  play.disabled = animFrames.length <= 1;
  play.setAttribute("aria-pressed", String(animPlaying));
}

function applyAnimFrame(frameIdx) {
  if (!mesh || !animFrames.length) return;
  const idx = Math.max(0, Math.min(frameIdx, animFrames.length - 1));
  const arr = animFrames[idx];
  const pos = mesh.geometry.getAttribute("position");
  // Frame positions come from the server in the model's native Z-up space. The
  // initial geometry is baked into Three.js's Y-up world via geo.rotateX(-90),
  // so each frame must get the SAME rotation here — otherwise writing the raw
  // Z-up positions reverts the mesh to lying on its side. rotateX(-90) maps
  // (x, y, z) -> (x, z, -y).
  const out = pos.array;
  for (let i = 0, c = 0; i < arr.length; i += 3, c++) {
    // Frames stay pristine in memory; vertex edits (same delta every frame,
    // per the issue #22 decision) are added here, before the bake.
    const d = meshEditor.deltaForCorner(c);
    const x = d ? arr[i] + d[0] : arr[i];
    const y = d ? arr[i + 1] + d[1] : arr[i + 1];
    const z = d ? arr[i + 2] + d[2] : arr[i + 2];
    out[i] = x;
    out[i + 1] = z;
    out[i + 2] = -y;
  }
  pos.needsUpdate = true;
  mesh.geometry.computeBoundingSphere();
  animFrame = idx;
  meshEditor.refreshHandles(); // handle positions are per-frame (pristine + delta)
  updateAnimUi();
}

async function load(path) {
  const myLoad = ++loadToken;
  disarmReset();
  const resp = await fetch("/api/model?path=" + encodeURIComponent(path) + "&includeFrames=1");
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
  // A newer load() started while we awaited: bail before touching any shared
  // state (flipV, currentPath, the mesh, the paint canvas). Otherwise a
  // superseded load's skin image finishes last and clobbers the current
  // model's canvas and currentPath.
  if (myLoad !== loadToken) return;
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
  mesh.name = "model";
  mesh.userData.vertexIndices = g.vertexIndices || [];
  window.__model = mesh; // debug/test handle for inspecting the loaded model
  scene.add(mesh);
  frameCamera(geo);
  fullMat = mat;
  mat565 = q;
  mesh.material = mode565 ? mat565 : fullMat;
  meshEditor.setModel({
    mesh,
    vertexIndices: g.vertexIndices || [],
    quant: g.quant,
    editable: !!g.quant && !g.framesGrouped,
    getFrame: () => animFrames[animFrame],
  });
  applyWire();
  animFrames = Array.isArray(g.frames) && g.frames.length ? g.frames : [g.positions];
  animPlaying = false;
  animFrame = 0;
  lastAnimMs = performance.now();
  applyAnimFrame(0);
  updateAnimUi();

  // Show skin0 immediately while the working dir extraction request completes
  // (the decoded skin is identical to the working PNG produced by extract
  // below). Guard the async image load so a superseded load can't clobber the
  // canvas of a newer one when its onload finally fires.
  loadSkinIntoCanvas(skinUrl(path, 0), undefined, () => myLoad === loadToken);

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
      skinFiles = Array.isArray(ex.skins) && ex.skins.length ? ex.skins : [ex.skin];
      editDir = ex.dir;
      setActiveSkin(0, true);
      showEditPath(editDir);
      // Working dir is ready: seed saved-but-unapplied mesh edits and arm
      // editing. applyAnimFrame re-applies them to the displayed mesh.
      meshEditor.setDeltas(ex.vertexDeltas || {});
      applyAnimFrame(animFrame);
    } else {
      editSkin = editDir = null;
      skinFiles = [];
      currentSkinIndex = 0;
      updateSkinUi();
      showEditPath(null);
      console.warn("extract failed", ex.error);
    }
    updateResetButton();
  } catch (e) {
    if (myLoad !== loadToken) return;
    editSkin = editDir = null;
    skinFiles = [];
    currentSkinIndex = 0;
    updateSkinUi();
    showEditPath(null);
    updateResetButton();
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
  if (wire && mesh && !meshEditor.isEditable()) {
    showMsg("Mesh editing is unavailable for this model (non-simple frame layout).");
  }
};

document.getElementById("animplay").onclick = (e) => {
  animPlaying = !animPlaying;
  lastAnimMs = performance.now();
  e.currentTarget.setAttribute("aria-pressed", String(animPlaying));
  updateAnimUi();
};

document.getElementById("animframe").addEventListener("input", (e) => {
  animPlaying = false;
  applyAnimFrame(+e.target.value);
});

document.getElementById("skinselect").addEventListener("change", (e) => {
  setActiveSkin(+e.target.value, true);
});

document.getElementById("skinadd").onclick = async () => {
  if (!currentPath) return;
  try {
    const r = await (await fetch("/api/skin-add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, fromIndex: currentSkinIndex }),
    })).json();
    if (!r.ok) {
      showMsg("Add skin failed: " + (r.error || "unknown error"));
      return;
    }
    skinFiles = r.skins || skinFiles;
    setActiveSkin(r.index ?? (skinFiles.length - 1), true);
    showMsg("");
  } catch (err) {
    showMsg("Add skin failed: " + err);
  }
};

document.getElementById("skinremove").onclick = async () => {
  if (!currentPath || skinFiles.length <= 1) return;
  try {
    const r = await (await fetch("/api/skin-remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, index: currentSkinIndex }),
    })).json();
    if (!r.ok) {
      showMsg("Remove skin failed: " + (r.error || "unknown error"));
      return;
    }
    skinFiles = r.skins || [];
    setActiveSkin(r.index ?? 0, true);
    showMsg("");
  } catch (err) {
    showMsg("Remove skin failed: " + err);
  }
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

document.getElementById("up2x").onclick = async (e) => {
  if (!currentPath || !editSkin) return;
  const btn = e.currentTarget;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Upscaling…";
  try {
    const r = await (await fetch("/api/upscale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, factor: 2, method: "nearest" }),
    })).json();
    if (r.ok) {
      showMsg("");
      // Force-refresh from disk so both paint canvas and model textures update.
      loadSkinIntoCanvas("/api/pngskin?file=" + encodeURIComponent(editSkin) + "&_=" + Date.now());
    } else {
      showMsg("Upscale failed: " + (r.error || "unknown error"));
    }
  } catch (err) {
    showMsg("Upscale failed: " + err);
  }
  btn.textContent = label;
  btn.disabled = false;
};

document.getElementById("paperpick").onclick = () => {
  document.getElementById("paperfile").click();
};

document.getElementById("paperfile").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  paperImageName = f.name;
  document.getElementById("paperimg").textContent = f.name;
  const rd = new FileReader();
  rd.onload = () => { paperImageData = rd.result; };
  rd.readAsDataURL(f);
});

document.getElementById("papergen").onclick = async (e) => {
  if (!paperImageData) {
    showMsg("Pick a source image first.");
    return;
  }
  const outPath = document.getElementById("paperout").value.trim();
  const heightRef = document.getElementById("paperheightsrc").value.trim();
  const animSrc = document.getElementById("paperanimsrc").value.trim();
  if (!outPath) {
    showMsg("Set an output .MDL path first.");
    return;
  }
  const btn = e.currentTarget;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Generating…";
  try {
    const r = await (await fetch("/api/paper-from-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        imageData: paperImageData,
        outPath,
        grid: 8,
        alphaThreshold: 10,
        pixelScale: 1.0,
        targetHeightModelPath: heightRef || null,
        animSourceModelPath: animSrc || null,
      }),
    })).json();
    if (r.ok) {
      const hInfo = r.heightMatchedTo ? " Height matched." : "";
      const aInfo = r.numframes > 1 ? (" Frames: " + r.numframes + ".") : "";
      showMsg("Generated " + outPath + " from " + paperImageName + "." + hInfo + aInfo + " Loading it now.");
      document.getElementById("path").value = outPath;
      load(outPath);
    } else {
      showMsg("Paper MDL generation failed: " + (r.error || "unknown error"));
    }
  } catch (err) {
    showMsg("Paper MDL generation failed: " + err);
  }
  btn.textContent = label;
  btn.disabled = false;
};

// --- reset skin ---
// Restore the pristine skin, discarding unsaved edits. Uses a two-step inline
// confirm rather than a native dialog: the first click arms the button for 3s
// and the second click within that window performs the reset.
const resetBtn = document.getElementById("reset");
let resetArmed = false, resetTimer = 0;

function updateResetButton() {
  resetBtn.disabled = !editSkin;
}

function disarmReset() {
  resetArmed = false;
  clearTimeout(resetTimer);
  resetTimer = 0;
  resetBtn.textContent = "Reset skin";
  resetBtn.classList.remove("toggle");
  resetBtn.setAttribute("aria-pressed", "false");
}

async function doReset() {
  if (!currentPath || !editSkin) return;
  disarmReset();
  const myLoad = loadToken; // bail if a different model is loaded mid-reset
  resetBtn.disabled = true;
  resetBtn.textContent = "Resetting…";
  // The server rewrites skin0.png; pre-suppress the watcher so its "changed"
  // event doesn't fire a second, redundant reload on top of ours.
  suppressWatchUntil = Date.now() + 1500;
  try {
    const r = await (await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, force: true }),
    })).json();
    if (myLoad !== loadToken) return; // superseded by a newer load
    if (r.skin) {
      // Reset re-extracts a pristine copy; reload the currently selected skin
      // (not always skin 0) so a force-reset respects the active slot. The
      // on-disk skin is already pristine, so no skin-write here.
      skinFiles = Array.isArray(r.skins) && r.skins.length ? r.skins : [r.skin];
      editDir = r.dir;
      setActiveSkin(Math.min(currentSkinIndex, skinFiles.length - 1), true);
      showMsg("");
    } else {
      showMsg("Reset failed: " + (r.error || "unknown error"));
    }
  } catch (err) {
    showMsg("Reset failed: " + err);
  }
  resetBtn.textContent = "Reset skin";
  updateResetButton();
}

resetBtn.addEventListener("click", () => {
  if (!editSkin) return;
  if (resetArmed) { doReset(); return; }
  resetArmed = true;
  resetBtn.textContent = "Confirm reset?";
  resetBtn.classList.add("toggle");
  resetBtn.setAttribute("aria-pressed", "true");
  resetTimer = setTimeout(disarmReset, 3000);
});

resize();
updateSkinUi();
load(document.getElementById("path").value);
renderer.setAnimationLoop(() => {
  const now = performance.now();
  if (animPlaying && animFrames.length > 1 && mesh) {
    const stepMs = 1000 / Math.max(1, animFps);
    if (now - lastAnimMs >= stepMs) {
      lastAnimMs = now;
      applyAnimFrame((animFrame + 1) % animFrames.length);
    }
  }
  controls.update();
  renderer.render(scene, camera);
});
