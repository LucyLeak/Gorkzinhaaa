from __future__ import annotations

import aiohttp


class GiphyClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def random_gif(self, tag: str) -> str | None:
        if not self.enabled:
            return None

        params = {"api_key": self.api_key, "tag": tag, "rating": "pg-13"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.giphy.com/v1/gifs/random",
                params=params,
                timeout=10,
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        data = payload.get("data") or {}
        images = data.get("images") or {}
        original = images.get("original") or {}
        return original.get("url") or data.get("url")
