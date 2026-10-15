"""Shared CCE release validation. No cluster state is changed here."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tarfile

import yaml


IMAGE_REF = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
RELEASE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class ConfigError(ValueError):
    pass


class CompatibilityError(ConfigError):
    pass


class LockError(ConfigError):
    pass


class MigrationUnavailable(ConfigError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ConfigError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read YAML: {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"expected YAML mapping: {path.name}")
    return data


def checksum(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def package_file(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ConfigError("package contains an unsafe path")
    return path


def verify_checksums(root: Path) -> None:
    checksums = root / "checksums.sha256"
    if not checksums.is_file():
        raise ConfigError("missing checksums.sha256")
    listed = set()
    for line in checksums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ConfigError("invalid checksum entry")
        expected, name = match.groups()
        if name in listed:
            raise ConfigError("duplicate checksum entry")
        listed.add(name)
        path = package_file(root, name)
        if not path.is_file() or checksum(path) != expected:
            raise ConfigError(f"checksum failed: {name}")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and path != checksums}
    if actual != listed:
        raise ConfigError("package file list differs from checksums")


def safe_extract(archive: Path, destination: Path, side: str) -> Path:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ConfigError("archive contains duplicate entries")
        roots = {Path(member.name).parts[0] for member in members if member.name}
        if len(roots) != 1 or not next(iter(roots)).startswith(f"{side}-cce-"):
            raise ConfigError(f"unexpected {side} package root")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ConfigError("archive contains unsafe path or special file")
        source.extractall(destination, filter="data")
    root = destination / next(iter(roots))
    verify_checksums(root)
    return root


def validate_release(root: Path, side: str) -> dict:
    verify_checksums(root)
    release = read_yaml(root / "release.yaml")
    if release.get("schemaVersion") != "cce-release/v1" or release.get("platform") not in ("linux/amd64", "linux/arm64"):
        raise ConfigError("unsupported release schema or platform")
    if not release.get("releaseVersion") or not release.get("chartVersion"):
        raise ConfigError("release version or chart version missing")
    images = release.get("images")
    if not isinstance(images, dict):
        raise ConfigError("release images missing")
    expected = {"modeling", "modelingAgent", "ontoModeling"} if side == "modeling" else None
    if expected is not None and images.keys() != expected:
        raise ConfigError("modeling release must contain exactly three owned images")
    for image_id, record in images.items():
        if not isinstance(record, dict):
            raise ConfigError(f"image record invalid: {image_id}")
        if "sourceManifestDigest" not in record:
            raise ConfigError(f"image source identity missing: {image_id}")
        archive = package_file(root, record.get("archive", ""))
        if not archive.is_file() or checksum(archive) != record.get("archiveSha256"):
            raise ConfigError(f"image archive invalid: {image_id}")
        source_digest = record.get("sourceManifestDigest")
        if record.get("platform") != release["platform"] or (
            source_digest is not None and (
                not isinstance(source_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_digest)
            )
        ):
            raise ConfigError(f"image identity invalid: {image_id}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("configDigest", "")):
            raise ConfigError(f"image config digest invalid: {image_id}")
    if side == "modeling":
        dependency = release.get("imageDependencies", {}).get("jiuwenbox", {})
        if dependency != {"sourceSide": "consumer", "sourceImageId": "jiuwenbox"}:
            raise ConfigError("JiuwenBox must be declared as Consumer image dependency")
    return release


def validate_images(values: dict, image_map: dict, release: dict) -> dict:
    if "resolvedImages" in values.get("global", {}):
        raise ConfigError("resolvedImages may only appear in the image mapping file")
    if set(image_map) != {"global"} or set(image_map.get("global", {})) != {"resolvedImages"}:
        raise ConfigError("image mapping must only contain global.resolvedImages")
    images = image_map["global"]["resolvedImages"]
    required = set(release["images"]) | set(release.get("imageDependencies", {}))
    if set(images) != required or any(not isinstance(ref, str) or not IMAGE_REF.fullmatch(ref) for ref in images.values()):
        raise ConfigError("all owned and external images require immutable digest references")
    if values.get("global", {}).get("targetArch") != release["platform"].split("/")[1]:
        raise ConfigError("values architecture does not match release")
    return images


def validate_compatibility(path: Path, release: dict) -> None:
    manifest = read_yaml(path)
    if manifest.get("schemaVersion") != "cce-compatibility/v1" or not isinstance(manifest.get("combinations"), list):
        raise ConfigError("unsupported compatibility manifest")
    if not any(all(item.get(key) == value for key, value in {
        "modelingReleaseVersion": release["releaseVersion"],
        "modelingChartVersion": release["chartVersion"],
        "platform": release["platform"],
        "operationSchemaVersion": "cce-operation/v1",
        "endpointSchemaVersion": "cce-endpoints/v1",
    }.items()) for item in manifest["combinations"] if isinstance(item, dict)):
        raise CompatibilityError("no verified compatibility combination for this Modeling release")


def endpoint_results(root: Path, release: str, namespace: str) -> list[dict]:
    manifest = read_yaml(root / "deploy/endpoints.yaml")
    if manifest.get("schemaVersion") != "cce-endpoints/v1":
        raise ConfigError("unsupported endpoint manifest")
    return [{"id": item["id"], "serviceName": f"{release}-{item['serviceNameSuffix']}",
             "namespace": namespace, "port": item["port"], "protocol": item["protocol"],
             "apiVersion": item["apiVersion"]} for item in manifest["endpoints"]]


def validate_name(value: str, field: str) -> None:
    if not RELEASE_NAME.fullmatch(value) or len(value) > 53:
        raise ConfigError(f"invalid {field}")
