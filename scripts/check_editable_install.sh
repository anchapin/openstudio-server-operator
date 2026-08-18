#!/usr/bin/env bash
# Guard against shared-venv editable-install drift (#71).
#
# Wave orchestration runs multiple git worktrees against ONE shared venv; each
# `pip install -e '.[dev]'` re-points the venv's `openstudio_operator` import at
# whichever worktree installed last. pytest/ruff then run against the wrong
# source tree and produce phantom "pre-existing failures". CI is unaffected
# (fresh runners) — this check is for local/wave workflows only.
#
# Usage: bash scripts/check_editable_install.sh
# Exit 0 = import resolves inside THIS checkout; exit 1 = drifted/broken.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PYTHON="${VIRTUAL_ENV}/bin/python"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

if ! RESOLVED_FILE="$("$PYTHON" -c 'import openstudio_operator; print(openstudio_operator.__file__)' 2>/dev/null)"; then
  echo "ERROR: openstudio_operator is not importable via: $PYTHON" >&2
  echo "  remedy: run: pip install -e '.[dev]' from THIS repo root before running tests" >&2
  exit 1
fi

MODULE_DIR="$(cd "$(dirname "$RESOLVED_FILE")" && pwd -P)"

if [[ "$MODULE_DIR" == "$REPO_ROOT"/* ]]; then
  echo "OK: openstudio_operator resolves inside this checkout:"
  echo "  $MODULE_DIR"
  exit 0
fi

echo "ERROR: editable-install drift detected (#71) — the active venv imports openstudio_operator from ANOTHER checkout" >&2
echo "  import resolves to: $MODULE_DIR" >&2
echo "  this checkout root: $REPO_ROOT" >&2
echo "  remedy: run: pip install -e '.[dev]' from THIS repo root before running tests" >&2
exit 1
