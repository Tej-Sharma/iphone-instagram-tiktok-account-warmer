"""Build the downloadable AutoWarmer zip.

    python3 tools/make_zip.py 1.0.0

Produces dist/AutoWarmer-<version>.zip containing the app as a user gets it.
Run by the release workflow; works locally too.
"""
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["autowarmer", "tools/vision_ocr.swift", "README.md", "LICENSE",
           "config.example.json", "AutoWarmer.command"]


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    name = f"AutoWarmer-{version}"
    zip_path = out / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = ROOT / item
            if p.is_file():
                z.write(p, f"{name}/{item}")
            else:
                for f in sorted(p.rglob("*")):
                    if f.is_file() and "__pycache__" not in f.parts:
                        z.write(f, f"{name}/{f.relative_to(ROOT)}")
        # the launcher needs its executable bit to survive the round trip
        info = z.getinfo(f"{name}/AutoWarmer.command")
        info.external_attr = 0o755 << 16
    size = zip_path.stat().st_size
    print(f"built {zip_path} ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
