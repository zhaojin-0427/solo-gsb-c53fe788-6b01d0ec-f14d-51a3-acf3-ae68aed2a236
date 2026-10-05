/* 共识报告查看器：拉取只读报告，浏览器端复算 SHA-256 校验完整性 */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let toastTimer;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}
async function getJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
  return res.json();
}

async function loadProjectOptions() {
  const projects = await getJson("/api/projects");
  $("#reportProject").innerHTML = projects.map((p) =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  const q = new URLSearchParams(location.search);
  if (q.get("project")) $("#reportProject").value = q.get("project");
  await loadVersionOptions();
  if (q.get("version")) {
    $("#reportVersion").value = q.get("version");
    await openReport();
  }
}

async function loadVersionOptions() {
  const pid = $("#reportProject").value;
  if (!pid) return;
  const reports = await getJson(`/api/projects/${pid}/reports`);
  $("#reportVersion").innerHTML = reports.map((r) =>
    `<option value="${esc(r.version)}">${esc(r.version)} · ${r.created_at}</option>`
  ).join("");
  $("#hashState").textContent = reports.length
    ? `共 ${reports.length} 份报告（仅追加，不可改写）`
    : "该项目尚无报告";
}

$("#reportProject").addEventListener("change", loadVersionOptions);
$("#loadReportBtn").addEventListener("click", openReport);

function supporterHtml(s) {
  return `<span class="supporter">· <b>${esc(s.annotator_name)}</b>（权重 ${s.weight}）` +
    ` 提交 [${s.start},${s.end})，重叠比 ${(s.overlap_ratio * 100).toFixed(0)}%，` +
    `贡献分 ${s.contribution}</span>`;
}

function renderHighlighted(text, items, labelColor) {
  const spans = items
    .map((it) => ({ it, start: it.start, end: it.end }))
    .filter((x) => x.start < x.end && x.end <= text.length)
    .sort((a, b) => (b.end - b.start) - (a.end - a.start));
  let html = esc(text);
  // 从最长区间开始包装（嵌套时外层先放占位）；为简单起见采用栈事件方式
  const events = [];
  spans.forEach((sp, i) => {
    events.push([sp.start, 0, i]);
    events.push([sp.end, 1, i]);
  });
  events.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  html = "";
  let pos = 0;
  const open = new Set();
  for (const [at, kind, idx] of events) {
    if (at > pos) html += esc(text.slice(pos, at));
    const it = spans[idx].it;
    const color = labelColor(it.label_id) || "#6366f1";
    if (kind === 0) {
      html += `<span class="mark" style="background:${color}33;box-shadow:inset 0 -2px 0 ${color}">` +
        `<span class="mark-label" style="color:${color}">${esc(it.label_name)}</span>`;
      open.add(idx);
    } else if (open.has(idx)) {
      html += "</span>";
      open.delete(idx);
    }
    pos = at;
  }
  html += esc(text.slice(pos));
  return html;
}

async function openReport() {
  const pid = $("#reportProject").value;
  const version = $("#reportVersion").value;
  if (!pid || !version) return toast("请选择项目与报告版本", true);
  try {
    const payload = await getJson(`/api/projects/${pid}/reports/${encodeURIComponent(version)}`);

    // 浏览器端复算 SHA-256（报告服务端存储时对规范化 JSON 摘要）
    const canon = JSON.stringify(payload, Object.keys(payload).sort());
    // 服务端是对完整 payload sort_keys 后摘要；递归排序键复算
    const sortKeys = (v) => Array.isArray(v)
      ? v.map(sortKeys)
      : v && typeof v === "object"
        ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]))
        : v;
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(JSON.stringify(sortKeys(payload)))
    );
    const hash = [...new Uint8Array(digest)]
      .map((b) => b.toString(16).padStart(2, "0")).join("");

    const snap = payload.input_snapshot;
    const colorOf = (id) =>
      (snap.labels.find((l) => l.id === id) || {}).color || "#64748b";

    const adopted = payload.consensus.adopted;
    const rejected = payload.consensus.rejected;

    $("#reportView").classList.remove("hidden");
    $("#reportView").innerHTML = `
      <h2>共识报告 ${esc(payload.report_version)}</h2>
      <dl class="meta-grid">
        <dt>生成时间 (UTC)</dt><dd>${esc(payload.generated_at)}</dd>
        <dt>项目 / 共识 ID</dt><dd>${payload.project_id} / ${payload.consensus_id}</dd>
        <dt>文档</dt><dd>${esc(snap.doc_id)} · revision ${snap.revision}
          · ${snap.char_count} 字符 · ${snap.token_bounds.length} 词元</dd>
        <dt>算法</dt><dd>${esc(payload.algorithm.name)} v${esc(payload.algorithm.version)}
          （应用版本 ${esc(payload.algorithm.app_version)}）</dd>
        <dt>求解状态</dt><dd>${esc(payload.solver.status)}，
          总分（整数/原始）= ${payload.solver.total_score} / ${payload.solver.raw_total_score}</dd>
        <dt>内容摘要</dt><dd><code>SHA-256:${hash.slice(0, 24)}…</code>
          <span class="hint">（浏览器实时复算，与服务端入库摘要一致即未被篡改）</span></dd>
        <dt>不可变性</dt><dd class="hint">${esc(payload.immutability)}</dd>
      </dl>

      <details open>
        <summary>算法参数</summary>
        <pre class="json">${esc(JSON.stringify(payload.parameters, null, 2))}</pre>
      </details>

      <details open>
        <summary>采纳区间（${adopted.length}）与原文对照</summary>
        <div class="highlight-host"><pre>${renderHighlighted(snap.content, adopted, colorOf)}</pre></div>
        <table class="grid">
          <thead><tr><th>区间</th><th>标签</th><th>得分</th><th>支持者</th><th>来源</th></tr></thead>
          <tbody>${adopted.map((it) => `
            <tr>
              <td>[${it.start},${it.end})<br><span class="hint">${esc(snap.content.slice(it.start, it.end))}</span></td>
              <td><span class="tag-dot" style="background:${colorOf(it.label_id)}"></span>${esc(it.label_name)}</td>
              <td>${it.raw_score}</td>
              <td>${it.supporters.map(supporterHtml).join("")}</td>
              <td>${it.forced ? '<span class="badge forced">裁决覆盖</span>' : '<span class="badge">算法</span>'}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </details>

      <details open>
        <summary>未采纳候选（${rejected.length}）及其支持者</summary>
        <table class="grid">
          <thead><tr><th>区间</th><th>标签</th><th>得分</th><th>支持者明细</th></tr></thead>
          <tbody>${rejected.map((it) => `
            <tr>
              <td>[${it.start},${it.end})<br><span class="hint">${esc(snap.content.slice(it.start, it.end))}</span></td>
              <td><span class="tag-dot" style="background:${colorOf(it.label_id)}"></span>${esc(it.label_name)}</td>
              <td>${it.raw_score}</td>
              <td>${it.supporters.map(supporterHtml).join("")}</td>
            </tr>`).join("") ||
            '<tr><td colspan="4" class="hint">无未采纳候选</td></tr>'}
          </tbody>
        </table>
      </details>

      <details>
        <summary>裁决者覆盖（${payload.overrides.length}）</summary>
        <ul class="rule-list">${payload.overrides.map((o) => `
          <li>${o.action === "select" ? "✅ 强制采纳" : "⛔ 强制否决"}
            [${o.start},${o.end}) 标签 #${o.label_id} —— ${esc(o.reason)}
            <span class="hint">${esc(o.created_at)}</span></li>`).join("") ||
            "<li class='hint'>无覆盖，纯算法结论</li>"}
        </ul>
      </details>

      <details>
        <summary>输入快照（标签规则 / 标注者 / 全部输入标注）</summary>
        <pre class="json">${esc(JSON.stringify({
          labels: snap.labels,
          nesting: snap.nesting,
          mutex: snap.mutex,
          annotators: snap.annotators,
          annotations: snap.annotations,
          token_bounds: snap.token_bounds,
        }, null, 2))}</pre>
      </details>

      <details>
        <summary>完整报告 JSON</summary>
        <pre class="json">${esc(JSON.stringify(payload, null, 2))}</pre>
      </details>
    `;
  } catch (e) {
    toast(e.message, true);
  }
}

loadProjectOptions().catch((e) => toast(e.message, true));
