from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.incidents import recluster
from app.incidents.cluster import (
    canonicalize,
    event_fingerprint,
    higher_severity,
)
from app.incidents.tokenizer import similarity, tokenize


def _incident(
    incident_id: int,
    title: str,
    *,
    component: str = "redis",
    severity: str = "P1",
    status: str = "new",
    root_cause: str | None = None,
) -> SimpleNamespace:
    now = datetime.now()
    return SimpleNamespace(
        id=incident_id,
        title=title,
        component=component,
        severity=severity,
        status=status,
        root_cause=root_cause,
        created_at=now + timedelta(seconds=incident_id),
        last_seen_at=now + timedelta(seconds=incident_id),
    )


def test_chinese_bigram_similarity_meets_cluster_threshold():
    first = "Redis 内存使用率超过 90%"
    second = "Redis 内存使用率达 92%"

    assert "内存" in tokenize(first)
    assert similarity(first, second, same_severity=True) >= 0.6


def test_event_fingerprint_canonicalizes_title():
    assert canonicalize(" Redis--Memory  > 90% ") == "redis memory 90"
    assert event_fingerprint("redis", "Redis memory > 90%") == event_fingerprint(
        "redis", " redis  memory 90 "
    )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("P0", "P1", "P0"), ("P3", "P2", "P2"), ("P1", "P1", "P1")],
)
def test_higher_severity(left, right, expected):
    assert higher_severity(left, right) == expected


def test_recluster_plan_keeps_targets_as_stable_roots(monkeypatch):
    incidents = [_incident(1, "A"), _incident(2, "B"), _incident(3, "C")]

    def score(source: str, target: str, same_severity: bool) -> float:
        return {("B", "A"): 0.8, ("C", "A"): 0.1}.get((source, target), 0.0)

    monkeypatch.setattr(recluster, "similarity", score)
    operations, report, _ = recluster.build_recluster_plan(incidents, set())

    assert [(op.old_incident_id, op.new_incident_id) for op in operations] == [(2, 1)]
    assert all(op.new_incident_id not in {op.old_incident_id for op in operations} for op in operations)
    assert report[-1]["decision"] == "unchanged"


def test_recluster_plan_skips_protected_incidents(monkeypatch):
    incidents = [_incident(1, "A"), _incident(2, "B", root_cause="disk full")]
    monkeypatch.setattr(recluster, "similarity", lambda *_: 0.9)

    operations, report, _ = recluster.build_recluster_plan(incidents, {2})

    assert operations == []
    assert report[1]["decision"] == "skipped=diagnosed"
