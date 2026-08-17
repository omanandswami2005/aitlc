"""Xray Cloud GraphQL client (FR-3).

GraphQL calls here match the shapes verified live this project: `getTests`
for reads, `updateGherkinTestDefinition` for writes. Introspection is used
(via `introspect_mutation`) to confirm a mutation's shape before relying on
it — never hardcode an assumed, undocumented schema (SRS FR-3.5).
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests
from aitlc.adapters.xray.gherkin_normalize import (
    diff_lines,
    normalize_gherkin_body,
    normalize_local_feature,
)


class XrayError(RuntimeError):
    """Raised on a GraphQL error response or an unexpected shape."""


@dataclass
class XrayTest:
    """One Xray Test: its issue identifiers and its Gherkin body."""

    issue_id: str
    key: str
    gherkin: str


@dataclass
class CompareResult:
    """Result of comparing a local feature file against live Xray."""

    key: str
    in_sync: bool
    diff: list[str]


@dataclass
class TestExecutionRef:
    """A reference to an Xray Test Execution issue."""

    issue_id: str
    key: str


@dataclass
class TestRunResult:
    """The recorded outcome of one test inside a Test Execution."""

    id: str
    test_key: str | None
    execution_key: str | None
    status: str | None


def authenticate(client_id: str, client_secret: str, timeout: int = 30) -> str:
    """Exchange Xray API client_id/client_secret for a bearer token.

    Xray Cloud's documented client-credentials flow: POST to /authenticate
    with the client id/secret, get back a JSON-encoded token string. This
    is a separate step from the GraphQL endpoint itself — the client_secret
    is not usable as a bearer token directly.
    """
    response = requests.post(
        "https://xray.cloud.getxray.app/api/v2/authenticate",
        json={"client_id": client_id, "client_secret": client_secret},
        timeout=timeout,
    )
    response.raise_for_status()
    # Xray returns the token as a bare JSON string, e.g. `"eyJhbGciOi..."`.
    return response.json()


class XrayClient:
    """Minimal Xray Cloud GraphQL client for Gherkin and execution data."""

    def __init__(self, graphql_url: str, token: str, timeout: int = 30):
        """Store the endpoint, bearer token and per-request timeout."""
        self._url = graphql_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout

    @classmethod
    def from_client_credentials(
        cls, graphql_url: str, client_id: str, client_secret: str, timeout: int = 30
    ) -> XrayClient:
        """Build a client by first exchanging client_id/client_secret for a token."""
        token = authenticate(client_id, client_secret, timeout=timeout)
        return cls(graphql_url=graphql_url, token=token, timeout=timeout)

    def _post(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = requests.post(
            self._url,
            json={"query": query, "variables": variables or {}},
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise XrayError(f"Xray GraphQL error: {payload['errors']}")
        return payload["data"]

    def introspect_mutation(self, name: str) -> dict[str, Any]:
        """Fetch a mutation's real arg/return shape via GraphQL introspection.

        SRS FR-3.5: never hardcode an assumed schema — this is how
        `get_gherkin`/`update_gherkin` were verified this project before
        being relied on.
        """
        query = """
        query IntrospectMutation {
          __type(name: "Mutation") { fields { name args { name type { name kind ofType { name kind } } } } }
        }
        """
        data = self._post(query)
        fields = data.get("__type", {}).get("fields", [])
        for field in fields:
            if field.get("name") == name:
                return field
        raise XrayError(f"No mutation named '{name}' found via introspection")

    def get_gherkin(self, key: str) -> XrayTest:
        """Fetch a Test's live Gherkin from Xray.

        Fetch a Test's live Gherkin (FR-3.1). Always a fresh fetch — never
        cache across calls, since Xray is the live source of truth and can
        have changed since a prior fetch in the same session (a real bug
        this project hit: a stale in-session copy would have masked drift).
        """
        query = """
        query GetTest($jql: String!) {
          getTests(jql: $jql, limit: 1) {
            results { issueId gherkin jira(fields: ["key"]) }
          }
        }
        """
        data = self._post(query, {"jql": f"key = {key}"})
        results = data.get("getTests", {}).get("results", [])
        if not results:
            raise XrayError(f"No Xray Test found for key '{key}'")
        result = results[0]
        return XrayTest(
            issue_id=result["issueId"],
            key=key,
            gherkin=result["gherkin"],
        )

    def update_gherkin(
        self, key: str, new_gherkin: str, *, verify: bool = True
    ) -> XrayTest:
        """Write a Test's Gherkin (FR-3.2).

        Re-fetches immediately before mutating (fresh issue_id, not reused
        from an earlier call), and re-fetches after to confirm the write
        actually persisted — the mutation response echoing back what was
        sent is not proof of a real write (verified pattern from this
        project's real usage).

        The body is normalized the same way ``compare_gherkin`` normalizes,
        because Xray stores only the step body. Without this, passing the
        `.feature` file you just compared writes its tags and ``Feature:``
        line into the Test and leaves it invalid — a real corruption of a
        shared Test, recovered by hand.
        """
        new_gherkin = normalize_gherkin_body(new_gherkin)
        current = self.get_gherkin(key)

        mutation = """
        mutation UpdateGherkin($issueId: String!, $gherkin: String!) {
          updateGherkinTestDefinition(issueId: $issueId, gherkin: $gherkin) {
            issueId
            gherkin
          }
        }
        """
        self._post(mutation, {"issueId": current.issue_id, "gherkin": new_gherkin})

        if not verify:
            return XrayTest(issue_id=current.issue_id, key=key, gherkin=new_gherkin)

        fresh = self.get_gherkin(key)
        if fresh.gherkin != new_gherkin:
            # Deliberately explicit that the Test WAS modified: the previous
            # wording ("write did not persist as sent") reads as "nothing
            # happened", and someone acting on that walks away from a Test
            # they have just changed.
            raise XrayError(
                f"update_gherkin for {key}: the Test WAS written, but the "
                "re-fetched Gherkin differs from what was sent — Xray may "
                "have reshaped it. Compare before writing again.\n"
                f"--- sent ---\n{new_gherkin}\n--- live now ---\n{fresh.gherkin}"
            )
        return fresh

    def compare_gherkin(self, key: str, local_feature_text: str) -> CompareResult:
        """Compare a local .feature file's body against live Xray (FR-3.3)."""
        live = self.get_gherkin(key)
        normalized_local = normalize_local_feature(local_feature_text)
        diff = diff_lines(normalized_local, live.gherkin)
        return CompareResult(key=key, in_sync=not diff, diff=diff)

    def create_test(self, project_key: str, summary: str, gherkin: str) -> XrayTest:
        """Create a new Cucumber-type Test (FR-3.4)."""
        mutation = """
        mutation CreateTest($testType: UpdateTestTypeInput!, $gherkin: String!, $jira: JSON!) {
          createTest(testType: $testType, gherkinDefinition: $gherkin, jira: $jira) {
            test { issueId jira(fields: ["key"]) }
          }
        }
        """
        variables = {
            "testType": {"name": "Cucumber"},
            "gherkin": gherkin,
            "jira": {
                "fields": {
                    "summary": summary,
                    "project": {"key": project_key},
                }
            },
        }
        data = self._post(mutation, variables)
        test = data["createTest"]["test"]
        return XrayTest(
            issue_id=test["issueId"],
            key=test["jira"]["key"],
            gherkin=gherkin,
        )

    def link_to_execution(self, test_key: str, execution_key: str) -> None:
        """Link a Test to a Test Execution (FR-3.4).

        NOTE: unlike get_gherkin/update_gherkin/compare_gherkin (verified
        live this project), this mutation's exact shape
        (`addTestsToTestExecution`) is inferred from Xray's naming
        convention, not confirmed via introspection yet. Per SRS FR-3.5,
        run `introspect_mutation` (below) against a real Xray instance
        before trusting this in production — do not ship this call
        unverified.
        """
        mutation = """
        mutation AddToExecution($executionIssueId: String!, $testIssueIds: [String]!) {
          addTestsToTestExecution(
            issueId: $executionIssueId
            testIssueIds: $testIssueIds
          ) {
            addedTests
            warning
          }
        }
        """
        test = self.get_gherkin(test_key)
        execution = self.get_gherkin(execution_key)
        self._post(
            mutation,
            {
                "executionIssueId": execution.issue_id,
                "testIssueIds": [test.issue_id],
            },
        )

    def get_test_executions_for_test(self, key: str) -> list[TestExecutionRef]:
        """Return the Test Executions a Test belongs to.

        Test -> its Test Executions. Verified live via schema introspection
        (Test.testExecutions is real, confirmed field this project's Xray
        instance exposes) before being relied on here, per SRS FR-3.5.
        """
        query = """
        query ExecutionsForTest($jql: String!) {
          getTests(jql: $jql, limit: 1) {
            results {
              testExecutions(limit: 100) {
                results { issueId jira(fields: ["key"]) }
              }
            }
          }
        }
        """
        data = self._post(query, {"jql": f"key = {key}"})
        results = data.get("getTests", {}).get("results", [])
        if not results:
            raise XrayError(f"No Xray Test found for key '{key}'")
        executions = results[0].get("testExecutions", {}).get("results", [])
        return [
            TestExecutionRef(issue_id=e["issueId"], key=e["jira"]["key"])
            for e in executions
        ]

    def get_tests_for_execution(self, execution_key: str) -> list[XrayTest]:
        """Test Execution -> its Tests (the reverse direction)."""
        query = """
        query TestsForExecution($jql: String!) {
          getTestExecutions(jql: $jql, limit: 1) {
            results {
              tests(limit: 100) {
                results { issueId gherkin jira(fields: ["key"]) }
              }
            }
          }
        }
        """
        data = self._post(query, {"jql": f"key = {execution_key}"})
        results = data.get("getTestExecutions", {}).get("results", [])
        if not results:
            raise XrayError(f"No Xray Test Execution found for key '{execution_key}'")
        tests = results[0].get("tests", {}).get("results", [])
        return [
            XrayTest(
                issue_id=t["issueId"],
                key=t["jira"]["key"],
                gherkin=t.get("gherkin", ""),
            )
            for t in tests
        ]

    def get_test_runs_for_execution(self, execution_key: str) -> list[TestRunResult]:
        """Return a Test Execution's runs with their pass/fail status.

        Test Execution -> its Test Runs, with actual pass/fail status —
        the leaf of the report -> run -> execution -> test hierarchy.
        """
        query = """
        query RunsForExecution($jql: String!) {
          getTestExecutions(jql: $jql, limit: 1) {
            results {
              testRuns(limit: 100) {
                results {
                  id
                  status { name }
                  test { jira(fields: ["key"]) }
                  testExecution { jira(fields: ["key"]) }
                }
              }
            }
          }
        }
        """
        data = self._post(query, {"jql": f"key = {execution_key}"})
        results = data.get("getTestExecutions", {}).get("results", [])
        if not results:
            raise XrayError(f"No Xray Test Execution found for key '{execution_key}'")
        runs = results[0].get("testRuns", {}).get("results", [])
        parsed = []
        for r in runs:
            test_ref = r.get("test") or {}
            exec_ref = r.get("testExecution") or {}
            parsed.append(
                TestRunResult(
                    id=r["id"],
                    test_key=(test_ref.get("jira") or {}).get("key"),
                    execution_key=(exec_ref.get("jira") or {}).get("key"),
                    status=(r.get("status") or {}).get("name"),
                )
            )
        return parsed

    def find_step_usage(
        self,
        jql: str,
        step_text: str,
        *,
        page_size: int = 100,
        max_workers: int = 10,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Find every Test whose Gherkin contains a given step.

        Which Tests contain step_text in their Gherkin — the real,
        verified answer to "where is this step used" (Xray's own
        BDD Step Library UI has no public API; confirmed via live
        introspection + official docs — this is the best available option).

        Concurrent pagination, not a single big fetch: verified live this
        session — 13.2s for a complete, correct search of 3598 real tests
        (19/19 matches, cross-checked against a sequential partial scan),
        vs 90s+ sequential. Jira's own `text ~` JQL operator was tested and
        ruled out first — it does not index the Gherkin field at all (0
        results for a test confirmed to contain the exact text).
        """
        first_query = """
        query FindStepUsagePage($jql: String!, $limit: Int!, $start: Int!) {
          getTests(jql: $jql, limit: $limit, start: $start) {
            total
            results { gherkin jira(fields: ["key"]) }
          }
        }
        """

        def fetch_page(start: int) -> dict[str, Any]:
            return self._post(
                first_query, {"jql": jql, "limit": page_size, "start": start}
            )["getTests"]

        first_page = fetch_page(0)
        total = first_page["total"]
        matches = [
            t["jira"]["key"]
            for t in first_page["results"]
            if step_text in (t.get("gherkin") or "")
        ]
        fetched = len(first_page["results"])
        if progress_callback:
            progress_callback(fetched, total)

        remaining_starts = list(range(page_size, total, page_size))
        if remaining_starts:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(fetch_page, s): s for s in remaining_starts}
                for future in as_completed(futures):
                    page = future.result()
                    for t in page["results"]:
                        if step_text in (t.get("gherkin") or ""):
                            matches.append(t["jira"]["key"])
                    fetched += len(page["results"])
                    if progress_callback:
                        progress_callback(fetched, total)

        return sorted(set(matches))
