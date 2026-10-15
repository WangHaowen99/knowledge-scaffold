# CodeArts build and development deployment

Use one fixed Modeling source commit for all three Modeling images and the
Chart. Give the build job a target architecture (`amd64` or `arm64`), version,
and authorized registry prefix. Build the Consumer images in its independent
repository; this job does not build Consumer or auth-server images.

1. Run `bash modeling/deploy/cce/build-images.sh VERSION ARCH REGISTRY_PREFIX`.
   The script prepares the target platform's Python wheel cache and drawio
   asset, reuses them on later builds, then pushes Core, Agent and Onto Modeling images from the current
   source checkout. Record each registry manifest digest and verify its
   platform. The build node needs Docker Buildx, Python/pip and npm, plus
   package index access when the cache is missing. Treat tags as build handles;
   deployment uses full digest refs.
2. Run `helm lint modeling/deploy/cce/chart -f VALUES -f RESOLVED_IMAGES`,
   then `helm package modeling/deploy/cce/chart --destination DIST`. Transfer
   the generated standard `.tgz` as a Build artifact. In CodeArts Helm3, the
   Chart input is the artifact's path in the deployment step's workspace.
   Use nonsecret values plus the registry digest mapping; create Kubernetes
   Secrets through the development environment's controlled process.
3. Install the independent Consumer release first, then the Modeling release
   in the development CCE cluster. Confirm that the Modeling release refers
   to the tested Consumer JiuwenBox digest. Perform application acceptance,
   including a synthetic Modeling script test through its local JiuwenBox
   Socket. Record the image digests, Chart versions and both source commits.
4. After acceptance, use `python3 modeling/deploy/cce/package-cce.py` with the
   three approved digest refs and the same version and architecture to create
   the Modeling offline package. It pulls and archives only those three
   images. The Consumer team packages JiuwenBox once in its own release.

The exact CodeArts Build artifact transfer and Helm3 credential fields depend
on the project tenant. Configure the build executor's registry write access,
the deploy executor's CCE API access, and the CCE nodes' registry read access
separately. Do not put registry credentials or kubeconfig in source, Chart,
package or job logs. The repository scripts provide commands for Build jobs;
they do not assume a particular CodeArts UI plugin configuration.
