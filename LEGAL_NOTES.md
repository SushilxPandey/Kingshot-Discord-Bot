# Kingshot Bot — Legal & Compliance Notes

*Last reviewed: 2026-08. This is a plain-language summary to help you make informed
decisions — it is **not legal advice**, and I'm not a lawyer. If anything here matters
a lot to you, run it past someone qualified. Most of the risks below are **contractual**
(terms-of-service) rather than criminal.*

## Short version

Running a community Discord bot that reads **publicly available** game stats and helps
humans redeem gift codes themselves is, in practice, low-risk and common. The bot does
**not** automate gameplay, does **not** auto-redeem codes, and does **not** bypass any
bot-protection. The main things to be aware of are the third-party site's terms, the
game's terms, Discord's policies, and treating members' data responsibly.

## The software libraries

Everything the bot is built on is open-source under permissive licenses that allow
commercial and private use:

- **discord.py** — MIT License.
- **aiohttp** — Apache 2.0.
- **asyncpg** — Apache 2.0.
- **python-dotenv** — BSD.
- **PostgreSQL / Neon** — used within their standard terms; the free tier is fine for a
  bot of this size.

No license problems here.

## The data sources

**The third-party stats site.** Player/kingdom stats come from a public community stats
website through its undocumented JSON endpoints. The data itself is publicly viewable by
anyone on that site. The considerations are: (1) the site may have Terms of Service that
restrict automated access or re-publishing of its data; (2) undocumented endpoints can
change or disappear without notice, and the site could rate-limit or block the bot; and
(3) it's good manners (and good engineering) to be gentle — the bot already paces its
requests and caches where it can. Recommended: skim that site's ToS, and ideally reach
out to ask whether bot use is OK or whether they offer an official API. If they object,
switch sources or stop.

**The official game endpoints.** The bot reads a public "active gift codes" list and a
kingdom-open-date tracker (read-only). It announces codes so **people redeem them
themselves**. Century Games put their actual redemption API behind bot-protection
(Akamai); the bot **deliberately does not attempt to bypass that** — bypassing technical
protection measures is exactly the kind of thing that turns a ToS issue into a bigger
one, so we don't.

**The game's Terms of Service.** Kingshot's own terms govern accounts and gameplay.
Automating gameplay or mass-redeeming codes with bots can violate them. This bot avoids
that entirely: it only reads public stats and posts codes for humans. Keeping it that
way is the safe posture.

## Discord

The bot must follow Discord's Terms of Service and Developer Policy: don't spam, respect
rate limits, only request the permissions/intents it needs, and handle user data
responsibly. Kicking, banning, timing out, and censoring are legitimate moderation
features an admin triggers — those are fine. Keep the stored data minimal.

## Members' data (privacy)

The bot stores only what it needs: the Discord ID, in-game ID/name, kingdom, and
alliance, plus moderation counters. It's partitioned per server, and a member's record is
**deleted** when they're unverified or banned. Good practices to adopt: tell members (e.g.
in the bot-info channel) what's stored and that it's deleted on unverify; don't share the
roster outside the server; and honor deletion requests (already supported). If you operate
where GDPR/UK-GDPR or similar applies, the "delete on unverify" flow covers the core
right-to-erasure expectation for this kind of data.

## Trademarks & affiliation

"Kingshot" and related marks belong to Century Games. This bot is an **unofficial,
fan-made community tool and is not affiliated with, endorsed by, or sponsored by Century
Games**. It's worth putting that one-line disclaimer somewhere visible (the bot-info
channel is a good spot).

## Practical recommendations

1. Keep the source repository **private** (the code references which sites it reads;
   that's not a secret, but there's no reason to advertise it).
2. Keep secrets in `.env` only (already gitignored) and **rotate** any credential that was
   ever pasted somewhere shared — e.g. the database URL and the bot token.
3. Be polite to the third-party site (pace requests, cache) and be ready for it to change.
4. Add the "unofficial / not affiliated with Century Games" disclaimer.
5. Don't add auto-redemption or anything that bypasses bot-protection.
