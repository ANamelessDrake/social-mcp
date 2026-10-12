#!/bin/bash

set -e

error_exit() {
  echo "ERROR: $1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || error_exit "$1 is not installed or not in PATH."
}

require_command cdk
require_command aws
require_command python3
require_command pip
require_command docker

# Set up virtual environment if not already active
if [[ -z "$VIRTUAL_ENV" ]]; then
  if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv || error_exit "Failed to create virtual environment."
  fi
  echo "Activating virtual environment..."
  # shellcheck disable=SC1091
  source .venv/bin/activate || error_exit "Failed to activate virtual environment."
fi

if ! pip show aws-cdk-lib >/dev/null 2>&1; then
  echo "Installing Python CDK dependencies..."
  pip install -r requirements.txt || error_exit "Failed to install from requirements.txt"
fi

env="$1"
if [ -z "$env" ]; then
  read -r -p "Enter environment (e.g. dev, prod): " env
fi
[ -f "config/${env}.json" ] || error_exit "config/${env}.json not found (copy config/example.json and fill it in)."

auto_approve=""
deploy_only=false
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-approve) auto_approve="--require-approval never" ;;
    --deploy-only) deploy_only=true ;;
    *) error_exit "Unknown option: $1" ;;
  esac
  shift
done

if [ "$deploy_only" = false ]; then
  echo "Synthesizing..."
  cdk synth --context env="$env" >/dev/null || error_exit "cdk synth failed."
fi

echo "Deploying all stacks to '${env}'..."
# shellcheck disable=SC2086
cdk deploy --all --context env="$env" $auto_approve
