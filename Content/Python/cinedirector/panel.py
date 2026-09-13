# Copyright Roundtree. All Rights Reserved.
"""
The in-editor panel.

Python cannot build Slate widgets, so the panel is a details view over a
Python-declared UObject. Unreal renders the uproperties as a real properties
panel with text boxes, checkboxes, sliders and asset pickers, and returns true
when the user presses OK. Menu entries under Tools open each section.
"""

import traceback

import unreal

from . import executor as cd_executor
from . import face as cd_face
from . import face_baker
from . import grammar
from . import llm
from . import plan as cd_plan
from . import render as cd_render
from . import scene as cd_scene
from . import settings as cd_settings

MENU_PATH = "LevelEditor.MainMenu.Tools"
SECTION_NAME = "CineDirector"
_LAST_REQUEST = {}


# ---------------------------------------------------------------------------
# Request objects. Declared lazily so importing this module outside the editor,
# or twice, does not re-register a uclass.
# ---------------------------------------------------------------------------

@unreal.uclass()
class CineDirectorShotRequest(unreal.Object):
    """Describe a shot in plain language; CineDirector builds the coverage."""

    description = unreal.uproperty(
        str, meta=dict(
            DisplayName="Shot description",
            MultiLine="true",
            ToolTip="For example: slow close-up push in on wyn943, shallow focus, "
                    "then orbit them from the left."))

    one_continuous_shot = unreal.uproperty(
        bool, meta=dict(
            DisplayName="One continuous take",
            ToolTip="Chain every move onto a single camera with no cuts. Saying "
                    "\"one take\" or \"oner\" in the description does the same."))

    create_camera_cuts = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Create camera cuts",
            ToolTip="Add a camera cut section per shot so the sequence plays "
                    "shot to shot."))

    target_sequence = unreal.uproperty(
        unreal.LevelSequence, meta=dict(
            DisplayName="Level Sequence",
            ToolTip="Leave empty to use the sequence currently open in Sequencer."))

    preview_only = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Preview only (do not author)",
            ToolTip="Interpret the description and log the shot plan without "
                    "spawning cameras or writing keys."))


@unreal.uclass()
class CineDirectorRenderRequest(unreal.Object):
    """Render the open Level Sequence through Movie Render Queue."""

    resolution_width = unreal.uproperty(
        int, meta=dict(DisplayName="Width", ClampMin="2", UIMin="640", UIMax="7680"))

    resolution_height = unreal.uproperty(
        int, meta=dict(DisplayName="Height", ClampMin="2", UIMin="360", UIMax="4320"))

    output_format = unreal.uproperty(
        str, meta=dict(
            DisplayName="Format",
            ToolTip="png, jpeg, exr, bmp or mp4. MP4 needs ffmpeg installed."))

    encode_quality = unreal.uproperty(
        int, meta=dict(
            DisplayName="MP4 quality (0-3)",
            ClampMin="0", ClampMax="3",
            ToolTip="0 low, 1 medium, 2 high, 3 epic. MP4 only."))

    temporal_samples = unreal.uproperty(
        int, meta=dict(
            DisplayName="Temporal samples",
            ClampMin="1", ClampMax="64",
            ToolTip="Sub-frame accumulation. Above 1 gives true motion blur and "
                    "costs render time proportionally."))

    spatial_samples = unreal.uproperty(
        int, meta=dict(DisplayName="Spatial samples", ClampMin="1", ClampMax="16"))

    output_directory = unreal.uproperty(
        unreal.DirectoryPath, meta=dict(
            DisplayName="Output folder",
            ToolTip="Leave empty for the project's Saved/MovieRenders."))

    target_sequence = unreal.uproperty(
        unreal.LevelSequence, meta=dict(
            DisplayName="Level Sequence",
            ToolTip="Leave empty to use the sequence open in Sequencer."))


@unreal.uenum()
class CineDirectorBackend(unreal.EnumBase):
    """Which planner interprets the description. Order matches _BACKEND_ORDER."""

    OFFLINE_GRAMMAR = unreal.uvalue(0)
    CLAUDE = unreal.uvalue(1)
    OPENAI = unreal.uvalue(2)
    OPENROUTER = unreal.uvalue(3)
    LOCAL_MODEL = unreal.uvalue(4)
    GEMINI = unreal.uvalue(5)
    CUSTOM_OPENAI_COMPATIBLE = unreal.uvalue(6)


_BACKEND_ORDER = (cd_settings.OFFLINE, cd_settings.ANTHROPIC, cd_settings.OPENAI,
                  cd_settings.OPENROUTER, cd_settings.LOCAL, cd_settings.GEMINI,
                  cd_settings.CUSTOM)


@unreal.uenum()
class CineDirectorEffort(unreal.EnumBase):
    """Reasoning depth, for the backends that expose one."""

    LOW = unreal.uvalue(0)
    MEDIUM = unreal.uvalue(1)
    HIGH = unreal.uvalue(2)


_EFFORT_ORDER = ("low", "medium", "high")


@unreal.uclass()
class CineDirectorSettingsRequest(unreal.Object):
    """Which model plans the shots, and how to reach it."""

    backend = unreal.uproperty(
        CineDirectorBackend, meta=dict(
            DisplayName="Backend",
            ToolTip="Offline grammar needs no key and never leaves the machine."))

    model = unreal.uproperty(
        str, meta=dict(
            DisplayName="Model",
            ToolTip="Spelled as the backend spells it: claude-opus-5, gpt-4o, "
                    "anthropic/claude-sonnet-4.5 on OpenRouter, llama3.1 on "
                    "Ollama. Empty uses the backend's default where one exists."))

    endpoint_override = unreal.uproperty(
        str, meta=dict(
            DisplayName="Endpoint URL override",
            ToolTip="Empty uses the backend's default. Set this for a self-hosted "
                    "model, a proxy, or a non-default Ollama port."))

    api_key = unreal.uproperty(
        str, meta=dict(
            DisplayName="API key (optional, prefer an environment variable)",
            PasswordField="true",
            ToolTip="Left empty, the key is read from the backend's environment "
                    "variable, then from Saved/CineDirector/<Backend>.key."))

    effort = unreal.uproperty(
        CineDirectorEffort, meta=dict(DisplayName="Reasoning effort"))

    max_output_tokens = unreal.uproperty(
        int, meta=dict(
            DisplayName="Max output tokens",
            ClampMin="1024", ClampMax="64000",
            ToolTip="On thinking models this ceiling covers the reasoning too."))

    timeout_seconds = unreal.uproperty(
        float, meta=dict(DisplayName="Timeout (seconds)",
                         ClampMin="5.0", ClampMax="600.0"))

    fall_back_to_grammar = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Fall back to the offline parser on failure",
            ToolTip="On a failure the built-in parser answers instead, and says "
                    "so in the shot notes."))

    send_scene_actors = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Send scene actor names",
            ToolTip="Lets the model pick subjects by name. Off means shots can "
                    "only be framed from the viewport."))

    max_scene_actors = unreal.uproperty(
        int, meta=dict(DisplayName="Max actors sent",
                       ClampMin="1", ClampMax="500"))

    log_traffic = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Log request and reply bodies",
            ToolTip="Written to the output log. Keys are never logged."))


@unreal.uclass()
class CineDirectorFaceRequest(unreal.Object):
    """Drive a character's face from audio, an emotion, or both."""

    character = unreal.uproperty(
        unreal.Actor, meta=dict(
            DisplayName="Character",
            ToolTip="Leave empty to use the actor selected in the level."))

    face_mesh = unreal.uproperty(
        unreal.SkeletalMesh, meta=dict(
            DisplayName="Face mesh (optional)",
            ToolTip="Leave empty to use the character's mesh with the most "
                    "blendshapes, which is normally the face."))

    audio_file = unreal.uproperty(
        unreal.FilePath, meta=dict(
            DisplayName="Dialogue audio",
            FilePathFilter="Audio files (*.wav;*.mp3;*.ogg;*.flac)|"
                           "*.wav;*.mp3;*.ogg;*.flac",
            ToolTip="A wav, mp3, ogg or flac. Anything but wav needs ffmpeg. "
                    "Leave empty for an emotion-only or silent-talking bake."))

    talking = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Talk without audio",
            ToolTip="Move the mouth procedurally when there is no dialogue file."))

    emotion = unreal.uproperty(
        str, meta=dict(
            DisplayName="Emotion",
            ToolTip="For example: angry. Or an arc: nervous then angry. "
                    "Understood: scared, angry, happy, sad, surprised, "
                    "disgusted, pain, suspicious, nervous, calm, neutral."))

    emotion_strength = unreal.uproperty(
        float, meta=dict(
            DisplayName="Emotion strength", ClampMin="0.0", ClampMax="2.0",
            UIMin="0.0", UIMax="2.0"))

    duration_seconds = unreal.uproperty(
        float, meta=dict(
            DisplayName="Duration (seconds)", ClampMin="0.1", UIMax="30.0",
            ToolTip="Used when there is no audio. Audio sets its own length."))

    mouth_strength = unreal.uproperty(
        float, meta=dict(
            DisplayName="Mouth strength", ClampMin="0.0", ClampMax="2.0",
            UIMin="0.0", UIMax="2.0"))

    articulation = unreal.uproperty(
        float, meta=dict(
            DisplayName="Articulation", ClampMin="0.0", ClampMax="2.0",
            UIMin="0.0", UIMax="2.0",
            ToolTip="1 is as analysed. Above 1 sharpens consonants, below 1 "
                    "softens into a mumble."))

    auto_blinks = unreal.uproperty(
        bool, meta=dict(DisplayName="Automatic blinks"))

    add_to_sequence = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Add to Level Sequence",
            ToolTip="Put the baked animation, and the dialogue audio, on the "
                    "open sequence."))

    target_sequence = unreal.uproperty(
        unreal.LevelSequence, meta=dict(
            DisplayName="Level Sequence",
            ToolTip="Leave empty to use the sequence open in Sequencer."))

    keep_as_new_take = unreal.uproperty(
        bool, meta=dict(
            DisplayName="Keep as a new take",
            ToolTip="Off overwrites one asset per character, which is normally "
                    "what you want. On timestamps every bake."))


def _defaults(request):
    """Restore the last values so the dialog does not reset between uses."""
    request.set_editor_property("description", _LAST_REQUEST.get("description", ""))
    request.set_editor_property("one_continuous_shot",
                                _LAST_REQUEST.get("one_continuous_shot", False))
    request.set_editor_property("create_camera_cuts",
                                _LAST_REQUEST.get("create_camera_cuts", True))
    request.set_editor_property("preview_only",
                                _LAST_REQUEST.get("preview_only", False))
    sequence = _LAST_REQUEST.get("target_sequence")
    if sequence is not None:
        try:
            request.set_editor_property("target_sequence", sequence)
        except Exception:
            pass


def _remember(request):
    for key in ("description", "one_continuous_shot", "create_camera_cuts",
                "preview_only", "target_sequence"):
        try:
            _LAST_REQUEST[key] = request.get_editor_property(key)
        except Exception:
            pass


def _message(title, text):
    try:
        unreal.EditorDialog.show_message(title, text, unreal.AppMsgType.OK)
    except Exception:
        unreal.log_warning("%s: %s" % (title, text))


def _show_details(title, request):
    """Modal details view. True when the user pressed OK."""
    options = unreal.EditorDialogLibraryObjectDetailsViewOptions()
    try:
        options.set_editor_property("show_object_name", False)
        options.set_editor_property("allow_search", False)
        options.set_editor_property("min_width", 560)
        options.set_editor_property("min_height", 320)
    except Exception:
        pass
    return unreal.EditorDialog.show_object_details_view(title, request, options)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def open_shots_dialog():
    """Tools > CineDirector > Shots. The main entry point."""
    try:
        request = CineDirectorShotRequest()
        _defaults(request)

        if not _show_details("CineDirector - Shots", request):
            return
        _remember(request)

        description = request.get_editor_property("description") or ""
        if not description.strip():
            _message("CineDirector", "Type a shot description first.")
            return

        scene = cd_scene.build_scene_context()
        unreal.log("CineDirector: %d actors in scene context." % len(scene.actors))

        try:
            plan, notes = llm.build_shot_plan(description, scene)
        except (llm.LlmError, grammar.GrammarError) as error:
            _message("CineDirector", str(error))
            return
        for note in notes:
            unreal.log("CineDirector: " + note)

        if request.get_editor_property("one_continuous_shot"):
            plan.one_continuous_shot = True
        plan.create_camera_cuts = bool(request.get_editor_property("create_camera_cuts"))

        summary = cd_plan.describe_plan(plan)
        unreal.log("CineDirector plan:\n" + summary)

        if request.get_editor_property("preview_only"):
            _message("CineDirector - Plan preview", summary)
            return

        sequence = request.get_editor_property("target_sequence")
        result = cd_executor.execute(plan, scene=scene, sequence=sequence)

        if not result.success:
            _message("CineDirector", result.error)
            return

        unreal.log("CineDirector: " + result.describe())
        _message("CineDirector", result.describe())

    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector failed:\n" + trace)
        _message("CineDirector - Error", trace)


_LAST_RENDER = {}


def open_render_dialog():
    """Tools > CineDirector > Render."""
    try:
        request = CineDirectorRenderRequest()
        request.set_editor_property("resolution_width",
                                    _LAST_RENDER.get("resolution_width", 1920))
        request.set_editor_property("resolution_height",
                                   _LAST_RENDER.get("resolution_height", 1080))
        request.set_editor_property("output_format",
                                   _LAST_RENDER.get("output_format", cd_render.PNG))
        request.set_editor_property("encode_quality",
                                   _LAST_RENDER.get("encode_quality", cd_render.QUALITY_HIGH))
        request.set_editor_property("temporal_samples",
                                   _LAST_RENDER.get("temporal_samples", 1))
        request.set_editor_property("spatial_samples",
                                   _LAST_RENDER.get("spatial_samples", 1))
        saved_directory = _LAST_RENDER.get("output_directory")
        if saved_directory is not None:
            request.set_editor_property("output_directory", saved_directory)

        if not _show_details("CineDirector - Render", request):
            return

        for key in ("resolution_width", "resolution_height", "output_format",
                    "encode_quality", "temporal_samples", "spatial_samples",
                    "output_directory"):
            try:
                _LAST_RENDER[key] = request.get_editor_property(key)
            except Exception:
                pass

        options = cd_render.RenderOptions()
        options.width = int(request.get_editor_property("resolution_width") or 1920)
        options.height = int(request.get_editor_property("resolution_height") or 1080)
        fmt = str(request.get_editor_property("output_format") or "").strip().lower()
        options.format = fmt if fmt in cd_render.FORMATS else cd_render.PNG
        if fmt and fmt not in cd_render.FORMATS:
            unreal.log_warning("CineDirector: unknown format '%s'; rendering PNG." % fmt)
        options.encode_quality = int(request.get_editor_property("encode_quality") or 2)
        options.temporal_samples = int(request.get_editor_property("temporal_samples") or 1)
        options.spatial_samples = int(request.get_editor_property("spatial_samples") or 1)

        directory = request.get_editor_property("output_directory")
        if directory is not None:
            options.output_directory = str(directory.get_editor_property("path") or "")

        sequence = request.get_editor_property("target_sequence")

        try:
            notes = cd_render.start_render(options, sequence=sequence)
        except cd_render.RenderError as error:
            _message("CineDirector - Render", str(error))
            return

        _message("CineDirector - Render", "\n".join(notes))

    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector render failed:\n" + trace)
        _message("CineDirector - Error", trace)


def open_dock_panel():
    """Tools > CineDirector > Panel. Imported late to avoid an import cycle."""
    from . import dock_panel
    dock_panel.open_panel()


def _enum_index(value):
    """The integer behind an EnumBase value, whatever shape Python hands back."""
    for attribute in ("value",):
        if hasattr(value, attribute):
            try:
                return int(getattr(value, attribute))
            except Exception:
                pass
    try:
        return int(value)
    except Exception:
        return 0


def open_settings_dialog():
    """Tools > CineDirector > Settings."""
    try:
        values = cd_settings.load(force=True)
        request = CineDirectorSettingsRequest()

        name = cd_settings.backend(values)
        index = _BACKEND_ORDER.index(name) if name in _BACKEND_ORDER else 0
        effort = str(values.get("effort") or "low").lower()
        effort_index = (_EFFORT_ORDER.index(effort)
                        if effort in _EFFORT_ORDER else 0)

        for key, value in (
                ("backend", CineDirectorBackend.cast(index)),
                ("model", str(values.get("model") or "")),
                ("endpoint_override", str(values.get("endpoint_override") or "")),
                ("api_key", str(values.get("api_key") or "")),
                ("effort", CineDirectorEffort.cast(effort_index)),
                ("max_output_tokens", int(values.get("max_output_tokens", 8000))),
                ("timeout_seconds", float(values.get("timeout_seconds", 120.0))),
                ("fall_back_to_grammar",
                 bool(values.get("fall_back_to_grammar", True))),
                ("send_scene_actors", bool(values.get("send_scene_actors", True))),
                ("max_scene_actors", int(values.get("max_scene_actors", 60))),
                ("log_traffic", bool(values.get("log_traffic", False)))):
            try:
                request.set_editor_property(key, value)
            except Exception as error:              # noqa: BLE001
                unreal.log_warning("CineDirector: could not show setting '%s' (%s)."
                                   % (key, error))

        if not _show_details("CineDirector - Settings", request):
            return

        chosen = _enum_index(request.get_editor_property("backend"))
        chosen_effort = _enum_index(request.get_editor_property("effort"))

        updated = {
            "backend": _BACKEND_ORDER[chosen % len(_BACKEND_ORDER)],
            "model": str(request.get_editor_property("model") or "").strip(),
            "endpoint_override": str(
                request.get_editor_property("endpoint_override") or "").strip(),
            "api_key": str(request.get_editor_property("api_key") or "").strip(),
            "effort": _EFFORT_ORDER[chosen_effort % len(_EFFORT_ORDER)],
            "max_output_tokens": int(
                request.get_editor_property("max_output_tokens") or 8000),
            "timeout_seconds": float(
                request.get_editor_property("timeout_seconds") or 120.0),
            "fall_back_to_grammar": bool(
                request.get_editor_property("fall_back_to_grammar")),
            "send_scene_actors": bool(
                request.get_editor_property("send_scene_actors")),
            "max_scene_actors": int(
                request.get_editor_property("max_scene_actors") or 60),
            "log_traffic": bool(request.get_editor_property("log_traffic")),
        }

        try:
            written = cd_settings.save(updated)
        except (IOError, OSError) as error:
            _message("CineDirector", "Could not write the settings file: %s" % error)
            return

        lines = ["Planner: %s." % llm.provider_name(updated)]
        chosen_name = updated["backend"]
        if chosen_name != cd_settings.OFFLINE:
            endpoint = cd_settings.resolve_endpoint(updated)
            lines.append("Endpoint: %s" % (endpoint or "(not set)"))
            if not cd_settings.resolve_model(updated):
                lines.append("No model id set, so requests will fail. Fill in Model.")
            if not cd_settings.resolve_key(updated) \
                    and chosen_name not in cd_settings.KEY_OPTIONAL:
                lines.append("No API key found. "
                             + cd_settings.describe_key_sources(updated))
            else:
                lines.append("API key: found.")
        lines.append("")
        lines.append("Saved to %s" % written)

        _message("CineDirector - Settings", "\n".join(lines))

    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector settings failed:\n" + trace)
        _message("CineDirector - Error", trace)


_LAST_FACE = {}

_FACE_KEYS = ("talking", "emotion", "emotion_strength", "duration_seconds",
              "mouth_strength", "articulation", "auto_blinks", "add_to_sequence",
              "keep_as_new_take", "character", "face_mesh", "target_sequence",
              "audio_file")

_FACE_DEFAULTS = {
    "talking": False,
    "emotion": "",
    "emotion_strength": 1.0,
    "duration_seconds": 4.0,
    "mouth_strength": 1.0,
    "articulation": 1.0,
    "auto_blinks": True,
    "add_to_sequence": True,
    "keep_as_new_take": False,
}


def _selected_actor():
    try:
        selected = unreal.get_editor_subsystem(
            unreal.EditorActorSubsystem).get_selected_level_actors()
        return selected[0] if selected else None
    except Exception:
        return None


def open_face_dialog():
    """Tools > CineDirector > Face and lipsync."""
    try:
        request = CineDirectorFaceRequest()
        for key, value in _FACE_DEFAULTS.items():
            try:
                request.set_editor_property(key, _LAST_FACE.get(key, value))
            except Exception:
                pass
        for key in ("character", "face_mesh", "target_sequence", "audio_file"):
            saved = _LAST_FACE.get(key)
            if saved is not None:
                try:
                    request.set_editor_property(key, saved)
                except Exception:
                    pass
        if _LAST_FACE.get("character") is None:
            actor = _selected_actor()
            if actor is not None:
                try:
                    request.set_editor_property("character", actor)
                except Exception:
                    pass

        if not _show_details("CineDirector - Face and lipsync", request):
            return
        for key in _FACE_KEYS:
            try:
                _LAST_FACE[key] = request.get_editor_property(key)
            except Exception:
                pass

        actor = request.get_editor_property("character") or _selected_actor()
        if actor is None:
            _message("CineDirector", "Select a character in the level first, or "
                                     "pick one in the Character field.")
            return

        mesh = request.get_editor_property("face_mesh")
        if mesh is None:
            mesh, _component = face_baker.pick_face_mesh(actor)
        if mesh is None:
            _message("CineDirector",
                     "'%s' has no skeletal mesh component, so it has no face to "
                     "drive." % actor.get_actor_label())
            return

        audio = request.get_editor_property("audio_file")
        audio_path = ""
        if audio is not None:
            audio_path = str(audio.get_editor_property("file_path") or "").strip()

        bake_request = face_baker.BakeRequest()
        bake_request.mesh = mesh
        bake_request.actor = actor
        bake_request.audio_path = audio_path
        bake_request.talking = bool(request.get_editor_property("talking"))
        bake_request.emotion_text = str(request.get_editor_property("emotion") or "")
        bake_request.emotion_strength = float(
            request.get_editor_property("emotion_strength") or 1.0)
        bake_request.duration_seconds = float(
            request.get_editor_property("duration_seconds") or 4.0)
        bake_request.mouth_strength = float(
            request.get_editor_property("mouth_strength") or 1.0)
        bake_request.articulation = float(
            request.get_editor_property("articulation") or 1.0)
        bake_request.auto_blinks = bool(request.get_editor_property("auto_blinks"))
        bake_request.keep_as_new_take = bool(
            request.get_editor_property("keep_as_new_take"))

        if not audio_path and not bake_request.talking \
                and not bake_request.emotion_text.strip():
            _message("CineDirector",
                     "Nothing to bake. Give a dialogue file, tick \"Talk without "
                     "audio\", or name an emotion.")
            return

        with unreal.ScopedEditorTransaction("CineDirector: bake face"):
            try:
                result = face_baker.bake(bake_request)
            except face_baker.BakeError as error:
                _message("CineDirector - Face", str(error))
                return
            except Exception as error:                  # noqa: BLE001
                unreal.log_error("CineDirector face bake failed:\n"
                                 + traceback.format_exc())
                _message("CineDirector - Face", str(error))
                return

            if request.get_editor_property("add_to_sequence"):
                face_baker.attach_to_sequence(
                    result, actor, mesh,
                    sequence=request.get_editor_property("target_sequence"),
                    fps=bake_request.fps)

        text = result.describe()
        unreal.log("CineDirector face:\n" + text)
        _message("CineDirector - Face", text)

    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector face failed:\n" + trace)
        _message("CineDirector - Error", trace)


def open_face_report():
    """Tools > CineDirector > Face report. What the selected character can do."""
    actor = _selected_actor()
    if actor is None:
        _message("CineDirector", "Select a character in the level first.")
        return
    mesh, component = face_baker.pick_face_mesh(actor)
    if mesh is None:
        _message("CineDirector", "'%s' has no skeletal mesh component."
                 % actor.get_actor_label())
        return
    profile = cd_face.analyze(mesh)
    text = "%s -> %s\n\n%s" % (actor.get_actor_label(),
                               component.get_name() if component else mesh.get_name(),
                               profile.describe())
    unreal.log("CineDirector face report:\n" + text)
    _message("CineDirector - Face report", text)


def open_vocabulary_help():
    """Tools > CineDirector > Vocabulary. What words the parser understands."""
    text = "\n".join([
        "Moves: orbit / circle, push in / dolly in, pull back, crane up, crane down,",
        "  truck left, truck right, pan left, pan right, tilt up, tilt down,",
        "  zoom in, zoom out, flyover / drone / aerial, static / locked off.",
        "",
        "Framing: extreme close-up, close-up, slight close-up, medium close-up,",
        "  medium, wide, extreme wide / establishing.",
        "",
        "Scale: wide and extreme wide make the subject SMALL in frame, which suits",
        "  a person. To see all of something big, say 'the whole', 'all of',",
        "  'fit ... in frame' or 'top to bottom' and the camera sits exactly far",
        "  enough back for it to fill the frame.",
        "  'everything', 'the terrain', 'the whole map' frame the entire level.",
        "",
        "Angles: low angle, high angle, overhead / bird's eye.",
        "Sides: from the left, from behind, its left, their back, over the shoulder.",
        "",
        "Lens: 85mm, wide-angle, telephoto, portrait. Aperture: f/1.4, shallow focus,",
        "  deep focus, fixed focus, rack focus from <actor> to <actor>.",
        "",
        "Feel: handheld (slight / very), dutch angle, film grain, vignette,",
        "  chromatic aberration, bloom, lens flare.",
        "",
        "Style kits: bodycam, cctv, found footage, CRT / VHS, Nolan / IMAX, horror,",
        "  action, cinematic, noir, thriller, romance, cyberpunk, documentary,",
        "  music video, western, indie.",
        "  A style holds for every shot after it, so name it once. Naming a",
        "  different kit later replaces it rather than stacking. Grain, vignette,",
        "  handheld and the grade carry the same way.",
        "",
        "Light: dawn, morning, noon, afternoon, golden hour, sunset, dusk, night,",
        "  midnight, overcast, fog / mist / haze (thick or light), god rays, volumetric.",
        "",
        "Timing: 6 seconds, slow, quickly, linear / constant speed.",
        "Cuts: 'then', 'cut to', 'next', or a full stop starts a new shot.",
        "One camera: say 'one take', 'oner' or 'continuous'.",
        "Pronouns: 'orbit the mask, then push in on it' carries the subject over.",
    ])
    _message("CineDirector - Vocabulary", text)


def open_scene_report():
    """Tools > CineDirector > Scene report. What the parser can see and name."""
    scene = cd_scene.build_scene_context()
    lines = ["%d framable actors. Nearest the viewport first."
             % len(scene.actors), ""]

    from . import vec
    eye = scene.viewport_location
    actors = sorted(scene.actors, key=lambda i: vec.dist(i.location, eye))

    for info in actors[:40]:
        head = cd_scene.find_head_world_location(info.actor)
        lines.append("%-34s r=%-7.0f yaw=%-7.1f %s"
                     % (info.label[:34], info.bounds_radius, info.facing[1],
                        "face" if head else ""))
    if len(actors) > 40:
        lines.append("... and %d more." % (len(actors) - 40))

    text = "\n".join(lines)
    unreal.log("CineDirector scene report:\n" + text)
    _message("CineDirector - Scene", text)


# ---------------------------------------------------------------------------
# Menu registration
# ---------------------------------------------------------------------------

_ENTRIES = (
    ("CineDirectorPanel", "Panel",
     "The dockable CineDirector panel: prompt, examples, vocabulary, face.",
     "open_dock_panel"),
    ("CineDirectorShots", "Shots...",
     "Describe a shot and author cine cameras, keys and cuts.",
     "open_shots_dialog"),
    ("CineDirectorFace", "Face and lipsync...",
     "Drive a character's face from dialogue audio and an emotion.",
     "open_face_dialog"),
    ("CineDirectorRender", "Render...",
     "Render the open Level Sequence through Movie Render Queue.",
     "open_render_dialog"),
    ("CineDirectorScene", "Scene report",
     "List the actors CineDirector can see and name.",
     "open_scene_report"),
    ("CineDirectorFaceReport", "Face report",
     "What facial blendshapes the selected character has.",
     "open_face_report"),
    ("CineDirectorSettings", "Settings...",
     "Which model plans the shots, and how to reach it.",
     "open_settings_dialog"),
    ("CineDirectorVocabulary", "Vocabulary",
     "The words the built-in parser understands.",
     "open_vocabulary_help"),
)


def register_menus():
    """Add a CineDirector section to the Tools menu. Safe to call twice."""
    menus = unreal.ToolMenus.get()
    tools = menus.find_menu(MENU_PATH)
    if tools is None:
        unreal.log_warning("CineDirector: could not find %s; menu not registered."
                           % MENU_PATH)
        return False

    tools.add_section(SECTION_NAME, "CineDirector")

    for name, label, tooltip, function in _ENTRIES:
        entry = unreal.ToolMenuEntry(
            name=name,
            type=unreal.MultiBlockType.MENU_ENTRY)
        entry.set_label(label)
        entry.set_tool_tip(tooltip)
        entry.set_string_command(
            unreal.ToolMenuStringCommandType.PYTHON, "",
            "import cinedirector.panel as p; p.%s()" % function)
        tools.add_menu_entry(SECTION_NAME, entry)

    menus.refresh_all_widgets()
    unreal.log("CineDirector: menus registered under Tools.")
    return True
