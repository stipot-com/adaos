#!/bin/sh
set -eu

json_escape() {
  printf '%s' "${1:-}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

write_field() {
  key="$1"
  value="${2-}"
  if [ -n "$value" ]; then
    printf '  "%s": "%s"' "$key" "$(json_escape "$value")"
  else
    printf '  "%s": null' "$key"
  fi
}

ROOT_BASE="${PUBLIC_ROOT_BASE:-https://api.inimatic.com}"
ADAOS_BASE="${PUBLIC_ADAOS_BASE:-}"
ADAOS_TOKEN="${PUBLIC_ADAOS_TOKEN:-}"

cat > /usr/share/nginx/html/runtime-config.json <<EOF
{
$(write_field "rootBase" "$ROOT_BASE"),
$(write_field "adaosBase" "$ADAOS_BASE"),
$(write_field "adaosToken" "$ADAOS_TOKEN")
}
EOF
