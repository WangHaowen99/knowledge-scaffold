# Compatibility record

The package includes `compatibility.yaml` with no combinations by default.
Consumer and Modeling teams must jointly record the exact verified release and
chart versions, platform, auth and Consumer API versions, operation schema,
endpoint schema, and JiuwenBox image combination. The installer rejects an
empty or mismatched manifest. This avoids claiming cross repository
compatibility that has not been established in a shared CCE environment.

When a joint upgrade has been exercised, record its `from`, `to`, and
`intermediateCombinationId` combination IDs with `steps: [consumer, modeling]`.
The intermediate combination is the new Consumer with the old Modeling. The
installer requires it to be listed even when cross-side traffic remains stopped
during maintenance. Also record `modelingDataMigration: none` and
`preserveModelingData: true` only after target binaries have been tested with
existing data. Any data-changing transition remains unsupported. Single-side
transitions use `steps: [modeling]` and cannot change the Consumer version.
Keep this manifest with acceptance evidence and the source-to-customer image
digest mapping. Do not include environment addresses, credentials or real data
in the release package.
