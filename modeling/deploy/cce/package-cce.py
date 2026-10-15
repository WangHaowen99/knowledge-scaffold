#!/usr/bin/env python3
"""Package three local or digest-pinned Modeling images for offline delivery."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

import yaml


IMAGE_IDS = {"modeling", "modelingAgent", "ontoModeling"}
LOCAL_NAMES = {"modeling": "modeling", "modelingAgent": "modeling-agent", "ontoModeling": "onto-modeling"}
IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--local", action="store_true", help="archive images built locally by build-images.sh")
    source.add_argument("--image", action="append", help="id=registry/repo@sha256:digest; repeat for all three images")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", args.version):
        parser.error("invalid version")
    if args.local:
        images = {
            image_id: f"modeling-offline/{name}:{args.version}-{args.arch}"
            for image_id, name in LOCAL_NAMES.items()
        }
    else:
        images = {}
        for item in args.image:
            image_id, separator, reference = item.partition("=")
            if not separator or image_id in images or image_id not in IMAGE_IDS or not IMAGE_RE.fullmatch(reference):
                parser.error("provide modeling, modelingAgent and ontoModeling once each, pinned by digest")
            images[image_id] = reference
        if images.keys() != IMAGE_IDS:
            parser.error("missing Modeling image")
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
        package_images = {}
        for image_id, reference in sorted(images.items()):
            if not args.local:
                run("docker", "pull", "--platform", f"linux/{args.arch}", reference)
            image_arch = run("docker", "image", "inspect", reference, "--format", "{{.Architecture}}", capture=True)
            if image_arch != args.arch:
                raise SystemExit(f"{image_id}: expected {args.arch}, got {image_arch}")
            config_digest = run("docker", "image", "inspect", reference, "--format", "{{.Id}}", capture=True)
            archive_ref = f"modeling-offline/{LOCAL_NAMES[image_id]}:{args.version}-{args.arch}"
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
        run("helm", "lint", str(chart_source), "-f", str(root / "deploy/cce/values.customer.example.yaml"),
            "-f", str(root / "deploy/cce/resolved-images.example.yaml"))
        run("helm", "package", str(chart_source), "--destination", str(deploy))
        chart_archive = next(deploy.glob("modeling-*.tgz"))
        with tarfile.open(chart_archive, "r:gz") as chart:
            members = chart.getmembers()
            if any(not (member.isfile() or member.isdir()) or Path(member.name).is_absolute()
                   or ".." in Path(member.name).parts for member in members):
                raise SystemExit("Chart archive contains unsafe entries")
            chart.extractall(deploy, filter="data")
        (deploy / "modeling").rename(deploy / "chart")
        chart_archive.unlink()
        for name in ("install-cce.sh", "install-cce.py", "common.py", "joint-cce.py", "deployment.schema.json",
                     "deployment.customer.example.yaml", "storage-inventory.yaml", "endpoints.yaml", "compatibility.yaml",
                     "values.customer.example.yaml", "resolved-images.example.yaml"):
            shutil.copy2(root / "deploy/cce" / name, deploy / name)
        shutil.copy2(root.parent / "deploy/install-consumer-modeling.sh", deploy / "install-consumer-modeling.sh")
        for name in ("install.md", "import-images.md", "upgrade-and-recovery.md", "compatibility.md"):
            shutil.copy2(root / "deploy/cce/docs" / name, docs / name)
        manifest = {
            "schemaVersion": "cce-release/v1", "releaseVersion": args.version,
            "chartVersion": chart_version, "platform": f"linux/{args.arch}",
            "components": ["modeling", "modelingAgent", "ontoModeling"],
            "images": package_images,
            "imageDependencies": {"jiuwenbox": {"sourceSide": "consumer", "sourceImageId": "jiuwenbox"}},
        }
        (folder / "release.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "checksums.sha256")
        (folder / "checksums.sha256").write_text("".join(
            f"{sha256(path)}  {path.relative_to(folder)}\n" for path in files
        ))
        with tarfile.open(output, "w:gz") as archive:
            archive.add(folder, arcname=folder.name)
    print(output)


if __name__ == "__main__":
    main()
