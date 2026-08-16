from aitlc.core.blast_radius import changed_files_from_unified_diff, check

REAL_SHAPED_DIFF = """\
diff --git a/pages/rabbitmq/rabbitmq_page.py b/pages/rabbitmq/rabbitmq_page.py
index abc123..def456 100644
--- a/pages/rabbitmq/rabbitmq_page.py
+++ b/pages/rabbitmq/rabbitmq_page.py
@@ -10,7 +10,7 @@ def call_rmq_endpoint():
-    proxy = {"http": ..., "https": ...}
+    if lt_proxy_host and lt_proxy_port:
+        proxy = {"http": ..., "https": ...}
"""

DIFF_TOUCHING_STEPS = """\
diff --git a/features/steps/step_definition_common_page.py b/features/steps/step_definition_common_page.py
index 111..222 100644
--- a/features/steps/step_definition_common_page.py
+++ b/features/steps/step_definition_common_page.py
@@ -5,3 +5,3 @@
-old
+new
"""


def test_extracts_changed_file_from_real_shaped_diff():
    files = changed_files_from_unified_diff(REAL_SHAPED_DIFF)
    assert files == ["pages/rabbitmq/rabbitmq_page.py"]


def test_multiple_files_deduped_and_sorted():
    diff = REAL_SHAPED_DIFF + DIFF_TOUCHING_STEPS
    files = changed_files_from_unified_diff(diff)
    assert files == [
        "features/steps/step_definition_common_page.py",
        "pages/rabbitmq/rabbitmq_page.py",
    ]


def test_scoped_change_is_narrowly_scoped():
    report = check(
        REAL_SHAPED_DIFF, step_dir="features/steps", locators_dir="config/web_locators"
    )
    assert report.is_scoped_narrowly
    assert report.touches_shared_dirs == []


def test_change_touching_step_dir_is_flagged():
    report = check(
        DIFF_TOUCHING_STEPS,
        step_dir="features/steps",
        locators_dir="config/web_locators",
    )
    assert not report.is_scoped_narrowly
    assert "features/steps/step_definition_common_page.py" in report.touches_shared_dirs


def test_flags_locator_dir_even_with_a_repo_root_prefix():
    # Real bug found live: a diff generated from the outer git repo root
    # (not the project subdirectory aitlc.toml lives in) has paths like
    # "automation/myproject/config/web_locators/x.py" — a strict
    # startswith("config/web_locators") check misses this entirely.
    diff = (
        "diff --git a/automation/myproject/config/web_locators/x.py "
        "b/automation/myproject/config/web_locators/x.py\n"
        "--- a/automation/myproject/config/web_locators/x.py\n"
        "+++ b/automation/myproject/config/web_locators/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    report = check(diff, step_dir="features/steps", locators_dir="config/web_locators")
    assert not report.is_scoped_narrowly
    assert report.touches_shared_dirs


def test_empty_diff_has_no_changed_files():
    report = check("", step_dir="features/steps", locators_dir="config/web_locators")
    assert report.changed_files == []
    assert report.is_scoped_narrowly
