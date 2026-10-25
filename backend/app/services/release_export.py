"""GitOps export of a release bundle (roadmap T31): one deterministic YAML document.

The point of committing a bundle to Git is that a diff of the file is a diff of the
release. That only works if the same input always yields the same bytes, so:

* keys are sorted, the dumper is fixed (`safe_dump`, no aliases, block style), and
  every value is reduced to plain JSON types first (datetimes -> ISO strings);
* the body carries no generation time, no row ids and no author - nothing that
  changes when the same release is exported twice, or from another database;
* what the document says is exactly the digest-relevant identity (name, method, spec,
  artifact coordinates) plus a *reference* to the evaluation run that was pinned to
  the bundle (ids and version, never the score blobs, which re-runs would churn).

Determinism is therefore guaranteed for one bundle, and across bundle rows that share
a digest AND the same evidence reference. Evidence is deliberately outside the digest
(see `promotion.bundle_digest`), so two rows with one digest but different pinned
evaluation runs are the same release with different paperwork, and their files differ
only in the `evidence` block.
"""

import json
from typing import Any

import yaml

from app.models.ledger import ReleaseBundle

API_VERSION = "launchpad.agentcore/v1"
KIND = "ReleaseBundle"
HEADER = (
    "# AgentCore Launchpad release bundle. Generated - do not edit by hand.\n"
    "# The digest below is the release identity; `launchpad promote` ships this bundle.\n"
)


def _plain(value: Any) -> Any:
    """Reduce to JSON types so the dumper never sees a datetime or an ORM object."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def evidence_reference(evaluation: dict[str, Any] | None) -> dict[str, Any]:
    """Which evaluation run vouches for this bundle - a pointer, not the results."""
    if not evaluation:
        return {"evaluated": False}
    ref: dict[str, Any] = {"evaluated": True}
    for key in ("run_id", "dataset_id", "dataset_version"):
        if evaluation.get(key) is not None:
            ref[key] = evaluation[key]
    return ref


def bundle_document(bundle: ReleaseBundle) -> dict[str, Any]:
    document = {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"name": bundle.agent_name, "digest": f"sha256:{bundle.digest}"},
        "spec": {"method": bundle.method, "agent": bundle.spec or {}},
        "artifact": bundle.artifact or {},
        "evidence": evidence_reference(bundle.evaluation),
    }
    return _plain(document)


def export_yaml(bundle: ReleaseBundle) -> str:
    body = yaml.safe_dump(
        bundle_document(bundle),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=100000,  # never fold: a re-wrap would show up as a spurious diff
    )
    return HEADER + body
