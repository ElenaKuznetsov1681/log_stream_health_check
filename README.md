# log_stream_health_check

A logging handler that records when records were last emitted and reports whether the stream has gone stale. Drop it into a logger, then call `check()` from your health endpoint.

```python
import logging
from log_stream_health_check import StreamHealthMonitor, HealthStatus

monitor = StreamHealthMonitor(interval_seconds=5.0, grace_seconds=1.0)
logging.getLogger("app").addHandler(monitor)

status: HealthStatus = monitor.check()
if not status.healthy:
    print(status.reason, status.stale_for_seconds)
```

## Why this exists

Silent loggers are a common root cause of undiagnosed outages: a worker stops emitting, nothing crashes, and the gap is only noticed hours later. This library turns that silence into an explicit signal by measuring the gap since the last `emit` call.

The trade-off is that the monitor only counts — it does not forward records to a sink. If you need both health checking and output, configure a second handler for the actual destination; this one is a side channel. Keeping it non-forwarding means the act of measuring cannot accidentally double your log volume or interfere with formatting.

## Edge cases

A stream that has **never** emitted reads as stale with a distinct reason (`"no log record has been emitted yet"`) and a `stale_for_seconds` of `0.0`, rather than being treated as infinitely old. This matters right after process start: you typically want to know "no logs yet" separately from "logs stopped".

The `grace_seconds` parameter exists because real loggers batch. Set it to absorb flush boundaries so a five-second interval does not flap a status page red on a one-second batch delay. The healthy boundary is inclusive: `now == last_emit + interval + grace` is still healthy.

Tests inject a fake clock rather than relying on wall time, so `check()` behaviour is deterministic regardless of machine speed.
