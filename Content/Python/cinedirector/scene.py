# Copyright Roundtree. All Rights Reserved.
"""
Snapshot of the current editor level: actor labels, bounds, facing, and the
viewport camera. This is everything a shot-plan provider is allowed to know.

Port of FShotPlanExecutor::BuildSceneContext and the character-facing helpers in
ShotPlanExecutor.cpp. All geometry leaves here as plain (x, y, z) tuples.
"""

import unreal

from . import vec

# ---------------------------------------------------------------------------
# Bone name tables. Void/VRM, Mixamo, MetaHuman and Biped schemes, in the order
# the C++ analyzer tries them.
# ---------------------------------------------------------------------------

LEFT_EYE_BONES = ("eye_L", "Eye_L", "EyeL", "eyeL", "LeftEye", "leftEye",
                  "EyeLeft", "mixamorig:LeftEye", "J_Adj_L_FaceEye", "faceeye_L")

RIGHT_EYE_BONES = ("eye_R", "Eye_R", "EyeR", "eyeR", "RightEye", "rightEye",
                   "EyeRight", "mixamorig:RightEye", "J_Adj_R_FaceEye", "faceeye_R")

HEAD_BONES = ("head", "Head", "HEAD", "Head_M", "head_M", "J_Bip_C_Head",
              "j_bip_c_head", "mixamorig:Head", "mixamorig_Head",
              "Bip001-Head", "Bip001 Head", "Bip01 Head", "bone_head",
              "J_Head", "Face", "face")

# Eye bones that are rig plumbing, not an eyeball: a midpoint or an aim control.
# Matching these as "left" or "right" would put the face axis 90 degrees out.
_EYE_EXCLUDE = ("eyem", "master", "mastar", "aim", "look", "target", "ctrl",
                "lid", "lash", "brow", "socket")

NECK_BONES = ("neck", "Neck", "J_Bip_C_Neck", "j_bip_c_neck",
              "mixamorig:Neck", "mixamorig_Neck", "Bip001-Neck", "Bip001 Neck")

# Actor classes that are scenery plumbing rather than things a director frames.
_SKIPPED_CLASSES = (
    "WorldSettings", "Brush", "AbstractNavData", "NavMeshBoundsVolume",
    "KillZVolume", "LevelBounds", "SphereReflectionCapture",
    "BoxReflectionCapture", "PlanarReflection", "LightmassImportanceVolume",
    "HierarchicalLODVolume", "WorldPartitionMiniMapVolume",
    "PlayerStart", "DefaultPhysicsVolume", "AtmosphericFog",
)


def _to_tuple(v):
    return (float(v.x), float(v.y), float(v.z))


def _rot_tuple(r):
    return (float(r.pitch), float(r.yaw), float(r.roll))


# ---------------------------------------------------------------------------
# Bone lookups
# ---------------------------------------------------------------------------

def _bone_location(comp, bone):
    """
    World location of a bone or socket, or None.

    Note: 5.5's Python surface has no get_bone_location, and get_bone_transform
    takes a bone NAME (an int raises). Sockets are the fallback because void FBX
    exports often carry face markers as sockets rather than bones.
    """
    try:
        if comp.get_bone_index(bone) != -1:
            return _to_tuple(comp.get_bone_transform(bone).translation)
    except Exception:
        pass
    try:
        if comp.does_socket_exist(bone):
            return _to_tuple(comp.get_socket_location(bone))
    except Exception:
        pass
    return None


def _first_bone_location(comp, candidates):
    for bone in candidates:
        found = _bone_location(comp, bone)
        if found is not None:
            return found, bone
    return None, None


def _fuzzy_eye_bone(comp, side):
    """
    Scan for an eyeball bone on one side when no known name matched.

    Void and VRM exports spell these many ways (EyeL, eye.L, L_eye), and the
    skeleton usually also carries midpoint and aim controls that must not be
    mistaken for an eyeball. `side` is "l" or "r".
    """
    best = None
    best_score = -1
    for i in range(comp.get_num_bones()):
        raw = str(comp.get_bone_name(i))
        low = raw.lower()
        if "eye" not in low:
            continue
        if any(bad in low for bad in _EYE_EXCLUDE):
            continue
        # The side marker has to be a standalone token, so "eyelid" style names
        # and a stray "r" inside a word do not count.
        tokens = [t for t in low.replace(".", "_").replace("-", "_").split("_") if t]
        matched = any(t == side or t == side + "eye" or t == "eye" + side
                      for t in tokens)
        if not matched:
            # Trailing single letter, as in "EyeL".
            stripped = low.rstrip("0123456789")
            matched = stripped.endswith(side) and stripped[:-1].endswith("eye")
        if not matched:
            continue
        score = 100 - len(low)
        if score > best_score:
            best_score = score
            best = raw
    if best is None:
        return None, None
    return _bone_location(comp, best), best


def _find_eyes(comp):
    """(left, right) eye world locations, or (None, None)."""
    left, _ = _first_bone_location(comp, LEFT_EYE_BONES)
    right, _ = _first_bone_location(comp, RIGHT_EYE_BONES)
    if left is None:
        left, _ = _fuzzy_eye_bone(comp, "l")
    if right is None:
        right, _ = _fuzzy_eye_bone(comp, "r")
    return left, right


def _skeletal_components(actor):
    try:
        return list(actor.get_components_by_class(unreal.SkeletalMeshComponent))
    except Exception:
        return []


def _bone_axes(comp, bone):
    """The bone's own X/Y/Z axes in world space, for the no-eyes fallback."""
    try:
        transform = comp.get_bone_transform(bone)
    except Exception:
        return []
    rot = transform.rotation.rotator()
    fwd = vec.direction_from_rotator((rot.pitch, rot.yaw, rot.roll))
    # Right = forward rotated -90 about Z; up completes the left-handed basis.
    right = vec.normal(vec.cross(vec.UP, fwd), (0.0, 1.0, 0.0))
    up = vec.normal(vec.cross(fwd, right), vec.UP)
    return [fwd, vec.mul(fwd, -1.0), right, vec.mul(right, -1.0),
            up, vec.mul(up, -1.0)]


# ---------------------------------------------------------------------------
# Facing
# ---------------------------------------------------------------------------

def resolve_character_facing_dir(actor):
    """
    Horizontal unit vector the character's FACE points.

    Root and mesh-component yaw are often 90 or 180 degrees off the mesh on void
    and VRM exports, so the eye axis is preferred and its sign is locked with
    eyes-out-of-head. Mesh component forward is never used: it inverted front and
    back on the void characters.
    """
    if actor is None:
        return vec.FORWARD

    try:
        actor_fwd = vec.normal_2d(_to_tuple(actor.get_actor_forward_vector()))
    except Exception:
        actor_fwd = vec.ZERO

    for comp in _skeletal_components(actor):
        if comp.get_num_bones() == 0:
            continue

        eye_l, eye_r = _find_eyes(comp)
        head_loc, head_bone = _first_bone_location(comp, HEAD_BONES)
        if head_loc is None:
            head_loc, head_bone = _best_head_bone(comp)

        # Eyes sit slightly in front of the skull: the best face-out prior.
        face_out_prior = vec.ZERO
        if eye_l and eye_r and head_loc:
            midpoint = vec.lerp(eye_l, eye_r, 0.5)
            face_out_prior = vec.normal_2d(vec.sub(midpoint, head_loc))

        if eye_l and eye_r:
            right = vec.normal(vec.sub(eye_r, eye_l))
            # UE is left-handed: forward = right x up.
            fwd = vec.normal_2d(vec.cross(right, vec.UP))
            if vec.is_near_zero(fwd):
                continue
            if not vec.is_near_zero(face_out_prior):
                if vec.dot(fwd, face_out_prior) < 0.0:
                    fwd = vec.mul(fwd, -1.0)
            elif not vec.is_near_zero(actor_fwd) and vec.dot(fwd, actor_fwd) < 0.0:
                fwd = vec.mul(fwd, -1.0)
            return fwd

        # No eyes: pick the head-bone axis closest to the face-out prior.
        axis_prior = face_out_prior
        if vec.is_near_zero(axis_prior):
            axis_prior = actor_fwd if not vec.is_near_zero(actor_fwd) else vec.FORWARD

        if head_bone:
            best = axis_prior
            best_dot = -1.0e12
            for candidate in _bone_axes(comp, head_bone) + [axis_prior]:
                flat = vec.normal_2d(candidate)
                if vec.is_near_zero(flat):
                    continue
                d = vec.dot(flat, axis_prior)
                if d > best_dot:
                    best_dot = d
                    best = flat
            if not vec.is_near_zero(best):
                return best

        if not vec.is_near_zero(axis_prior):
            return axis_prior

    return actor_fwd if not vec.is_near_zero(actor_fwd) else vec.FORWARD


def _best_head_bone(comp):
    """
    Scan the skeleton for a head-ish bone when no known name matched.
    Returns (location, bone_name). The void rigs spell theirs "Head-_1_".
    """
    best_name = None
    best_score = -1
    for i in range(comp.get_num_bones()):
        raw = str(comp.get_bone_name(i))
        name = raw.lower()
        if "head" not in name:
            continue
        if any(bad in name for bad in ("end", "nub", "twist", "top", "leaf")):
            continue
        score = 100 - len(name) + (50 if name == "head" else 0)
        if score > best_score:
            best_score = score
            best_name = raw
    if best_name is None:
        return None, None
    return _bone_location(comp, best_name), best_name


def find_head_world_location(actor):
    """
    Mid-face world location, or None when the actor has no usable skeleton.

    Prefers the eye midpoint, then a neck/head blend, then the head bone. The
    result is nudged along FACE forward (not root yaw) so a close-up lands on the
    grills and nose rather than the side of the skull.
    """
    if actor is None:
        return None

    face_fwd = resolve_character_facing_dir(actor)

    for comp in _skeletal_components(actor):
        if comp.get_num_bones() == 0:
            continue

        eye_l, eye_r = _find_eyes(comp)
        if eye_l and eye_r:
            head = vec.lerp(eye_l, eye_r, 0.5)
            head = (head[0], head[1], head[2] - 4.0)
            return vec.add(head, vec.mul(face_fwd, 6.0))

        head_loc, _ = _first_bone_location(comp, HEAD_BONES)
        if head_loc is None:
            head_loc, _ = _best_head_bone(comp)
        if head_loc is None:
            continue

        neck_loc, _ = _first_bone_location(comp, NECK_BONES)
        if neck_loc is not None:
            head = vec.lerp(neck_loc, head_loc, 0.62)
        else:
            head = (head_loc[0], head_loc[1], head_loc[2] - 8.0)

        return vec.add(head, vec.mul(face_fwd, 8.0))

    return None


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def actor_bounds(actor):
    """(center, extent) of the actor's world bounds, as tuples."""
    try:
        origin, extent = actor.get_actor_bounds(False)
        return _to_tuple(origin), _to_tuple(extent)
    except Exception:
        try:
            return _to_tuple(actor.get_actor_location()), (50.0, 50.0, 50.0)
        except Exception:
            return vec.ZERO, (50.0, 50.0, 50.0)


def bounds_height_point(actor, height01):
    """Point on the actor's vertical bounds: 0 = feet, 1 = top of mesh."""
    center, extent = actor_bounds(actor)
    low = center[2] - extent[2]
    high = center[2] + extent[2]
    return (center[0], center[1], low + (high - low) * height01)


# ---------------------------------------------------------------------------
# Scene context
# ---------------------------------------------------------------------------

class ActorInfo(object):
    """One level actor the parser can match target names against."""

    __slots__ = ("actor", "label", "location", "bounds_radius", "facing")

    def __init__(self, actor, label, location, bounds_radius, facing):
        self.actor = actor
        self.label = label
        self.location = location
        self.bounds_radius = bounds_radius
        self.facing = facing          # (pitch, yaw, roll)


class SceneContext(object):
    """Everything a shot-plan provider is allowed to know about the level."""

    def __init__(self):
        self.actors = []
        self.viewport_location = vec.ZERO
        self.viewport_rotation = (0.0, 0.0, 0.0)
        # Combined bounds of everything framable, backdrops excluded, so a shot
        # can be asked for of the level rather than of one actor in it.
        self.has_level_bounds = False
        self.level_center = vec.ZERO
        self.level_extent = (500.0, 500.0, 500.0)

    def find_by_label(self, label):
        """
        Exact case-insensitive match wins; then either string containing the
        other, longest match first, which handles "the Knight" vs "Knight_BP_2".
        """
        if not label:
            return None
        wanted = label.strip().lower()

        for info in self.actors:
            if info.label.lower() == wanted:
                return info

        best = None
        best_length = 0
        for info in self.actors:
            low = info.label.lower()
            if (wanted in low or low in wanted) and len(info.label) > best_length:
                best = info
                best_length = len(info.label)
        return best


def _viewport_camera():
    try:
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        location, rotation = ues.get_level_viewport_camera_info()
        if location is not None and rotation is not None:
            return _to_tuple(location), _rot_tuple(rotation)
    except Exception:
        pass
    return (0.0, 0.0, 200.0), (0.0, 0.0, 0.0)


# Classes that wrap the whole world rather than sit in it.
_SKY_CLASSES = ("SkyAtmosphere", "SkyLight", "VolumetricCloud", "ExponentialHeightFog",
                "AtmosphericFog")

# Label fragments that mark a backdrop dome. These are matched case-insensitively
# against the whole label, so an actor called "SkyscraperLobby" is not caught.
_SKY_WORDS = ("skysphere", "sky sphere", "sky_sphere", "skydome", "sky dome",
              "sky_dome", "galaxysky", "galaxy sky", "galaxy_sky", "skybox",
              "sky box", "starfield", "star field", "bp_sky", "sm_sky")


def is_backdrop(actor, label=None, radius=None):
    """
    True for a sky sphere, galaxy dome or similar wrapper.

    These are the largest actors in almost any level: this project's sky sphere
    has a 28 km radius. Framed as a subject it would put the camera hundreds of
    kilometres out, so they are never offered as one. They are still lit and
    still render; they just cannot be the thing a shot is about.
    """
    try:
        class_name = actor.get_class().get_name()
    except Exception:
        return False
    if class_name in _SKY_CLASSES:
        return True

    if label is None:
        try:
            label = actor.get_actor_label()
        except Exception:
            label = ""
    lowered = (label or "").lower()
    if any(word in lowered for word in _SKY_WORDS):
        return True

    # A last catch for anything kilometres across that is not a landscape: at
    # that size it is a backdrop whatever it is called.
    if radius is not None and radius > 250000.0:
        return class_name != "Landscape"
    return False


def _is_framable(actor):
    try:
        class_name = actor.get_class().get_name()
    except Exception:
        return False
    if class_name in _SKIPPED_CLASSES:
        return False
    # CineDirector's own cameras are output, not subjects.
    try:
        if actor.get_actor_label().startswith("CineDirector "):
            return False
    except Exception:
        pass
    return True


def build_scene_context(max_actors=0):
    """Snapshot the current editor level for a provider."""
    context = SceneContext()
    context.viewport_location, context.viewport_rotation = _viewport_camera()

    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = eas.get_all_level_actors()
    except Exception:
        return context

    low = [None, None, None]
    high = [None, None, None]

    for actor in actors:
        if actor is None or not _is_framable(actor):
            continue
        try:
            label = actor.get_actor_label()
            center, extent = actor_bounds(actor)
            radius = max(vec.length(extent), 25.0)
            if is_backdrop(actor, label, radius):
                continue
            facing_dir = resolve_character_facing_dir(actor)
            facing = (0.0, vec.yaw_degrees(facing_dir), 0.0)
            context.actors.append(ActorInfo(actor, label, center, radius, facing))

            # Running world bounds, for "frame everything".
            for axis in range(3):
                bottom = center[axis] - extent[axis]
                top = center[axis] + extent[axis]
                low[axis] = bottom if low[axis] is None else min(low[axis], bottom)
                high[axis] = top if high[axis] is None else max(high[axis], top)
        except Exception:
            continue

    if low[0] is not None:
        context.level_center = tuple(
            (low[axis] + high[axis]) * 0.5 for axis in range(3))
        context.level_extent = tuple(
            max((high[axis] - low[axis]) * 0.5, 50.0) for axis in range(3))
        context.has_level_bounds = True

    if max_actors and len(context.actors) > max_actors:
        eye = context.viewport_location
        context.actors.sort(key=lambda i: vec.dist(i.location, eye))
        context.actors = context.actors[:max_actors]

    return context
