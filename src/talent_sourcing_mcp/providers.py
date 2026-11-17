"""Clients for each talent data source.

Every client returns plain dicts in a shared "candidate" shape so the
verification layer can compare people across sources:

    {
      "id": "github:octocat",
      "source": "github",
      "name": "...", "headline": "...", "location": "...", "company": "...",
      "emails": [...], "skills": [...],
      "links": {"github": "...", "linkedin": "...", "website": "..."},
      "signals": {...source-specific evidence used for scoring...},
    }
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings

USER_AGENT = "talent-sourcing-mcp/0.1 (+https://modelcontextprotocol.io)"


class ProviderError(RuntimeError):
    """A data source failed or is not configured. The message is safe to show the user."""


# ------------------------------------------------------------------------ regions
# People rarely write "Europe" or "LATAM" in a profile, so a region is expanded into
# the countries it covers (for People Data Labs and location matching) and into the
# biggest tech hubs (for GitHub, whose location search matches one place at a time).

_EU = [
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czech republic", "czechia",
    "denmark", "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
    "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands", "poland",
    "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
]
_EUROPE = _EU + [
    "albania", "andorra", "belarus", "bosnia and herzegovina", "iceland", "kosovo",
    "liechtenstein", "moldova", "monaco", "montenegro", "north macedonia", "norway",
    "san marino", "serbia", "switzerland", "ukraine", "united kingdom",
]
_LATAM = [
    "argentina", "bolivia", "brazil", "chile", "colombia", "costa rica", "cuba",
    "dominican republic", "ecuador", "el salvador", "guatemala", "honduras", "mexico",
    "nicaragua", "panama", "paraguay", "peru", "puerto rico", "uruguay", "venezuela",
]
_AFRICA = [
    "algeria", "angola", "benin", "botswana", "burkina faso", "burundi", "cameroon",
    "cape verde", "central african republic", "chad", "comoros", "djibouti", "egypt",
    "democratic republic of the congo", "republic of the congo", "equatorial guinea",
    "eritrea", "eswatini", "ethiopia", "gabon", "gambia", "ghana", "guinea",
    "guinea-bissau", "ivory coast", "côte d'ivoire", "kenya", "lesotho", "liberia",
    "libya", "madagascar", "malawi", "mali", "mauritania", "mauritius", "morocco",
    "mozambique", "namibia", "niger", "nigeria", "rwanda", "sao tome and principe",
    "senegal", "seychelles", "sierra leone", "somalia", "south africa", "south sudan",
    "sudan", "tanzania", "togo", "tunisia", "uganda", "zambia", "zimbabwe",
]
_US_HUBS = ["San Francisco", "New York", "Seattle", "Austin", "Boston", "Los Angeles", "Chicago"]
_US_ALIASES = ["usa", "united states", "us", "america", "california", "texas", "new york",
               "washington", "massachusetts", "illinois", "florida", "georgia", "colorado",
               "ca", "ny", "tx", "wa", "ma", "il", "fl"]

REGIONS: dict[str, dict[str, list[str]]] = {
    "europe": {
        "countries": _EUROPE,
        "github_hubs": ["Berlin", "London", "Amsterdam", "Warsaw", "Lisbon", "Madrid",
                        "Paris", "Kyiv", "Bucharest", "Stockholm"],
        "aliases": ["uk", "england", "scotland", "wales", "deutschland", "london", "berlin",
                    "amsterdam", "paris", "madrid", "lisbon", "warsaw", "munich", "dublin"],
    },
    "eu": {
        "countries": _EU,
        "github_hubs": ["Berlin", "Amsterdam", "Warsaw", "Lisbon", "Madrid", "Paris",
                        "Munich", "Bucharest", "Stockholm", "Prague"],
        "aliases": ["deutschland", "berlin", "amsterdam", "paris", "madrid", "lisbon",
                    "warsaw", "munich", "dublin", "prague"],
    },
    "latam": {
        "countries": _LATAM,
        "github_hubs": ["Brazil", "Mexico", "Argentina", "Colombia", "Chile", "Peru", "Uruguay"],
        "aliases": ["brasil", "méxico", "são paulo", "sao paulo", "buenos aires", "bogotá",
                    "bogota", "santiago", "lima", "montevideo", "cdmx"],
    },
    "africa": {
        "countries": _AFRICA,
        "github_hubs": ["Nigeria", "Kenya", "South Africa", "Egypt", "Ghana", "Morocco", "Rwanda"],
        "aliases": ["lagos", "abuja", "nairobi", "cape town", "johannesburg", "cairo",
                    "accra", "kigali", "casablanca"],
    },
    "north america": {
        "countries": ["united states", "canada"],
        "github_hubs": _US_HUBS + ["Toronto", "Vancouver"],
        "aliases": _US_ALIASES + ["toronto", "vancouver", "montreal", "ontario", "canada"],
    },
    "us": {
        "countries": ["united states"],
        "github_hubs": _US_HUBS,
        "aliases": _US_ALIASES,
    },
}
_REGION_NAMES = {
    "europe": "europe", "european": "europe",
    "eu": "eu", "european union": "eu",
    "latam": "latam", "latin america": "latam", "south america": "latam",
    "central america": "latam",
    "africa": "africa", "african": "africa",
    "north america": "north america", "na": "north america",
    "us": "us", "usa": "us", "united states": "us", "united states of america": "us",
    "america": "us",
}


def resolve_region(location: str | None) -> str | None:
    """Return the region key if `location` names a region (e.g. 'Europe', 'LATAM')."""
    if not location:
        return None
    return _REGION_NAMES.get(location.strip().lower().rstrip("."))


def region_matches(candidate_location: str | None, region: str) -> bool:
    """True if a free-text location (e.g. 'Berlin, Germany') falls inside the region."""
    if not candidate_location:
        return False
    text = " " + " ".join(candidate_location.lower().replace(",", " ").split()) + " "
    r = REGIONS[region]
    for term in r["countries"] + [h.lower() for h in r["github_hubs"]] + r["aliases"]:
        if f" {term} " in text:
            return True
    return False


def _days_since(iso_or_epoch: str | int | float | None) -> int | None:
    if iso_or_epoch in (None, ""):
        return None
    if isinstance(iso_or_epoch, (int, float)):
        dt = datetime.fromtimestamp(iso_or_epoch, tz=timezone.utc)
    else:
        text = str(iso_or_epoch).replace("Z", "+00:00")
        if len(text) == 7:  # "YYYY-MM"
            text += "-01"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _raise_for(resp: httpx.Response, source: str) -> None:
    if resp.status_code < 400:
        return
    if resp.status_code in (401, 403) and "rate limit" in resp.text.lower():
        raise ProviderError(f"{source}: rate limit reached. Add an API key or try again later.")
    if resp.status_code == 429:
        raise ProviderError(f"{source}: too many requests. Try again shortly.")
    if resp.status_code in (401, 403):
        raise ProviderError(f"{source}: the API key was rejected or lacks access.")
    if resp.status_code == 404:
        raise ProviderError(f"{source}: not found.")
    raise ProviderError(f"{source}: request failed with HTTP {resp.status_code}.")


class BaseClient:
    source = "base"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client

    async def _get(self, url: str, **kwargs: Any) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        try:
            headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
            try:
                resp = await client.get(url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.source}: could not reach the API ({exc.__class__.__name__}).") from exc
            _raise_for(resp, self.source)
            return resp.json()
        finally:
            if self._client is None:
                await client.aclose()

    async def _post(self, url: str, **kwargs: Any) -> Any:
        client = self._client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        try:
            headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
            try:
                resp = await client.post(url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.source}: could not reach the API ({exc.__class__.__name__}).") from exc
            _raise_for(resp, self.source)
            return resp.json()
        finally:
            if self._client is None:
                await client.aclose()


# --------------------------------------------------------------------------- GitHub


class GitHubClient(BaseClient):
    source = "github"
    base = "https://api.github.com"

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github_token:
            h["Authorization"] = f"Bearer {self.settings.github_token}"
        return h

    @staticmethod
    def build_query(
        keywords: str | None = None,
        location: str | None = None,
        language: str | None = None,
        min_followers: int | None = None,
        min_repos: int | None = None,
    ) -> str:
        parts = ["type:user"]
        if keywords:
            parts.insert(0, keywords)
        if location:
            parts.append(f'location:"{location}"' if " " in location else f"location:{location}")
        if language:
            parts.append(f'language:"{language}"' if " " in language else f"language:{language}")
        if min_followers:
            parts.append(f"followers:>={min_followers}")
        if min_repos:
            parts.append(f"repos:>={min_repos}")
        return " ".join(parts)

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 30))
        data = await self._get(
            f"{self.base}/search/users",
            params={"q": query, "per_page": limit, "sort": "followers", "order": "desc"},
            headers=self._headers(),
        )
        logins = [item["login"] for item in data.get("items", [])][:limit]
        sem = asyncio.Semaphore(5)

        errors: list[ProviderError] = []

        async def one(login: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    return await self.profile(login)
                except ProviderError as exc:
                    errors.append(exc)
                    return None

        results = [r for r in await asyncio.gather(*(one(l) for l in logins)) if r]
        if logins and not results and errors:
            raise errors[0]  # every profile failed: report why instead of returning nothing
        return results

    async def profile(self, login: str) -> dict[str, Any]:
        user = await self._get(f"{self.base}/users/{login}", headers=self._headers())
        repos = await self._get(
            f"{self.base}/users/{login}/repos",
            params={"sort": "pushed", "per_page": 10, "type": "owner"},
            headers=self._headers(),
        )
        return self.normalize(user, repos)

    @staticmethod
    def normalize(user: dict[str, Any], repos: list[dict[str, Any]]) -> dict[str, Any]:
        own = [r for r in repos if not r.get("fork")]
        langs: dict[str, int] = {}
        for r in own:
            if r.get("language"):
                langs[r["language"]] = langs.get(r["language"], 0) + 1
        last_push = max((r.get("pushed_at") or "" for r in repos), default="") or None
        stars = sum(int(r.get("stargazers_count") or 0) for r in own)
        links = {"github": user.get("html_url")}
        if user.get("blog"):
            blog = user["blog"]
            links["website"] = blog if blog.startswith("http") else f"https://{blog}"
            if "linkedin.com/" in blog:
                links["linkedin"] = links["website"]
        if user.get("twitter_username"):
            links["x"] = f"https://x.com/{user['twitter_username']}"
        return {
            "id": f"github:{user.get('login')}",
            "source": "github",
            "name": user.get("name") or user.get("login"),
            "headline": user.get("bio"),
            "location": user.get("location"),
            "company": (user.get("company") or "").lstrip("@") or None,
            "emails": [user["email"]] if user.get("email") else [],
            "skills": sorted(langs, key=langs.get, reverse=True),
            "links": links,
            "signals": {
                "github_login": user.get("login"),
                "account_age_days": _days_since(user.get("created_at")),
                "last_active_days": _days_since(last_push),
                "followers": user.get("followers", 0),
                "public_repos": user.get("public_repos", 0),
                "original_repos_sampled": len(own),
                "stars_on_recent_repos": stars,
                "hireable": bool(user.get("hireable")),
            },
        }


# ------------------------------------------------------------------ Stack Overflow


class StackExchangeClient(BaseClient):
    source = "stackoverflow"
    base = "https://api.stackexchange.com/2.3"

    def _params(self, **extra: Any) -> dict[str, Any]:
        p = {"site": "stackoverflow", **extra}
        if self.settings.stackexchange_key:
            p["key"] = self.settings.stackexchange_key
        return p

    async def top_answerers(
        self, tag: str, location: str | None = None, min_reputation: int = 0, limit: int = 10
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        top = await self._get(
            f"{self.base}/tags/{tag}/top-answerers/all_time", params=self._params(pagesize=100)
        )
        tag_scores = {
            item["user"]["user_id"]: item.get("score", 0)
            for item in top.get("items", [])
            if item.get("user", {}).get("user_id")
        }
        if not tag_scores:
            return []
        users = await self.users(list(tag_scores))
        out = []
        for u in users:
            u["signals"]["tag_score"] = {tag: tag_scores.get(u["signals"]["stackoverflow_user_id"], 0)}
            if u["signals"]["reputation"] < min_reputation:
                continue
            region = resolve_region(location)
            if region and not region_matches(u.get("location"), region):
                continue
            if location and not region and location.lower() not in (u.get("location") or "").lower():
                continue
            out.append(u)
        out.sort(key=lambda c: -sum(c["signals"]["tag_score"].values()))
        return out[:limit]

    async def users(self, ids: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            chunk = ";".join(str(x) for x in ids[i : i + 100])
            data = await self._get(f"{self.base}/users/{chunk}", params=self._params(pagesize=100))
            out.extend(self.normalize(u) for u in data.get("items", []))
        return out

    @staticmethod
    def normalize(u: dict[str, Any]) -> dict[str, Any]:
        links = {"stackoverflow": u.get("link")}
        if u.get("website_url"):
            links["website"] = u["website_url"]
        badges = u.get("badge_counts") or {}
        return {
            "id": f"stackoverflow:{u.get('user_id')}",
            "source": "stackoverflow",
            "name": html.unescape(u.get("display_name") or ""),
            "headline": None,
            "location": html.unescape(u["location"]) if u.get("location") else None,
            "company": None,
            "emails": [],
            "skills": [],
            "links": links,
            "signals": {
                "stackoverflow_user_id": u.get("user_id"),
                "reputation": u.get("reputation", 0),
                "account_age_days": _days_since(u.get("creation_date")),
                "last_active_days": _days_since(u.get("last_access_date")),
                "gold_badges": badges.get("gold", 0),
                "silver_badges": badges.get("silver", 0),
            },
        }


# ------------------------------------------------------------- People Data Labs


class PeopleDataLabsClient(BaseClient):
    source = "peopledatalabs"
    base = "https://api.peopledatalabs.com/v5"

    def _require(self) -> dict[str, str]:
        if not self.settings.pdl_api_key:
            raise ProviderError("peopledatalabs: not configured. Set PDL_API_KEY to search any role or industry.")
        return {"X-Api-Key": self.settings.pdl_api_key}

    @staticmethod
    def build_query(
        job_title: str | None = None,
        location_country: str | None = None,
        location_locality: str | None = None,
        skills: list[str] | None = None,
        min_years_experience: int | None = None,
        industry: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        must: list[dict[str, Any]] = []
        if job_title:
            must.append({"match": {"job_title": job_title}})
        for loc in (location, location_country):
            region = resolve_region(loc)
            if region:
                must.append({"terms": {"location_country": REGIONS[region]["countries"]}})
                if loc == location:
                    location = None
                else:
                    location_country = None
        if location:
            must.append({"match": {"location_name": {"query": location.lower(), "operator": "and"}}})
        if location_country:
            must.append({"term": {"location_country": location_country.lower()}})
        if location_locality:
            must.append({"match": {"location_locality": location_locality.lower()}})
        if industry:
            must.append({"match": {"industry": industry.lower()}})
        for s in skills or []:
            must.append({"term": {"skills": s.lower()}})
        if min_years_experience:
            must.append({"range": {"inferred_years_experience": {"gte": min_years_experience}}})
        if not must:
            raise ProviderError("peopledatalabs: give at least one filter (title, location, skills, ...).")
        return {"query": {"bool": {"must": must}}}

    async def search(self, query: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        headers = self._require()
        body = {**query, "size": max(1, min(limit, 100)), "dataset": "resume", "titlecase": True}
        data = await self._post(f"{self.base}/person/search", json=body, headers=headers)
        return [self.normalize(p) for p in data.get("data", [])]

    async def enrich(self, **params: Any) -> dict[str, Any] | None:
        headers = self._require()
        params = {k: v for k, v in params.items() if v}
        if not params:
            raise ProviderError("peopledatalabs: give a profile URL, email, or name plus company.")
        try:
            data = await self._get(
                f"{self.base}/person/enrich", params={**params, "titlecase": "true"}, headers=headers
            )
        except ProviderError as exc:
            if "not found" in str(exc):
                return None
            raise
        return self.normalize(data.get("data") or {}, likelihood=data.get("likelihood"))

    @staticmethod
    def normalize(p: dict[str, Any], likelihood: int | None = None) -> dict[str, Any]:
        # On PDL's free plan some fields come back as True ("we have it") instead of a value.
        p = {k: (None if isinstance(v, bool) else v) for k, v in p.items()}
        links: dict[str, str] = {}
        if p.get("linkedin_url"):
            links["linkedin"] = p["linkedin_url"] if p["linkedin_url"].startswith("http") else f"https://{p['linkedin_url']}"
        if p.get("github_url"):
            links["github"] = p["github_url"] if p["github_url"].startswith("http") else f"https://{p['github_url']}"
        emails = []
        if isinstance(p.get("work_email"), str):
            emails.append(p["work_email"])
        updated = p.get("job_last_updated") or p.get("job_last_verified")
        return {
            "id": f"peopledatalabs:{p.get('id')}",
            "source": "peopledatalabs",
            "name": p.get("full_name"),
            "headline": " at ".join(x for x in [p.get("job_title"), p.get("job_company_name")] if x) or None,
            "location": p.get("location_name"),
            "company": p.get("job_company_name"),
            "company_domain": p.get("job_company_website"),
            "emails": emails,
            "skills": list(p.get("skills") or [])[:25],
            "links": links,
            "signals": {
                "years_experience": p.get("inferred_years_experience"),
                "job_start_date": p.get("job_start_date"),
                "profile_updated": updated,
                "last_active_days": _days_since(updated) if updated else None,
                "match_likelihood": likelihood,
            },
        }


# ------------------------------------------------------------------------ Hunter


class HunterClient(BaseClient):
    source = "hunter"
    base = "https://api.hunter.io/v2"

    def _key(self) -> str:
        if not self.settings.hunter_api_key:
            raise ProviderError("hunter: not configured. Set HUNTER_API_KEY to find and verify emails.")
        return self.settings.hunter_api_key

    async def find_email(self, domain: str, first_name: str, last_name: str) -> dict[str, Any]:
        data = await self._get(
            f"{self.base}/email-finder",
            params={"domain": domain, "first_name": first_name, "last_name": last_name, "api_key": self._key()},
        )
        d = data.get("data") or {}
        return {
            "email": d.get("email"),
            "confidence": d.get("score"),
            "position": d.get("position"),
            "linkedin": d.get("linkedin_url"),
            "verification_status": (d.get("verification") or {}).get("status"),
        }

    async def verify_email(self, email: str) -> dict[str, Any]:
        data = await self._get(f"{self.base}/email-verifier", params={"email": email, "api_key": self._key()})
        d = data.get("data") or {}
        return {
            "email": email,
            "status": d.get("status"),  # valid | invalid | accept_all | webmail | disposable | unknown
            "score": d.get("score"),
            "disposable": d.get("disposable"),
            "webmail": d.get("webmail"),
        }
