#!/bin/sh
set -eu

SSH_KEY_SOURCE_PATH="${SSH_KEY_SOURCE_PATH:-/run/secrets/tunnel_id_rsa}"
SSH_KEY_RUNTIME_PATH="${SSH_KEY_RUNTIME_PATH:-/data/ssh/id_rsa}"

if [ -f "$SSH_KEY_SOURCE_PATH" ]; then
  mkdir -p "$(dirname "$SSH_KEY_RUNTIME_PATH")"
  cp "$SSH_KEY_SOURCE_PATH" "$SSH_KEY_RUNTIME_PATH"
  chmod 700 "$(dirname "$SSH_KEY_RUNTIME_PATH")"
  chmod 600 "$SSH_KEY_RUNTIME_PATH"
fi

export DEFAULT_SSH_KEY_PATH="${DEFAULT_SSH_KEY_PATH:-$SSH_KEY_RUNTIME_PATH}"
export SSH_KEY_PATH="${SSH_KEY_PATH:-$SSH_KEY_RUNTIME_PATH}"

exec "$@"
