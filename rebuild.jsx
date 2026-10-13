// rebuild.jsx: rebuild an aep2ec SCENE through the After Effects scripting API.
// aep2ec.py prepends `var SCENE = {...};` and runs it with `effectcraft-cli script`.
// Plain ExtendScript, apart from EffectCraft hooks for the text box position, Curves and
// the expression error check.

(function (SCENE) {
  var project = app.project;
  var byId = {};        // scene item id -> rebuilt item
  var report = [];      // what didn't make it, one line each
  var expressions = []; // applied last: they may point anywhere in the project
  var locks = [];

  function warn(msg) {
    for (var i = 0; i < report.length; i++) if (report[i] === msg) return;
    report.push(msg);
  }
  function attempt(where, fn) {
    try { return fn(); } catch (e) { warn(where + ": " + e.message); }
  }
  function assign(obj, attrs, where) {
    for (var k in attrs) (function (k) {
      attempt(where + " " + k, function () {
        var v = value(null, attrs[k]);
        if (obj[k] !== v) obj[k] = v;
      });
    })(k);
  }

  // ---- values ----------------------------------------------------------

  function value(prop, v) {
    if (v === null || typeof v !== "object" || v instanceof Array) return v;
    var out, k;
    if (v.$enum) {
      out = $.global[v.$enum[0]] && $.global[v.$enum[0]][v.$enum[1]];
      if (out === undefined) throw new Error("unknown " + v.$enum.join("."));
    } else if (v.$shape) {
      out = new Shape();
      for (k in v.$shape) out[k] = v.$shape[k];
    } else if (v.$text) {
      out = prop.value;
      for (k in v.$text) attempt("text " + k, function () { out[k] = value(null, v.$text[k]); });
      // EffectCraft keeps boxTextPos read-only and centres the box on resize
      var pos = v.$text.boxTextPos, size = v.$text.boxTextSize;
      if (pos && String(out.boxTextPos) !== String(pos) && out.__set) out.__set("box", pos.concat(size));
    } else if (v.$marker) {
      out = new MarkerValue(v.$marker.comment || "");
      for (k in v.$marker) {
        if (k === "params") out.setParameters(v.$marker.params);
        else if (k !== "comment") attempt("marker " + k, function () { out[k] = v.$marker[k]; });
      }
    }
    return out;
  }

  function eases(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) out.push(new KeyframeEase(list[i][0], list[i][1]));
    return out;
  }

  function animate(prop, node, where) {
    if (node.sep) attempt(where + " separate", function () { prop.dimensionsSeparated = true; });
    if (node.v !== undefined) attempt(where, function () { prop.setValue(value(prop, node.v)); });
    if (node.k) attempt(where + " keys", function () {
      var i, k, n = node.k.length;
      while (prop.numKeys) prop.removeKey(1); // e.g. the pair enabling Time Remap creates
      for (i = 0; i < n; i++) prop.setValueAtTime(node.k[i].t, value(prop, node.k[i].v));
      for (i = 0; i < n; i++) {
        k = node.k[i];
        if (k["in"] === undefined) continue; // markers
        var at = prop.nearestKeyIndex(k.t);
        prop.setInterpolationTypeAtKey(at, value(null, k["in"]), value(null, k.out));
        if (k.ie) attempt(where + " ease", function () { prop.setTemporalEaseAtKey(at, eases(k.ie), eases(k.oe)); });
        if (k.ist) attempt(where + " tangents", function () {
          prop.setSpatialTangentsAtKey(at, k.ist, k.ost);
          prop.setSpatialContinuousAtKey(at, k.sc);
          prop.setSpatialAutoBezierAtKey(at, k.sab);
        });
        attempt(where + " key flags", function () {
          prop.setTemporalContinuousAtKey(at, k.tc);
          prop.setTemporalAutoBezierAtKey(at, k.tab);
          if (k.rv) prop.setRovingAtKey(at, true);
        });
      }
    });
    if (node.x) expressions.push([prop, node.x, !node.xOff, where]);
  }

  // A child of `group` by display name, else by match name: EffectCraft's match names
  // differ from After Effects' in places (Displacement Map params, text selectors).
  function get(group, key) {
    try { return group.property(key); } catch (e) { return null; }
  }
  function find(group, node) {
    return (node.n && get(group, node.n)) || get(group, node.m);
  }
  // Layer styles can't be added, only switched on from the Layer ▸ Layer Styles menu.
  function styleOn(layer, name) {
    layer.containingComp.openInViewer(); // menu commands act on the active comp
    layer.selected = true;
    app.executeCommand(app.findMenuCommandId(name));
    layer.selected = false;
  }
  function child(group, node, layer) {
    var p = !node.add && find(group, node);
    if (p) return p;
    if (node.m === "ADBE Layer Styles") {
      for (var i = 0; i < node.c.length; i++) if (node.c[i].n !== "Blending Options") styleOn(layer, node.c[i].n);
      return find(group, node);
    }
    if (group.matchName === "ADBE Layer Styles") {
      styleOn(layer, node.n);
      return find(group, node);
    }
    return group.addProperty(node.m);
  }

  function build(group, nodes, where, layer) {
    for (var i = 0; i < nodes.length; i++) (function (node) {
      var label = where + " › " + (node.n || node.m);
      if (node.v && node.v.$curves) return curves(group, node.v.$curves, label);
      var p = attempt(label, function () { return child(group, node, layer); });
      if (!p) return;
      if (node.n && node.add) attempt(label + " name", function () { if (p.name !== node.n) p.name = node.n; });
      if (node.maskMode !== undefined) assign(p, { maskMode: node.maskMode, inverted: node.inverted,
        rotoBezier: node.rotoBezier, color: node.color }, label);
      if (node.c) build(p, node.c, label, layer);
      else if (!node.add) animate(p, node, label);
      if (node.off) attempt(label + " off", function () { p.enabled = false; });
      if (node.maskMode !== undefined && node.locked) locks.push(p);
    })(nodes[i]);
  }

  // Curves channels are hidden parameters (in After Effects too): EffectCraft's engine
  // call still takes them, as "x,y x,y" strings.
  function curves(fx, channels, where) {
    for (var name in channels) (function (name) {
      attempt(where + " " + name, function () {
        var p = fx.property(name);
        __call("prop.set", p.__ref({ value: channels[name] }));
      });
    })(name);
  }

  // ---- items -----------------------------------------------------------

  function placeIn(item, parentId) {
    var folder = byId[parentId];
    if (folder) item.parentFolder = folder;
  }

  function footage(f) {
    var item;
    if (f.file) item = attempt(f.name + " import", function () {
      var opts = new ImportOptions(new File(f.file));
      if (f.sequence) opts.sequence = true;
      return project.importFile(opts);
    });
    if (!item) item = project.importPlaceholder(f.name, f.width || 1920, f.height || 1080,
      f.frameRate, f.duration || 1);
    assign(item.mainSource, f.interp, f.name);
    return item;
  }

  function addLayer(comp, L) {
    var ls = comp.layers;
    switch (L.kind) {
      case "TextLayer": return L.box ? ls.addBoxText(L.box) : ls.addText();
      case "ShapeLayer": return ls.addShape();
      case "CameraLayer": return ls.addCamera(L.name, [comp.width / 2, comp.height / 2]);
      case "LightLayer": return ls.addLight(L.name, [comp.width / 2, comp.height / 2]);
      case "Null": return ls.addNull(comp.duration);
      case "Solid":
        var s = L.solid;
        return ls.addSolid(s.color, s.name, s.width, s.height, s.pixelAspect, comp.duration);
      case "AV":
        if (!byId[L.source]) throw new Error("missing source");
        return ls.add(byId[L.source]);
    }
    throw new Error("unsupported layer type " + L.kind);
  }

  function layers(C) {
    var comp = byId[C.id], made = [], i, L, layer;
    // bottom-up: each add lands on top, so the stack ends in the original order
    for (i = C.layers.length - 1; i >= 0; i--) {
      L = C.layers[i];
      layer = attempt(C.name + " › " + L.name, function () { return addLayer(comp, L); });
      made[i] = layer || comp.layers.addNull(); // keep indices aligned
      if (!layer) made[i].name = L.name + " (unconverted)";
    }
    for (i = 0; i < C.layers.length; i++) {
      L = C.layers[i]; layer = made[i];
      var where = C.name + " › " + L.name;
      layer.name = L.name;
      assign(layer, L.attrs, where);
      // timing first: stretch rescales keys, so keys go in afterwards
      if (L.timeRemap) attempt(where + " time remap", function () { layer.timeRemapEnabled = true; });
      assign(layer, { startTime: L.startTime, stretch: L.stretch }, where);
      assign(layer, { inPoint: L.inPoint, outPoint: L.outPoint }, where);
      if (L.parent) attempt(where + " parent", function () { layer.setParentWithJump(made[L.parent - 1]); });
    }
    for (i = 0; i < C.layers.length; i++) {
      L = C.layers[i]; layer = made[i];
      build(layer, L.props, C.name + " › " + L.name, layer);
      if (L.matte) attempt(C.name + " › " + L.name + " matte", function () {
        var matte = made[L.matte[0] - 1];
        layer.setTrackMatte(matte, value(null, L.matte[1]));
        matte.enabled = C.layers[L.matte[0] - 1].attrs.enabled; // setTrackMatte hides the matte
      });
      if (L.locked) locks.push(layer);
    }
  }

  var items = SCENE.items, i, it;
  for (i = 0; i < items.length; i++) {
    it = items[i];
    if (it.type === "Folder") byId[it.id] = project.items.addFolder(it.name);
    else if (it.type === "Footage") byId[it.id] = footage(it);
    else if (it.type === "Comp") {
      byId[it.id] = project.items.addComp(it.name, it.width, it.height, it.pixelAspect, it.duration, it.frameRate);
      assign(byId[it.id], it.attrs, it.name);
      if (it.markers) animate(byId[it.id].markerProperty, it.markers, it.name + " markers");
    }
  }
  for (i = 0; i < items.length; i++) placeIn(byId[items[i].id], items[i].parent);
  for (i = 0; i < items.length; i++) if (items[i].type === "Comp") layers(items[i]);

  for (i = 0; i < expressions.length; i++) (function (e) {
    attempt(e[3] + " expression", function () {
      e[0].expression = e[1];
      e[0].expressionEnabled = e[2];
    });
  })(expressions[i]);
  for (i = 0; i < locks.length; i++) locks[i].locked = true;

  // Expression errors as the app's error bar shows them, at the start, middle and end
  // of every comp (the scripting API's expressionError stays empty in EffectCraft).
  for (i = 0; i < items.length; i++) if (items[i].type === "Comp") (function (C) {
    var comp = byId[C.id], ts = [0, C.duration / 2, Math.max(0, C.duration - 1 / C.frameRate)];
    for (var j = 0; j < ts.length; j++) attempt(C.name + " expression check", function () {
      var res = __call("expr.errors", { comp: comp.id, time: ts[j] });
      for (var e = 0; e < res.errors.length; e++) {
        var err = res.errors[e];
        warn(C.name + " › " + err.layerName + " › " + err.name + " expression: " + err.message);
      }
    });
  })(items[i]);

  writeLn("REPORT " + JSON.stringify(report));
})(SCENE);
