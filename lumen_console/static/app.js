const byId = (id) => document.getElementById(id);
const authPanel = byId("auth-panel");
const consolePanel = byId("console");
const transcript = byId("transcript");
const rail = byId("connection-rail");
const runStatus = byId("run-status");

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "요청을 완료하지 못했습니다.");
  return body;
}

function setConnection(connected, label) {
  rail.classList.toggle("connected", connected);
  rail.querySelector("span").textContent = label;
}

function addMessage(role, text) {
  transcript.querySelector(".empty-state")?.remove();
  const node = byId("message-template").content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  node.querySelector(".message-label").textContent = role === "user" ? "operator input" : "run replay";
  node.querySelector(".message-body").textContent = text;
  transcript.append(node);
  transcript.scrollTop = transcript.scrollHeight;
  return node.querySelector(".message-body");
}

function showAuth(needsBootstrap) {
  authPanel.classList.remove("hidden");
  consolePanel.classList.add("hidden");
  byId("auth-mode").textContent = needsBootstrap ? "FIRST OPERATOR" : "LOCAL SIGN IN";
  byId("auth-title").textContent = needsBootstrap ? "로컬 운영자를 만드세요" : "다시 로그인하세요";
  byId("auth-description").textContent = needsBootstrap
    ? "Lumen의 Keystone/API key와 분리된 이 브라우저 콘솔 전용 계정입니다."
    : "이 로컬 콘솔에 등록된 운영자 계정으로 로그인합니다.";
  byId("auth-submit").textContent = needsBootstrap ? "로컬 계정 만들기" : "로그인";
  byId("auth-form").dataset.mode = needsBootstrap ? "bootstrap" : "login";
}

async function loadModels() {
  const models = await api("/api/models");
  const select = byId("model-id");
  select.replaceChildren();
  let missingKeyCount = 0;
  for (const model of models) {
    const option = document.createElement("option");
    const missingKey = model.provider_api_key_configured === false;
    if (missingKey) missingKeyCount += 1;
    option.value = model.model_name;
    option.textContent = `${model.display_name || model.model_name} · ${model.provider || "provider"}${missingKey ? " · provider API key 없음" : ""}`;
    select.append(option);
  }
  const ready = models.length > 0;
  select.disabled = !ready;
  byId("message").disabled = !ready;
  byId("send").disabled = !ready;
  if (!ready) {
    byId("scope-note").textContent = "활성 모델이 없습니다. Lumen 관리자에서 provider/model을 등록하세요.";
  } else if (missingKeyCount === models.length) {
    byId("scope-note").textContent = "Provider API key가 없습니다. 환경 변수 또는 관리자 API에서 key를 등록하세요.";
  } else if (missingKeyCount > 0) {
    byId("scope-note").textContent = `${models.length}개 활성 모델 중 ${missingKeyCount}개는 provider API key가 없습니다.`;
  } else {
    byId("scope-note").textContent = `${models.length}개 활성 모델을 불러왔습니다.`;
  }
  const empty = transcript.querySelector(".empty-state");
  if (empty) {
    empty.textContent = ready
      ? "모델을 선택하고 첫 run을 생성하세요. 응답은 durable run journal에서 replay됩니다."
      : "Lumen upstream을 연결하고 모델을 선택하세요. 모든 응답은 durable run journal에서 replay됩니다.";
  }
}

async function showConsole() {
  const me = await api("/api/auth/me");
  const connection = await api("/api/connection");
  authPanel.classList.add("hidden");
  consolePanel.classList.remove("hidden");
  byId("username").textContent = me.username;
  if (!connection.connected) {
    setConnection(false, "Lumen API 연결 없음");
    byId("scope-note").textContent = "Lumen API 연결 정보가 없습니다. URL과 API key 또는 Keystone token을 입력하세요.";
    return;
  }
  const form = byId("connection-form");
  form.base_url.value = connection.base_url || "";
  form.auth_mode.value = connection.auth_mode || "api_key";
  form.project_id.value = connection.project_id || "";
  syncAuthFields();
  setConnection(true, `${connection.auth_mode} · ${connection.base_url}`);
  await loadModels();
}

function syncAuthFields() {
  const keystone = byId("auth-type").value === "keystone";
  byId("project-label").classList.toggle("hidden", !keystone);
  byId("credential-label").firstChild.textContent = keystone ? "Keystone token" : "API key";
  byId("credential-label").querySelector("input").placeholder = keystone ? "gAAAA..." : "sk-afgl-...";
}

byId("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api(`/api/auth/${event.currentTarget.dataset.mode}`, { method: "POST", body: JSON.stringify(Object.fromEntries(form)) });
    await showConsole();
  } catch (error) { alert(error.message); }
});

byId("auth-type").addEventListener("change", syncAuthFields);
byId("connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form);
  try {
    await api("/api/connection", { method: "PUT", body: JSON.stringify(payload) });
    event.currentTarget.credential.value = "";
    setConnection(true, `${payload.auth_mode} · ${payload.base_url}`);
    await loadModels();
  } catch (error) { byId("scope-note").textContent = error.message; }
});

byId("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = byId("message").value.trim();
  if (!text) return;
  const button = byId("send");
  button.disabled = true;
  addMessage("user", text);
  byId("message").value = "";
  runStatus.textContent = "run 생성 중";
  try {
    const run = await api("/api/chat/runs", {
      method: "POST",
      body: JSON.stringify({ model_id: byId("model-id").value, text, memory: byId("memory").checked, tools: byId("tools").checked }),
    });
    runStatus.textContent = `queued · ${run.run_id.slice(0, 8)}`;
    const output = addMessage("assistant", "");
    const stream = new EventSource(`/api/chat/runs/${run.run_id}/events`);
    stream.addEventListener("part.delta", (event) => {
      const payload = JSON.parse(event.data);
      if (payload.payload.part_type === "text") output.textContent += payload.payload.delta;
    });
    stream.addEventListener("usage.updated", () => { runStatus.textContent = `usage recorded · ${run.run_id.slice(0, 8)}`; });
    for (const terminal of ["run.completed", "run.failed", "run.canceled"]) {
      stream.addEventListener(terminal, (event) => {
        const payload = JSON.parse(event.data);
        if (terminal !== "run.completed" && !output.textContent) output.textContent = payload.payload.safe_message || terminal;
        runStatus.textContent = terminal.replace("run.", "");
        stream.close();
        button.disabled = false;
      });
    }
    stream.addEventListener("error", () => { runStatus.textContent = "replay 연결 오류"; stream.close(); button.disabled = false; });
  } catch (error) { runStatus.textContent = error.message; button.disabled = false; }
});

byId("logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); });

(async () => {
  try { await showConsole(); } catch (_) { const status = await api("/api/bootstrap"); showAuth(status.needs_bootstrap); }
})();
