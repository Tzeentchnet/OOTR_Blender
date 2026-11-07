"""Level placement discovery and actor-model resolution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .byml import BymlError, BymlValue, read_byml


PLACEMENT_SUFFIX = ".bcett.byml.zs"
LEVEL_PATH = re.compile(
    r"^Banc/(?P<world>[^/]+)/(?P<name>.+)_(?P<kind>Static|Dynamic)"
    + re.escape(PLACEMENT_SUFFIX)
    + r"$"
)


class LevelError(ValueError):
    """A level placement or actor model reference is malformed."""


@dataclass(frozen=True, slots=True)
class LevelDefinition:
    identifier: str
    world: str
    name: str
    static_path: str | None
    dynamic_path: str | None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.static_path, self.dynamic_path) if path is not None)


@dataclass(frozen=True, slots=True)
class PlacementActor:
    gyaml: str
    actor_hash: int
    srt_hash: int | None
    translate: tuple[float, float, float]
    rotate: tuple[float, float, float]
    scale: tuple[float, float, float]
    name: str | None
    dynamic: bool
    source_path: str


@dataclass(frozen=True, slots=True)
class PlacementData:
    source_path: str
    dynamic: bool
    actors: tuple[PlacementActor, ...]
    skipped_without_transform: int


@dataclass(frozen=True, slots=True)
class ActorModel:
    project_name: str
    fmdb_name: str
    resource_path: str


@dataclass(frozen=True, slots=True)
class ActorModelResolution:
    gyaml: str
    models: tuple[ActorModel, ...]
    reason: str | None = None


def discover_levels(entries: dict[str, object]) -> tuple[LevelDefinition, ...]:
    grouped: dict[str, dict[str, str | None]] = {}
    for path in entries:
        match = LEVEL_PATH.fullmatch(path)
        if match is None:
            continue
        world = match.group("world")
        name = match.group("name")
        identifier = f"{world}/{name}"
        values = grouped.setdefault(
            identifier,
            {
                "world": world,
                "name": name,
                "static_path": None,
                "dynamic_path": None,
            },
        )
        key = "static_path" if match.group("kind") == "Static" else "dynamic_path"
        if values[key] is not None:
            raise LevelError(f"duplicate {match.group('kind').lower()} placement for {identifier}")
        values[key] = path

    return tuple(
        LevelDefinition(
            identifier=identifier,
            world=str(values["world"]),
            name=str(values["name"]),
            static_path=values["static_path"],
            dynamic_path=values["dynamic_path"],
        )
        for identifier, values in sorted(
            grouped.items(),
            key=lambda item: (
                str(item[1]["world"]).casefold(),
                str(item[1]["name"]).casefold(),
            ),
        )
    )


def _document_dict(value: BymlValue, label: str) -> dict[str, BymlValue]:
    if not isinstance(value, dict):
        raise LevelError(f"{label} root is not a dictionary")
    return value


def _vector(value: BymlValue, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise LevelError(f"{label} is not a three-component vector")
    result: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise LevelError(f"{label} contains a non-numeric component")
        converted = float(component)
        if not math.isfinite(converted):
            raise LevelError(f"{label} contains a non-finite component")
        result.append(converted)
    return result[0], result[1], result[2]


def read_placement(
    data: bytes,
    source_path: str,
    *,
    dynamic: bool,
) -> PlacementData:
    try:
        root = _document_dict(read_byml(data), source_path)
    except BymlError as error:
        raise LevelError(f"cannot read {source_path}: {error}") from error
    raw_actors = root.get("Actors", [])
    if not isinstance(raw_actors, list):
        raise LevelError(f"{source_path} Actors is not an array")

    actors: list[PlacementActor] = []
    skipped = 0
    for index, value in enumerate(raw_actors):
        label = f"{source_path} actor {index}"
        if not isinstance(value, dict):
            raise LevelError(f"{label} is not a dictionary")
        if "Translate" not in value:
            skipped += 1
            continue
        gyaml = value.get("Gyaml")
        actor_hash = value.get("Hash")
        srt_hash = value.get("SRTHash")
        name = value.get("Name")
        if not isinstance(gyaml, str) or not gyaml:
            raise LevelError(f"{label} has no Gyaml name")
        if isinstance(actor_hash, bool) or not isinstance(actor_hash, int):
            raise LevelError(f"{label} has no integer Hash")
        if srt_hash is not None and (isinstance(srt_hash, bool) or not isinstance(srt_hash, int)):
            raise LevelError(f"{label} has an invalid SRTHash")
        if name is not None and not isinstance(name, str):
            raise LevelError(f"{label} has an invalid Name")
        actors.append(
            PlacementActor(
                gyaml=gyaml,
                actor_hash=actor_hash,
                srt_hash=srt_hash,
                translate=_vector(value["Translate"], f"{label} Translate"),
                rotate=_vector(value.get("Rotate", [0.0, 0.0, 0.0]), f"{label} Rotate"),
                scale=_vector(value.get("Scale", [1.0, 1.0, 1.0]), f"{label} Scale"),
                name=name,
                dynamic=dynamic,
                source_path=source_path,
            )
        )
    return PlacementData(
        source_path=source_path,
        dynamic=dynamic,
        actors=tuple(actors),
        skipped_without_transform=skipped,
    )


def actor_pack_path(gyaml: str) -> str:
    if (
        not gyaml
        or gyaml in (".", "..")
        or "/" in gyaml
        or "\\" in gyaml
        or PurePosixPath(gyaml).name != gyaml
    ):
        raise LevelError(f"unsafe actor name {gyaml!r}")
    return f"Pack/Actor/{gyaml}.pack.zs"


def _actor_parameter(
    gyaml: str,
    members: dict[str, bytes],
) -> dict[str, BymlValue] | None:
    expected = f"Actor/{gyaml}.engine__actor__ActorParam.bgyml"
    data = members.get(expected)
    if data is None:
        candidates = [
            member
            for name, member in members.items()
            if name.startswith("Actor/") and name.endswith(".engine__actor__ActorParam.bgyml")
        ]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise LevelError(f"actor pack for {gyaml} has no unambiguous ActorParam")
        data = candidates[0]
    try:
        return _document_dict(read_byml(data), f"ActorParam for {gyaml}")
    except BymlError as error:
        raise LevelError(f"cannot read ActorParam for {gyaml}: {error}") from error


def _model_info_member(reference: str) -> str:
    member = reference.removeprefix("?")
    if member.startswith("Work/"):
        member = member[len("Work/"):]
    if member.endswith(".gyml"):
        member = member[:-len(".gyml")] + ".bgyml"
    if (
        not member.startswith("Component/ModelInfo/")
        or member.startswith("/")
        or "\\" in member
        or any(part in ("", ".", "..") for part in member.split("/"))
    ):
        raise LevelError(f"unsafe ModelInfoRef {reference!r}")
    return member


def _model_values(value: BymlValue, label: str) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        raise LevelError(f"{label} is not a dictionary")
    project = value.get("ModelProjectName", "")
    fmdb = value.get("FmdbName", "")
    if not isinstance(project, str) or not isinstance(fmdb, str):
        raise LevelError(f"{label} has invalid model names")
    if not project and not fmdb:
        return None
    if not project or not fmdb or any(character in project + fmdb for character in "/\\"):
        raise LevelError(f"{label} has unsafe or incomplete model names")
    return project, fmdb


def resolve_actor_models(
    gyaml: str,
    members: dict[str, bytes],
    entries: dict[str, object],
) -> ActorModelResolution:
    parameter = _actor_parameter(gyaml, members)
    if parameter is None:
        return ActorModelResolution(gyaml, (), "actor pack has no ActorParam")
    components = parameter.get("Components")
    if not isinstance(components, dict):
        raise LevelError(f"ActorParam for {gyaml} has no Components dictionary")
    reference = components.get("ModelInfoRef")
    if reference is None:
        return ActorModelResolution(gyaml, (), "actor has no model component")
    if not isinstance(reference, str):
        raise LevelError(f"ActorParam for {gyaml} has an invalid ModelInfoRef")

    member_name = _model_info_member(reference)
    model_data = members.get(member_name)
    if model_data is None:
        raise LevelError(f"actor pack for {gyaml} is missing {member_name}")
    try:
        info = _document_dict(read_byml(model_data), f"ModelInfo for {gyaml}")
    except BymlError as error:
        raise LevelError(f"cannot read ModelInfo for {gyaml}: {error}") from error

    definitions: list[tuple[str, str]] = []
    primary = _model_values(info, f"ModelInfo for {gyaml}")
    if primary is not None:
        definitions.append(primary)
    submodels = info.get("SubModels", [])
    if not isinstance(submodels, list):
        raise LevelError(f"ModelInfo for {gyaml} has an invalid SubModels array")
    for index, submodel in enumerate(submodels):
        values = _model_values(submodel, f"ModelInfo for {gyaml} submodel {index}")
        if values is not None:
            definitions.append(values)
    definitions = list(dict.fromkeys(definitions))
    if not definitions:
        return ActorModelResolution(gyaml, (), "actor model reference is empty")

    models: list[ActorModel] = []
    missing: list[str] = []
    for project, fmdb in definitions:
        resource_path = f"Model/{project}.{fmdb}.bfres.zs"
        if resource_path not in entries:
            missing.append(resource_path)
            continue
        models.append(
            ActorModel(
                project_name=project,
                fmdb_name=fmdb,
                resource_path=resource_path,
            )
        )
    if missing:
        return ActorModelResolution(
            gyaml,
            tuple(models),
            "missing model resource(s): " + ", ".join(missing),
        )
    return ActorModelResolution(gyaml, tuple(models))
