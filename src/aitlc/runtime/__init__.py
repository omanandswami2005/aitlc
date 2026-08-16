"""Attachable runtime instrumentation for behave.

Everything in this package exists so aitlc can add behaviour to a behave
run **without the target project changing a single file** — no edits to
`environment.py`, no hook blocks pasted into `after_step`, nothing to keep
in sync when aitlc is upgraded.

That constraint is not cosmetic. A debugging tool that requires editing the
suite it debugs cannot be adopted incrementally, cannot be uninstalled
cleanly, and silently becomes a no-op for anyone who installs the tool but
not the edit — which is exactly the failure this package was written to
remove.
"""
