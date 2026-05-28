#!/bin/sh
set -eu

SSH_KEY_DIR="${SSH_KEY_DIR:-/host_ssh}"
SSH_KEY_NAME="${SSH_KEY_NAME:-}"
SSH_KEY_SOURCE_PATH="${SSH_KEY_SOURCE_PATH:-}"
SSH_KEY_RUNTIME_PATH="${SSH_KEY_RUNTIME_PATH:-/data/ssh/tunnel_key}"

if [ -z "$SSH_KEY_SOURCE_PATH" ]; then
  if [ -n "$SSH_KEY_NAME" ]; then
    SSH_KEY_SOURCE_PATH="$SSH_KEY_DIR/$SSH_KEY_NAME"
  else
    for candidate in id_ed25519 id_rsa id_ecdsa id_dsa; do
      if [ -f "$SSH_KEY_DIR/$candidate" ]; then
        SSH_KEY_SOURCE_PATH="$SSH_KEY_DIR/$candidate"
        break
      fi
    done
  fi
fi

if [ -n "$SSH_KEY_SOURCE_PATH" ] && [ -f "$SSH_KEY_SOURCE_PATH" ]; then
  mkdir -p "$(dirname "$SSH_KEY_RUNTIME_PATH")"
  cp "$SSH_KEY_SOURCE_PATH" "$SSH_KEY_RUNTIME_PATH"
  chmod 700 "$(dirname "$SSH_KEY_RUNTIME_PATH")"
  chmod 600 "$SSH_KEY_RUNTIME_PATH"
else
  echo "warning: no SSH private key found under $SSH_KEY_DIR" >&2
  echo "warning: looked for SSH_KEY_NAME or id_ed25519, id_rsa, id_ecdsa, id_dsa" >&2
  if [ -d "$SSH_KEY_DIR" ]; then
    echo "warning: $SSH_KEY_DIR contains:" >&2
    ls -la "$SSH_KEY_DIR" >&2
  else
    echo "warning: $SSH_KEY_DIR is not a directory" >&2
  fi
fi

export DEFAULT_SSH_KEY_PATH="${DEFAULT_SSH_KEY_PATH:-$SSH_KEY_RUNTIME_PATH}"
export SSH_KEY_PATH="${SSH_KEY_PATH:-$SSH_KEY_RUNTIME_PATH}"

exec "$@"
