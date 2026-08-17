#!/bin/sh

set -eu

revision_source=/app/sandbox-runtime/python/.runtime-revision
revision_target=/run/onto-runtime-attestation/revision

test -s "${revision_source}"
mkdir -p "$(dirname "${revision_target}")"
revision_tmp="${revision_target}.tmp.$$"
trap 'rm -f "${revision_tmp}"' EXIT HUP INT TERM
umask 022
cp "${revision_source}" "${revision_tmp}"
chmod 0444 "${revision_tmp}"
mv -f "${revision_tmp}" "${revision_target}"
trap - EXIT HUP INT TERM

exec "$@"
