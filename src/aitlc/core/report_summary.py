"""Parse the S3-stored daily HTML regression report into a compact, token-efficient structured summary.

Real, confirmed problem this solves: the raw HTML report this project uploads to S3
(helper/reporting/enhanced_test_report_generator.py) runs 3.7MB-32MB per day — no
smaller structured artifact (e.g. the underlying JSON) is uploaded alongside it. Feeding
that raw file to an agent is not viable; a single report can run into the tens of
millions of tokens. This module extracts just the summary counts and a failure list
(feature key, scenario name, truncated error) via regex over the report's consistent,
machine-generated structure — verified live against a real 32MB report, which reduced to
a few KB of structured JSON. It deliberately never touches the embedded base64
screenshots (the actual size driver) or the verbose per-step captured-logging blocks.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field


@dataclass
class FeatureSummary:
    """Pass/fail counts for one feature in a report."""

    key: str
    title: str
    status: str
    passed: int
    failed: int
    skipped: int
    duration: str


@dataclass
class ScenarioFailure:
    """One failing scenario and the step that failed."""

    feature_key: str
    feature_title: str
    scenario_name: str
    error: str


@dataclass
class ReportSummary:
    """A whole report reduced to counts and failures."""

    pass_rate: str
    total_features: int
    total_scenarios: int
    features_passed: int
    features_failed: int
    features_skipped: int
    scenarios_passed: int
    scenarios_failed: int
    scenarios_skipped: int
    features: list[FeatureSummary] = field(default_factory=list)
    failures: list[ScenarioFailure] = field(default_factory=list)

    def to_dict(self, max_failures: int | None = None) -> dict:
        """Return a JSON-serializable form, optionally truncated."""
        failures = self.failures
        truncated = 0
        if max_failures is not None and len(failures) > max_failures:
            truncated = len(failures) - max_failures
            failures = failures[:max_failures]
        return {
            "pass_rate": self.pass_rate,
            "total_features": self.total_features,
            "total_scenarios": self.total_scenarios,
            "features": {
                "passed": self.features_passed,
                "failed": self.features_failed,
                "skipped": self.features_skipped,
            },
            "scenarios": {
                "passed": self.scenarios_passed,
                "failed": self.scenarios_failed,
                "skipped": self.scenarios_skipped,
            },
            "feature_summaries": [
                {
                    "key": f.key,
                    "title": f.title,
                    "status": f.status,
                    "passed": f.passed,
                    "failed": f.failed,
                    "skipped": f.skipped,
                    "duration": f.duration,
                }
                for f in self.features
            ],
            "failures": [
                {
                    "feature_key": x.feature_key,
                    "feature_title": x.feature_title,
                    "scenario": x.scenario_name,
                    "error": x.error,
                }
                for x in failures
            ],
            "failures_truncated": truncated,
        }


_STATS_BLOCK_RE = re.compile(
    r'<div class="dashboard">(.*?)<div class="chart-container">', re.DOTALL
)
_STATS_VALUE_RE = re.compile(
    r'([A-Za-z ]+?):\s*<span class="stats-value">\s*([\d.]+)\s*%?\s*</span>'
)

_ROW_RE = re.compile(
    r'<tr class="(feature|scenario|error-row)"[^>]*>(.*?)</tr>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")

_FEATURE_KEY_RE = re.compile(r'class="execution-id">\[([^\]]+)\]')
_FEATURE_TITLE_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.DOTALL)
_STATUS_RE = re.compile(r'class="status"[^>]*>\s*(\w+)\s*<', re.DOTALL)
_STATS_ROW_RE = re.compile(r"P:\s*(\d+)\s*F:\s*(\d+)\s*S:\s*(\d+)")
_DURATION_RE = re.compile(r'class="duration"[^>]*>\s*([^<]+?)\s*<')
_SCENARIO_NAME_RE = re.compile(r'class="scenario-name">(.*?)</td>', re.DOTALL)
_ERROR_STEP_RE = re.compile(r'class="error-step"[^>]*>\s*<span>(.*?)</span>', re.DOTALL)
_ERROR_DETAILS_RE = re.compile(r'<pre class="error-details">(.*?)</pre>', re.DOTALL)


def _strip_tags(text: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", text)).strip()


def _extract_real_error(raw_pre_text: str) -> str:
    """Truncate a captured-logging tail down to the useful error text.

    Same truncation technique as core/behave_runner.py's
    _extract_error_message: the <pre> block's captured-logging tail can be
    huge (full GraphQL payloads, per-line INFO logs, real example seen:
    dozens of lines per failure) — keep only the real exception line.
    """
    text = (
        raw_pre_text.replace("<br>", "\n")
        .replace("<br/>", "\n")
        .replace("<br />", "\n")
    )
    text = html_lib.unescape(_TAG_RE.sub("", text))
    for marker in ("\nCaptured stdout:", "\nCaptured logging:"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def _extract_metric_card(stats_block: str, header: str) -> dict[str, str]:
    match = re.search(re.escape(header) + r"</h2>(.*?)</div>", stats_block, re.DOTALL)
    if not match:
        return {}
    return dict(_STATS_VALUE_RE.findall(match.group(1)))


def _as_int(value: str | None) -> int:
    if not value:
        return 0
    return int(float(value))


def parse_html_report(html_text: str) -> ReportSummary:
    """Parse an HTML report into a structured summary."""
    stats_match = _STATS_BLOCK_RE.search(html_text)
    stats_block = stats_match.group(1) if stats_match else ""

    overall = _extract_metric_card(stats_block, "Overall Statistics")
    feature_stats = _extract_metric_card(stats_block, "Feature Statistics")
    scenario_stats = _extract_metric_card(stats_block, "Scenario Statistics")

    summary = ReportSummary(
        pass_rate=f"{overall.get('Pass Rate', '0')}%",
        total_features=_as_int(overall.get("Total Features")),
        total_scenarios=_as_int(overall.get("Total Scenarios")),
        features_passed=_as_int(feature_stats.get("Passed")),
        features_failed=_as_int(feature_stats.get("Failed")),
        features_skipped=_as_int(feature_stats.get("Skipped")),
        scenarios_passed=_as_int(scenario_stats.get("Passed")),
        scenarios_failed=_as_int(scenario_stats.get("Failed")),
        scenarios_skipped=_as_int(scenario_stats.get("Skipped")),
    )

    current_feature_key = ""
    current_feature_title = ""
    current_scenario_name = ""

    for row_type, row_html in _ROW_RE.findall(html_text):
        if row_type == "feature":
            key_m = _FEATURE_KEY_RE.search(row_html)
            title_m = _FEATURE_TITLE_RE.search(row_html)
            status_m = _STATUS_RE.search(row_html)
            stats_m = _STATS_ROW_RE.search(row_html)
            duration_m = _DURATION_RE.search(row_html)
            current_feature_key = key_m.group(1) if key_m else ""
            current_feature_title = _strip_tags(title_m.group(1)) if title_m else ""
            summary.features.append(
                FeatureSummary(
                    key=current_feature_key,
                    title=current_feature_title,
                    status=status_m.group(1) if status_m else "",
                    passed=int(stats_m.group(1)) if stats_m else 0,
                    failed=int(stats_m.group(2)) if stats_m else 0,
                    skipped=int(stats_m.group(3)) if stats_m else 0,
                    duration=duration_m.group(1).strip() if duration_m else "",
                )
            )
        elif row_type == "scenario":
            name_m = _SCENARIO_NAME_RE.search(row_html)
            current_scenario_name = _strip_tags(name_m.group(1)) if name_m else ""
        elif row_type == "error-row":
            step_m = _ERROR_STEP_RE.search(row_html)
            details_m = _ERROR_DETAILS_RE.search(row_html)
            step_text = _strip_tags(step_m.group(1)) if step_m else ""
            error_text = _extract_real_error(details_m.group(1)) if details_m else ""
            combined = f"{step_text}: {error_text}" if step_text else error_text
            summary.failures.append(
                ScenarioFailure(
                    feature_key=current_feature_key,
                    feature_title=current_feature_title,
                    scenario_name=current_scenario_name,
                    error=combined,
                )
            )

    return summary
