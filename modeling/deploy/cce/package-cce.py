#!/usr/bin/env python3
"""Package five local or digest-pinned Modeling, Hub, and Evaluation images."""

import argparse
import hashlib
import logging
from pathlib import Path
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import yaml

from common import ConfigError, bounded_archive_members, emit_stdout, extract_regular_members, run_program


LOCAL_NAMES = {
    "modeling": "modeling",
    "modelingAgent": "modeling-agent",
    "ontoModeling": "onto-modeling",
    "knowledgeHub": "knowledge-hub",
    "evaluation": "evaluation",
}
IMAGE_IDS = set(LOCAL_NAMES)
IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
LOGGER = logging.getLogger(__name__)
MAX_HELM_BINARY_BYTES = 100 * 1024 * 1024
MAX_HELM_DOWNLOAD_BYTES = 120 * 1024 * 1024


def run(*args, capture=False):
    result = run_program(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_helm(temp_root: Path) -> tuple[str | None, str]:
    """Use local Helm, a temporary download, or fall back to Chart source."""
    existing = shutil.which("helm")
    if existing:
        try:
            run_program((existing, "version", "--short"), check=True, capture_output=True, timeout=10)
            return existing, "helm-tgz"
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("Installed Helm is unusable; trying a temporary download.")
    machine = platform.machine().lower()
    helm_arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not helm_arch:
        LOGGER.warning("Unsupported Helm host architecture %s; copying Chart source.", machine)
        return None, "chart-source"
    version = os.environ.get("CCE_HELM_VERSION", "v3.19.0")
    if not re.fullmatch(r"v3\.[0-9]+\.[0-9]+", version):
        LOGGER.warning("Invalid CCE_HELM_VERSION; copying Chart source.")
        return None, "chart-source"
    archive = temp_root / f"helm-{version}-linux-{helm_arch}.tar.gz"
    url = f"https://get.helm.sh/helm-{version}-linux-{helm_arch}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=30) as response, archive.open("wb") as output:
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_HELM_DOWNLOAD_BYTES:
                    raise RuntimeError("downloaded Helm archive exceeds size limit")
                output.write(chunk)
        with tarfile.open(archive, "r:gz") as source:  # NOSONAR: bounded download and members
            expected = f"linux-{helm_arch}/helm"
            member = next(item for item in bounded_archive_members(source) if item.name == expected)
            if not member.isfile() or member.size > MAX_HELM_BINARY_BYTES:
                raise RuntimeError("downloaded Helm entry is not a regular file")
            helm = temp_root / expected
            helm.parent.mkdir(parents=True, exist_ok=True)
            binary = source.extractfile(member)
            if binary is None:
                raise RuntimeError("downloaded Helm binary is missing")
            with binary, helm.open("wb") as target:
                shutil.copyfileobj(binary, target, length=1024 * 1024)
        helm.chmod(0o755)
        run_program((str(helm), "version", "--short"), check=True, capture_output=True, timeout=10)
        return str(helm), "helm-tgz"
    except Exception as exc:
        LOGGER.warning("Helm unavailable (%s); copying Chart source.", exc)
        return None, "chart-source"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--local", action="store_true", help="archive images built locally by build-images.sh")
    source.add_argument("--image", action="append", help="id=registry/repo@sha256:digest; repeat for all five images")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", args.version):
        parser.error("invalid version")
    if args.local:
        images = {
            image_id: f"modeling-offline/{name}:{args.version}"
            for image_id, name in LOCAL_NAMES.items()
        }
    else:
        images = {}
        for item in args.image:
            image_id, separator, reference = item.partition("=")
            if not separator or image_id in images:
                parser.error("provide modeling, modelingAgent, ontoModeling, knowledgeHub and evaluation once each, pinned by digest")
            if image_id not in IMAGE_IDS or not IMAGE_RE.fullmatch(reference):
                parser.error("provide modeling, modelingAgent, ontoModeling, knowledgeHub and evaluation once each, pinned by digest")
            images[image_id] = reference
        if images.keys() != IMAGE_IDS:
            parser.error("all five owned images are required")
    root = Path(__file__).resolve().parents[2]
    chart_source = root / "deploy/cce/chart"
    chart_version = yaml.safe_load((chart_source / "Chart.yaml").read_text())["version"]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="modeling-cce-") as temp:
        folder = Path(temp) / f"modeling-cce-{args.version}-{args.arch}"
        (folder / "images").mkdir(parents=True)
        deploy = folder / "deploy"
        deploy.mkdir()
        docs = folder / "docs"
        docs.mkdir()
        packaged_chart = Path(temp) / "chart"
        shutil.copytree(chart_source, packaged_chart)
        chart_metadata = yaml.safe_load((packaged_chart / "Chart.yaml").read_text())
        chart_metadata["appVersion"] = args.version
        (packaged_chart / "Chart.yaml").write_text(
            yaml.safe_dump(chart_metadata, allow_unicode=True, sort_keys=False))
        package_images = {}
        for image_id, reference in sorted(images.items()):
            if not args.local:
                run("docker", "pull", "--platform", f"linux/{args.arch}", reference)
            image_arch = run("docker", "image", "inspect", reference, "--format", "{{.Architecture}}", capture=True)
            if image_arch != args.arch:
                raise SystemExit(f"{image_id}: expected {args.arch}, got {image_arch}")
            config_digest = run("docker", "image", "inspect", reference, "--format", "{{.Id}}", capture=True)
            archive_ref = f"modeling-offline/{LOCAL_NAMES[image_id]}:{args.version}"
            archive = folder / "images" / f"{image_id}.tar"
            if reference != archive_ref:
                run("docker", "tag", reference, archive_ref)
            run("docker", "save", "--output", str(archive), archive_ref)
            package_images[image_id] = {
                "archive": str(archive.relative_to(folder)),
                "archiveSha256": sha256(archive),
                "archiveRef": archive_ref,
                "sourceManifestDigest": None if args.local else reference.rsplit("@", 1)[1],
                "configDigest": config_digest,
                "platform": f"linux/{args.arch}",
                "purpose": "business",
            }
        helm, chart_delivery = resolve_helm(Path(temp))
        if helm:
            run(helm, "lint", str(packaged_chart), "-f", str(root / "deploy/cce/values.customer.example.yaml"),
                "-f", str(root / "deploy/cce/resolved-images.example.yaml"))
            run(helm, "package", str(packaged_chart), "--destination", str(deploy))
            chart_archive = next(deploy.glob("modeling-*.tgz"))
            with tarfile.open(chart_archive, "r:gz") as chart:  # NOSONAR: bounded generated Chart
                members = bounded_archive_members(chart)
                paths = [Path(member.name) for member in members]
                if (len(paths) != len(set(paths)) or
                        any(not (member.isfile() or member.isdir()) or path.is_absolute()
                            or ".." in path.parts or not path.parts or path.parts[0] != "modeling"
                            for member, path in zip(members, paths))):
                    raise SystemExit("Chart archive contains unsafe entries")
                extract_regular_members(chart, members, deploy)
            (deploy / "modeling").rename(deploy / "chart")
            chart_archive.unlink()
        else:
            shutil.copytree(packaged_chart, deploy / "chart")
        for name in ("install-cce.sh", "install-cce.py", "common.py", "upgrade.py", "joint-cce.py", "deployment.schema.json",
                     "deployment.customer.example.yaml", "storage-inventory.yaml", "endpoints.yaml", "compatibility.yaml",
                     "values.customer.example.yaml", "resolved-images.example.yaml"):
            shutil.copy2(root / "deploy/cce" / name, deploy / name)
        shutil.copy2(root.parent / "deploy/install-consumer-modeling.sh", deploy / "install-consumer-modeling.sh")
        for name in ("offline-package-build.md", "modeling-deployment.md", "joint-deployment.md"):
            documentation = root / "deploy/cce/docs" / name
            if documentation.is_file():
                shutil.copy2(documentation, docs / name)
            else:
                LOGGER.warning("Optional documentation missing, skipping %s", name)
        manifest = {
            "schemaVersion": "cce-release/v1", "releaseVersion": args.version,
            "chartVersion": chart_version, "platform": f"linux-{args.arch}",
            "components": list(package_images),
            "chartDelivery": chart_delivery,
            "images": package_images,
            "imageDependencies": {"jiuwenbox": {"sourceSide": "consumer", "sourceImageId": "jiuwenbox"}},
        }
        (folder / "release.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "checksums.sha256")
        (folder / "checksums.sha256").write_text("".join(
            f"{sha256(path)}  {path.relative_to(folder)}\n" for path in files
        ))
        with tarfile.open(output, "w:gz") as archive:  # NOSONAR: writes local package files
            archive.add(folder, arcname=folder.name)
    emit_stdout(str(output))


if __name__ == "__main__":
    main()
