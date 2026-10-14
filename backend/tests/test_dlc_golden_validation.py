"""Golden-set server-side validation (golden-import G4): no unusable item is ever written.

An item with no question, no expected answer or criteria the served table does not have
is refused with a 422 naming the rows; `seed` refuses the whole batch and leaves the
holdout unsealed, so the curator can fix the file and retry.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import criteria as criteria_svc
from app.dlc import golden as golden_svc
from app.evaluation.models import EvalDataset
from app.models.dlc import CriteriaSet, Criterion
from app.models.ledger import Agent

WS = DEFAULT_WORKSPACE_ID


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (Criterion, CriteriaSet):
            session.query(model).delete()
        session.query(EvalDataset).filter(EvalDataset.role == "golden").delete()
        session.query(Agent).filter(Agent.name.like("gv-%")).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


def _lineage(db, *, publish=True) -> str:
    agent = Agent(workspace_id=WS, name="gv-agent", method="zip_runtime", status="active",
                  spec={"name": "gv"}, version="1", resource_id="rt_gv-abc", owner="dev1")
    db.add(agent)
    db.commit()
    s = criteria_svc.create_set(db, WS, kind="agent", agent_id=agent.id, name="c", actor="biz")
    criteria_svc.save_draft(db, s, [
        {"key": "R1", "text": "agent never leaks", "dimension": "responsibility",
         "tier": "redline", "executor": {"kind": "evaluator", "evaluator_id": "code-pii"},
         "notes": "n/a:cognition n/a:cost n/a:performance (test fixture)"},
        {"key": "G1", "text": "agent quotes stock", "dimension": "quality", "tier": "gate",
         "threshold": 0.9, "executor": {"kind": "evaluator", "evaluator_id": "code-stock"}},
    ], actor="biz", kind_of=lambda _e: "code")
    if publish:
        criteria_svc.publish(db, s, actor="biz")
    db.commit()
    return s.lineage_id


def _set(db, lineage):
    parent = golden_svc.create(db, WS, name="gv", criteria_lineage_id=lineage, actor="biz")
    db.commit()
    return parent, golden_svc.splits_of(db, parent)


def _item(sid, *, q="q", a="a", criteria=None, **extra):
    meta = {"dlc": {"criteria_ids": criteria}} if criteria is not None else {}
    return {"scenario_id": sid, "turns": [{"input": q, "expected_response": a}],
            "metadata": meta, **extra}


@pytest.mark.parametrize("bad, code", [
    ({"scenario_id": "x", "turns": []}, "golden.empty_input"),
    ({"scenario_id": "x"}, "golden.empty_input"),
    ({"scenario_id": "x", "turns": [{"input": "  ", "expected_response": "a"}]},
     "golden.empty_input"),
    ({"scenario_id": "x", "turns": [{"input": "q", "expected_response": "a"},
                                    {"expected_response": "b"}]}, "golden.empty_input"),
    ({"scenario_id": "x", "turns": [{"input": "q"}]}, "golden.empty_expected"),
    ({"scenario_id": "x", "turns": [{"input": "q", "expected_response": "a"},
                                    {"input": "q2", "expected_response": ""}]},
     "golden.empty_expected"),
])
def test_add_items_refuses_an_item_without_question_or_answer(db, bad, code):
    _, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["dev"], [_item("ok"), bad], actor="biz")
    assert exc.value.code == code and exc.value.status_code == 422
    assert exc.value.detail["errors"] == [{"index": 1, "scenario_id": "x", "code": code}]
    assert exc.value.detail["count"] == 1
    assert splits["dev"].items == []  # nothing written, not even the good row


def test_add_items_refuses_criteria_the_served_table_does_not_have(db):
    _, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["regression"], [
            _item("a", criteria=["G1"]), _item("b", criteria=["G1", "G9", "X"]),
        ], actor="biz")
    assert exc.value.code == "golden.unknown_criteria" and exc.value.status_code == 422
    assert exc.value.detail["errors"] == [{"index": 1, "scenario_id": "b",
                                           "code": "golden.unknown_criteria",
                                           "criteria_ids": ["G9", "X"]}]
    assert exc.value.detail["unknown_criteria"] == ["G9", "X"]
    # known keys, and items that name no criterion, are accepted
    added = golden_svc.add_items(db, splits["regression"],
                                 [_item("a", criteria=["G1", "R1"]), _item("c")], actor="biz")
    assert [i["scenario_id"] for i in added] == ["a", "c"]


def test_a_draft_only_table_still_checks_its_keys(db):
    _, splits = _set(db, _lineage(db, publish=False))
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["dev"], [_item("a", criteria=["NOPE"])], actor="biz")
    assert exc.value.code == "golden.unknown_criteria"
    golden_svc.add_items(db, splits["dev"], [_item("a", criteria=["G1"])], actor="biz")


def test_a_set_with_no_criteria_lineage_skips_the_criteria_check(db):
    _, splits = _set(db, None)
    added = golden_svc.add_items(db, splits["dev"], [_item("a", criteria=["ANY"])],
                                 actor="biz")
    assert len(added) == 1
    with pytest.raises(AppError) as exc:  # the answer rules still apply
        golden_svc.add_items(db, splits["dev"], [_item("b", a="")], actor="biz")
    assert exc.value.code == "golden.empty_expected"


def test_the_first_twenty_failures_are_listed_and_all_are_counted(db):
    _, splits = _set(db, _lineage(db))
    items = [_item(f"s{n}", a="") for n in range(25)] + [_item("u", criteria=["Z"])]
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["dev"], items, actor="biz")
    detail = exc.value.detail
    assert exc.value.code == "golden.empty_expected"  # the first failing row's rule
    assert detail["count"] == 26 and len(detail["errors"]) == golden_svc.MAX_REPORTED_ERRORS
    assert [e["index"] for e in detail["errors"]] == list(range(20))
    assert detail["unknown_criteria"] == ["Z"]  # summarised even past the listed rows


def test_one_row_can_break_two_rules(db):
    _, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["dev"], [_item("x", a=" ", criteria=["Q"])],
                             actor="biz")
    assert [e["code"] for e in exc.value.detail["errors"]] == [
        "golden.empty_expected", "golden.unknown_criteria"]


def test_holdout_closed_is_still_answered_before_validation(db):
    _, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.add_items(db, splits["holdout"], [{"scenario_id": "x"}], actor="biz")
    assert exc.value.code == "golden.holdout_closed"


def test_seed_with_one_bad_item_writes_nothing_and_leaves_the_holdout_unsealed(db):
    parent, splits = _set(db, _lineage(db))
    good = [_item(f"s{n}", criteria=["G1"]) for n in range(9)]
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, parent, good + [_item("bad", a="")], actor="biz")
    assert exc.value.code == "golden.empty_expected" and exc.value.status_code == 422
    assert exc.value.detail["errors"] == [{"index": 9, "scenario_id": "bad",
                                           "code": "golden.empty_expected"}]
    assert all(not s.items for s in splits.values())
    # not sealed: the corrected batch goes through
    counts = golden_svc.seed(db, parent, good + [_item("bad")], actor="biz")
    assert sum(counts.values()) == 10


def test_seed_index_is_the_position_in_the_request_even_with_named_splits(db):
    parent, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, parent, [
            _item("h1", split="holdout"), _item("d1", split="dev"),
            _item("d2", split="dev", criteria=["NOPE"]),
        ], actor="biz")
    assert exc.value.detail["errors"][0]["index"] == 2
    assert all(not s.items for s in splits.values())


def test_seed_refuses_a_late_duplicate_without_writing_earlier_splits(db):
    """Duplicates inside one split used to surface only after other splits were written."""
    parent, splits = _set(db, _lineage(db))
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, parent, [
            _item("d1", split="dev"), _item("h1", split="holdout"),
            _item("h1", split="holdout"),
        ], actor="biz")
    assert exc.value.code == "golden.duplicate_scenario"
    assert all(not s.items for s in splits.values())
