# CineDirector ODK — handoff

Updated 2026-09-12. Development project:
`C:\Users\Round\Documents\Unreal Projects\ImmaDegenTest`.

Read `README.md` first if you want to *use* the plugin. This file is for whoever
picks up the *development* of it.

## Where this lives

This repo is the master copy. It is installed into the ODK engine rather than
into each project, by `Tools/Install-EnginePlugin.ps1`, which junctions this
folder into `<Engine>/Engine/Plugins/Marketplace/CineDirectorODK`. So editing a
file here changes the plugin in every ODK project, and a `git pull` updates them
all. There is no per-project copy to keep in sync, and there should not be: two
plugins with the same name is a startup error.

The ODK installs into numbered folders (`C:\ODK\Installs\1`, `\2`, ...), so an
engine update lands in a new folder without the plugin. Re-run the install script
after one; with no arguments it picks the highest-numbered install.

The UE 5.8 C++ original is a **separate repo**,
[drdogeDOTeth/CineDirector](https://github.com/drdogeDOTeth/CineDirector). The
two share the shot-plan format and nothing else. Changes to the planner's
vocabulary or schema should land in both; changes to executors should not.

## What this is

The Otherside ODK is Unreal **5.5.4**, licensee build CL 299273, branch
`++M2Unreal+release`, installed at `C:\ODK\Installs\1\output\Windows`. It is a
**Blueprint-only engine**: there are no engine C++ headers anywhere under `C:\ODK`
(no `CoreMinimal.h`; `Engine/Source` holds only `Programs` and two `.Target.cs`).
Otherside's own docs confirm it. No C++ module can ever be compiled against it, so
the UE 5.8 CineDirector plugin cannot be loaded here and its assets cannot be
copied back, because Unreal assets are forward-compatible only.

The answer was a **Python port** in a content-only plugin at
`Plugins/CineDirectorODK`. It reuses the same strict JSON shot-plan schema as the
5.8 C++ build, so one planner feeds both executors.

**Agreed scope:** core shots, lighting/fog/sky, the three LLM backends, Movie
Render Queue, and face/lipsync. **Not** body performance.

## Hard-won facts, do not relearn these

- **Never put `EngineVersion` in the `.uplugin`.** This licensee build reports no
  compatible changelist, so any stated version is rejected: the log says
  `requires engine version '5.5.0' and may not be compatible with
  '5.5.4-299273+++M2Unreal+release'`, a modal is auto-answered No, and the plugin
  is silently skipped. The file deliberately has no such field.
- Python cannot build Slate. Two shapes work and nothing else does:
  a `@unreal.uclass()` UObject shown with `unreal.EditorDialog.show_object_details_view`
  (the class is `EditorDialog`; `EditorDialogLibrary` does not exist), or an
  Editor Utility Widget filled at runtime.
- `unreal.WidgetTree` and `UserWidget.get_root_widget` are **not exposed** in this
  build, so a widget tree cannot be authored from script. But
  `unreal.new_object(SomeWidgetClass)`, `add_child`, and `on_clicked.add_callable`
  all work, which is the whole basis of the dockable panel.
- Anything built at runtime must stay referenced from module scope or it is
  collected and the buttons go dead seconds after the tab opens. That is what
  `dock_panel._LIVE` is for.
- `@unreal.uenum()` / `unreal.uvalue` / `unreal.EnumBase` **do** work and give a
  real dropdown. Verified round-trip.
- Call `AssetRegistry.search_all_assets(True)` before loading any asset by path,
  or `LoadAsset` fails with "could not be found in the Asset Registry".
- Sequencer channels: `unreal.MovieSceneSectionExtensions.get_all_channels(section)`,
  not `section.get_channels`. The enum is `unreal.MovieSceneTimeUnit`, not
  `SequenceTimeUnit`. Scripting channels expose keys but **no default setter**.
- `get_bone_transform` takes a bone **name**; an index raises. There is no
  `get_bone_location` at all.
- `ALight::GetLightComponent` is not exposed; find the component by class.
- `AnimationDataController.set_number_of_frames` needs a `FrameNumber`, not an int.
  Use `set_play_length(seconds, True)`. Frame count only updates after `close_bracket`.
- **Every void character's face is 90° off its root yaw.** Facing is resolved from
  eye bones (`EyeL`/`EyeR` spellings included) with a fuzzy scan, not root yaw.
  Without this, "from the front" frames their side on every shot.
- Spawning a `SkeletalMeshActor` from Python **hard-crashes the headless editor**
  under `-nullrhi` (`EXCEPTION_INT_DIVIDE_BY_ZERO` inside Engine). Test against
  actors already in the level. The plugin itself never spawns one.
- Movie Render Queue renders inside a **PIE session**. See the crash note below.
- UE5 uses an infinite reversed-Z far plane. `CineCameraComponent` exposes only
  `custom_near_clipping_plane`, so there is nothing to push out for distant shots.
- Python 3.11.8, numpy 1.24.4, 57 CA certs loadable, so HTTPS works.

## Files

All under `Plugins/CineDirectorODK/`.

| File | What it does | State |
|---|---|---|
| `CineDirectorODK.uplugin` | content-only, no `Modules`, no `EngineVersion` | done |
| `Content/EUW_CineDirector.uasset` | the panel shell; needs a `Root` Scroll Box once | done |
| `Content/Python/init_unreal.py` | defers menu registration until the Tools menu exists | done |
| `cinedirector/vec.py` | pure-tuple Z-up maths, yaw continuity | done |
| `cinedirector/plan.py` | shot-plan types, strict schema, prompts, parser, look continuity | done |
| `cinedirector/scene.py` | level snapshot, head/eye bones, bounds, backdrop rejection | done |
| `cinedirector/geometry.py` | framing, fit distance, move sampling | done |
| `cinedirector/executor.py` | spawns cameras, bakes keys, camera cuts | done |
| `cinedirector/grammar.py` | rule-based parser, 16 style kits | done |
| `cinedirector/lighting.py` | sun/sky/fog keying, 10 times of day | done |
| `cinedirector/render.py` | Movie Render Queue, wave output, ffmpeg encode | done |
| `cinedirector/face.py` | blendshape analyser, 29 canonical slots | done |
| `cinedirector/lipsync.py` | audio to viseme frames (numpy DFT bins) | done |
| `cinedirector/face_baker.py` | additive curves-only AnimSequence + Sequencer | done |
| `cinedirector/settings.py` | JSON settings + API-key lookup | done, untested |
| `cinedirector/llm.py` | Claude / OpenAI-style / Gemini backends | done, stub-tested |
| `cinedirector/panel.py` | menus and the modal dialogs | done |
| `cinedirector/dock_panel.py` | the dockable panel | done |

## What changed since the first handoff

### The dockable panel

`dock_panel.py` is the panel the 5.8 build has: prompt box, example prompts,
clickable vocabulary chips, face and audio controls, render buttons, all in one
docked tab instead of six modal dialogs.

It works by spawning `Content/EUW_CineDirector` through `EditorUtilitySubsystem`
and then building every widget under it at runtime. **The asset needs one manual
step, once:** open it, drag a Scroll Box in, rename it `Root`, tick Is Variable,
compile, save. There is nothing to anchor, because that Scroll Box becomes the
root widget and fills the tab by itself. `open_panel()` shows those instructions
if it cannot find the container. `_find_root` duck-types on `add_child`, so any
panel-like widget named in `ROOT_CANDIDATES` works.

Two styling traps, both fixed and both worth remembering. UMG's default button and
editable-text backgrounds are near-white on a dark editor, so every control needs
`set_background_color`. And the default editable text is 24pt, three times too
tall for a row; the size lives at `widget_style` → `text_style` → `font.size`,
not on the widget.

Buttons are only ever registered with `Run Utility Widget` in mind as a failure
mode: pressing that in the Content Browser bypasses Python entirely and produces
an empty tab. Always open the panel from Tools > CineDirector > Panel.

### Framing things that are far away or very large

- `SHOT_SIZES` gained `"fit"`, and `Segment` gained `frame_level`.
- `geometry.fit_distance(radius, focal_length_mm)` answers "how far back to see
  all of it", as `radius / tan(hfov/2) * FIT_MARGIN`. The ordinary framing
  multiples answer a different question, "how small in frame", which is why an
  extreme wide of a landscape used to sit inside the landscape.
- `scene.is_backdrop()` rejects sky spheres, atmosphere and anything over 250,000 cm
  that is not a Landscape, so "everything" does not resolve to the sky.
- `SceneContext` carries `has_level_bounds`, `level_center` and `level_extent`;
  `executor._level_sample()` turns those into a subject when `frame_level` is set
  and nothing else matched.
- `grammar` has `LEVEL_PHRASES` and `FIT_PHRASES`, and a post-framing override:
  "wide shot of everything" means show the level, not make the level small, so
  `unspecified`/`wide`/`extreme_wide` are promoted to `fit` when `frame_level` is on.
  Only a genuinely tight framing overrides it.

Watch for trailing spaces in phrase tables. `contains_phrase` needs a
non-alphanumeric character after the match, so a phrase stored with a trailing
space never matches at end of string. That cost an hour.

### Style carried across cuts

`plan.apply_look_continuity(plan)` copies the look scalars in `LOOK_SCALARS`
forward from one segment to the next, so a style typed once applies to every
later cut. A **named style kit** resets the look wholesale, which is how you
change looks mid-plan. Called from both `grammar.build_shot_plan` and
`parse_plan`, so it applies to grammar and LLM plans alike. `SYSTEM_PROMPT` tells
the model about it.

### The Movie Render Queue crash

Symptom: the render finishes, then the editor dies with "Pure virtual function
being called", preceded by `ensure(UE::GetPlayInEditorID() == INDEX_NONE)`.

Cause: MRQ renders in a PIE world. With the same sequence still open in Sequencer,
the editor's own Sequencer keeps evaluating its bindings while that PIE world is
torn down, and the vtable is already gone. Three fixes, all in place:

1. `render._close_sequencer()` shuts the Sequencer window before the job starts.
   The asset is untouched and reopens from the Content Browser.
2. `face_baker` starts its audio section at frame **-1**. MRQ evaluates from frame
   -1 when temporal sub-sampling is on and warns that a section starting at 0
   cannot be auto-expanded to cover it.
3. `face_baker` saves the imported audio package to disk. An unsaved package can
   be collected while a Level Sequence still points at it, and PIE teardown is
   exactly when that happens.

### Audio actually reaching the file

`MoviePipelineWaveOutput` is what makes Sequencer audio land on disk. It was only
being added on the MP4 path, so every PNG render was silent with nothing to
recover. `render._add_wave_output()` is now called from both paths.

For folders rendered before that fix, `encode_folder` has a fallback: if there is
no wav beside the frames it reads the audio section off the sequence, follows the
SoundWave back to the file it was imported from, and lines it up using the section
start, the section's `start_frame_offset` in tick resolution, and the first frame
number in the folder. A wav beside the frames always wins over the fallback.

## Verified by headless test

Run tests like this and grep the output for `CDTEST|`:

```bash
"C:/ODK/Installs/1/output/Windows/Engine/Binaries/Win64/UnrealEditor-Cmd.exe" "C:/Users/Round/Documents/Unreal Projects/ImmaDegenTest/ImmaDegenTest.uproject" -run=pythonscript -script="<script.py>" -unattended -nopause -nosplash -nullrhi -NoLiveCoding -stdout -FullStdOutLogOutput
```

Exit code 3 with `DONE` printed is fine: it is an M2Http leak-sentinel ensure at
shutdown, unrelated to the script. An uncaught Python exception aborts the whole
script silently, so if output stops mid-way without `DONE`, that is why.

- **Shots**: authoring, idempotent re-runs (40 keys both runs), 12 keys per shot
  after the key-density fix (was 726).
- **Lighting**: idempotent, physical-sky mode detected, base sun intensity stored
  in the `CineDirectorBaseIntensity` actor tag.
- **Render**: all five formats queue; ffmpeg found at the winget Gyan.FFmpeg path.
- **Face analyser**: 29 of 29 slots mapped on `SK_Andor_Face_ARKit_01`, 30 rival
  targets dropped across 8 slots.
- **Lipsync**: band-to-shape mapping validated against a synthetic wav with known
  per-second spectra.
- **Face baker**: audio + emotion arc, emotion only, procedural talking,
  articulation extremes, idempotency (25 curves / 2250 keys on both runs),
  additive + ref-pose confirmed, and a morph-free mesh refused with a clear message.
- **Sequencer attachment**: animation bound to the face *component*, audio track,
  playback range extended, and a second run replaces rather than stacks.
- **LLM**: all six backend request bodies built; the key never appears in a URL or
  body; missing model and missing key reported clearly; and against a local stub
  server, the Claude, OpenAI and Gemini reply envelopes all parsed, plus
  truncation, refusal, API-error, non-JSON, HTTP 500, HTTP 401 and dead-port
  paths, plus grammar fallback on and off.
- **Fit and level framing**: fit distance against known radii, backdrop rejection,
  and "wide shot of everything" resolving to `fit` with `frame_level` set.
- **Look continuity**: scalars carried forward across cuts, a named style kit
  resetting them.
- **Encode audio**: wav beside the frames wins; with no wav the sequence fallback
  found the source mp3 and seeked correctly (10.0 s for a folder starting at frame
  300 on a 30 fps sequence); with no sequence at all it still encodes, silent.

## Bugs found by those tests and fixed

1. **Lipsync gated an entire clip shut** when the audio had no pauses: the
   20th-percentile noise floor sat at the speech level itself and
   `noise_floor * 2.5` exceeded every frame. Guarded in `lipsync.analyze`; clips
   that do contain silence are bit-identical to before. The baker now says so in
   its notes when the mouth never moves.
2. **Unknown enum tokens were silently flattened**, turning a requested dolly into
   a locked-off shot. `plan._token` takes a synonym table (`push_in` → `dolly_in`,
   `birds_eye` → `overhead`) and notes anything still unrecognised.
3. **"left" matched the actor `INTERACT_Simulation_Left`**, beating pronoun
   carry-over. `grammar.RESERVED_LABEL_WORDS` filters single-word label suffixes.

## What is left

1. **A real LLM request has never been sent.** Everything was tested against a
   local stub, deliberately. Put a key in `ANTHROPIC_API_KEY` or
   `Saved/CineDirector/Anthropic.key`, set the backend in Settings, and try one
   prompt with "Log request and reply bodies" on.
2. **The request blocks the game thread** while a slow-task dialog pumps Slate.
   The HTTP runs on a worker thread and the user can cancel, but the editor is not
   fully interactive during the wait. A post-tick callback would fix it properly.
3. **The Quality preset dropdown from 5.8 is not in the render dialog.** 5.8
   offered 1 / 4 / 8 / 16 temporal samples with spatial pinned at 1. Here the two
   counts are typed in by hand, which works but is easy to get wrong.
4. **A later shot with no subject and no pronoun does not inherit one.** The look
   carries forward now; the subject does not. Same mechanism would do it.
5. **Decide whether trailer mode is wanted.** Mentioned once when scope was agreed;
   `CineTrailerProcessor.cpp` in the 5.8 tree is the source.
6. **Keep the shared vocabulary in step with the 5.8 repo.** Synonyms, style kits
   and the shot-plan schema exist in both trees and have already drifted once.
