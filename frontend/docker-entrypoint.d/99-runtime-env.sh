#!/bin/sh
set -eu

replace_placeholder_in_file() {
  file="$1"
  placeholder="$2"
  value="$3"
  escaped_value="$(printf '%s' "$value" | sed -e 's/[\\&@]/\\&/g')"

  if [ -f "$file" ]; then
    sed -i "s@${placeholder}@${escaped_value}@g" "$file"
  fi
}

# Runtime Nginx upstream replacement.
replace_placeholder_in_file "/etc/nginx/conf.d/default.conf" "__NGINX_BACKEND_UPSTREAM__" "${NGINX_BACKEND_UPSTREAM:-http://backend:5055}"
replace_placeholder_in_file "/etc/nginx/conf.d/default.conf" "__NGINX_TYPEDB_UPSTREAM__" "${NGINX_TYPEDB_UPSTREAM:-http://backend:9991}"
