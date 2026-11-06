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


def test_a_list_is_added_explained_changed_and_removed(client: httpx.Client) -> None:
    listed = client.get("/v1/sources/lists")
    assert listed.status_code == 200
    matches_shape("sources.lists", listed.json()[0])

    added = client.post(
        "/v1/sources/lists", json={"name": "Mine", "entries": ["farm.example"], "tag": "seo_spam"}
    )
    assert added.status_code == 201
    matches_shape("sources.list", added.json())
    assert added.json()["hosts"] == 1
    list_id = added.json()["id"]

    explained = client.get("/v1/sources/farm.example/explain")
    assert explained.status_code == 200
    matches_shape("sources.explain", explained.json())
    assert explained.json()["source"]["tier"] == "avoid"
    assert explained.json()["source"]["listed_by"] == ["Mine"]
    assert explained.json()["lists"][0]["state"] == "applies"

    off = client.patch(f"/v1/sources/lists/{list_id}", json={"enabled": False})
    assert off.status_code == 200
    assert client.get("/v1/sources/farm.example").json()["tier"] != "avoid"
    assert client.get("/v1/sources/farm.example/explain").json()["lists"][0]["state"] == "off"

    assert client.post(f"/v1/sources/lists/{list_id}/refresh").status_code == 200
    assert client.delete(f"/v1/sources/lists/{list_id}").status_code == 204
    assert client.delete(f"/v1/sources/lists/{list_id}").status_code == 404
    assert client.patch(f"/v1/sources/lists/{list_id}", json={}).status_code == 404


def test_a_run_naming_an_unknown_list_is_refused(client: httpx.Client) -> None:
    refused = client.post(
        "/v1/runs",
        json={"method": "ledger", "input": {"question": "x"}, "options": {"lists_on": ["nope"]}},
    )
    assert refused.status_code == 422
    assert refused.json() == {"message": "no list nope"}


def test_a_mistaken_list_says_why(client: httpx.Client) -> None:
    nameless = client.post("/v1/sources/lists", json={"entries": ["a.example"]})
    assert nameless.status_code == 422
    assert list(nameless.json()) == ["message"]

    unknown = client.post("/v1/sources/lists", json={"name": "x", "nope": 1})
    assert unknown.status_code == 422

    catalog = client.get("/v1/sources/lists").json()[0]["id"]
    fixed = client.patch(f"/v1/sources/lists/{catalog}", json={"url": "https://a.example/l"})
    assert fixed.status_code == 422


def test_an_exempt_mark_cancels_a_list_and_grants_no_trust(client: httpx.Client) -> None:
    added = client.post("/v1/sources/lists", json={"name": "Mine", "entries": ["spared.example"]})
    assert client.get("/v1/sources/spared.example").json()["tier"] == "avoid"

    marked = client.put("/v1/sources/spared.example/mark", json={"value": "exempt"})

    assert marked.json()["tier"] == "unknown"
    assert marked.json()["tags"] == ["exempt"]
    assert client.delete("/v1/sources/spared.example/mark").status_code == 204
    assert client.delete(f"/v1/sources/lists/{added.json()['id']}").status_code == 204


def test_tags_answer_for_many_hosts_at_once(client: httpx.Client) -> None:
    marked = client.put("/v1/sources/farm.example/mark", json={"value": "deny"})
    assert marked.status_code == 200
    farm = {"kind": "unknown", "tags": ["deny"], "tier": "avoid"}
    other = {"kind": "unknown", "tags": [], "tier": "unknown"}

    queried = client.get("/v1/sources/tags", params={"host": ["farm.example", "other.example"]})
    assert queried.status_code == 200
    assert queried.json() == {"farm.example": farm, "other.example": other}

    posted = client.post("/v1/sources/tags", json={"hosts": ["farm.example", "not a host"]})
    assert posted.status_code == 200
    assert posted.json() == {"farm.example": farm, "not a host": other}

    assert client.get("/v1/sources/tags").json() == {}
    assert client.delete("/v1/sources/farm.example/mark").status_code == 204


def test_tags_refuse_too_many_hosts(client: httpx.Client) -> None:
    hosts = [f"h{number}.example" for number in range(1001)]

    answer = client.post("/v1/sources/tags", json={"hosts": hosts})

    assert answer.status_code == 422
    assert answer.json() == {"message": "at most 1000 hosts at a time, got 1001"}
    assert client.post("/v1/sources/tags", json={"hosts": hosts[:1000]}).status_code == 200
    assert client.post("/v1/sources/tags", json={}).json()["detail"][0]["loc"] == ["body", "hosts"]


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
            client.get("/v1/sources/tags", params={"host": "nature.com"}),
            client.post("/v1/sources/tags", json={"hosts": ["nature.com"]}),
            client.delete("/v1/sources"),
            client.put("/v1/sources/x.example/mark", json={"value": "deny"}),
            client.delete("/v1/sources/x.example/mark"),
        ):
            assert response.status_code == 404
            assert response.json() == message
