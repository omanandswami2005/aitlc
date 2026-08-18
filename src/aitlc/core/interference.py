"""Did a parallel failure come from a sibling, or from the test?

`--verify-failures` answers this by re-running each failure serially. That is
the right thing to keep opt-in -- a re-run is expensive -- but it was the
*only* signal offered, so declining it left nothing at all.

Cheaper evidence is already to hand and was going unused:

- **Overlap.** Two runs that never overlapped in wall-clock cannot have
  interfered with each other. That rules suspects out for free.
- **Account collision.** Suites routinely share a handful of accounts across
  features. Two runs signed in as the same account, at the same time, are a
  near-certain explanation for a failure that looks like corrupted state, and
  detecting it costs a string comparison.

Neither proves interference. Both are stated as suspicion, not verdict: the
point is to narrow where to look before paying for a re-run, not to replace
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `search_account`-style step arguments, and any e-mail-looking literal. Broad
# on purpose: a missed account is a missed collision, while a false one only
# adds a suspect that overlap will usually clear.
_ACCOUNT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class Overlap:
    """Two runs that were in flight at the same time."""

    feature: str
    other: str
    seconds: float
    same_account: str = ""

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "overlapped_with": self.other,
            "overlap_seconds": round(self.seconds, 1),
            **({"shared_account": self.same_account} if self.same_account else {}),
        }


def accounts_in(text: str) -> set[str]:
    """Account identifiers mentioned in a feature or a log."""
    return set(_ACCOUNT.findall(text or ""))


def overlap_seconds(a: dict, b: dict) -> float:
    """Wall-clock seconds two runs were both in flight. 0.0 when disjoint."""
    start = max(a.get("started_at", 0.0), b.get("started_at", 0.0))
    end = min(a.get("ended_at", 0.0), b.get("ended_at", 0.0))
    return max(0.0, end - start)


def suspects_for(
    failure: dict, results: list[dict], accounts_by_feature: dict[str, set[str]]
) -> list[Overlap]:
    """Runs that could plausibly have interfered with this failure.

    Ordered by shared account first, then by how long they overlapped: an
    account collision is a far stronger signal than mere concurrency, and
    burying it under a longer-but-unrelated overlap wastes the reader's
    attention.
    """
    name = failure.get("feature", "")
    mine = accounts_by_feature.get(name, set())
    found: list[Overlap] = []
    for other in results:
        other_name = other.get("feature", "")
        if other_name == name:
            continue
        seconds = overlap_seconds(failure, other)
        if seconds <= 0:
            continue
        shared = sorted(mine & accounts_by_feature.get(other_name, set()))
        found.append(
            Overlap(
                feature=name,
                other=other_name,
                seconds=seconds,
                same_account=shared[0] if shared else "",
            )
        )
    found.sort(key=lambda o: (o.same_account == "", -o.seconds))
    return found


def interference_note(suspects: list[Overlap]) -> str:
    """One sentence a reader can act on, or nothing."""
    if not suspects:
        return "nothing else was in flight; a sibling cannot explain this one"
    shared = [s for s in suspects if s.same_account]
    if shared:
        return (
            f"{shared[0].other} ran at the same time as the same account "
            f"({shared[0].same_account}) -- a likely explanation before "
            "suspecting the test"
        )
    return (
        f"{len(suspects)} other run(s) overlapped but on different accounts; "
        "concurrency alone is weak evidence here"
    )
