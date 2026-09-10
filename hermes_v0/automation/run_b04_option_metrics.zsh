#!/bin/zsh
# LaunchAgent entry point for the approved B04 read-only collector.
# The Keychain keeps the OpenAlgo API key out of source, plist, logs, and shell history.

set -eu
set -o pipefail

script_dir="${0:A:h}"
hermes_root="${script_dir:h}"
workspace_root="${hermes_root:h}"
python_bin="${hermes_root}/.venv/bin/python"
keychain_service="com.openalgo.hermes-v0.option-metrics"
keychain_account="$(/usr/bin/id -un)"

if [[ ! -x "${python_bin}" ]]; then
    print -u2 "Hermes B05 did not start: dedicated Python runtime is missing."
    exit 2
fi

# ``security`` returns the secret only to this process. It is never echoed.
if ! api_key="$(/usr/bin/security find-generic-password \
    -a "${keychain_account}" -s "${keychain_service}" -w 2>/dev/null)"; then
    print -u2 "Hermes B05 did not start: OpenAlgo API key is absent from the macOS Keychain."
    exit 2
fi
if [[ -z "${api_key}" ]]; then
    print -u2 "Hermes B05 did not start: Keychain API key is empty."
    exit 2
fi

export OPENALGO_API_KEY="${api_key}"
unset api_key
export HERMES_UNDERLYING="NIFTY"
export HERMES_OPTION_INTERVAL_SECONDS="5"
# LaunchAgents start with a stripped environment. Pin the local Postgres TCP
# address so asyncpg does not rely on inherited user/network defaults.
export HERMES_DB_HOST="${HERMES_DB_HOST:-127.0.0.1}"
export HERMES_DB_NAME="${HERMES_DB_NAME:-hermes}"
export HERMES_DB_USER="${HERMES_DB_USER:-postgres}"
export PYTHONPATH="${workspace_root}"

# The collector itself rejects a duplicate process and stops at 15:30 IST.
exec "${python_bin}" -m hermes_v0.collector.recovery --interval-seconds 5 "$@"
