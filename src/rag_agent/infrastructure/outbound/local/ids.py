from __future__ import annotations

import uuid


class UuidGenerator:
    def response_id(self) -> str:
        return "resp_" + uuid.uuid4().hex[:24]

    def item_id(self, prefix: str) -> str:
        return f"{prefix}_" + uuid.uuid4().hex[:12]
