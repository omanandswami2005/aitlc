"""Tests for the S3 HTML report -> compact JSON summary parser.

The fixture below mirrors the real report's structure (confirmed live
against a real 32MB report from enhanced_test_report_generator.py:
dashboard/metric-card stats, feature/scenario/error-row tables, pre.error-
details with <br> tags and a Captured logging: tail) — small enough to be
a real, maintainable test fixture rather than checked-in real report data.
"""

from aitlc.core.report_summary import parse_html_report

FIXTURE_HTML = """
<html><body>
<div class="dashboard">
    <div class="metric-card">
       <h2>Overall Statistics</h2>
       <p>Pass Rate: <span class="stats-value">50.0%</span></p>
       <p>Total Features: <span class="stats-value">2</span></p>
       <p>Total Scenarios: <span class="stats-value">2</span></p>
    </div>
    <div class="metric-card">
       <h2>Feature Statistics</h2>
       <p>Passed: <span class="stats-value">1</span></p>
       <p>Failed: <span class="stats-value">1</span></p>
       <p>Skipped: <span class="stats-value">0</span></p>
    </div>
    <div class="metric-card">
       <h2>Scenario Statistics</h2>
       <p>Passed: <span class="stats-value">1</span></p>
       <p>Failed: <span class="stats-value">1</span></p>
       <p>Skipped: <span class="stats-value">0</span></p>
    </div>
</div>
<div class="chart-container"></div>
<table><tbody>
<tr class="feature">
    <td class="feature-name">
        <span class="toggle-icon">&#9658;</span>
        <span class="execution-id">[PROJ-1]</span>
        <a href="https://example.atlassian.net/browse/PROJ-1" target="_blank">Feature One</a>
        <span class="scenario-count">(1 scenarios)</span>
    </td>
    <td class="status" style="background: #ffaaaa">FAILED</td>
    <td class="stats">P: 0 F: 1 S: 0</td>
    <td class="duration">1m 2.00s</td>
</tr>
<tr class="scenario" data-feature="Feature One" style="display: none">
    <td class="scenario-name"><span class="indent"></span>Validate the thing works</td>
    <td class="status" style="color: #dc3545; font-weight: bold">FAILED</td>
    <td class="stats">P: 3 F: 1 S: 0</td>
    <td class="duration">45.10s</td>
</tr>
<tr class="error-row" data-feature="Feature One" style="display: none">
    <td colspan="4" class="error-message">
    <div class="error-step" style="display: flex;">
    <span>When click on submit button</span>
    <div class="dropdown">
        <button class="dropdown-btn"><span class="material-symbols-rounded">visibility</span></button>
        <div class="dropdown-content">
            <a href="#" onclick="openScreenshotModal('iVBORw0KGgoAAAANSUhEUgFAKESCREENSHOTDATA')">View</a>
        </div>
    </div>
    </div>
    <div class="error-data-table-container"></div>
    <pre class="error-details">Assertion Failed: Locator expected to be visible<br>Error: element(s) not found<br>Call log:<br>  - waiting for locator("#submit")<br><br>Captured logging:<br>2026-08-13 05:47:17 INFO :: some very long noisy log line one<br>2026-08-13 05:47:18 INFO :: some very long noisy log line two<br></pre>
    </td>
</tr>
<tr class="feature">
    <td class="feature-name">
        <span class="toggle-icon">&#9658;</span>
        <span class="execution-id">[PROJ-2]</span>
        <a href="https://example.atlassian.net/browse/PROJ-2" target="_blank">Feature Two</a>
        <span class="scenario-count">(1 scenarios)</span>
    </td>
    <td class="status" style="background: #aaffaa">PASSED</td>
    <td class="stats">P: 5 F: 0 S: 0</td>
    <td class="duration">2m 0.00s</td>
</tr>
<tr class="scenario" data-feature="Feature Two" style="display: none">
    <td class="scenario-name"><span class="indent"></span>Validate the other thing works</td>
    <td class="status" style="color: #28a745; font-weight: bold">PASSED</td>
    <td class="stats">P: 5 F: 0 S: 0</td>
    <td class="duration">2m 0.00s</td>
</tr>
</tbody></table>
</body></html>
"""


def test_parses_overall_stats():
    summary = parse_html_report(FIXTURE_HTML)
    assert summary.pass_rate == "50.0%"
    assert summary.total_features == 2
    assert summary.total_scenarios == 2


def test_feature_and_scenario_stats_are_not_confused():
    # Real risk this guards: both metric-cards use the SAME "Passed:"/
    # "Failed:"/"Skipped:" labels — a naive whole-block regex would let
    # the second card silently overwrite the first.
    summary = parse_html_report(FIXTURE_HTML)
    assert summary.features_passed == 1
    assert summary.features_failed == 1
    assert summary.scenarios_passed == 1
    assert summary.scenarios_failed == 1


def test_parses_feature_summaries():
    summary = parse_html_report(FIXTURE_HTML)
    assert len(summary.features) == 2
    f1 = summary.features[0]
    assert f1.key == "PROJ-1"
    assert f1.title == "Feature One"
    assert f1.status == "FAILED"
    assert f1.failed == 1
    assert f1.duration == "1m 2.00s"
    assert summary.features[1].status == "PASSED"


def test_parses_failure_with_scenario_and_step():
    summary = parse_html_report(FIXTURE_HTML)
    assert len(summary.failures) == 1
    failure = summary.failures[0]
    assert failure.feature_key == "PROJ-1"
    assert failure.scenario_name == "Validate the thing works"
    assert "When click on submit button" in failure.error


def test_error_truncates_captured_logging_tail():
    # Real bug this guards against: the raw <pre> block's noisy captured-
    # logging tail is much larger than the actual exception — must not
    # leak into the compact summary.
    summary = parse_html_report(FIXTURE_HTML)
    error = summary.failures[0].error
    assert "noisy log line" not in error
    assert "Captured logging" not in error


def test_screenshot_base64_never_appears_in_output():
    # The whole point: never surface the embedded screenshot data.
    summary = parse_html_report(FIXTURE_HTML)
    dump = str(summary.to_dict())
    assert "FAKESCREENSHOTDATA" not in dump


def test_to_dict_caps_failures_and_reports_truncated_count():
    summary = parse_html_report(FIXTURE_HTML)
    payload = summary.to_dict(max_failures=0)
    assert payload["failures"] == []
    assert payload["failures_truncated"] == 1


def test_to_dict_no_cap_by_default():
    summary = parse_html_report(FIXTURE_HTML)
    payload = summary.to_dict()
    assert len(payload["failures"]) == 1
    assert payload["failures_truncated"] == 0


def test_empty_html_returns_zeroed_summary_not_a_crash():
    summary = parse_html_report("<html><body>nothing here</body></html>")
    assert summary.total_features == 0
    assert summary.total_scenarios == 0
    assert summary.features == []
    assert summary.failures == []
