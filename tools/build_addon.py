#!/usr/bin/env python3
"""Build and optionally install the Blender 5.2 Ocarina of Time Remake importer.

The native converter, third-party checkouts, licenses, and staging directory stay
under the ignored build/ tree. Installable release zips go to dist/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "oot_remake_importer"
BUILD_ROOT = PROJECT_ROOT / "build" / "blender-addon"
DIST_ROOT = PROJECT_ROOT / "dist"

with (SOURCE / "blender_manifest.toml").open("rb") as manifest_file:
    MANIFEST = tomllib.load(manifest_file)

PACKAGE_ID = MANIFEST["id"]
PACKAGE_VERSION = MANIFEST["version"]

BFRASS_URL = "https://github.com/RAMDRAGONS/BfrAss.git"
BFRASS_COMMIT = "c1ca30879029750a230ef7d3d5abcba7ec5c8afb"
ASSIMP_URL = "https://github.com/assimp/assimp.git"
ASSIMP_COMMIT = "78ba52270265bcf3b6534fcfd0f52bcfe6838640"


class BuildError(RuntimeError):
    pass


def validate_source_layout() -> None:
    required = (
        SOURCE / "__init__.py",
        SOURCE / "blender_manifest.toml",
        SOURCE / "data" / "material_semantics.json",
        PROJECT_ROOT / "LICENSE",
        PROJECT_ROOT / "THIRD_PARTY_NOTICES.md",
    )
    missing = tuple(path for path in required if not path.is_file())
    if missing:
        detail = ", ".join(str(path.relative_to(PROJECT_ROOT)) for path in missing)
        raise BuildError(f"add-on source is incomplete; missing: {detail}")
    if SOURCE.name != PACKAGE_ID:
        raise BuildError(
            f"add-on folder {SOURCE.name!r} does not match manifest id {PACKAGE_ID!r}"
        )


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise BuildError(f"{command[0]} exited with code {completed.returncode}")


def output(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip()
        raise BuildError(f"{command[0]} exited with code {completed.returncode}: {detail}")
    return completed.stdout.strip()


def checkout(url: str, commit: str, target: Path) -> None:
    if not (target / ".git").is_dir():
        if target.exists():
            raise BuildError(f"{target} exists but is not a Git checkout")
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", str(target)])
        run(["git", "-C", str(target), "remote", "add", "origin", url])
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    current = head.stdout.strip() if head.returncode == 0 else ""
    if current == commit:
        return
    status = output(["git", "-C", str(target), "status", "--porcelain"])
    if status:
        raise BuildError(f"third-party checkout has local changes: {target}")
    run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", commit])
    run(["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"])
    actual = output(["git", "-C", str(target), "rev-parse", "HEAD"])
    if actual != commit:
        raise BuildError(f"{target} resolved to {actual}, expected {commit}")


def native_library_name() -> str:
    if os.name == "nt":
        return "ootr_native.dll"
    if sys.platform == "darwin":
        return "ootr_native.dylib"
    return "ootr_native.so"


def build_native(native_root: Path, jobs: int) -> tuple[Path, Path, Path, Path]:
    bfrass = native_root / "bfrass"
    assimp = native_root / "assimp"
    build = native_root / "combined-build"
    checkout(BFRASS_URL, BFRASS_COMMIT, bfrass)
    checkout(ASSIMP_URL, ASSIMP_COMMIT, assimp)

    configure = [
        "cmake",
        "-S",
        str(PROJECT_ROOT / "native"),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DBFRASS_SOURCE_DIR={bfrass}",
        f"-DBFRASS_ASSIMP_SOURCE_DIR={assimp}",
        "-DASSIMP_BUILD_ALL_IMPORTERS_BY_DEFAULT=OFF",
        "-DASSIMP_BUILD_ALL_EXPORTERS_BY_DEFAULT=OFF",
        "-DASSIMP_BUILD_GLTF_EXPORTER=ON",
    ]
    run(configure)
    run(
        [
            "cmake",
            "--build",
            str(build),
            "--target",
            "bfrass-cli",
            "ootr_native",
            "--parallel",
            str(jobs),
        ]
    )
    executable = build / ("bfrass.exe" if os.name == "nt" else "bfrass")
    library = build / native_library_name()
    if not executable.is_file():
        raise BuildError(f"native build did not create {executable}")
    if not library.is_file():
        raise BuildError(f"native build did not create {library}")
    return executable, library, bfrass / "LICENSE", assimp / "LICENSE"


def copy_extension(
    stage: Path,
    executable: Path,
    library: Path,
    bfrass_license: Path,
    assimp_license: Path,
) -> None:
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(
        SOURCE,
        stage,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "bin", "licenses"),
    )
    bin_dir = stage / "bin"
    licenses = stage / "licenses"
    bin_dir.mkdir()
    licenses.mkdir()
    shutil.copy2(PROJECT_ROOT / "LICENSE", stage / "LICENSE")
    shutil.copy2(
        PROJECT_ROOT / "THIRD_PARTY_NOTICES.md",
        licenses / "THIRD_PARTY_NOTICES.md",
    )
    shutil.copy2(executable, bin_dir / ("bfrass.exe" if os.name == "nt" else "bfrass"))
    shutil.copy2(library, bin_dir / native_library_name())
    shutil.copy2(bfrass_license, licenses / "BfrAss-LICENSE.txt")
    shutil.copy2(assimp_license, licenses / "Assimp-LICENSE.txt")
    (licenses / "NOTICE.txt").write_text(
        "BfrAss\n"
        f"  Source: {BFRASS_URL.removesuffix('.git')} @ {BFRASS_COMMIT}\n"
        "  Copyright (c) 2026 Judas Drekonym\n"
        "  License: MIT (BfrAss-LICENSE.txt)\n\n"
        "Assimp\n"
        f"  Source: {ASSIMP_URL.removesuffix('.git')} @ {ASSIMP_COMMIT}\n"
        "  Copyright (c) 2006-2026 assimp team\n"
        "  License: BSD-3-Clause and bundled component notices (Assimp-LICENSE.txt)\n",
        encoding="utf-8",
    )


def make_zip(stage: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(stage).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, default=BUILD_ROOT)
    parser.add_argument("--dist-dir", type=Path, default=DIST_ROOT)
    parser.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 16)))
    parser.add_argument("--blender", type=Path, help="Blender executable, required with --install")
    parser.add_argument("--install", action="store_true", help="install and enable the built extension in Blender")
    args = parser.parse_args()

    validate_source_layout()
    build_root = args.build_dir.resolve()
    executable, library, bfrass_license, assimp_license = build_native(
        build_root / "native",
        args.jobs,
    )
    stage = build_root / "package"
    copy_extension(stage, executable, library, bfrass_license, assimp_license)
    package = args.dist_dir.resolve() / f"{PACKAGE_ID}-{PACKAGE_VERSION}.zip"
    make_zip(stage, package)
    print(f"Built {package} ({package.stat().st_size:,} bytes)")

    if args.install:
        if args.blender is None:
            raise BuildError("--install requires --blender")
        blender = args.blender.resolve()
        if not blender.is_file():
            raise BuildError(f"Blender executable does not exist: {blender}")
        run(
            [
                str(blender),
                "--background",
                "--command",
                "extension",
                "install-file",
                "-r",
                "user_default",
                "--enable",
                str(package),
            ]
        )
        print(f"Installed and enabled {PACKAGE_ID} in {blender.parent}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"build_addon: {error}", file=sys.stderr)
        raise SystemExit(1) from error
