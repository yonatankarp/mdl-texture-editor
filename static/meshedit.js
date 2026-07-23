import * as THREE from "three";

// Vertex editing for Wireframe mode (issue #22). Owns the draggable handle
// cloud and the cumulative per-vertex delta map. Deltas live in DECODED model
// space (the server's parse_geometry output, pre Y-up bake) relative to the
// PRISTINE geometry, and are always exact multiples of the format's
// quantization step, so the on-screen mesh is exactly what Save writes.
//
// The mesh's position attribute holds baked (Y-up) coordinates: the loader
// applies rotateX(-90), i.e. baked(x,y,z) = (x, z, -y). unbake inverts that.
const bake = (v) => [v[0], v[2], -v[1]];
const unbake = (v) => [v[0], -v[2], v[1]];

export function createMeshEditor({ scene, camera, canvas, controls, onDragStart, onCommit, persist }) {
  let model = null;      // {mesh, vertexIndices, quant, editable, getFrame}
  let ready = false;     // working dir extracted; persisted deltas seeded
  let wireOn = false;
  let uniqueVerts = [];  // MDL vertex index per handle point
  let firstCorner = new Map();   // vi -> a corner index (for pristine lookup)
  let cornersByVertex = new Map(); // vi -> [corner...]
  let deltas = new Map();        // vi -> [dx, dy, dz] decoded space
  let points = null;             // THREE.Points handle cloud

  const raycaster = new THREE.Raycaster();
  let drag = null; // {vi, startDelta, plane, grabOffset, pointerId}

  function active() {
    return !!(model && model.editable && ready && wireOn);
  }

  function updateVisibility() {
    if (points) points.visible = active();
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

  function refreshHandles() {
    if (!points) return;
    const pos = points.geometry.getAttribute("position");
    for (let i = 0; i < uniqueVerts.length; i++) {
      const [x, y, z] = bake(displayedOf(uniqueVerts[i]));
      pos.setXYZ(i, x, y, z);
    }
    pos.needsUpdate = true;
    points.geometry.computeBoundingSphere();
  }

  // Rewrite this vertex's corners in the mesh (baked space) after a delta change.
  function refreshMeshVertex(vi) {
    const attr = model.mesh.geometry.getAttribute("position");
    const [x, y, z] = bake(displayedOf(vi));
    for (const c of cornersByVertex.get(vi)) attr.setXYZ(c, x, y, z);
    attr.needsUpdate = true;
  }

  function disposePoints() {
    if (!points) return;
    scene.remove(points);
    points.geometry.dispose();
    points.material.dispose();
    points = null;
  }

  function setModel(m) {
    disposePoints();
    model = m;
    ready = false;
    deltas = new Map();
    uniqueVerts = [];
    firstCorner = new Map();
    cornersByVertex = new Map();
    if (!m) return;
    m.vertexIndices.forEach((vi, c) => {
      if (!firstCorner.has(vi)) {
        firstCorner.set(vi, c);
        cornersByVertex.set(vi, []);
        uniqueVerts.push(vi);
      }
      cornersByVertex.get(vi).push(c);
    });
    if (!m.editable || !uniqueVerts.length) {
      updateVisibility();
      return;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position",
      new THREE.Float32BufferAttribute(new Float32Array(uniqueVerts.length * 3), 3));
    points = new THREE.Points(geo, new THREE.PointsMaterial({
      color: 0xffcc00, size: 7, sizeAttenuation: false, depthTest: false, transparent: true,
    }));
    points.name = "vhandles";
    points.renderOrder = 2;
    scene.add(points);
    m.mesh.geometry.computeBoundingSphere();
    raycaster.params.Points.threshold = m.mesh.geometry.boundingSphere.radius * 0.02;
    // Handle positions are NOT computed here: the caller's frame data may
    // still belong to the previous model. The first applyAnimFrame() call
    // refreshes them via refreshHandles().
    updateVisibility();
  }

  // Seed persisted deltas (from /api/extract) once the working dir is ready;
  // editing is enabled only from this point so an early drag can't race the
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

  function setDelta(vi, delta) {
    if (delta && delta.some((v) => v !== 0)) deltas.set(vi, delta.slice());
    else deltas.delete(vi);
    refreshMeshVertex(vi);
    refreshHandles();
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

  function onDown(e) {
    if (!active() || e.button !== 0 || !points) return;
    raycaster.setFromCamera(eventNdc(e), camera);
    const hit = raycaster.intersectObject(points)[0];
    if (!hit) return; // not on a handle: let OrbitControls have the gesture
    e.stopPropagation();
    e.preventDefault();
    const vi = uniqueVerts[hit.index];
    const handle = new THREE.Vector3(...bake(displayedOf(vi)));
    const plane = new THREE.Plane();
    plane.setFromNormalAndCoplanarPoint(
      camera.getWorldDirection(new THREE.Vector3()), handle);
    const at = new THREE.Vector3();
    raycaster.ray.intersectPlane(plane, at);
    const existing = deltas.get(vi);
    drag = {
      vi,
      plane,
      grabOffset: at ? handle.clone().sub(at) : new THREE.Vector3(),
      startDelta: existing ? existing.slice() : null,
      pointerId: e.pointerId,
    };
    controls.enabled = false;
    canvas.setPointerCapture(e.pointerId);
    if (onDragStart) onDragStart();
  }

  function onMove(e) {
    if (!drag) return;
    e.stopPropagation();
    raycaster.setFromCamera(eventNdc(e), camera);
    const at = new THREE.Vector3();
    if (!raycaster.ray.intersectPlane(drag.plane, at)) return;
    const desired = unbake(at.add(drag.grabOffset).toArray());
    setDelta(drag.vi, snappedDelta(drag.vi, desired));
  }

  function onUp(e) {
    if (!drag) return;
    e.stopPropagation();
    try { canvas.releasePointerCapture(drag.pointerId); } catch (_) {}
    controls.enabled = true;
    const { vi, startDelta } = drag;
    drag = null;
    const now = deltas.get(vi) || null;
    const same = (startDelta === null && now === null) ||
      (startDelta && now && startDelta.every((v, ax) => v === now[ax]));
    if (same) return;
    if (onCommit) onCommit({ vi, prev: startDelta, next: now ? now.slice() : null });
    if (persist) persist();
  }

  // Capture phase so a handle grab preempts OrbitControls' own pointerdown.
  canvas.addEventListener("pointerdown", onDown, { capture: true });
  canvas.addEventListener("pointermove", onMove, { capture: true });
  canvas.addEventListener("pointerup", onUp, { capture: true });
  canvas.addEventListener("pointercancel", onUp, { capture: true });

  return {
    setModel,
    setDeltas,
    setWire,
    setDelta,
    deltaForCorner,
    getDeltasObject,
    refreshHandles,
    isEditable: () => !!(model && model.editable),
  };
}
