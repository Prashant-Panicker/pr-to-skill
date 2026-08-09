"""Validation helpers for application-owned configuration."""


def trusted_reviewers(config: dict) -> list[str]:
    person = config.get("person", {})
    configured = person.get("github_usernames", person.get("github_username"))
    values = [configured] if isinstance(configured, str) else configured
    if not isinstance(values, list):
        raise ValueError("person.github_usernames must be a list of GitHub logins")
    reviewers = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Trusted GitHub usernames must be non-empty strings")
        normalized = value.strip()
        if normalized.lower() not in seen:
            seen.add(normalized.lower())
            reviewers.append(normalized)
    if not reviewers:
        raise ValueError("At least one trusted GitHub username is required")
    return reviewers