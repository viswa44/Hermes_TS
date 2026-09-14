#!/bin/zsh
# Optional manual launcher. The installed LaunchAgent calls Python directly.
# Settings reads secrets from the environment or Keychain; nothing is sourced
# or persisted here. Boto3 uses the user's normal AWS credential chain.
set -euo pipefail
umask 077

cleaning_automation_dir="${0:A:h}"
cleaning_project_dir="${cleaning_automation_dir:h}"
cleaning_workspace_dir="${cleaning_project_dir:h}"
cleaning_python="${cleaning_project_dir}/.venv/bin/python"

if [[ ! -x "${cleaning_python}" ]]; then
  print -u2 -- "Missing data_cleaning_agent/.venv/bin/python; install project dependencies first."
  exit 1
fi

export PYTHONPATH="${cleaning_workspace_dir}"
export PYTHONUNBUFFERED=1
cd "${cleaning_workspace_dir}"
exec "${cleaning_python}" -m market_calendar.run_guarded cleaner "$@"
