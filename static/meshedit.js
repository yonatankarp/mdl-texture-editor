import * as THREE from "three";
import { TransformControls } from "three/addons/controls/TransformControls.js";

// Vertex editing for Wireframe mode (issue #22), select-then-gizmo interaction.
// Click a handle to select its vertex; a TransformControls translate gizmo
// moves it axis/plane-constrained. Deltas live in DECODED model space (the
// server's parse_geometry output, pre Y-up bake) relative to the PRISTINE
// geometry, always exact multiples of the format's quantization step, so the
// on-screen mesh is exactly what Save writes.
//
// The mesh's position attribute holds baked (Y-up) coordinates: the loader
// applies rotateX(-90), i.e. baked(x,y,z) = (x, z, -y). unbake inverts that.
const bake = (v) => [v[0], v[2], -v[1]];
const unbake = (v) => [v[0], -v[2], v[1]];

const LIT = new THREE.Color(1.0, 0.8, 0.0);    // pickable handle
const DIM = new THREE.Color(0.28, 0.22, 0.06); // occluded: dimmed, unpickable
const HOVER = new THREE.Color(1.0, 1.0, 1.0);

export function createMeshEditor({
  scene, camera, canvas, controls, onDragStart, onCommit, persist, onSelectionChange,
}) {
  let model = null;      // {mesh, vertexIndices, quant, editable, getFrame}
  let ready = false;     // working dir extracted; persisted deltas seeded
  let wireOn = false;
  let uniqueVerts = [];  // MDL vertex index per handle point
  let vertToIdx = new Map();     // vi -> handle index
  let firstCorner = new Map();   // vi -> a corner index (for pristine lookup)
  let cornersByVertex = new Map(); // vi -> [corner...]
  let deltas = new Map();        // vi -> [dx, dy, dz] decoded space
  let points = null;             // THREE.Points handle cloud
  let occluded = null;           // Uint8Array per handle
  let hoverIdx = -1;
  let selectedVi = null;
  let dragStartDelta = null;
  let missDown = null;           // pointerdown on empty space (click-deselect)
  let occlusionTimer = 0;

  const raycaster = new THREE.Raycaster();
  const occRaycaster = new THREE.Raycaster();

  // Selected-vertex marker.
  const marker = new THREE.Points(
    new THREE.BufferGeometry().setAttribute(
      "position", new THREE.Float32BufferAttribute(3, 3)),
    new THREE.PointsMaterial({
      color: 0x6ea8fe, size: 12, sizeAttenuation: false, depthTest: false, transparent: true,
    }));
  marker.name = "vselect";
  marker.renderOrder = 3;
  marker.visible = false;
  scene.add(marker);

  // The gizmo drives a proxy object; every change is snapped to the vertex
  // quantization grid and written back through the delta map.
  const proxy = new THREE.Object3D();
  scene.add(proxy);
  const gizmo = new TransformControls(camera, canvas);
  gizmo.name = "vgizmo";
  gizmo.setSize(0.9);
  scene.add(gizmo);

  gizmo.addEventListener("dragging-changed", (e) => {
    controls.enabled = !e.value;
    if (e.value) {
      const d = selectedVi !== null ? deltas.get(selectedVi) : null;
      dragStartDelta = d ? d.slice() : null;
      if (onDragStart) onDragStart();
    } else if (selectedVi !== null) {
      commitIfChanged(selectedVi, dragStartDelta);
      scheduleOcclusion();
    }
  });

  gizmo.addEventListener("objectChange", () => {
    if (selectedVi === null) return;
    const desired = unbake(proxy.position.toArray());
    applyDelta(selectedVi, snappedDelta(selectedVi, desired));
    notifySelection();
  });

  function commitIfChanged(vi, prev) {
    const now = deltas.get(vi) || null;
    const same = (prev === null && now === null) ||
      (prev && now && prev.every((v, ax) => v === now[ax]));
    if (same) return;
    if (onCommit) onCommit({ vi, prev, next: now ? now.slice() : null });
    if (persist) persist();
  }

  function active() {
    return !!(model && model.editable && ready && wireOn);
  }

  function updateVisibility() {
    const on = active();
    if (points) points.visible = on;
    if (!on && selectedVi !== null) select(null);
  }

  function pristineOf(vi) {
    const f = model.getFrame();
    const c = firstCorner.get(vi) * 3;
    return [f[c], f[c + 1], f[c + 2]];
  }

  function displayedOf(vi) {
    const p = pristineOf(vi);
    const d = deltas.get(vi);
    return d ? [p[0] + d[0], p[1] + d[1], p[2] + d[2]] : p;
  }

  function positionSelection() {
    if (selectedVi === null) return;
    const [x, y, z] = bake(displayedOf(selectedVi));
    proxy.position.set(x, y, z);
    marker.geometry.getAttribute("position").setXYZ(0, x, y, z);
    marker.geometry.getAttribute("position").needsUpdate = true;
    marker.geometry.computeBoundingSphere();
  }

  function refreshHandles() {
    if (!points) return;
    const pos = points.geometry.getAttribute("position");
    for (let i = 0; i < uniqueVerts.length; i++) {
      const [x, y, z] = bake(displayedOf(uniqueVerts[i]));
      pos.setXYZ(i, x, y, z);
    }
    pos.needsUpdate = true;
    points.geometry.computeBoundingSphere();
    positionSelection();
    scheduleOcclusion();
  }

  function refreshHandlePoint(vi) {
    const i = vertToIdx.get(vi);
    if (points && i !== undefined) {
      const pos = points.geometry.getAttribute("position");
      const [x, y, z] = bake(displayedOf(vi));
      pos.setXYZ(i, x, y, z);
      pos.needsUpdate = true;
    }
    if (vi === selectedVi) positionSelection();
  }

  // Rewrite this vertex's corners in the mesh (baked space) after a delta change.
  function refreshMeshVertex(vi) {
    const attr = model.mesh.geometry.getAttribute("position");
    const [x, y, z] = bake(displayedOf(vi));
    for (const c of cornersByVertex.get(vi)) attr.setXYZ(c, x, y, z);
    attr.needsUpdate = true;
  }

  function applyDelta(vi, delta) {
    if (delta && delta.some((v) => v !== 0)) deltas.set(vi, delta.slice());
    else deltas.delete(vi);
    refreshMeshVertex(vi);
    refreshHandlePoint(vi);
  }

  function refreshColors() {
    if (!points) return;
    const col = points.geometry.getAttribute("color");
    for (let i = 0; i < uniqueVerts.length; i++) {
      const c = i === hoverIdx ? HOVER : (occluded && occluded[i] ? DIM : LIT);
      col.setXYZ(i, c.r, c.g, c.b);
    }
    col.needsUpdate = true;
  }

  // Occlusion pass: a vertex is unpickable (and dimmed) when the model surface
  // sits between it and the camera. Errs visible on grazing hits (epsilon).
  function computeOcclusion() {
    if (!points || !model) return;
    const sphere = model.mesh.geometry.boundingSphere;
    const eps = (sphere ? sphere.radius : 1) * 0.01;
    const origin = camera.position;
    const dir = new THREE.Vector3();
    const p = new THREE.Vector3();
    for (let i = 0; i < uniqueVerts.length; i++) {
      p.set(...bake(displayedOf(uniqueVerts[i])));
      dir.copy(p).sub(origin);
      const dist = dir.length();
      occRaycaster.set(origin, dir.normalize());
      occRaycaster.far = dist;
      const hit = occRaycaster.intersectObject(model.mesh, false)[0];
      occluded[i] = hit && hit.distance < dist - eps ? 1 : 0;
    }
    refreshColors();
  }

  function scheduleOcclusion() {
    clearTimeout(occlusionTimer);
    occlusionTimer = setTimeout(computeOcclusion, 100);
  }
  controls.addEventListener("change", scheduleOcclusion);

  function disposePoints() {
    if (!points) return;
    scene.remove(points);
    points.geometry.dispose();
    points.material.dispose();
    points = null;
  }

  function setModel(m) {
    select(null);
    disposePoints();
    model = m;
    ready = false;
    deltas = new Map();
    uniqueVerts = [];
    vertToIdx = new Map();
    firstCorner = new Map();
    cornersByVertex = new Map();
    occluded = null;
    hoverIdx = -1;
    if (!m) return;
    m.vertexIndices.forEach((vi, c) => {
      if (!firstCorner.has(vi)) {
        firstCorner.set(vi, c);
        cornersByVertex.set(vi, []);
        vertToIdx.set(vi, uniqueVerts.length);
        uniqueVerts.push(vi);
      }
      cornersByVertex.get(vi).push(c);
    });
    if (!m.editable || !uniqueVerts.length) {
      updateVisibility();
      return;
    }
    occluded = new Uint8Array(uniqueVerts.length);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position",
      new THREE.Float32BufferAttribute(new Float32Array(uniqueVerts.length * 3), 3));
    const colors = new Float32Array(uniqueVerts.length * 3);
    for (let i = 0; i < uniqueVerts.length; i++) {
      colors[3 * i] = LIT.r; colors[3 * i + 1] = LIT.g; colors[3 * i + 2] = LIT.b;
    }
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    points = new THREE.Points(geo, new THREE.PointsMaterial({
      vertexColors: true, size: 6, sizeAttenuation: false, depthTest: false, transparent: true,
    }));
    points.name = "vhandles";
    points.renderOrder = 2;
    points.userData.vertices = uniqueVerts;
    scene.add(points);
    m.mesh.geometry.computeBoundingSphere();
    raycaster.params.Points.threshold = m.mesh.geometry.boundingSphere.radius * 0.02;
    // Handle positions are NOT computed here: the caller's frame data may
    // still belong to the previous model. The first applyAnimFrame() call
    // refreshes them via refreshHandles().
    updateVisibility();
  }

  // Seed persisted deltas (from /api/extract) once the working dir is ready;
  // editing is enabled only from this point so an early click can't race the
  // extract round-trip.
  function setDeltas(obj) {
    deltas = new Map(Object.entries(obj).map(([k, v]) => [Number(k), v.slice()]));
    ready = true;
    refreshHandles();
    updateVisibility();
  }

  function setWire(on) {
    wireOn = on;
    updateVisibility();
  }

  function select(vi) {
    selectedVi = vi;
    if (vi === null) {
      gizmo.detach();
      marker.visible = false;
    } else {
      positionSelection();
      gizmo.attach(proxy);
      marker.visible = true;
    }
    notifySelection();
  }

  function notifySelection() {
    if (!onSelectionChange) return;
    if (selectedVi === null) onSelectionChange(null, null);
    else onSelectionChange(selectedVi, (deltas.get(selectedVi) || [0, 0, 0]).slice());
  }

  function setDelta(vi, delta) {
    applyDelta(vi, delta);
    notifySelection();
  }

  function resetSelected() {
    if (selectedVi === null) return;
    const prev = deltas.get(selectedVi);
    if (!prev) return;
    applyDelta(selectedVi, null);
    commitIfChanged(selectedVi, prev.slice());
    notifySelection();
  }

  function deltaForCorner(c) {
    if (!model || !deltas.size) return null;
    return deltas.get(model.vertexIndices[c]) || null;
  }

  function getDeltasObject() {
    const out = {};
    for (const [vi, d] of deltas) out[String(vi)] = d;
    return out;
  }

  function eventNdc(e) {
    const r = canvas.getBoundingClientRect();
    return new THREE.Vector2(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1,
    );
  }

  // Snap a desired decoded position to the quantization grid, clamped to the
  // packed range, and return it as a delta from the pristine position.
  function snappedDelta(vi, desired) {
    const { a, b, max } = model.quant;
    const pristine = pristineOf(vi);
    const d = [0, 0, 0];
    for (let ax = 0; ax < 3; ax++) {
      const packed = Math.round((pristine[ax] - b[ax]) / a[ax]);
      const steps = Math.round((desired[ax] - pristine[ax]) / a[ax]);
      const clamped = Math.max(0, Math.min(max, packed + steps));
      d[ax] = (clamped - packed) * a[ax];
    }
    return d;
  }

  // Nearest handle under the pointer, skipping occluded vertices. -1 if none.
  function pick(e) {
    if (!points) return -1;
    raycaster.setFromCamera(eventNdc(e), camera);
    for (const hit of raycaster.intersectObject(points)) {
      if (!occluded || !occluded[hit.index]) return hit.index;
    }
    return -1;
  }

  function onPointerDown(e) {
    if (!active() || e.button !== 0) return;
    if (gizmo.dragging || gizmo.axis) return; // the gizmo owns this gesture
    const idx = pick(e);
    if (idx >= 0) {
      e.stopPropagation(); // selecting must not also start an orbit
      select(uniqueVerts[idx]);
    } else {
      missDown = [e.clientX, e.clientY]; // maybe a click-deselect (see onPointerUp)
    }
  }

  function onPointerMove(e) {
    if (!active()) return;
    if (gizmo.dragging) return;
    const idx = gizmo.axis ? -1 : pick(e); // gizmo hover wins over handle hover
    if (idx !== hoverIdx) {
      hoverIdx = idx;
      refreshColors();
      canvas.style.cursor = idx >= 0 ? "pointer" : "";
    }
  }

  function onPointerUp(e) {
    // Deselect on a stationary click over empty space; an orbit drag (down,
    // move, up) keeps the selection.
    if (missDown) {
      const still = Math.hypot(e.clientX - missDown[0], e.clientY - missDown[1]) < 5;
      missDown = null;
      if (still && selectedVi !== null && !gizmo.dragging) select(null);
    }
  }

  function onKeyDown(e) {
    if (e.key === "Escape" && selectedVi !== null && active()) select(null);
  }

  // Capture phase so a handle grab preempts OrbitControls' own pointerdown.
  canvas.addEventListener("pointerdown", onPointerDown, { capture: true });
  canvas.addEventListener("pointermove", onPointerMove, { capture: true });
  canvas.addEventListener("pointerup", onPointerUp, { capture: true });
  window.addEventListener("keydown", onKeyDown);

  return {
    setModel,
    setDeltas,
    setWire,
    setDelta,
    select,
    resetSelected,
    deltaForCorner,
    getDeltasObject,
    refreshHandles,
    isEditable: () => !!(model && model.editable),
  };
}
