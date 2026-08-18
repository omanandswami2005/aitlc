"""Credential resolution for S3: a profile beats static keys, and expiry says so."""

from __future__ import annotations

from aitlc.adapters.s3 import evidence
from aitlc.commands import s3_cmd


class _Cfg:
    s3_region = "us-east-2"
    s3_profile = ""

    class env:
        @staticmethod
        def resolve(_name):
            return None

    @staticmethod
    def require_env(name):
        raise AssertionError(f"static keys must not be read when a profile is set: {name}")


class TestProfileWins:
    def test_a_configured_profile_is_used_instead_of_static_keys(self, monkeypatch):
        """Static keys expire; a profile is resolved fresh on every call."""
        seen = {}
        monkeypatch.setattr(
            evidence,
            "build_client_from_profile",
            lambda profile, region: seen.update(profile=profile, region=region) or "client",
        )
        cfg = _Cfg()
        cfg.s3_profile = "some-profile"

        assert s3_cmd._build_s3_client(cfg) == "client"
        assert seen == {"profile": "some-profile", "region": "us-east-2"}

    def test_the_environment_overrides_the_config_file(self, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "from-env")
        monkeypatch.setattr(
            evidence, "build_client_from_profile", lambda profile, _r: profile
        )
        cfg = _Cfg()
        cfg.s3_profile = "from-file"
        assert s3_cmd._build_s3_client(cfg) == "from-env"


class TestExpiryIsNamed:
    """An expired token used to surface as a traceback out of a paginator."""

    class _Expired:
        @staticmethod
        def list_buckets():
            raise RuntimeError("An error occurred (ExpiredToken) when calling ...")

    class _Denied:
        @staticmethod
        def list_buckets():
            raise RuntimeError("An error occurred (AccessDenied) when calling ...")

    class _Fine:
        @staticmethod
        def list_buckets():
            return {"Buckets": []}

    def test_expired_credentials_are_reported_as_expired(self):
        ok, why = evidence.credentials_are_usable(self._Expired())
        assert not ok
        assert "expired" in why.lower()
        assert "profile" in why, "the message must say how to stop it happening again"

    def test_permission_problems_are_not_called_expiry(self):
        ok, why = evidence.credentials_are_usable(self._Denied())
        assert not ok and "permission" in why

    def test_working_credentials_report_no_problem(self):
        assert evidence.credentials_are_usable(self._Fine()) == (True, "")
