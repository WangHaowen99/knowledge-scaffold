# solver.job.v1 canonicalization

The contract uses UTF-8 JSON with the following deterministic rules:

1. Object keys are strings, normalized to Unicode NFC, and sorted by their
   normalized UTF-8 names.
2. Strings are normalized to Unicode NFC.
3. Arrays preserve order.
4. Whitespace is omitted (``,``, `:` separators); JSON is encoded as UTF-8
   without a BOM.
5. Floating-point JSON values, `NaN`, and infinities are rejected. Decimal
   quantities and objective values are represented as canonical strings when a
   fractional value is required.
6. Duplicate keys, including keys that collide after NFC normalization, are
   rejected.

The platform's `Idempotency-Key` and `request_ref` are opaque values. Their
derivation is outside the wire contract; a Connector must compare the complete
canonical submit request when checking idempotency and must not interpret either
value as a workspace, user, or problem identifier.
