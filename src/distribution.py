"""Build the downloadable Mac app from this release's distributable files."""
from io import BytesIO
from pathlib import Path
import plistlib
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
APP = "Split Studio.app/Contents/"
RESOURCES = APP + "Resources/"
FILES = ("app.py", "desktop.py", "video.py", "highlights.py", "distribution.py", "tiktok.py", "launcher.sh",
         "requirements.txt", "requirements-video.txt", "DEMARRER.md", "TIKTOK.md", "SplitStudio.icns")
ASSETS = ("index.html", "app.js", "style.css", "exemple.jpg")
VERSION = "3.4"
BUILD = 9


def info_plist():
    return plistlib.dumps({
        "CFBundleName": "Split Studio", "CFBundleDisplayName": "Split Studio",
        "CFBundleIdentifier": "studio.split.app", "CFBundleVersion": str(BUILD),
        "CFBundleShortVersionString": VERSION, "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "SplitStudio", "CFBundleIconFile": "SplitStudio",
        "CFBundleDevelopmentRegion": "fr", "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.video",
        "NSHighResolutionCapable": True, "NSHumanReadableCopyright": "Split Studio",
    })


def package():
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        def add(name, content, executable=False):
            entry = ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (0o100755 if executable else 0o100644) << 16
            archive.writestr(entry, content, compress_type=ZIP_DEFLATED)

        add(APP + "Info.plist", info_plist())
        add(APP + "PkgInfo", b"APPL????")
        add(APP + "MacOS/SplitStudio", (ROOT / "launcher.sh").read_bytes(), True)
        add(RESOURCES + "BUILD", f"{BUILD}\n")
        add(RESOURCES + "VERSION", f"{VERSION}\n")
        for name in FILES:
            add(RESOURCES + name, (ROOT / name).read_bytes(), name == "launcher.sh")
        for name in ASSETS:
            add(RESOURCES + "dist/" + name, (ROOT / "dist" / name).read_bytes())
        add("Lisez-moi.txt", (ROOT / "DEMARRER.md").read_bytes())
        add("Configurer TikTok.txt", (ROOT / "TIKTOK.md").read_bytes())
    buffer.seek(0)
    return buffer
