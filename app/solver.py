"""CP-SAT 共识求解器。

输入一组标注（每条带字符区间、标签、标注者权重），以及标签之间的
嵌套/互斥规则，求出总得分最大的候选区间子集。

评分方式（参数化，快照进报告）：
    每个候选 = 一条输入标注的 (start, end, label) 去重。
    候选支持者 = 所有与候选「同标签且区间相交」的输入标注
        （同一标注可支持多个候选；异标签或零重叠的标注不计分）。
    候选得分 = Σ 标注者权重 × 标签一致系数(label_match_weight)
               × 量化重叠比例 quantize(overlap_ratio)
    重叠比例采用对称的 Sørensen–Dice 系数：
        overlap_ratio = 2 * |A∩B| / (|A| + |B|)
    量化：quantize(r) = round(r × SCORE_SCALE)，得到 0..SCORE_SCALE
    的整数；权重 1..10 也是整数，因此候选得分是**精确整数**，直接
    作为 CP-SAT 整数目标；展示分 = 整数得分 / SCORE_SCALE，与逐位
    支持者的贡献分之和严格一致。

约束：
    * 同一候选区间至多取一次；
    * 两个区间重叠时，只有「严格包含且标签允许嵌套」才能共存；
      相交但互不包含（交叉）或互斥标签一律禁止共存。

平分（总得分相同）时，通过次级目标在所有最优解中确定性地选择
按 (start 升序, end 升序, label 升序) 排序后的候选布尔向量
字典序最大的解：排序靠前的候选优先被采纳。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

ALGORITHM_NAME = "adj-cpsat-consensus"
ALGORITHM_VERSION = "1.1.0"

# 得分放大倍数：权重 1..10 的整数与 Dice 系数（一位小数量化）
# 相乘后仍为整数，CP-SAT 只处理整数目标。
SCORE_SCALE = 10
# 默认标签不一致时的系数（本应用候选带标签，实际只聚合同标签支持）。
DEFAULT_LABEL_MATCH_WEIGHT = 1.0


def dice_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """对称区间重叠比例 2|∩|/(|A|+|B|)，空区间按 0 处理。"""
    inter = max(0, min(a_end, b_end) - max(a_start, b_start))
    la, lb = a_end - a_start, b_end - b_start
    denom = la + lb
    if denom <= 0:
        return 0.0
    return (2.0 * inter) / denom


def quantize_overlap(ratio: float) -> int:
    """把 [0,1] 的重叠比例量化到 0..SCORE_SCALE 的整数（四舍五入）。"""
    return min(SCORE_SCALE, max(0, int(math.floor(ratio * SCORE_SCALE + 0.5))))


@dataclass(frozen=True)
class InputAnnotation:
    annotator_id: int
    annotator_name: str
    weight: int
    start: int
    end: int
    label_id: int
    label_name: str


@dataclass
class SolveParams:
    label_match_weight: float = DEFAULT_LABEL_MATCH_WEIGHT
    # 是否强制最终共识中必须包含裁决者覆盖（false 时覆盖仍作为建议展示）
    respect_overrides: bool = True


@dataclass
class Candidate:
    index: int
    start: int
    end: int
    label_id: int
    label_name: str
    score: int                      # 放大后的整数得分
    raw_score: float                # 未放大的浮点得分（展示用）
    supporters: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SolveResult:
    candidates: list[Candidate]
    chosen: list[bool]
    total_score: int
    raw_total_score: float
    status: str
    wall_seconds: float
    objective_value: int
    forced: list[int] = field(default_factory=list)       # 覆盖强制采纳的候选
    forced_out: list[int] = field(default_factory=list)   # 覆盖强制否决的候选


def _overlap(a: Candidate, b: Candidate) -> bool:
    return a.start < b.end and b.start < a.end


def _strict_contains(outer: Candidate, inner: Candidate) -> bool:
    """outer 是否严格包含 inner（边界相等不算嵌套）。"""
    return outer.start <= inner.start and inner.end <= outer.end and (
        outer.start < inner.start or inner.end < outer.end
    )


def solve_consensus(
    annotations: list[InputAnnotation],
    nesting_edges: set[tuple[int, int]],
    mutex_pairs: set[tuple[int, int]],
    params: SolveParams | None = None,
    force_selected_keys: set[tuple[int, int, int]] | None = None,
    force_deselected_keys: set[tuple[int, int, int]] | None = None,
) -> SolveResult:
    """求满足标签约束的最大得分共识。

    nesting_edges: (outer_label_id, inner_label_id)，外层标签可嵌套内层。
    mutex_pairs:   无序标签 id 对，二者区间不得重叠共存。
    force_*_keys:  (start, end, label_id) 覆盖指令。
    """
    params = params or SolveParams()
    force_selected_keys = force_selected_keys or set()
    force_deselected_keys = force_deselected_keys or set()

    # ---- 1. 聚合候选 -----------------------------------------------------
    # 候选 = 输入标注按 (start, end, label) 去重；
    # 候选的支持者 = 所有与候选同标签且区间相交（Dice 量化后 > 0）的
    # 输入标注——同一标注可支持多个候选；异标签或零重叠不计分。
    grouped: dict[tuple[int, int, int], str] = {}
    by_label: dict[int, list[InputAnnotation]] = {}
    for ann in annotations:
        grouped.setdefault((ann.start, ann.end, ann.label_id), ann.label_name)
        by_label.setdefault(ann.label_id, []).append(ann)

    ordered_keys = sorted(
        grouped.keys(), key=lambda k: (k[0], k[1], grouped[k], k[2])
    )

    candidates: list[Candidate] = []
    for i, key in enumerate(ordered_keys):
        start, end, label_id = key
        units_total = 0  # 整数得分单元：Σ 权重 × 量化重叠比（精确整数）
        supporters: list[dict[str, Any]] = []
        for ann in by_label.get(label_id, []):
            ratio = dice_overlap(start, end, ann.start, ann.end)
            q = quantize_overlap(ratio)
            if q <= 0:
                continue  # 零重叠（或量化后为 0）不计分
            units = ann.weight * q
            units_total += units
            supporters.append(
                {
                    "annotator_id": ann.annotator_id,
                    "annotator_name": ann.annotator_name,
                    "weight": ann.weight,
                    "start": ann.start,
                    "end": ann.end,
                    "overlap_ratio": round(q / SCORE_SCALE, 4),
                    "contribution": round(
                        units * params.label_match_weight / SCORE_SCALE, 4
                    ),
                }
            )
        supporters.sort(key=lambda s: (s["annotator_id"], s["start"], s["end"]))
        score = int(math.floor(units_total * params.label_match_weight + 0.5))
        candidates.append(
            Candidate(
                index=i,
                start=start,
                end=end,
                label_id=label_id,
                label_name=grouped[key],
                score=score,
                raw_score=round(score / SCORE_SCALE, 4),
                supporters=supporters,
            )
        )

    n = len(candidates)
    by_key = {(c.start, c.end, c.label_id): c.index for c in candidates}
    forced = sorted(by_key[k] for k in force_selected_keys if k in by_key)
    forced_out = sorted(by_key[k] for k in force_deselected_keys if k in by_key)

    model = cp_model.CpModel()
    x = [model.new_bool_var(f"cand_{i}") for i in range(n)]
    mutex_sets = {frozenset(pair) for pair in mutex_pairs}

    # ---- 2. 标签结构约束 -------------------------------------------------
    def nesting_allowed(outer: Candidate, inner: Candidate) -> bool:
        return (outer.label_id, inner.label_id) in nesting_edges

    for i in range(n):
        for j in range(i + 1, n):
            a, b = candidates[i], candidates[j]
            if not _overlap(a, b):
                continue  # 不重叠，互不影响
            if frozenset((a.label_id, b.label_id)) in mutex_sets:
                model.add(x[i] + x[j] <= 1)
                continue
            if _strict_contains(a, b) and nesting_allowed(a, b):
                continue  # b 可嵌套在 a 中
            if _strict_contains(b, a) and nesting_allowed(b, a):
                continue  # a 可嵌套在 b 中
            # 交叉、同点不同长度且不允许嵌套、同标签重叠等：禁止同时出现
            model.add(x[i] + x[j] <= 1)

    # ---- 3. 裁决者覆盖 ---------------------------------------------------
    if params.respect_overrides:
        for i in forced:
            model.add(x[i] == 1)
        for i in forced_out:
            model.add(x[i] == 0)

    # ---- 4. 目标 + 确定性平分裁决 ---------------------------------------
    total = sum(c.score * x[c.index] for c in candidates)

    # 次级权重：候选已按 (start,end,label) 排序，靠前的给更大权重，
    # 在「总得分相同」的解集合中选字典序最优 => 确定性、与需求一致。
    # 最大候选数受区间校验约束，通常很小；N<=60 时用 2 的幂保证严格字典序，
    # 更大时退化为线性权重（仍单调，记录在参数说明中）。
    if n == 0:
        tie_weight: list[int] = []
    elif n <= 60:
        tie_weight = [1 << (n - 1 - k) for k in range(n)]
    else:
        tie_weight = [n - k for k in range(n)]

    if n <= 60:
        # 严格字典序：即使平分，次级目标永远小于 1 个主目标单位。
        big = 1 << n  # > 最大次级和 (2^n - 1)
        model.maximize(big * total + sum(tie_weight[k] * x[k] for k in range(n)))
    elif n:
        big = n * (n + 1) // 2 + 1
        model.maximize(big * total + sum(tie_weight[k] * x[k] for k in range(n)))

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1   # 单线程 => 结果确定
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.random_seed = 42
    status = solver.solve(model)

    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    chosen = [bool(solver.value(x[i])) for i in range(n)] if feasible else [False] * n
    total_score = sum(candidates[i].score for i in range(n) if chosen[i])
    raw_total = round(total_score / SCORE_SCALE, 4)

    return SolveResult(
        candidates=candidates,
        chosen=chosen,
        total_score=total_score,
        raw_total_score=raw_total,
        status=solver.status_name(status),
        wall_seconds=solver.wall_time,
        objective_value=int(solver.objective_value) if feasible else 0,
        forced=forced,
        forced_out=forced_out,
    )
