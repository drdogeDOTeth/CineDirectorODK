# CineDirector — Otherside ODK edition

**This is the Otherside ODK build of CineDirector.** It is a pure-Python plugin
for the ODK's Blueprint-only Unreal 5.5 engine, where no C++ module can be
compiled. The UE 5.8 C++ original lives at
[drdogeDOTeth/CineDirector](https://github.com/drdogeDOTeth/CineDirector).
Both speak the same shot-plan format, so the same prompt produces the same plan
on either engine, but they share no code and neither can be installed into the
other's engine.

Describe a shot in plain language. CineDirector builds the cine cameras,
keyframes, focus and camera cuts in your open Level Sequence, keys the lighting
and fog to match, drives the character's face from a dialogue clip, and renders
the result through Movie Render Queue.

> Developers: read `HANDOFF.md` instead. This file is about using the plugin.

## Install

The plugin is content-only, so there is nothing to compile. Install it once into
the engine and every ODK project on the machine picks it up:

```powershell
.\Tools\Install-EnginePlugin.ps1
```

That links this folder into the ODK engine's `Engine/Plugins/Marketplace`, so a
`git pull` here updates every project at once. Restart the editor afterwards.
Pass `-EngineRoot` if your ODK is not at `C:\ODK\Installs\1\output\Windows`,
`-Copy` to copy instead of link, and `-Uninstall` to remove it.

To install into a single project instead, copy this folder into that project's
`Plugins` folder. Do not do both: two copies of the same plugin name is a startup
error.

Either way the menus appear under **Tools > CineDirector** after a restart. If
they are missing, open the Output Log and search for `CineDirector`; the startup
hook reports its own failures there.

**Do not add an `EngineVersion` field to the `.uplugin`.** The ODK build reports
no compatible changelist, so any stated version makes the engine skip the plugin
without telling you.

Nothing else is required, but MP4 output needs ffmpeg on the machine:

```bash
winget install Gyan.FFmpeg
```

## One-time panel setup

The dockable panel is an Editor Utility Widget that Python fills at runtime. This
engine build does not expose the widget tree to script, so the container it fills
has to be placed by hand, once:

1. Open **Tools > CineDirector > Panel**. The asset is created for you.
2. Open `Content/EUW_CineDirector` in the Content Browser.
3. Drag a **Scroll Box** from the Palette onto the empty graph. It becomes the
   root widget and fills the tab on its own, so there are no anchors to set.
4. Rename it to **Root** and tick **Is Variable**.
5. Compile, save, and open the panel again.

Always open the panel from the Tools menu. Pressing **Run Utility Widget** in the
Content Browser skips Python entirely and gives you an empty tab.

## The panel

**Shot description.** Type what you want. "Author shots" builds it; "Preview plan"
shows you the plan without touching the sequence. Two checkboxes control camera
cuts and whether the whole description is one continuous take. "Add selected
actor" drops the selected actor's name into the prompt so you do not have to type
it exactly.

**Example prompts.** Click one to load it. `<selected>` is replaced with whatever
you have selected in the level.

**Click to add.** Vocabulary chips, grouped by what they do. Clicking one appends
it to the prompt. These are the words the offline parser understands; an LLM
backend understands more, but these always work.

**Face and lipsync.** An emotion and a path to a dialogue clip. Bake face reads
the audio, writes visemes and an emotion arc onto a curves-only additive
AnimSequence, and binds it to the selected character's face component in the
sequence. "Talk without audio" gives procedural mouth movement with no clip.
"Face report" lists the blendshapes the selected character actually has.

**More.** A frames folder and "Frames to MP4", the render dialog, the scene
report, and settings.

**Status.** What the last action did, including any warnings about words it did
not recognise.

## Writing prompts

A prompt is one or more shots. `then` and `cut to` start a new shot.

```
slow push in on Andor, 85mm, shallow focus, then cut to a low angle close-up
```

Some things worth knowing:

- **A style applies to every later cut.** Type "film grain, handheld" once and it
  carries forward. Naming a different style kit ("noir", "bodycam") resets the
  look wholesale, which is how you change looks partway through.
- **Framing size and fitting are different questions.** "Extreme wide shot of the
  tower" makes the tower small in frame. "Fit the whole tower in frame", "show
  everything", or "the whole terrain" backs the camera up until all of it is
  visible. Sky spheres, atmosphere and other backdrops are ignored, so
  "everything" means the level, not the sky.
- **Pronouns carry over.** "Push in on Andor, then orbit him" works.
- **Duration** can be given per shot: "6 seconds", "slowly", "quickly".

Vocabulary, in full, is under **Tools > CineDirector > Vocabulary**.

## Settings

**Tools > CineDirector > Settings.** Stored as JSON in
`<Project>/Saved/CineDirector/settings.json`, which is outside source control in
every project template. That matters because it can hold an API key.

Backends: offline grammar, Claude, OpenAI, OpenRouter, a local model
(Ollama / LM Studio), Gemini, or any OpenAI-compatible server. The offline parser
needs nothing, and unless you turn the fallback off it also catches any request
that fails, so the plugin still works with no network. When that happens it says
so in the status line rather than quietly giving you a duller shot.

An API key is looked up in three places, in order: the settings file, the
environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`GEMINI_API_KEY` / `GOOGLE_API_KEY`, or `CINEDIRECTOR_API_KEY` for any of them),
then `<Project>/Saved/CineDirector/<Backend>.key`. The key is sent as a header
only, never in a URL or a request body.

## Rendering

**Tools > CineDirector > Render**, or the button in the panel. You pick
resolution, format (`png`, `jpeg`, `exr`, `bmp`, `mp4`), MP4 quality 0 to 3,
temporal and spatial sample counts, an output folder, and which sequence.

Temporal samples above 1 accumulate sub-frame renders into real motion blur, and
cost render time in proportion. 1 is draft. 4 is a good default. Spatial samples
are almost always best left at 1.

Three things to expect:

- **The render closes Sequencer first.** Movie Render Queue renders inside a
  play-in-editor session, and leaving the same sequence open in the editor crashes
  it during teardown. The asset is untouched and reopens from the Content Browser.
- **A long pause at the start is shader compilation, not a hang.** Check for
  `ShaderCompileWorker` processes and a ticking log before assuming otherwise.
- **Audio is written as a wav beside the frames** on an image-sequence render, and
  muxed directly into the file on an MP4 render.

### Frames to MP4

If you rendered an image sequence and want a video, press **Frames to MP4**. Leave
the frames field empty to use the last render of this session, or type any folder
into it. It encodes at 30 fps with a browser-safe profile and muxes the wav
sitting beside the frames.

If there is no wav, it falls back to the audio on the sequence itself, follows it
back to the file it was imported from, and lines it up against the first frame
number in the folder. That is what recovers sound from a render made before the
wav output existed. A wav beside the frames always wins.

## Troubleshooting

**The menu is not there.** The Tools menu search matches entry labels, not section
headings, so searching "CineDirector" finds nothing. Look under Tools directly.

**The panel opens empty.** Either the `Root` Scroll Box is missing, or you pressed
Run Utility Widget instead of using the Tools menu.

**A shot came out locked-off when you asked for a move.** The status line names any
word it could not place. Check it against the vocabulary list.

**A wide shot ended up inside the thing it was meant to show.** Ask it to fit
rather than to be wide: "fit the whole X in frame".

**The face never moves.** The bake reports this in its notes. Usually the mesh has
no morph targets; run Face report on the character to confirm.

**The MP4 is silent.** The render had no wave output pass, which is fixed for new
renders. For frames you already have, Frames to MP4 recovers the audio from the
sequence.
