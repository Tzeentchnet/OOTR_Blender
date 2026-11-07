"""Run the bundled BfrAss converter and validate its output."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ConversionError(RuntimeError):
    """The native BFRES-to-glTF conversion failed."""


@dataclass(frozen=True, slots=True)
class ConversionReport:
    output: Path
    log: str
    warnings: tuple[str, ...]


def convert_bfres(
    executable: str | os.PathLike[str],
    model: str | os.PathLike[str],
    output: str | os.PathLike[str],
    textures: Iterable[str | os.PathLike[str]] = (),
    *,
    import_lods: bool = False,
    model_name: str | None = None,
) -> ConversionReport:
    executable = Path(executable)
    model = Path(model)
    output = Path(output)
    texture_paths = tuple(Path(path) for path in textures)

    if not executable.is_file():
        raise ConversionError(f"BFRES converter is missing: {executable}")
    if not model.is_file():
        raise ConversionError(f"BFRES model is missing: {model}")

    command = [
        str(executable),
        "convert",
        str(model),
        "-o",
        str(output),
        "--no-auto-textures",
    ]
    if model_name is not None:
        if not model_name.strip():
            raise ConversionError("BFRES model selection cannot be empty")
        command.extend(("--model", model_name))
    if texture_paths:
        command.append("--embed-textures")
        for texture in texture_paths:
            if not texture.is_file():
                raise ConversionError(f"BNTX texture is missing: {texture}")
            command.extend(("--textures", str(texture)))
    else:
        command.append("--no-texture-files")
    if import_lods:
        command.append("--lods")

    if os.name == "nt" and len(subprocess.list2cmdline(command)) >= 30_000:
        raise ConversionError("the model references too many textures for the Windows command-line limit")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        cwd=output.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    log = completed.stdout + completed.stderr
    if completed.returncode != 0:
        detail = "\n".join(log.strip().splitlines()[-20:])
        raise ConversionError(f"BfrAss exited with code {completed.returncode}\n{detail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise ConversionError("BfrAss reported success but did not create a glTF file")
    warnings = tuple(line for line in log.splitlines() if line.lower().startswith("warning:"))
    return ConversionReport(output=output, log=log, warnings=warnings)
