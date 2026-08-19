"""
Deeper redemption probe — tests the two things the first probe didn't, plus
checks whether the ACTIVE-CODES LIST endpoint is alive (that one powers
auto-announcing new codes even though redemption is blocked).

Usage:
    python test_redeem2.py <playerId> <kingdom> <giftcode>
    e.g. python test_redeem2.py 73372825 466 VIP777

Nothing is redeemed for real unless the endpoint actually accepts it. Paste the
whole output back.
"""

import asyncio
import hashlib
import sys
import time

import aiohttp

SALT = "tB87#kPtkxqOS2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://kingshot-giftcode.centurygame.com"
ORIGIN = "https://ks-giftcode.centurygame.com"


def sign(params: dict) -> str:
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5((query + SALT).encode()).hexdigest()


def hdr(json_body=False):
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json" if json_body else "application/x-www-form-urlencoded",
        "origin": ORIGIN,
        "referer": ORIGIN + "/",
        "user-agent": UA,
    }


async def post(session, url, params):
    body = dict(params)
    body["sign"] = sign(params)
    try:
        async with session.post(url, data=body, headers=hdr(),
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            print(f"    HTTP {r.status}: {(await r.text())[:400]}")
    except Exception as e:
        print(f"    error: {e!r}")


async def main(fid, kid, code):
    print(f"=== deep probe fid={fid} kid={kid} code={code} ===")
    async with aiohttp.ClientSession() as s:
        # 1. Active-codes LIST (for auto-announce) — try both hosts.
        print("\n[1] active-codes list (kingshot.net/api/gift-codes):")
        try:
            async with s.get("https://kingshot.net/api/gift-codes",
                             headers={"accept": "application/json", "user-agent": UA}) as r:
                print(f"    HTTP {r.status}: {(await r.text())[:400]}")
        except Exception as e:
            print(f"    error: {e!r}")

        now_ms = int(time.time() * 1000)
        now_s = int(time.time())

        # 2. Handshake: gift_code_config (may establish server time / session).
        print("\n[2] gift_code_config (handshake):")
        await post(s, BASE + "/api/gift_code_config", {"fid": fid, "time": now_ms})

        # 3. Redeem with time in SECONDS (vs the ms we tried before).
        print("\n[3] gift_code with time in SECONDS:")
        await post(s, BASE + "/api/gift_code", {"fid": fid, "kid": kid, "cdk": code, "time": now_s})

        # 4. Redeem right after the config call (same session cookies), ms time.
        print("\n[4] gift_code again (ms time, post-handshake, same session):")
        await post(s, BASE + "/api/gift_code", {"fid": fid, "kid": kid, "cdk": code, "time": int(time.time() * 1000)})


if __name__ == "__main__":
    fid = sys.argv[1] if len(sys.argv) > 1 else "73372825"
    kid = sys.argv[2] if len(sys.argv) > 2 else "466"
    code = sys.argv[3] if len(sys.argv) > 3 else "VIP777"
    asyncio.run(main(fid, kid, code))
