# Copyright Roundtree. All Rights Reserved.
"""
Level lighting per shot: sun, sky, fog and god rays.

Port of ApplyLightingTracks in ShotPlanExecutor.cpp. Time-of-day words re-key the
level's own directional light with constant-interpolation keys, so lighting snaps
at each cut instead of sliding across shots.

Two modes. With a SkyAtmosphere present the atmosphere derives sky and sun colour
from the keyed pitch, so only pitch and a multiplier on the level's existing
brightness are written and lux-scale setups keep their exposure. Without one, the
mood is faked entirely through absolute intensity and a tinted colour.
"""

import unreal

# Base sun brightness is remembered in an actor tag rather than a channel default,
# because the Python scripting channels expose keys but no default setter. Without
# this, a second run would read back the keyed night value as the new base and the
# level would get darker every time.
BASE_INTENSITY_TAG = "CineDirectorBaseIntensity"

SUN_LABEL = "CineDirector Sun"
SKY_LABEL = "CineDirector Sky Atmosphere"
SKYLIGHT_LABEL = "CineDirector Sky Light"
FOG_LABEL = "CineDirector Fog"

WHITE = (1.0, 1.0, 1.0)


class SunPreset(object):
    """
    What the sun should look like for a time-of-day word.

    `set_pitch` is false for overcast, which keeps the level's own sun angle and
    only flattens colour and intensity.
    """

    __slots__ = ("set_pitch", "pitch_deg", "color", "intensity",
                 "physical_multiplier", "physical_color")

    def __init__(self, set_pitch, pitch_deg, color, intensity,
                 physical_multiplier, physical_color):
        self.set_pitch = set_pitch
        self.pitch_deg = pitch_deg
        self.color = color
        self.intensity = intensity
        self.physical_multiplier = physical_multiplier
        self.physical_color = physical_color


SUN_PRESETS = {
    "dawn":        SunPreset(True,  -6.0, (1.00, 0.62, 0.38),  4.00, 0.80, WHITE),
    "morning":     SunPreset(True, -30.0, (1.00, 0.93, 0.82),  8.00, 1.00, WHITE),
    "noon":        SunPreset(True, -75.0, (1.00, 1.00, 1.00), 10.00, 1.00, WHITE),
    "afternoon":   SunPreset(True, -45.0, (1.00, 0.96, 0.88),  9.00, 1.00, WHITE),
    "golden_hour": SunPreset(True,  -9.0, (1.00, 0.68, 0.32),  5.00, 0.90, WHITE),
    "sunset":      SunPreset(True,  -3.0, (1.00, 0.45, 0.18),  3.00, 0.80, WHITE),
    # Sun just below the horizon: the sky and skylight carry the scene.
    "dusk":        SunPreset(True,   4.0, (0.55, 0.55, 0.75),  1.20, 0.30, (0.80, 0.82, 0.95)),
    # A cool dim moon stand-in rather than true darkness.
    "night":       SunPreset(True, -35.0, (0.45, 0.55, 0.90),  0.35, 0.02, (0.70, 0.80, 1.00)),
    "midnight":    SunPreset(True, -60.0, (0.40, 0.48, 0.85),  0.15, 0.01, (0.65, 0.75, 1.00)),
    "overcast":    SunPreset(False,  0.0, (0.82, 0.86, 0.95),  3.00, 0.35, (0.90, 0.93, 1.00)),
}


# ---------------------------------------------------------------------------
# Level helpers
# ---------------------------------------------------------------------------

def _all_actors():
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        return eas.get_all_level_actors()
    except Exception:
        return []


def _find_actor_of_class(cls):
    for actor in _all_actors():
        if isinstance(actor, cls):
            return actor
    return None


def _component_of_class(actor, cls):
    """
    An actor's component of a given class.

    ALight::GetLightComponent and AExponentialHeightFog::GetComponent are not
    exposed to Python, so go through the component list instead.
    """
    if actor is None:
        return None
    try:
        found = actor.get_components_by_class(cls)
        if found:
            return found[0]
    except Exception:
        pass
    for name in ("light_component", "component"):
        try:
            comp = actor.get_editor_property(name)
            if comp is not None:
                return comp
        except Exception:
            pass
    return None


def _light_component(actor):
    return _component_of_class(actor, unreal.LightComponent)


def _set_property_any(obj, names, value):
    """
    Set the first property that exists, and say which one worked.

    Boolean UPROPERTY names lose their "b" prefix on the way to Python, but not
    always the way you would guess, so the candidates are tried in order.
    """
    for name in names:
        try:
            obj.set_editor_property(name, value)
            return name
        except Exception:
            continue
    return None


VOLUMETRIC_FOG_PROPERTIES = ("volumetric_fog", "enable_volumetric_fog",
                             "b_enable_volumetric_fog")
LIGHT_SHAFT_PROPERTIES = ("enable_light_shaft_bloom", "light_shaft_bloom",
                          "b_enable_light_shaft_bloom")


def _find_sun():
    """
    Prefer the directional light the atmosphere already follows. With two suns in
    a level, keying the other one would change nothing visible.
    """
    first = None
    for actor in _all_actors():
        if not isinstance(actor, unreal.DirectionalLight):
            continue
        if first is None:
            first = actor
        try:
            comp = _light_component(actor)
            if comp.get_editor_property("atmosphere_sun_light"):
                return actor
        except Exception:
            pass
    return first


def _spawn(cls, label, rotation=None):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    rot = rotation or unreal.Rotator(0.0, 0.0, 0.0)
    actor = eas.spawn_actor_from_class(cls, unreal.Vector(0.0, 0.0, 0.0), rot)
    if actor is not None:
        actor.set_actor_label(label)
    return actor


def _base_intensity(sun, light_comp):
    """
    The sun's pre-CineDirector brightness, remembered across runs in an actor tag.
    """
    try:
        for tag in sun.tags:
            text = str(tag)
            if text.startswith(BASE_INTENSITY_TAG + "="):
                return float(text.split("=", 1)[1])
    except Exception:
        pass

    try:
        current = float(light_comp.get_editor_property("intensity"))
    except Exception:
        current = 10.0

    try:
        tags = list(sun.tags)
        tags.append(unreal.Name("%s=%.6f" % (BASE_INTENSITY_TAG, current)))
        sun.set_editor_property("tags", tags)
    except Exception:
        pass
    return current


# ---------------------------------------------------------------------------
# Sequence helpers
# ---------------------------------------------------------------------------

def _find_or_add_possessable(sequence, obj, display_name):
    """
    Reuse an existing binding with the same display name so re-running does not
    stack duplicate sun and fog bindings.
    """
    try:
        for binding in sequence.get_bindings():
            if str(binding.get_display_name()) == display_name:
                return binding
    except Exception:
        pass
    binding = sequence.add_possessable(obj)
    if binding is not None:
        try:
            binding.set_display_name(display_name)
        except Exception:
            pass
    return binding


def _find_or_add_property_track(binding, track_class, property_name):
    for track in binding.get_tracks():
        if isinstance(track, track_class):
            try:
                if str(track.get_property_name()) == property_name:
                    return track
            except Exception:
                return track
    track = binding.add_track(track_class)
    track.set_property_name_and_path(property_name, property_name)
    return track


def _clear_channel(channel):
    """
    Drop every existing key.

    The sun and fog are the level's own actors, so their bindings are reused
    rather than rebuilt. Without this, a re-run whose shots land on different
    frames would leave the previous run's keys interleaved with the new ones.
    """
    if channel is None:
        return
    try:
        for key in list(channel.get_keys()):
            channel.remove_key(key)
    except Exception:
        pass


def _get_or_create_section(track, start_frame, end_frame):
    """The track's first section, grown to cover the range, or a fresh one."""
    sections = track.get_sections()
    if sections:
        section = sections[0]
        try:
            current_start = unreal.MovieSceneSectionExtensions.get_start_frame(section)
            current_end = unreal.MovieSceneSectionExtensions.get_end_frame(section)
            section.set_range(int(min(current_start, start_frame)),
                              int(max(current_end, end_frame)))
        except Exception:
            pass
        return section
    section = track.add_section()
    section.set_range(int(start_frame), int(end_frame))
    return section


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply_lighting(sequence, segment_starts, start_frame, end_frame, result, add_key):
    """
    Key the sun and fog per shot.

    `segment_starts` is a list of (segment, start_frame). `add_key` is the
    executor's channel keying shim, called as add_key(channel, frame, value,
    interp) so both modules agree on the engine's signature.
    """
    want_sun = any(s.time_of_day != "unchanged" for s, _ in segment_starts)
    want_fog = any(s.fog_density >= 0.0 for s, _ in segment_starts)
    want_god_rays = any(s.god_rays for s, _ in segment_starts)
    want_volumetric = any(s.volumetric_fog for s, _ in segment_starts)

    if not (want_sun or want_fog or want_god_rays or want_volumetric):
        return

    from .executor import INTERP_CONSTANT

    sun = _find_sun()
    if sun is None and (want_sun or want_god_rays):
        sun = _spawn(unreal.DirectionalLight, SUN_LABEL, unreal.Rotator(-45.0, 0.0, 0.0))
        if sun is not None:
            result.note("No directional light in the level - spawned '%s'." % SUN_LABEL)

    physical_sky = False

    if sun is not None and want_sun:
        physical_sky = _prepare_sky(result)
        _key_sun(sequence, sun, segment_starts, start_frame, end_frame,
                 physical_sky, result, add_key, INTERP_CONSTANT)

    if sun is not None and want_god_rays:
        comp = _light_component(sun)
        if _set_property_any(comp, LIGHT_SHAFT_PROPERTIES, True):
            result.note("God rays: light-shaft bloom enabled on the sun.")
        else:
            result.note("Could not enable light-shaft bloom on the sun.")

    if want_fog or want_volumetric:
        _apply_fog(sequence, segment_starts, start_frame, end_frame,
                   want_fog, want_volumetric or (want_god_rays and want_fog),
                   result, add_key, INTERP_CONSTANT)


def _prepare_sky(result):
    """
    Make sure the physical sky stack exists. Returns True when an atmosphere is
    present, which switches the sun keys into multiplier mode.
    """
    atmosphere = _find_actor_of_class(unreal.SkyAtmosphere)
    if atmosphere is None:
        atmosphere = _spawn(unreal.SkyAtmosphere, SKY_LABEL)
        if atmosphere is not None:
            result.note("No SkyAtmosphere in the level - spawned '%s'." % SKY_LABEL)

    sky_light = _find_actor_of_class(unreal.SkyLight)
    if sky_light is None:
        sky_light = _spawn(unreal.SkyLight, SKYLIGHT_LABEL)
        if sky_light is not None:
            try:
                comp = _component_of_class(sky_light, unreal.SkyLightComponent)
                # Real-time capture keeps ambient light in step with the keyed sun,
                # so night shots go dark without a manual recapture.
                comp.set_mobility(unreal.ComponentMobility.MOVABLE)
                comp.set_editor_property("real_time_capture", True)
                result.note("No SkyLight in the level - spawned '%s' "
                            "(real-time capture)." % SKYLIGHT_LABEL)
            except Exception:
                result.note("Spawned '%s' but could not enable real-time capture."
                            % SKYLIGHT_LABEL)
    return atmosphere is not None


def _key_sun(sequence, sun, segment_starts, start_frame, end_frame,
             physical_sky, result, add_key, interp_constant):
    light_comp = _light_component(sun)

    if physical_sky:
        try:
            if not light_comp.get_editor_property("atmosphere_sun_light"):
                light_comp.set_editor_property("atmosphere_sun_light", True)
                result.note("Enabled 'Atmosphere Sun Light' on the sun so the sky "
                            "follows it.")
        except Exception:
            pass

    sun_label = sun.get_actor_label()
    sun_binding = _find_or_add_possessable(sequence, sun, sun_label)
    if sun_binding is None:
        result.note("Could not bind the sun into the sequence; lighting skipped.")
        return

    # Pitch drives elevation. Yaw and position are left alone so the light's
    # existing composition survives.
    transform_track = None
    for track in sun_binding.get_tracks():
        if isinstance(track, unreal.MovieScene3DTransformTrack):
            transform_track = track
            break
    if transform_track is None:
        transform_track = sun_binding.add_track(unreal.MovieScene3DTransformTrack)
    transform_section = _get_or_create_section(transform_track, start_frame, end_frame)

    channels = list(unreal.MovieSceneSectionExtensions.get_all_channels(transform_section))
    pitch_channel = channels[4] if len(channels) >= 9 else None

    base_intensity = _base_intensity(sun, light_comp)

    comp_name = light_comp.get_name()
    light_binding = _find_or_add_possessable(sequence, light_comp, comp_name)

    intensity_channel = None
    color_channels = []
    if light_binding is not None:
        try:
            intensity_track = _find_or_add_property_track(
                light_binding, unreal.MovieSceneFloatTrack, "Intensity")
            intensity_section = _get_or_create_section(intensity_track, start_frame, end_frame)
            found = list(unreal.MovieSceneSectionExtensions.get_all_channels(intensity_section))
            intensity_channel = found[0] if found else None
        except Exception as error:      # noqa: BLE001
            result.note("Sun intensity track skipped (%s)." % error)

        try:
            color_track = _find_or_add_property_track(
                light_binding, unreal.MovieSceneColorTrack, "LightColor")
            color_section = _get_or_create_section(color_track, start_frame, end_frame)
            color_channels = list(
                unreal.MovieSceneSectionExtensions.get_all_channels(color_section))
        except Exception as error:      # noqa: BLE001
            result.note("Sun colour track skipped (%s)." % error)

    _clear_channel(pitch_channel)
    _clear_channel(intensity_channel)
    for channel in color_channels:
        _clear_channel(channel)

    keyed = 0
    for segment, frame in segment_starts:
        preset = SUN_PRESETS.get(segment.time_of_day)
        if preset is None:
            continue

        intensity = (base_intensity * preset.physical_multiplier
                     if physical_sky else preset.intensity)
        color = preset.physical_color if physical_sky else preset.color

        if preset.set_pitch and pitch_channel is not None:
            add_key(pitch_channel, frame, float(preset.pitch_deg), interp_constant)
        if intensity_channel is not None:
            add_key(intensity_channel, frame, float(intensity), interp_constant)
        if len(color_channels) >= 4:
            add_key(color_channels[0], frame, float(color[0]), interp_constant)
            add_key(color_channels[1], frame, float(color[1]), interp_constant)
            add_key(color_channels[2], frame, float(color[2]), interp_constant)
            add_key(color_channels[3], frame, 1.0, interp_constant)
        keyed += 1

    if keyed:
        if physical_sky:
            result.note("Sun '%s': time-of-day keyed on %d shot%s "
                        "(physical sky - the atmosphere colours the light)."
                        % (sun_label, keyed, "" if keyed == 1 else "s"))
        else:
            result.note("Sun '%s': time-of-day keyed on %d shot%s "
                        "(pitch, colour, intensity)."
                        % (sun_label, keyed, "" if keyed == 1 else "s"))


def _apply_fog(sequence, segment_starts, start_frame, end_frame,
               want_fog, want_volumetric, result, add_key, interp_constant):
    fog = _find_actor_of_class(unreal.ExponentialHeightFog)
    if fog is None:
        fog = _spawn(unreal.ExponentialHeightFog, FOG_LABEL)
        if fog is not None:
            result.note("No height fog in the level - spawned '%s'." % FOG_LABEL)
    if fog is None:
        return

    fog_comp = _component_of_class(fog, unreal.ExponentialHeightFogComponent)
    if fog_comp is None:
        result.note("Found '%s' but not its fog component; density not keyed."
                    % fog.get_actor_label())
        return

    if want_volumetric:
        if _set_property_any(fog_comp, VOLUMETRIC_FOG_PROPERTIES, True):
            result.note("Volumetric fog enabled.")
        else:
            result.note("Could not enable volumetric fog on '%s'."
                        % fog.get_actor_label())

    if not want_fog:
        return

    actor_binding = _find_or_add_possessable(sequence, fog, fog.get_actor_label())
    comp_binding = _find_or_add_possessable(sequence, fog_comp, fog_comp.get_name())
    if comp_binding is None:
        result.note("Could not bind the fog component; density not keyed.")
        return

    try:
        density_track = _find_or_add_property_track(
            comp_binding, unreal.MovieSceneFloatTrack, "FogDensity")
        density_section = _get_or_create_section(density_track, start_frame, end_frame)
        channels = list(
            unreal.MovieSceneSectionExtensions.get_all_channels(density_section))
    except Exception as error:      # noqa: BLE001
        result.note("Fog density track skipped (%s)." % error)
        return

    if not channels:
        return

    _clear_channel(channels[0])

    keyed = 0
    for segment, frame in segment_starts:
        if segment.fog_density >= 0.0:
            add_key(channels[0], frame, float(segment.fog_density), interp_constant)
            keyed += 1
    if keyed:
        result.note("Fog density keyed on %d shot%s."
                    % (keyed, "" if keyed == 1 else "s"))
