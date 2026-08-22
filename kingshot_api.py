"""
Kingshot external lookups.

  * get_player_info — live player lookup by game ID via kingshotstats' public
    search API (real name/kingdom/TC level/alliance/power).
  * get_active_codes — the live active gift-code list (for auto-announce).
  * get_kingdom_stats — kingdom-tracker used by /age.

Callers handle failures gracefully (get_player_info raises only when the site is
unreachable, so verification can fall back to self-attested).
"""

import logging
import os

import aiohttp

KINGSHOT_NET_BASE = os.getenv("KINGSHOT_NET_BASE", "https://kingshot.net/api")

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


# The stats provider base URL. Overridable via env so it need not be hardcoded in a
# public repo (it's a public site, not a secret — this is convenience, not real security).
KSTATS_BASE = os.getenv("KSTATS_BASE", "https://kingshotstats.com/api")
KSTATS_SEARCH = KSTATS_BASE + "/search"


async def _kstats_get(path: str) -> dict:
    """GET a kingshotstats API path and return parsed JSON (raises on error)."""
    async with _get_session().get(
        KSTATS_BASE + path, headers=NET_HEADERS, timeout=aiohttp.ClientTimeout(total=25)
    ) as response:
        if response.status != 200:
            raise ValueError(f"kingshotstats HTTP {response.status}")
        return await response.json(content_type=None)


async def get_alliance(aid) -> dict | None:
    """Alliance detail incl. members (sorted by power). Returns None on failure."""
    try:
        return await _kstats_get(f"/alliances/{aid}")
    except (ValueError, aiohttp.ClientError):
        return None


async def get_player_detail(uid) -> dict | None:
    """Full player profile incl. heroes + gear. Returns None on failure."""
    try:
        return await _kstats_get(f"/players/{uid}")
    except (ValueError, aiohttp.ClientError):
        return None


async def get_kingdom(kid) -> dict | None:
    """
    Kingdom overview: aggregate battle stats (player/alliance counts, total/avg/top
    power, activity, health) plus a ``players`` roster. Returns None on failure.
    """
    try:
        return await _kstats_get(f"/kingdoms/{kid}")
    except (ValueError, aiohttp.ClientError):
        return None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _normalize_player(match: dict, ingame_id) -> dict:
    """Map a kingshotstats search result into the bot's expected player shape."""
    return {
        "name": match.get("nick_name") or match.get("name"),
        "playerId": match.get("fid", ingame_id),
        "uid": match.get("uid"),
        "aid": match.get("aid"),
        "kingdom": _to_int(match.get("kid")),
        "level": _to_int(match.get("town_center_level") or match.get("stove_lv")),
        "levelRendered": match.get("town_center_level") or match.get("stove_lv") or "Unknown",
        "alliance_abbr": match.get("alliance_abbr"),
        "alliance_name": match.get("alliance_name"),
        "power": match.get("power"),
        "kills": match.get("kills"),
        "x": _to_int(match.get("x")),
        "y": _to_int(match.get("y")),
        "online": match.get("online"),
        "last_login": match.get("last_login"),
        "profilePhoto": match.get("avatar_url") or "",
    }


async def _search(ingame_id, live: int) -> dict:
    url = f"{KSTATS_SEARCH}?q={ingame_id}&limit=40&live={live}"
    async with _get_session().get(
        url, headers=NET_HEADERS, timeout=aiohttp.ClientTimeout(total=25)
    ) as response:
        if response.status != 200:
            raise aiohttp.ClientError(f"kingshotstats HTTP {response.status}")
        return await response.json(content_type=None)


async def get_player_info(ingame_id: int) -> dict:
    """
    Live player lookup via kingshotstats' search API (by game ID).

    Tries the local (cached) index first, then a live kingdom scan. Returns
    ``{"data": {...}}`` with the player's real name/kingdom/level/alliance/power,
    or ``{"data": {}}`` if no record was found. Raises ``ValueError`` only if the
    site was completely unreachable, so callers can fall back to self-attested.
    """
    reached = False
    for live in (0, 1):
        try:
            data = await _search(ingame_id, live)
            reached = True
        except aiohttp.ClientError as exc:
            logging.info("kingshotstats search (live=%s) failed: %s", live, exc)
            continue
        results = data.get("results") or []
        match = next((r for r in results if str(r.get("fid")) == str(ingame_id)), None)
        if match:
            return {"data": _normalize_player(match, ingame_id)}
    if not reached:
        raise ValueError("kingshotstats unreachable")
    return {"data": {}}


async def get_active_codes() -> list[dict]:
    """
    Fetch the list of currently-active Kingshot gift codes.

    Returns a list of ``{"code", "expiresAt", "createdAt"}`` dicts (empty on any
    failure). This LIST endpoint is live even though the redemption API is
    bot-blocked, so it powers auto-announcing new codes.
    """
    url = f"{KINGSHOT_NET_BASE}/gift-codes"
    try:
        async with _get_session().get(url, headers=NET_HEADERS,
                                      timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status != 200:
                return []
            data = await response.json(content_type=None)
    except aiohttp.ClientError:
        return []
    if not isinstance(data, dict) or data.get("status") != "success":
        return []
    return (data.get("data") or {}).get("giftCodes", []) or []


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
