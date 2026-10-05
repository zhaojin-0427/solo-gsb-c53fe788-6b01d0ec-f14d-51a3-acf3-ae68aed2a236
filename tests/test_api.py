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


def test_sixty_min_weight_candidates_end_to_end():
    """大候选数回归：60 个合法候选、每个仅一条权重 1 的自身标注。
    旧单项目标系数 10×2^60 超出 int64，求解报 MODEL_INVALID；
    修复后接口应返回 OPTIMAL 且全部入选。"""
    pid = client.post("/api/projects", json={"name": "sixty-candidates"}).json()["id"]
    client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "d60", "content": "x" * 120,
        "token_bounds": [[i, i + 1] for i in range(120)]})
    lab = client.post(f"/api/projects/{pid}/labels",
                      json={"name": "ENTITY", "color": "#2563eb"}).json()["id"]
    u1 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "甲", "weight": 1}).json()["id"]
    u2 = client.post(f"/api/projects/{pid}/annotators",
                     json={"name": "乙", "weight": 1}).json()["id"]
    batch = [{"annotator_id": u1 if i % 2 == 0 else u2, "label_id": lab,
              "start": 2 * i, "end": 2 * i + 2} for i in range(60)]
    r = client.post(f"/api/projects/{pid}/documents/d60/annotations/batch",
                    json={"annotations": batch})
    assert r.status_code == 201, r.text

    con = client.post(f"/api/projects/{pid}/documents/d60/consensus", json={}).json()
    assert con["solver_status"] == "OPTIMAL"
    assert len(con["items"]) == 60
    assert all(i["adopted"] for i in con["items"])
    assert con["total_score"] == 600


def test_large_candidate_consensus_override_and_report():
    """大候选数回归（n=70）：求解、带理由覆盖、报告全链路正常；
    总分平分时按 (起点,终点,标签) 字典序确定胜者，不靠限制候选数。"""
    pid = client.post("/api/projects", json={"name": "large-candidates"}).json()["id"]
    client.post(f"/api/projects/{pid}/documents", json={
        "doc_id": "big", "content": "x" * 100,
        "token_bounds": [[i, i + 1] for i in range(100)]})
    lab = client.post(f"/api/projects/{pid}/labels",
                      json={"name": "ENTITY", "color": "#2563eb"}).json()["id"]
    uids = [
        client.post(f"/api/projects/{pid}/annotators",
                    json={"name": f"u{k}", "weight": w}).json()["id"]
        for k, w in enumerate([10, 10, 10, 10, 10, 10, 9, 1])
    ]
    # 7 名高权重标注者一致标 [0,100)（得分 690）；
    # 第 8 名（权重 1）标 69 个互不冲突的小区间（各 10 分，共 690）=> 总分平分
    batch = [{"annotator_id": uids[k], "label_id": lab, "start": 0, "end": 100}
             for k in range(7)]
    batch += [{"annotator_id": uids[7], "label_id": lab,
               "start": i, "end": i + 1} for i in range(1, 70)]
    r = client.post(f"/api/projects/{pid}/documents/big/annotations/batch",
                    json={"annotations": batch})
    assert r.status_code == 201, r.text

    con = client.post(f"/api/projects/{pid}/documents/big/consensus", json={}).json()
    assert con["solver_status"] == "OPTIMAL"
    assert len(con["items"]) == 70          # 候选不截断、不限制数量
    adopted = [(i["start"], i["end"]) for i in con["items"] if i["adopted"]]
    assert adopted == [(0, 100)]            # 平分字典序：排序最前者胜
    assert con["total_score"] == 690

    # 带理由覆盖：强制否决 [0,100) => 同一快照重解，69 个小区间入选
    con2 = client.post(
        f"/api/projects/{pid}/consensuses/{con['id']}/override", json={
            "action": "deselect", "start": 0, "end": 100, "label_id": lab,
            "reason": "裁决：改采全部小区间"}).json()
    adopted2 = {(i["start"], i["end"]) for i in con2["items"] if i["adopted"]}
    assert len(adopted2) == 69 and (0, 100) not in adopted2
    assert con2["total_score"] == 690
    assert con2["id"] != con["id"]

    # 大候选共识可生成不可变报告，快照与明细完整
    rep = client.post(
        f"/api/projects/{pid}/consensuses/{con2['id']}/report", json={}).json()
    assert rep["solver"]["total_score"] == 690
    assert len(rep["input_snapshot"]["annotations"]) == 76
    assert len(rep["consensus"]["all_items"]) == 70
    assert len(rep["consensus"]["adopted"]) == 69
