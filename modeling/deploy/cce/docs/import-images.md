# Offline image import

`release.yaml` lists the three Modeling image archives, their SHA-256 values,
image config digests and single target architecture. A locally built image has
`sourceManifestDigest: null`; a registry-sourced image records its verified
source manifest digest. Verify every file
against `checksums.sha256` before import. Use an approved customer image tool
to load each archive and push its `archiveRef` to the customer's SWR namespace.
After upload, query the destination manifest and record the resulting full
`repository@sha256:digest` reference in `resolved-images.yaml`. Do not assume
the source digest is unchanged by a registry conversion.

JiuwenBox is a declared dependency only. Import its archive from the Consumer
package and use that imported reference for both releases on first joint
install. The two releases run separate instances. During a single side upgrade,
retain the old digest until an exact compatible combination is verified and the
Modeling release is explicitly updated. Keep the old image available for
recovery. The package contains no SWR credentials, kubeconfig or application
Secrets.
