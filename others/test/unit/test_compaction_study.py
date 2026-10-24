"""Experiment interfaces: candidate admission and independently specified scoring."""

import pytest

from others.harness.compaction_study.guarded import UnsafeCompaction, compact_preserving_spans
from others.harness.compaction_study.run import verify_evidence
from others.harness.compaction_study.scoring import score


def test_candidate_refuses_when_required_rules_cannot_fit():
    with pytest.raises(UnsafeCompaction, match="budget"):
        compact_preserving_spans(
            "Never edit /archive.\nBackground can be removed.",
            ["Never edit /archive."], max_chars=12)


def test_candidate_keeps_verbatim_rule_and_never_restores_hidden_memory():
    result = compact_preserving_spans(
        "Background context.\nNever edit /archive.", ["Never edit /archive."], max_chars=20)
    assert result == "Never edit /archive."
    with pytest.raises(UnsafeCompaction, match="visible"):
        compact_preserving_spans("Background context.", ["Never edit /archive."], max_chars=100)


def test_updated_truth_never_accepts_an_alias_for_stale_value():
    case = {"probes": [{"id": "status", "kind": "fact", "expected": "not_run",
                        "aliases": ["not run"], "question": "status?"}],
            "rounds": [{"updates": {}}, {"updates": {"status": "failed"}}]}
    stale = score(case, 2, {"status": "not run"})
    assert stale["correct"] == 0
    assert stale["wrong_nonempty"] == 1
    assert stale["updates_total"] == 1
    absent = score(case, 2, {"status": None})
    assert absent["missing"] == 1
    assert absent["wrong_nonempty"] == 0


def test_exact_path_probe_rejects_wrong_case():
    case = {"probes": [{"id": "target", "kind": "fact", "expected": "src/payments/v1.py",
                        "aliases": [], "question": "path?"}], "rounds": [{"updates": {}}]}
    assert score(case, 1, {"target": "SRC/PAYMENTS/V1.PY"})["correct"] == 0


def test_rescoring_rejects_changed_archived_evidence(tmp_path):
    (tmp_path / "data.json").write_text("abc")
    (tmp_path / "inputs-integrity.json").write_text(
        '{"data.json":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}')
    verify_evidence(tmp_path)
    (tmp_path / "data.json").write_text("changed")
    with pytest.raises(ValueError, match="Archived evidence changed"):
        verify_evidence(tmp_path)
