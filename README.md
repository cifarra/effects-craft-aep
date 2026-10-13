# aep2ec

After Effects `.aep` in, EffectCraft `.ecproj` out.

```sh
uv run aep2ec.py project.aep                 # → project.ecproj next to it
uv run aep2ec.py a.aep b.aep -o converted/   # batch into a folder
uv run aep2ec.py project.aep --keep-json     # also write the intermediate scene
```

Needs `effectcraft-cli` on `PATH` (or `EFFECTCRAFT_CLI=/path/to/effectcraft-cli`), from the
[EffectCraft releases](https://github.com/storytold/effectcraft/releases)
(`effectcraft-cli-<version>-macos-universal.zip`). No After Effects needed.

**Open the result with the EffectCraft app of the same version as the CLI.** The project format
is moving fast: 0.2.0 used `.pcraft` and can't open `.ecproj` at all. Tested with 0.9.2.

## How it works

1. **`aep2ec.py`** reads the binary `.aep` with [py-aep](https://github.com/forticheprod/py-aep)
   and flattens it into a small JSON scene: folders, footage, comps, layers, and every
   property that differs from its default (value, keyframes with eases and tangents, expression).
2. **`rebuild.jsx`** replays that scene inside EffectCraft through its After Effects
   scripting API (`addComp`, `addProperty`, `setValueAtTime`, `setTemporalEaseAtKey`…), so
   the `.ecproj` is written by EffectCraft itself and keeps up with its format.

Everything that doesn't make it over is listed after each file, including any expression that
errors in EffectCraft (checked at the start, middle and end of every comp, the same check as the
app's error bar).

## What carries over

Comps, folders, footage (found next to the `.aep` if the recorded path is gone, otherwise a
placeholder), solids, nulls, adjustment, text, shape, camera and light layers; timing, stretch,
time remap, parenting, track mattes, blend modes and switches; masks; effects with all their
parameters; layer styles; text documents and animators; keyframes with interpolation, eases
and spatial tangents; expressions; comp and layer markers.

- **Third-party plug-ins** are skipped and reported.
- **Pseudo effects** (custom effect UIs) become native expression controls with the same name,
  so `effect("Harmony")("Menu")` keeps working. Dropdown item labels are lost; values aren't.

## Workarounds for EffectCraft 0.9.2 bugs

| After Effects | EffectCraft | aep2ec does |
|---|---|---|
| `LayerQuality.BEST` is 4614 | the enum is numbered in reverse (4614 = wireframe) | passes every enum by name |
| `\r` breaks lines in text | only `\n` does | converts text, and `"\r"` in Source Text expressions, to `\n` |
| expressions saved with CR line endings | `//` comments don't end at CR | normalises expressions to LF |
| `boxTextPos` settable | read-only, box re-centred on resize | sets the box through the document's internal setter |
| Curves channels hidden from scripting | same, but the engine takes `"x,y x,y"` | sets them through the engine call |
| effect param match names | some shifted (Displacement Map), some renamed (text selectors) | finds parameters by display name first |
| `setTrackMatte` keeps the matte's visibility | hides the matte layer | restores it |
| a Key object reads as its value (`linear(t, 0, 1, key(1), key(2))`) | "expression result must be a number" | adds `.value` where a Key is only used as a value |
