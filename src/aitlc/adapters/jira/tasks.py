"""Plain Jira board Task creation (FR-7).

Distinct from everything the xray adapter does — Xray's GraphQL schema
(`createTest`) only creates Test issues. This is the everyday work-tracking
kind, going through Jira's own REST API via the `jira` package.

The `jira` package is commonly already installed in suites that talk to Xray,
often only for its type helpers; this uses the part of it that actually
creates issues.
"""

from __future__ import annotations

from dataclasses import dataclass

from jira import JIRA


class JiraTaskError(RuntimeError):
    """Raised when Jira rejects a task create/read request."""


@dataclass
class CreatedTask:
    """A Jira issue that was just created, and where to find it."""

    key: str
    url: str


def create_task(
    server_url: str,
    email: str,
    api_token: str,
    project_key: str,
    summary: str,
    description: str = "",
) -> CreatedTask:
    """Create a Jira Task assigned to the authenticating user.

    Create a plain Task, always assigned to whoever's credentials
    authenticate this call (FR-7.2 — a hard requirement, not a default:
    no --assignee flag exists anywhere in the CLI surface on top of this,
    on purpose).
    """
    try:
        client = JIRA(server=server_url, basic_auth=(email, api_token))
        issue = client.create_issue(
            project=project_key,
            summary=summary,
            description=description,
            issuetype={"name": "Task"},
        )
        # Jira Cloud's REST API creates unassigned by default even with
        # basic auth as the acting user in some project configurations —
        # explicitly assign to self to make FR-7.2 a guarantee, not a
        # side effect of whichever default the project happens to have.
        client.assign_issue(issue, client.current_user())
    except Exception as exc:
        raise JiraTaskError(str(exc)) from exc

    return CreatedTask(
        key=issue.key, url=f"{server_url.rstrip('/')}/browse/{issue.key}"
    )
