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

平分（总得分相同）时，确定性地选择按
(start 升序, end 升序, label 升序) 排序后的候选布尔向量
字典序最大的解：排序靠前的候选优先被采纳。为避免旧版
「2 的幂次合成目标」在候选数较大时单项目标系数超过
CP-SAT 的 2^62 系数上限（模型 INVALID），采用分阶段求解：
阶段 1 最大化总得分；阶段 2 固定最优总分后，按候选排序分块
（每块 TIE_BLOCK_SIZE 个）逐块最大化二进制权重和并锁定前序块，
任意候选数下都严格等价于全局字典序裁决且整数系数安全。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

ALGORITHM_NAME = "adj-cpsat-consensus"
ALGORITHM_VERSION = "1.2.0"

# 得分放大倍数：权重 1..10 的整数与 Dice 系数（一位小数量化）
# 相乘后仍为整数，CP-SAT 只处理整数目标。
SCORE_SCALE = 10
# 默认标签不一致时的系数（本应用候选带标签，实际只聚合同标签支持）。
DEFAULT_LABEL_MATCH_WEIGHT = 1.0

# CP-SAT 目标系数必须严格小于 2^62（见 ortools LinearExpr 校验）。
# 旧版「big × 总分 + 2 的幂次平分权重」在 60 个候选、每候选至少一条
# 权重 1 的自身标注（score=10）时，单项目标系数 = 10 × 2^60 ≈ 1.15e19，
# 超过该上限，模型被判 INVALID；n>60 时退化为线性权重又不能保证严格
# 字典序。改为**分阶段求解**：先最大化总分，固定最优总分后按候选排序
# 分块（每块 TIE_BLOCK_SIZE 个）逐块最大化二进制权重和——任意候选数下
# 系数都远小于 2^62，且严格等价于「最优总分集合中的字典序最大解」。
TIE_BLOCK_SIZE = 40


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

    mutex_sets = {frozenset(pair) for pair in mutex_pairs}

    def build_model():
        """构建一个带全部结构约束/覆盖的新模型（每阶段必须新建）。"""
        m = cp_model.CpModel()
        bv = [m.new_bool_var(f"cand_{i}") for i in range(n)]

        def nesting_allowed(outer: Candidate, inner: Candidate) -> bool:
            return (outer.label_id, inner.label_id) in nesting_edges

        for i in range(n):
            for j in range(i + 1, n):
                a, b = candidates[i], candidates[j]
                if not _overlap(a, b):
                    continue  # 不重叠，互不影响
                if frozenset((a.label_id, b.label_id)) in mutex_sets:
                    m.add(bv[i] + bv[j] <= 1)
                    continue
                if _strict_contains(a, b) and nesting_allowed(a, b):
                    continue  # b 可嵌套在 a 中
                if _strict_contains(b, a) and nesting_allowed(b, a):
                    continue  # a 可嵌套在 b 中
                # 交叉、同点不同长度且不允许嵌套、同标签重叠等：禁止同时出现
                m.add(bv[i] + bv[j] <= 1)

        if params.respect_overrides:
            for i in forced:
                m.add(bv[i] == 1)
            for i in forced_out:
                m.add(bv[i] == 0)
        return m, bv

    def new_solver():
        s = cp_model.CpSolver()
        s.parameters.num_search_workers = 1   # 单线程 => 结果确定
        s.parameters.max_time_in_seconds = 30.0
        s.parameters.random_seed = 42
        return s

    def total_expr(bv):
        return sum(c.score * bv[c.index] for c in candidates)

    # ---- 阶段 1：在全部约束下最大化总得分 -------------------------------
    status = cp_model.MODEL_INVALID
    solver: cp_model.CpSolver | None = None
    x: list = []
    best_score = 0
    if n > 0:
        model, x = build_model()
        model.maximize(total_expr(x))
        solver = new_solver()
        status = solver.solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            best_score = sum(
                candidates[i].score for i in range(n) if solver.value(x[i])
            )

        # ---- 阶段 2：固定最优总分，分块严格裁决字典序平分 --------------
        # 候选已按 (start,end,label_name,label_id) 排序；对每个块 B 最大化
        # Σ_{k∈B} 2^(B-1-pos(k))·x[k]（二进制权重 ⇒ 块内严格字典序），
        # 并锁定此前各块的加权和。块大小取 TIE_BLOCK_SIZE，最大权重
        # 2^(B-1) 远低于 CP-SAT 的 2^62 系数上限；总分仅以 == 约束固定，
        # 不再与任何大常数相乘，因此候选数无上限。
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            locked_sums: list[tuple[range, int]] = []
            for lo in range(0, n, TIE_BLOCK_SIZE):
                block = range(lo, min(lo + TIE_BLOCK_SIZE, n))
                block_model, block_vars = build_model()
                block_model.add(total_expr(block_vars) == best_score)
                # 锁定此前各块在其最优字典序下的加权和
                for fixed_range, fixed_sum in locked_sums:
                    width = fixed_range.stop - fixed_range.start
                    block_model.add(
                        sum(
                            (1 << (width - 1 - (k - fixed_range.start)))
                            * block_vars[k]
                            for k in fixed_range
                        )
                        == fixed_sum
                    )
                width = block.stop - block.start
                block_model.maximize(
                    sum(
                        (1 << (width - 1 - (k - lo))) * block_vars[k]
                        for k in block
                    )
                )
                block_solver = new_solver()
                block_status = block_solver.solve(block_model)
                if block_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    # 理论上不可达：上一阶段（或前一块）的解满足
                    # 总分==最优且前序块锁定，必为本块可行解。
                    # 万一发生，保留上一个已求得的最优总分解
                    # （前序块仍处于字典序最优），不再继续后续块。
                    break
                block_value = int(
                    sum(
                        (1 << (width - 1 - (k - lo)))
                        * block_solver.value(block_vars[k])
                        for k in block
                    )
                )
                locked_sums.append((block, block_value))
                solver, x, status = block_solver, block_vars, block_status

    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    chosen = [bool(solver.value(x[i])) for i in range(n)] if (feasible and n) else [False] * n
    total_score = sum(candidates[i].score for i in range(n) if chosen[i])
    raw_total = round(total_score / SCORE_SCALE, 4)

    return SolveResult(
        candidates=candidates,
        chosen=chosen,
        total_score=total_score,
        raw_total_score=raw_total,
        status=solver.status_name(status) if solver is not None else "OPTIMAL",
        wall_seconds=solver.wall_time if solver is not None else 0.0,
        objective_value=total_score,
        forced=forced,
        forced_out=forced_out,
    )
