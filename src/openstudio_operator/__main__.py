"""Programmatic operator entrypoint — ``python -m openstudio_operator`` (issue #681).

Replaces the ``kopf run --module openstudio_operator.handlers --namespace
openstudio-server`` CLI invocation in ``deploy/operator-deployment.yaml`` and
the Dockerfile ``CMD``. The kopf CLI cannot carry
:class:`kopf.OperatorSettings` (its ``run`` command exposes no persistence
options and no env-var hook), and the settings are the ONLY kopf-blessed way
to move the diffbase storage into ``/status`` and disable finalizer stamping
— the two pieces of kopf bookkeeping that otherwise PATCH the read-only OSCM
main resource under the #228 RBAC and produce #681's recurring
``APIForbiddenError`` retry storm.

kopf's documented embedding pattern is exactly this: build the settings,
then call :func:`kopf.run` programmatically. The ordering below deliberately
mirrors the CLI's own sequence (``kopf/cli.py``):

1. ``kopf.configure(...)`` with the no-flags CLI defaults (INFO level, full
   format) — the CLI applies logging options BEFORE preloading the module,
2. import :mod:`openstudio_operator.handlers` (the CLI's
   ``--module``/``loaders.preload`` equivalent) — the import registers every
   handler, starts the /metrics server, installs JSON logging, and arms the
   singleton guard,
3. build the #681 persistence settings and disarm the OSCM timers' finalizer
   requirement (see :mod:`openstudio_operator.kopf_persistence`),
4. ``kopf.run(namespaces=[...], settings=...)`` — standalone peering
   auto-detection, matching the CLI defaults (``standalone=None``).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

import kopf

#: Issue #717 — graceful-shutdown flag shared with the tick runner.
#: Set by the SIGTERM handler below; checked at the start of every
#: ``run_oscm_tick`` invocation to skip new ticks while in-flight work
#: completes.
from openstudio_operator._oscm_handlers import _shutdown_requested  # noqa: F401


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the thin CLI (single ``--namespace`` flag, CLI-flag-compatible)."""
    parser = argparse.ArgumentParser(
        prog="python -m openstudio_operator",
        description="Run the OpenStudio Server operator (kopf-based).",
    )
    parser.add_argument(
        "--namespace",
        default="openstudio-server",
        help=(
            "Namespace to watch (default: openstudio-server — the only "
            "namespace the CRD/RBAC install into; any other value yields a "
            "no-op operator)"
        ),
    )
    return parser.parse_args(argv)


def _handle_sigterm(signum: int, frame: object) -> None:
    """Issue #717 — SIGTERM graceful-shutdown handler.

    Sets the shared shutdown flag so in-flight status writes can complete
    before the process exits. New ticks are prevented from starting by the
    ``_shutdown_requested`` check at the top of ``run_oscm_tick``. The kopf
    framework handles its own graceful shutdown of child watchers; we just
    ensure mid-tick writes are not interrupted.
    """
    # Access the module-level flag via the import to avoid shadowing the
    # binding name.  ``frame`` is unused but required by the signal signature.
    from openstudio_operator import _oscm_handlers

    _oscm_handlers._shutdown_requested = True
    logging.getLogger(__name__).info("SIGTERM received, initiating graceful shutdown")
    sys.exit(0)


def main() -> None:
    """Run the operator with #681's read-only-main-resource persistence."""
    args = _parse_args()
    # Step 1 — logging, with the exact no-flags ``kopf run`` CLI defaults.
    kopf.configure(log_format=kopf.LogFormat.FULL)

    # Issue #717 — register SIGTERM handler before starting kopf so the flag
    # is set before any timer tick can read it.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Step 2 — import the handlers package (registrations + boot wiring).
    # Function-local on purpose: importing this module (tests, tooling) must
    # not trigger the operator's import-time side effects.
    # Step 3 — #681 persistence settings + OSCM finalizer disarm.
    from openstudio_operator import (
        handlers,  # noqa: F401  — imported for registration
        kopf_persistence,
    )

    settings = kopf_persistence.operator_persistence_settings()
    kopf_persistence.disarm_oscm_finalizer_requirements()

    # Step 4 — run, CLI-default flags (standalone=None → auto peering detect).
    kopf.run(namespaces=[args.namespace], settings=settings)


if __name__ == "__main__":  # pragma: no cover (thin process boundary)
    main()
