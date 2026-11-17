"""Merge a person's profiles from several sources and score how well they check out.

"Verified" here means *corroborated*: independent public sources agree on who the
person is, they are genuinely active, and (when checked) their email is deliverable.
It is evidence for a recruiter to weigh, not proof of identity or of employment
history. Every score comes with the checks behind it so a human can audit it.
"""

from __future__ import annotations

import re
from typing import Any

VERIFIED_THRESHOLD = 70
LIKELY_THRESHOLD = 40

_STOP = {"the", "of", "and", "state", "city", "area", "greater", "metropolitan", "remote"}


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOP}


def names_match(a: str | None, b: str | None) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    small = min(len(ta), len(tb))
    return len(ta & tb) >= min(2, small)


def locations_match(a: str | None, b: str | None) -> bool:
    return bool(_tokens(a) & _tokens(b))


def merge_profiles(profiles: list[dict[str, Any]], email_check: dict[str, Any] | None = None) -> dict[str, Any]:
    """Combine one person's per-source profiles into a single candidate record."""
    profiles = [p for p in profiles if p]
    if not profiles:
        raise ValueError("No profiles to merge.")
    first = profiles[0]
    merged: dict[str, Any] = {
        "id": first["id"],
        "name": next((p["name"] for p in profiles if p.get("name")), None),
        "headline": next((p["headline"] for p in profiles if p.get("headline")), None),
        "location": next((p["location"] for p in profiles if p.get("location")), None),
        "company": next((p["company"] for p in profiles if p.get("company")), None),
        "sources": [p["source"] for p in profiles],
        "emails": [],
        "skills": [],
        "links": {},
        "signals": {},
        "profiles": profiles,
    }
    for p in profiles:
        for e in p.get("emails", []):
            if e and e.lower() not in {x.lower() for x in merged["emails"]}:
                merged["emails"].append(e)
        for s in p.get("skills", []):
            if s and s.lower() not in {x.lower() for x in merged["skills"]}:
                merged["skills"].append(s)
        for k, v in (p.get("links") or {}).items():
            if v:
                merged["links"].setdefault(k, v)
        merged["signals"][p["source"]] = p.get("signals", {})
    if email_check:
        merged["email_check"] = email_check
    merged["verification"] = score_verification(merged)
    return merged


def score_verification(c: dict[str, Any]) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = c.get("profiles") or []
    sig: dict[str, dict[str, Any]] = c.get("signals") or {}
    checks: list[dict[str, Any]] = []

    def add(name: str, points: int, max_points: int, evidence: str) -> None:
        checks.append({"check": name, "points": points, "max": max_points, "evidence": evidence})

    # 1. Independent sources
    sources = sorted(set(c.get("sources") or []))
    n = len(sources)
    add("independent_sources", 20 if n >= 3 else 15 if n == 2 else 0, 20,
        f"Found on {n} source(s): {', '.join(sources) or 'none'}.")

    # 2. Name consistency across sources
    names = [p.get("name") for p in profiles if p.get("name")]
    if len(names) >= 2:
        ok = all(names_match(names[0], other) for other in names[1:])
        add("name_consistency", 15 if ok else 0, 15,
            ("Names agree: " if ok else "Names differ: ") + " / ".join(names))
    else:
        add("name_consistency", 0, 15, "Only one source, so the name can't be cross-checked.")

    # 3. Location consistency
    locs = [p.get("location") for p in profiles if p.get("location")]
    if len(locs) >= 2:
        ok = all(locations_match(locs[0], other) for other in locs[1:])
        add("location_consistency", 10 if ok else 0, 10,
            ("Locations agree: " if ok else "Locations differ: ") + " / ".join(locs))
    else:
        add("location_consistency", 0, 10, "Fewer than two sources list a location.")

    # 4. Recent activity
    actives = [s.get("last_active_days") for s in sig.values() if s.get("last_active_days") is not None]
    recent = min(actives) if actives else None
    if recent is None:
        add("recent_activity", 0, 15, "No activity dates available.")
    else:
        pts = 15 if recent <= 90 else 8 if recent <= 365 else 0
        add("recent_activity", pts, 15, f"Last public activity {recent} day(s) ago.")

    # 5. Account maturity (guards against freshly made fake profiles)
    ages = [s.get("account_age_days") for s in sig.values() if s.get("account_age_days") is not None]
    oldest = max(ages) if ages else None
    if oldest is None:
        add("account_maturity", 0, 10, "No account creation dates available.")
    else:
        pts = 10 if oldest >= 730 else 5 if oldest >= 365 else 0
        add("account_maturity", pts, 10, f"Oldest account is {oldest // 365} year(s) old.")

    # 6. Public track record
    gh, so, pdl = sig.get("github", {}), sig.get("stackoverflow", {}), sig.get("peopledatalabs", {})
    strong = (
        gh.get("followers", 0) >= 20 or gh.get("stars_on_recent_repos", 0) >= 20
        or so.get("reputation", 0) >= 1000 or (pdl.get("years_experience") or 0) >= 3
    )
    some = (
        gh.get("public_repos", 0) >= 5 or so.get("reputation", 0) >= 200
        or (pdl.get("years_experience") or 0) >= 1
    )
    evidence = []
    if gh:
        evidence.append(f"GitHub: {gh.get('followers', 0)} followers, {gh.get('public_repos', 0)} repos")
    if so:
        evidence.append(f"Stack Overflow: {so.get('reputation', 0)} reputation")
    if pdl.get("years_experience") is not None:
        evidence.append(f"~{pdl['years_experience']} years' experience on record")
    add("track_record", 10 if strong else 5 if some else 0, 10, "; ".join(evidence) or "No track-record data.")

    # 7. Contact verified
    ec = c.get("email_check") or {}
    status = ec.get("status")
    if status == "valid":
        add("contact_verified", 15, 15, f"{ec.get('email')} is deliverable.")
    elif status in ("accept_all", "webmail"):
        add("contact_verified", 5, 15, f"{ec.get('email')} status '{status}': can't fully confirm the mailbox.")
    elif status:
        add("contact_verified", 0, 15, f"{ec.get('email')} status '{status}'.")
    else:
        add("contact_verified", 0, 15, "Email not checked.")

    # 8. Professional profile
    has_li = bool((c.get("links") or {}).get("linkedin"))
    add("professional_profile", 5 if has_li else 0, 5,
        "LinkedIn profile linked." if has_li else "No LinkedIn profile linked.")

    score = sum(ch["points"] for ch in checks)
    level = "verified" if score >= VERIFIED_THRESHOLD else "likely_genuine" if score >= LIKELY_THRESHOLD else "unverified"
    return {
        "score": score,
        "level": level,
        "checks": checks,
        "how_to_raise": [ch["check"] for ch in checks if ch["points"] < ch["max"]],
    }


def score_fit(c: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    """Score a candidate against a role: skills (60), location (20), experience (20)."""
    want = [s.strip().lower() for s in requirements.get("skills") or [] if s.strip()]
    have = {s.lower() for s in c.get("skills") or []}
    text = " ".join(filter(None, [c.get("headline"), " ".join(c.get("skills") or [])])).lower()
    matched = [s for s in want if s in have or s in text]
    missing = [s for s in want if s not in matched]
    skill_pts = round(60 * len(matched) / len(want)) if want else 60

    loc_req = requirements.get("location")
    if not loc_req or str(loc_req).lower() in ("remote", "anywhere"):
        loc_pts, loc_note = 20, "No location requirement."
    elif locations_match(c.get("location"), loc_req):
        loc_pts, loc_note = 20, f"Located in {c.get('location')}."
    elif c.get("location"):
        loc_pts, loc_note = 0, f"Located in {c.get('location')}, not {loc_req}."
    else:
        loc_pts, loc_note = 10, "Location unknown."

    min_years = requirements.get("min_years_experience")
    years = (c.get("signals") or {}).get("peopledatalabs", {}).get("years_experience")
    if not min_years:
        yr_pts, yr_note = 20, "No experience requirement."
    elif years is None:
        yr_pts, yr_note = 10, "Years of experience unknown."
    elif years >= min_years:
        yr_pts, yr_note = 20, f"~{years} years (needs {min_years})."
    else:
        yr_pts, yr_note = round(20 * years / min_years), f"~{years} years (needs {min_years})."

    return {
        "score": skill_pts + loc_pts + yr_pts,
        "matched_skills": matched,
        "missing_skills": missing,
        "location": loc_note,
        "experience": yr_note,
    }
