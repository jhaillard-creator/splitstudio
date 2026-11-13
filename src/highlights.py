"""Score candidate windows with multilingual embeddings or audiovisual activity."""
from functools import lru_cache
import logging
import math
import os
from pathlib import Path
import subprocess

import numpy as np

MODEL = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "2c4055b12046f11709e9df2c122e59ffbdc2f900"
# Passages that make a clip worth watching on their own.
POSITIVE = [
    "An unexpected discovery changes everything. I never saw this coming!",
    "Here is exactly how I solved it. This trick really works.",
    "Everyone started laughing. I couldn't believe what happened next!",
    "This is the crucial mistake, and here is the reason it matters.",
    "Everything finally clicked. It was the most moving moment of my life.",
    "Now watch the decisive move, the tension, and the final result.",
    "Here's the secret most people don't know about this.",
    "Wait, what? No way, that's completely insane!",
    "Let me tell you the craziest story that happened to me.",
    "I strongly disagree, and honestly this opinion will make people angry.",
    "The number is shocking: it costs ten times more than you think.",
    "If you remember only one thing from this video, remember this.",
]
# Housekeeping that nobody wants to see in a short clip.
NEGATIVE = [
    "Welcome to the channel. Please like and subscribe. This video is sponsored.",
    "We are waiting to get started. Just checking the microphone, one two three.",
    "Thanks for watching, see you in the next video. Don't forget the link below.",
    "Um, so, yeah, anyway, let's move on to the next part.",
]
ANCHORS = POSITIVE + NEGATIVE


@lru_cache(maxsize=1)
def _semantic_model():
    from huggingface_hub import snapshot_download
    from onnxruntime import InferenceSession, SessionOptions
    from tokenizers import Tokenizer
    patterns = ["onnx/model_quantized.onnx", "tokenizer.json"]
    try:
        root = Path(snapshot_download(MODEL, revision=REVISION, allow_patterns=patterns, local_files_only=True))
        if not all((root / p).is_file() for p in patterns):
            raise FileNotFoundError()
    except (FileNotFoundError, OSError):
        root = Path(snapshot_download(MODEL, revision=REVISION, allow_patterns=patterns))
    tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
    tokenizer.enable_truncation(128)
    tokenizer.enable_padding(pad_id=tokenizer.token_to_id("<pad>") or 0)
    opts = SessionOptions()
    opts.intra_op_num_threads = min(os.cpu_count() or 2, 4)
    model = InferenceSession(str(root / patterns[0]), sess_options=opts, providers=["CPUExecutionProvider"])
    return tokenizer, model


def _vectors(texts):
    tokenizer, model = _semantic_model()
    enc = tokenizer.encode_batch(texts)
    mask = np.array([x.attention_mask for x in enc], dtype=np.int64)
    ids = np.array([x.ids for x in enc], dtype=np.int64)
    inputs = dict(input_ids=ids, attention_mask=mask, token_type_ids=np.zeros_like(ids))
    output = model.run(None, {v.name: inputs[v.name] for v in model.get_inputs()})[0]
    pooled = (output * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
    return pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-8)


def _normalize(values):
    lower, upper = np.percentile(values, [5, 95])
    if upper <= lower + 1e-8:
        lower, upper = values.min(), values.max()
    return np.clip((values - lower) / max(float(upper - lower), 1e-8), 0, 1)


def _transcript_signals(cues, bins, progress):
    passages = []
    for begin, end, text in cues:
        if (passages and begin - passages[-1][1] <= 1.25 and end - passages[-1][0] < 9
                and not passages[-1][2].endswith((".", "?", "!", "。", "？", "！"))):
            passages[-1][1:] = [end, passages[-1][2] + " " + text]
        else:
            passages.append([begin, end, text])
    anchors = _vectors(ANCHORS)
    batches = []
    for i in range(0, len(passages), 24):
        batches.append(_vectors([p[2] for p in passages[i:i + 24]]))
        progress("Comparaison des passages et des sujets…", 56 + 9 * min(1, (i + 24) / len(passages)), "analyze")
    vectors = np.vstack(batches)
    scores = vectors @ anchors.T
    # Penalise housekeeping and reward topical passages without requiring keywords.
    centrality = vectors @ vectors.mean(axis=0)
    top = np.sort(scores[:, :len(POSITIVE)], axis=1)[:, -2:].mean(axis=1)
    relevance = top - 0.6 * scores[:, len(POSITIVE):].max(axis=1) + 0.16 * centrality
    weights = 0.35 + 0.65 * _normalize(relevance)
    interest = np.zeros(bins)
    for (begin, end, _), weight in zip(passages, weights):
        low, high = max(0, math.floor(begin)), min(bins, math.ceil(end))
        interval = np.arange(low, high)
        covered = np.maximum(0, np.minimum(end, interval + 1) - np.maximum(begin, interval))
        interest[low:high] += weight * covered
    return np.clip(interest, 0, 1), passages, weights


def _smooth(values, width):
    if width <= 1 or len(values) < width:
        return values
    return np.convolve(values, np.ones(width) / width, mode="same")


def _audio(source, ffmpeg):
    """Mono 16 kHz samples, reused from the transcription step when available."""
    path = source.parent / "dialogues.f32"
    if not path.is_file():
        result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(source), "-map", "0:a:0?", "-vn",
                                 "-ac", "1", "-ar", "16000", "-f", "f32le", str(path)], capture_output=True)
        if result.returncode:
            path.unlink(missing_ok=True)
            return None
    if not path.stat().st_size:
        return None
    return np.memmap(path, dtype="<f4", mode="r")


def _activity(source, ffmpeg, bins):
    # Decode keyframes only: an order of magnitude faster than decoding every frame,
    # and keyframes land on scene changes, which is what we want to detect.
    frames = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-skip_frame", "nokey", "-i", str(source),
                             "-an", "-sn", "-dn", "-vf", "fps=1,scale=32:18,format=gray", "-f", "rawvideo", "pipe:1"],
                            capture_output=True, check=True).stdout
    motion = np.zeros(bins)
    frames = np.frombuffer(frames, dtype=np.uint8)
    frames = frames[:len(frames) // 576 * 576].reshape(-1, 576)
    n = min(bins, len(frames))
    if n > 1:
        motion[1:n] = np.abs(np.diff(frames[:n].astype(np.int16), axis=0)).mean(axis=1)
    motion = _smooth(motion, 3)
    audio = _audio(source, ffmpeg)
    if audio is None:
        return _normalize(motion)
    power = np.zeros(bins)
    for second in range(min(bins, math.ceil(len(audio) / 16000))):
        samples = audio[second * 16000:(second + 1) * 16000:4]
        if len(samples):
            power[second] = np.log1p(100 * np.sqrt(np.mean(np.square(samples))))
    # Excitement: loudness above the local level (laughter, applause, raised voices).
    baseline = _smooth(power, 31)
    excitement = np.maximum(0, power - baseline)
    return 0.45 * _normalize(motion) + 0.25 * _normalize(power) + 0.30 * _normalize(_smooth(excitement, 3))


def _excerpt(passages, weights, start, end):
    best, text = -1, ""
    for (begin, finish, words), weight in zip(passages, weights):
        if finish > start and begin < end and weight > best:
            best, text = weight, words
    text = " ".join(text.split())
    return text if len(text) <= 110 else text[:107].rsplit(" ", 1)[0] + "…"


def select_windows(source, ffmpeg, duration, seconds, cues, progress):
    """Return [(start, length, info)] for the most interesting windows, in chronological order."""
    if duration <= seconds:
        return [(0, duration, {"score": 100})]
    bins = math.ceil(duration)
    progress("Modèle sémantique · téléchargement à la première utilisation…" if cues else "Analyse du mouvement et du son…",
             55, "analyze")
    passages, weights = [], []
    if cues:
        try:
            interest, passages, weights = _transcript_signals(cues, bins, progress)
        except Exception:
            # No semantic model (offline, failed download…): rely on sound and motion only.
            logging.exception("Analyse sémantique indisponible")
            cues = []
    if cues:
        progress("Analyse du rythme, du mouvement et des réactions…", 65, "analyze")
        try:
            activity = _activity(source, ffmpeg, bins)
            interest = 0.72 * interest + 0.28 * activity
        except (subprocess.CalledProcessError, ValueError):
            pass
    else:
        interest = _activity(source, ffmpeg, bins)
    running = np.r_[0, np.cumsum(interest)]
    max_start = duration - seconds
    starts = {0.0, max_start}
    starts.update(float(t) for t in range(0, int(max_start) + 1, max(1, seconds // 16)))
    boundaries = {round(float(p[0]), 3) for p in passages}
    endings = {round(float(p[1]), 3) for p in passages}
    starts.update(min(max_start, max(0, t - 0.12)) for t in boundaries)
    starts.update(min(max_start, max(0, t + 0.12 - seconds)) for t in endings)
    ordered = np.array(sorted(starts))
    axis = np.arange(bins + 1)
    evidence = (np.interp(ordered + seconds, axis, running) - np.interp(ordered, axis, running)) / seconds
    scores = evidence.copy()
    if passages:
        # Reward boundaries that avoid cutting off spoken words.
        b = np.array(sorted(boundaries | {0.0}))
        e = np.array(sorted(endings | {duration}))
        def proximity(values, targets):
            idx = np.searchsorted(targets, values).clip(0, len(targets)-1)
            distance = np.minimum(abs(targets[idx] - values), abs(targets[np.maximum(idx-1, 0)] - values))
            return np.exp(-distance / 0.65)
        scores += 0.075 * proximity(ordered, b) + 0.075 * proximity(ordered + seconds, e)
    # About one clip per four clip-lengths of source, between 1 and 10.
    limit = min(10, max(1, round(duration / (4 * seconds))))
    selected = []
    # The quota is a ceiling. A short exchange surrounded by silence needs one clip.
    peak = max(float(evidence.max()), 1e-8)
    floor = peak * (0.42 if passages else 0.20)
    for idx in np.argsort(-scores, kind="stable"):
        if evidence[idx] < floor:
            continue
        start = float(ordered[idx])
        if all(start + seconds <= prev + 1e-6 or start >= prev + seconds - 1e-6 for prev, _ in selected):
            selected.append((start, round(100 * float(evidence[idx]) / peak)))
            if len(selected) == limit:
                break
    progress(f"{len(selected)} moment(s) fort(s) sélectionné(s).", 68, "analyze")
    windows = []
    for start, score in sorted(selected):
        info = {"score": score}
        excerpt = _excerpt(passages, weights, start, start + seconds) if passages else ""
        if excerpt:
            info["excerpt"] = excerpt
        windows.append((start, seconds, info))
    return windows
