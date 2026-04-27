"""HTTP backend: ships runs to an Examen API server (e.g. `examen-backend`).

Authenticates via `Authorization: Bearer <api_key>`. The server resolves the
calling user from the auth header; the SDK does not send `user_id` in the
payload.
"""

from typing import Any, cast

import httpx


class Connector:
    """HTTP backend client.

    Args:
        base_url: Root URL of the Examen API server (e.g. ``http://localhost:8000``).
        api_key: Bearer token sent in the ``Authorization`` header.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def ingest_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/ingest/runs",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            r.raise_for_status()
            return cast(dict[str, Any], r.json())

    async def close(self) -> None:
        # Each ingest opens its own httpx client, so there's nothing persistent
        # to release here. Kept to satisfy the Backend protocol.
        return None
