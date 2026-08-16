"""Guard: internal planning docs must never enter the published package.

`idea.md`, `SRS.md`, `ROADMAP.md` and `ARCHITECTURE.md` are internal design
and planning material, and `patterns.yaml` encodes one project's failure
signatures. A stale build produced before the sdist allowlist existed did
ship all of them, which is exactly the mistake this pins down: the packaging
config is easy to widen by accident (a stray `include = ["*.md"]`), and
nothing else would catch it before publish.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Files that exist in the working tree but must stay out of any artifact.
INTERNAL_ONLY = ("idea.md", "SRS.md", "ROADMAP.md", "ARCHITECTURE.md", "patterns.yaml")


def _sdist_include() -> list[str]:
    with open(PACKAGE_ROOT / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


class TestSdistAllowlist:
    def test_is_an_allowlist_not_an_excludelist(self):
        # An allowlist fails closed: a new internal doc is excluded by
        # default. An exclude list fails open — every future file ships
        # unless someone remembers to add it.
        assert _sdist_include(), "sdist must declare an explicit include allowlist"

    def test_no_internal_doc_is_listed(self):
        include = _sdist_include()
        for name in INTERNAL_ONLY:
            assert name not in include, f"{name} is internal and must not be packaged"

    def test_no_bare_markdown_glob(self):
        # `*.md` would sweep every internal doc back in.
        for entry in _sdist_include():
            assert not entry.strip().endswith(
                "*.md"
            ), f"'{entry}' would package internal docs; list files explicitly"

    def test_config_template_is_shipped(self):
        # Without it a fresh install cannot resolve a project's env var
        # names or feature layout — the package is unusable out of the box.
        assert "aitlc.toml.example" in _sdist_include()

    def test_source_and_license_are_shipped(self):
        include = _sdist_include()
        assert "src/aitlc" in include
        assert "LICENSE" in include


class TestInternalDocsStillExistLocally:
    def test_they_are_present_in_the_working_tree(self):
        # If one is deleted outright the exclusion tests above would pass
        # vacuously; this keeps them meaningful.
        present = [n for n in INTERNAL_ONLY if (PACKAGE_ROOT / n).exists()]
        assert present, "expected internal docs to exist locally"


class TestNoProjectIdentifiers:
    """The published package must not name the company or product it grew from.

    aitlc is a generic tool. Internal names leaking into comments, help text
    or fixtures is a disclosure problem the moment the package is published,
    and it is easy to reintroduce by pasting a real example while debugging.
    """

    # Deliberately broad: any of these appearing anywhere in shipped code is
    # a review signal, even inside a string or comment.
    FORBIDDEN = (
        "caldera",
        "salesgenie",
        "infogroup",
        "dss3",
        "nxg",
        "oess",
        "genie",
        "dataaxle",
        "data-axle",
    )

    def _all_files(self) -> list[Path]:
        """Every file in the package folder, not just Python sources.

        The earlier scan covered src/ and tests/ only, which let identifiers
        sit undetected in the config template, the packaging metadata and
        the design docs — all of which are read by anyone adopting the tool.
        """
        skip_dirs = {
            ".venv",
            "__pycache__",
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
        }
        return sorted(
            path
            for path in PACKAGE_ROOT.rglob("*")
            if path.is_file()
            and not any(part in skip_dirs for part in path.parts)
            and path.suffix not in {".pyc", ".whl", ".gz"}
            # This file spells the terms out in order to search for them,
            # so it is the one file excluded from its own scan. LICENSE used
            # to be excluded too, back when it named the originating company
            # as copyright holder; now that it names an individual there is
            # no reason to leave the one file a stale org name would most
            # embarrassingly survive in unscanned.
            and path.name != Path(__file__).name
        )

    def _shipped_python_files(self) -> list[Path]:
        # This file necessarily spells the forbidden terms out in order to
        # search for them, so it is the one file excluded from its own scan.
        return sorted(
            path
            for path in [
                *(PACKAGE_ROOT / "src").rglob("*.py"),
                *(PACKAGE_ROOT / "tests").rglob("*.py"),
            ]
            if path.name != Path(__file__).name
        )

    def test_files_were_found(self):
        # Without this, an empty glob would make the scan below vacuous.
        assert len(self._shipped_python_files()) > 20

    def test_no_forbidden_identifier_anywhere_in_the_package(self):
        offenders: list[str] = []
        for path in self._all_files():
            try:
                lowered = path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            for term in self.FORBIDDEN:
                if term in lowered:
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {term}")
        assert not offenders, "project identifiers found: " + "; ".join(offenders[:10])

    def test_no_forbidden_identifier_in_shipped_code(self):
        offenders: list[str] = []
        for path in self._shipped_python_files():
            lowered = path.read_text(encoding="utf-8", errors="replace").lower()
            for term in self.FORBIDDEN:
                if term in lowered:
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {term}")
        assert not offenders, "project identifiers found in shipped code: " + "; ".join(
            offenders[:10]
        )
