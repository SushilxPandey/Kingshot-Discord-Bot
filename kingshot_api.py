"""
Kingshot kingdom lookup (for /age).

The player-lookup API was removed by the game, so the bot no longer does live
player lookups — verification is self-attested. The only remaining external call
is the kingdom-tracker used by /age, which may itself be flaky; callers handle
its ValueError gracefully.
"""

import aiohttp

KINGSHOT_NET_BASE = "https://kingshot.net/api"

NET_HEADERS = {
    "accept": "application/json",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 KingshotBot/1.0"),
}

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def get_kingdom_stats(kingdom_id: int) -> dict:
    """Kingdom-tracker data for /age. Raises ValueError if unknown/unavailable."""
    url = f"{KINGSHOT_NET_BASE}/kingdom-tracker?kingdomId={kingdom_id}&recent=1&limit=20&sort=openTime-desc"
    async with _get_session().get(url, headers=NET_HEADERS) as response:
        if response.status != 200:
            raise ValueError("Kingshot API error")
        data = await response.json(content_type=None)
        servers = (data.get("data") or {}).get("servers") if isinstance(data, dict) else None
        if not servers:
            raise ValueError("Invalid response from Kingshot API")
        return data
