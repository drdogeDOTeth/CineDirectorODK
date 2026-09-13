# Copyright Roundtree. All Rights Reserved.
"""
Bakes slot timelines into an additive, curves-only AnimSequence.

Port of CineFaceBaker.cpp. The output carries float curves and no bone tracks, and
is marked additive against the reference pose, so it layers over whatever body
animation the character is already playing without touching the pose.

The sequence length has to be set through the animation data controller, reached
as the AnimSequence's "controller" editor property. Writing curve keys past the
end of a one-frame asset silently does nothing otherwise.
"""

import math
import os

import unreal

from . import face, lipsync

DEFAULT_FPS = 30.0
ASSET_DIR = "/Game/CineDirector/FaceAnims"
AUDIO_DIR = "/Game/CineDirector/Audio"

# Lips lead the audio slightly: a speaker's mouth is already forming a shape
# before the sound arrives. Jaw and closures stay on-frame.
LIP_LEAD_SECONDS = 0.055

LEAD_SLOTS = (face.MOUTH_WIDE, face.MOUTH_PUCKER, face.MOUTH_FUNNEL)

# Mouth-region slots that yield to speech, so an emotion does not fight a viseme.
MOUTH_EMOTION_YIELD = {
    face.MOUTH_SMILE: 0.55,
    face.MOUTH_FROWN: 0.55,
    face.MOUTH_PRESS: 0.45,
    face.NOSE_SNEER: 0.25,
    face.EXPR_HAPPY: 0.30,
    face.EXPR_ANGRY: 0.30,
    face.EXPR_SAD: 0.30,
    face.EXPR_SURPRISED: 0.30,
}

# Emotion poses. The full-face Expr* slots are driven hard so VRM/MMD style
# whole-face morphs read clearly, while the micro-slots still fire on ARKit and
# MetaHuman faces that have no full-face equivalent.
EMOTIONS = (
    ("scared,afraid,terrified,fear,frightened",
     ((face.EXPR_SURPRISED, 0.90), (face.BROW_SAD, 0.70), (face.EYE_WIDE, 0.95),
      (face.MOUTH_FROWN, 0.55), (face.MOUTH_PRESS, 0.45))),
    ("angry,furious,mad,rage,pissed",
     ((face.EXPR_ANGRY, 1.00), (face.BROW_DOWN, 0.90), (face.EYE_SQUINT, 0.80),
      (face.NOSE_SNEER, 0.85), (face.MOUTH_PRESS, 0.80), (face.MOUTH_FROWN, 0.70))),
    ("happy,joyful,cheerful,smiling,glad",
     ((face.EXPR_HAPPY, 0.90), (face.MOUTH_SMILE, 0.85), (face.EYE_SQUINT, 0.35),
      (face.BROW_UP, 0.22))),
    ("sad,somber,mournful,depressed,grief",
     ((face.EXPR_SAD, 1.00), (face.BROW_SAD, 0.85), (face.MOUTH_FROWN, 0.95),
      (face.EYE_BLINK, 0.15))),
    ("surprised,shocked,amazed,startled",
     ((face.EXPR_SURPRISED, 1.00), (face.BROW_UP, 0.65), (face.EYE_WIDE, 1.00),
      (face.JAW_OPEN, 0.45))),
    ("disgusted,disgust,revolted,grossed",
     ((face.EXPR_ANGRY, 0.75), (face.NOSE_SNEER, 1.00), (face.BROW_DOWN, 0.75),
      (face.MOUTH_FROWN, 0.80), (face.EYE_SQUINT, 0.70))),
    ("pain,hurt,agony,wincing",
     ((face.EXPR_SAD, 0.85), (face.EYE_SQUINT, 1.00), (face.BROW_SAD, 0.75),
      (face.NOSE_SNEER, 0.70), (face.MOUTH_WIDE, 0.55))),
    ("suspicious,wary,distrustful,skeptical",
     ((face.EXPR_ANGRY, 0.60), (face.BROW_DOWN, 0.75), (face.EYE_SQUINT, 0.90),
      (face.MOUTH_PRESS, 0.60))),
    ("nervous,anxious,uneasy,worried,tense",
     ((face.BROW_SAD, 0.60), (face.EYE_SQUINT, 0.30), (face.MOUTH_PRESS, 0.55),
      (face.EYE_BLINK, 0.20))),
    ("calm,relaxed", ((face.BROW_UP, 0.05), (face.MOUTH_SMILE, 0.05))),
    ("neutral,blank", ()),
)


class BakeError(Exception):
    """Raised when a face animation cannot be baked."""


def _clamp(value, low=0.0, high=1.0):
    return low if value < low else (high if value > high else value)


class BakeRequest(object):
    """Everything the baker needs. Mirrors the panel's Face section."""

    def __init__(self):
        self.mesh = None                 # SkeletalMesh whose morphs are driven
        self.profile = None              # face.FaceProfile
        self.actor = None                # optional, for adding to Sequencer
        self.audio_path = ""             # wav/mp3/ogg, or empty
        self.talking = False             # procedural mouth when there is no audio
        self.emotion_text = ""           # "nervous then angry"
        self.emotion_strength = 1.0      # 0..2
        self.duration_seconds = 4.0      # used when there is no audio
        self.fps = DEFAULT_FPS
        self.mouth_strength = 1.0        # 0..2
        self.articulation = 1.0          # 0..2; 1 leaves the shapes as analysed
        self.auto_blinks = True
        self.asset_name = ""             # defaults to <Mesh>_Face
        self.asset_dir = ASSET_DIR
        self.keep_as_new_take = False     # False reuses one stable asset


class BakeResult(object):
    def __init__(self):
        self.animation = None
        self.sound = None
        self.notes = []
        self.frame_count = 0
        self.duration_seconds = 0.0
        self.curves_written = 0

    def note(self, text):
        self.notes.append(text)

    def describe(self):
        head = ("Baked %s: %d frames, %.2fs, %d curves."
                % (self.animation.get_name() if self.animation else "(nothing)",
                   self.frame_count, self.duration_seconds, self.curves_written))
        return "\n".join([head] + ["  " + n for n in self.notes])


# ---------------------------------------------------------------------------
# Emotion
# ---------------------------------------------------------------------------

def _emotion_pose(text):
    """
    Accumulate a slot pose from one clause of emotion words.
    Returns (pose dict, matched words).
    """
    pose = {}
    matched = []
    lowered = (text or "").lower()
    for keywords, slots in EMOTIONS:
        for word in keywords.split(","):
            if word and word in lowered:
                matched.append(word)
                for slot, weight in slots:
                    pose[slot] = max(pose.get(slot, 0.0), weight)
                break
    return pose, matched


def _emotion_timeline(request, frame_count):
    """
    Per-frame slot values from the emotion text.

    "nervous then angry" arcs from the first pose to the second across the take,
    which is how the C++ handles a "then" in the description.
    """
    timeline = {}
    text = (request.emotion_text or "").strip()
    if not text:
        return timeline, []

    clauses = [c.strip() for c in text.lower().split(" then ") if c.strip()]
    poses = []
    all_matched = []
    for clause in clauses:
        pose, matched = _emotion_pose(clause)
        poses.append(pose)
        all_matched.extend(matched)

    if not poses:
        return timeline, []

    strength = _clamp(request.emotion_strength, 0.0, 2.0)
    slots = set()
    for pose in poses:
        slots.update(pose.keys())

    for slot in slots:
        series = [0.0] * frame_count
        for i in range(frame_count):
            if len(poses) == 1:
                value = poses[0].get(slot, 0.0)
            else:
                # Position along the chain of poses, blending between neighbours.
                position = (i / float(max(frame_count - 1, 1))) * (len(poses) - 1)
                low = int(math.floor(position))
                high = min(low + 1, len(poses) - 1)
                blend = position - low
                value = (poses[low].get(slot, 0.0) * (1.0 - blend)
                         + poses[high].get(slot, 0.0) * blend)
            series[i] = _clamp(value * strength, 0.0, 1.0)
        timeline[slot] = series

    return timeline, sorted(set(all_matched))


# ---------------------------------------------------------------------------
# Visemes to slots
# ---------------------------------------------------------------------------

_VISEME_TO_SLOT = (
    ("jaw", face.JAW_OPEN),
    ("wide", face.MOUTH_WIDE),
    ("pucker", face.MOUTH_PUCKER),
    ("funnel", face.MOUTH_FUNNEL),
    ("close", face.MOUTH_CLOSE),
    ("fv", face.VISEME_FV),
    ("l", face.VISEME_L),
    ("th", face.VISEME_TH),
    ("ch", face.VISEME_CH),
)

_EXCLUSIVE_CANDIDATES = ("jaw", "wide", "pucker", "funnel", "close")


def _viseme_timeline(frames, profile, mouth_strength):
    """Viseme frames to slot timelines, honouring an exclusive-viseme rig."""
    timeline = {}
    count = len(frames)
    strength = _clamp(mouth_strength, 0.0, 2.0)

    for channel, slot in _VISEME_TO_SLOT:
        timeline[slot] = [0.0] * count

    for i, frame in enumerate(frames):
        if profile is not None and profile.exclusive_visemes:
            # One dominant mouth shape per frame; the others stay at rest.
            best_channel = None
            best_value = 0.0
            for channel in _EXCLUSIVE_CANDIDATES:
                value = getattr(frame, channel)
                # A closure outranks a vowel of the same size: lips shut reads
                # as a consonant, while a half-open vowel reads as mush.
                weighted = value * (1.15 if channel == "close" else 1.0)
                if weighted > best_value:
                    best_value = weighted
                    best_channel = channel
            if best_channel is not None:
                timeline[dict(_VISEME_TO_SLOT)[best_channel]][i] = \
                    _clamp(getattr(frame, best_channel) * strength)
            for channel, slot in _VISEME_TO_SLOT:
                if channel not in _EXCLUSIVE_CANDIDATES:
                    timeline[slot][i] = _clamp(getattr(frame, channel) * strength)
        else:
            for channel, slot in _VISEME_TO_SLOT:
                timeline[slot][i] = _clamp(getattr(frame, channel) * strength)

    # Sibilance shows teeth rather than opening the jaw.
    sibilant = [f.sibilant for f in frames]
    if any(sibilant):
        timeline.setdefault(face.MOUTH_WIDE, [0.0] * count)
        for i, value in enumerate(sibilant):
            timeline[face.MOUTH_WIDE][i] = max(timeline[face.MOUTH_WIDE][i],
                                               _clamp(value * 0.55 * strength))
    return timeline


def _apply_articulation(timeline, amount):
    """
    Sharpen or soften the speech shapes.

    Above 1 is an unsharp mask, which makes consonants crisper. Below 1 blends
    toward a short local average, which softens a mumble. Only the speech
    channels are touched; brows and eyes are left alone.
    """
    if abs(amount - 1.0) < 1e-3:
        return

    speech_slots = (face.JAW_OPEN, face.MOUTH_WIDE, face.MOUTH_PUCKER,
                    face.MOUTH_FUNNEL, face.MOUTH_CLOSE)
    for slot in speech_slots:
        series = timeline.get(slot)
        if not series:
            continue
        window = 2      # about 80 ms either side at 30 fps
        averaged = []
        for i in range(len(series)):
            low = max(0, i - window)
            high = min(len(series), i + window + 1)
            averaged.append(sum(series[low:high]) / float(high - low))
        for i, value in enumerate(series):
            if amount > 1.0:
                sharpened = value + (value - averaged[i]) * (amount - 1.0) * 1.6
            else:
                sharpened = averaged[i] + (value - averaged[i]) * amount
            series[i] = _clamp(sharpened)


def _apply_lip_lead(timeline, fps):
    """Shift the rounded and wide shapes earlier; the jaw stays on the audio."""
    lead_frames = int(round(LIP_LEAD_SECONDS * fps))
    if lead_frames <= 0:
        return
    for slot in LEAD_SLOTS:
        series = timeline.get(slot)
        if not series:
            continue
        shifted = series[lead_frames:] + [series[-1]] * min(lead_frames, len(series))
        timeline[slot] = shifted[:len(series)]


def _apply_teeth_reveal(timeline, profile):
    """
    Derive the upper-lip raise and lower-lip depress from the final jaw and width,
    so open vowels show teeth. Suppressed by rounding and by closures, where the
    lips cover the teeth.
    """
    if profile is None:
        return
    if not (profile.has(face.MOUTH_UPPER_UP) or profile.has(face.MOUTH_LOWER_DOWN)):
        return

    jaw = timeline.get(face.JAW_OPEN) or []
    wide = timeline.get(face.MOUTH_WIDE) or []
    pucker = timeline.get(face.MOUTH_PUCKER) or []
    funnel = timeline.get(face.MOUTH_FUNNEL) or []
    close = timeline.get(face.MOUTH_CLOSE) or []
    count = len(jaw) or len(wide)
    if not count:
        return

    def at(series, index):
        return series[index] if index < len(series) else 0.0

    upper = timeline.setdefault(face.MOUTH_UPPER_UP, [0.0] * count)
    lower = timeline.setdefault(face.MOUTH_LOWER_DOWN, [0.0] * count)

    for i in range(count):
        openness = at(jaw, i)
        width = at(wide, i)
        rounding = max(at(pucker, i), at(funnel, i))
        shut = at(close, i)
        reveal = _clamp((openness * 0.55 + width * 0.45) * (1.0 - rounding * 0.85)
                        * (1.0 - shut))
        upper[i] = max(upper[i], reveal * 0.45)
        lower[i] = max(lower[i], reveal * 0.60)


def _apply_auto_blinks(timeline, frame_count, fps, seed=7):
    """
    Scatter blinks at human intervals: roughly one every 2 to 6 seconds, each
    about 120 ms, never on top of an existing hard blink from an emotion.
    """
    series = timeline.setdefault(face.EYE_BLINK, [0.0] * frame_count)
    while len(series) < frame_count:
        series.append(0.0)

    state = (seed * 2654435761) & 0xFFFFFFFF

    def rand():
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 4294967296.0

    blink_frames = max(2, int(round(0.12 * fps)))
    index = int(rand() * 2.0 * fps)
    count = 0
    while index < frame_count:
        peak = index + blink_frames // 2
        for step in range(blink_frames):
            position = index + step
            if position >= frame_count:
                break
            # Triangular close and open.
            distance = abs(position - peak) / float(max(blink_frames / 2.0, 1.0))
            series[position] = max(series[position], _clamp(1.0 - distance))
        count += 1
        index += int((2.0 + rand() * 4.0) * fps)
    return count


def _yield_mouth_emotion(timeline, frames, fps):
    """
    Scale the mouth-region emotion down while the character is speaking, so a
    smile does not fight a viseme for the same lips. Brows and eyes hold.
    """
    if not frames:
        return
    # A smoothed speech envelope, so the yield eases in rather than flickering.
    envelope = [max(f.jaw, f.wide, f.pucker, f.funnel) for f in frames]
    lipsync.smooth(envelope, 0.45, 0.25)

    for slot, factor in MOUTH_EMOTION_YIELD.items():
        series = timeline.get(slot)
        if not series:
            continue
        for i in range(len(series)):
            speech = envelope[i] if i < len(envelope) else 0.0
            series[i] = _clamp(series[i] * (1.0 - speech * factor))


# ---------------------------------------------------------------------------
# Asset writing
# ---------------------------------------------------------------------------

def _unique_asset_name(base, directory, keep_as_new_take):
    """
    One stable asset per mesh unless the user asked to archive takes.

    Reusing the name is what keeps a folder of hundreds of timestamped takes from
    building up; archiving is opt-in.
    """
    if not keep_as_new_take:
        return base
    import time
    return "%s_%s" % (base, time.strftime("%Y%m%d_%H%M%S"))


def _create_or_reuse(anim_name, directory, skeleton, result):
    path = "%s/%s" % (directory.rstrip("/"), anim_name)
    existing = None
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(path):
            existing = unreal.EditorAssetLibrary.load_asset(path)
    except Exception:
        existing = None

    if existing is not None and isinstance(existing, unreal.AnimSequence):
        # Wipe the previous bake so old curves do not survive underneath.
        try:
            unreal.AnimationLibrary.remove_all_curve_data(existing)
        except Exception:
            pass
        result.note("Reused '%s'." % anim_name)
        return existing

    factory = unreal.AnimSequenceFactory()
    factory.set_editor_property("target_skeleton", skeleton)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    animation = tools.create_asset(anim_name, directory, unreal.AnimSequence, factory)
    if animation is None:
        raise BakeError("Could not create an AnimSequence at %s." % path)
    result.note("Created '%s'." % anim_name)
    return animation


def _write_curves(animation, timeline, profile, fps, frame_count, result):
    """Set the length, then write one float curve per driven morph target."""
    controller = animation.get_editor_property("controller")
    if controller is None:
        raise BakeError("This AnimSequence exposes no animation data controller, "
                        "so its length cannot be set.")

    duration = max(frame_count / float(fps), 1.0 / fps)

    controller.open_bracket("CineDirector face bake", False)
    try:
        try:
            controller.set_frame_rate(unreal.FrameRate(int(round(fps)), 1), False)
        except Exception as error:      # noqa: BLE001
            result.note("Could not set the frame rate (%s)." % error)
        controller.set_play_length(duration, False)

        # Collect per curve, because two slots can legitimately drive the same
        # morph and the larger value should win rather than the later one.
        curves = {}
        for slot, series in timeline.items():
            targets = profile.targets(slot) if profile is not None else []
            for target in targets:
                values = curves.setdefault(target.name, [0.0] * frame_count)
                for i in range(min(frame_count, len(series))):
                    scaled = _clamp(series[i] * target.scale)
                    if scaled > values[i]:
                        values[i] = scaled

        written = 0
        times = [i / float(fps) for i in range(frame_count)]
        for name, values in curves.items():
            if not any(v > 1e-4 for v in values):
                continue
            try:
                if not unreal.AnimationLibrary.does_curve_exist(
                        animation, name, unreal.RawCurveTrackTypes.RCT_FLOAT):
                    unreal.AnimationLibrary.add_curve(animation, name)
            except Exception:
                try:
                    unreal.AnimationLibrary.add_curve(animation, name)
                except Exception as error:      # noqa: BLE001
                    result.note("Curve '%s' skipped (%s)." % (name, error))
                    continue
            try:
                unreal.AnimationLibrary.add_float_curve_keys(animation, name,
                                                             times, values)
                written += 1
            except Exception as error:      # noqa: BLE001
                result.note("Curve '%s' could not be keyed (%s)." % (name, error))
    finally:
        try:
            controller.close_bracket(False)
        except Exception:
            pass

    # Curves only, additive against the reference pose, so this layers over body
    # animation without touching the pose.
    try:
        unreal.AnimationLibrary.set_additive_animation_type(
            animation, unreal.AdditiveAnimationType.AAT_LOCAL_SPACE_BASE)
        unreal.AnimationLibrary.set_additive_base_pose_type(
            animation, unreal.AdditiveBasePoseType.ABPT_REF_POSE)
    except Exception as error:          # noqa: BLE001
        result.note("Could not mark the animation additive (%s)." % error)

    return written, duration


# ---------------------------------------------------------------------------
# Audio import
# ---------------------------------------------------------------------------

def import_audio(wav_path, result):
    """Import a wav into the project so Sequencer can play it. Returns the SoundWave."""
    if not wav_path or not os.path.isfile(wav_path):
        return None
    name = os.path.splitext(os.path.basename(wav_path))[0]
    target = "%s/%s" % (AUDIO_DIR, name)
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(target):
            return unreal.EditorAssetLibrary.load_asset(target)
    except Exception:
        pass

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", wav_path)
    task.set_editor_property("destination_path", AUDIO_DIR)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    # Saved to disk on purpose. An unsaved package can be collected while a
    # Level Sequence still points at it, and Movie Render Queue tearing down its
    # PIE world is exactly when that happens.
    task.set_editor_property("save", True)
    try:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        objects = task.get_editor_property("imported_object_paths")
        if objects:
            sound = unreal.EditorAssetLibrary.load_asset(str(objects[0]))
            try:
                unreal.EditorAssetLibrary.save_loaded_asset(sound, False)
            except Exception:
                pass
            return sound
    except Exception as error:          # noqa: BLE001
        result.note("Audio import failed (%s)." % error)
    return None


# ---------------------------------------------------------------------------
# Sequencer
# ---------------------------------------------------------------------------

def _face_component(actor, mesh):
    """The component on this actor whose asset is the face we just baked."""
    if actor is None:
        return None
    best = None
    try:
        components = actor.get_components_by_class(unreal.SkeletalMeshComponent)
    except Exception:
        return None
    for component in components:
        try:
            asset = component.get_skeletal_mesh_asset()
        except Exception:
            asset = None
        if asset is None:
            continue
        if mesh is not None and asset == mesh:
            return component
        if best is None:
            best = component
    return best


def pick_face_mesh(actor):
    """
    The mesh on this actor most likely to carry the face.

    A character is often a body mesh plus a separate face mesh, so the component
    with the most morph targets is the face. Returns (mesh, component) and leaves
    the "no morphs anywhere" case for bake() to report.
    """
    if actor is None:
        return None, None
    best_mesh = None
    best_component = None
    best_count = -1
    try:
        components = actor.get_components_by_class(unreal.SkeletalMeshComponent)
    except Exception:
        return None, None
    for component in components:
        try:
            mesh = component.get_skeletal_mesh_asset()
        except Exception:
            mesh = None
        if mesh is None:
            continue
        try:
            count = len(mesh.get_editor_property("morph_targets"))
        except Exception:
            count = 0
        if count > best_count:
            best_count = count
            best_mesh = mesh
            best_component = component
    return best_mesh, best_component


def _find_binding(sequence, name):
    for binding in sequence.get_bindings():
        try:
            if binding.get_name() == name:
                return binding
        except Exception:
            continue
    return None


def attach_to_sequence(result, actor, mesh, sequence=None, fps=DEFAULT_FPS):
    """
    Put the baked animation, and its audio, on the Level Sequence.

    The animation goes on the component that owns the face rather than the actor,
    because a character's face is often a separate mesh component and a track on
    the actor's root would drive the body instead.
    """
    from . import executor as cd_executor

    target = cd_executor._resolve_sequence(sequence)
    if target is None:
        result.note("No Level Sequence open, so the animation was not added to "
                    "one. Open a sequence and re-bake, or drag the asset in.")
        return False
    if actor is None:
        result.note("No actor selected, so the animation was not added to '%s'."
                    % target.get_name())
        return False

    frame_count = max(1, result.frame_count)
    component = _face_component(actor, mesh)
    subject = component if component is not None else actor

    try:
        name = (component.get_name() if component is not None
                else actor.get_actor_label())
        binding = _find_binding(target, name)
        if binding is None:
            binding = target.add_possessable(subject)
        if binding is None:
            result.note("Could not bind '%s' into the sequence." % name)
            return False

        # One face track per binding: replace ours rather than stacking takes.
        for track in binding.get_tracks():
            if isinstance(track, unreal.MovieSceneSkeletalAnimationTrack):
                binding.remove_track(track)

        track = binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)
        track.set_display_name("CineDirector Face")
        section = track.add_section()
        section.set_range(0, int(frame_count))
        params = section.get_editor_property("params")
        params.set_editor_property("animation", result.animation)
        section.set_editor_property("params", params)
        result.note("Added the animation to '%s' on binding '%s'."
                    % (target.get_name(), name))
    except Exception as error:              # noqa: BLE001
        result.note("Could not add the animation to the sequence (%s)." % error)
        return False

    if result.sound is not None:
        try:
            audio_track = None
            for track in target.get_tracks():
                if isinstance(track, unreal.MovieSceneAudioTrack):
                    try:
                        if track.get_display_name() == "CineDirector Dialogue":
                            audio_track = track
                            break
                    except Exception:
                        continue
            if audio_track is None:
                audio_track = target.add_track(unreal.MovieSceneAudioTrack)
                audio_track.set_display_name("CineDirector Dialogue")
            else:
                for old in audio_track.get_sections():
                    audio_track.remove_section(old)
            audio = audio_track.add_section()
            # One frame early: Movie Render Queue evaluates from frame -1 when
            # temporal sub-sampling is on, and warns that a section starting at 0
            # cannot be auto-expanded to cover it.
            audio.set_range(-1, int(frame_count))
            audio.set_editor_property("sound", result.sound)
            result.note("Added the dialogue audio.")
        except Exception as error:          # noqa: BLE001
            result.note("Could not add the audio track (%s)." % error)

    # A sequence shorter than the performance would clip the tail off.
    try:
        if target.get_playback_end() < frame_count:
            target.set_playback_end(int(frame_count))
            result.note("Extended the sequence to %d frames." % frame_count)
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def bake(request):
    """Bake a face performance. Returns a BakeResult, or raises BakeError."""
    result = BakeResult()

    if request.mesh is None:
        raise BakeError("No skeletal mesh selected.")

    profile = request.profile or face.analyze(request.mesh)
    if profile.mapped_slot_count == 0:
        raise BakeError("'%s' has no recognisable facial blendshapes, so there is "
                        "nothing to drive." % profile.mesh_name)

    try:
        skeleton = request.mesh.skeleton
    except Exception:
        skeleton = None
    if skeleton is None:
        raise BakeError("'%s' has no skeleton." % profile.mesh_name)

    fps = float(request.fps or DEFAULT_FPS)

    # --- speech -----------------------------------------------------------
    viseme_frames = []
    sound = None
    if request.audio_path:
        samples, rate, wav_path = lipsync.load_mono(request.audio_path)
        viseme_frames = lipsync.analyze(samples, rate, fps=fps)
        duration = len(samples) / float(rate)
        result.note("Analysed %.2fs of audio at %d Hz into %d frames."
                    % (duration, rate, len(viseme_frames)))
        sound = import_audio(wav_path, result)
        if sound is not None:
            result.note("Imported audio as '%s'." % sound.get_name())
    elif request.talking:
        viseme_frames = lipsync.synthesize_talking(request.duration_seconds, fps=fps)
        result.note("No audio: synthesised %d frames of talking."
                    % len(viseme_frames))

    frame_count = (len(viseme_frames) if viseme_frames
                   else max(2, int(round(max(request.duration_seconds, 0.1) * fps))))

    # --- slot timelines ---------------------------------------------------
    timeline = {}
    if viseme_frames:
        timeline.update(_viseme_timeline(viseme_frames, profile,
                                         request.mouth_strength))
        _apply_articulation(timeline, _clamp(request.articulation, 0.0, 2.0))
        _apply_lip_lead(timeline, fps)

        # Say so rather than shipping a silent mouth: a clip that never clears
        # the speech gate is almost always the wrong file, a dead channel, or a
        # recording so quiet the analyser reads it as room noise.
        if not any(max(series or [0.0]) > 0.02 for series in timeline.values()):
            result.note("The audio never rose above the speech floor, so the "
                        "mouth does not move. Check the level, or that the clip "
                        "actually contains speech.")

    emotion_timeline, matched = _emotion_timeline(request, frame_count)
    if matched:
        result.note("Emotion words recognised: %s." % ", ".join(matched))
    elif request.emotion_text.strip():
        result.note("No emotion words recognised in '%s'." % request.emotion_text)

    for slot, series in emotion_timeline.items():
        existing = timeline.get(slot)
        if existing is None:
            timeline[slot] = series
        else:
            for i in range(min(len(existing), len(series))):
                existing[i] = max(existing[i], series[i])

    if viseme_frames:
        _yield_mouth_emotion(timeline, viseme_frames, fps)
        _apply_teeth_reveal(timeline, profile)

    if request.auto_blinks:
        blinks = _apply_auto_blinks(timeline, frame_count, fps)
        result.note("Added %d automatic blink%s." % (blinks, "" if blinks == 1 else "s"))

    # Drop slots this mesh cannot drive, so the curve pass stays honest.
    unmapped = [slot for slot in timeline if not profile.has(slot)]
    for slot in unmapped:
        del timeline[slot]
    if unmapped:
        result.note("%d slot%s not present on this face and skipped: %s."
                    % (len(unmapped), "" if len(unmapped) == 1 else "s",
                       ", ".join(sorted(unmapped))))

    if not timeline:
        raise BakeError("Nothing to bake: no driven slot survived mapping onto "
                        "'%s'." % profile.mesh_name)

    # --- write ------------------------------------------------------------
    base_name = request.asset_name or ("%s_Face" % profile.mesh_name)
    anim_name = _unique_asset_name(base_name, request.asset_dir,
                                   request.keep_as_new_take)
    animation = _create_or_reuse(anim_name, request.asset_dir, skeleton, result)

    written, duration = _write_curves(animation, timeline, profile, fps,
                                      frame_count, result)

    result.animation = animation
    result.sound = sound
    result.frame_count = frame_count
    result.duration_seconds = duration
    result.curves_written = written
    return result
