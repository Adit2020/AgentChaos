"""Fictional, deterministic source data and JSON-returning research tools."""

import json
import re

PROJECTS = {
    "Project Atlas": {
        "project_name": "Project Atlas",
        "launch_date": "2026-11-18",
        "owner": "Maya Chen",
        "status": "approved",
        "summary": "A field operations planning workspace.",
    },
    "Project Borealis": {
        "project_name": "Project Borealis",
        "launch_date": "2027-02-03",
        "owner": "Noah Patel",
        "status": "planning",
        "summary": "A regional inventory dashboard.",
    },
    "Project Cedar": {
        "project_name": "Project Cedar",
        "launch_date": "2026-12-09",
        "owner": "Lena Ortiz",
        "status": "in review",
        "summary": "An internal training library.",
    },
}

DOCUMENTS = {
    "atlas-brief": {
        "document_id": "atlas-brief",
        "project_name": "Project Atlas",
        "title": "Project Atlas brief",
        "summary": "Atlas ownership and approved launch plan.",
        "content": (
            "Project Atlas is owned by Maya Chen. "
            "Its launch is approved for 2026-11-18."
        ),
    },
    "atlas-release": {
        "document_id": "atlas-release",
        "project_name": "Project Atlas",
        "title": "Project Atlas release decision",
        "summary": "Approved Atlas status and launch date.",
        "content": (
            "Project Atlas status: approved. Launch date: 2026-11-18. Owner: Maya Chen."
        ),
    },
    "borealis-brief": {
        "document_id": "borealis-brief",
        "project_name": "Project Borealis",
        "title": "Project Borealis brief",
        "summary": "Borealis planning schedule and owner.",
        "content": (
            "Project Borealis is in planning. Noah Patel owns it. "
            "Target launch: 2027-02-03."
        ),
    },
    "cedar-brief": {
        "document_id": "cedar-brief",
        "project_name": "Project Cedar",
        "title": "Project Cedar brief",
        "summary": "Cedar review schedule and owner.",
        "content": (
            "Project Cedar is in review. Lena Ortiz owns it. Target launch: 2026-12-09."
        ),
    },
}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def search_documents(query: str) -> str:
    """Return JSON IDs and summaries for a named project or matching terms."""

    normalized = query.casefold()
    words = re.findall(r"[a-z0-9]+", normalized)
    matches = [
        {
            "document_id": document["document_id"],
            "project_name": document["project_name"],
            "summary": document["summary"],
        }
        for document in DOCUMENTS.values()
        if document["project_name"].casefold() in normalized
        or (
            words
            and all(word in " ".join(document.values()).casefold() for word in words)
        )
    ]
    return _json({"query": query, "matches": matches})


def get_document(document_id: str) -> str:
    """Return JSON with one full local document, or found=false."""

    document = DOCUMENTS.get(document_id)
    return _json({"found": document is not None, "document": document})


def get_project_details(project_name: str) -> str:
    """Return JSON with independent structured project facts, or found=false."""

    project = PROJECTS.get(project_name)
    return _json({"found": project is not None, "project": project})


def expected_facts(project_name: str) -> dict[str, str]:
    """Return the local oracle facts used for deterministic grading."""

    project = PROJECTS[project_name]
    return {key: project[key] for key in ("launch_date", "owner", "status")}
