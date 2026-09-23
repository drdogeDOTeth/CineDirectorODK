"""Encode MRQ frames outside Unreal, then verify and remove the sources."""

import argparse
import glob
import json
import os
import subprocess
import time
import wave


def frame_paths(directory, stem):
    result = []
    for path in glob.glob(os.path.join(directory, stem + ".*.png")):
        number = os.path.basename(path)[len(stem) + 1:-4]
        if number.isdigit():
            result.append((int(number), path))
    return sorted(result)


def valid_video(path, frames, ffprobe):
    try:
        info = json.loads(subprocess.check_output([
            ffprobe, "-v", "error", "-count_frames", "-show_entries",
            "stream=codec_type,nb_read_frames:format=duration", "-of", "json", path,
        ], timeout=600))
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = any(s["codec_type"] == "audio" for s in info["streams"])
        return (audio and int(video["nb_read_frames"]) == len(frames)
                and float(info["format"]["duration"]) > 0)
    except (OSError, ValueError, KeyError, StopIteration,
            subprocess.SubprocessError):
        return False


def encode_when_ready(directory, stem, ffmpeg, ffprobe, expected_frames,
                      fps, quality, timeout=86400):
    """Encode after the complete frame set and matching WAV settle on disk."""
    deadline = time.monotonic() + timeout
    wav = os.path.join(directory, stem + ".wav")
    stable_since = None
    previous_size = -1
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(wav)
            with wave.open(wav, "rb") as audio:
                audio_frames = round(audio.getnframes() /
                                     audio.getframerate() * fps)
        except (OSError, ValueError, wave.Error, ZeroDivisionError):
            size = 0
            audio_frames = -1
        audio_ready = abs(audio_frames - expected_frames) <= 1 and size > 0
        frames = frame_paths(directory, stem) if audio_ready else []
        ready = len(frames) == expected_frames and audio_ready
        if ready and size == previous_size:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 10:
                break
        else:
            stable_since = None
        previous_size = size
        time.sleep(2)
    else:
        return False
    numbers = [number for number, _ in frames]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return False
    if not os.path.isfile(wav) or os.path.getsize(wav) == 0:
        return False
    # MRQ uses a fixed-width frame number; confirm it before asking ffmpeg to
    # read a numeric sequence.
    if any(len(os.path.basename(path)[len(stem) + 1:-4]) !=
           len(os.path.basename(frames[0][1])[len(stem) + 1:-4])
           for _, path in frames):
        return False
    width = len(os.path.basename(frames[0][1])[len(stem) + 1:-4])
    pattern = os.path.join(directory, stem + ".%0" + str(width) + "d.png")
    output = os.path.join(directory, stem + ".mp4")
    temporary = os.path.join(directory, stem + ".partial.mp4")
    crf = (28, 23, 20, 16)[max(0, min(int(quality), 3))]
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(fps), "-start_number", str(numbers[0]),
               "-i", pattern, "-i", wav, "-c:v", "libx264", "-crf", str(crf),
               "-threads", "2", "-pix_fmt", "yuv420p", "-profile:v", "high",
               "-level", "4.2", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", temporary]
    try:
        if subprocess.call(command, timeout=14400) != 0:
            return False
        if not valid_video(temporary, frames, ffprobe):
            return False
        os.replace(temporary, output)
        for _, path in frames:
            os.remove(path)
        os.remove(wav)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("stem")
    parser.add_argument("ffprobe")
    parser.add_argument("expected_frames", type=int)
    parser.add_argument("ffmpeg")
    parser.add_argument("fps", type=float)
    parser.add_argument("quality", type=int)
    args = parser.parse_args()
    encode_when_ready(args.directory, args.stem, args.ffmpeg, args.ffprobe,
                      args.expected_frames, args.fps, args.quality)
