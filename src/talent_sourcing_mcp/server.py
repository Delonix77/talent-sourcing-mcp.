"""Talent Sourcing MCP server.

Run locally for desktop AI apps (stdio):      talent-sourcing-mcp
Run as a network service for other tools:     talent-sourcing-mcp --transport http --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .config import Settings
from .providers import (
    REGIONS,
    GitHubClient,
    region_matches,
    resolve_region,
    HunterClient,
    PeopleDataLabsClient,
    ProviderError,
    StackExchangeClient,
)
from .store import Store
from .verification import merge_profiles, score_fit

log = logging.getLogger("talent_sourcing_mcp")

INSTRUCTIONS = """\
Finds and verifies candidates from public, terms-compliant sources: GitHub, Stack Overflow,
People Data Labs (any role, needs a key) and Hunter (email find/verify, needs a key).

Locations can be a city, a country, or a whole region: Europe, EU, LATAM, Africa,
North America or US (see list_regions). Regions are expanded to their countries and
biggest tech hubs automatically.

Typical flow: list_sources -> find_verified_talent (or a single-source search) ->
verify_candidate on promising people -> shortlist_add -> shortlist_export.

A "verified" level means independent sources corroborate the person and their activity.
It is evidence for a recruiter to review, not proof of identity. Always show the checks.
Respect the do-not-contact list and data-protection law (e.g. NDPA, GDPR): only use what
the person made public, for recruiting, and remove anyone who asks.
"""

mcp = MCPServer(
    name="talent-sourcing",
    title="Talent Sourcing",
    version=__version__,
    instructions=INSTRUCTIONS,
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def _settings() -> Settings:
    return Settings()


def _store() -> Store:
    return Store(_settings().data_dir)


def _public(c: dict[str, Any]) -> dict[str, Any]:
    """Drop raw per-source payloads to keep tool output compact."""
    return {k: v for k, v in c.items() if k != "profiles"}


def _fail(exc: Exception) -> None:
    raise ToolError(str(exc)) from exc


LOCATION_HELP = "City, country, or region: 'Lagos', 'Germany', 'Europe', 'EU', 'LATAM', 'Africa', 'North America', 'US'"


async def _github_search(
    s: Settings, keywords: str | None, location: str | None, language: str | None,
    min_followers: int | None, min_repos: int | None, limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Search GitHub; a region is searched across its biggest tech hubs and merged."""
    client = GitHubClient(s)
    region = resolve_region(location)
    if not region:
        q = client.build_query(keywords, location, language, min_followers, min_repos)
        return await client.search(q, limit), [q]
    hubs = REGIONS[region]["github_hubs"]
    per_hub = max(1, -(-limit // len(hubs)))  # ceiling division
    queries = [client.build_query(keywords, hub, language, min_followers, min_repos) for hub in hubs]
    results = await asyncio.gather(*(client.search(q, per_hub) for q in queries), return_exceptions=True)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors = [r for r in results if isinstance(r, Exception)]
    # Interleave hubs so the top of the list covers the whole region.
    lists = [r for r in results if not isinstance(r, Exception)]
    for i in range(per_hub):
        for lst in lists:
            if i < len(lst) and lst[i]["id"] not in seen:
                seen.add(lst[i]["id"])
                found.append(lst[i])
    if not found and errors:
        raise errors[0]
    return found[:limit], queries


def _fit(c: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """score_fit, with region-aware location matching."""
    region = resolve_region(req.get("location"))
    if not region:
        return score_fit(c, req)
    fit = score_fit(c, {**req, "location": None})
    label = region.upper() if len(region) <= 5 else region.title()
    if region_matches(c.get("location"), region):
        fit["location"] = f"Located in {c.get('location')} ({label})."
    elif c.get("location"):
        fit["score"] -= 20
        fit["location"] = f"Located in {c.get('location')}, outside {label}."
    else:
        fit["score"] -= 10
        fit["location"] = "Location unknown."
    return fit


# --------------------------------------------------------------------- sources


@mcp.tool(annotations=READ)
async def list_sources() -> dict[str, Any]:
    """Show which talent data sources are configured and their limits."""
    s = _settings()
    return {"sources": s.sources(), "data_dir": str(s.data_dir)}


# --------------------------------------------------------------------- searches


@mcp.tool(annotations=READ)
async def search_github_talent(
    keywords: Annotated[str | None, Field(description="Words in bio/name, e.g. 'backend fintech'")] = None,
    location: Annotated[str | None, Field(description=LOCATION_HELP)] = None,
    language: Annotated[str | None, Field(description="Main programming language, e.g. 'Python'")] = None,
    min_followers: Annotated[int | None, Field(ge=0)] = None,
    min_repos: Annotated[int | None, Field(ge=0)] = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """Find software engineers on GitHub by location, language and activity."""
    if not any([keywords, location, language]):
        raise ToolError("Give at least one of keywords, location or language.")
    try:
        found, queries = await _github_search(_settings(), keywords, location, language,
                                              min_followers, min_repos, limit)
    except ProviderError as exc:
        _fail(exc)
    q = queries[0] if len(queries) == 1 else queries
    store = _store()
    results = [_public(merge_profiles([c])) for c in found if not store.is_blocked(c)]
    return {"query": q, "count": len(results), "candidates": results}


@mcp.tool(annotations=READ)
async def search_stackoverflow_experts(
    tag: Annotated[str, Field(description="Stack Overflow tag, e.g. 'python', 'react', 'kubernetes'")],
    location: Annotated[str | None, Field(description=LOCATION_HELP)] = None,
    min_reputation: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find proven experts in a technology from Stack Overflow's top answerers for a tag."""
    try:
        found = await StackExchangeClient(_settings()).top_answerers(tag, location, min_reputation, limit)
    except ProviderError as exc:
        _fail(exc)
    store = _store()
    results = [_public(merge_profiles([c])) for c in found if not store.is_blocked(c)]
    return {"tag": tag, "count": len(results), "candidates": results}


@mcp.tool(annotations=READ)
async def search_people(
    job_title: Annotated[str | None, Field(description="e.g. 'talent acquisition', 'accountant'")] = None,
    location_country: Annotated[str | None, Field(description="Country or region, e.g. 'nigeria', 'europe', 'latam'")] = None,
    location_locality: Annotated[str | None, Field(description="City, e.g. 'lagos'")] = None,
    skills: Annotated[list[str] | None, Field(description="Every skill listed must be present")] = None,
    min_years_experience: Annotated[int | None, Field(ge=0)] = None,
    industry: Annotated[str | None, Field(description="e.g. 'financial services'")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """Search professionals in any role or industry (People Data Labs; needs PDL_API_KEY). Uses credits."""
    client = PeopleDataLabsClient(_settings())
    try:
        query = client.build_query(job_title, location_country, location_locality, skills,
                                   min_years_experience, industry)
        found = await client.search(query, limit)
    except ProviderError as exc:
        _fail(exc)
    store = _store()
    results = [_public(merge_profiles([c])) for c in found if not store.is_blocked(c)]
    return {"count": len(results), "candidates": results}


@mcp.tool(annotations=READ)
async def enrich_person(
    linkedin_url: str | None = None,
    github_url: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
) -> dict[str, Any]:
    """Look up one person's professional profile (People Data Labs; needs PDL_API_KEY). Uses a credit."""
    profile = linkedin_url or github_url
    try:
        found = await PeopleDataLabsClient(_settings()).enrich(
            profile=profile, email=email, name=name, company=company
        )
    except ProviderError as exc:
        _fail(exc)
    if not found:
        return {"found": False}
    return {"found": True, "candidate": _public(merge_profiles([found]))}


@mcp.tool(annotations=READ)
async def find_email(first_name: str, last_name: str, company_domain: str) -> dict[str, Any]:
    """Find someone's work email from their name and company domain (Hunter; needs HUNTER_API_KEY)."""
    try:
        return await HunterClient(_settings()).find_email(company_domain, first_name, last_name)
    except ProviderError as exc:
        _fail(exc)


@mcp.tool(annotations=READ)
async def verify_email(email: str) -> dict[str, Any]:
    """Check whether an email address is real and deliverable (Hunter; needs HUNTER_API_KEY)."""
    try:
        return await HunterClient(_settings()).verify_email(email)
    except ProviderError as exc:
        _fail(exc)


# ------------------------------------------------------------- verification


async def _gather_person(
    s: Settings,
    github_login: str | None = None,
    stackoverflow_user_id: int | None = None,
    linkedin_url: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
    company_domain: str | None = None,
    existing: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Fetch everything we can about one person from every configured source, then merge."""
    profiles: list[dict[str, Any]] = list(existing or [])
    have = {p["source"] for p in profiles}
    notes: list[str] = []

    if github_login and "github" not in have:
        try:
            profiles.append(await GitHubClient(s).profile(github_login))
        except ProviderError as exc:
            notes.append(str(exc))
    if stackoverflow_user_id and "stackoverflow" not in have:
        try:
            profiles.extend(await StackExchangeClient(s).users([stackoverflow_user_id]))
        except ProviderError as exc:
            notes.append(str(exc))

    if s.pdl_api_key and "peopledatalabs" not in have:
        gh_url = next((p["links"].get("github") for p in profiles if p["links"].get("github")), None)
        li_url = linkedin_url or next((p["links"].get("linkedin") for p in profiles if p["links"].get("linkedin")), None)
        nm = name or next((p["name"] for p in profiles if p.get("name")), None)
        co = company or next((p["company"] for p in profiles if p.get("company")), None)
        if li_url or gh_url or email or (nm and co):
            try:
                hit = await PeopleDataLabsClient(s).enrich(
                    profile=li_url or gh_url, email=email,
                    name=None if (li_url or gh_url or email) else nm,
                    company=None if (li_url or gh_url or email) else co,
                )
                if hit:
                    profiles.append(hit)
                    have.add("peopledatalabs")
            except ProviderError as exc:
                notes.append(str(exc))

    # A GitHub link found via PDL lets us pull GitHub evidence too.
    if "github" not in {p["source"] for p in profiles}:
        gh = next((p["links"].get("github") for p in profiles if p["links"].get("github")), None)
        if gh:
            login = gh.rstrip("/").rsplit("/", 1)[-1]
            try:
                profiles.append(await GitHubClient(s).profile(login))
            except ProviderError as exc:
                notes.append(str(exc))

    if not profiles:
        raise ToolError("Couldn't find this person on any source. " + " ".join(notes))

    email_check = None
    if s.hunter_api_key:
        hunter = HunterClient(s)
        candidate_email = email or next((e for p in profiles for e in p.get("emails", [])), None)
        try:
            if not candidate_email:
                full = name or next((p["name"] for p in profiles if p.get("name")), "") or ""
                domain = company_domain or next(
                    (p.get("company_domain") for p in profiles if p.get("company_domain")), None
                )
                parts = full.split()
                if domain and len(parts) >= 2:
                    found = await hunter.find_email(domain, parts[0], parts[-1])
                    candidate_email = found.get("email")
            if candidate_email:
                email_check = await hunter.verify_email(candidate_email)
                if email_check.get("status") in ("valid", "accept_all", "webmail"):
                    profiles[0].setdefault("emails", [])
                    if candidate_email not in profiles[0]["emails"]:
                        profiles[0]["emails"].append(candidate_email)
        except ProviderError as exc:
            notes.append(str(exc))

    return merge_profiles(profiles, email_check), notes


@mcp.tool(annotations=READ)
async def verify_candidate(
    github_login: str | None = None,
    stackoverflow_user_id: int | None = None,
    linkedin_url: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
    company_domain: Annotated[str | None, Field(description="Used with name to find a work email")] = None,
) -> dict[str, Any]:
    """Cross-check one person across every configured source and return a verification score
    (0-100) with the evidence behind each check. Give as many identifiers as you have."""
    if not any([github_login, stackoverflow_user_id, linkedin_url, email, (name and company)]):
        raise ToolError("Give a GitHub login, Stack Overflow ID, LinkedIn URL, email, or name plus company.")
    merged, notes = await _gather_person(
        _settings(), github_login, stackoverflow_user_id, linkedin_url, email, name, company, company_domain
    )
    out = _public(merged)
    if _store().is_blocked(merged):
        out["do_not_contact"] = True
    if notes:
        out["notes"] = notes
    return out


@mcp.tool(annotations=READ)
async def score_candidate_fit(
    candidate: Annotated[dict[str, Any], Field(description="A candidate object returned by another tool")],
    skills: list[str] | None = None,
    location: str | None = None,
    min_years_experience: int | None = None,
) -> dict[str, Any]:
    """Score how well a candidate fits a role (0-100): skills 60, location 20, experience 20."""
    return _fit(candidate, {"skills": skills, "location": location,
                                 "min_years_experience": min_years_experience})


@mcp.tool(annotations=READ)
async def find_verified_talent(
    skills: Annotated[list[str], Field(description="Must-have skills, e.g. ['python', 'django']")],
    location: Annotated[str | None, Field(description=LOCATION_HELP)] = None,
    job_title: Annotated[str | None, Field(description="Used for People Data Labs search")] = None,
    min_years_experience: int | None = None,
    github_language: Annotated[str | None, Field(description="Defaults to the first skill")] = None,
    stackoverflow_tag: Annotated[str | None, Field(description="Defaults to the first skill")] = None,
    include_sources: Annotated[list[str] | None, Field(
        description="Any of github, stackoverflow, peopledatalabs. Defaults to all configured.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    verify_top: Annotated[int, Field(ge=0, le=25, description=(
        "How many top results to cross-check against other sources (uses API credits)"))] = 5,
    min_verification_score: Annotated[int, Field(ge=0, le=100)] = 0,
) -> dict[str, Any]:
    """One-call pipeline: search every configured source for a role, cross-verify the best
    matches, then rank by fit and verification. Best starting point for integrations."""
    s = _settings()
    store = _store()
    wanted = set(include_sources or ["github", "stackoverflow", "peopledatalabs"])
    first_skill = skills[0] if skills else None
    tasks: dict[str, Any] = {}
    if "github" in wanted and (github_language or first_skill or location):
        async def gh_task():
            return (await _github_search(s, None, location, github_language or first_skill,
                                         None, None, limit))[0]
        tasks["github"] = gh_task()
    if "stackoverflow" in wanted and (stackoverflow_tag or first_skill):
        tag = (stackoverflow_tag or first_skill).lower().replace(" ", "-")
        tasks["stackoverflow"] = StackExchangeClient(s).top_answerers(tag, location, 0, limit)
    if "peopledatalabs" in wanted and s.pdl_api_key:
        pdl = PeopleDataLabsClient(s)
        tasks["peopledatalabs"] = pdl.search(
            pdl.build_query(job_title, skills=skills[:3],
                            min_years_experience=min_years_experience, location=location),
            limit,
        )
    if not tasks:
        raise ToolError("No sources to search. Check include_sources and your API keys (list_sources).")

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    for src, res in zip(tasks, results):
        if isinstance(res, Exception):
            errors.append(str(res))
        else:
            found.extend(res)

    req = {"skills": skills, "location": location, "min_years_experience": min_years_experience}
    candidates = [merge_profiles([c]) for c in found if not store.is_blocked(c)]
    candidates.sort(key=lambda c: -_fit(c, req)["score"])

    verified: list[dict[str, Any]] = []
    for i, c in enumerate(candidates):
        if i < verify_top:
            sig = c["signals"]
            try:
                c, notes = await _gather_person(
                    s,
                    github_login=sig.get("github", {}).get("github_login"),
                    stackoverflow_user_id=sig.get("stackoverflow", {}).get("stackoverflow_user_id"),
                    linkedin_url=c["links"].get("linkedin"),
                    existing=c["profiles"],
                )
                errors.extend(n for n in notes if n not in errors)
            except ToolError as exc:
                errors.append(str(exc))
        fit = _fit(c, req)
        c["fit"] = fit
        if c["verification"]["score"] >= min_verification_score:
            verified.append(c)

    verified.sort(key=lambda c: -(c["fit"]["score"] * 0.6 + c["verification"]["score"] * 0.4))
    return {
        "searched": list(tasks),
        "count": len(verified[:limit]),
        "candidates": [_public(c) for c in verified[:limit]],
        "warnings": errors,
        "ranking": "60% fit + 40% verification",
    }


@mcp.tool(annotations=READ)
async def list_regions() -> dict[str, Any]:
    """Show the regions you can search by name, with the countries and GitHub hubs each covers."""
    return {
        name: {"countries": len(r["countries"]), "github_hubs": r["github_hubs"],
               "sample_countries": r["countries"][:8]}
        for name, r in REGIONS.items()
    }


# ------------------------------------------------------------------ shortlists


@mcp.tool(annotations=WRITE)
async def shortlist_add(
    shortlist: Annotated[str, Field(description="Name, e.g. 'moniepoint-backend-nov'")],
    candidate: Annotated[dict[str, Any], Field(description="A candidate object returned by another tool")],
    notes: str | None = None,
    fit_score: int | None = None,
) -> dict[str, Any]:
    """Save a candidate to a named shortlist (kept on disk between sessions)."""
    if fit_score is None and isinstance(candidate.get("fit"), dict):
        fit_score = candidate["fit"].get("score")
    try:
        return _store().add(shortlist, candidate, notes, fit_score)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ)
async def shortlist_view(shortlist: str | None = None) -> dict[str, Any]:
    """List all shortlists with counts, or show the candidates in one shortlist."""
    try:
        return _store().list(shortlist)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def shortlist_remove(shortlist: str, candidate_id: str) -> dict[str, Any]:
    """Remove a candidate (by id, e.g. 'github:octocat') from a shortlist."""
    return _store().remove(shortlist, candidate_id)


@mcp.tool(annotations=WRITE)
async def shortlist_export(
    shortlist: str,
    format: Annotated[str, Field(pattern="^(csv|json)$")] = "csv",
) -> dict[str, Any]:
    """Export a shortlist as CSV (for spreadsheets/ATS import) or JSON. Returns the content and file path."""
    try:
        content, path = _store().export(shortlist, format)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {"path": str(path), "format": format, "content": content}


@mcp.tool(annotations=WRITE)
async def do_not_contact(
    key: Annotated[str, Field(description="Email, profile URL, or candidate id to block")],
    reason: str | None = None,
) -> dict[str, Any]:
    """Add someone to the do-not-contact list. They are removed from shortlists and hidden from searches."""
    return _store().block(key, reason)


# ------------------------------------------------------------------------ main


def _http_app(host: str, token: str | None):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = mcp.streamable_http_app(host=host)
    if token:
        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # Accept the token as a bearer header, or as ?key=... in the URL for
                # clients (like some connector dialogs) that can't set headers.
                header = request.headers.get("authorization", "")
                query = request.query_params.get("key", "")
                ok = secrets.compare_digest(header, f"Bearer {token}") or (
                    bool(query) and secrets.compare_digest(query, token)
                )
                if not ok:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="talent-sourcing-mcp", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn

    token = _settings().http_auth_token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        p.error("Refusing to listen on a public address without TALENT_MCP_AUTH_TOKEN set.")
    log.info("Serving MCP over HTTP at http://%s:%d/mcp (auth %s)", args.host, args.port,
             "on" if token else "off")
    uvicorn.run(_http_app(args.host, token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
