#!/usr/bin/env bash

set -euo pipefail

./.venv/bin/python ./src/main.py "$@"
