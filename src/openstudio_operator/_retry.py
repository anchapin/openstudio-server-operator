"""Shared retry-loop test seam (issue #416).

Both :meth:`openstudio_operator.openstudio_client.OpenStudioClient._request`
and :meth:`openstudio_operator.status_store.StatusStore._mutate` back off
between retries by sleeping. The sleep previously lived as two byte-identical
module-local ``_sleep`` helpers; it is collapsed here so a future change
(e.g. graceful-shutdown handling, structured-log recording) lands in one
place. Callers import it as ``from ._retry import _sleep`` — the imported
name remains a module attribute on each caller, so the existing test seams
that ``monkeypatch.setattr`` ``openstudio_client._sleep`` /
``status_store._sleep`` keep working unchanged.
"""

from __future__ import annotations

import time


def _sleep(seconds: float) -> None:
    """Separate seam so tests can record backoff instead of sleeping."""
    time.sleep(seconds)
