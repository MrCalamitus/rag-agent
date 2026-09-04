from __future__ import annotations

import time


class SystemClock:
    def unix_seconds(self) -> int:
        return int(time.time())

    def monotonic_ms(self) -> float:
        return time.perf_counter() * 1000.0
