# Copyright Roundtree. All Rights Reserved.
"""
Turns a ShotPlan into real editor state.

Spawns one ACineCameraActor per segment, binds it into a Level Sequence, and
authors baked transform keys, a focal-length track and camera cuts. Re-running
replaces the cameras and cut sections CineDirector made last time, so the
sequence always matches the latest prompt.

Port of FShotPlanExecutor::Execute. The Sequencer scripting API keys channels
one at a time, so transforms are baked at the sequence display rate rather than
handed to an interpolator, which is also what the C++ executor does for moves.
"""

import math

import unreal

from . import geometry, lighting, scene as scene_mod, vec

LOG = unreal.log

CAMERA_PREFIX = "CineDirector Shot"
TAKE_PREFIX = "CineDirector Take"

DEFAULT_FOCAL_MM = 35.0
DEFAULT_APERTURE = 2.8

BAKE_FPS = 30.0

# Samples per second for a baked move. Well under the display rate on purpose:
# the curve between samples is smooth, so keying every frame only bloats the
# sequence. A followed subject gets the densest sampling so the face stays framed
# through a performance.
SAMPLES_PER_SECOND_DEFAULT = 6
SAMPLES_PER_SECOND_HANDHELD = 10
SAMPLES_PER_SECOND_FOLLOW = 12
MIN_DENSE_SAMPLES = 8
MAX_DENSE_SAMPLES = 400


def _needs_dense_keys(seg):
    """A still camera needs two keys; anything that moves needs a baked curve."""
    return (seg.move != "static"
            or seg.handheld_intensity > 0.0
            or seg.follow_subject)


def _sample_count(seg, duration):
    """How many samples to bake for one segment, including both endpoints."""
    if not _needs_dense_keys(seg):
        return 2
    if seg.follow_subject:
        rate = SAMPLES_PER_SECOND_FOLLOW
    elif seg.handheld_intensity > 0.0:
        rate = SAMPLES_PER_SECOND_HANDHELD
    else:
        rate = SAMPLES_PER_SECOND_DEFAULT
    count = int(duration * rate)
    return max(MIN_DENSE_SAMPLES, min(count, MAX_DENSE_SAMPLES)) + 1


# ---------------------------------------------------------------------------
# Enum and API shims. Names moved between engine versions, so resolve once.
# ---------------------------------------------------------------------------

def _resolve_time_unit():
    for attr in ("MovieSceneTimeUnit", "SequenceTimeUnit"):
        enum = getattr(unreal, attr, None)
        if enum is not None:
            for member in ("DISPLAY_RATE", "TICK_RESOLUTION"):
                if hasattr(enum, member):
                    return getattr(enum, member)
    return None


def _resolve_interpolation(name="AUTO"):
    enum = getattr(unreal, "MovieSceneKeyInterpolation", None)
    if enum is None:
        return None
    for member in (name, "AUTO", "LINEAR", "CONSTANT"):
        if hasattr(enum, member):
            return getattr(enum, member)
    return None


TIME_UNIT = _resolve_time_unit()
INTERP_AUTO = _resolve_interpolation("AUTO")
INTERP_LINEAR = _resolve_interpolation("LINEAR")
INTERP_CONSTANT = _resolve_interpolation("CONSTANT")


def _add_key(channel, frame, value, interp=None):
    """Key a scripting channel, tolerating the signature differences in 5.5."""
    interpolation = INTERP_AUTO if interp is None else interp
    frame_number = unreal.FrameNumber(int(round(frame)))
    attempts = (
        (frame_number, value, 0.0, TIME_UNIT, interpolation),
        (frame_number, value, 0.0, TIME_UNIT),
        (frame_number, value),
    )
    last = None
    for args in attempts:
        if any(a is None for a in args):
            continue
        try:
            return channel.add_key(*args)
        except Exception as error:      # noqa: BLE001 - probing call shapes
            last = error
    raise RuntimeError("could not key channel %s: %s" % (channel.get_name(), last))


def _channels(section):
    return list(unreal.MovieSceneSectionExtensions.get_all_channels(section))


def _channel_map(section):
    """Channels keyed by their leading name, e.g. 'Location.X'."""
    out = {}
    for channel in _channels(section):
        name = str(channel.get_name())
        # Scripting names carry a trailing index: "Location.X_0".
        base = name.rsplit("_", 1)[0] if "_" in name else name
        out.setdefault(base, channel)
    return out


class ExecuteResult(object):
    def __init__(self):
        self.success = False
        self.error = ""
        self.notes = []
        self.num_shots = 0
        self.total_duration_seconds = 0.0
        self.sequence_path = ""
        self.camera_labels = []

    def note(self, text):
        self.notes.append(text)

    def describe(self):
        head = ("Authored %d shot%s, %.1fs total."
                % (self.num_shots, "" if self.num_shots == 1 else "s",
                   self.total_duration_seconds)) if self.success else ("Failed: " + self.error)
        return "\n".join([head] + ["  " + n for n in self.notes])


# ---------------------------------------------------------------------------
# Subject sampling
# ---------------------------------------------------------------------------

def _sample_actor(actor):
    """Freeze one actor into the shape geometry.py expects."""
    if actor is None:
        return None
    center, extent = scene_mod.actor_bounds(actor)
    try:
        location = (float(actor.get_actor_location().x),
                    float(actor.get_actor_location().y),
                    float(actor.get_actor_location().z))
    except Exception:
        location = center
    head = scene_mod.find_head_world_location(actor)
    facing = scene_mod.resolve_character_facing_dir(actor)
    return geometry.SubjectSample(center, extent, location, head, facing)


def _level_sample(scene, result=None):
    """The whole scene as a subject, for 'frame everything'."""
    if scene is None or not getattr(scene, "has_level_bounds", False):
        if result is not None:
            result.note("Nothing framable in the level, so 'everything' fell "
                        "back to the viewport.")
        return None
    center = scene.level_center
    extent = scene.level_extent
    if result is not None:
        result.note("Framing the whole level: %.0f x %.0f x %.0f m."
                    % (extent[0] * 2 / 100.0, extent[1] * 2 / 100.0,
                       extent[2] * 2 / 100.0))
    # No head and no facing: the level does not look anywhere, so view sides
    # fall back to viewport-relative, which is what "from the left" should mean
    # for a landscape anyway.
    return geometry.SubjectSample(center, extent, center, None, None)


def _bind_segment_actors(seg, scene):
    """
    Resolve a segment's labels against the level, noting anything missing.
    Returns (target_actor, look_at_actor, rack_focus_actor).
    """
    def resolve(label, role):
        if not label:
            return None
        info = scene.find_by_label(label)
        if info is None:
            seg.notes.append('No actor named "%s" in the level - %s ignored.' % (label, role))
            return None
        return info.actor

    return (resolve(seg.target, "subject"),
            resolve(seg.look_at, "look-at target"),
            resolve(seg.rack_focus_to, "rack focus target"))


# ---------------------------------------------------------------------------
# Camera setup
# ---------------------------------------------------------------------------

def _handheld_offset(intensity, seconds, seed):
    """
    Deterministic operator wobble. Three detuned sines per axis read as breathing
    rather than vibration, and the same seed always bakes the same take.
    """
    if intensity <= 0.0:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)

    def wave(freqs, phase):
        return sum(math.sin(seconds * f + phase + seed * 1.7) / (i + 1.0)
                   for i, f in enumerate(freqs))

    amp = intensity * 1.6
    pos = (wave((1.7, 3.1, 5.3), 0.0) * amp,
           wave((1.3, 2.9, 4.7), 1.1) * amp,
           wave((2.1, 3.7, 6.1), 2.3) * amp * 0.7)
    rot_amp = intensity * 0.45
    rot = (wave((1.9, 3.3), 0.7) * rot_amp,
           wave((1.5, 2.7), 1.9) * rot_amp,
           wave((1.1, 2.3), 3.1) * rot_amp * 0.5)
    return pos, rot


def _apply_lens(lens, seg, focus_actor, focus_distance_cm, size):
    """Focal length, aperture, focus method and post-process for one shot."""
    if lens is None:
        return

    focal = seg.focal_length_mm if seg.focal_length_mm > 0.0 else DEFAULT_FOCAL_MM
    aperture = seg.aperture if seg.aperture > 0.0 else DEFAULT_APERTURE
    try:
        lens.set_editor_property("current_focal_length", float(focal))
        lens.set_editor_property("current_aperture", float(aperture))
    except Exception:
        pass

    if seg.filmback_sensor_height_mm > 0.0 and seg.filmback_sensor_width_mm > 0.0:
        try:
            filmback = lens.get_editor_property("filmback")
            filmback.set_editor_property("sensor_width", float(seg.filmback_sensor_width_mm))
            filmback.set_editor_property("sensor_height", float(seg.filmback_sensor_height_mm))
            lens.set_editor_property("filmback", filmback)
        except Exception:
            pass

    # Focus. Tracking follows the subject; deep and fixed hold a manual plane.
    try:
        focus = lens.get_editor_property("focus_settings")
        if seg.deep_focus:
            focus.set_editor_property("focus_method", unreal.CameraFocusMethod.MANUAL)
            focus.set_editor_property("manual_focus_distance", 100000.0)
        elif seg.fixed_focus:
            focus.set_editor_property("focus_method", unreal.CameraFocusMethod.MANUAL)
            focus.set_editor_property("manual_focus_distance", max(float(focus_distance_cm), 10.0))
        elif seg.track_focus and focus_actor is not None:
            focus.set_editor_property("focus_method", unreal.CameraFocusMethod.TRACKING)
            tracking = focus.get_editor_property("tracking_focus_settings")
            tracking.set_editor_property("actor_to_track", focus_actor)
            offset = _tracking_focus_offset(focus_actor, size)
            tracking.set_editor_property("relative_offset", unreal.Vector(*offset))
            focus.set_editor_property("tracking_focus_settings", tracking)
        else:
            focus.set_editor_property("focus_method", unreal.CameraFocusMethod.MANUAL)
            focus.set_editor_property("manual_focus_distance", max(float(focus_distance_cm), 10.0))
        focus.set_editor_property("smooth_focus_changes", False)
        focus.set_editor_property("focus_offset", 0.0)
        lens.set_editor_property("focus_settings", focus)
    except Exception:
        pass

    _apply_post_process(lens, seg)


def _tracking_focus_offset(actor, size):
    """Offset from actor origin for DOF: the face on a close-up, framing point otherwise."""
    sample = _sample_actor(actor)
    if sample is None:
        return (0.0, 0.0, 0.0)
    if size in ("extreme_close_up", "close_up"):
        interest = sample.face_point
    else:
        interest = geometry.resolve_subject_framing(sample, size).point
    return vec.sub(interest, sample.location)


def _apply_post_process(lens, seg):
    """Grain, vignette, fringe, bloom, flare and the optional colour grade."""
    try:
        pp = lens.get_editor_property("post_process_settings")
    except Exception:
        return

    def override(flag, value_name, value):
        try:
            pp.set_editor_property(flag, True)
            pp.set_editor_property(value_name, value)
        except Exception:
            pass

    if seg.film_grain > 0.0:
        override("override_film_grain_intensity", "film_grain_intensity", seg.film_grain)
    if seg.vignette > 0.0:
        override("override_vignette_intensity", "vignette_intensity", seg.vignette)
    if seg.chromatic_aberration > 0.0:
        override("override_scene_fringe_intensity", "scene_fringe_intensity",
                 seg.chromatic_aberration)
    if seg.bloom > 0.0:
        override("override_bloom_intensity", "bloom_intensity", seg.bloom)
    if seg.lens_flare > 0.0:
        override("override_lens_flare_intensity", "lens_flare_intensity", seg.lens_flare)

    look = seg.look
    if look.apply_grade:
        sat = vec.clamp(look.saturation, 0.0, 2.0)
        con = vec.clamp(look.contrast, 0.2, 2.0)
        gain = vec.clamp(look.gain, 0.2, 2.0)
        override("override_color_saturation", "color_saturation", unreal.Vector4(sat, sat, sat, 1.0))
        override("override_color_contrast", "color_contrast", unreal.Vector4(con, con, con, 1.0))
        override("override_color_gain", "color_gain", unreal.Vector4(gain, gain, gain, 1.0))
        if look.white_temp_kelvin > 0.0:
            override("override_white_temp", "white_temp",
                     vec.clamp(look.white_temp_kelvin, 1500.0, 15000.0))
        if abs(look.white_tint) > 1e-4:
            override("override_white_tint", "white_tint", vec.clamp(look.white_tint, -1.0, 1.0))

    if look.motion_blur >= 0.0:
        override("override_motion_blur_amount", "motion_blur_amount", look.motion_blur)

    try:
        lens.set_editor_property("post_process_settings", pp)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sequence plumbing
# ---------------------------------------------------------------------------

def _binding_resolves(binding):
    """True when the binding still points at a live object in the open world."""
    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
    except Exception:
        world = None
    try:
        return bool(unreal.MovieSceneBindingExtensions.get_objects(binding, world))
    except Exception:
        pass
    try:
        return bool(binding.get_bound_objects())
    except Exception:
        # Unknown either way: treat as live so nothing is swept by mistake.
        return True


def _clear_previous(sequence, result):
    """Drop the cameras and cut sections CineDirector authored last time."""
    removed_bindings = 0
    try:
        bindings = sequence.get_bindings()
    except Exception:
        bindings = []

    for binding in bindings:
        try:
            name = str(binding.get_display_name())
        except Exception:
            continue

        ours = name.startswith(CAMERA_PREFIX) or name.startswith(TAKE_PREFIX)

        # Lens bindings from before they were renamed are left behind as
        # "CameraComponent" pointing at a destroyed actor. Only unbound ones are
        # swept, so a camera the user added themselves is never touched.
        if not ours and name == "CameraComponent":
            try:
                ours = not binding.get_object_template() and not _binding_resolves(binding)
            except Exception:
                ours = False

        if ours:
            try:
                binding.remove()
                removed_bindings += 1
            except Exception:
                pass

    removed_cuts = 0
    for track in list(sequence.get_tracks()):
        if isinstance(track, unreal.MovieSceneCameraCutTrack):
            try:
                sequence.remove_track(track)
                removed_cuts += 1
            except Exception:
                pass

    # Destroy the orphaned camera actors themselves.
    destroyed = 0
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for actor in eas.get_all_level_actors():
            try:
                label = actor.get_actor_label()
            except Exception:
                continue
            if label.startswith(CAMERA_PREFIX) or label.startswith(TAKE_PREFIX):
                eas.destroy_actor(actor)
                destroyed += 1
    except Exception:
        pass

    if removed_bindings or destroyed or removed_cuts:
        result.note("Replaced %d previous camera%s and %d cut track%s."
                    % (destroyed, "" if destroyed == 1 else "s",
                       removed_cuts, "" if removed_cuts == 1 else "s"))


def _resolve_sequence(explicit):
    """The sequence to author into: the caller's, else the one open in Sequencer."""
    if explicit is not None:
        return explicit
    try:
        current = unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
        if current is not None:
            return current
    except Exception:
        pass
    return None


def _spawn_camera(label, position, rotation):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    camera = eas.spawn_actor_from_class(
        unreal.CineCameraActor,
        unreal.Vector(*position),
        unreal.Rotator(rotation[0], rotation[1], rotation[2]))
    if camera is not None:
        camera.set_actor_label(label)
    return camera


def _bake_transform(section, samples, fps):
    """Write baked position and rotation keys, keeping yaw continuous."""
    channels = _channel_map(section)
    needed = ["Location.X", "Location.Y", "Location.Z",
              "Rotation.X", "Rotation.Y", "Rotation.Z"]
    for name in needed:
        if name not in channels:
            raise RuntimeError("transform section is missing channel " + name)

    prev_rot = None
    for frame, position, rotation in samples:
        if prev_rot is not None:
            # Unwind so a yaw crossing 180 degrees does not spin the long way.
            rotation = (
                vec.unwind_towards(rotation[0], prev_rot[0]),
                vec.unwind_towards(rotation[1], prev_rot[1]),
                vec.unwind_towards(rotation[2], prev_rot[2]),
            )
        prev_rot = rotation

        _add_key(channels["Location.X"], frame, float(position[0]))
        _add_key(channels["Location.Y"], frame, float(position[1]))
        _add_key(channels["Location.Z"], frame, float(position[2]))
        # Sequencer's Rotation.X/Y/Z are roll/pitch/yaw in that order.
        _add_key(channels["Rotation.X"], frame, float(rotation[2]))
        _add_key(channels["Rotation.Y"], frame, float(rotation[0]))
        _add_key(channels["Rotation.Z"], frame, float(rotation[1]))


def _add_focal_track(sequence, lens, start_frame, end_frame, keys, camera_label):
    """
    Key CurrentFocalLength on the camera component binding.

    The binding is renamed after its camera ("CineDirector Shot 1 Lens") so that
    clearing a previous run catches it by prefix. Left as the default
    "CameraComponent" it survives as an orphan pointing at a destroyed actor, and
    a second run stacks another one beside it.
    """
    if not keys:
        return None
    try:
        binding = sequence.add_possessable(lens)
        try:
            binding.set_display_name("%s Lens" % camera_label)
        except Exception:
            pass
        track = binding.add_track(unreal.MovieSceneFloatTrack)
        track.set_property_name_and_path("CurrentFocalLength", "CurrentFocalLength")
        section = track.add_section()
        section.set_range(int(start_frame), int(end_frame))
        channels = _channels(section)
        if not channels:
            return None
        for frame, value in keys:
            _add_key(channels[0], frame, float(value))
        return binding
    except Exception as error:      # noqa: BLE001
        LOG("CineDirector: focal length track skipped (%s)" % error)
        return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute(plan, scene=None, sequence=None):
    """
    Author `plan` into a Level Sequence. Returns an ExecuteResult.

    `sequence` defaults to whatever is open in Sequencer. `scene` defaults to a
    fresh snapshot of the current level.
    """
    result = ExecuteResult()

    if not plan.segments:
        result.error = "The plan contained no shots."
        return result

    target_sequence = _resolve_sequence(sequence)
    if target_sequence is None:
        result.error = ("No Level Sequence is open. Open one in Sequencer, or pass "
                        "a sequence to execute().")
        return result

    if scene is None:
        scene = scene_mod.build_scene_context()

    result.sequence_path = target_sequence.get_path_name()

    display_rate = target_sequence.get_display_rate()
    fps = float(display_rate.numerator) / float(max(display_rate.denominator, 1))
    if fps <= 0.0:
        fps = BAKE_FPS

    with unreal.ScopedEditorTransaction("CineDirector: author shots"):
        _clear_previous(target_sequence, result)

        try:
            start_frame = int(target_sequence.get_playback_start())
        except Exception:
            start_frame = 0

        cut_track = None
        if plan.create_camera_cuts:
            cut_track = target_sequence.add_track(unreal.MovieSceneCameraCutTrack)

        cursor_frame = start_frame
        segment_starts = []          # (segment, first frame) for the lighting pass
        continuous = plan.one_continuous_shot
        take_camera = None
        take_binding = None
        take_lens = None
        take_samples = []
        take_focal_keys = []
        prev_pos = None
        prev_rot = None

        for index, seg in enumerate(plan.segments):
            target_actor, look_at_actor, rack_actor = _bind_segment_actors(seg, scene)
            target_sample = _sample_actor(target_actor)
            look_at_sample = _sample_actor(look_at_actor)

            if seg.frame_level and target_sample is None:
                # "Everything" is a subject with bounds but no actor: the whole
                # scene stands in for one, so framing, angle and moves all work
                # against it exactly as they would against a mesh.
                target_sample = _level_sample(scene, result)

            size = geometry.effective_shot_size(seg)

            if continuous and prev_pos is not None:
                geo = geometry.compute_geometry_chained(
                    seg, target_sample, look_at_sample, prev_pos, prev_rot)
            else:
                geo = geometry.compute_geometry(
                    seg, target_sample, look_at_sample,
                    scene.viewport_location, scene.viewport_rotation)

            amount = geometry.default_move_amount(seg, geo)
            duration = max(seg.duration_seconds, 1.0 / fps)
            frame_count = max(int(round(duration * fps)), 1)
            sample_count = _sample_count(seg, duration)

            base_focal = seg.focal_length_mm if seg.focal_length_mm > 0.0 else DEFAULT_FOCAL_MM

            samples = []
            focal_keys = []
            for step in range(sample_count):
                alpha = step / float(sample_count - 1)
                position, aim, focal_scale = geometry.sample_move(seg, geo, alpha, amount)

                seconds = alpha * duration
                pos_shake, rot_shake = _handheld_offset(
                    seg.handheld_intensity, seconds, index + 1)
                position = vec.add(position, pos_shake)

                if seg.look_at_target or geo.has_look_at:
                    rotation = vec.rotator_from_direction(vec.sub(aim, position))
                else:
                    rotation = vec.rotator_from_direction(
                        vec.sub(geo.target_point, position))

                rotation = (rotation[0] + rot_shake[0],
                            rotation[1] + rot_shake[1],
                            rotation[2] + seg.dutch_angle_deg + rot_shake[2])

                frame = cursor_frame + int(round(alpha * frame_count))
                samples.append((frame, position, rotation))
                if abs(focal_scale - 1.0) > 1e-6 or step == 0:
                    focal_keys.append((frame, base_focal * focal_scale))

            end_frame = cursor_frame + frame_count

            # Focus distance for the manual modes, measured at the opening frame.
            focus_actor = look_at_actor or target_actor
            focus_point = geo.aim_point if (geo.has_look_at or geo.has_target) else geo.target_point
            focus_distance = vec.dist(samples[0][1], focus_point)

            if continuous:
                if take_camera is None:
                    label = "%s 1" % TAKE_PREFIX
                    take_camera = _spawn_camera(label, samples[0][1], samples[0][2])
                    if take_camera is None:
                        result.error = "Could not spawn a cine camera actor."
                        return result
                    take_lens = take_camera.get_cine_camera_component()
                    take_binding = target_sequence.add_possessable(take_camera)
                    take_binding.set_display_name(label)
                    result.camera_labels.append(label)
                _apply_lens(take_lens, seg, focus_actor, focus_distance, size)
                take_samples.extend(samples)
                take_focal_keys.extend(focal_keys)
            else:
                label = "%s %d" % (CAMERA_PREFIX, index + 1)
                camera = _spawn_camera(label, samples[0][1], samples[0][2])
                if camera is None:
                    result.error = "Could not spawn a cine camera actor."
                    return result
                lens = camera.get_cine_camera_component()
                _apply_lens(lens, seg, focus_actor, focus_distance, size)

                binding = target_sequence.add_possessable(camera)
                binding.set_display_name(label)
                result.camera_labels.append(label)

                transform_section = binding.add_track(
                    unreal.MovieScene3DTransformTrack).add_section()
                transform_section.set_range(int(cursor_frame), int(end_frame))
                _bake_transform(transform_section, samples, fps)

                _add_focal_track(target_sequence, lens, cursor_frame, end_frame,
                                 focal_keys, label)

                if cut_track is not None:
                    cut_section = cut_track.add_section()
                    cut_section.set_range(int(cursor_frame), int(end_frame))
                    binding_id = unreal.MovieSceneObjectBindingID()
                    binding_id.set_editor_property("guid", binding.get_id())
                    cut_section.set_camera_binding_id(binding_id)

            segment_starts.append((seg, cursor_frame))
            prev_pos = samples[-1][1]
            prev_rot = samples[-1][2]
            cursor_frame = end_frame

            result.note("%d. %s | %s | %s | %.1fs%s"
                        % (index + 1,
                           seg.target or "(viewport)",
                           seg.move,
                           size,
                           duration,
                           (" | %s" % seg.style_kit_name) if seg.style_kit_name else ""))
            for note in seg.notes:
                result.note("   note: " + note)

        # One continuous take: everything went onto a single camera and section.
        if continuous and take_camera is not None:
            transform_section = take_binding.add_track(
                unreal.MovieScene3DTransformTrack).add_section()
            transform_section.set_range(int(start_frame), int(cursor_frame))
            _bake_transform(transform_section, take_samples, fps)
            _add_focal_track(target_sequence, take_lens, start_frame, cursor_frame,
                             take_focal_keys, "%s 1" % TAKE_PREFIX)
            if cut_track is not None:
                cut_section = cut_track.add_section()
                cut_section.set_range(int(start_frame), int(cursor_frame))
                binding_id = unreal.MovieSceneObjectBindingID()
                binding_id.set_editor_property("guid", take_binding.get_id())
                cut_section.set_camera_binding_id(binding_id)

        # Sun, sky, fog and god rays, keyed per shot with constant interpolation
        # so the lighting snaps at each cut rather than sliding between shots.
        try:
            lighting.apply_lighting(target_sequence, segment_starts,
                                    start_frame, cursor_frame, result, _add_key)
        except Exception as error:      # noqa: BLE001
            result.note("Lighting pass failed (%s)." % error)

        # Grow the playback range so every shot is inside it.
        try:
            if int(target_sequence.get_playback_end()) < cursor_frame:
                target_sequence.set_playback_end(int(cursor_frame))
        except Exception:
            pass

    result.success = True
    result.num_shots = len(plan.segments)
    result.total_duration_seconds = plan.total_seconds
    return result
