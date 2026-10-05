# 多标注语料冲突裁决台（Multi-Annotator Adjudication Console）

多名标注者对同一篇 UTF-8 文档提交字符区间标注后，本系统使用
**OR-Tools CP-SAT** 在「嵌套 / 互斥」标签约束下求出**总加权得分最大**的
确定性共识；裁决者可逐条查看未采纳标注及其支持者、带理由地强制覆盖，
并生成**带输入快照、算法参数与版本号、SHA-256 固化的不可变报告**。

- 后端：Python · FastAPI · SQLite · OR-Tools（CP-SAT）
- 前端：原生 HTML / CSS / JavaScript（无构建、无框架）
- 编排：Docker Compose（SQLite 数据卷持久化）

## 功能对照

| 需求 | 实现 |
| --- | --- |
| 导入 UTF-8 文档及词元边界 | `POST /api/projects/{id}/documents`，校验边界连续、非空、不越界；未提供时按单字符切分 |
| 可嵌套 / 互斥标签 | 标签规则接口；嵌套为「外层 ⊃ 内层」有向关系，互斥为无序对 |
| 标注者权重 1～10 | 整数权重 CHECK 约束，2～8 名标注者参与才可求解 |
| 拒绝越界 / 切开词元的区间 | 提交时校验 `[start,end)` 在范围内、端点对齐词元边界、不切开任何词元 |
| 得分 = 重叠比例 × 标签一致 × 权重 | Sørensen–Dice 对称重叠比 `2|∩|/(|A|+|B|)`；同标签且相交（含部分重叠）的标注均为候选贡献，同一标注可支持多个候选（参数快照进报告） |
| 满足约束的最大得分共识 | CP-SAT 二元候选选择模型，重叠区间仅在「严格包含且允许嵌套」时可共存 |
| 平分确定性 | 单线程固定随机种子 + 次级目标：按 `(起点, 终点, 标签名, 标签ID)` 字典序优先 |
| 展示未采纳标注及支持者 | 工作台逐条列出区间、文本、得分、每位支持者的权重/重叠比/贡献分 |
| 裁决者覆盖 + 理由 | 强制采纳/否决必须填写理由；覆盖与约束冲突时求解器报 409；每次覆盖基于**同一输入快照**生成新共识版本 |
| 不可变报告 | 报告含输入快照、算法参数、算法版本、应用版本与 SHA-256；`reports` 表在 SQLite 触发器层禁止 UPDATE/DELETE；报告页浏览器端复算摘要 |
| 重新导入 / 改规则不改旧报告 | 文档按 `(doc_id, revision)` 追加；共识求解时整包快照；标签/权重修改只影响此后的新求解 |

## 启动步骤

```bash
# 1. 构建并启动（首次约 1–3 分钟，主要为拉取 OR-Tools）
docker compose up -d --build

# 2. 查看健康状态
docker compose ps
curl http://localhost:8000/health
```

## 访问入口

| 入口 | 地址 |
| --- | --- |
| 裁决台（主工作台） | <http://localhost:8000/> （自动跳转 `/static/index.html`） |
| 共识报告查看器 | <http://localhost:8000/static/report.html> |
| OpenAPI 文档 | <http://localhost:8000/docs> |
| 健康检查 | <http://localhost:8000/health> |

数据持久化在 Docker 卷 `adj-data`（容器内 `/data/adjudication.db`）。

## 快速体验

打开工作台后点击右上角 **「载入示例」**，系统会自动创建：

- 文档 `demo-doc`（含中文词元边界）；
- 标签 `PERSON / ORG / TIME / CLAIM`，其中 `CLAIM ⊃ PERSON`、
  `CLAIM ⊃ TIME` 为嵌套，`PERSON ✕ ORG` 为互斥；
- 3 名权重不同的标注者（8 / 5 / 3）提交一批含冲突的标注；

随后进入 **步骤 5** 点击 **「求最大得分共识」**，即可看到：

1. 采纳区高亮与得分表；
2. 未采纳候选（如权重较低者把「张伟」标成 `ORG`）及其支持者明细；
3. 可对任意候选发起「强制采纳/否决」并填写理由，系统重新求解为新版本；
4. 点击「生成不可变共识报告」，在报告页打开并核对 SHA-256。

## 本地开发（不使用 Docker）

```bash
python3 -m virtualenv .venv
. .venv/bin/activate
pip install -r requirements.txt

export ADJ_DB_PATH=./adjudication.db
python -c "from app import database; database.init_db()"
uvicorn app.main:app --reload --port 8000
```

运行求解器/接口冒烟测试：

```bash
ADJ_DB_PATH=/tmp/adj_test.db python -m pytest -q
```

## 评分模型与确定性说明

候选集合 = 输入标注去重后的 `(start, end, label)`。候选 `c` 的支持者 =
所有与 `c` **同标签且区间相交**（Dice 重叠比量化后 > 0）的输入标注——
边界不必完全相同，部分重叠同样计入；同一标注可支持多个候选，
异标签或零重叠的标注不计分。候选得分：

```
q(c,a)   = round(dice(c,a) × score_scale)              # 重叠比量化为 0..10 整数
score(c) = Σ_{a 支持 c} weight(a) × label_match × q(c,a)   # 精确整数（CP-SAT 目标）
dice(c,a) = 2 × |c∩a| / (|c| + |a|)
展示分    = score(c) / score_scale = Σ 各支持者贡献分（页面/明细/报告一致）
```

- 标签一致：候选按标签分组，只有相同标签的标注计入支持（`label_match_weight=1`，
  完整参数随报告保存于 `params_json`，共识页「实际算法参数与版本」可查看）。
- 结构约束：两个候选区间重叠时，仅当一个**严格包含**另一个且外层标签声明可嵌套
  内层标签时才能同时入选；交叉、互斥、同标签重叠一律至多取一。
- 平分裁决：候选先按 `(start, end, label_name, label_id)` 排序，
  在所有总分相同的最优解中选布尔向量字典序最大者（候选数 ≤ 60 时用
  `2 的幂` 次级权重严格保证字典序）；求解器固定单线程、随机种子 42。

## 数据模型（摘要）

`projects → document_revisions(追加) / labels / label_nesting / label_mutex /
annotators / annotations → consensuses(含 snapshot_json) →
consensus_items / overrides → reports(触发器禁止 UPDATE/DELETE)`

每份共识在创建时刻冻结完整输入快照（文档文本、词元边界、标签规则、
标注者权重、全部标注、算法参数）；此后重新导入文档或修改规则只产生
新版本，旧共识与旧报告内容不变。
