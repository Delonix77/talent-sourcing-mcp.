"""Tests run fully offline: every external API is mocked with respx."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from talent_sourcing_mcp import server
from talent_sourcing_mcp.verification import merge_profiles, names_match, score_fit


def iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


GH_USER = {
    "login": "adaeze-dev", "name": "Adaeze Okafor", "bio": "Backend engineer, Python & Django",
    "location": "Lagos, Nigeria", "company": "@paystack", "email": None, "blog": "",
    "html_url": "https://github.com/adaeze-dev", "twitter_username": None, "hireable": True,
    "followers": 140, "public_repos": 38, "created_at": iso(6 * 365),
}
GH_REPOS = [
    {"name": "api", "fork": False, "language": "Python", "pushed_at": iso(4), "stargazers_count": 52},
    {"name": "web", "fork": False, "language": "Python", "pushed_at": iso(30), "stargazers_count": 3},
    {"name": "forked", "fork": True, "language": "Go", "pushed_at": iso(1), "stargazers_count": 999},
]
PDL_PERSON = {
    "id": "pdl123", "full_name": "Adaeze Okafor", "job_title": "Senior Backend Engineer",
    "job_company_name": "Paystack", "job_company_website": "paystack.com",
    "location_name": "Lagos, Lagos, Nigeria", "linkedin_url": "linkedin.com/in/adaeze-okafor",
    "github_url": "github.com/adaeze-dev", "skills": ["python", "django", "postgresql", "aws"],
    "inferred_years_experience": 6, "work_email": True,
}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TALENT_MCP_DATA_DIR", str(tmp_path))
    for k in ("GITHUB_TOKEN", "STACKEXCHANGE_KEY", "PDL_API_KEY", "HUNTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield


def mock_github(router: respx.Router) -> None:
    router.get("https://api.github.com/search/users").respond(json={"items": [{"login": "adaeze-dev"}]})
    router.get("https://api.github.com/users/adaeze-dev").respond(json=GH_USER)
    router.get("https://api.github.com/users/adaeze-dev/repos").respond(json=GH_REPOS)


# ------------------------------------------------------------------ verification


def test_names_match():
    assert names_match("Adaeze Okafor", "Adaeze N. Okafor")
    assert names_match("adaeze", "Adaeze Okafor")
    assert not names_match("Adaeze Okafor", "Chidi Eze")


def test_single_source_is_not_verified():
    from talent_sourcing_mcp.providers import GitHubClient
    c = merge_profiles([GitHubClient.normalize(GH_USER, GH_REPOS)])
    v = c["verification"]
    assert v["level"] != "verified"
    assert c["skills"] == ["Python"]  # forks ignored
    assert c["signals"]["github"]["stars_on_recent_repos"] == 55


def test_three_corroborating_sources_are_verified():
    from talent_sourcing_mcp.providers import GitHubClient, PeopleDataLabsClient
    gh = GitHubClient.normalize(GH_USER, GH_REPOS)
    pdl = PeopleDataLabsClient.normalize(PDL_PERSON, likelihood=9)
    c = merge_profiles([gh, pdl], email_check={"email": "adaeze@paystack.com", "status": "valid"})
    v = c["verification"]
    assert v["level"] == "verified", v
    assert v["score"] >= 80
    assert {ch["check"] for ch in v["checks"] if ch["points"] == ch["max"]} >= {
        "name_consistency", "location_consistency", "recent_activity", "contact_verified"}


def test_mismatched_identity_is_flagged():
    from talent_sourcing_mcp.providers import GitHubClient, PeopleDataLabsClient
    gh = GitHubClient.normalize(GH_USER, GH_REPOS)
    other = PeopleDataLabsClient.normalize({**PDL_PERSON, "full_name": "Chidi Eze", "location_name": "Nairobi, Kenya"})
    v = merge_profiles([gh, other])["verification"]
    checks = {ch["check"]: ch for ch in v["checks"]}
    assert checks["name_consistency"]["points"] == 0
    assert checks["location_consistency"]["points"] == 0


def test_fit_score():
    from talent_sourcing_mcp.providers import PeopleDataLabsClient
    c = merge_profiles([PeopleDataLabsClient.normalize(PDL_PERSON)])
    fit = score_fit(c, {"skills": ["Python", "Django", "Kubernetes"], "location": "Lagos", "min_years_experience": 5})
    assert fit["matched_skills"] == ["python", "django"]
    assert fit["missing_skills"] == ["kubernetes"]
    assert fit["score"] == 40 + 20 + 20


# ----------------------------------------------------------------------- tools


@respx.mock
async def test_search_github_talent():
    mock_github(respx.mock)
    out = await server.search_github_talent(location="Lagos", language="Python", limit=5)
    assert out["query"] == "type:user location:Lagos language:Python"
    assert out["candidates"][0]["name"] == "Adaeze Okafor"
    assert "profiles" not in out["candidates"][0]


@respx.mock
async def test_github_rate_limit_message():
    respx.get("https://api.github.com/search/users").respond(403, text="API rate limit exceeded")
    with pytest.raises(ToolError, match="rate limit"):
        await server.search_github_talent(location="Lagos")


@respx.mock
async def test_stackoverflow_experts_filters_location():
    respx.get("https://api.stackexchange.com/2.3/tags/python/top-answerers/all_time").respond(json={"items": [
        {"user": {"user_id": 1}, "score": 900}, {"user": {"user_id": 2}, "score": 500}]})
    respx.get(url__regex=r"https://api\.stackexchange\.com/2\.3/users/1;2").respond(json={"items": [
        {"user_id": 1, "display_name": "Tunde &amp; Co", "location": "Abuja, Nigeria", "reputation": 25000,
         "creation_date": 1400000000, "last_access_date": int(datetime.now().timestamp()) - 3600,
         "link": "https://stackoverflow.com/users/1", "badge_counts": {"gold": 3}},
        {"user_id": 2, "display_name": "Someone", "location": "Berlin", "reputation": 9000,
         "creation_date": 1400000000, "last_access_date": 1700000000, "link": "x"}]})
    out = await server.search_stackoverflow_experts(tag="python", location="Nigeria")
    assert out["count"] == 1
    c = out["candidates"][0]
    assert c["name"] == "Tunde & Co"
    assert c["signals"]["stackoverflow"]["tag_score"] == {"python": 900}


async def test_paid_sources_explain_missing_keys():
    with pytest.raises(ToolError, match="PDL_API_KEY"):
        await server.search_people(job_title="recruiter")
    with pytest.raises(ToolError, match="HUNTER_API_KEY"):
        await server.verify_email("a@b.com")


@respx.mock
async def test_verify_candidate_cross_checks_all_sources(monkeypatch):
    monkeypatch.setenv("PDL_API_KEY", "k")
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    mock_github(respx.mock)
    pdl = respx.get("https://api.peopledatalabs.com/v5/person/enrich").respond(
        json={"status": 200, "likelihood": 9, "data": PDL_PERSON})
    respx.get("https://api.hunter.io/v2/email-finder").respond(
        json={"data": {"email": "adaeze@paystack.com", "score": 94}})
    respx.get("https://api.hunter.io/v2/email-verifier").respond(
        json={"data": {"status": "valid", "score": 98}})

    out = await server.verify_candidate(github_login="adaeze-dev")
    assert pdl.calls.last.request.url.params["profile"] == "https://github.com/adaeze-dev"
    assert out["sources"] == ["github", "peopledatalabs"]
    assert out["links"]["linkedin"] == "https://linkedin.com/in/adaeze-okafor"
    assert "adaeze@paystack.com" in out["emails"]
    assert out["verification"]["level"] == "verified"


@respx.mock
async def test_find_verified_talent_ranks_and_survives_source_failure(monkeypatch):
    monkeypatch.setenv("PDL_API_KEY", "k")
    mock_github(respx.mock)
    respx.get(url__regex=r"https://api\.stackexchange\.com/.*").mock(side_effect=httpx.ConnectError("down"))
    search = respx.post("https://api.peopledatalabs.com/v5/person/search").respond(json={"data": [PDL_PERSON]})
    respx.get("https://api.peopledatalabs.com/v5/person/enrich").respond(
        json={"status": 200, "likelihood": 9, "data": PDL_PERSON})

    out = await server.find_verified_talent(skills=["Python", "Django"], location="Lagos",
                                            job_title="backend engineer", verify_top=5)
    assert set(out["searched"]) == {"github", "stackoverflow", "peopledatalabs"}
    assert any("stackoverflow" in w for w in out["warnings"])
    assert out["count"] >= 1
    top = out["candidates"][0]
    assert set(top["sources"]) == {"github", "peopledatalabs"}
    assert top["fit"]["matched_skills"] == ["python", "django"]
    body = search.calls.last.request.content.decode()
    assert "location_name" in body and "lagos" in body


# -------------------------------------------------------------------- shortlist


@respx.mock
async def test_shortlist_export_and_do_not_contact():
    mock_github(respx.mock)
    found = (await server.search_github_talent(location="Lagos"))["candidates"][0]
    await server.shortlist_add("backend-nov", found, notes="Strong Django work", fit_score=80)
    await server.shortlist_add("backend-nov", found)  # de-duplicated
    assert (await server.shortlist_view())["backend-nov"] == 1

    exp = await server.shortlist_export("backend-nov", "csv")
    rows = list(csv.DictReader(io.StringIO(exp["content"])))
    assert rows[0]["name"] == "Adaeze Okafor"
    assert rows[0]["github"] == "https://github.com/adaeze-dev"
    assert rows[0]["notes"] == "Strong Django work"

    await server.do_not_contact("https://github.com/adaeze-dev/", reason="Asked not to be contacted")
    assert (await server.shortlist_view("backend-nov"))["candidates"] == []
    assert (await server.search_github_talent(location="Lagos"))["count"] == 0
    with pytest.raises(ToolError, match="do-not-contact"):
        await server.shortlist_add("backend-nov", found)
