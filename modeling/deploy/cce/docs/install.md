# Modeling CCE installation

The Modeling package contains three owned images and a Helm release for Core,
Agent, and Onto Modeling. The Core Pod also runs a JiuwenBox sidecar. The
JiuwenBox image is supplied by the Consumer package; shared auth-server and
Consumer API are referenced through Services. This package never installs a
second auth-server.

Requirements: Python 3.11+ with PyYAML and jsonschema, Helm 3, kubectl, a
Kubernetes context with deployment permissions, precreated namespace, three
dedicated RWO Filesystem PVCs, image pull Secrets, service token Secret, TLS
Secret, and a CCE compatible ELB Ingress controller. The supplied example files
contain synthetic values and must be filled in outside the repository.

1. Import the three owned images using `import-images.md`. Import the Consumer
   package's JiuwenBox image once. Record the customer registry's manifest
   digest for each owned image in `resolved-images.yaml`. The example's
   JiuwenBox digest is a nonfunctional placeholder. During a joint operation,
   remove that entry or replace it with the exact Consumer digest; the joint
   entry creates a temporary effective image mapping. For a standalone
   Modeling operation, supply the verified Consumer digest explicitly.
2. Fill `values.customer.yaml` with the precreated PVC names, shared auth and
   Consumer Services, issuer and audience, and this release's Ingress route.
3. Provide the jointly verified compatibility manifest. The bundled
   `compatibility.yaml` is intentionally empty until both teams verify an exact
   combination; an unverified combination is rejected.
4. Run `bash deploy/install-cce.sh plan --context CONTEXT --namespace NAMESPACE
   --release modeling --values values.customer.yaml --resolved-images
   resolved-images.yaml --compatibility compatibility.yaml --state-dir STATE_DIR`.
   Review the rendered YAML in `STATE_DIR`.
5. Run the same command with `preflight`, then `install`, then `verify`. The
   `--operation-id` flag identifies a single installation attempt. Set
   `--timeout-seconds` if 900 seconds is insufficient.

For a joint install, place both packages beside a customer controlled
`deployment.customer.yaml` and use:

```text
bash deploy/install-consumer-modeling.sh cce install --config deployment.customer.yaml
```

The joint entry uses the Consumer package's `deploy/install-cce.sh` contract.
It does not read Consumer source code. Each side owns its Helm release and
runtime resources. The entry validates package checksums and the exact
compatibility combination before invoking either side. The operation lease
requires permission to create, update and delete Leases in the Consumer
namespace. Customer values, keys and operational state must be kept out of the
release package and Git.

The current preflight checks declarative resources and basic cluster
availability. It cannot prove that all candidate nodes can pull every image,
that the selected ELB and Ingress annotations work on the target CCE flavor, or
that privileged JiuwenBox is allowed. These require a real cluster acceptance
run before production delivery. A successful Pod rollout also does not prove
script test execution; perform a real synthetic script execution in each
target architecture as part of acceptance.

For a data-preserving upgrade, see `upgrade-and-recovery.md`. The upgrade path
requires a tested no-migration compatibility transition, stopped old Modeling
Pods, ready PVC snapshots and a verified backup record. Joint execution is
always Consumer upgrade and verify, then Modeling upgrade and verify. Consumer
package upgrade behavior belongs to its independent repository.

For source checkout validation, the Modeling-side command expects the package
layout. Point `--package-root` at an extracted Modeling CCE package when
running the Python entry directly; in the delivered package this is resolved
automatically by the wrapper.

For customer-side offline delivery without a development registry, run this
single command from the repository root on a build host with Docker Buildx:

```bash
bash modeling/deploy/cce/build-offline.sh v1.0.0 amd64
```

The default output is `modeling/dist/modeling-cce-v1.0.0-amd64.tar.gz`.
Pass a third argument to choose another output path. `build-images.sh` prepares
and reuses the vendor inputs before loading three images into local Docker;
`build-offline.sh` then packages them.
Drawio preparation follows the VM release behavior: an existing
`modeling/onto-modeling/vendor/drawio/webapp/index.html` is reused; otherwise
the script tries host `npm pack`. If `npm` is missing or preparation fails, it
warns and continues building without the Drawio editor. To include the editor
on a disconnected build host, transfer the complete `vendor/drawio/webapp/`
directory from an online build host before building. Docker base images and
Python dependencies must also be available locally or through an approved
mirror. Other build failures stop the script; rerunning uses the same versioned
image tags.
Use `arm64` consistently for an arm64 target. A cross-architecture builder
needs working emulation or a native node. The local images are tagged
`modeling-offline/modeling`, `modeling-offline/modeling-agent` and
`modeling-offline/onto-modeling` with the version and architecture suffix.
`package-cce.py --local` verifies their architecture and archives them without
pulling from or pushing to a registry. The release manifest records the image
config digests and archive SHA-256 values. Its `sourceManifestDigest` is null
until an image has been pushed to a registry. Importing into the customer SWR
produces the digest used for deployment. JiuwenBox is not built or archived by
this job. Existing virtual machine build scripts and tar packages remain
separate.

For a development registry workflow, pass `REGISTRY_PREFIX` as the third
argument to `build-images.sh`; it pushes the three images. Package the approved
digest refs with repeated `--image id=repository@sha256:digest` arguments.
