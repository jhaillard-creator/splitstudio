import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock, RLock, Timer
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from distribution import package
from tiktok import bp

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SPLITSTUDIO_DATA", ROOT / ".studio")).resolve()
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(DATA / "models"))
# Clips are also filed, one folder per video, where the Finder can see them.
_home = Path.home()
EXPORT = Path(os.environ.get("SPLITSTUDIO_EXPORT") or
              ((_home / "Movies" if (_home / "Movies").is_dir() else _home) / "Split Studio")).resolve()
import video

app = Flask(__name__, static_folder=None)
app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=32768,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)
pool = ThreadPoolExecutor(max_workers=1)
lock = Lock()
store = RLock()
busy = False


@app.before_request
def protect_local_app():
    if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
        abort(403)
    if request.method == "POST":
        if not request.is_json or request.headers.get("Origin") != request.host_url.rstrip("/"):
            abort(403)


@app.errorhandler(HTTPException)
def api_error(exc):
    if request.path.startswith("/api/"):
        return jsonify(error=exc.description), exc.code
    return exc


@app.get("/")
def home():
    return send_from_directory(ROOT / "dist", "index.html")


@app.get("/SplitStudio.zip")
def download_app():
    return send_file(package(), mimetype="application/zip", as_attachment=True, download_name="SplitStudio.zip")


@app.get("/<path:asset>")
def static_asset(asset):
    return send_from_directory(ROOT / "dist", asset)


@app.get("/api/health")
def health():
    try:
        version = (ROOT / "VERSION").read_text().strip()
    except OSError:
        version = "dev"
    return jsonify(ok=True, app="SplitStudio", version=version)


def job_folder(key):
    try:
        if uuid.UUID(hex=key).hex != key:
            abort(404)
    except ValueError:
        abort(404)
    return DATA / key


def read_job(key):
    try:
        return json.loads((job_folder(key) / "job.json").read_text())
    except FileNotFoundError:
        abort(404)


def save(folder, record):
    tmp = folder / "job.new"
    tmp.write_text(json.dumps(record, ensure_ascii=False))
    tmp.replace(folder / "job.json")


def produce(folder, record, url, secondary, captions):
    global busy
    last_save = 0.0
    guard = store

    def progress(message, percent=None, stage=None):
        nonlocal last_save
        with guard:
            now = time.monotonic()
            stage_changed = stage != record.get("stage")
            record["message"] = str(message)
            if percent is not None:
                record["progress"] = max(record["progress"], min(99, int(percent)))
            if stage:
                record["stage"] = stage
            if stage_changed or now - last_save >= 0.2:
                save(folder, record)
                last_save = now

    def on_title(title):
        with guard:
            record["title"] = title
            save(folder, record)

    def on_clip(path, start, end, info):
        # Publish each clip as soon as it is encoded, so it can be previewed and downloaded right away.
        with guard:
            clip = dict(id=path.name, url=f"/api/jobs/{folder.name}/clips/{path.name}",
                        start=start, end=end, name=f"Partie {info.get('part', len(record['clips']) + 1)}", **info)
            try:
                file_clip(folder, clip, record.get("folder") or default_folder(record))
            except OSError:
                logging.exception("Classement du clip impossible")
            record["clips"].append(clip)
            record["clips"].sort(key=lambda c: c["start"])
            save(folder, record)

    try:
        video.render(url, secondary, record["seconds"], folder, progress, captions,
                     highlights=record["highlights"], on_clip=on_clip, on_title=on_title)
        with guard:
            record.update(status="done", message=f"{len(record['clips'])} clip(s) prêt(s).", stage="done", progress=100)
    except Exception as exc:
        logging.exception("Échec du montage")
        with guard:
            record.update(status="error", message=str(exc)[:600] or "Le montage a échoué.")
    finally:
        try:
            with guard:
                save(folder, record)
        finally:
            with lock:
                busy = False


def clean_folder(name):
    name = re.sub(r"[\x00-\x1f/:\\]+", " ", str(name or ""))
    name = " ".join(name.split()).strip(" .")
    return name[:80].rstrip(" .")


def next_video_folder():
    """Next free "Vidéo N" folder name, counting the Finder folders and the studio's history."""
    pattern = re.compile(r"^Vid[eé]o (\d+)$", re.IGNORECASE)
    used = [0]
    if EXPORT.is_dir():
        used += [int(m.group(1)) for p in EXPORT.iterdir() if p.is_dir() and (m := pattern.match(p.name))]
    for path in DATA.glob("*/job.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        names = [record.get("folder", "")] + [c.get("folder", "") for c in record.get("clips", [])]
        used += [int(m.group(1)) for name in names if (m := pattern.match(name or ""))]
    return f"Vidéo {max(used) + 1}"


def default_folder(record):
    when = time.localtime(record.get("created", time.time()))
    return time.strftime("Vidéo du %d-%m-%Y %Hh%M", when)


def folder_of(record, clip):
    return clip.get("folder") or record.get("folder") or default_folder(record)


def _unique(directory, stem):
    candidate, n = directory / f"{stem}.mp4", 2
    while candidate.exists():
        candidate, n = directory / f"{stem} ({n}).mp4", n + 1
    return candidate


def file_clip(job_dir, clip, target):
    """Put (or move) the clip's Finder copy into EXPORT/target and record where it is."""
    target = clean_folder(target)
    if not target:
        raise ValueError("Nom de dossier invalide.")
    current = EXPORT / clip["file"] if clip.get("file") else None
    if current and current.parent.name == target and current.exists():
        clip["folder"] = target
        return
    directory = EXPORT / target
    directory.mkdir(parents=True, exist_ok=True)
    destination = _unique(directory, clip["name"])
    if current and current.exists():
        current.replace(destination)
        try:
            current.parent.rmdir()
        except OSError:
            pass
    else:
        source = job_dir / clip["id"]
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    clip["folder"] = target
    clip["file"] = str(destination.relative_to(EXPORT))


def valid_url(value):
    try:
        u = urlsplit(value)
        return u.scheme in ("https", "http") and bool(u.hostname) and not u.username and not u.password
    except (ValueError, TypeError, AttributeError):
        return False


@app.post("/api/jobs")
def create_job():
    global busy
    form = request.get_json()
    if not isinstance(form, dict):
        abort(400)
    url, secondary = form.get("url"), form.get("secondary_url")
    seconds, captions = form.get("seconds"), form.get("captions", True)
    highlights = form.get("highlights", True)
    if not valid_url(url) or (secondary and not valid_url(secondary)):
        return jsonify(error="Saisis une URL vidéo HTTP ou HTTPS valide."), 400
    if type(seconds) is not int or seconds not in video.DURATIONS or type(captions) is not bool or type(highlights) is not bool:
        return jsonify(error="Paramètres de montage invalides."), 400
    key = uuid.uuid4().hex
    record = dict(id=key, created=time.time(), status="running", seconds=seconds,
                  message="Préparation du montage…", highlights=highlights, progress=0, stage="download", clips=[])
    with lock:
        if busy:
            return jsonify(error="Un montage est déjà en cours."), 409
        record["folder"] = next_video_folder()
        folder = DATA / key
        folder.mkdir(mode=0o700)
        save(folder, record)
        busy = True
        pool.submit(produce, folder, record, url, secondary or None, captions)
    return jsonify(id=key), 202


@app.get("/api/jobs")
def history():
    jobs = []
    for path in DATA.glob("*/job.json"):
        try:
            record = json.loads(path.read_text())
            for clip in record.get("clips", []):
                clip.setdefault("folder", folder_of(record, clip))
            jobs.append(record)
        except (OSError, ValueError):
            logging.warning("Historique illisible ignoré : %s", path)
    return jsonify(jobs=sorted(jobs, key=lambda r: r.get("created", 0), reverse=True))


@app.get("/api/jobs/<key>")
def job(key):
    return jsonify(read_job(key))


def clip_path(key, clip_id):
    record = read_job(key)
    if not any(c["id"] == clip_id for c in record["clips"]):
        abort(404)
    return job_folder(key) / clip_id


@app.get("/api/jobs/<key>/clips/<clip_id>")
def clip(key, clip_id):
    path = clip_path(key, clip_id)
    return send_from_directory(path.parent, path.name, conditional=True)


@app.get("/api/jobs/<key>/archive")
def archive(key):
    """All clips of a finished job in one ZIP (stored, not recompressed: MP4 is already compressed)."""
    record = read_job(key)
    if record.get("status") != "done" or not record.get("clips"):
        abort(409, "Les clips de ce montage ne sont pas encore prêts.")
    folder = job_folder(key)
    target = folder / "clips.zip"
    if not target.exists():
        tmp = folder / "clips.zip.part"
        with ZipFile(tmp, "w", ZIP_STORED) as bundle:
            for clip in record["clips"]:
                bundle.write(folder / clip["id"], f"{clip['name']}.mp4")
        tmp.replace(target)
    stamp = time.strftime("%Y-%m-%d-%Hh%M", time.localtime(record.get("created", time.time())))
    return send_from_directory(folder, target.name, conditional=True, as_attachment=True,
                               download_name=f"Split Studio {stamp}.zip")


def _selection(items):
    """[(job_dir, record, clip)] for a list of {"job": ..., "clip": ...} references."""
    if not isinstance(items, list) or not items or len(items) > 2000:
        abort(400, "Sélection invalide.")
    records, picked = {}, []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("job"), str) or not isinstance(item.get("clip"), str):
            abort(400, "Sélection invalide.")
        if item["job"] not in records:
            records[item["job"]] = read_job(item["job"])
        record = records[item["job"]]
        clip = next((c for c in record["clips"] if c["id"] == item["clip"]), None)
        if clip is None:
            abort(404, "Clip introuvable.")
        picked.append((job_folder(item["job"]), record, clip))
    return picked


def _move(picked, target):
    if any(record.get("status") == "running" for _, record, _ in picked):
        abort(409, "Attends la fin du montage en cours pour ranger ses clips.")
    touched = {}
    for job_dir, record, clip in picked:
        file_clip(job_dir, clip, target)
        touched[job_dir] = record
    for job_dir, record in touched.items():
        save(job_dir, record)


@app.post("/api/clips/move")
def move_clips():
    form = request.get_json()
    target = clean_folder(form.get("folder") if isinstance(form, dict) else None)
    if not target:
        return jsonify(error="Donne un nom au dossier."), 400
    with store:
        picked = _selection(form.get("clips"))
        _move(picked, target)
    return jsonify(ok=True, folder=target)


def _clips_in(folder):
    found = []
    for path in DATA.glob("*/job.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for clip in record.get("clips", []):
            if folder_of(record, clip) == folder:
                found.append({"job": record["id"], "clip": clip["id"]})
    return found


@app.post("/api/folders/rename")
def rename_folder():
    form = request.get_json()
    if not isinstance(form, dict):
        abort(400)
    source, target = form.get("from"), clean_folder(form.get("to"))
    if not isinstance(source, str) or not target:
        return jsonify(error="Donne un nom au dossier."), 400
    with store:
        items = _clips_in(source)
        if not items:
            abort(404, "Dossier introuvable.")
        _move(_selection(items), target)
    return jsonify(ok=True, folder=target)


@app.post("/api/folders/open")
def open_folder():
    form = request.get_json()
    name = clean_folder(form.get("folder")) if isinstance(form, dict) and form.get("folder") else ""
    path = EXPORT / name if name else EXPORT
    path.mkdir(parents=True, exist_ok=True)
    opener = {"Darwin": "open", "Windows": "explorer"}.get(platform.system(), "xdg-open")
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return jsonify(error=f"Ouvre ce dossier manuellement : {path}"), 501
    return jsonify(ok=True, path=str(path))


@app.get("/api/library")
def library():
    return jsonify(path=str(EXPORT))


@app.get("/api/archive")
def archive_selection():
    """ZIP (stored) of any set of clips: c=<job>:<clip>, repeated."""
    refs = [dict(zip(("job", "clip"), value.split(":", 1))) for value in request.args.getlist("c") if ":" in value]
    picked = _selection(refs)
    if any(record.get("status") != "done" for _, record, _ in picked):
        abort(409, "Les clips de ce montage ne sont pas encore prêts.")
    cache = DATA / ".archives"
    cache.mkdir(mode=0o700, exist_ok=True)
    for old in cache.glob("*.zip"):
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    digest = hashlib.sha256(json.dumps([[str(d), c["id"], c["name"]] for d, _, c in picked]).encode()).hexdigest()[:24]
    target = cache / f"{digest}.zip"
    if not target.exists():
        tmp = cache / f"{digest}.part"
        used = set()
        with ZipFile(tmp, "w", ZIP_STORED) as bundle:
            for job_dir, record, clip in picked:
                folder = folder_of(record, clip)
                name, n = f"{folder}/{clip['name']}.mp4", 2
                while name in used:
                    name, n = f"{folder}/{clip['name']} ({n}).mp4", n + 1
                used.add(name)
                bundle.write(job_dir / clip["id"], name)
        tmp.replace(target)
    label = clean_folder(request.args.get("name")) or "Split Studio"
    return send_from_directory(cache, target.name, conditional=True, as_attachment=True,
                               download_name=f"{label}.zip")


def resolve_clip(key, clip_id):
    path = clip_path(key, clip_id)
    return path, video.duration_seconds(path)


app.config["TIKTOK_DATA_DIR"] = DATA / "tiktok"
app.config["TIKTOK_CLIP_RESOLVER"] = resolve_clip
app.register_blueprint(bp)

def recover_interrupted_jobs():
    for file in DATA.glob("*/job.json"):
        try:
            job_data = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        if job_data.get("status") == "running":
            job_data.update(status="error", message="Le studio a été fermé pendant le montage. Relance la création.")
            save(file.parent, job_data)


if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("SPLITSTUDIO_PORT", "8765"))
    server = make_server("127.0.0.1", port, app, threaded=True)
    recover_interrupted_jobs()
    address = f"http://127.0.0.1:{port}"
    print(f"Split Studio est prêt · {address}", flush=True)
    if not os.environ.get("SPLITSTUDIO_NO_BROWSER"):
        Timer(0.6, webbrowser.open, args=(address,)).start()
    server.serve_forever()
