"""Local video acquisition, transcription and rendering for SplitStudio."""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import platform
import shutil
import time
from functools import lru_cache
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import av
import imageio_ffmpeg
import numpy as np
from highlights import select_windows
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


DURATIONS = (10, 180, 600)


def duration_seconds(path):
    with av.open(str(path)) as media:
        streams = [s for s in media.streams.video if not s.disposition.attached_pic & s.disposition]
        if not streams:
            raise ValueError("La source ne contient aucune piste vidéo.")
        video = streams[0]
        if video.duration is not None:
            duration = float(video.duration * video.time_base)
        elif media.duration is not None:
            duration = media.duration / av.time_base
        else:
            raise ValueError("Impossible de déterminer la durée de cette vidéo.")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("La vidéo est vide.")
    return duration


def _download(url, directory, ffmpeg, progress, low, high, label):
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Saisissez une URL de vidéo HTTP ou HTTPS valide.")
    directory.mkdir()
    # yt-dlp requires both ffmpeg and ffprobe to merge separate streams.
    # Select a combined format when available. Merge split streams ourselves otherwise.
    track, tracks = 0, 1
    def report(item):
        current = item.get("downloaded_bytes", 0)
        total = item.get("total_bytes") or item.get("total_bytes_estimate")
        ratio = min(1, current / total) if total else 0
        detail = f"{current / 1048576:.1f} Mo"
        if total:
            detail += f" sur {total / 1048576:.1f} Mo"
        progress(f"{label} · {detail}", low + (high - low) * (track + ratio) / tracks, "download")
    opts = dict(progress_hooks=[report], concurrent_fragment_downloads=4,outtmpl=str(directory / "source.%(format_id)s.%(ext)s"),
                format="bv*[height<=720]+ba/b[height<=720]/bv*[height<=1080]+ba/b/bv*+ba/bv",
                noplaylist=True, playlistend=1, noprogress=True, quiet=True, no_warnings=True,
                socket_timeout=30, retries=3, fixup="never", allow_unplayable_formats=False)
    try:
        with YoutubeDL(opts) as client:
            meta = client.extract_info(url, download=False)
            if not meta or meta.get("entries") is not None or meta.get("is_live"):
                raise ValueError("Choisissez une vidéo individuelle dont la diffusion est terminée.")
            formats = meta.get("requested_formats") or [meta]
            paths = []
            tracks = len(formats)
            for track, item in enumerate(formats):
                chosen = dict(meta)
                chosen.pop("requested_formats", None)
                chosen.pop("requested_downloads", None)
                chosen.update(item)
                client.process_info(chosen)
                paths.append(Path(client.prepare_filename(chosen)))
    except DownloadError as exc:
        raise ValueError("Impossible de récupérer la vidéo. Vérifiez que le lien est accessible et pris en charge.") from exc
    title = str(meta.get("title") or "").strip()
    if len(paths) == 1:
        return paths[0], title
    merged = directory / "source.mkv"
    _ffmpeg(ffmpeg, ["-i", str(paths[0]), "-i", str(paths[1]), "-map", "0:v:0", "-map", "1:a:0",
                      "-c", "copy", str(merged)], directory)
    return merged, title


def _cache_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _cached_source(url, directory, cache, ffmpeg, progress, low, high, label):
    key = _cache_key(url)
    cached = cache / (key + ".media")
    named = cache / (key + ".title")
    valid = cached.exists() and time.time() - cached.stat().st_mtime < 86400
    title = named.read_text(encoding="utf-8") if valid and named.exists() else ""
    if valid:
        directory.mkdir()
        path = directory / "source.media"
        try:
            os.link(cached, path)
        except OSError:
            shutil.copy2(cached, path)
        progress(f"{label} retrouvée en cache.", high, "download")
    else:
        path, title = _download(url, directory, ffmpeg, progress, low, high, label)
        named.write_text(title, encoding="utf-8")
        if path.stat().st_size <= 2 * 1024 ** 3:
            tmp = cache / (key + ".part")
            try:
                os.link(path, tmp)
            except OSError:
                shutil.copyfile(path, tmp)
            tmp.replace(cached)
    return path, title


def _trim_cache(cache):
    used = 0
    for p in sorted(cache.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
        keep = (p.suffix != ".part" and time.time() - p.stat().st_mtime < 86400
                and used + p.stat().st_size <= 3 * 1024 ** 3)
        if keep:
            used += p.stat().st_size
        elif p.is_file():
            p.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def _whisper_model(name):
    from faster_whisper import WhisperModel
    args = dict(device="cpu", compute_type="int8", cpu_threads=max(1, os.cpu_count() or 4))
    try:
        return WhisperModel(name, local_files_only=True, **args)
    except (OSError, RuntimeError):
        return WhisperModel(name, **args)


def _ffmpeg(binary, arguments, cwd):
    result = subprocess.run([binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
                            cwd=cwd, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError("FFmpeg n’a pas pu traiter la vidéo. " + result.stderr.strip()[-400:])


def _captions(source, progress, duration=None):
    duration = duration or duration_seconds(source)
    with av.open(str(source)) as media:
        if not media.streams.audio:
            return []
    progress("Chargement de Whisper · téléchargement à la première utilisation…", 24, "transcribe")
    model = _whisper_model(os.environ.get("SPLITSTUDIO_WHISPER_MODEL", "base"))
    progress("Transcription des dialogues…", 25, "transcribe")
    audio_path = source.parent / "dialogues.f32"
    _ffmpeg(imageio_ffmpeg.get_ffmpeg_exe(),
            ["-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-f", "f32le", str(audio_path)], source.parent)
    if audio_path.stat().st_size == 0:
        return []
    audio = np.memmap(audio_path, dtype="<f4", mode="r")
    segments, _ = model.transcribe(audio, vad_filter=True, word_timestamps=True, beam_size=1,
                                   condition_on_previous_text=False)
    cues = []
    for segment in segments:
        ratio = min(1, segment.end / duration)
        progress(f"Transcription · {round(segment.end)} s sur {math.ceil(duration)} s", 25 + 29 * ratio, "transcribe")
        group = []
        for word in segment.words or []:
            if group and (word.end - group[0].start > 2.8 or len("".join(w.word for w in [*group, word])) > 36):
                cues.append((group[0].start, group[-1].end, "".join(w.word for w in group).strip()))
                group = []
            group.append(word)
        if group:
            cues.append((group[0].start, group[-1].end, "".join(w.word for w in group).strip()))
        elif not segment.words and segment.text.strip():
            cues.append((segment.start, segment.end, segment.text.strip()))
    return cues


def _stamp(seconds):
    milliseconds = max(0, round(seconds * 1000))
    minutes, ms = divmod(milliseconds, 60000)
    hours, minutes = divmod(minutes, 60)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


@lru_cache(maxsize=1)
def _hardware_available(ffmpeg):
    if platform.system() != "Darwin":
        return False
    probe = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=160x90",
                            "-frames:v", "1", "-c:v", "h264_videotoolbox", "-allow_sw", "0",
                            "-f", "null", "-"], capture_output=True)
    return probe.returncode == 0


def _encode(ffmpeg, args, cwd, length, update):
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                               "-stats_period", "0.4", "-progress", "pipe:1", *args],
                              cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True) as process:
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    value = line.strip().partition("=")[-1]
                    if value.lstrip("-").isdigit():
                        update(min(1.0, max(0, int(value)) / (length * 1e6)))
            if process.wait():
                errors.seek(0)
                raise RuntimeError("Échec de l’encodage vidéo. " + errors.read().decode(errors="replace")[-400:])


def _workers(hardware):
    cpus = os.cpu_count() or 2
    # VideoToolbox handles a couple of sessions at once; x264 is already multithreaded.
    return 2 if hardware else max(1, min(3, cpus // 4))


def _layout(secondary):
    if secondary:
        panel = "scale=720:640:force_original_aspect_ratio=increase,crop=720:640,setsar=1,fps=30"
        return f"[0:v:0]{panel}[top];[1:v:0]{panel}[bottom];[top][bottom]vstack=shortest=1"
    return ("[0:v:0]scale=720:720:force_original_aspect_ratio=increase,crop=720:720,setsar=1,fps=30,"
            "pad=720:1280:0:208:black")


def _ass_time(seconds):
    centis = max(0, round(seconds * 100))
    minutes, cs = divmod(centis, 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{cs // 100:02}.{cs % 100:02}"


def _ass_text(text):
    text = " ".join(str(text).split())
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _overlay(cues, start, length, path, split, parts):
    """Write an ASS file: "Partie N" in the top black band, captions in the bottom black band.

    parts: [(begin, end, label)] relative to the output clip/timeline.
    """
    # Centered layout: video occupies y=208..928 of the 720x1280 frame.
    if split:
        # No black bands in split screen: label on a dark box at the top, captions on the seam.
        part = "Part,Arial,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,3,14,0,8,30,30,34,1"
        caption = "Caption,Arial,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,28,28,670,1"
    else:
        part = "Part,Arial,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,8,30,30,68,1"
        caption = "Caption,Arial,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,8,36,36,968,1"
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: " + part, "Style: " + caption, "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for begin, end, label in parts:
        lines.append(f"Dialogue: 1,{_ass_time(begin)},{_ass_time(end)},Part,,0,0,0,,{_ass_text(label)}")
    # Captions centred in the bottom black band (y=928..1280), whatever their line count.
    place = "" if split else "{\\an5\\pos(360,1104)}"
    for begin, end, phrase in cues:
        if end > start and begin < start + length:
            begin, end = max(0, begin - start), min(length, end - start)
            if round(end * 100) > round(begin * 100) and phrase.strip():
                lines.append(f"Dialogue: 0,{_ass_time(begin)},{_ass_time(end)},Caption,,0,0,0,,{place}{_ass_text(phrase)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f",ass={path.name}"


AUDIO = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]


def _codec(hardware):
    if hardware:
        return ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-b:v", "4M"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]


def _render_sequential(source, secondary, seconds, duration, staging, cues, ffmpeg, hardware, progress, emit):
    """Encode the whole video once and cut it into clips on forced keyframes."""
    count = math.ceil(round(duration, 6) / seconds)
    parts = [(k * seconds, min(duration, (k + 1) * seconds), f"Partie {k + 1}") for k in range(count)]
    graph = _layout(secondary) + _overlay(cues, 0, duration, staging / "overlay.ass", bool(secondary), parts)
    inputs = ["-i", str(source)]
    if secondary:
        inputs += ["-stream_loop", "-1", "-i", str(secondary)]
    state = {"emitted": 0, "start": 0.0}

    def flush(final=False):
        while state["emitted"] < count:
            current = staging / f"clip-{state['emitted'] + 1:03}.mp4"
            following = staging / f"clip-{state['emitted'] + 2:03}.mp4"
            if not current.exists() or not (final or following.exists()):
                return
            length = duration_seconds(current)
            emit(current, state["start"], state["start"] + length, {"part": state["emitted"] + 1})
            state["start"] += length
            state["emitted"] += 1

    def update(fraction):
        done = min(count, int(fraction * duration // seconds))
        progress(f"Montage en une passe · clip {min(count, done + 1)} sur {count} · {round(fraction * 100)} %",
                 69 + 30 * fraction, "render")
        if done > state["emitted"]:
            flush()

    def attempt(use_hardware):
        for leftover in staging.glob("clip-*.mp4"):
            leftover.unlink()
        _encode(ffmpeg, [*inputs, "-filter_complex", graph + "[out]", "-map", "[out]", "-map", "0:a:0?",
                         "-t", f"{duration:.6f}", *_codec(use_hardware), "-bf", "0", *AUDIO,
                         # B-frames make the segment muxer miss the forced keyframes.
                         "-force_key_frames", f"expr:gte(t,n_forced*{seconds})",
                         "-f", "segment", "-segment_time", str(seconds), "-segment_start_number", "1",
                         "-reset_timestamps", "1", "-segment_format", "mp4",
                         "-segment_format_options", "movflags=+faststart", "clip-%03d.mp4"],
                staging, duration, update)

    update(0)
    if hardware:
        try:
            attempt(True)
        except RuntimeError:
            if state["emitted"]:
                raise
            hardware = False
    if not hardware:
        attempt(False)
    count = len(list(staging.glob("clip-*.mp4"))) + state["emitted"]
    flush(final=True)


def _render_windows(source, secondary, windows, staging, cues, ffmpeg, hardware, progress, emit):
    """Encode selected windows independently, several at a time."""
    secondary_duration = duration_seconds(secondary) if secondary else None
    count = len(windows)
    fractions = [0.0] * count
    guard = threading.Lock()
    flags = {"hardware": hardware}

    def report():
        done = sum(1 for f in fractions if f >= 1)
        progress(f"Encodage des clips · {done} sur {count} terminés", 69 + 30 * sum(fractions) / count, "render")

    def job(i, start, length, info):
        def update(fraction):
            with guard:
                fractions[i] = min(fraction, 0.999)
            report()
        work = staging / f"clip-{i + 1:03}"
        work.mkdir()
        graph = _layout(secondary) + _overlay(cues, start, length, work / "overlay.ass", bool(secondary),
                                              [(0, length, f"Partie {i + 1}")])
        inputs = ["-ss", str(start), "-i", str(source)]
        if secondary:
            inputs += ["-stream_loop", "-1", "-ss", str(start % secondary_duration), "-i", str(secondary)]
        destination = work / f"clip-{i + 1:03}.mp4"
        prefix = [*inputs, "-filter_complex", graph + "[out]", "-map", "[out]", "-map", "0:a:0?", "-t", f"{length:.6f}"]
        suffix = [*AUDIO, "-movflags", "+faststart", str(destination)]
        if flags["hardware"]:
            try:
                _encode(ffmpeg, [*prefix, *_codec(True), *suffix], work, length, update)
            except RuntimeError:
                flags["hardware"] = False
        if not flags["hardware"]:
            _encode(ffmpeg, [*prefix, *_codec(False), *suffix], work, length, update)
        with guard:
            fractions[i] = 1.0
            emit(destination, start, start + length, {**info, "part": i + 1})
        report()

    report()
    with ThreadPoolExecutor(max_workers=min(count, _workers(hardware))) as pool:
        for future in [pool.submit(job, i, *window) for i, window in enumerate(windows)]:
            future.result()


def _render_files(source, secondary, seconds, staging, progress, captions, ffmpeg, emit,
                  highlights=False, transcript_cache=None):
    duration = duration_seconds(source)
    cues = []
    if captions or (highlights and duration > seconds):
        if transcript_cache and transcript_cache.exists():
            cues = json.loads(transcript_cache.read_text())
            progress("Transcription retrouvée en cache.", 54, "transcribe")
        else:
            cues = _captions(source, progress, duration)
            if transcript_cache and cues:
                tmp = transcript_cache.with_suffix(".part")
                tmp.write_text(json.dumps(cues, ensure_ascii=False))
                tmp.replace(transcript_cache)
    if not captions:
        subtitles = []
    else:
        subtitles = cues
    hardware = _hardware_available(ffmpeg)
    if highlights:
        windows = select_windows(source, ffmpeg, duration, seconds, cues, progress)
        _render_windows(source, secondary, windows, staging, subtitles, ffmpeg, hardware, progress, emit)
    else:
        _render_sequential(source, secondary, seconds, duration, staging, subtitles, ffmpeg, hardware, progress, emit)


def _reporter(callback):
    try:
        inspect.signature(callback).bind("message", 0, "stage")
        detailed = True
    except (ValueError, TypeError):
        detailed = False
    last_time, last_percent, last_stage = 0, -1, None
    guard = threading.Lock()
    def report(message, percent, stage):
        with guard:
            _report(message, percent, stage)
    def _report(message, percent, stage):
        nonlocal last_time, last_percent, last_stage
        now = time.monotonic()
        percent = max(last_percent, min(100, round(percent)))
        if stage == last_stage and percent <= last_percent + 1 and now - last_time < 0.45:
            return
        last_time, last_percent, last_stage = now, percent, stage
        if detailed:
            callback(message, percent, stage)
        else:
            callback(message)
    return report


def render(url, secondary_url, seconds, folder: Path, progress, captions=True, highlights=False, on_clip=None,
           on_title=None):
    """Render chronological clips or selected highlights for a single job.

    on_clip(path, start, end, info) is called as soon as each clip is ready, possibly from a worker thread.
    """
    if seconds not in DURATIONS:
        raise ValueError("Choisissez une durée de clip proposée par le studio.")
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    progress = _reporter(progress)
    cache = folder.parent / ".media-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    _trim_cache(cache)
    paths = []

    def emit(path, start, end, info):
        final = Path(path).replace(folder / Path(path).name)
        paths.append(final)
        if on_clip:
            on_clip(final, start, end, info)

    try:
        with tempfile.TemporaryDirectory(dir=folder, prefix=".montage-") as scratch:
            staging = Path(scratch)
            progress("Ouverture de la vidéo principale…", 1, "download")
            source, title = _cached_source(url, staging / "principale", cache, ffmpeg, progress, 1,
                                           12 if secondary_url else 22, "Vidéo principale")
            if on_title:
                on_title(title)
            secondary = None
            if secondary_url:
                progress("Ouverture de la vidéo secondaire…", 12, "download")
                secondary, _ = _cached_source(secondary_url, staging / "secondaire", cache, ffmpeg, progress,
                                           12, 22, "Vidéo secondaire")
            fingerprint = f"{url}:{source.stat().st_size}:{source.stat().st_mtime_ns}"
            name = os.environ.get("SPLITSTUDIO_WHISPER_MODEL", "base")
            transcript_cache = cache / (_cache_key(f"whisper-v2:{name}:{fingerprint}") + ".json")
            _render_files(source, secondary, seconds, staging, progress, captions, ffmpeg, emit,
                          highlights=highlights, transcript_cache=transcript_cache)
            progress("Clips prêts.", 100, "done")
            return sorted(paths)
    finally:
        _trim_cache(cache)
