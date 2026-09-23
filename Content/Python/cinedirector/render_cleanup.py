"""Encode MRQ frames outside Unreal, then verify and remove the sources."""

import argparse
import ctypes
import glob
import json
import os
import subprocess
import time
import wave


def process_alive(pid):
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


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


def encode_when_ready(directory, stem, ffmpeg, ffprobe, parent_pid,
                      marker, fps, quality, timeout=86400):
    """Encode outside Unreal after MRQ signals that its PNG and WAV passes ended."""
    deadline = time.monotonic() + timeout
    while not os.path.isfile(marker):
        if not process_alive(parent_pid):
            # The editor may have exited just before its completion delegate.
            # Wait for disk writes to settle, then require the WAV duration to
            # account for every frame before attempting recovery.
            time.sleep(30)
            frames = frame_paths(directory, stem)
            wav = os.path.join(directory, stem + ".wav")
            try:
                with wave.open(wav, "rb") as audio:
                    expected = round(audio.getnframes() / audio.getframerate() * fps)
                if abs(len(frames) - expected) > 1:
                    return False
            except (OSError, ValueError, wave.Error):
                return False
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)
    frames = frame_paths(directory, stem)
    if not frames:
        return False
    numbers = [number for number, _ in frames]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return False
    wav = os.path.join(directory, stem + ".wav")
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
    finally:
        try:
            os.remove(marker)
        except OSError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("stem")
    parser.add_argument("ffprobe")
    parser.add_argument("parent_pid", type=int)
    parser.add_argument("marker")
    parser.add_argument("ffmpeg")
    parser.add_argument("fps", type=float)
    parser.add_argument("quality", type=int)
    args = parser.parse_args()
    encode_when_ready(args.directory, args.stem, args.ffmpeg, args.ffprobe,
                      args.parent_pid, args.marker, args.fps, args.quality)
