"""aep2ec: After Effects .aep in, EffectCraft .ecproj out.

py-aep reads the binary .aep into an ExtendScript-shaped object model. We flatten
that into a small JSON scene, then let EffectCraft rebuild it through its own
After Effects scripting API (rebuild.jsx), so the .ecproj is always written by
EffectCraft itself.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import py_aep

HERE = Path(__file__).resolve().parent

# Pseudo effects (custom effect UIs) don't exist outside After Effects. Each of
# their parameters becomes the matching native expression control.
CONTROL_FOR = {0: "ADBE Layer Control", 1: "ADBE Slider Control", 2: "ADBE Slider Control",
               3: "ADBE Angle Control", 4: "ADBE Checkbox Control", 5: "ADBE Color Control",
               6: "ADBE Point Control", 7: "ADBE Dropdown Control", 10: "ADBE Slider Control",
               18: "ADBE Point3D Control"}

LAYER_ATTRS = ["comment", "label", "enabled", "solo", "shy", "audio_enabled", "three_d_layer",
               "three_d_per_char", "adjustment_layer", "guide_layer", "environment_layer",
               "motion_blur", "collapse_transformation", "blending_mode", "quality",
               "sampling_quality", "preserve_transparency", "auto_orient", "frame_blending_type",
               "light_type"]
COMP_ATTRS = ["comment", "label", "bg_color", "work_area_start", "work_area_duration",
              "display_start_time", "motion_blur", "shutter_angle", "shutter_phase",
              "frame_blending", "hide_shy_layers", "preserve_nested_frame_rate",
              "preserve_nested_resolution", "draft3d"]
SOURCE_ATTRS = ["loop", "conform_frame_rate", "alpha_mode", "invert_alpha", "premul_color",
                "field_separation_type", "remove_pulldown"]
TEXT_ATTRS = ["text", "font", "font_size", "apply_fill", "apply_stroke", "fill_color",
              "stroke_color", "stroke_width", "stroke_over_fill", "justification", "tracking",
              "leading", "auto_leading", "baseline_shift", "horizontal_scale", "vertical_scale",
              "faux_bold", "faux_italic", "all_caps", "small_caps", "superscript", "subscript",
              "tsume", "first_line_indent", "start_indent", "end_indent", "space_before",
              "space_after", "box_text_size", "box_text_pos"]
MARKER_ATTRS = ["comment", "duration", "chapter", "url", "frame_target", "cue_point_name",
                "event_cue_point", "label", "protected_region", "params"]

# Groups that only make sense inside After Effects (Essential Graphics plumbing).
SKIP = {"ADBE Source Options Group"}


def lf(s: str) -> str:
    """After Effects breaks lines with CR; EffectCraft only with LF (and doesn't end // comments at CR)."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


KEY_CALL = re.compile(r"(?:\b|\.)key\(\s*[^()]*\)(?!\s*\.)")


def key_values(expr: str) -> str:
    """After Effects reads a Key object as its value (`linear(t, 0, 1, key(1), key(2))`);
    EffectCraft doesn't. Add `.value` where a Key is only ever used as a value."""
    out, last = [], 0
    for m in KEY_CALL.finditer(expr):
        var = re.search(r"(\w+)\s*=\s*$", expr[:m.start()])
        if var and re.search(rf"\b{var.group(1)}\s*\.", expr):
            continue                           # the Key is used as an object elsewhere
        out += [expr[last:m.end()], ".value"]
        last = m.end()
    return "".join(out) + expr[last:]


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest).replace("ThreeD", "threeD")


def plain(v):
    """py-aep value -> JSON. Shapes, text documents and markers get a tagged object."""
    if isinstance(v, enum.Enum):
        # by name: the numbers aren't reliably the same in EffectCraft (LayerQuality is reversed)
        return v.value if type(v).__name__ == "Label" else {"$enum": [type(v).__name__, v.name]}
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    kind = type(v).__name__
    if kind == "Shape":
        return {"$shape": {"vertices": v.vertices, "inTangents": v.in_tangents,
                           "outTangents": v.out_tangents, "closed": v.closed}}
    if kind == "TextDocument":
        doc = attrs(v, TEXT_ATTRS, skip_box=not v.box_text)
        doc["text"] = lf(doc["text"])
        return {"$text": doc}
    if kind == "MarkerValue":
        return {"$marker": attrs(v, MARKER_ATTRS)}
    if kind == "Curves":                       # Curves effect: one "x,y x,y" string per channel
        return {"$curves": {ch.name.upper() if ch.name == "rgb" else ch.name.title():
                            " ".join(f"{x / 255:g},{y / 255:g}" for x, y in ch.points)
                            for ch in v.channels.values() if not ch.is_identity}}
    raise TypeError(kind)


def attrs(obj, names, skip_box=False):
    out = {}
    for n in names:
        if skip_box and n.startswith("box_"):
            continue
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if v is not None:
            out[camel(n)] = plain(v)
    return out


class Dumper:
    def __init__(self, aep: Path):
        self.aep = aep
        self.notes: list[str] = []

    def note(self, msg: str):
        if msg not in self.notes:
            self.notes.append(msg)

    # -- properties -------------------------------------------------------

    def prop(self, p, where):
        node = {"m": p.match_name, "n": p.name}
        if getattr(p, "is_separation_leader", False) and p.dimensions_separated:
            node["sep"] = True
        if p.is_modified:
            try:
                node["v"] = plain(p.value)
            except TypeError as e:
                self.note(f"{where}: {p.name} holds a {e} value, left at default")
        keys = []
        for k in p.keyframes:
            try:
                keys.append(self.key(k))
            except TypeError as e:
                self.note(f"{where}: {p.name} keys hold {e} values, left unanimated")
                keys = []
                break
        if keys:
            node["k"] = keys
            node.pop("v", None)
        if p.expression:
            node["x"] = key_values(lf(p.expression))
            if p.match_name == "ADBE Text Document":   # "\r" line breaks built in Source Text
                node["x"] = node["x"].replace("\\r", "\\n")
            if not p.expression_enabled:
                node["xOff"] = True
        return node if len(node) > 2 else None

    @staticmethod
    def key(k):
        out = {"t": k.time, "v": plain(k.value)}
        if type(k.value).__name__ == "MarkerValue":
            return out                        # markers have no interpolation
        out |= {"in": plain(k.in_interpolation_type), "out": plain(k.out_interpolation_type)}
        if k.in_temporal_ease:
            out["ie"] = [[e.speed, e.influence] for e in k.in_temporal_ease]
            out["oe"] = [[e.speed, e.influence] for e in k.out_temporal_ease]
        if k.in_spatial_tangent is not None:
            out["ist"], out["ost"] = k.in_spatial_tangent, k.out_spatial_tangent
            out["sc"], out["sab"] = k.spatial_continuous, k.spatial_auto_bezier
        out["tc"], out["tab"], out["rv"] = k.temporal_continuous, k.temporal_auto_bezier, k.roving
        return out

    def group(self, g, where, indexed_parent=False):
        if g.match_name.startswith("Pseudo/"):
            return self.pseudo(g, where)
        kids = self.children(g, where, indexed=g.property_type.name == "INDEXED_GROUP")
        node = {"m": g.match_name, "n": g.name}
        if indexed_parent:
            node["add"] = True                 # must be addProperty'd, even if untouched
        if getattr(g, "can_set_enabled", False) and not g.enabled:
            node["off"] = True
        if g.is_mask:
            node.update(attrs(g, ["mask_mode", "inverted", "roto_bezier", "locked", "color"]))
        if kids:
            node["c"] = kids
        return node if len(node) > 2 else None

    def children(self, g, where, indexed=False):
        kids = []
        for p in g:
            if p.match_name in SKIP or not p.name:   # hidden params have no name
                continue
            if type(p).__name__ == "Property":
                if getattr(p, "is_separation_follower", False) and \
                        not p.separation_leader.dimensions_separated:
                    continue
                n = self.prop(p, where)
            else:
                n = self.group(p, where, indexed_parent=indexed)
            if n:
                kids.extend(n if isinstance(n, list) else [n])
        return kids

    def pseudo(self, fx, where):
        params = [p for p in fx if type(p).__name__ == "Property"]
        out = []
        for p in params:
            mn = CONTROL_FOR.get(int(p.property_control_type))
            if not mn:
                self.note(f"{where}: pseudo effect '{fx.name}' param '{p.name}' has no native control")
                continue
            leaf = self.prop(p, where)
            ctl = {"m": mn, "add": True, "n": fx.name if len(params) == 1 else f"{fx.name} {p.name}"}
            if leaf:
                ctl["c"] = [dict(leaf, m=mn + "-0001", n=None)]
            out.append(ctl)
        self.note(f"{where}: pseudo effect '{fx.name}' -> native expression control(s)")
        return out

    # -- layers, items ----------------------------------------------------

    def layer(self, l, comp):
        where = f"{comp.name} › {l.name}"
        kind = type(l).__name__
        src = getattr(l, "source", None)
        if kind == "AVLayer":
            main = getattr(src, "main_source", None)
            kind = ("Null" if l.null_layer else
                    "Solid" if type(main).__name__ == "SolidSource" else
                    "AV")
        out = {"kind": kind, "name": l.name, "attrs": attrs(l, LAYER_ATTRS),
               "startTime": l.start_time, "stretch": l.stretch,
               "inPoint": l.in_point, "outPoint": l.out_point,
               "locked": l.locked, "timeRemap": getattr(l, "time_remap_enabled", False)}
        if kind == "AV":
            out["source"] = src.id
        if kind == "Solid":
            out["solid"] = {"color": plain(src.main_source.color), "name": src.name,
                            "width": src.width, "height": src.height, "pixelAspect": src.pixel_aspect}
        if kind == "TextLayer":
            doc = l.property("ADBE Text Properties").property("ADBE Text Document").value
            out["box"] = list(doc.box_text_size) if doc.box_text else None
        layers = list(comp.layers)
        if l.parent is not None:
            out["parent"] = layers.index(l.parent) + 1
        if getattr(l, "track_matte_layer", None) is not None:
            out["matte"] = [layers.index(l.track_matte_layer) + 1, plain(l.track_matte_type)]
        out["props"] = [n for n in self.children(l, where)
                        if not (n["m"] == "ADBE Time Remapping" and not out["timeRemap"])
                        and not (n["m"] == "ADBE Material Options Group" and not l.three_d_layer)]
        return out

    def footage(self, f):
        src = f.main_source
        kind = type(src).__name__
        out = {"type": "Footage", "id": f.id, "name": f.name, "parent": f.parent_folder.id,
               "width": f.width, "height": f.height, "duration": f.duration,
               "frameRate": f.frame_rate or 30, "interp": self.interp(src)}
        if kind == "SolidSource":
            return None                       # rebuilt per layer by addSolid
        if kind == "FileSource":
            path = self.locate(src.file or src.missing_footage_path or "")
            if path:
                out["file"] = str(path)
                out["sequence"] = len(src.file_names or []) > 1
            else:
                self.note(f"footage '{f.name}' not found ({src.file}) -> placeholder")
        return out

    @staticmethod
    def interp(src):
        """Interpret Footage settings that differ from a plain import."""
        out = attrs(src, SOURCE_ATTRS)
        for k in ("fieldSeparationType", "removePulldown"):
            if out.get(k, {}).get("$enum", [0, "OFF"])[1] == "OFF":
                out.pop(k, None)
        if not src.has_alpha:
            out.pop("alphaMode", None)
            out.pop("premulColor", None)
            out.pop("invertAlpha", None)
        return out

    def locate(self, recorded: str) -> Path | None:
        """The recorded path, else a file of the same name next to the .aep (collected projects)."""
        if recorded and Path(recorded).exists():
            return Path(recorded)
        name = Path(recorded).name
        if name:
            for hit in self.aep.parent.rglob(name):
                return hit
        return None

    def comp(self, c):
        out = {"type": "Comp", "id": c.id, "name": c.name, "parent": c.parent_folder.id,
               "width": c.width, "height": c.height, "pixelAspect": c.pixel_aspect,
               "duration": c.duration, "frameRate": c.frame_rate, "attrs": attrs(c, COMP_ATTRS)}
        if c.marker_property is not None:
            m = self.prop(c.marker_property, c.name)
            if m:
                out["markers"] = m
        out["layers"] = [self.layer(l, c) for l in c.layers]
        return out

    def dump(self):
        project = py_aep.parse(str(self.aep)).project
        items = []
        for it in project.items.values():
            kind = type(it).__name__
            if kind == "FolderItem" and it.parent_folder is not None:
                items.append({"type": "Folder", "id": it.id, "name": it.name, "parent": it.parent_folder.id})
            elif kind == "FootageItem":
                f = self.footage(it)
                if f:
                    items.append(f)
            elif kind == "CompItem":
                items.append(self.comp(it))
        # folders before everything, so parents exist when children are placed
        items.sort(key=lambda i: i["type"] != "Folder")
        return {"source": str(self.aep), "items": items}


def find_cli() -> str:
    cli = os.environ.get("EFFECTCRAFT_CLI") or shutil.which("effectcraft-cli")
    if not cli:
        sys.exit("aep2ec: effectcraft-cli not found (put it on PATH or set EFFECTCRAFT_CLI)")
    return cli


def convert(aep: Path, out: Path, keep_json: bool = False) -> list[str]:
    dumper = Dumper(aep)
    scene = dumper.dump()
    if keep_json:
        out.with_suffix(".scene.json").write_text(json.dumps(scene, indent=1))
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "rebuild.jsx"
        script.write_text(f"var SCENE = {json.dumps(scene)};\n" + (HERE / "rebuild.jsx").read_text())
        run = subprocess.run([find_cli(), "script", str(script), "--empty", "--save-as", str(out.resolve())],
                             capture_output=True, text=True, cwd=tmp)
    if run.returncode != 0:
        sys.exit(f"aep2ec: EffectCraft failed on {aep.name}\n{run.stderr or run.stdout}")
    issues = []
    for line in run.stdout.splitlines():
        if line.startswith("REPORT "):
            issues = json.loads(line[7:])
    return dumper.notes + issues


def main():
    ap = argparse.ArgumentParser(prog="aep2ec", description="Convert After Effects .aep to EffectCraft .ecproj")
    ap.add_argument("aep", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="output .ecproj (single input) or directory")
    ap.add_argument("--keep-json", action="store_true", help="also write the intermediate scene JSON")
    args = ap.parse_args()
    for aep in args.aep:
        out = args.out or aep.with_suffix(".ecproj")
        if out.is_dir():
            out = out / aep.with_suffix(".ecproj").name
        notes = convert(aep, out, args.keep_json)
        print(f"✓ {aep.name} → {out}")
        for n in notes:
            print(f"  · {n}")


if __name__ == "__main__":
    main()
