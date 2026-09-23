# Copyright Roundtree. All Rights Reserved.
"""
The dockable CineDirector panel.

Python cannot build Slate, and this build exposes neither UWidgetTree nor
UserWidget.get_root_widget, so the widget tree cannot be authored into an asset
from script. What does work is building UMG widgets at runtime with new_object,
parenting them with add_child, and binding their delegates to Python callables.

So the panel is: a nearly empty Editor Utility Widget asset holding one named
container, and this module fills that container every time the tab is opened.
The asset is created by ensure_asset(); the one thing a human has to do once is
drop a Scroll Box into it named "Root" with Is Variable ticked, because that is
the handle Python reaches through.
"""

import traceback

import unreal

from . import executor as cd_executor
from . import face_baker
from . import grammar
from . import llm
from . import panel as cd_dialogs
from . import plan as cd_plan
from . import scene as cd_scene
from . import settings as cd_settings

ASSET_PATH = "/CineDirectorODK/EUW_CineDirector"
ASSET_NAME = "EUW_CineDirector"
ASSET_DIR = "/CineDirectorODK"
TAB_ID = "CineDirectorPanel"

# Property names tried when looking for the container to fill, in order.
ROOT_CANDIDATES = ("Root", "RootBox", "RootScroll", "CanvasPanel_0", "ScrollBox_0")

# Anything Python builds at runtime has to stay referenced from here, or the
# callbacks are collected and the buttons go dead a few seconds after opening.
_LIVE = {"widget": None, "root": None, "handlers": [], "fields": {}}
_ENCODE = {"state": None, "tick": None}


EXAMPLES = (
    "slow push in on {subject}, 85mm, shallow focus",
    "orbit {subject} from the left, then cut to a close-up",
    "low angle wide establishing shot, golden hour, god rays",
    "handheld medium shot over the shoulder of {subject}, 6 seconds",
    "crane up off {subject} to an extreme wide, one take",
    "bodycam chase through the corridor, dutch angle, film grain",
    "cctv high angle on {subject}, static, deep focus",
    "night, thick fog, slow pull back from {subject}",
)

VOCABULARY = (
    ("Moves", ("push in", "pull back", "orbit", "truck left", "truck right",
               "crane up", "crane down", "pan left", "pan right", "tilt up",
               "tilt down", "zoom in", "flyover", "static")),
    ("Framing", ("extreme close-up", "close-up", "medium close-up", "medium",
                 "wide", "extreme wide")),
    ("Scale", ("the whole", "fit in frame", "everything", "the terrain",
               "the landscape", "the whole area", "top to bottom")),
    ("Angle", ("low angle", "high angle", "overhead", "from the left",
               "from behind", "over the shoulder")),
    ("Lens", ("24mm", "35mm", "50mm", "85mm", "135mm", "shallow focus",
              "deep focus", "rack focus to")),
    ("Feel", ("handheld", "very handheld", "dutch angle", "film grain",
              "vignette", "bloom", "lens flare")),
    ("Style", ("bodycam", "cctv", "found footage", "VHS", "IMAX", "horror",
               "action", "cinematic", "noir", "thriller", "romance",
               "cyberpunk", "documentary", "music video", "western", "indie")),
    ("Light", ("dawn", "morning", "noon", "golden hour", "sunset", "dusk",
               "night", "midnight", "overcast", "fog", "thick fog", "god rays",
               "volumetric fog")),
    ("Timing", ("2 seconds", "6 seconds", "12 seconds", "slowly", "quickly",
                "one take", "then", "cut to")),
)


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

# UMG's stock button brush is near-white, and a white label on it is unreadable.
# Nothing here inherits the editor's dark theme, so the panel paints its own.
INK = (0.87, 0.89, 0.93, 1.0)          # default label
INK_DIM = (0.62, 0.66, 0.72, 1.0)      # secondary label
INK_HEAD = (0.55, 0.72, 0.98, 1.0)     # section heading
BTN = (0.17, 0.18, 0.21, 1.0)          # ordinary button
BTN_PRIMARY = (0.17, 0.33, 0.55, 1.0)  # the action you came here for
BTN_CHIP = (0.13, 0.14, 0.17, 1.0)     # vocabulary chip
BTN_EXAMPLE = (0.15, 0.16, 0.19, 1.0)  # example prompt
FIELD_BG = (0.09, 0.10, 0.12, 1.0)     # editable field well

_STYLE_REPORT = []


def _new(cls, outer):
    return unreal.new_object(cls, outer)


def _shade(colour, factor):
    return (min(colour[0] * factor, 1.0), min(colour[1] * factor, 1.0),
            min(colour[2] * factor, 1.0), colour[3])


def _style_button(button, colour):
    """
    Tint a button dark. set_background_color multiplies the stock brush, which is
    all that is needed; the per-state brushes are the fallback for builds where
    that setter is missing.
    """
    try:
        button.set_background_color(unreal.LinearColor(*colour))
        if "background_color" not in _STYLE_REPORT:
            _STYLE_REPORT.append("background_color")
        return True
    except Exception:
        pass
    try:
        style = button.get_editor_property("style")
        for state, factor in (("normal", 1.0), ("hovered", 1.45),
                              ("pressed", 0.75)):
            brush = style.get_editor_property(state)
            brush.set_editor_property(
                "tint_color", unreal.SlateColor(
                    unreal.LinearColor(*_shade(colour, factor))))
            style.set_editor_property(state, brush)
        button.set_editor_property("style", style)
        if "style brushes" not in _STYLE_REPORT:
            _STYLE_REPORT.append("style brushes")
        return True
    except Exception as error:                          # noqa: BLE001
        note = "button tint failed: %s" % error
        if note not in _STYLE_REPORT:
            _STYLE_REPORT.append(note)
        return False


# Brush names on FEditableTextBoxStyle, tinted so a field reads as a dark well
# rather than the stock white slab.
_FIELD_BRUSHES = ("background_image_normal", "background_image_hovered",
                  "background_image_focused", "background_image_read_only")


def _note(text):
    if text not in _STYLE_REPORT:
        _STYLE_REPORT.append(text)


def _style_field(field, size=10):
    """
    Make an editable field match the panel: dark well, light text, sane size.

    UMG's default editable text is 24pt, which is why the emotion and audio
    boxes came out three times the height of everything around them. The font
    lives inside the widget style, not on the widget, so it has to be read out,
    edited and written back whole.
    """
    try:
        style = field.get_editor_property("widget_style")
    except Exception as error:                          # noqa: BLE001
        _note("no widget_style: %s" % error)
        style = None

    if style is not None:
        # Font: on the nested text style in 5.1+, on the style itself before that.
        for owner_name in ("text_style", None):
            try:
                owner = (style.get_editor_property(owner_name)
                         if owner_name else style)
                font = owner.get_editor_property("font")
                font.set_editor_property("size", size)
                owner.set_editor_property("font", font)
                if owner_name:
                    owner.set_editor_property(
                        "color_and_opacity",
                        unreal.SlateColor(unreal.LinearColor(*INK)))
                    style.set_editor_property(owner_name, owner)
                _note("field font via %s" % (owner_name or "style"))
                break
            except Exception:
                continue

        for brush_name in _FIELD_BRUSHES:
            try:
                brush = style.get_editor_property(brush_name)
                brush.set_editor_property(
                    "tint_color", unreal.SlateColor(unreal.LinearColor(*FIELD_BG)))
                style.set_editor_property(brush_name, brush)
            except Exception:
                continue

        try:
            field.set_editor_property("widget_style", style)
            _note("field widget_style")
        except Exception as error:                      # noqa: BLE001
            _note("widget_style write failed: %s" % error)

    try:
        field.set_foreground_color(unreal.LinearColor(*INK))
        _note("field foreground")
    except Exception:
        pass
    return True


def _sized(outer, widget, height=None, width=None):
    """Wrap a widget so it gets a real size instead of its desired size."""
    box = _new(unreal.SizeBox, outer)
    try:
        if height is not None:
            box.set_height_override(float(height))
        if width is not None:
            box.set_width_override(float(width))
    except Exception:
        pass
    box.add_child(widget)
    return box


def _text_block(outer, text, size=9, colour=None, wrap=0.0):
    block = _new(unreal.TextBlock, outer)
    block.set_text(text)
    if colour is None:
        colour = INK
    try:
        font = block.get_editor_property("font")
        font.set_editor_property("size", size)
        block.set_editor_property("font", font)
    except Exception:
        pass
    if colour is not None:
        try:
            block.set_color_and_opacity(
                unreal.SlateColor(unreal.LinearColor(*colour)))
        except Exception:
            pass
    if wrap:
        try:
            block.set_editor_property("auto_wrap_text", True)
            block.set_editor_property("wrap_text_at", wrap)
        except Exception:
            pass
    return block


def _button(outer, label, callback, tooltip="", size=9, colour=None,
            ink=None, justify=None):
    """A button whose click runs a Python callable. Keeps the callable alive."""
    button = _new(unreal.Button, outer)
    _style_button(button, colour or BTN)
    text = _text_block(outer, label, size, ink or INK)
    if justify is not None:
        try:
            text.set_justification(justify)
        except Exception:
            pass
    content = button.add_child(text)
    try:
        # A button's child is centred with no padding by default, which is why
        # the example prompts read as centred headings rather than list rows.
        content.set_padding(unreal.Margin(9.0, 4.0, 9.0, 4.0))
        content.set_horizontal_alignment(
            unreal.HorizontalAlignment.H_ALIGN_LEFT if justify is not None
            else unreal.HorizontalAlignment.H_ALIGN_CENTER)
    except Exception:
        pass
    if tooltip:
        try:
            button.set_tool_tip_text(tooltip)
        except Exception:
            pass
    button.on_clicked.add_callable(callback)
    # Both halves are held: the delegate does not own the Python function.
    _LIVE["handlers"].append((button, callback))
    return button


def _add(panel, widget, padding=(8.0, 2.0, 8.0, 2.0), fill=False):
    slot = panel.add_child(widget)
    try:
        slot.set_padding(unreal.Margin(*padding))
        slot.set_horizontal_alignment(unreal.HorizontalAlignment.H_ALIGN_FILL)
        if fill:
            slot.set_size(unreal.SlateChildSize(1.0, unreal.SlateSizeRule.FILL))
    except Exception:
        pass
    return widget


def _row(outer, parent):
    box = _new(unreal.HorizontalBox, outer)
    _add(parent, box)
    return box


def _row_add(row, widget, fill=True, padding=(0.0, 0.0, 4.0, 0.0)):
    slot = row.add_child(widget)
    try:
        slot.set_padding(unreal.Margin(*padding))
        slot.set_vertical_alignment(unreal.VerticalAlignment.V_ALIGN_CENTER)
        if fill:
            slot.set_size(unreal.SlateChildSize(1.0, unreal.SlateSizeRule.FILL))
    except Exception:
        pass
    return widget


def _heading(outer, parent, text):
    _add(parent, _text_block(outer, text.upper(), 8, INK_HEAD),
         padding=(8.0, 12.0, 8.0, 3.0))


def _checkbox(outer, parent, label, checked):
    row = _row(outer, parent)
    box = _new(unreal.CheckBox, outer)
    try:
        box.set_is_checked(bool(checked))
    except Exception:
        pass
    _row_add(row, box, fill=False, padding=(0.0, 0.0, 6.0, 0.0))
    _row_add(row, _text_block(outer, label, 9, INK), fill=True)
    return box


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _prompt_text():
    field = _LIVE["fields"].get("prompt")
    if field is None:
        return ""
    try:
        return str(field.get_text())
    except Exception:
        return ""


def _set_prompt(text):
    field = _LIVE["fields"].get("prompt")
    if field is not None:
        try:
            field.set_text(text)
        except Exception:
            pass


def _append_prompt(word):
    """Vocabulary chips append rather than replace, so a prompt builds up."""
    current = _prompt_text().rstrip()
    if current and not current.endswith((",", ".", " ")):
        current += " "
    _set_prompt((current + word).strip())


def _status(text, error=False):
    block = _LIVE["fields"].get("status")
    if block is not None:
        try:
            block.set_text(text)
            block.set_color_and_opacity(unreal.SlateColor(unreal.LinearColor(
                *((1.0, 0.45, 0.35, 1.0) if error else (0.75, 0.80, 0.85, 1.0)))))
        except Exception:
            pass
    (unreal.log_warning if error else unreal.log)("CineDirector: " + text)


def _checked(name, default=False):
    box = _LIVE["fields"].get(name)
    if box is None:
        return default
    try:
        return bool(box.is_checked())
    except Exception:
        return default


def _field_text(name):
    field = _LIVE["fields"].get(name)
    if field is None:
        return ""
    try:
        return str(field.get_text()).strip()
    except Exception:
        return ""


def _subject_hint():
    """A real actor label for the example prompts, so they run as written."""
    try:
        selected = unreal.get_editor_subsystem(
            unreal.EditorActorSubsystem).get_selected_level_actors()
        if selected:
            return selected[0].get_actor_label()
    except Exception:
        pass
    try:
        scene = cd_scene.build_scene_context()
        for info in scene.actors:
            if "NPC" in info.label or "Char" in info.label:
                return info.label
        if scene.actors:
            return scene.actors[0].label
    except Exception:
        pass
    return "the subject"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _on_author(preview=False):
    def run():
        try:
            description = _prompt_text().strip()
            if not description:
                _status("Type a shot description first.", True)
                return

            _status("Reading the level...")
            scene = cd_scene.build_scene_context()

            _status("Planning with %s..." % llm.provider_name())
            try:
                shot_plan, notes = llm.build_shot_plan(description, scene)
            except (llm.LlmError, grammar.GrammarError) as error:
                _status(str(error), True)
                return

            if _checked("one_take"):
                shot_plan.one_continuous_shot = True
            shot_plan.create_camera_cuts = _checked("cuts", True)

            summary = cd_plan.describe_plan(shot_plan)
            unreal.log("CineDirector plan:\n" + summary)

            if preview:
                _status(summary)
                return

            result = cd_executor.execute(shot_plan, scene=scene)
            if not result.success:
                _status(result.error, True)
                return
            _status("\n".join(notes + [result.describe()]))
        except Exception:
            trace = traceback.format_exc()
            unreal.log_error("CineDirector panel failed:\n" + trace)
            _status(trace.strip().split("\n")[-1], True)
    return run


def _on_bake_face():
    try:
        actor = None
        try:
            selected = unreal.get_editor_subsystem(
                unreal.EditorActorSubsystem).get_selected_level_actors()
            actor = selected[0] if selected else None
        except Exception:
            actor = None
        if actor is None:
            _status("Select a character in the level first.", True)
            return

        mesh, _component = face_baker.pick_face_mesh(actor)
        if mesh is None:
            _status("'%s' has no skeletal mesh component."
                    % actor.get_actor_label(), True)
            return

        request = face_baker.BakeRequest()
        request.mesh = mesh
        request.actor = actor
        request.audio_path = _field_text("audio")
        request.emotion_text = _field_text("emotion")
        request.talking = _checked("talking")
        request.auto_blinks = _checked("blinks", True)

        if not (request.audio_path or request.talking
                or request.emotion_text.strip()):
            _status("Give a dialogue file, tick Talk without audio, or name an "
                    "emotion.", True)
            return

        _status("Baking the face for %s..." % actor.get_actor_label())
        with unreal.ScopedEditorTransaction("CineDirector: bake face"):
            result = face_baker.bake(request)
            if _checked("to_sequence", True):
                face_baker.attach_to_sequence(result, actor, mesh)
        _status(result.describe())

    except face_baker.BakeError as error:
        _status(str(error), True)
    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector face bake failed:\n" + trace)
        _status(trace.strip().split("\n")[-1], True)


def _on_frames_to_mp4():
    """Encode the last render's frame folder, or whatever the field points at."""
    try:
        from . import render as cd_render
        if _ENCODE["state"] is not None:
            _status("An MP4 encode is already running.")
            return
        folder = _field_text("frames") or cd_render.last_output_directory()
        if not folder:
            _status("Type the folder your frames are in, then press this again.",
                    True)
            return
        def started(state):
            _ENCODE["state"] = state
            def poll(_delta_time):
                if not state["done"]:
                    return
                unreal.unregister_slate_post_tick_callback(_ENCODE["tick"])
                _ENCODE["tick"] = None
                _ENCODE["state"] = None
                _path, notes, error = state["result"]
                _status(str(error) if error else "\n".join(notes), bool(error))
            _ENCODE["tick"] = unreal.register_slate_post_tick_callback(poll)
        _status("Encoding %s..." % folder)
        cd_render.encode_folder(folder, on_complete=started, delete_frames=True)
    except Exception as error:                          # noqa: BLE001
        _status(str(error), True)


def _on_clear():
    _set_prompt("")
    _status("Ready.")


def _on_use_selected():
    label = _subject_hint()
    _append_prompt(label)
    _status("Added '%s' to the prompt." % label)


def _on_example(text):
    def run():
        _set_prompt(text.replace("{subject}", _subject_hint()))
        _status("Loaded an example. Edit it, then press Author shots.")
    return run


def _on_chip(word):
    def run():
        _append_prompt(word)
    return run


def _on_refresh():
    build(_LIVE.get("widget"))


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _find_root(widget):
    """
    The container the human dropped into the asset, by property name.

    Duck-typed rather than isinstance-checked against PanelWidget: the ODK's
    palette offers its own scroll widgets alongside the engine ones, and what
    matters is only whether children can be added. What was found is logged
    either way, because "the panel is empty" is otherwise unexplainable.
    """
    for name in ROOT_CANDIDATES:
        try:
            found = widget.get_editor_property(name)
        except Exception:
            continue
        if found is None:
            continue
        if hasattr(found, "add_child"):
            return found, name
        unreal.log_warning(
            "CineDirector: '%s' is a %s, which cannot take children. Replace it "
            "with a plain Scroll Box or Vertical Box named Root."
            % (name, type(found).__name__))
    return None, ""


def build(widget):
    """Fill the panel. Safe to call again; the container is emptied first."""
    if widget is None:
        return False

    root, found_name = _find_root(widget)
    if root is None:
        unreal.log_error(
            "CineDirector: the panel asset has no container Python can reach. "
            "Open %s, drop a Scroll Box into it, name it \"Root\", tick Is "
            "Variable, and save." % ASSET_PATH)
        return False

    try:
        root.clear_children()
    except Exception:
        pass
    _LIVE["widget"] = widget
    _LIVE["root"] = root
    _LIVE["handlers"] = []
    _LIVE["fields"] = {}

    outer = widget

    _STYLE_REPORT[:] = []
    _add(root, _text_block(outer, "CineDirector", 13, INK),
         padding=(8.0, 8.0, 8.0, 0.0))
    _add(root, _text_block(outer, "Planner: %s" % llm.provider_name(), 8,
                           INK_DIM))

    # --- the prompt -------------------------------------------------------
    _heading(outer, root, "Shot description")
    prompt = _new(unreal.MultiLineEditableTextBox, outer)
    try:
        prompt.set_hint_text("slow push in on the mask, 85mm, shallow focus, "
                             "then orbit it from the left")
    except Exception:
        pass
    _style_field(prompt, 11)
    _LIVE["fields"]["prompt"] = prompt
    # A multiline box collapses to one line on its desired size, so the height
    # is forced: this is the field the whole panel exists to fill in.
    _add(root, _sized(outer, prompt, height=96), padding=(8.0, 2.0, 8.0, 6.0))

    row = _row(outer, root)
    _row_add(row, _button(outer, "Author shots", _on_author(False),
                          "Interpret the description and build the coverage.",
                          colour=BTN_PRIMARY))
    _row_add(row, _button(outer, "Preview plan", _on_author(True),
                          "Show the plan without spawning anything."))
    _row_add(row, _button(outer, "Clear", _on_clear), fill=False)

    row = _row(outer, root)
    _row_add(row, _button(outer, "Add selected actor", _on_use_selected,
                          "Append the selected actor's label to the prompt."))
    _row_add(row, _button(outer, "Refresh panel", _on_refresh,
                          "Rebuild this panel after a code change."), fill=False)

    _LIVE["fields"]["cuts"] = _checkbox(outer, root, "Create camera cuts", True)
    _LIVE["fields"]["one_take"] = _checkbox(outer, root, "One continuous take",
                                            False)

    # --- examples ---------------------------------------------------------
    _heading(outer, root, "Example prompts")
    for example in EXAMPLES:
        _add(root, _button(outer, example.replace("{subject}", "<selected>"),
                           _on_example(example), example, 8,
                           colour=BTN_EXAMPLE, ink=INK_DIM,
                           justify=unreal.TextJustify.LEFT),
             padding=(8.0, 1.0, 8.0, 1.0))

    # --- vocabulary -------------------------------------------------------
    _heading(outer, root, "Click to add")
    for title, words in VOCABULARY:
        _add(root, _text_block(outer, title, 8, INK_DIM),
             padding=(8.0, 9.0, 8.0, 1.0))
        wrap = _new(unreal.WrapBox, outer)
        try:
            wrap.set_editor_property("inner_slot_padding",
                                     unreal.Vector2D(3.0, 3.0))
        except Exception:
            pass
        _add(root, wrap, padding=(8.0, 0.0, 8.0, 3.0))
        for word in words:
            wrap.add_child(_button(outer, word, _on_chip(word), "", 8,
                                   colour=BTN_CHIP))

    # --- face -------------------------------------------------------------
    _heading(outer, root, "Face and lipsync")
    _add(root, _text_block(outer, "Drives the selected character.", 8, INK_DIM))

    def labelled_field(caption, hint):
        row = _row(outer, root)
        # A fixed label column keeps the two fields aligned with each other
        # instead of each starting wherever its caption happens to end.
        _row_add(row, _sized(outer, _text_block(outer, caption, 9, INK_DIM),
                             width=52), fill=False)
        field = _new(unreal.EditableTextBox, outer)
        try:
            field.set_hint_text(hint)
        except Exception:
            pass
        _style_field(field)
        _row_add(row, _sized(outer, field, height=26))
        return field

    _LIVE["fields"]["emotion"] = labelled_field(
        "Emotion", "angry, or an arc: nervous then angry")
    _LIVE["fields"]["audio"] = labelled_field(
        "Audio", "full path to a .wav, or leave empty")

    _LIVE["fields"]["talking"] = _checkbox(outer, root, "Talk without audio",
                                           False)
    _LIVE["fields"]["blinks"] = _checkbox(outer, root, "Automatic blinks", True)
    _LIVE["fields"]["to_sequence"] = _checkbox(outer, root,
                                               "Add to Level Sequence", True)

    row = _row(outer, root)
    _row_add(row, _button(outer, "Bake face", _on_bake_face,
                          "Bake the performance onto the selected character.",
                          colour=BTN_PRIMARY))
    _row_add(row, _button(outer, "Face report", cd_dialogs.open_face_report,
                          "What blendshapes the selected character has."))

    # --- the rest ---------------------------------------------------------
    _heading(outer, root, "More")

    row = _row(outer, root)
    _row_add(row, _text_block(outer, "Frames", 9, INK_DIM), fill=False)
    frames = _new(unreal.EditableTextBox, outer)
    try:
        frames.set_hint_text("folder of rendered frames, empty uses the last render")
    except Exception:
        pass
    _style_field(frames)
    _LIVE["fields"]["frames"] = frames
    _row_add(row, _sized(outer, frames, height=26))
    _row_add(row, _button(outer, "Frames to MP4", _on_frames_to_mp4,
                          "Encode a folder of rendered frames into an MP4 with "
                          "ffmpeg."), fill=False)

    row = _row(outer, root)
    _row_add(row, _button(outer, "Render...", cd_dialogs.open_render_dialog))
    _row_add(row, _button(outer, "Scene report", cd_dialogs.open_scene_report))
    _row_add(row, _button(outer, "Settings...", cd_dialogs.open_settings_dialog))

    # --- status -----------------------------------------------------------
    _heading(outer, root, "Status")
    status = _text_block(outer, "Ready.", 9, INK, wrap=420.0)
    _LIVE["fields"]["status"] = status
    _add(root, status, padding=(8.0, 2.0, 8.0, 16.0))

    if _STYLE_REPORT:
        unreal.log("CineDirector panel styling: " + ", ".join(_STYLE_REPORT))
    return True


# ---------------------------------------------------------------------------
# The asset and the tab
# ---------------------------------------------------------------------------

def ensure_asset():
    """Create the Editor Utility Widget asset if it is not there yet."""
    if unreal.EditorAssetLibrary.does_asset_exist(ASSET_PATH):
        return unreal.EditorAssetLibrary.load_asset(ASSET_PATH)

    factory = unreal.EditorUtilityWidgetBlueprintFactory()
    try:
        factory.set_editor_property("parent_class", unreal.EditorUtilityWidget)
    except Exception:
        pass
    blueprint = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        ASSET_NAME, ASSET_DIR, None, factory)
    if blueprint is None:
        return None
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(blueprint, False)
    except Exception:
        pass
    return blueprint


def open_panel():
    """Tools > CineDirector > Panel. Spawns the tab and fills it."""
    try:
        blueprint = ensure_asset()
        if blueprint is None:
            cd_dialogs._message("CineDirector",
                                "Could not create the panel asset at %s."
                                % ASSET_PATH)
            return

        subsystem = unreal.get_editor_subsystem(unreal.EditorUtilitySubsystem)
        widget = None
        try:
            widget, _tab_id = subsystem.spawn_and_register_tab_and_get_id(blueprint)
        except Exception:
            widget = subsystem.spawn_and_register_tab(blueprint)

        if widget is None:
            cd_dialogs._message("CineDirector", "The panel tab did not open.")
            return

        if not build(widget):
            cd_dialogs._message(
                "CineDirector - one-time setup",
                "The panel asset needs one container before Python can fill it.\n\n"
                "1. Open %s (it was just created for you).\n"
                "2. From the Palette, drag a Scroll Box onto the empty graph.\n"
                "   It becomes the root widget and fills the tab on its own,\n"
                "   so there are no anchors to set.\n"
                "3. Rename it to Root and tick Is Variable in its details.\n"
                "4. Compile and save, then open the panel again.\n\n"
                "Everything else is built by script." % ASSET_PATH)

    except Exception:
        trace = traceback.format_exc()
        unreal.log_error("CineDirector panel failed to open:\n" + trace)
        cd_dialogs._message("CineDirector - Error", trace)
