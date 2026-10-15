# Upgrade and recovery

An upgrade must preserve all existing Core, Agent and Onto Modeling data. Their
three precreated PVCs belong to the customer, not to this Helm release. The
Chart mounts them at the same paths and uses one replica with `Recreate` updates;
it does not create, initialize, replace or delete PVCs. Keep the shared
Consumer auth data under Consumer ownership as well.

For the no-data-migration upgrade path, the installer:

1. Read the installed release and record each workload's PVC claim name,
   namespace, bound PV identity and data format version. Reject a missing,
   empty, replaced or differently bound claim. Target values must retain these
   bindings unless an explicit, separately tested data migration is requested.
2. Match the installed and target versions to one verified compatibility
   transition. A joint upgrade always runs Consumer first, waits for its
   verification to pass, then runs Modeling. Verify that the new Consumer can
   coexist with the old Modeling during the interval; otherwise keep cross-side
   requests stopped throughout that interval. Stop relevant writes and wait
   for the old writer to exit before a maintenance task mounts the same volume.
   Keep Core, Agent and Onto Modeling at one replica during this process.
3. Requires a VolumeSnapshot for every Modeling PVC and verifies that each
   snapshot is ready, points to the same claim, has the recorded UID, and was
   created after writes stopped. The backup record stays in the customer's
   controlled operations area, outside both release packages. The storage
   driver must support restorable VolumeSnapshots, verified by a restore test.
4. Accepts only a transition explicitly marked `modelingDataMigration: none`
   and `preserveModelingData: true`. Those flags must come from a tested
   target binary/data-format combination. Data-changing releases require a
   separate migration implementation; never initialize an empty volume as a
   substitute for the prior data.
5. Update and verify the Consumer Helm release before starting the Modeling
   upgrade. Then verify that the new Modeling Pods mount the same claims and PVs
   and that existing projects, sessions and other persisted records remain
   accessible. If Consumer fails, do not start Modeling. If Modeling fails after
   Consumer succeeds, retain the upgraded Consumer and the maintenance state
   until the actual versions and data are checked. Do not automatically roll
   back Consumer. Keep old images and the verified backup until acceptance.

Before invoking upgrade, stop incoming writes, scale the three Modeling
Deployments to zero, and wait until their old Pods are gone. Take one snapshot
per PVC and record its immutable identity. A customer-controlled backup record
has this shape (all values below are synthetic):

```json
{
  "schemaVersion": "cce-backup/v1",
  "context": "customer-demo",
  "namespace": "modeling-demo",
  "releaseName": "modeling",
  "stoppedAt": "2026-10-15T08:00:00Z",
  "dataEvidence": {"core": 3, "agent": 2, "onto": 1},
  "claims": {
    "core": {"claimName": "modeling-core-data", "claimUid": "synthetic-core-uid", "volumeName": "synthetic-core-pv", "snapshotName": "core-snapshot", "snapshotUid": "synthetic-core-snapshot-uid"},
    "agent": {"claimName": "modeling-agent-data", "claimUid": "synthetic-agent-uid", "volumeName": "synthetic-agent-pv", "snapshotName": "agent-snapshot", "snapshotUid": "synthetic-agent-snapshot-uid"},
    "onto": {"claimName": "modeling-onto-data", "claimUid": "synthetic-onto-uid", "volumeName": "synthetic-onto-pv", "snapshotName": "onto-snapshot", "snapshotUid": "synthetic-onto-snapshot-uid"}
  }
}
```

The installer checks the Kubernetes VolumeSnapshot objects and the live PVC/PV
bindings; it does not create snapshots or test a restore. This record alone is
not evidence of backup recoverability. The installer also checks that the old
Deployments have zero replicas and no old Pods before it updates the Chart.
`dataEvidence` contains the pre-upgrade Core project count, Agent run count,
and Onto Modeling workspace count. Gather these from the stopped volumes using
an approved read-only maintenance process. After startup, the installer checks
database readability and verifies these counts have not decreased. This count
check does not replace business-level data validation or a snapshot restore
test.

The Modeling-side `deploy/install-cce.sh upgrade` is called by the joint entry
with the usual context, namespace, release, values, resolved-images,
compatibility and state-dir arguments, plus `--transition-id`, `--backup-record`
and a delegated operation Lease. A standalone invocation without a verified
operation Lease is rejected. The joint
entry accepts `cce upgrade --scope both --config ... --transition-id ...
--backup-record <Modeling record> --consumer-backup-record <Consumer record>`.
It calls the Consumer package's own upgrade entry first, waits for its success,
then calls Modeling. The Consumer package must implement its backup and data
retention checks independently. These commands must be exercised against a
real CCE cluster and both packages before customer production use.

The Modeling Chart is first upgraded with all three data-writing Deployments
at zero replicas. The installer waits for their old Pods to disappear, checks
PVC/PV identity again, then applies the target Chart at one replica and checks
the data evidence. A failed volume check leaves the workloads stopped. The
updated Chart and backup snapshots remain available for controlled recovery.

If a step fails, inspect the operation ID and customer state directory.
`--resume <operation-id>` resumes only when package/configuration checksums
match and the actual Helm versions agree with the journal. A half-applied step
whose actual version cannot be verified remains blocked for manual recovery.
Helm rollback changes manifests only and cannot restore modified data.
Uninstalling a release must not delete its PVCs.

If installation of the second release fails, leave the first release intact,
inspect actual cluster state and retry with the same operation ID only after
confirming the failed phase. Do not automatically uninstall the successful
side or clear either side's data.
