# Blocklists and allowlists: plan

Status: written 2026-11-06.

This plan lets an operator subscribe to community blocklists from a catalog, add their own, choose
what each list does, and override any list with an allowlist. It builds on source trust
([sources-plan.md](sources-plan.md)) and works only with `sources.enabled`.

The idea comes from [Super SEO Spam Suppressor](https://github.com/NotaInutilis/Super-SEO-Spam-Suppressor)
(SSSS). kvasir downloads the lists SSSS aggregates, from where they are published, and never SSSS's
own output.

## What was measured

Run 2026-11-06, on a list parser ported to Python, against the Tranco top 1M, the 716 hosts in a
local `sources.db`, and the 96 hosts cited by the 5 runs in `data/eval/sources-before-dataset`.

- SSSS imports 22 live lists, about 96,000 hosts before dedup. With `BadWebsiteBlocklist`, 23 lists
  hold 93,893 unique hosts.
- Three lists SSSS cites answer 404: `no-cmyk/Search-Engine-Spam-Blocklist`,
  `ite-usagi/ublacklist-noai` and `ranmaru22/ublacklist-anti-ai-art-subscription`. The first
  accounts for 320,472 of SSSS's 416,738 domains, which SSSS keeps as a frozen copy.
- The largest live list is 821 KB, far under `MAX_LIST_BYTES`.
- No list names any of the 96 cited hosts, so these lists do not change those 5 runs. The value is
  in results a run would otherwise consider, which phase 6 of the source trust plan has to measure.
- The AI lists name `chatgpt.com`, `openai.com` and `claude.ai`. Blocking them would stop research on
  those products, so the catalog demotes them.

The sample is small, and the eval runs cover 5 questions of 21.

## Lists

A list is one row:

| Field | Meaning |
| --- | --- |
| `id` | The catalog's id, or generated for a custom list. |
| `name` | Shown in the UI. |
| `url` or `entries` | Where its hosts come from: a download, or hosts typed in. |
| `enabled` | Whether it applies. Catalog lists start off, except CISA. |
| `effect` | What a hit does; see below. |
| `tag` | Any label, shown in the site's badge. |
| `ttl_days` | How often it is fetched. Empty means `sources.ttl_days`. |
| `guard` | Whether popular sites and suffix-rule sites are spared. |
| `except` | Hosts, with their subdomains, this list does not apply to. |

| Effect | Result |
| --- | --- |
| `block` | Tier `avoid`: never read. |
| `demote` | Tier `low`: ranked lower and still read. |
| `exempt` | Cancels `block` and `demote` hits on the site. Trust does not change. |
| `trust` | Tier `high`. |
| `kind:<kind>` | Sets the kind, as the CISA list does. |

`exempt` exists because `allow` also raises a site to `high`, so unblocking a site should not mean
trusting it fully. Marks gain the same value.

## How a host resolves

1. `sources.deny`, then `sources.allow`.
2. A mark, including `exempt`.
3. `exempt` and `trust` lists.
4. The bundled list.
5. `block` and `demote` lists, the strongest hit winning, after `except` and the guard.
6. `kind:<kind>` lists, suffix rules, the cache, Wikidata.

A list hit that blocks a site ends resolution, so Wikidata is not asked about it. A `demote` hit or a
tag joins whatever the rest of the order finds.

A list entry matches the host and its parents, down to the registrable domain and no further. Lists
hold entries such as `ru.com` and `com.com`, which are suffixes other sites sit under.

## The guard

With `guard` on, a `block` or `demote` hit does not apply to a site that is either:

- in the Tranco top `sources.guard_rank` (default 10,000, and 0 turns this part off), or
- of a kind a suffix rule gives (`.gov`, `.edu`, `.int`, `europa.eu`).

Tranco includes a CC BY-NC 4.0 source, so it is downloaded at runtime and never shipped, as for
`sources.survey`.

## Storage and memory

| What | Where | Encrypted |
| --- | --- | --- |
| List bodies, as downloaded | `data_dir/sources-<hash>.txt`, as today | No: public lists say nothing about a user |
| List settings, `entries`, `except` | Table `lists` in `sources.db` | Yes: they show what the user reads |
| The index | RAM: one sorted string blob per list, with offsets | Not on disk |

One sorted array per list took 2.2 MiB for the 93,893 hosts, against 63 MiB for today's
`HashMap<String, Source>`, at 55 ns a lookup. The per-list shape gives attribution with no merged
index: a hit on an array names its list. Enabling, disabling or refreshing a list swaps one array, and
a run's overrides choose which arrays to check.

A SQLite index was rejected. It took 2.4 MB of disk and answered a 50-host batch in about 25 µs, which
is fast enough, but a plain file would need a second opener, since `Db::open` encrypts any plain
file, and it gains nothing until lists grow by orders of magnitude.

## Catalog

`crates/kvasir-sites/src/sources/catalog.toml` ships URLs only, so no list data is redistributed. Each
entry gives a list's name, URL, group, licence, effect and tag.

| Group | Lists | Effect |
| --- | --- | --- |
| Official | CISA `.gov` | `kind:government`, on |
| SEO and farms | `content-farm-list`, `SpamdexingSites`, `uBlock-Origin-dev-filter`, `seo-garbage`, `BadWebsiteBlocklist`, uBlacklist Stack Overflow and GitHub translation, `my_filters_001` clickbait, `steamdeck_spamblock` | `block` |
| Scams and fakes | Red Flag Domains, hagezi Fake, AliExpress fake sites (2), `my_filters_001` antitypo, Pi-hole dropshipping | `block` |
| Malware | Anti-Malware Domains, NoCoin, `my_filters_001` antipup | `block` |
| Malware | FMHY | `demote` |
| AI | AI uBlock Origin Blacklist, image-search AI Blocklist, HUGE AI Blocklist, `ublacklist-ai` | `demote` |

Left out, and addable by URL: the three dead lists, `a.dove.isdumb.one` (Adobe telemetry) and the
`jmdugan` corporate lists.

Several lists have no licence (`content-farm-list`, AliExpress, Pi-hole dropshipping), and Red Flag
Domains is CC BY-NC-SA according to SSSS's README. The UI shows each list's licence and last update.

## Security

A subscribed list is untrusted input. Its body is only parsed, under the size cap, into host names.
A list can hide any site that the guard does not spare. A remote `trust` or `exempt` list lets its
maintainer promote or unblock any site, so the catalog ships none and the UI warns when one is added.
List URLs need the API token, as `sources.lists` does today.

## Build order

One commit per step, each with its tests passing.

1. Index and parser. One sorted array per list, multi-list attribution, matching down to the
   registrable domain. Parse `||host^`, and count skipped rules per list.
2. Storage. The `lists` table and `catalog.toml`. Migrate stored `sources.lists` rows, then remove the
   field.
3. Effects. `exempt` marks and lists, `except`, tags, and the resolution order above.
4. Guard. `sources.guard_rank`, using the Tranco loader.
5. API. `GET` and `POST /v1/sources/lists`, `PATCH` and `DELETE /v1/sources/lists/{id}`,
   `POST /v1/sources/lists/{id}/refresh`, `GET /v1/sources/{host}/explain`, and `listed_by` on
   `SourceInfo`.
6. Per-run overrides. A run names lists to add or leave out, passed through `search.rs`, `corpus.rs`
   and `curation.rs`.
7. Visibility. Run steps report blocked hosts and the lists responsible, since `search.rs` drops them
   silently today.
8. Web. A lists page with group toggles, per-list editing, custom lists, status and a host tester, and
   `exempt` in `SourceMenu`.
9. Docs and evaluation. `README.md`, `docs/architecture.md`, the phase 7 report's SSSS row, and
   `just eval` with lists on and off.

Not in this plan: path rules, such as the 205 in `BadWebsiteBlocklist`, which need a check per URL.

## Decisions

Settled 2026-11-06:

- Community lists may block outright, which reverses phase 7's "never read is too strong" for lists
  the operator enables. Every catalog list but CISA starts off.
- The guard is on, at `sources.guard_rank = 10000`.
- Per-run overrides are in the first version.
- The index lives in RAM, not in SQLite.
