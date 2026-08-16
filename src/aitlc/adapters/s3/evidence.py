"""S3 trace evidence pulling.

Two real gotchas found and documented while proving this live,
both baked into this adapter rather than left as tribal knowledge:

1. Many projects' S3 client wrapper (this one included) passes explicit
   credentials to boto3.client() under custom env var names, which disables
   boto3's own standard-name auto-detection. aitlc's own EnvMap (config.py)
   already models this — s3_access_key_id/s3_secret_access_key/
   s3_session_token map to whatever a project's actual env var names are,
   same pattern as every other credential in this tool.
2. Trace upload to S3 is typically a separate, real-CI-only post-suite
   step — an ad-hoc local/remote debugging run will usually find nothing
   for a test ID that was only ever run locally. This adapter surfaces an
   empty result plainly rather than erroring, so a caller can fall back to
   local reports/traces/*.zip (core/trace_evidence.py operates on those
   directly, no S3 involved).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3


class S3EvidenceError(RuntimeError):
    """Raised when an S3 lookup or download cannot be completed."""


@dataclass
class S3ObjectMatch:
    """An S3 object that matched a search, with its modification time."""

    key: str
    last_modified_epoch: float


def build_client(
    access_key_id: str, secret_access_key: str, session_token: str | None, region: str
) -> Any:
    """Build a boto3 S3 client, including a session token when present."""
    kwargs = {
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
        "region_name": region,
    }
    if session_token:
        kwargs["aws_session_token"] = session_token
    return boto3.client("s3", **kwargs)


def find_objects(
    client: Any, bucket: str, name_contains: str, *, prefix: str = ""
) -> list[S3ObjectMatch]:
    """List objects whose key contains name_contains, newest first.

    Generic — not trace-specific despite this module's original scope
    (traces). The daily HTML report this project uploads
    (`S3Utility.upload_file_and_get_presigned_url`, real confirmed key
    shape: `{s3_bucket_folder_name}/{environment_url.title()}/{name}...`)
    uses the exact same "list by prefix, filter by substring, take newest"
    shape as a trace search — one function serves both.

    Returns an empty list (not an error) when nothing matches — a genuinely
    common, expected outcome for an ad-hoc local/remote run (upload is
    real-CI-only for traces; a report search with a too-narrow prefix is
    just as normal), not a failure condition.
    """
    paginator = client.get_paginator("list_objects_v2")
    matches: list[S3ObjectMatch] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if name_contains in obj["Key"]:
                matches.append(
                    S3ObjectMatch(
                        key=obj["Key"],
                        last_modified_epoch=obj["LastModified"].timestamp(),
                    )
                )
    matches.sort(key=lambda m: m.last_modified_epoch, reverse=True)
    return matches


# Backward-compatible alias — trace_cmd.py's existing fetch-s3 subcommand
# calls this name; kept so that command doesn't need touching.
find_trace_keys = find_objects
S3TraceKey = S3ObjectMatch


def download_object(client: Any, bucket: str, key: str, output_path: Path) -> Path:
    """Download one S3 object to a local path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(output_path))
    except Exception as exc:
        raise S3EvidenceError(f"Failed to download s3://{bucket}/{key}: {exc}") from exc
    return output_path


# Backward-compatible alias.
download_trace = download_object


def fetch_object_bytes(client: Any, bucket: str, key: str) -> bytes:
    """Read an S3 object's body into memory.

    Read an object's body directly into memory — for a caller that's
    going to parse/summarize it (core/report_summary.py) rather than keep
    a copy on disk, avoiding a real 3.7-32MB write for a throwaway read.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:
        raise S3EvidenceError(f"Failed to read s3://{bucket}/{key}: {exc}") from exc
