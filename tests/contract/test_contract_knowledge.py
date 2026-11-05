"""The page cache and what is known of sites: pages, sites, recall, icons and sources."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from harness import ICON, Fakes, matches_shape, transcript


@pytest.fixture
def read(client: httpx.Client, fakes: Fakes) -> Iterator[str]:
    """The URL of the one page a run has read, as the only page in the cache."""
    assert client.delete("/v1/pages").status_code == 204
    events = transcript(
        client,
        "/v1/runs",
        json={
            "method": "ledger",
            "input": {"question": "When was the Kvasir stone catalogued?"},
            "options": {"max_searches": 1, "results_per_query": 1, "max_attempts": 1},
        },
    )
    assert events[-1][0] == "done"
    # The cache keys a page by its normalized URL, which prefers https.
    yield f"{fakes.url}/web/stone.html".replace("http://", "https://")


def test_the_cache_is_empty_when_flushed(client: httpx.Client) -> None:
    assert client.delete("/v1/pages").status_code == 204

    assert client.get("/v1/pages").json() == []
    assert client.get("/v1/sites").json() == []
    assert client.get("/v1/sites/graph").json() == {"sites": [], "edges": []}
    assert client.get("/v1/pages/recall", params={"q": "x"}).json()["pieces"] == []
    stats = client.get("/v1/pages/stats").json()
    matches_shape("pages.stats", stats)
    assert stats["pages"] == stats["pieces"] == 0


def test_a_page_read_is_listed(client: httpx.Client, read: str) -> None:
    pages = client.get("/v1/pages").json()

    matches_shape("pages.list", pages)
    assert [page["url"] for page in pages] == [read]
    assert pages[0]["engine"] == "static"
    assert pages[0]["title"] == "The Kvasir stone"
    assert pages[0]["pieces"] >= 1
    host = read.split("/")[2]
    assert client.get("/v1/pages", params={"site": host}).json() == pages
    assert client.get("/v1/pages", params={"site": "other.example"}).json() == []
    assert client.get("/v1/pages/stats").json()["pages"] == 1


def test_a_page_says_what_it_links_to_and_who_cited_it(client: httpx.Client, read: str) -> None:
    page_id = client.get("/v1/pages").json()[0]["id"]

    detail = client.get(f"/v1/pages/{page_id}").json()

    matches_shape("pages.detail", detail)
    assert detail["url"] == read
    assert detail["final_url"] == read.replace("https://", "http://")
    assert "catalogued in 1783" in detail["text"]
    assert [lead["anchor"] for leads in detail["leads"].values() for lead in leads] == [
        "the other page"
    ]
    assert len(detail["runs"]) >= 1


def test_sites_group_their_pages(client: httpx.Client, read: str) -> None:
    sites = client.get("/v1/sites").json()
    graph = client.get("/v1/sites/graph").json()

    matches_shape("sites.list", sites)
    matches_shape("sites.graph", graph)
    assert [site["domain"] for site in sites] == [read.split("/")[2]]
    assert sites[0]["pages"] == 1
    assert sites[0]["engines"] == ["static"]
    assert graph["sites"][0]["links"] == 1


def test_recall_scores_pieces(client: httpx.Client, read: str) -> None:
    recall = client.get("/v1/pages/recall", params={"q": "kvasir stone catalogued"}).json()

    matches_shape("pages.recall", recall)
    assert recall["pieces"][0]["url"] == read
    assert recall["pieces"][0]["similarity"] > 0


def test_recall_needs_a_query(client: httpx.Client) -> None:
    response = client.get("/v1/pages/recall")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "q"]


def test_a_page_can_be_read_again(client: httpx.Client, read: str, fakes: Fakes) -> None:
    page_id = client.get("/v1/pages").json()[0]["id"]
    fakes.requests.clear()

    response = client.post(f"/v1/pages/{page_id}/refetch")

    assert response.status_code == 200
    assert response.json()["url"] == read
    assert ("/web/stone.html", {}) in fakes.requests


def test_a_page_that_cannot_be_read_again_is_502(
    client: httpx.Client, read: str, fakes: Fakes
) -> None:
    page_id = client.get("/v1/pages").json()[0]["id"]
    fakes.web_status = 404
    try:
        response = client.post(f"/v1/pages/{page_id}/refetch")
    finally:
        fakes.web_status = None

    assert response.status_code == 502
    assert list(response.json()) == ["message"]


def test_a_page_can_be_deleted(client: httpx.Client, read: str) -> None:
    page_id = client.get("/v1/pages").json()[0]["id"]

    assert client.delete(f"/v1/pages/{page_id}").status_code == 204

    assert client.get("/v1/pages").json() == []
    for response in (
        client.get(f"/v1/pages/{page_id}"),
        client.delete(f"/v1/pages/{page_id}"),
        client.post(f"/v1/pages/{page_id}/refetch"),
    ):
        assert response.status_code == 404
        assert response.json() == {"message": f"no page {page_id}"}


def test_a_site_can_be_deleted(client: httpx.Client, read: str) -> None:
    host = read.split("/")[2]

    assert client.delete(f"/v1/sites/{host}").status_code == 204

    assert client.get("/v1/pages").json() == []
    response = client.delete(f"/v1/sites/{host}")
    assert response.status_code == 404
    assert response.json() == {"message": f"no pages of {host}"}


# -- Icons -----------------------------------------------------------------------------------------


def test_an_icon_is_locked_down(client: httpx.Client) -> None:
    response = client.get("/v1/favicons/known.example")

    assert response.status_code == 200
    assert response.content == ICON
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cache-control"] == "public, max-age=604800"
    assert client.delete("/v1/favicons").status_code == 204


def test_a_site_with_no_icon_is_a_cacheable_404(client: httpx.Client) -> None:
    response = client.get("/v1/favicons/unknown.example")

    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"].startswith("public, max-age=")


def test_icons_turned_off_miss_for_a_day(new_service: Any) -> None:
    service = new_service()
    with service.client() as client:
        assert client.patch("/v1/settings", json={"favicons.providers": []}).status_code == 200

        response = client.get("/v1/favicons/known.example")

        assert response.status_code == 404
        assert response.headers["cache-control"] == "public, max-age=86400"
        assert client.delete("/v1/favicons").status_code == 204


# -- Sources ---------------------------------------------------------------------------------------


def test_what_is_known_of_a_site(client: httpx.Client) -> None:
    response = client.get("/v1/sources/nature.com")

    assert response.status_code == 200
    matches_shape("sources.info", response.json())


def test_a_mark_is_set_and_cleared(client: httpx.Client) -> None:
    marked = client.put("/v1/sources/marked.example/mark", json={"value": "deny"})

    assert marked.status_code == 200
    assert marked.json()["tier"] == "avoid"
    matches_shape("sources.marked", marked.json())

    assert client.delete("/v1/sources/marked.example/mark").status_code == 204
    assert client.get("/v1/sources/marked.example").json()["tier"] != "avoid"
    assert client.delete("/v1/sources").status_code == 204


def test_a_mistaken_source_request_says_why(client: httpx.Client) -> None:
    bad_host = client.get("/v1/sources/not a host")
    assert bad_host.status_code == 422
    assert bad_host.json() == {"message": "'not a host' is not a host name"}

    bad_mark = client.put("/v1/sources/marked.example/mark", json={"value": "nope"})
    assert bad_mark.status_code == 422
    assert list(bad_mark.json()) == ["message"]

    no_value = client.put("/v1/sources/marked.example/mark", json={})
    assert no_value.status_code == 422
    assert no_value.json()["detail"][0]["loc"] == ["body", "value"]


def test_a_host_the_settings_decide_cannot_be_marked(client: httpx.Client) -> None:
    message = {
        "message": "denied.example is in sources.allow or sources.deny, so a mark cannot change it"
    }

    marked = client.put("/v1/sources/denied.example/mark", json={"value": "allow"})
    assert marked.status_code == 409
    assert marked.json() == message

    cleared = client.delete("/v1/sources/denied.example/mark")
    assert cleared.status_code == 409
    assert cleared.json() == message


def test_sources_turned_off_say_so(new_service: Any) -> None:
    service = new_service()
    message = {"message": "sources are off: set sources.enabled"}
    with service.client() as client:
        for response in (
            client.get("/v1/sources/nature.com"),
            client.delete("/v1/sources"),
            client.put("/v1/sources/x.example/mark", json={"value": "deny"}),
            client.delete("/v1/sources/x.example/mark"),
        ):
            assert response.status_code == 404
            assert response.json() == message
