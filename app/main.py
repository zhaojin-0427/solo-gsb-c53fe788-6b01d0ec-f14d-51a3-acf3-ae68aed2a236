"""FastAPI 主应用：项目/文档/标签/标注者/标注 CRUD、共识求解、覆盖、不可变报告。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from .models import (
    AnnotationBatchIn,
    AnnotationIn,
    AnnotatorIn,
    DocumentIn,
    LabelIn,
    LabelRuleIn,
    OverrideIn,
    ProjectIn,
)
from .solver import (
    ALGORITHM_NAME,
    ALGORITHM_VERSION,
    SCORE_SCALE,
    TIE_BLOCK_SIZE,
    InputAnnotation,
    SolveParams,
    solve_consensus,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="多标注语料冲突裁决台", version=ALGORITHM_VERSION,
              lifespan=_lifespan)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def row(r):
    return dict(r) if r is not None else None


def get_project(conn, project_id: int):
    r = conn.execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"项目 {project_id} 不存在")
    return r


def latest_revision(conn, project_id: int, doc_id: str):
    r = conn.execute(
        "SELECT * FROM document_revisions WHERE project_id=? AND doc_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (project_id, doc_id),
    ).fetchone()
    if r is None:
        raise HTTPException(404, f"文档 {doc_id} 尚未导入")
    return r


def labels_map(conn, project_id: int) -> dict[int, dict]:
    return {
        r["id"]: row(r)
        for r in conn.execute(
            "SELECT * FROM labels WHERE project_id=?", (project_id,)
        )
    }


def annotators_map(conn, project_id: int) -> dict[int, dict]:
    return {
        r["id"]: row(r)
        for r in conn.execute(
            "SELECT * FROM annotators WHERE project_id=?", (project_id,)
        )
    }


def validate_token_bounds(content_len: int, bounds: list[list[int]]) -> None:
    if not bounds:
        raise HTTPException(400, "词元边界不能为空：至少需要一个词元")
    prev_end = -1
    for i, (s, e) in enumerate(bounds):
        if not (isinstance(s, int) and isinstance(e, int)):
            raise HTTPException(400, f"词元 #{i} 边界必须为整数")
        if s < 0 or e > content_len or s >= e:
            raise HTTPException(400, f"词元 #{i} [{s},{e}) 越界或为空")
        if s < prev_end:
            raise HTTPException(400, f"词元 #{i} 与前面的词元重叠或乱序")
        prev_end = e


def validate_span(start: int, end: int, char_count: int, token_bounds) -> None:
    """区间必须：在文档范围内、非空、端点落在词元边界上、不切开词元。"""
    if start < 0 or end > char_count or start >= end:
        raise HTTPException(400, f"区间 [{start},{end}) 越界或为空")
    boundaries = {0, char_count}
    for ts, te in token_bounds:
        boundaries.add(ts)
        boundaries.add(te)
    if start not in boundaries or end not in boundaries:
        raise HTTPException(400, f"区间 [{start},{end}) 端点未对齐词元边界")
    for ts, te in token_bounds:
        # 区间与词元相交但未完整包含该词元，即切开词元
        if start < te and end > ts and not (start <= ts and te <= end):
            raise HTTPException(
                400, f"区间 [{start},{end}) 切开了词元 [{ts},{te})"
            )


def build_snapshot(conn, project_id: int, doc) -> dict[str, Any]:
    """求共识时刻的完整输入快照（此后任何修改都与该快照无关）。"""
    rev = doc["revision"]
    labels = [
        row(r)
        for r in conn.execute(
            "SELECT id,name,color FROM labels WHERE project_id=? ORDER BY id",
            (project_id,),
        )
    ]
    nesting = [
        [r["outer_label_id"], r["inner_label_id"]]
        for r in conn.execute(
            "SELECT outer_label_id, inner_label_id FROM label_nesting WHERE project_id=?",
            (project_id,),
        )
    ]
    mutex = [
        sorted([r["label_a_id"], r["label_b_id"]])
        for r in conn.execute(
            "SELECT label_a_id, label_b_id FROM label_mutex WHERE project_id=?",
            (project_id,),
        )
    ]
    annotators = [
        row(r)
        for r in conn.execute(
            "SELECT id,name,weight FROM annotators WHERE project_id=? ORDER BY id",
            (project_id,),
        )
    ]
    annotations = [
        row(r)
        for r in conn.execute(
            "SELECT id,annotator_id,label_id,start,end FROM annotations "
            "WHERE project_id=? AND doc_id=? AND revision=? ORDER BY id",
            (project_id, doc["doc_id"], rev),
        )
    ]
    return {
        "doc_id": doc["doc_id"],
        "revision": rev,
        "content": doc["content"],
        "token_bounds": json.loads(doc["token_bounds"]),
        "char_count": doc["char_count"],
        "labels": labels,
        "nesting": nesting,
        "mutex": mutex,
        "annotators": annotators,
        "annotations": annotations,
    }


def run_from_snapshot(snapshot: dict[str, Any], params: SolveParams,
                      force_in=None, force_out=None):
    labels = {l["id"]: l for l in snapshot["labels"]}
    annotators = {a["id"]: a for a in snapshot["annotators"]}
    inputs: list[InputAnnotation] = []
    for a in snapshot["annotations"]:
        if a["annotator_id"] not in annotators or a["label_id"] not in labels:
            continue
        ann = annotators[a["annotator_id"]]
        lab = labels[a["label_id"]]
        inputs.append(
            InputAnnotation(
                annotator_id=ann["id"],
                annotator_name=ann["name"],
                weight=ann["weight"],
                start=a["start"],
                end=a["end"],
                label_id=lab["id"],
                label_name=lab["name"],
            )
        )
    nesting = {(o, i) for o, i in snapshot["nesting"]}
    mutex = {tuple(p) for p in snapshot["mutex"]}
    return solve_consensus(
        inputs, nesting, mutex, params,
        force_selected_keys=force_in, force_deselected_keys=force_out,
    )


def persist_consensus(conn, project_id: int, snapshot, params: SolveParams,
                      result, overrides_applied) -> int:
    cur = conn.execute(
        "INSERT INTO consensuses (project_id,doc_id,revision,algorithm_name,"
        "algorithm_version,solver_status,total_score,raw_total_score,"
        "params_json,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id, snapshot["doc_id"], snapshot["revision"],
            ALGORITHM_NAME, ALGORITHM_VERSION, result.status,
            result.total_score, result.raw_total_score,
            db.dumps({
                "label_match_weight": params.label_match_weight,
                "respect_overrides": params.respect_overrides,
                "score_scale": SCORE_SCALE,
                "overlap_ratio": "2*intersection/(|A|+|B|) (Sorensen-Dice)",
                "overlap_quantization": "round(dice*score_scale)/score_scale",
                "support_rule": "same label & overlap>0; one annotation may support multiple candidates",
                "contribution": "weight * label_match_weight * quantize(dice) / score_scale",
                "tie_break": "lexicographic by (start,end,label_name,label_id)",
                "tie_break_method": (
                    "staged: maximize total score first, then fix it and maximize "
                    f"blocked power-of-two binary weights (block_size={TIE_BLOCK_SIZE}); "
                    "safe 64-bit integer coefficients for any candidate count"
                ),
            }),
            db.dumps(snapshot),
            db.utcnow(),
        ),
    )
    consensus_id = cur.lastrowid
    forced_set = set(result.forced)
    forced_out_set = set(result.forced_out)
    for c in result.candidates:
        conn.execute(
            "INSERT INTO consensus_items (consensus_id,candidate_idx,start,end,"
            "label_id,label_name,score,raw_score,adopted,forced,supporters_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                consensus_id, c.index, c.start, c.end, c.label_id, c.label_name,
                c.score, c.raw_score, 1 if result.chosen[c.index] else 0,
                1 if c.index in forced_set or c.index in forced_out_set else 0,
                db.dumps(c.supporters),
            ),
        )
    for ov in overrides_applied:
        conn.execute(
            "INSERT INTO overrides (consensus_id,action,start,end,label_id,"
            "reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                consensus_id, ov["action"], ov["start"], ov["end"],
                ov["label_id"], ov["reason"], db.utcnow(),
            ),
        )
    return consensus_id


def serialize_consensus(conn, consensus_id: int) -> dict[str, Any]:
    c = conn.execute(
        "SELECT * FROM consensuses WHERE id=?", (consensus_id,)
    ).fetchone()
    if c is None:
        raise HTTPException(404, "共识不存在")
    items = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM consensus_items WHERE consensus_id=? ORDER BY candidate_idx",
            (consensus_id,),
        )
    ]
    for it in items:
        it["supporters"] = json.loads(it.pop("supporters_json"))
        it["adopted"] = bool(it["adopted"])
        it["forced"] = bool(it["forced"])
    overrides = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM overrides WHERE consensus_id=? ORDER BY id",
            (consensus_id,),
        )
    ]
    reports = [
        {"id": r["id"], "version": r["version"], "created_at": r["created_at"]}
        for r in conn.execute(
            "SELECT id,version,created_at FROM reports WHERE consensus_id=? ORDER BY id",
            (consensus_id,),
        )
    ]
    return {
        **row(c),
        "params": json.loads(c["params_json"]),
        "items": items,
        "overrides": overrides,
        "reports": reports,
    }


# ---------------------------------------------------------------------------
# 项目 & 文档
# ---------------------------------------------------------------------------
@app.post("/api/projects", status_code=201)
def create_project(body: ProjectIn):
    with db.connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO projects (name,created_at) VALUES (?,?)",
                (body.name, db.utcnow()),
            )
        except Exception:
            raise HTTPException(409, f"项目 {body.name} 已存在")
        return {"id": cur.lastrowid, "name": body.name}


@app.get("/api/projects")
def list_projects():
    with db.connect() as conn:
        return [row(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]


@app.post("/api/projects/{project_id}/documents", status_code=201)
def import_document(project_id: int, body: DocumentIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        content = body.content
        if any(ord(ch) < 0x20 and ch not in "\n\r\t" for ch in content):
            raise HTTPException(400, "文档含非法控制字符，请使用 UTF-8 纯文本")
        char_count = len(content)
        if body.token_bounds is None:
            bounds = [[i, i + 1] for i in range(char_count)]
        else:
            bounds = [list(p) for p in body.token_bounds]
        validate_token_bounds(char_count, bounds)

        last = conn.execute(
            "SELECT MAX(revision) AS m FROM document_revisions WHERE project_id=? AND doc_id=?",
            (project_id, body.doc_id),
        ).fetchone()["m"]
        revision = (last or 0) + 1
        cur = conn.execute(
            "INSERT INTO document_revisions (project_id,doc_id,revision,content,"
            "token_bounds,char_count,created_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, body.doc_id, revision, content,
             db.dumps(bounds), char_count, db.utcnow()),
        )
        return {
            "id": cur.lastrowid, "doc_id": body.doc_id, "revision": revision,
            "char_count": char_count, "token_bounds": bounds,
            "notice": "新版本已追加；旧版本及既有报告保持不变" if last else None,
        }


@app.get("/api/projects/{project_id}/documents")
def list_documents(project_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        return [
            {
                "doc_id": r["doc_id"],
                "revision": r["revision"],
                "char_count": r["char_count"],
                "created_at": r["created_at"],
            }
            for r in conn.execute(
                "SELECT doc_id,revision,char_count,created_at FROM document_revisions "
                "ORDER BY doc_id, revision",
                (project_id,),
            )
        ]


@app.get("/api/projects/{project_id}/documents/{doc_id}")
def get_document(project_id: int, doc_id: str, revision: int | None = None):
    with db.connect() as conn:
        get_project(conn, project_id)
        if revision is None:
            d = latest_revision(conn, project_id, doc_id)
        else:
            d = conn.execute(
                "SELECT * FROM document_revisions WHERE project_id=? AND doc_id=? AND revision=?",
                (project_id, doc_id, revision),
            ).fetchone()
            if d is None:
                raise HTTPException(404, "该文档版本不存在")
        return {
            "doc_id": d["doc_id"], "revision": d["revision"],
            "content": d["content"],
            "token_bounds": json.loads(d["token_bounds"]),
            "char_count": d["char_count"],
            "created_at": d["created_at"],
        }


# ---------------------------------------------------------------------------
# 标签与规则
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/labels", status_code=201)
def create_label(project_id: int, body: LabelIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        try:
            cur = conn.execute(
                "INSERT INTO labels (project_id,name,color,created_at) VALUES (?,?,?,?)",
                (project_id, body.name, body.color, db.utcnow()),
            )
        except Exception:
            raise HTTPException(409, f"标签 {body.name} 已存在")
        return {"id": cur.lastrowid, "name": body.name, "color": body.color}


@app.get("/api/projects/{project_id}/labels")
def list_labels(project_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        labels = [row(r) for r in conn.execute(
            "SELECT * FROM labels WHERE project_id=? ORDER BY id", (project_id,))]
        nesting = [
            {"outer_label_id": r["outer_label_id"], "inner_label_id": r["inner_label_id"]}
            for r in conn.execute(
                "SELECT outer_label_id,inner_label_id FROM label_nesting WHERE project_id=?",
                (project_id,),
            )
        ]
        mutex = [
            {"label_a_id": r["label_a_id"], "label_b_id": r["label_b_id"]}
            for r in conn.execute(
                "SELECT label_a_id,label_b_id FROM label_mutex WHERE project_id=?",
                (project_id,),
            )
        ]
        return {"labels": labels, "nesting": nesting, "mutex": mutex}


@app.post("/api/projects/{project_id}/label-rules", status_code=201)
def add_label_rule(project_id: int, body: LabelRuleIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        labs = labels_map(conn, project_id)
        if body.label_a_id not in labs or body.label_b_id not in labs:
            raise HTTPException(400, "标签不存在")
        if body.label_a_id == body.label_b_id:
            raise HTTPException(400, "规则必须作用于两个不同标签（同标签天然不可重叠共存）")
        if body.kind == "nesting":
            conn.execute(
                "INSERT OR IGNORE INTO label_nesting (project_id,outer_label_id,inner_label_id) "
                "VALUES (?,?,?)",
                (project_id, body.label_a_id, body.label_b_id),
            )
            return {"kind": "nesting", "outer_label_id": body.label_a_id,
                    "inner_label_id": body.label_b_id}
        a, b = sorted((body.label_a_id, body.label_b_id))
        conn.execute(
            "INSERT OR IGNORE INTO label_mutex (project_id,label_a_id,label_b_id) VALUES (?,?,?)",
            (project_id, a, b),
        )
        return {"kind": "mutex", "label_a_id": a, "label_b_id": b}


@app.delete("/api/projects/{project_id}/label-rules")
def remove_label_rule(project_id: int, kind: str, a: int, b: int):
    if kind not in ("nesting", "mutex"):
        raise HTTPException(400, "kind 必须是 nesting 或 mutex")
    with db.connect() as conn:
        get_project(conn, project_id)
        if kind == "nesting":
            conn.execute(
                "DELETE FROM label_nesting WHERE project_id=? AND outer_label_id=? AND inner_label_id=?",
                (project_id, a, b),
            )
        else:
            lo, hi = sorted((a, b))
            conn.execute(
                "DELETE FROM label_mutex WHERE project_id=? AND label_a_id=? AND label_b_id=?",
                (project_id, lo, hi),
            )
        return {"deleted": True}


# ---------------------------------------------------------------------------
# 标注者
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/annotators", status_code=201)
def create_annotator(project_id: int, body: AnnotatorIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM annotators WHERE project_id=?", (project_id,)
        ).fetchone()["n"]
        if count >= 8:
            raise HTTPException(400, "每个项目最多 8 名标注者")
        try:
            cur = conn.execute(
                "INSERT INTO annotators (project_id,name,weight,created_at) VALUES (?,?,?,?)",
                (project_id, body.name, body.weight, db.utcnow()),
            )
        except Exception:
            raise HTTPException(409, f"标注者 {body.name} 已存在")
        return {"id": cur.lastrowid, "name": body.name, "weight": body.weight}


@app.get("/api/projects/{project_id}/annotators")
def list_annotators(project_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        return [row(r) for r in conn.execute(
            "SELECT * FROM annotators WHERE project_id=? ORDER BY id", (project_id,))]


@app.patch("/api/projects/{project_id}/annotators/{annotator_id}")
def update_annotator_weight(project_id: int, annotator_id: int, weight: int):
    if not 1 <= weight <= 10:
        raise HTTPException(400, "权重必须在 1~10 之间")
    with db.connect() as conn:
        get_project(conn, project_id)
        cur = conn.execute(
            "UPDATE annotators SET weight=? WHERE id=? AND project_id=?",
            (weight, annotator_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "标注者不存在")
        return {"id": annotator_id, "weight": weight}


@app.delete("/api/projects/{project_id}/annotators/{annotator_id}")
def delete_annotator(project_id: int, annotator_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        conn.execute(
            "DELETE FROM annotations WHERE project_id=? AND annotator_id=?",
            (project_id, annotator_id),
        )
        cur = conn.execute(
            "DELETE FROM annotators WHERE id=? AND project_id=?",
            (annotator_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "标注者不存在")
        return {"deleted": True}


# ---------------------------------------------------------------------------
# 标注
# ---------------------------------------------------------------------------
def _prepare_annotation(conn, project_id, doc, body: AnnotationIn) -> None:
    bounds = [tuple(p) for p in json.loads(doc["token_bounds"])]
    validate_span(body.start, body.end, doc["char_count"], bounds)
    if body.label_id not in labels_map(conn, project_id):
        raise HTTPException(400, "标签不存在于本项目")
    if body.annotator_id not in annotators_map(conn, project_id):
        raise HTTPException(400, "标注者不存在于本项目")


@app.post("/api/projects/{project_id}/documents/{doc_id}/annotations", status_code=201)
def add_annotation(project_id: int, doc_id: str, body: AnnotationIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        doc = latest_revision(conn, project_id, doc_id)
        _prepare_annotation(conn, project_id, doc, body)
        cur = conn.execute(
            "INSERT INTO annotations (project_id,doc_id,revision,annotator_id,"
            "label_id,start,end,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, doc_id, doc["revision"], body.annotator_id,
             body.label_id, body.start, body.end, db.utcnow()),
        )
        return {"id": cur.lastrowid, "doc_id": doc_id, "revision": doc["revision"],
                "annotator_id": body.annotator_id, "label_id": body.label_id,
                "start": body.start, "end": body.end}


@app.post("/api/projects/{project_id}/documents/{doc_id}/annotations/batch", status_code=201)
def add_annotation_batch(project_id: int, doc_id: str, body: AnnotationBatchIn):
    with db.connect() as conn:
        get_project(conn, project_id)
        doc = latest_revision(conn, project_id, doc_id)
        created = []
        for item in body.annotations:
            _prepare_annotation(conn, project_id, doc, item)
            cur = conn.execute(
                "INSERT INTO annotations (project_id,doc_id,revision,annotator_id,"
                "label_id,start,end,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (project_id, doc_id, doc["revision"], item.annotator_id,
                 item.label_id, item.start, item.end, db.utcnow()),
            )
            created.append(cur.lastrowid)
        return {"created_ids": created, "count": len(created),
                "revision": doc["revision"]}


@app.get("/api/projects/{project_id}/documents/{doc_id}/annotations")
def list_annotations(project_id: int, doc_id: str):
    with db.connect() as conn:
        get_project(conn, project_id)
        doc = latest_revision(conn, project_id, doc_id)
        rows = [
            row(r)
            for r in conn.execute(
                "SELECT a.*, l.name AS label_name, l.color AS color, "
                "u.name AS annotator_name, u.weight AS weight "
                "FROM annotations a JOIN labels l ON a.label_id=l.id "
                "JOIN annotators u ON a.annotator_id=u.id "
                "WHERE a.project_id=? AND a.doc_id=? AND a.revision=? ORDER BY a.id",
                (project_id, doc_id, doc["revision"]),
            )
        ]
        return {"revision": doc["revision"], "annotations": rows}


@app.delete("/api/projects/{project_id}/annotations/{annotation_id}")
def delete_annotation(project_id: int, annotation_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        cur = conn.execute(
            "DELETE FROM annotations WHERE id=? AND project_id=?",
            (annotation_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "标注不存在")
        return {"deleted": True}


# ---------------------------------------------------------------------------
# 共识求解
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/documents/{doc_id}/consensus", status_code=201)
def solve(project_id: int, doc_id: str):
    with db.connect() as conn:
        get_project(conn, project_id)
        doc = latest_revision(conn, project_id, doc_id)
        snapshot = build_snapshot(conn, project_id, doc)

        used = {a["annotator_id"] for a in snapshot["annotations"]}
        if not (2 <= len(used) <= 8):
            raise HTTPException(
                400,
                f"参与本稿标注的标注者必须为 2~8 名，当前为 {len(used)} 名",
            )
        if not snapshot["annotations"]:
            raise HTTPException(400, "尚无标注，无法求共识")

        params = SolveParams()
        result = run_from_snapshot(snapshot, params)
        if result.status not in ("OPTIMAL", "FEASIBLE"):
            raise HTTPException(
                409,
                "求解器未能找到可行共识（模型不可行或无效）；"
                "请检查嵌套/互斥标签规则与标注区间后重试",
            )
        cid = persist_consensus(conn, project_id, snapshot, params, result, [])
        return serialize_consensus(conn, cid)


@app.get("/api/projects/{project_id}/consensuses/{consensus_id}")
def get_consensus(project_id: int, consensus_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        return serialize_consensus(conn, consensus_id)


@app.get("/api/projects/{project_id}/documents/{doc_id}/consensuses")
def list_consensuses(project_id: int, doc_id: str):
    with db.connect() as conn:
        get_project(conn, project_id)
        return [
            {
                "id": r["id"], "revision": r["revision"],
                "total_score": r["total_score"],
                "raw_total_score": r["raw_total_score"],
                "solver_status": r["solver_status"],
                "algorithm_version": r["algorithm_version"],
                "created_at": r["created_at"],
            }
            for r in conn.execute(
                "SELECT * FROM consensuses WHERE project_id=? AND doc_id=? ORDER BY id",
                (project_id, doc_id),
            )
        ]


@app.post("/api/projects/{project_id}/consensuses/{consensus_id}/override", status_code=201)
def add_override(project_id: int, consensus_id: int, body: OverrideIn):
    """追加裁决者覆盖并基于同一快照重新求解；产生一个新的不可变共识版本。"""
    with db.connect() as conn:
        get_project(conn, project_id)
        old = conn.execute(
            "SELECT * FROM consensuses WHERE id=? AND project_id=?",
            (consensus_id, project_id),
        ).fetchone()
        if old is None:
            raise HTTPException(404, "共识不存在")
        snapshot = json.loads(old["snapshot_json"])

        # 校验覆盖目标在快照范围内、不切开词元
        bounds = [tuple(p) for p in snapshot["token_bounds"]]
        validate_span(body.start, body.end, snapshot["char_count"], bounds)
        label_ids = {l["id"] for l in snapshot["labels"]}
        if body.label_id not in label_ids:
            raise HTTPException(400, "标签不在该共识的输入快照中")

        prior_overrides = [
            dict(r)
            for r in conn.execute(
                "SELECT action,start,end,label_id,reason FROM overrides "
                "WHERE consensus_id=? ORDER BY id",
                (consensus_id,),
            )
        ]
        key = (body.start, body.end, body.label_id)
        # 同一目标上重复的相反指令 => 必然不可行
        conflicting = any(
            (o["start"], o["end"], o["label_id"]) == key
            and o["action"] != body.action
            for o in prior_overrides
        )
        if conflicting:
            raise HTTPException(409, "该区间已有相反的覆盖指令，约束不可同时满足")

        force_in, force_out = set(), set()
        all_overrides = prior_overrides + [body.model_dump()]
        for o in all_overrides:
            k = (o["start"], o["end"], o["label_id"])
            (force_in if o["action"] == "select" else force_out).add(k)
        force_in -= force_out  # 后者（理论上不会同时出现）防御

        params = SolveParams()
        result = run_from_snapshot(snapshot, params, force_in, force_out)
        if result.status not in ("OPTIMAL", "FEASIBLE"):
            raise HTTPException(
                409,
                "覆盖与嵌套/互斥约束冲突，求解器找不到可行共识；请调整覆盖后重试",
            )
        new_id = persist_consensus(
            conn, project_id, snapshot, params, result, all_overrides
        )
        return serialize_consensus(conn, new_id)


# ---------------------------------------------------------------------------
# 不可变报告
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/consensuses/{consensus_id}/report", status_code=201)
def create_report(project_id: int, consensus_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        consensus = serialize_consensus(conn, consensus_id)  # 校验存在
        snapshot = json.loads(
            conn.execute(
                "SELECT snapshot_json FROM consensuses WHERE id=?", (consensus_id,)
            ).fetchone()["snapshot_json"]
        )
        version = f"rpt-{consensus_id}-{uuid.uuid4().hex[:12]}"
        payload = {
            "report_version": version,
            "generated_at": db.utcnow(),
            "project_id": project_id,
            "consensus_id": consensus_id,
            "algorithm": {
                "name": consensus["algorithm_name"],
                "version": consensus["algorithm_version"],
                "app_version": app.version,
            },
            "parameters": consensus["params"],
            "solver": {
                "status": consensus["solver_status"],
                "total_score": consensus["total_score"],
                "raw_total_score": consensus["raw_total_score"],
            },
            "input_snapshot": snapshot,
            "consensus": {
                "adopted": [it for it in consensus["items"] if it["adopted"]],
                "rejected": [it for it in consensus["items"] if not it["adopted"]],
                "all_items": consensus["items"],
            },
            "overrides": consensus["overrides"],
            "immutability": (
                "本报告内容经 SHA-256 摘要固化并写入只追加表；"
                "重新导入文档或修改标签规则均生成新版本，不改写本报告。"
            ),
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        try:
            cur = conn.execute(
                "INSERT INTO reports (project_id,consensus_id,version,"
                "content_hash,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (project_id, consensus_id, version, content_hash,
                 payload_json, db.utcnow()),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"报告写入失败：{exc}")
        return {"id": cur.lastrowid, "version": version,
                "content_hash": content_hash, **payload}


@app.get("/api/projects/{project_id}/reports")
def list_reports(project_id: int):
    with db.connect() as conn:
        get_project(conn, project_id)
        return [
            {"id": r["id"], "consensus_id": r["consensus_id"],
             "version": r["version"], "content_hash": r["content_hash"],
             "created_at": r["created_at"]}
            for r in conn.execute(
                "SELECT id,consensus_id,version,content_hash,created_at FROM reports "
                "WHERE project_id=? ORDER BY id", (project_id,))
        ]


@app.get("/api/projects/{project_id}/reports/{version}")
def get_report(project_id: int, version: str):
    with db.connect() as conn:
        get_project(conn, project_id)
        r = conn.execute(
            "SELECT payload_json,content_hash FROM reports WHERE project_id=? AND version=?",
            (project_id, version),
        ).fetchone()
        if r is None:
            raise HTTPException(404, "报告不存在（报告一经创建永不删除）")
        return JSONResponse(json.loads(r["payload_json"]))


# ---------------------------------------------------------------------------
# 前端静态资源
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/")
def root():
    return RedirectResponse("/static/index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
