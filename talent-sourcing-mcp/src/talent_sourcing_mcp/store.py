"""Shortlists and a do-not-contact list, saved as JSON in the data directory."""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPORT_COLUMNS = [
    "name", "headline", "location", "company", "emails", "skills",
    "verification_score", "verification_level", "fit_score",
    "linkedin", "github", "stackoverflow", "website", "sources", "notes", "added_at",
]


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "shortlists.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"shortlists": {}, "do_not_contact": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- shortlists

    def add(self, shortlist: str, candidate: dict[str, Any], notes: str | None = None,
            fit_score: int | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            if self._blocked(data, candidate):
                raise ValueError(f"{candidate.get('name')} is on the do-not-contact list.")
            items = data["shortlists"].setdefault(shortlist, [])
            entry = {
                "id": candidate.get("id"),
                "name": candidate.get("name"),
                "headline": candidate.get("headline"),
                "location": candidate.get("location"),
                "company": candidate.get("company"),
                "emails": candidate.get("emails") or [],
                "skills": candidate.get("skills") or [],
                "links": candidate.get("links") or {},
                "sources": candidate.get("sources") or [candidate.get("source")],
                "verification": {
                    k: (candidate.get("verification") or {}).get(k) for k in ("score", "level")
                },
                "fit_score": fit_score,
                "notes": notes,
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            existing = next((i for i, e in enumerate(items) if e["id"] == entry["id"]), None)
            if existing is None:
                items.append(entry)
            else:
                items[existing] = {**items[existing], **{k: v for k, v in entry.items() if v is not None}}
            self._save(data)
            return {"shortlist": shortlist, "count": len(items), "saved": entry["name"]}

    def list(self, shortlist: str | None = None) -> dict[str, Any]:
        data = self._load()
        if shortlist is None:
            return {name: len(items) for name, items in data["shortlists"].items()}
        if shortlist not in data["shortlists"]:
            raise ValueError(f"No shortlist named '{shortlist}'.")
        return {"shortlist": shortlist, "candidates": data["shortlists"][shortlist]}

    def remove(self, shortlist: str, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            items = data["shortlists"].get(shortlist, [])
            before = len(items)
            data["shortlists"][shortlist] = [e for e in items if e["id"] != candidate_id]
            self._save(data)
            return {"removed": before - len(data["shortlists"][shortlist])}

    def export(self, shortlist: str, fmt: str = "csv") -> tuple[str, Path]:
        items = self.list(shortlist)["candidates"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in shortlist)
        self.dir.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            content = json.dumps(items, indent=2, ensure_ascii=False)
            path = self.dir / f"{safe}-{stamp}.json"
        elif fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
            w.writeheader()
            for e in items:
                links = e.get("links") or {}
                w.writerow({
                    "name": e.get("name"), "headline": e.get("headline"),
                    "location": e.get("location"), "company": e.get("company"),
                    "emails": "; ".join(e.get("emails") or []),
                    "skills": "; ".join(e.get("skills") or []),
                    "verification_score": (e.get("verification") or {}).get("score"),
                    "verification_level": (e.get("verification") or {}).get("level"),
                    "fit_score": e.get("fit_score"),
                    "linkedin": links.get("linkedin"), "github": links.get("github"),
                    "stackoverflow": links.get("stackoverflow"), "website": links.get("website"),
                    "sources": "; ".join(filter(None, e.get("sources") or [])),
                    "notes": e.get("notes"), "added_at": e.get("added_at"),
                })
            content = buf.getvalue()
            path = self.dir / f"{safe}-{stamp}.csv"
        else:
            raise ValueError("format must be 'csv' or 'json'.")
        path.write_text(content, encoding="utf-8")
        return content, path

    # ----------------------------------------------------------- do-not-contact

    @staticmethod
    def _keys(candidate: dict[str, Any]) -> set[str]:
        keys = {str(candidate.get("id") or "").lower()}
        keys |= {e.lower() for e in candidate.get("emails") or []}
        keys |= {str(v).lower().rstrip("/") for v in (candidate.get("links") or {}).values() if v}
        return {k for k in keys if k}

    def _blocked(self, data: dict[str, Any], candidate: dict[str, Any]) -> bool:
        blocked = {b["key"] for b in data.get("do_not_contact", [])}
        return bool(self._keys(candidate) & blocked)

    def block(self, key: str, reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            key = key.strip().lower().rstrip("/")
            if key not in {b["key"] for b in data["do_not_contact"]}:
                data["do_not_contact"].append({
                    "key": key, "reason": reason,
                    "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
            # Also drop them from every shortlist.
            for name, items in data["shortlists"].items():
                data["shortlists"][name] = [e for e in items if key not in self._keys(e)]
            self._save(data)
            return {"blocked": key, "total": len(data["do_not_contact"])}

    def is_blocked(self, candidate: dict[str, Any]) -> bool:
        return self._blocked(self._load(), candidate)
