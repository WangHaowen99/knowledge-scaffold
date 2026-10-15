# Upgrade and recovery

Keep Core, Agent and Onto Modeling at one replica with `Recreate` updates. Each
uses a separate precreated PVC. Before upgrading, stop relevant writes, verify
the old writer has exited, make an independent, restorable backup or snapshot
of every affected PVC, and record the backup in a customer controlled file.
Data format migration and restore order must be checked for the exact old and
new release combination. Helm rollback changes manifests only; it does not
restore data.

Both Modeling and joint CCE `upgrade` commands currently reject the operation
before changing the cluster. A backup record alone cannot prove stopped
writers, consistent PVC snapshots, data format compatibility or migration
idempotence. A future upgrade implementation must verify all of these and an
exact compatible transition. Never delete a PVC when uninstalling the release.
Keep both image digests and chart versions available until validation finishes.

This implementation has no application specific, idempotent data migration
Jobs or backup verification. It must not be used for a data changing production
upgrade until those steps are implemented and tested. If install of the second
release fails, leave the first release intact, inspect its actual cluster
state, and retry with the same operation ID only after confirming the failed
phase. Do not automatically uninstall the successful side.
