"""Split Studio as a native macOS app: the studio server in a WebKit window, no browser, no Terminal."""
import logging
import os
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.request import urlopen

import webview
from werkzeug.serving import make_server

os.environ["SPLITSTUDIO_NO_BROWSER"] = "1"
import app as studio  # noqa: E402  (reads SPLITSTUDIO_DATA set by the launcher)

ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def free_port(preferred=8765, attempts=20):
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Aucun port local disponible pour le studio.")


def brand_process():
    """Show the Split Studio name and icon in the Dock and menu bar, not Python's."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Split Studio"
        icon = ROOT / "SplitStudio.icns"
        if icon.exists():
            image = NSImage.alloc().initWithContentsOfFile_(str(icon))
            if image is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        logging.exception("Impossible d'appliquer l'icône de l'application")


class Bridge:
    """Native services for the page: Save dialog with progress, reveal in Finder."""

    def __init__(self, base):
        self._base = base
        self._transfers = {}
        self._lock = threading.Lock()
        self.window = None

    def save_file(self, url, filename):
        if not isinstance(url, str) or not url.startswith("/api/"):
            return {"error": "Adresse invalide."}
        result = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=str(filename or "clip.mp4"))
        if not result:
            return {"cancelled": True}
        destination = Path(result if isinstance(result, str) else result[0])
        token = uuid.uuid4().hex
        with self._lock:
            self._transfers[token] = {"received": 0, "total": 0, "done": False, "error": "", "path": str(destination)}
        threading.Thread(target=self._copy, args=(token, url, destination), daemon=True).start()
        return {"token": token, "path": str(destination), "name": destination.name}

    def _copy(self, token, url, destination):
        partial = destination.with_name(destination.name + ".part")
        state = self._transfers[token]
        try:
            with urlopen(self._base + url, timeout=600) as response, open(partial, "wb") as out:
                state["total"] = int(response.headers.get("Content-Length") or 0)
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    state["received"] += len(chunk)
            partial.replace(destination)
            state["done"] = True
        except Exception as exc:
            logging.exception("Échec de l'enregistrement")
            partial.unlink(missing_ok=True)
            state["error"] = str(getattr(exc, "reason", exc)) or "Échec de l'enregistrement."

    def transfer(self, token):
        with self._lock:
            return dict(self._transfers.get(token) or {"error": "Transfert inconnu."})

    def reveal(self, path):
        if path and Path(path).exists():
            subprocess.Popen(["open", "-R", str(path)])
        return True


def main():
    port = free_port()
    server = make_server("127.0.0.1", port, studio.app, threaded=True)
    studio.recover_interrupted_jobs()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{port}/"
    logging.info("Split Studio prêt · %s", address)
    bridge = Bridge(address.rstrip("/"))
    window = webview.create_window("Split Studio", address, js_api=bridge, width=1320, height=900,
                                   min_size=(760, 560), background_color="#F4F4F1", text_select=True)
    bridge.window = window
    try:
        brand_process()
        webview.start(private_mode=False, storage_path=str(studio.DATA / "webview"))
    finally:
        server.shutdown()


def crash_dialog(message):
    script = ('on run argv\n'
              'display dialog (item 1 of argv) with title "Split Studio" buttons {"Fermer"} '
              'default button "Fermer" with icon stop\nend run')
    subprocess.run(["osascript", "-e", script, message], check=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("Split Studio s'est arrêté")
        crash_dialog("Split Studio s’est arrêté de façon inattendue : " + str(exc)[:300]
                     + "\n\nJournal : ~/Library/Logs/Split Studio.log")
        raise
