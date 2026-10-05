"""求解器单元测试：评分、约束、平分确定性、覆盖可行性。"""
from app.solver import (
    InputAnnotation,
    SolveParams,
    dice_overlap,
    quantize_overlap,
    solve_consensus,
)


def ann(aid, start, end, lid, weight=5, name=None):
    return InputAnnotation(
        annotator_id=aid, annotator_name=name or f"a{aid}",
        weight=weight, start=start, end=end,
        label_id=lid, label_name=f"L{lid}",
    )


def test_dice_and_quantize():
    assert dice_overlap(0, 10, 0, 10) == 1.0
    assert dice_overlap(0, 4, 2, 6) == 0.5
    assert quantize_overlap(1.0) == 10
    assert quantize_overlap(0.5) == 5


def test_weighted_winner_and_rejection():
    # PERSON=1, ORG=2 互斥；区间相同，高权重 PERSON(8+5) 胜 ORG(3)
    annotations = [
        ann(1, 0, 2, 1, 8), ann(2, 0, 2, 1, 5), ann(3, 0, 2, 2, 3),
        # 让三名标注者都"参与"文档的另一处不冲突标注
        ann(1, 4, 6, 1, 8), ann(2, 4, 6, 1, 5), ann(3, 4, 6, 1, 3),
    ]
    res = solve_consensus(annotations, set(), {(1, 2)})
    chosen = {
        (c.start, c.end, c.label_id): res.chosen[c.index]
        for c in res.candidates
    }
    assert chosen[(0, 2, 1)] is True
    assert chosen[(0, 2, 2)] is False
    assert chosen[(4, 6, 1)] is True
    # 拒绝候选仍保留支持者明细
    rejected = res.candidates[[
        (c.start, c.end, c.label_id) for c in res.candidates
    ].index((0, 2, 2))]
    assert rejected.supporters[0]["weight"] == 3


def test_nesting_allowed_crossing_forbidden():
    # CLAIM=3 可嵌套 PERSON=1；ORG=2 与 PERSON 交叉且无嵌套 => 不能同时取
    annotations = [
        ann(1, 0, 10, 3, 5), ann(2, 0, 10, 3, 5),  # 外层 CLAIM
        ann(1, 2, 4, 1, 5), ann(2, 2, 4, 1, 5),    # 内层 PERSON（允许）
        ann(3, 8, 12, 2, 10),                      # 与 CLAIM 交叉
        ann(1, 20, 22, 1, 1), ann(2, 20, 22, 1, 1),
        ann(3, 20, 22, 1, 1),
    ]
    res = solve_consensus(annotations, {(3, 1)}, {(1, 2)})
    by = {(c.start, c.end, c.label_id): res.chosen[c.index] for c in res.candidates}
    assert by[(0, 10, 3)] is True
    assert by[(2, 4, 1)] is True       # 嵌套共存
    assert by[(8, 12, 2)] is False     # 交叉被排除


def test_tie_break_is_deterministic_and_lexicographic():
    # 两个等长同标签不重叠、得分相同 => 都取；构造互斥重叠的等分场景：
    # 区间 [0,2) 与 [2,4) 不冲突。改为 [0,4) vs [2,6) 互斥同分时取起点小的
    annotations = [
        ann(1, 0, 4, 1, 5), ann(2, 0, 4, 1, 5),
        ann(1, 2, 6, 1, 5), ann(2, 2, 6, 1, 5),
    ]
    r1 = solve_consensus(annotations, set(), set())
    r2 = solve_consensus(annotations, set(), set())
    assert [tuple(res.chosen) for res in (r1,)]
    assert r1.chosen == r2.chosen
    starts = [c.start for c, ok in zip(r1.candidates, r1.chosen) if ok]
    assert starts == [0]  # 起点小者胜


def test_override_forces_low_score_candidate():
    annotations = [
        ann(1, 0, 2, 1, 8), ann(2, 0, 2, 1, 5), ann(3, 0, 2, 2, 3),
        ann(1, 4, 6, 1, 8), ann(2, 4, 6, 1, 5), ann(3, 4, 6, 1, 3),
    ]
    res = solve_consensus(
        annotations, set(), {(1, 2)}, SolveParams(),
        force_selected_keys={(0, 2, 2)},
    )
    # 强制采纳 ORG => 同区间 PERSON 因互斥必须落选
    by = {(c.start, c.end, c.label_id): res.chosen[c.index] for c in res.candidates}
    assert by[(0, 2, 2)] is True
    assert by[(0, 2, 1)] is False


def test_conflicting_overrides_infeasible():
    annotations = [
        ann(1, 0, 2, 1, 8), ann(2, 0, 2, 1, 5),
    ]
    res = solve_consensus(
        annotations, set(), set(), SolveParams(),
        force_selected_keys={(0, 2, 1)},
        force_deselected_keys={(0, 2, 1)},
    )
    assert res.status not in ("OPTIMAL", "FEASIBLE")


def test_partial_overlap_support_same_label():
    # 同标签、边界不同但相交的标注互相为对方候选贡献；
    # 零重叠与异标签标注不得计分。
    annotations = [
        ann(1, 0, 4, 1, 8),    # 候选 A [0,4) L1
        ann(2, 2, 6, 1, 5),    # 候选 B [2,6) L1，与 A 相交
        ann(3, 10, 12, 1, 3),  # 同标签但零重叠
        ann(1, 0, 4, 2, 8),    # 同区间但异标签
    ]
    res = solve_consensus(annotations, set(), {(1, 2)})
    by = {(c.start, c.end, c.label_id): c for c in res.candidates}
    a, b = by[(0, 4, 1)], by[(2, 6, 1)]
    # dice(A,B)=0.5 → q=5：A = 8*10 + 5*5 = 105；B = 5*10 + 8*5 = 90
    assert a.score == 105 and a.raw_score == 10.5
    assert b.score == 90 and b.raw_score == 9.0
    # 同一标注（a1 的 [0,4)）支持多个候选
    assert {s["annotator_id"] for s in a.supporters} == {1, 2}
    assert {s["annotator_id"] for s in b.supporters} == {1, 2}
    # 零重叠（a3）与异标签标注不出现在支持者中
    assert all(s["annotator_id"] != 3 for s in a.supporters + b.supporters)
    assert [s["annotator_id"] for s in by[(0, 4, 2)].supporters] == [1]
    # 每位支持者：贡献 = 权重 × 量化重叠比；Σ贡献 = 候选展示分
    for cand in res.candidates:
        for s in cand.supporters:
            assert abs(s["contribution"] - s["weight"] * s["overlap_ratio"]) < 1e-9
        assert abs(sum(s["contribution"] for s in cand.supporters)
                   - cand.raw_score) < 1e-9
        assert abs(cand.raw_score * 10 - cand.score) < 1e-9
    # 同标签相交不可共存：分高者 A 入选
    assert res.chosen[a.index] is True
    assert res.chosen[b.index] is False


def test_partial_overlap_support_changes_winner():
    # 回归：只汇集完全相同区间时会选错共识。
    # 三条同标签相交标注互为支持，部分重叠贡献使 [2,6) 反超 [0,4)。
    annotations = [
        ann(1, 0, 4, 1, 10),
        ann(2, 2, 6, 1, 8),
        ann(3, 3, 5, 1, 6),
    ]
    res = solve_consensus(annotations, set(), set())
    by = {(c.start, c.end): c for c in res.candidates}
    a, b, c = by[(0, 4)], by[(2, 6)], by[(3, 5)]
    # dice(A,B)=0.5→q5, dice(A,C)=1/3→q3, dice(B,C)=2/3→q7
    assert a.score == 10 * 10 + 8 * 5 + 6 * 3   # 158
    assert b.score == 10 * 5 + 8 * 10 + 6 * 7   # 172
    assert c.score == 10 * 3 + 8 * 7 + 6 * 10   # 146
    assert res.chosen[b.index] is True
    assert res.chosen[a.index] is False
    assert res.chosen[c.index] is False


def test_zero_overlap_and_foreign_label_not_counted():
    annotations = [
        ann(1, 0, 4, 1, 8),
        ann(2, 4, 8, 1, 5),   # 端点相接，零重叠
        ann(3, 0, 4, 2, 10),  # 异标签
    ]
    res = solve_consensus(annotations, set(), set())
    by = {(c.start, c.end, c.label_id): c for c in res.candidates}
    a = by[(0, 4, 1)]
    assert [s["annotator_id"] for s in a.supporters] == [1]
    assert a.score == 80
    assert by[(4, 8, 1)].score == 50
    assert by[(0, 4, 2)].score == 100
