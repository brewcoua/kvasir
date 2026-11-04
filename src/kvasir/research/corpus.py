"""What a run has read: every piece of every page, with its vector, and which pieces intake has
read, so that a later question can be answered from pages already downloaded. Also the links of
those pages, kept as leads for a search to follow when one looks worth more than a result."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import numpy as np

from kvasir.research.claims import Claim
from kvasir.research.corroborate import domain
from kvasir.research.fetch import Page, normalize
from kvasir.research.pages import PageStore
from kvasir.research.search import NOVELTY_WEIGHT
from kvasir.storm.encoder import Encoder, cosine_similarity

if TYPE_CHECKING:
    from kvasir.research.sources import Sources

logger = logging.getLogger(__name__)

# Untuned first guesses, like the weights of search.py. The cosine a piece must reach with the
# question to be recalled, and how many pieces one recall returns, at most this many from one
# site, so that a single site cannot fill it.
RECALL_MIN = 0.6
RECALL_PIECES = 3
RECALL_PER_SITE = 2
# Also untuned. A page leaves at most LEADS_PER_PAGE links behind, none of a page more than
# LEAD_DEPTH links from a search result. A lead is a candidate only above LEAD_MIN cosine to the
# question, so that a thin search does not get a poor lead read in its place. A lead's worth is its
# relevance, plus NOVELTY_WEIGHT (search.py's) times its novelty, plus AFFINITY_WEIGHT times its
# site's affinity, plus RELATED_BONUS for a link in a related block. A site whose affinity reaches
# SITE_AFFINITY earns a search of its own, SITE_SEARCHES of them at most in a run, if its tier is
# high or normal. TRUST_BONUS[tier] is added to a lead's worth, and each corroboration counts in
# affinity by CORROBORATION_WEIGHT of the corroborating claim's tier (sources enabled only). No run
# reads more than PAGES_PER_SITE pages of one site.
LEADS_PER_PAGE = 20
LEAD_DEPTH = 2
LEAD_MIN = 0.5
AFFINITY_WEIGHT = 0.5
RELATED_BONUS = 0.1
SITE_AFFINITY = 1.0
PAGES_PER_SITE = 6
SITE_SEARCHES = 2
TRUST_BONUS = {"high": 0.1, "normal": 0.05, "unknown": 0.0, "low": -0.1}
CORROBORATION_WEIGHT = {"high": 1.0, "normal": 1.0, "unknown": 0.5, "low": 0.25, "avoid": 0.0}

_NON_WORD = re.compile(r"[\W_]+")


@dataclass
class Piece:
    """A piece of a page, with its vector, and whether intake has read it."""

    page: Page
    text: str
    vector: np.ndarray
    read: bool = False


@dataclass
class Lead:
    """A link of a page read, to a page that is not: what it says about itself and its vector."""

    url: str
    anchor: str
    context: str
    region: str
    # Links from a search result: 1 for a link of a result, 2 for one of that page, and so on.
    depth: int
    vector: np.ndarray


class Corpus:
    """The pieces of the pages a run read, and how well each site has done. With a `store`, the
    pieces are kept there, vectors of `model`, and recall reaches pieces other runs kept, unless
    `recall_past_runs` is off. With `sources`, sites of tier `avoid` are never recalled or led to,
    and the tier weighs in a lead's worth, a site's affinity and its site search."""

    def __init__(
        self,
        store: PageStore | None = None,
        model: str = "",
        recall_past_runs: bool = True,
        sources: Sources | None = None,
    ) -> None:
        self._store = store
        self.sources = sources
        self._model = model
        self._recall_past_runs = recall_past_runs
        self.pieces: list[Piece] = []
        # Pages whose every piece is here, none of which the store can add.
        self._whole: set[str] = set()
        self.pages: Counter[str] = Counter()
        self.claims: Counter[str] = Counter()
        # By normalized URL, so that two spellings of a page are one lead.
        self.leads: dict[str, Lead] = {}
        self._refused: set[str] = set()
        self._site_searched: set[tuple[int, str]] = set()
        self._site_searches = 0
        # Sites found `avoid`, kept so that a search of the store leaves them out the first time.
        self._avoid: set[str] = set()

    def add(self, page: Page, texts: list[str], vectors: np.ndarray) -> list[Piece]:
        """Add the pieces of `page`, none read yet, and return them."""
        self.pages[domain(page.url)] += 1
        added = [Piece(page, text, vector) for text, vector in zip(texts, vectors, strict=True)]
        self.pieces += added
        self._whole.add(page.url)
        if self._store:
            # The cache saves reading; one that fails must not cost the run.
            try:
                self._store.put_pieces(page.url, texts, vectors, self._model)
            except sqlite3.Error as exc:
                logger.warning("pieces of %s not cached: %s", page.url, exc)
        return added

    def add_leads(self, encoder: Encoder, page: Page, depth: int) -> None:
        """Keep the links of `page` in a related block or its text as leads of `depth`, related
        first and then in page order, and not those past `LEAD_DEPTH`. A lead already kept, or
        refused, stays as it was."""
        if depth > LEAD_DEPTH:
            return
        own = {normalize(page.url), normalize(page.final_url)}
        links = [
            link
            for link in page.links
            if link.region in ("related", "main")
            and (key := normalize(link.url)) not in own
            and key not in self.leads
            and key not in self._refused
        ]
        # A stable sort, so each region keeps the page's order.
        links = sorted(links, key=lambda link: link.region != "related")[:LEADS_PER_PAGE]
        if not links:
            return
        words = [_NON_WORD.sub(" ", urlsplit(link.url).path).strip() for link in links]
        vectors = np.asarray(
            encoder.encode(
                [
                    f"{link.anchor}\n{link.context}\n{w}"
                    for link, w in zip(links, words, strict=True)
                ]
            ),
            dtype=float,
        )
        for link, vector in zip(links, vectors, strict=True):
            self.leads[normalize(link.url)] = Lead(
                link.url, link.anchor, link.context, link.region, depth, vector
            )

    def refuse(self, url: str) -> None:
        """Drop the lead at `url` for good, as one its site's robots.txt does not allow."""
        key = normalize(url)
        self._refused.add(key)
        self.leads.pop(key, None)

    def tiers(self, hosts: Iterable[str], deadline: float = 0.0) -> dict[str, str]:
        """The tier of each of `hosts`; all `unknown` without a classifier. By default only what
        the cache, rules and lists know, as the hosts were classified when found; a `deadline`
        waits that long for Wikidata."""
        if self.sources is None:
            return dict.fromkeys(hosts, "unknown")
        classified = self.sources.classify(hosts, deadline)
        return {host: source.tier for host, source in classified.items()}

    def avoided(self, claims: list[Claim], deadline: float = 0.0) -> set[str]:
        """The sites of `claims` that are now of tier `avoid`."""
        tiers = self.tiers({domain(claim.url) for claim in claims}, deadline)
        return {host for host, tier in tiers.items() if tier == "avoid"}

    def low_trust(self, claims: list[Claim]) -> set[int]:
        """The ids of the `claims` read from sites of tier `low`."""
        tiers = self.tiers({domain(claim.url) for claim in claims})
        return {claim.id for claim in claims if tiers[domain(claim.url)] == "low"}

    def full(self, url: str) -> bool:
        """Whether the run has read as many pages as it may from the site of `url`."""
        return self.pages[domain(url)] >= PAGES_PER_SITE

    def leads_for(
        self,
        encoder: Encoder,
        question: str,
        held: np.ndarray | None,
        claims: list[Claim],
        seen: set[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """The `limit` leads worth most for `question`, best first, as search hits. Those under
        `LEAD_MIN`, already seen or read, of an `avoid` site, or of a site read `PAGES_PER_SITE`
        times are left out."""
        skip = {u for u in map(_normalized, (*seen, *self._whole)) if u}
        candidates = [
            lead for key, lead in self.leads.items() if key not in skip and not self.full(lead.url)
        ]
        tiers = self.tiers({domain(lead.url) for lead in candidates})
        candidates = [lead for lead in candidates if tiers[domain(lead.url)] != "avoid"]
        if not candidates:
            return []
        vectors = np.stack([lead.vector for lead in candidates])
        wanted = np.asarray(encoder.encode([question], query=True), dtype=float)
        relevance = cosine_similarity(wanted, vectors)[0]
        novelty = np.zeros(len(candidates))
        if held is not None and len(held):
            novelty = 1.0 - cosine_similarity(vectors, held).max(axis=1)
        affinity = self._affinity(claims)
        worth = [
            relevance[i]
            + NOVELTY_WEIGHT * novelty[i]
            + AFFINITY_WEIGHT * affinity.get(domain(lead.url), 0.0)
            + (RELATED_BONUS if lead.region == "related" else 0.0)
            + TRUST_BONUS.get(tiers[domain(lead.url)], 0.0)
            for i, lead in enumerate(candidates)
        ]
        order = sorted(
            (i for i in range(len(candidates)) if relevance[i] >= LEAD_MIN), key=lambda i: -worth[i]
        )
        return [
            {
                "url": candidates[i].url,
                "title": candidates[i].anchor,
                "description": candidates[i].context,
                "depth": candidates[i].depth,
            }
            for i in order[:limit]
        ]

    def site_query(self, question_id: int, question: str, claims: list[Claim]) -> str | None:
        """A search of the site with the highest affinity, `SITE_AFFINITY` or more, that this
        question has not searched and that is under `PAGES_PER_SITE`. With `sources`, only a site of
        tier `high` or `normal`. None when no site is one, or after `SITE_SEARCHES` in the run."""
        if self._site_searches >= SITE_SEARCHES:
            return None
        affinity = self._affinity(claims)
        for site in sorted(affinity, key=lambda site: -affinity[site]):
            if affinity[site] < SITE_AFFINITY:
                break
            if (
                self.pages[site] < PAGES_PER_SITE
                and (question_id, site) not in self._site_searched
                and (self.sources is None or self.tiers([site])[site] in ("high", "normal"))
            ):
                self._site_searched.add((question_id, site))
                self._site_searches += 1
                return f"site:{site} {question}"
        return None

    def _affinity(self, claims: list[Claim]) -> dict[str, float]:
        """Per site, its claims another site corroborates over its pages read. With `sources`, a
        claim counts by the best `CORROBORATION_WEIGHT` among its corroborating claims' tiers.
        Corroboration is known only to a frontier that merges claims, so under the breadth frontier
        this is empty and a lead's worth is its relevance, novelty and region."""
        by_id = {claim.id: claim for claim in claims}
        tiers = self.tiers({domain(claim.url) for claim in claims})
        confirmed: defaultdict[str, float] = defaultdict(float)
        for claim in claims:
            if self.sources is None:
                weight = 1.0 if claim.corroborated_by else 0.0
            else:
                weight = max(
                    (
                        CORROBORATION_WEIGHT[tiers[domain(by_id[i].url)]]
                        for i in claim.corroborated_by
                        if i in by_id
                    ),
                    default=0.0,
                )
            confirmed[domain(claim.url)] += weight
        return {
            site: n / self.pages[site] for site, n in confirmed.items() if n and self.pages[site]
        }

    def record(self, claims: list[Claim]) -> None:
        """Count `claims` kept against the sites they were read from."""
        self.claims.update(domain(claim.url) for claim in claims)

    def recall(self, encoder: Encoder, question: str) -> list[tuple[Page, str]]:
        """The unread pieces closest to `question`, best first, and marks them read. Pieces of
        cached pages not in the corpus compete too, and join it when recalled. Pieces of `avoid`
        sites are never recalled."""
        tiers = self.tiers({domain(piece.page.url) for piece in self.pieces})
        candidates = [
            piece
            for piece in self.pieces
            if not piece.read and tiers[domain(piece.page.url)] != "avoid"
        ]
        stored = self._stored(question)
        candidates += stored
        if not candidates:
            return []
        scores = cosine_similarity(
            encoder.encode([question], query=True),
            np.stack([piece.vector for piece in candidates]),
        )[0]
        recalled: list[Piece] = []
        sites: Counter[str] = Counter()
        for i in (int(i) for i in np.argsort(-scores, kind="stable")):
            site = domain(candidates[i].page.url)
            if scores[i] < RECALL_MIN or len(recalled) == RECALL_PIECES:
                break
            if sites[site] < RECALL_PER_SITE:
                recalled.append(candidates[i])
                sites[site] += 1
        for piece in recalled:
            piece.read = True
            if any(piece is cached for cached in stored):
                if all(held.page.url != piece.page.url for held in self.pieces):
                    self.pages[domain(piece.page.url)] += 1
                self.pieces.append(piece)
        return [(piece.page, piece.text) for piece in recalled]

    def _stored(self, question: str) -> list[Piece]:
        """The cached pieces that best match the words of `question`, less those held here. A page
        recalled from the store is held only in part: its others may answer a later question."""
        if not self._store or not self._recall_past_runs:
            return []
        try:
            found = self._store.search(
                question, self._model, list(self._whole), exclude_hosts=self._avoid
            )
            tiers = self.tiers({domain(page.url) for page, _, _ in found})
            new = {host for host, tier in tiers.items() if tier == "avoid"}
            if new:
                self._avoid |= new
                found = self._store.search(
                    question, self._model, list(self._whole), exclude_hosts=self._avoid
                )
        except sqlite3.Error as exc:
            logger.warning("cached pieces not searched: %s", exc)
            return []
        held = {(piece.page.url, piece.text) for piece in self.pieces}
        return [
            Piece(page, text, vector)
            for page, text, vector in found
            if (page.url, text) not in held
        ]


def _normalized(url: str) -> str:
    try:
        return normalize(url)
    except ValueError:
        return ""
