# Copyright Roundtree. All Rights Reserved.
"""
Movie Render Queue launcher.

Port of CineRenderLauncher.cpp. Queues the Level Sequence open in Sequencer and
starts an in-editor render. MP4 goes out through Movie Render Queue's command
line encoder, which needs ffmpeg on the machine.
"""

import glob
import os
import shutil
import subprocess
import sys
import threading

import unreal

from . import settings as cd_settings

PNG = "png"
JPEG = "jpeg"
EXR = "exr"
BMP = "bmp"
MP4 = "mp4"

FORMATS = (PNG, JPEG, EXR, BMP, MP4)

QUALITY_LOW, QUALITY_MED, QUALITY_HIGH, QUALITY_EPIC = 0, 1, 2, 3

# Windows players and most browsers refuse libx264 output that comes out as
# yuv444p / "High 4:4:4 Predictive", which is what the default settings pick when
# encoding from PNG. Forcing 4:2:0 and the High profile keeps the file playable,
# and faststart puts the index up front so it streams. -shortest keeps audio and
# video aligned when the wav and the frame sequence differ by a few samples.
_FFMPEG_COMPAT = (" -pix_fmt yuv420p -profile:v high -level 4.2"
                  " -threads 2 -movflags +faststart -b:a 192k -shortest")

_ENCODE_SETTINGS = {
    "encode_settings_low": "-crf 28" + _FFMPEG_COMPAT,
    "encode_settings_med": "-crf 23" + _FFMPEG_COMPAT,
    "encode_settings_high": "-crf 20" + _FFMPEG_COMPAT,
    "encode_settings_epic": "-crf 16" + _FFMPEG_COMPAT,
}

_COMMAND_LINE_FORMAT = ('-hide_banner -y -loglevel error {AdditionalLocalArgs} '
                        '{VideoInputs} {AudioInputs} -acodec {AudioCodec} '
                        '-vcodec {VideoCodec} {Quality} "{OutputPath}"')


class RenderOptions(object):
    """What the user picked in the panel's Render section."""

    def __init__(self):
        self.width = 1920
        self.height = 1080
        self.format = MP4
        self.encode_quality = QUALITY_HIGH
        # Anti-aliasing accumulation. 1/1 is draft, engine AA only. Higher
        # temporal counts add true sub-frame motion blur, and cost render time.
        self.temporal_samples = 1
        self.spatial_samples = 1
        # Empty uses Movie Render Queue's default, {project}/Saved/MovieRenders.
        self.output_directory = ""
        # Optional short range for scripted renders; the panel uses full range.
        self.start_frame = None
        self.end_frame = None


class RenderError(Exception):
    """Raised when a render cannot be started, with a message fit for the panel."""


# ---------------------------------------------------------------------------
# ffmpeg discovery: encoder settings, then PATH, then the WinGet layout.
# ---------------------------------------------------------------------------

def find_ffmpeg():
    """Absolute path to ffmpeg, or an empty string."""
    settings = _encoder_settings()
    if settings is not None:
        try:
            configured = str(settings.get_editor_property("executable_path") or "")
            if configured and os.path.isfile(configured):
                return configured
        except Exception:
            pass

    on_path = shutil.which("ffmpeg")
    if on_path and os.path.isfile(on_path):
        return on_path

    # Windows: `where` finds shims that shutil.which can miss.
    try:
        completed = subprocess.run(["where", "ffmpeg"], capture_output=True,
                                   text=True, timeout=10)
        if completed.returncode == 0:
            for row in completed.stdout.splitlines():
                candidate = row.strip()
                if candidate and os.path.isfile(candidate):
                    return candidate
    except Exception:
        pass

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        pattern = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages",
                               "*", "**", "ffmpeg.exe")
        matches = glob.glob(pattern, recursive=True)
        for candidate in matches:
            if "Gyan.FFmpeg" in candidate:
                return candidate
        if matches:
            return matches[0]

    return ""


def _encoder_settings():
    try:
        return unreal.MoviePipelineCommandLineEncoderSettings.get_default_object()
    except Exception:
        return None


def _configure_ffmpeg(ffmpeg_path):
    """Point the project's CLI encoder at ffmpeg and force playable output."""
    settings = _encoder_settings()
    if settings is None:
        raise RenderError("Movie Render Queue's command line encoder settings are "
                          "unavailable, so MP4 cannot be configured.")

    def put(name, value):
        try:
            settings.set_editor_property(name, value)
        except Exception:
            pass

    put("executable_path", ffmpeg_path)
    put("video_codec", "libx264")
    put("audio_codec", "aac")
    put("output_file_extension", "mp4")
    for name, value in _ENCODE_SETTINGS.items():
        put(name, value)

    # Keep the stock template unless it is missing the quality token, which is
    # where the pixel format and profile arguments get injected.
    try:
        current = str(settings.get_editor_property("command_line_format") or "")
        if "{Quality}" not in current:
            put("command_line_format", _COMMAND_LINE_FORMAT)
    except Exception:
        put("command_line_format", _COMMAND_LINE_FORMAT)

    # Persist so the stock Movie Render Queue UI benefits too.
    for method in ("try_update_default_config_file", "save_config"):
        try:
            getattr(settings, method)()
            break
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Output passes
# ---------------------------------------------------------------------------

def _load_class(path):
    try:
        return unreal.load_class(None, path)
    except Exception:
        return None


def _add_wave_output(config, notes):
    """
    Render the Sequencer audio to a wav alongside the frames.

    Without this an image-sequence render produces no audio at all, so a PNG
    render that is later encoded to MP4 comes out silent with nothing to
    recover. The wav costs almost nothing and sits next to the frames, where
    encode_folder picks it up.
    """
    wave = getattr(unreal, "MoviePipelineWaveOutput", None)
    if wave is None:
        notes.append("Wave Output unavailable, so this render has no audio.")
        return False
    config.find_or_add_setting_by_class(wave)
    return True


def _add_output_setting(config, fmt, notes):
    """Add the image or video output pass for the chosen format."""
    if fmt == JPEG:
        config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_JPG)
        return
    if fmt == BMP:
        config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_BMP)
        return
    if fmt == EXR:
        exr = getattr(unreal, "MoviePipelineImageSequenceOutput_EXR", None)
        if exr is None:
            exr = _load_class("/Script/MovieRenderPipelineRenderPasses."
                              "MoviePipelineImageSequenceOutput_EXR")
        if exr is not None:
            config.find_or_add_setting_by_class(exr)
            return
        notes.append("EXR output class not found; rendering PNG instead.")
        config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
        return
    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)


def _add_mp4_settings(config, quality, notes):
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        raise RenderError(
            "MP4 needs ffmpeg and it was not found. Install it with "
            "\"winget install Gyan.FFmpeg\", or set the path in Project Settings "
            "under Movie Pipeline CLI Encoder.")

    _configure_ffmpeg(ffmpeg_path)

    # PNG frames for the video, plus a wav from the Sequencer audio. The encoder
    # looks for the audio pass and muxes it; without Wave Output, ffmpeg only
    # ever sees images and every MP4 comes out silent.
    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    if not _add_wave_output(config, notes):
        raise RenderError("MP4 with audio requires MoviePipelineWaveOutput.")

    # Unreal's CLI encoder waits inside finalization while ffmpeg encodes. On
    # long jobs this can stall the editor and prevent its source cleanup callback.
    # The standalone worker starts only after MRQ finishes its PNG/WAV passes.
    notes.append("MP4 via external ffmpeg at %s (Sequencer audio to AAC)."
                 % ffmpeg_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _close_sequencer():
    """
    Shut the Sequencer window before handing the job to Movie Render Queue.

    MRQ renders inside a PIE session. With the same sequence still open in the
    editor, the editor's own Sequencer keeps evaluating its bindings while that
    PIE world is torn down, which trips
    `ensure(UE::GetPlayInEditorID() == INDEX_NONE)` and can follow it with a pure
    virtual call inside MovieSceneTracks, taking the editor with it.

    Closing it costs nothing: the sequence asset is untouched and reopens from
    the Content Browser.
    """
    try:
        library = unreal.LevelSequenceEditorBlueprintLibrary
        if library.get_current_level_sequence() is None:
            return False
        library.close_level_sequence()
        return True
    except Exception as error:              # noqa: BLE001
        unreal.log_warning("CineDirector: could not close Sequencer before the "
                           "render (%s). Close it by hand if the editor crashes "
                           "when the render finishes." % error)
        return False


_ACTIVE_AUDIO_RESTORES = []


_ACTIVE_EXECUTORS = []


def _launch_external_encode(directory, sequence_name, ffmpeg_path,
                            expected_frames, fps, quality):
    """Run video encoding outside Unreal after all render files settle."""
    if not directory:
        directory = os.path.join(unreal.Paths.project_saved_dir(), "MovieRenders")
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    bundled_python = os.path.join(unreal.Paths.engine_dir(), "Binaries",
                                  "ThirdParty", "Python3", "Win64", "python.exe")
    python = (bundled_python if os.path.isfile(bundled_python) else
              sys.executable if os.path.basename(sys.executable).lower() in
              ("python.exe", "pythonw.exe") else
              shutil.which("pythonw") or shutil.which("python"))
    ffprobe = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
    if not python or not os.path.isfile(python) or not os.path.isfile(ffprobe):
        unreal.log_warning("CineDirector: external MP4 encoder unavailable "
                           "(Python or ffprobe missing).")
        return False
    helper = os.path.join(os.path.dirname(__file__), "render_cleanup.py")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([python, helper, directory, sequence_name, ffprobe,
                      str(expected_frames), ffmpeg_path, str(fps),
                      str(quality)],
                     creationflags=flags, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True


def _flatten_render_audio(sequence):
    """Make spatialized sequence audio audible to this ODK build's MRQ WAV pass.

    MRQ records silence for sounds with attenuation settings in ODK 5.5. The
    changes live only in editor memory and are restored when the executor ends.
    """
    original = []
    visited_sequences = set()
    visited_sounds = set()
    try:
        dirty_before = {p.get_name() for p in
                        unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    except Exception:
        dirty_before = set()

    def visit(current):
        path = current.get_path_name()
        if path in visited_sequences:
            return
        visited_sequences.add(path)
        tracks = list(current.get_tracks())
        for binding in current.get_bindings():
            tracks.extend(binding.get_tracks())
        for track in tracks:
            for section in track.get_sections():
                if isinstance(track, unreal.MovieSceneAudioTrack):
                    try:
                        sound = section.get_editor_property("sound")
                        if sound is None or sound.get_path_name() in visited_sounds:
                            continue
                        visited_sounds.add(sound.get_path_name())
                        attenuation = sound.get_editor_property("attenuation_settings")
                        if attenuation is not None:
                            was_dirty = sound.get_outermost().get_name() in dirty_before
                            original.append((sound, attenuation, was_dirty))
                            sound.set_editor_property("attenuation_settings", None)
                    except Exception as error:
                        unreal.log_warning("CineDirector: audio override failed: %s" % error)
                elif isinstance(track, unreal.MovieSceneSubTrack):
                    child = section.get_sequence()
                    if child is not None:
                        visit(child)

    visit(sequence)
    return original


def _restore_render_audio(original):
    asset_subsystem = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
    for sound, attenuation, was_dirty in original:
        try:
            sound.set_editor_property("attenuation_settings", attenuation)
            if not was_dirty:
                asset_subsystem.set_dirty_flag(sound, False)
        except Exception as error:
            unreal.log_error("CineDirector: could not restore %s audio: %s"
                             % (sound.get_name(), error))


def build_job(options, sequence=None, dry_run=False):
    """
    Queue a render job and return (job, notes).

    With dry_run the queue is built but not started, which is how this is tested
    without spinning up a play-in-editor session.
    """
    notes = []

    if sequence is None:
        try:
            sequence = unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
        except Exception:
            sequence = None
    if sequence is None:
        raise RenderError("No Level Sequence is open in Sequencer. Create some "
                          "shots first.")

    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
    except Exception:
        world = None
    if world is None:
        raise RenderError("No editor world available.")

    # Movie Render Queue loads both the map and the sequence by asset path, so
    # neither can be an unsaved in-memory package.
    world_package = world.get_outer().get_name() if hasattr(world, "get_outer") else ""
    try:
        world_package = world.get_outermost().get_name()
    except Exception:
        pass
    if world_package.startswith("/Temp/") or "Untitled" in world_package:
        raise RenderError("Save your level first. Movie Render Queue loads the map "
                          "from disk.")

    try:
        sequence_package = sequence.get_outermost().get_name()
    except Exception:
        sequence_package = ""
    if sequence_package.startswith("/Temp/"):
        raise RenderError("Save the Level Sequence asset first. Movie Render Queue "
                          "loads it from disk.")

    try:
        subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    except Exception:
        subsystem = None
    if subsystem is None:
        raise RenderError("Movie Render Queue is unavailable. Enable the Movie "
                          "Render Queue plugin and restart the editor.")
    if subsystem.is_rendering():
        raise RenderError("A render is already in progress.")

    # No folder picked: the project's render folders from CineDirector's settings, if set. A folder for
    # the sequence's content path first (longest prefix wins), else the root with a subfolder per sequence.
    if not options.output_directory:
        values = cd_settings.load()
        sequence_path = sequence.get_path_name()
        by_path = values.get("render_output_by_path") or {}
        for prefix in sorted(by_path, key=len, reverse=True):
            if by_path[prefix] and sequence_path.startswith(prefix):
                options.output_directory = str(by_path[prefix])
                break
        else:
            root = str(values.get("render_output_directory") or "").strip()
            if root:
                options.output_directory = os.path.join(root, sequence.get_name())

    queue = subsystem.get_queue()
    queue.delete_all_jobs()

    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.set_editor_property("sequence", unreal.SoftObjectPath(sequence.get_path_name()))
    job.set_editor_property("map", unreal.SoftObjectPath(world_package))
    job.set_editor_property("job_name", sequence.get_name())

    config = job.get_configuration()
    config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)

    fmt = options.format if options.format in FORMATS else PNG
    if fmt == MP4:
        _add_mp4_settings(config, options.encode_quality, notes)
    else:
        _add_output_setting(config, fmt, notes)
        # Image sequences carry no sound of their own, so the wav is written
        # beside them and "Frames to MP4" muxes it back in later.
        _add_wave_output(config, notes)

    output = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    output.set_editor_property(
        "output_resolution",
        unreal.IntPoint(max(int(options.width), 2), max(int(options.height), 2)))
    if options.start_frame is not None and options.end_frame is not None:
        output.set_editor_property("use_custom_playback_range", True)
        output.set_editor_property("custom_start_frame", int(options.start_frame))
        output.set_editor_property("custom_end_frame", int(options.end_frame))

    if options.output_directory:
        try:
            os.makedirs(options.output_directory, exist_ok=True)
        except Exception:
            pass
        directory = unreal.DirectoryPath()
        directory.set_editor_property("path", options.output_directory)
        output.set_editor_property("output_directory", directory)

    if options.temporal_samples > 1 or options.spatial_samples > 1:
        aa = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
        aa.set_editor_property("temporal_sample_count", max(int(options.temporal_samples), 1))
        aa.set_editor_property("spatial_sample_count", max(int(options.spatial_samples), 1))

    notes.append("Queued '%s' on map '%s' at %dx%d, temporal %d / spatial %d."
                 % (sequence.get_name(), world_package, options.width, options.height,
                    options.temporal_samples, options.spatial_samples))
    notes.append("Output: %s" % (options.output_directory
                                 or "<default Saved/MovieRenders>"))

    if dry_run:
        notes.append("Dry run: the queue was built but no render was started.")
        return job, notes

    # Remembered so "Frames to MP4" can default to the folder just written, and
    # so it can fall back to this sequence's own audio file if the wav is missing.
    _LAST_OUTPUT["directory"] = options.output_directory or ""
    _LAST_OUTPUT["sequence"] = sequence.get_path_name()

    if _close_sequencer():
        notes.append("Closed Sequencer for the render.")

    audio_restore = _flatten_render_audio(sequence) if fmt == MP4 else []
    try:
        executor = subsystem.render_queue_with_executor(unreal.MoviePipelinePIEExecutor)
    except Exception:
        _restore_render_audio(audio_restore)
        raise
    if executor is None:
        _restore_render_audio(audio_restore)
        raise RenderError("Movie Render Queue refused to start the render. Check "
                          "the Output Log.")
    _ACTIVE_EXECUTORS.append(executor)
    def release_executor(_executor, _success):
        _ACTIVE_EXECUTORS.remove(executor)
    executor.on_executor_finished_delegate.add_callable(release_executor)
    if fmt == MP4:
        try:
            rate = sequence.get_display_rate()
            fps = float(rate.numerator) / float(rate.denominator)
            expected_frames = (int(options.end_frame) - int(options.start_frame)
                               if options.start_frame is not None and
                               options.end_frame is not None else
                               int(sequence.get_playback_end()) -
                               int(sequence.get_playback_start()))
            if expected_frames <= 0:
                raise RenderError("Sequence has no frames to encode.")
            if not _launch_external_encode(options.output_directory,
                                           sequence.get_name(), find_ffmpeg(),
                                           expected_frames, fps,
                                           options.encode_quality):
                raise RenderError("External MP4 encoder could not start.")
        except Exception as error:
            unreal.log_error("CineDirector: external MP4 encode setup failed: %s"
                             % error)
            raise
    if audio_restore:
        def restore_audio(_executor, _success):
            _restore_render_audio(audio_restore)
            _ACTIVE_AUDIO_RESTORES.remove(restore_audio)

        _ACTIVE_AUDIO_RESTORES.append(restore_audio)
        executor.on_executor_finished_delegate.add_callable(restore_audio)
        notes.append("MRQ audio workaround: spatial sound is mixed in 2D for "
                     "the MP4; original sound settings are restored afterward.")
    notes.append("Render started.")
    return job, notes


_LAST_OUTPUT = {"directory": "", "sequence": ""}


def last_output_directory():
    """Where the most recent render in this session wrote its frames."""
    directory = _LAST_OUTPUT.get("directory") or ""
    if directory and os.path.isdir(directory):
        return directory
    # Fall back to Movie Render Queue's default.
    try:
        default = os.path.join(
            os.path.abspath(unreal.Paths.project_saved_dir()), "MovieRenders")
        return default if os.path.isdir(default) else ""
    except Exception:
        return ""


def _find_audio(directory):
    """The wav Movie Render Queue wrote for this render, if there is one."""
    candidates = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            if name.lower().endswith(".wav"):
                candidates.append(os.path.join(root, name))
    if not candidates:
        return ""
    # Largest wins: MRQ can emit a short per-shot stub next to the full mix.
    candidates.sort(key=lambda path: os.path.getsize(path), reverse=True)
    return candidates[0]


def _audio_tracks(sequence):
    """Every audio track on the sequence, root-level ones and bound ones."""
    tracks = []
    try:
        tracks += [t for t in sequence.get_tracks()
                   if isinstance(t, unreal.MovieSceneAudioTrack)]
    except Exception:
        pass
    try:
        for binding in sequence.get_bindings():
            tracks += [t for t in binding.get_tracks()
                       if isinstance(t, unreal.MovieSceneAudioTrack)]
    except Exception:
        pass
    return tracks


def _sequence_for_fallback():
    """The sequence this render came from, or whatever is open in Sequencer."""
    path = _LAST_OUTPUT.get("sequence") or ""
    if path:
        try:
            sequence = unreal.load_asset(path)
            if sequence is not None:
                return sequence
        except Exception:
            pass
    try:
        return unreal.LevelSequenceEditorBlueprintLibrary.get_current_level_sequence()
    except Exception:
        return ""


def _sequence_audio_fallback(first_frame):
    """
    (path, seek_seconds, delay_seconds) for the sequence's own source audio.

    Frames rendered before Wave Output was in place have no wav beside them, and
    re-rendering an hour of stills just to recover the sound is a poor trade. The
    audio section still points at a SoundWave that remembers the file it was
    imported from, so read that file directly and line it up by hand.

    `first_frame` is the sequence frame the folder's first image belongs to, which
    is how far into the audio this encode has to start.
    """
    sequence = _sequence_for_fallback()
    if not sequence:
        return ()

    try:
        rate = sequence.get_display_rate()
        fps = float(rate.numerator) / float(rate.denominator or 1)
        ticks = sequence.get_tick_resolution()
        tick_rate = float(ticks.numerator) / float(ticks.denominator or 1)
    except Exception:
        return ()
    if fps <= 0 or tick_rate <= 0:
        return ()

    for track in _audio_tracks(sequence):
        for section in track.get_sections():
            try:
                sound = section.get_editor_property("sound")
            except Exception:
                sound = None
            if sound is None:
                continue

            source = ""
            try:
                for name in sound.get_editor_property("asset_import_data").extract_filenames():
                    if name and os.path.isfile(str(name)):
                        source = str(name)
                        break
            except Exception:
                pass
            if not source:
                continue

            try:
                section_start = int(section.get_start_frame())
            except Exception:
                section_start = 0
            offset_seconds = 0.0
            try:
                offset_seconds = float(
                    section.get_editor_property("start_frame_offset").value) / tick_rate
            except Exception:
                pass

            # A section that begins before the first rendered frame means the
            # encode starts partway into the clip; one that begins after it means
            # the clip has to wait.
            shift = (section_start - int(first_frame)) / fps
            return source, offset_seconds + max(0.0, -shift), max(0.0, shift)

    return ()


def encode_folder(directory, fps=30.0, quality=QUALITY_HIGH, output_path="",
                  audio_path="", on_complete=None, delete_frames=False):
    """
    Turn a folder of rendered frames into an MP4.

    Movie Render Queue only encodes video when the job asks for MP4 up front, so
    a PNG render that turns out to be worth keeping would otherwise mean doing
    this by hand. Returns (output_path, notes).
    """
    notes = []
    directory = str(directory or "").strip()
    if not os.path.isdir(directory):
        raise RenderError("No such folder: %s" % (directory or "(empty)"))

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RenderError(
            "ffmpeg was not found. Install it with \"winget install Gyan.FFmpeg\", "
            "or set its path in Movie Render Queue's command line encoder settings.")

    frames = sorted(name for name in os.listdir(directory)
                    if os.path.splitext(name)[1].lower()
                    in (".png", ".jpg", ".jpeg", ".bmp"))
    if not frames:
        raise RenderError("No rendered frames in %s." % directory)

    # Frames are named <sequence>.<number>.<ext>; the pattern has to match the
    # real digit count or ffmpeg reads one frame and stops.
    stem, _, tail = frames[0].rpartition(".")
    base, _, number = stem.rpartition(".")
    if not number.isdigit():
        raise RenderError("Frame names in %s are not <name>.<number>.<ext>, so "
                          "they cannot be encoded as a sequence." % directory)
    frames = [name for name in frames
              if name.startswith(base + ".")
              and name.endswith("." + tail)
              and name[len(base) + 1:-len(tail) - 1].isdigit()]
    pattern = os.path.join(directory, "%s.%%0%dd.%s" % (base, len(number), tail))
    start = int(number)

    crf = {QUALITY_LOW: 28, QUALITY_MED: 23,
           QUALITY_HIGH: 20, QUALITY_EPIC: 16}.get(quality, 20)

    output_path = str(output_path or "").strip() or os.path.join(
        directory, base + ".mp4")

    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", "%g" % max(float(fps), 1.0),
        "-start_number", str(start),
        "-i", pattern,
    ]

    # Movie Render Queue writes the Sequencer audio as a wav beside the frames.
    # Mux it in if it is there, rather than shipping a silent video.
    wav = audio_path or _find_audio(directory)
    delay = 0.0
    if wav:
        notes.append("Found audio: %s" % os.path.basename(wav))
    else:
        fallback = _sequence_audio_fallback(start)
        if fallback:
            wav, seek, delay = fallback
            if seek > 0:
                command += ["-ss", "%.6f" % seek]
            notes.append("No wav beside the frames, so this used the sequence's "
                         "own audio file: %s" % os.path.basename(wav))
        else:
            notes.append("No wav beside the frames, so this MP4 is silent.")

    if wav:
        command += ["-i", wav, "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                    "-ar", "48000", "-shortest"]
        if delay > 0:
            # adelay rather than -itsoffset: a timestamp shift is something
            # players are free to ignore, silence at the head is not.
            command += ["-af", "adelay=%d:all=1" % int(round(delay * 1000.0))]

    command += [
        "-c:v", "libx264", "-crf", str(crf),
        "-threads", "2",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
        "-movflags", "+faststart",
        output_path,
    ]

    notes.append("Encoding %d frames at %g fps, crf %d." % (len(frames), fps, crf))
    def run_encode():
        try:
            finished = subprocess.run(command, capture_output=True, text=True,
                                      timeout=3600)
            if finished.returncode != 0:
                raise RenderError("ffmpeg failed: %s"
                                  % (finished.stderr or "").strip()[:400])
            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                raise RenderError("ffmpeg finished without a playable output file.")
            size_mb = os.path.getsize(output_path) / (1024.0 * 1024.0)
            notes.append("Wrote %s (%.1f MB, %.1f seconds)."
                         % (output_path, size_mb,
                            len(frames) / max(float(fps), 1.0)))
            if delete_frames:
                for name in frames:
                    os.remove(os.path.join(directory, name))
                if wav and os.path.dirname(os.path.abspath(wav)) == os.path.abspath(directory):
                    os.remove(wav)
                notes.append("Removed source frames and render audio.")
            return output_path, notes, None
        except Exception as error:          # noqa: BLE001
            return output_path, notes, error

    if on_complete is not None:
        # The panel polls this state on the editor thread. ffmpeg can take many
        # minutes; waiting for it in a Slate button callback freezes Unreal.
        state = {"done": False, "result": None}
        def worker():
            state["result"] = run_encode()
            state["done"] = True
        threading.Thread(target=worker, name="CineDirector-ffmpeg", daemon=True).start()
        on_complete(state)
        return output_path, notes

    path, notes, error = run_encode()
    if error:
        raise error
    return path, notes


def start_render(options, sequence=None):
    """Start a render. Returns the notes list; raises RenderError on failure."""
    _, notes = build_job(options, sequence=sequence, dry_run=False)
    for note in notes:
        unreal.log("CineDirector render: " + note)
    return notes
