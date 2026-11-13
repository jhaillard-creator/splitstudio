"""TikTok Login Kit (desktop) and Direct Post for a single-user loopback app."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, jsonify, redirect, request, session

bp = Blueprint("tiktok", __name__, url_prefix="/api/tiktok")
API = "https://open.tiktokapis.com/v2/"
auth_lock = threading.RLock()
post_lock = threading.Lock()


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@bp.errorhandler(Problem)
def report(error):
    return jsonify(error=str(error)), error.status


@bp.before_request
def local_access():
    if urlsplit(request.host_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise Problem("Ouvrez le studio depuis son adresse locale.", 403)
    if request.method == "POST":
        if request.headers.get("Origin") != request.host_url.rstrip("/") or not request.is_json:
            raise Problem("Cette action doit être lancée depuis le studio.", 403)


@bp.after_request
def private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def data_dir():
    base = Path(current_app.config.get("TIKTOK_DATA_DIR", Path(current_app.instance_path) / "tiktok"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def stored_credentials():
    try:
        saved = json.loads((data_dir() / "credentials.json").read_text())
        return saved.get("client_key"), saved.get("client_secret")
    except (OSError, ValueError, AttributeError):
        return None, None


def settings():
    saved_key, saved_secret = stored_credentials()
    key = os.getenv("TIKTOK_CLIENT_KEY") or saved_key
    secret = os.getenv("TIKTOK_CLIENT_SECRET") or saved_secret
    uri = os.getenv("TIKTOK_REDIRECT_URI") or request.host_url.rstrip("/") + "/api/tiktok/callback"
    return key, secret, uri


def database():
    filename = data_dir() / "account.sqlite3"
    os.close(os.open(filename, os.O_CREAT | os.O_RDWR, 0o600))
    db = sqlite3.connect(str(filename), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, token TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS posts (
        open_id TEXT, job TEXT, clip TEXT, publish_id TEXT UNIQUE, state TEXT, reason TEXT,
        PRIMARY KEY (open_id, job, clip))""")
    return db


def saved_token():
    with closing(database()) as db:
        row = db.execute("SELECT token FROM account WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def api(path, data, token=None, form=False):
    payload = urlencode(data).encode() if form else json.dumps(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(API + path, data=payload, headers=headers)
    try:
        with urlopen(req, timeout=35) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, UnicodeError):
            raise Problem(f"TikTok a répondu avec le code HTTP {exc.code}.", 502)
        if not result.get("error"):
            raise Problem(f"TikTok a répondu avec le code HTTP {exc.code}.", 502)
    except (URLError, TimeoutError, OSError, ValueError):
        raise Problem("TikTok est momentanément inaccessible.", 502)
    error = result.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    if code and code != "ok":
        raise Problem(f"TikTok a refusé la demande ({code}).", 409)
    return result if form else result.get("data", {})


def exchange(params):
    key, secret, _ = settings()
    result = api("oauth/token/", dict(params, client_key=key, client_secret=secret), form=True)
    if not all(result.get(k) for k in ("access_token", "refresh_token", "open_id", "expires_in")):
        raise Problem("TikTok a renvoyé une autorisation incomplète.", 502)
    if "video.publish" not in result.get("scope", "").split(","):
        raise Problem("Autorisez la publication vidéo pendant la connexion.", 403)
    result["expires_at"] = time.time() + result["expires_in"]
    with closing(database()) as db, db:
        db.execute("INSERT OR REPLACE INTO account VALUES (1,?)", (json.dumps(result),))
    return result


def account_token():
    with auth_lock:
        token = saved_token()
        if not token:
            raise Problem("Associez votre compte TikTok pour publier.", 401)
        if token["expires_at"] < time.time() + 120:
            return exchange({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        return token


def creator(token):
    return api("post/publish/creator_info/query/", {}, token["access_token"])


@bp.post("/setup")
def setup():
    """Save the TikTok for Developers keys from the in-app setup, readable by this user only."""
    data = request.get_json(silent=True) or {}
    key, secret = str(data.get("client_key", "")).strip(), str(data.get("client_secret", "")).strip()
    if not (4 <= len(key) <= 200 and 4 <= len(secret) <= 200) or any(c.isspace() for c in key + secret):
        raise Problem("Colle la Client key et le Client secret de ton application TikTok.")
    target = data_dir() / "credentials.json"
    tmp = target.with_suffix(".part")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"client_key": key, "client_secret": secret}, out)
    tmp.replace(target)
    return jsonify(configured=True)


@bp.get("/posts")
def posts():
    token = saved_token()
    if not token:
        return jsonify(posts=[])
    with closing(database()) as db:
        rows = db.execute("SELECT job, clip, state FROM posts WHERE open_id=?", (token["open_id"],)).fetchall()
    return jsonify(posts=[dict(job=j, clip=c, state=st) for j, c, st in rows])


@bp.get("/account")
def account():
    configured = all(settings()[:2])
    redirect_uri = settings()[2]
    if not configured or not saved_token():
        return jsonify(configured=configured, connected=False, redirect_uri=redirect_uri)
    try:
        info = creator(account_token())
        return jsonify(configured=True, connected=True, creator=info)
    except Problem as exc:
        return jsonify(configured=True, connected=True, error=str(exc))


@bp.get("/connect")
def connect():
    key, secret, uri = settings()
    if not key or not secret:
        raise Problem("Configurez les identifiants TikTok développeur avant la connexion.", 503)
    if uri != request.host_url.rstrip("/") + "/api/tiktok/callback":
        raise Problem("Ouvrez le studio à l’adresse locale enregistrée pour TikTok.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    session["tiktok_flow"] = [state, verifier, time.time()]
    params = dict(client_key=key, response_type="code", scope="video.publish", state=state,
                  redirect_uri=uri, code_challenge=hashlib.sha256(verifier.encode()).hexdigest(),
                  code_challenge_method="S256")
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params))


@bp.get("/callback")
def callback():
    flow = session.pop("tiktok_flow", None)
    try:
        if not flow or time.time() - flow[2] > 600 or not hmac.compare_digest(
            flow[0].encode(), request.args.get("state", "").encode()
        ):
            raise Problem("La connexion a expiré. Réessayez depuis le studio.")
        if not request.args.get("code") or request.args.get("error"):
            raise Problem("Connexion TikTok annulée.")
        with auth_lock:
            exchange(dict(grant_type="authorization_code", code=request.args["code"],
                          redirect_uri=settings()[2], code_verifier=flow[1]))
    except Problem as exc:
        return redirect("/?" + urlencode({"tiktok": "error", "message": str(exc)}))
    return redirect("/?tiktok=connected")


@bp.post("/disconnect")
def disconnect():
    with auth_lock:
        token = saved_token()
        if token:
            key, secret, _ = settings()
            api("oauth/revoke/", dict(client_key=key, client_secret=secret,
                                      token=account_token()["access_token"]), form=True)
            with closing(database()) as db, db:
                db.execute("DELETE FROM account")
    session.pop("tiktok_flow", None)
    return jsonify(connected=False)


def post_options(data, info, duration):
    if not info.get("creator_username") or info["creator_username"] != data.get("creator_username"):
        raise Problem("Le compte associé a changé. Rouvrez la fenêtre de publication.", 409)
    if duration > info.get("max_video_post_duration_sec", 0) + 0.04:
        raise Problem("Ce clip dépasse la durée maximale autorisée par ce compte.")
    privacy = data.get("privacy_level")
    if privacy not in info.get("privacy_level_options", []):
        raise Problem("Sélectionnez une visibilité disponible pour ce compte.")
    branded = data.get("brand_content_toggle") is True
    if branded and privacy == "SELF_ONLY":
        raise Problem("Le contenu de marque nécessite une visibilité autre que privée.")
    title = data.get("title", "")
    if not isinstance(title, str) or len(title.encode("utf-16-le")) // 2 > 2200:
        raise Problem("La légende dépasse la longueur autorisée.")
    options = dict(title=title, privacy_level=privacy, brand_content_toggle=branded,
                   brand_organic_toggle=data.get("brand_organic_toggle") is True)
    for action in ("comment", "duet", "stitch"):
        options["disable_" + action] = bool(info.get(action + "_disabled", True)
                                             or data.get("allow_" + action) is not True)
    return options


def update_status(pid, state, reason=""):
    with closing(database()) as db, db:
        db.execute("UPDATE posts SET state=?, reason=? WHERE publish_id=?", (state, reason, pid))


def upload(app, pid, url, path, size, chunk, count):
    with app.app_context():
        try:
            with path.open("rb") as stream:
                for index in range(count):
                    start = stream.tell()
                    length = size - start if index == count - 1 else chunk
                    data = stream.read(length)
                    if len(data) != length:
                        raise OSError("incomplete file")
                    req = Request(url, data=data, method="PUT", headers={
                        "Content-Type": "video/mp4", "Content-Length": str(length),
                        "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
                    with urlopen(req, timeout=180) as response:
                        if response.status not in {201, 206}:
                            raise OSError("upload status")
            update_status(pid, "PROCESSING_UPLOAD")
        except (URLError, OSError, ValueError):
            # The final response can be lost after TikTok accepted the clip.
            # Poll TikTok before allowing any new post for the same clip.
            update_status(pid, "VERIFY_UPLOAD", "Transfert interrompu. Vérification auprès de TikTok.")


@bp.post("/publish")
def publish():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("consent") is not True:
        raise Problem("Confirmez la publication de ce clip.")
    job, clip = data.get("job_id"), data.get("clip_id")
    if not isinstance(job, str) or not isinstance(clip, (int, str)):
        raise Problem("Clip inconnu.", 404)
    try:
        path, duration = current_app.config["TIKTOK_CLIP_RESOLVER"](job, str(clip))
        path = Path(path)
        size = path.stat().st_size
    except (LookupError, OSError, TypeError, ValueError):
        raise Problem("Le fichier de ce clip est introuvable.", 404)
    chunk = size if size <= 64 * 1024**2 else 32 * 1024**2
    count = size // chunk if chunk else 0
    if not 1 <= count <= 1000 or size > 4 * 1024**3 or duration < 3:
        raise Problem("Ce clip doit durer au moins 3 secondes et peser au maximum 4 Go.")
    with post_lock:
        token = account_token()
        ids = (token["open_id"], job, str(clip))
        with closing(database()) as db, db:
            prior = db.execute("SELECT publish_id,state FROM posts WHERE open_id=? AND job=? AND clip=?", ids).fetchone()
            if prior and prior[1] != "FAILED":
                return jsonify(publish_id=prior[0])
            options = post_options(data, creator(token), duration)
            result = api("post/publish/video/init/", dict(post_info=options, source_info=dict(
                source="FILE_UPLOAD", video_size=size, chunk_size=chunk, total_chunk_count=count)),
                token["access_token"])
            pid, url = result.get("publish_id"), result.get("upload_url")
            if not pid or not url or urlsplit(url).scheme != "https":
                raise Problem("TikTok a renvoyé une adresse de transfert invalide.", 502)
            db.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?)", (*ids, pid, "TRANSFERRING", ""))
        app = current_app._get_current_object()
        threading.Thread(target=upload, args=(app, pid, url, path, size, chunk, count), daemon=True).start()
    return jsonify(publish_id=pid), 202


@bp.get("/status/<path:pid>")
def status(pid):
    token = account_token()
    with closing(database()) as db:
        row = db.execute("SELECT state,reason FROM posts WHERE open_id=? AND publish_id=?",
                         (token["open_id"], pid)).fetchone()
    if not row:
        raise Problem("Publication introuvable.", 404)
    if row[0] in {"FAILED", "PUBLISH_COMPLETE"}:
        return jsonify(status=row[0], fail_reason=row[1])
    result = api("post/publish/status/fetch/", {"publish_id": pid}, token["access_token"])
    state, reason = result.get("status", "PROCESSING_UPLOAD"), result.get("fail_reason", "")
    if state in {"FAILED", "PUBLISH_COMPLETE"}:
        update_status(pid, state, reason)
    return jsonify(status=state, fail_reason=reason)
