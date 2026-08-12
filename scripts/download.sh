#!/bin/bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$SKILL_DIR/run.sh" ingest "$@"
