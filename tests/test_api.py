"""端到端 API 测试：导入→规则→标注者→标注→共识→覆盖→报告→不可变性。"""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["ADJ_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402

database.init_db()
client = TestClient(app)


@pytest.fixture(scope="module")
def setup():
    p = client.post("/api/projects", json={"name": "e2e"}).json()
    pid = p["id"]
    content = "张三和李四在上海开会。"
    # 词元：张三/和/李四/在/上海/开会/。
    words = ["张三", "和", "李四", "在", "上海", "开会", "。"]
    bounds, at = [], 0
    for w in words:
        bounds.append([at, at + len(w)])
        at += len(w)

    doc = client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "d1", "content": content, "token_bounds": bounds,
    }).json()
    assert doc["revision"] == 1

    person = client.post(f"/api/projects/{pid}/labels",
                         json={"name": "PERSON", "color": "#2563eb"}).json()["id"]
    city = client.post(f"/api/projects/{pid}/labels",
                       json={"name": "CITY", "color": "#0891b2"}).json()["id"]
    org = client.post(f"/api/projects/{pid}/labels",
                      json={"name": "ORG", "color": "#db2777"}).json()["id"]
    assert person < city
    r = client.post(f"/api/projects/{pid}/label-rules", json={
        "kind": "mutex", "label_a_id": person, "label_b_id": org})
    assert r.status_code == 201

    a = client.post(f"/api/projects/{pid}/annotators",
                    json={"name": "甲", "weight": 9}).json()["id"]
    b = client.post(f"/api/projects/{pid}/annotators",
                    json={"name": "乙", "weight": 4}).json()["id"]
    c = client.post(f"/api/projects/{pid}/annotators",
                    json={"name": "丙", "weight": 2}).json()["id"]
    return {
        "pid": pid, "content": content, "bounds": bounds,
        "person": person, "city": city, "org": org,
        "a": a, "b": b, "c": c,
    }


def test_weight_bounds(setup):
    r = client.post(f"/api/projects/{setup['pid']}/annotators",
                    json={"name": "超范围", "weight": 11})
    assert r.status_code == 422


def test_reject_out_of_bounds_and_token_cutting(setup):
    pid, doc = setup["pid"], "d1"
    # 越界
    r = client.post(f"/api/projects/{pid}/documents/{doc}/annotations", json={
        "annotator_id": setup["a"], "label_id": setup["person"],
        "start": 0, "end": 99})
    assert r.status_code == 400 and "越界" in r.json()["detail"]
    # 切开词元「张三」[0,2)：端点 1 不是词元边界
    r = client.post(f"/api/projects/{pid}/documents/{doc}/annotations", json={
        "annotator_id": setup["a"], "label_id": setup["person"],
        "start": 0, "end": 1})
    assert r.status_code == 400 and "词元" in r.json()["detail"]


def post_ann(pid, who, label, s, e):
    r = client.post(f"/api/projects/{pid}/documents/d1/annotations", json={
        "annotator_id": who, "label_id": label, "start": s, "end": e})
    assert r.status_code == 201, r.text


def test_consensus_flow_and_override(setup):
    pid = setup["pid"]
    # 张三[0,2)：甲乙 PERSON vs 丙 ORG（互斥）
    post_ann(pid, setup["a"], setup["person"], 0, 2)
    post_ann(pid, setup["b"], setup["person"], 0, 2)
    post_ann(pid, setup["c"], setup["org"], 0, 2)
    # 上海[6,8)：三人一致 CITY
    post_ann(pid, setup["a"], setup["city"], 6, 8)
    post_ann(pid, setup["b"], setup["city"], 6, 8)
    post_ann(pid, setup["c"], setup["city"], 6, 8)

    con = client.post(f"/api/projects/{pid}/documents/d1/consensus",
                      json={}).json()
    adopted = {(i["start"], i["end"], i["label_name"]) for i in con["items"] if i["adopted"]}
    rejected = [(i["start"], i["end"], i["label_name"]) for i in con["items"] if not i["adopted"]]
    assert (0, 2, "PERSON") in adopted
    assert (0, 2, "ORG") in rejected
    assert (6, 8, "CITY") in adopted
    # 拒绝项有支持者明细
    org_item = next(i for i in con["items"] if (i["start"], i["end"], i["label_name"]) == (0, 2, "ORG"))
    assert org_item["supporters"][0]["annotator_name"] == "丙"
    cid1 = con["id"]

    # 裁决者强制采纳 ORG（低权重少数派），必须给理由
    r = client.post(f"/api/projects/{pid}/consensuses/{cid1}/override", json={
        "action": "select", "start": 0, "end": 2,
        "label_id": setup["org"], "reason": ""})
    assert r.status_code == 422  # 理由必填

    con2 = client.post(f"/api/projects/{pid}/consensuses/{cid1}/override", json={
        "action": "select", "start": 0, "end": 2,
        "label_id": setup["org"],
        "reason": "外部知识库确认此处为机构简称"}).json()
    adopted2 = {(i["start"], i["end"], i["label_name"]) for i in con2["items"] if i["adopted"]}
    assert (0, 2, "ORG") in adopted2
    assert (0, 2, "PERSON") not in adopted2
    assert con2["id"] != cid1  # 新版本，旧共识保留

    old = client.get(f"/api/projects/{pid}/consensuses/{cid1}").json()
    assert any(i["adopted"] and (i["start"], i["end"], i["label_name"]) == (0, 2, "PERSON")
               for i in old["items"])
    globals()["_OVERRIDDEN_CONSENSUS"] = con2["id"]


def test_report_immutable(setup):
    pid = setup["pid"]
    cid = globals()["_OVERRIDDEN_CONSENSUS"]
    rep = client.post(f"/api/projects/{pid}/consensuses/{cid}/report", json={}).json()
    version = rep["report_version"]
    assert rep["algorithm"]["version"]
    assert rep["input_snapshot"]["annotations"]
    assert rep["parameters"]["score_scale"] == 10

    # 重新导入同名文档 => revision 2；旧报告仍可打开且内容指向 revision 1
    r = client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "d1", "content": setup["content"] + "追加",
        "token_bounds": setup["bounds"] + [[11, 13]]})
    assert r.json()["revision"] == 2

    again = client.get(f"/api/projects/{pid}/reports/{version}").json()
    assert again["input_snapshot"]["revision"] == 1
    assert again["report_version"] == version

    # 数据库层禁止 UPDATE/DELETE
    import sqlite3
    conn = sqlite3.connect(os.environ["ADJ_DB_PATH"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE reports SET payload_json='x' WHERE version=?", (version,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM reports WHERE version=?", (version,))
    conn.close()


def test_partial_overlap_scoring_end_to_end():
    """同标签、边界不同但相交的标注：部分重叠支持计入得分，
    共识页明细与报告中的贡献/得分/算法参数一致。"""
    pid = client.post("/api/projects", json={"name": "partial-overlap"}).json()["id"]
    bounds = [[i, i + 1] for i in range(8)]
    client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "d1", "content": "abcdefgh", "token_bounds": bounds})
    lab = client.post(f"/api/projects/{pid}/labels",
                      json={"name": "ENTITY", "color": "#2563eb"}).json()["id"]
    u1 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "甲", "weight": 10}).json()["id"]
    u2 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "乙", "weight": 8}).json()["id"]
    u3 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "丙", "weight": 6}).json()["id"]
    for who, s, e in [(u1, 0, 4), (u2, 2, 6), (u3, 3, 5)]:
        r = client.post(f"/api/projects/{pid}/documents/d1/annotations", json={
            "annotator_id": who, "label_id": lab, "start": s, "end": e})
        assert r.status_code == 201, r.text

    con = client.post(f"/api/projects/{pid}/documents/d1/consensus", json={}).json()
    items = {(i["start"], i["end"]): i for i in con["items"]}
    # 候选仍按 (start,end,label) 去重：3 个候选
    assert len(con["items"]) == 3
    # 逐支持者量化计分（dice 0.5→0.5, 1/3→0.3, 2/3→0.7）
    assert items[(0, 4)]["score"] == 158
    assert items[(2, 6)]["score"] == 172
    assert items[(3, 5)]["score"] == 146
    # 部分重叠支持使 [2,6) 反超 [0,4)
    assert items[(2, 6)]["adopted"] is True
    assert items[(0, 4)]["adopted"] is False
    # 同一标注支持多个候选；Σ贡献 = 展示分；整数目标 = 展示分 × scale
    for it in con["items"]:
        assert len(it["supporters"]) == 3
        assert abs(sum(s["contribution"] for s in it["supporters"])
                   - it["raw_score"]) < 1e-9
        assert abs(it["raw_score"] * 10 - it["score"]) < 1e-9
    assert con["raw_total_score"] == 17.2
    # 实际算法参数与版本随共识返回
    assert con["algorithm_version"]
    assert con["params"]["score_scale"] == 10
    assert "quantize" in con["params"]["contribution"]

    # 报告与共识页一致：同样的得分、支持者贡献、参数与版本
    rep = client.post(
        f"/api/projects/{pid}/consensuses/{con['id']}/report", json={}).json()
    assert rep["algorithm"]["version"] == con["algorithm_version"]
    assert rep["parameters"] == con["params"]
    assert rep["solver"]["raw_total_score"] == con["raw_total_score"]
    adopted = rep["consensus"]["adopted"]
    assert len(adopted) == 1 and (adopted[0]["start"], adopted[0]["end"]) == (2, 6)
    assert adopted[0]["score"] == 172
    assert {s["annotator_name"] for s in adopted[0]["supporters"]} == {"甲", "乙", "丙"}
    assert abs(sum(s["contribution"] for s in adopted[0]["supporters"])
               - adopted[0]["raw_score"]) < 1e-9


def test_large_candidate_consensus_end_to_end():
    """60+ 候选（每候选至少一条权重 1 的自身标注）端到端：
    旧版在此规模下 CP-SAT 模型 INVALID；修复后应求出最优共识、
    支持覆盖并生成不可变报告。"""
    pid = client.post("/api/projects", json={"name": "large-candidates"}).json()["id"]
    n = 65
    client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "big",
        "content": "ab" * n,
        "token_bounds": [[2 * k, 2 * k + 2] for k in range(n)],
    })
    lab = client.post(f"/api/projects/{pid}/labels",
                      json={"name": "ENT", "color": "#2563eb"}).json()["id"]
    u1 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "子", "weight": 1}).json()["id"]
    u2 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "丑", "weight": 1}).json()["id"]
    r = client.post(f"/api/projects/{pid}/documents/big/annotations/batch", json={
        "annotations": [
            {"annotator_id": u1, "label_id": lab, "start": 2 * k, "end": 2 * k + 2}
            for k in range(n)
        ] + [
            {"annotator_id": u2, "label_id": lab, "start": 2 * k, "end": 2 * k + 2}
            for k in range(n)
        ],
    })
    assert r.status_code == 201, r.text

    con = client.post(f"/api/projects/{pid}/documents/big/consensus", json={})
    assert con.status_code == 201, con.text
    body = con.json()
    assert body["solver_status"] == "OPTIMAL"
    assert len(body["items"]) == n
    assert all(it["adopted"] for it in body["items"])
    # 每候选两名权重 1 标注者：展示分 2.0，整数分 20
    assert all(it["score"] == 20 and it["raw_score"] == 2.0 for it in body["items"])
    assert body["raw_total_score"] == round(2.0 * n, 4)
    assert "tie_break_method" in body["params"]

    # 覆盖：否决第一个候选后重新求解，其余仍全入选
    ov = client.post(
        f"/api/projects/{pid}/consensuses/{body['id']}/override", json={
            "action": "deselect", "start": 0, "end": 2, "label_id": lab,
            "reason": "大候选数下的覆盖回归",
        })
    assert ov.status_code == 201, ov.text
    ob = ov.json()
    assert ob["solver_status"] == "OPTIMAL"
    assert sum(1 for it in ob["items"] if it["adopted"]) == n - 1

    # 报告可生成、读取，且分阶段算法参数随报告固化
    rep = client.post(
        f"/api/projects/{pid}/consensuses/{ob['id']}/report", json={})
    assert rep.status_code == 201, rep.text
    rb = rep.json()
    got = client.get(f"/api/projects/{pid}/reports/{rb['report_version']}").json()
    assert got["algorithm"]["version"] == body["algorithm_version"]
    assert "tie_break_method" in got["parameters"]
    assert len(got["consensus"]["adopted"]) == n - 1
