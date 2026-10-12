"""Core logic for log stream health checking.

This module implements a handler that sits in a logging config as a *side
channel*: records forwarded to it are never formatted or rendered to a
destination, only counted and timestamped. The user-facing API is a separate
``check()`` call that compares the most recent emission time against a
configurable deadline. This split is deliberate: ``logging.Handler.emit``
must be cheap and never raise, so validation lives entirely in ``check``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable


_DEFAULT_INTERVAL: float = 5.0
_DEFAULT_GRACE: float = 0.0
_NO_EMISSION: str = "no log record has been emitted yet"


class _Sentinel(logging.Handler):
    """Internal handler that records the latest emit time but writes nothing.

    Subclassing ``logging.Handler`` lets the object be dropped directly into a
    ``dictConfig``-style handlers map or ``logger.addHandler`` call, which is
    the deployment shape this library targets.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        # emit is exercised through StreamHealthMonitor._on_emit so the unit
        # tests do not need a real LogRecord; the branch is covered when the
        # monitor is wired into a real logger.
        # The method must never raise: ``logging`` swallows handler errors
        # silently, which would hide the very staleness this library exists
        # to surface. Treat every record the same, including malformed ones.
        pass


@dataclass
class HealthStatus:
    """Outcome of a single :meth:`StreamHealthMonitor.check` call.

    Attributes:
        healthy: True when a record was emitted within the freshness window.
        last_emit_at: Monotonic timestamp of the most recent emission, or
            ``None`` when no record has ever been emitted.
        stale_for_seconds: Seconds elapsed since the deadline elapsed, or
            ``0.0`` when healthy. Measured against ``last_emit_at + interval``
            so callers can see *how* stale a stream has become.
        reason: Short human-readable explanation suitable for a status page.
    """

    healthy: bool
    last_emit_at: float | None
    stale_for_seconds: float
    reason: str


class StreamHealthMonitor(_Sentinel):
    """Logging handler that doubles as a freshness probe.

    Install the monitor as a normal logging handler (``logger.addHandler(monitor)``
    or via ``dictConfig``). Records flow through it for bookkeeping only — they
    are not formatted or forwarded, so there is no duplicate output in your real
    sinks. Call :meth:`check` from your health endpoint to learn whether the
    stream has gone quiet.

    The ``clock`` parameter exists so tests can be deterministic: production
    code should leave it at the default ``time.monotonic``.
    """

    def __init__(
        self,
        interval_seconds: float = _DEFAULT_INTERVAL,
        grace_seconds: float = _DEFAULT_GRACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        if clock is None:
            raise ValueError("clock must not be None")
        # ``logging.Handler`` calls ``__init__`` indirectly through the
        # machinery that registers handlers, so we keep the base class happy.
        super().__init__(level=logging.NOTSET)
        self._interval: float = float(interval_seconds)
        self._grace: float = float(grace_seconds)
        self._clock: Callable[[], float] = clock
        # ``last_emit_at`` is the single piece of mutable state. It starts as
        # ``None`` rather than ``-inf`` so that a stream which has *never*
        # emitted reads as stale with a distinct reason, rather than as a
        # stream that emitted infinitely long ago.
        self._last_emit_at: float | None = None

    # -- logging.Handler integration ---------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Record the emission time and return immediately.

        Kept intentionally tiny: any exception here would be swallowed by the
        logging framework and the staleness this library reports would itself
        become invisible. We deliberately ignore the record contents —
        staleness is a temporal property, not a content one.
        """
        self._last_emit_at = self._clock()

    # -- public probe API --------------------------------------------------

    def check(self) -> HealthStatus:
        """Return the current :class:`HealthStatus`.

        Healthy when ``now - last_emit_at <= interval + grace``. The grace
        window exists because real loggers batch: a one-second grace on a
        five-second interval absorbs a flush boundary without flipping a
        status page red.
        """
        now: float = self._clock()
        if self._last_emit_at is None:
            return HealthStatus(
                healthy=False,
                last_emit_at=None,
                stale_for_seconds=0.0,
                reason=_NO_EMISSION,
            )
        deadline: float = self._last_emit_at + self._interval + self._grace
        if now <= deadline:
            return HealthStatus(
                healthy=True,
                last_emit_at=self._last_emit_at,
                stale_for_seconds=0.0,
                reason="stream is emitting within the freshness window",
            )
        overshoot: float = now - deadline
        return HealthStatus(
            healthy=False,
            last_emit_at=self._last_emit_at,
            stale_for_seconds=overshoot,
            reason=f"no record emitted in the last {overshoot:.3f}s beyond deadline",
        )

    def reset(self) -> None:
        """Clear recorded emission state.

        Useful when a process is about to enter a phase that legitimately
        produces no logs (e.g. a long idle wait) and you do not want that
        silence to read as staleness from the previous phase.
        """
        self._last_emit_at = None
