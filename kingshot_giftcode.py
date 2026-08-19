"""
Kingshot gift-code redemption client (Century Games API).

Redemption requires, per player:
  1. a signed **login** call (``/player``),
  2. fetching an image **captcha** (``/captcha``) and solving it with OCR,
  3. a signed **redeem** call (``/gift_code``) carrying the solved captcha.

Every request is signed with an MD5 of the alphabetically-sorted query string
plus a fixed salt. The captcha is solved with ``ddddocr`` (imported lazily so the
rest of the bot still loads if OCR isn't installed). Because OCR is imperfect and
Century Games rate-limits, ``redeem_player`` retries on captcha misses and the
caller paces requests.

⚠️ These endpoints are undocumented and obfuscated by the game developer. If
redemption starts failing wholesale, re-check the constants at the top of this
file against a live browser session — that's the single place to adjust.
"""

import asyncio
import base64
import hashlib
import logging
import time
from typing import Optional

import aiohttp

# ── Tunable constants (adjust here if the live API changes) ───
BASE_URL = "https://kingshot-giftcode.centurygame.com/api"
SALT = "tB87#kPtkxqOS2"
PLAYER_PATH = "/player"
CAPTCHA_PATH = "/captcha"
REDEEM_PATH = "/gift_code"

MAX_CAPTCHA_ATTEMPTS = 5      # OCR retries per player before giving up
REQUEST_TIMEOUT = 20          # seconds per HTTP call

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://ks-giftcode.centurygame.com",
    "referer": "https://ks-giftcode.centurygame.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KingshotBot/1.0",
}

# Outcome constants returned to the caller.
SUCCESS = "success"
ALREADY = "already_claimed"
INVALID = "invalid_code"
EXPIRED = "expired_code"
CAPTCHA_FAIL = "captcha_failed"
RATE_LIMIT = "rate_limited"
NOT_FOUND = "player_not_found"
ERROR = "error"

# Known Century Games err_code → outcome. Unknown codes fall through to ERROR.
ERR_CODES = {
    20000: SUCCESS,
    40008: ALREADY,     # already received
    40014: INVALID,     # code not found
    40007: EXPIRED,     # code expired
    40005: EXPIRED,     # usage limit reached
    40004: ERROR,       # timeout, retry
    40010: NOT_FOUND,   # player not found
    40100: CAPTCHA_FAIL,
    40101: CAPTCHA_FAIL,
    40103: CAPTCHA_FAIL,
}

# ── Lazy OCR ──────────────────────────────────────────────────
_ocr = None
OCR_IMPORT_ERROR: Optional[str] = None


def _get_ocr():
    """Load ddddocr once, on first use. Returns None if unavailable."""
    global _ocr, OCR_IMPORT_ERROR
    if _ocr is None and OCR_IMPORT_ERROR is None:
        try:
            import ddddocr  # noqa: WPS433 (deferred heavy import)
            _ocr = ddddocr.DdddOcr(show_ad=False)
        except Exception as exc:  # pragma: no cover - depends on host install
            OCR_IMPORT_ERROR = str(exc)
            logging.error("ddddocr unavailable — gift-code redemption disabled: %s", exc)
    return _ocr


def ocr_available() -> bool:
    return _get_ocr() is not None


# ── Signing ───────────────────────────────────────────────────
def sign(params: dict) -> str:
    """MD5 of the alphabetically-sorted ``k=v&k=v`` query string plus the salt."""
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5((query + SALT).encode("utf-8")).hexdigest()


def _signed(params: dict) -> dict:
    body = dict(params)
    body["sign"] = sign(params)
    return body


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── HTTP steps ────────────────────────────────────────────────
async def _post(session: aiohttp.ClientSession, path: str, params: dict) -> dict:
    body = _signed(params)
    async with session.post(
        BASE_URL + path,
        data=body,
        headers=HEADERS,
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as resp:
        if resp.status == 429:
            return {"__http__": 429}
        try:
            return await resp.json(content_type=None)
        except Exception:
            return {"__http__": resp.status}


async def login(session, fid: int) -> dict:
    return await _post(session, PLAYER_PATH, {"fid": fid, "time": _now_ms()})


async def fetch_captcha(session, fid: int) -> Optional[bytes]:
    data = await _post(session, CAPTCHA_PATH, {"fid": fid, "time": _now_ms(), "init": 0})
    img = (data.get("data") or {}).get("img") if isinstance(data.get("data"), dict) else None
    if not img:
        return None
    if "," in img:  # strip a data:image/...;base64, prefix
        img = img.split(",", 1)[1]
    try:
        return base64.b64decode(img)
    except Exception:
        return None


def _solve_captcha(image_bytes: bytes) -> Optional[str]:
    ocr = _get_ocr()
    if ocr is None:
        return None
    try:
        text = ocr.classification(image_bytes)
    except Exception as exc:  # pragma: no cover
        logging.warning("captcha OCR error: %s", exc)
        return None
    cleaned = "".join(ch for ch in (text or "") if ch.isalnum())
    return cleaned if len(cleaned) == 4 else None


async def redeem(session, fid: int, code: str, captcha_code: str) -> dict:
    return await _post(
        session,
        REDEEM_PATH,
        {"fid": fid, "cdk": code, "time": _now_ms(), "captcha_code": captcha_code},
    )


def _classify(data: dict) -> str:
    if data.get("__http__") == 429:
        return RATE_LIMIT
    err = data.get("err_code")
    if err in ERR_CODES:
        return ERR_CODES[err]
    msg = str(data.get("msg", "")).upper()
    if "CAPTCHA" in msg:
        return CAPTCHA_FAIL
    if "RECEIVED" in msg:
        return ALREADY
    if "CDK NOT FOUND" in msg or "NOT FOUND" in msg:
        return INVALID
    if "EXPIRED" in msg or "TIME ERROR" in msg:
        return EXPIRED
    if data.get("code") == 0 or "SUCCESS" in msg:
        return SUCCESS
    return ERROR


# ── Orchestration ─────────────────────────────────────────────
async def redeem_player(session, fid: int, code: str) -> str:
    """Full login→captcha→redeem flow for one player. Returns an outcome constant."""
    if not ocr_available():
        return ERROR

    login_resp = await login(session, fid)
    login_outcome = _classify(login_resp)
    if login_outcome == NOT_FOUND:
        return NOT_FOUND
    if login_resp.get("__http__") == 429:
        return RATE_LIMIT

    for _ in range(MAX_CAPTCHA_ATTEMPTS):
        image = await fetch_captcha(session, fid)
        if image is None:
            await asyncio.sleep(1)
            continue
        captcha_code = await asyncio.to_thread(_solve_captcha, image)
        if not captcha_code:
            continue
        result = await redeem(session, fid, code, captcha_code)
        outcome = _classify(result)
        if outcome == CAPTCHA_FAIL:
            await asyncio.sleep(0.5)
            continue  # bad OCR guess — try a fresh captcha
        return outcome

    return CAPTCHA_FAIL


def new_session() -> aiohttp.ClientSession:
    """A fresh cookie-bearing session for one redemption batch."""
    return aiohttp.ClientSession()
