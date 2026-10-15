# Compatibility record

The package includes `compatibility.yaml` with no combinations by default.
Consumer and Modeling teams must jointly record the exact verified release and
chart versions, platform, auth and Consumer API versions, operation schema,
endpoint schema, and JiuwenBox image combination. The installer rejects an
empty or mismatched manifest. This avoids claiming cross repository
compatibility that has not been established in a shared CCE environment.

When an upgrade has been exercised, record its `from` and `to` combination IDs,
ordered side steps and whether coordinated maintenance is required. Keep this
manifest together with acceptance evidence and the source-to-customer image
digest mapping. Do not include environment addresses, credentials or real data
in the release package.
