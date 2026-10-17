import pytest

from career_copilot.adapters.embed.fake import FakeEmbedder
from career_copilot.usecases.rank import cosine, rank


def test_cosine_basics() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0


def test_job_like_the_exemplar_ranks_first() -> None:
    e = FakeEmbedder()
    fde, sales, data = e.embed(
        [
            "forward deployed engineer customers python berlin",
            "account executive sales quota",
            "data engineer spark pipelines",
        ]
    )
    (exemplar,) = e.embed(["forward deployed engineer python customers"])
    ranked = rank({1: sales, 2: fde, 3: data}, [exemplar], [])
    assert ranked[0][0] == 2


def test_blend_uses_both_reference_kinds() -> None:
    jobs = {1: [1.0, 0.0], 2: [0.0, 1.0]}
    only_exemplar = rank(jobs, [[1.0, 0.0]], [])
    blended = rank(jobs, [[1.0, 0.0]], [[0.0, 1.0]], exemplar_weight=0.4)
    assert only_exemplar[0][0] == 1
    assert blended[0][0] == 2  # intent weighs 0.6 here


def test_only_intents_rank_by_intent_alone() -> None:
    ranked = rank({1: [1.0, 0.0], 2: [0.0, 1.0]}, [], [[0.0, 1.0]])
    assert ranked == [(2, 1.0), (1, 0.0)]


def test_no_references_ranks_nothing() -> None:
    assert rank({1: [1.0]}, [], []) == []


def test_ties_break_by_job_id() -> None:
    assert [j for j, _ in rank({5: [1.0, 0.0], 3: [1.0, 0.0]}, [[1.0, 0.0]], [])] == [3, 5]


def test_fake_embedder_is_normalised_deterministic_and_recorded() -> None:
    e = FakeEmbedder(dim=16)
    (a,) = e.embed(["Python python Berlin"])
    (b,) = e.embed(["python berlin python"])
    assert a == b
    assert len(a) == 16
    assert sum(x * x for x in a) == pytest.approx(1.0)
    assert e.calls == [["Python python Berlin"], ["python berlin python"]]
    assert e.model == "fake-embed"


def test_a_job_close_to_the_cv_moves_up() -> None:
    jobs = {1: [1.0, 0.0], 2: [0.0, 1.0]}
    cv = [0.0, 1.0]
    without = rank(jobs, [[1.0, 0.0]], [])
    with_cv = rank(jobs, [[1.0, 0.0]], [], cv=cv, cv_weight=0.7)
    assert without[0][0] == 1
    assert with_cv[0][0] == 2


def test_zero_cv_weight_or_no_cv_changes_nothing() -> None:
    jobs = {1: [1.0, 0.0], 2: [0.5, 0.5]}
    base = rank(jobs, [[1.0, 0.0]], [[0.0, 1.0]])
    assert rank(jobs, [[1.0, 0.0]], [[0.0, 1.0]], cv=None, cv_weight=0.5) == base
    assert rank(jobs, [[1.0, 0.0]], [[0.0, 1.0]], cv=[1.0, 0.0], cv_weight=0.0) == base


def test_the_cv_alone_does_not_make_a_ranking() -> None:
    assert rank({1: [1.0, 0.0]}, [], [], cv=[1.0, 0.0], cv_weight=0.3) == []
