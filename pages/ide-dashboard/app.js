const API_PREFIX = "ide_sandbox";
const MAX_READ_LINES = 2000;

const state = {
  sandboxId: "",
  selectedPath: "",
  fileTree: [],
  sandboxes: [],
  overview: null,
  todos: [],
  config: null,
  currentFileContent: "",
  liveTimer: null,
  refreshing: false,
};

const els = {
  appShell: document.querySelector(".app"),
  brandTitle: document.getElementById("brandTitle"),
  sandboxSelect: document.getElementById("sandboxSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  liveStatus: document.getElementById("liveStatus"),
  userBadge: document.getElementById("userBadge"),
  sandboxStatus: document.getElementById("sandboxStatus"),
  executionStatus: document.getElementById("executionStatus"),
  limitStatus: document.getElementById("limitStatus"),
  overviewUpdated: document.getElementById("overviewUpdated"),
  overviewSummary: document.getElementById("overviewSummary"),
  overviewSandboxList: document.getElementById("overviewSandboxList"),
  overviewToolList: document.getElementById("overviewToolList") || document.getElementById("overviewCommandList"),
  fileTree: document.getElementById("fileTree"),
  newFileBtn: document.getElementById("newFileBtn"),
  newFolderBtn: document.getElementById("newFolderBtn"),
  tabs: document.querySelectorAll(".tab"),
  panels: document.querySelectorAll(".tab-panel"),
  editorPath: document.getElementById("editorPath"),
  editor: document.getElementById("editor"),
  saveFileBtn: document.getElementById("saveFileBtn"),
  deleteFileBtn: document.getElementById("deleteFileBtn"),
  renameFileBtn: document.getElementById("renameFileBtn"),
  editorStatus: document.getElementById("editorStatus"),
  commandInput: document.getElementById("commandInput"),
  runCommandBtn: document.getElementById("runCommandBtn"),
  bgTaskCheck: document.getElementById("bgTaskCheck"),
  terminalOutput: document.getElementById("terminalOutput"),
  tasksList: document.getElementById("tasksList"),
  refreshTasksBtn: document.getElementById("refreshTasksBtn"),
  todosList: document.getElementById("todosList"),
  addTodoBtn: document.getElementById("addTodoBtn"),
  saveTodosBtn: document.getElementById("saveTodosBtn"),
  historyList: document.getElementById("historyList"),
  refreshHistoryBtn: document.getElementById("refreshHistoryBtn"),
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modalTitle"),
  modalInput: document.getElementById("modalInput"),
  modalCancel: document.getElementById("modalCancel"),
  modalConfirm: document.getElementById("modalConfirm"),
};

let bridge = null;
function unwrapBridgeData(res) {
  if (res && typeof res === "object" && "status" in res) {
    if (res.status === "ok") return res.data;
    if (res.status === "error") throw new Error(res.message || "请求失败");
  }
  return res;
}


async function initBridge() {
  bridge = window.AstrBotPluginPage;
  if (!bridge) {
    showToast("未找到 AstrBotPluginPage Bridge，请从 AstrBot WebUI 打开本页面", "error");
    els.executionStatus.textContent = "Bridge 未连接";
    els.limitStatus.textContent = "无法读取";
    return;
  }
  try {
    await bridge.ready();
  } catch (e) {
    console.warn("Bridge ready 失败:", e);
  }
  await loadInfo();
  await loadConfig();
  await loadSandboxes();
  await loadOverview();
  switchTab("overview");
  startLiveRefresh();
}

async function apiGet(endpoint, params = {}) {
  if (!bridge) throw new Error("Bridge 未初始化");
  return unwrapBridgeData(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  if (!bridge) throw new Error("Bridge 未初始化");
  return unwrapBridgeData(await bridge.apiPost(endpoint, body));
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

function setLoading(el, loading) {
  if (loading) el.dataset.loading = "true";
  else delete el.dataset.loading;
}

function getErrorMessage(error, fallback = "请求失败") {
  if (!error) return fallback;
  const data = error.response && error.response.data;
  if (data && typeof data === "object" && data.message) return data.message;
  if (typeof data === "string") return data;
  return error.message || fallback;
}

async function loadInfo() {
  try {
    const res = await apiGet("info");
    if (res.display_name) {
      els.brandTitle.textContent = res.display_name;
      document.title = res.display_name;
    }
    els.userBadge.textContent = res.username || "未知用户";
  } catch (e) {
    showToast("获取用户信息失败: " + e.message, "error");
  }
}

async function loadConfig() {
  try {
    const res = await apiGet("config");
    state.config = res;
    renderConfigSummary(res);
  } catch (e) {
    els.executionStatus.textContent = "配置读取失败";
    els.limitStatus.textContent = e.message;
  }
}

function renderConfigSummary(config) {
  if (!config) {
    els.executionStatus.textContent = "未知";
    els.limitStatus.textContent = "未知";
    return;
  }
  if (config.cover_only_mode) {
    els.executionStatus.textContent = "仅翻唱联动";
  } else {
    els.executionStatus.textContent = config.allow_execution ? "命令可执行" : "命令已关闭";
  }
  const fileLimit = config.max_file_size_mb ? `${config.max_file_size_mb}MB` : "未设置";
  const timeout = config.cmd_timeout ? `${config.cmd_timeout}s` : "未设置";
  els.limitStatus.textContent = `文件 ${fileLimit} / 命令 ${timeout}`;
}

async function loadSandboxes(options = {}) {
  const reloadCurrent = options.reloadCurrent !== false;
  const silent = Boolean(options.silent);
  try {
    const res = await apiGet("sandboxes");
    const previous = state.sandboxId || els.sandboxSelect.value;
    state.sandboxes = res.sandboxes || [];
    els.sandboxSelect.innerHTML = '<option value="">请选择沙盒...</option>';
    for (const sb of state.sandboxes) {
      const opt = document.createElement("option");
      opt.value = sb.id;
      opt.textContent = `${sb.id} (${sb.file_count} 个文件)`;
      els.sandboxSelect.appendChild(opt);
    }
    const stillExists = state.sandboxes.some((sb) => sb.id === previous);
    let next = stillExists ? previous : "";
    if (!next && state.sandboxes.length === 1) {
      next = state.sandboxes[0].id;
    }
    els.sandboxSelect.value = next;
    if (next) {
      const changed = next !== state.sandboxId;
      if (changed || reloadCurrent) {
        await onSandboxChange({ resetSelection: changed });
      } else {
        state.sandboxId = next;
        updateSandboxStatus();
        enableControls(true);
      }
    } else {
      state.sandboxId = "";
      state.selectedPath = "";
      updateEditorState();
      updateSandboxStatus();
      enableControls(false);
    }
  } catch (e) {
    if (!silent) showToast("加载沙盒失败: " + e.message, "error");
    els.sandboxStatus.textContent = "加载失败";
  }
}

async function onSandboxChange(options = {}) {
  const resetSelection = options.resetSelection !== false;
  state.sandboxId = els.sandboxSelect.value;
  if (resetSelection) {
    state.selectedPath = "";
    updateEditorState();
  }
  updateSandboxStatus();
  if (state.sandboxId) {
    await loadFileTree();
    await loadTodos();
    await loadHistory();
    await loadTasks();
    enableControls(true);
  } else {
    els.fileTree.innerHTML = "请先选择沙盒";
    enableControls(false);
  }
}

function updateSandboxStatus() {
  if (!state.sandboxId) {
    els.sandboxStatus.textContent = state.sandboxes.length
      ? `${state.sandboxes.length} 个沙盒可选`
      : "暂无沙盒";
    return;
  }
  const current = state.sandboxes.find((sb) => sb.id === state.sandboxId);
  const fileCount = current && Number.isFinite(current.file_count)
    ? `${current.file_count} 个文件`
    : "文件数未知";
  els.sandboxStatus.textContent = `${state.sandboxId} / ${fileCount}`;
}

function setLiveStatus(text, tone = "ok") {
  if (!els.liveStatus) return;
  if (els.liveStatus.textContent === text && els.liveStatus.dataset.tone === tone) return;
  els.liveStatus.textContent = text;
  els.liveStatus.dataset.tone = tone;
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function loadOverview(options = {}) {
  const silent = Boolean(options.silent);
  try {
    const res = await apiGet("overview", {
      history_limit: 40,
      recent_files: 8,
    });
    state.overview = res;
    renderOverview(res);
  } catch (e) {
    if (!silent) showToast("加载总览失败: " + e.message, "error");
    if (els.overviewSandboxList) {
      els.overviewSandboxList.innerHTML = `<div class="empty">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }
}

function renderOverview(data) {
  if (!data) return;
  const summary = data.summary || {};
  if (els.overviewUpdated) {
    els.overviewUpdated.textContent = `更新于 ${formatHistoryTime(summary.updated_at) || "-"}`;
  }
  if (els.overviewSummary) {
    els.overviewSummary.innerHTML = `
      <div class="metric"><strong>${summary.sandbox_count || 0}</strong><span>沙盒</span></div>
      <div class="metric"><strong>${summary.file_count || 0}</strong><span>文件</span></div>
      <div class="metric"><strong>${summary.dir_count || 0}</strong><span>目录</span></div>
      <div class="metric"><strong>${summary.tool_count ?? summary.command_count ?? 0}</strong><span>工具活动</span></div>
    `;
  }
  renderOverviewSandboxes(data.sandboxes || []);
  renderOverviewTools(data.tools || data.commands || []);
}

function renderOverviewSandboxes(sandboxes) {
  if (!els.overviewSandboxList) return;
  els.overviewSandboxList.innerHTML = "";
  if (sandboxes.length === 0) {
    els.overviewSandboxList.innerHTML = '<div class="empty">暂无沙盒；机器人产生文件后会出现在这里。</div>';
    return;
  }
  for (const sb of sandboxes) {
    const div = document.createElement("button");
    div.type = "button";
    div.className = "overview-item sandbox-card";
    div.dataset.sandbox = sb.id;
    const recentFiles = (sb.recent_files || []).slice(0, 3)
      .map((file) => {
        const fullPath = file.path || file.name || "";
        const label = file.name || fullPath;
        return `<span title="${escapeHtml(fullPath)}">${escapeHtml(label)} &middot; ${formatBytes(file.size)}</span>`;
      })
      .join("");
    const activity = sb.recent_activity
      ? `${getActionMeta(sb.recent_activity.action).label}: ${sb.recent_activity.detail || ""}`
      : "暂无活动";
    div.innerHTML = `
      <div class="overview-item-head">
        <strong>${escapeHtml(sb.id)}</strong>
        <span>${sb.file_count || 0} 文件 / ${sb.dir_count || 0} 目录</span>
      </div>
      <div class="overview-path">${escapeHtml(sb.path || "")}</div>
      <div class="overview-activity">${escapeHtml(activity)}</div>
      <div class="recent-files">${recentFiles || "<span>没有最近文件</span>"}</div>
    `;
    div.addEventListener("click", () => selectSandbox(sb.id, "editor"));
    els.overviewSandboxList.appendChild(div);
  }
}

function renderOverviewTools(tools) {
  if (!els.overviewToolList) return;
  els.overviewToolList.innerHTML = "";
  if (tools.length === 0) {
    els.overviewToolList.innerHTML = '<div class="empty">暂无工具执行记录。</div>';
    return;
  }
  for (const tool of tools) {
    const meta = getActionMeta(tool.action);
    const div = document.createElement("button");
    div.type = "button";
    div.className = `overview-item command-card activity-${meta.tone}`;
    div.dataset.sandbox = tool.sandbox_id;
    div.innerHTML = `
      <div class="overview-item-head">
        <strong>${escapeHtml(tool.sandbox_id || "未知沙盒")}</strong>
        <span>${escapeHtml(formatHistoryTime(tool.time))}</span>
      </div>
      <div class="history-main command-main">
        <span class="history-badge">${escapeHtml(meta.label)}</span>
        <span class="history-action">${escapeHtml(tool.action || "")}</span>
      </div>
      <div class="history-detail">${escapeHtml(tool.detail || "无详情")}</div>
      <div class="overview-path">${escapeHtml(tool.cwd || "")}</div>
    `;
    div.addEventListener("click", () => selectSandbox(tool.sandbox_id, "history"));
    els.overviewToolList.appendChild(div);
  }
}

async function selectSandbox(sandboxId, tabName = "editor") {
  if (!sandboxId) return;
  els.sandboxSelect.value = sandboxId;
  await onSandboxChange();
  switchTab(tabName);
}

async function refreshLiveData() {
  if (state.refreshing) return;
  if (document.hidden) return;
  state.refreshing = true;
  try {
    await loadOverview({ silent: true });
    await loadSandboxes({ silent: true, reloadCurrent: false });
    if (state.sandboxId) {
      await Promise.all([
        loadFileTree({ silent: true }),
        loadHistory({ silent: true }),
        loadTasks({ silent: true }),
      ]);
    }
    setLiveStatus("实时更新", "ok");
  } catch (e) {
    setLiveStatus("同步失败", "error");
  } finally {
    state.refreshing = false;
  }
}

function startLiveRefresh() {
  if (state.liveTimer) clearInterval(state.liveTimer);
  state.liveTimer = setInterval(refreshLiveData, 2500);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshLiveData();
  });
}

function enableControls(enabled) {
  els.commandInput.disabled = !enabled;
  els.runCommandBtn.disabled = !enabled;
  els.newFileBtn.disabled = !enabled;
  els.newFolderBtn.disabled = !enabled;
  els.bgTaskCheck.disabled = !enabled;
  if (!enabled) {
    els.saveFileBtn.disabled = true;
    els.deleteFileBtn.disabled = true;
    els.renameFileBtn.disabled = true;
    els.editor.disabled = true;
  }
}

async function loadFileTree() {
  if (!state.sandboxId) return;
  try {
    const res = await apiGet("file_tree", {
      sandbox_id: state.sandboxId,
      root: "",
      max_depth: 6,
    });
    state.fileTree = res.tree || [];
    renderFileTree(state.fileTree, els.fileTree);
  } catch (e) {
    els.fileTree.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  }
}

function renderFileTree(nodes, container) {
  container.innerHTML = "";
  if (!nodes || nodes.length === 0) {
    container.innerHTML = '<div class="empty">沙盒为空，可以新建文件或目录。</div>';
    return;
  }
  for (const node of nodes) {
    container.appendChild(buildTreeNode(node));
  }
}

function buildTreeNode(node) {
  const wrap = document.createElement("div");
  wrap.className = "tree-node";

  const item = document.createElement("button");
  item.type = "button";
  item.className = "tree-item";
  item.dataset.path = node.path;
  item.dataset.type = node.type;
  item.title = node.path || node.name;
  item.setAttribute("aria-selected", node.path === state.selectedPath ? "true" : "false");
  if (node.path === state.selectedPath) {
    item.classList.add("active");
  }

  const icon = document.createElement("span");
  icon.className = `tree-icon ${node.type === "directory" ? "directory" : "file"}`;
  icon.setAttribute("aria-hidden", "true");

  const name = document.createElement("span");
  name.className = "tree-name";
  name.textContent = node.name;

  item.appendChild(icon);
  item.appendChild(name);
  item.addEventListener("click", (event) => {
    event.preventDefault();
    onFileClick(node, item);
  });
  wrap.appendChild(item);

  if (node.type === "directory" && node.children) {
    const children = document.createElement("div");
    children.className = "tree-children";
    for (const child of node.children) {
      children.appendChild(buildTreeNode(child));
    }
    wrap.appendChild(children);
  }

  return wrap;
}

function setSelectedTreeItem(el) {
  document.querySelectorAll(".tree-item").forEach((i) => {
    i.classList.remove("active");
    i.setAttribute("aria-selected", "false");
  });
  el.classList.add("active");
  el.setAttribute("aria-selected", "true");
}

function prepareSelectedPath(node) {
  state.selectedPath = node.path;
  state.currentFileContent = "";
  els.editorPath.textContent = node.type === "directory"
    ? `已选择目录: ${node.path || "."}`
    : node.path;
  els.editor.value = "";
  els.editor.disabled = true;
  els.saveFileBtn.disabled = true;
  els.deleteFileBtn.disabled = false;
  els.renameFileBtn.disabled = false;
  const baseStatus = node.type === "directory" ? "目录" : "正在读取";
  els.editorStatus.dataset.base = baseStatus;
  els.editorStatus.textContent = baseStatus;
  switchTab("editor");
}

async function onFileClick(node, el) {
  setSelectedTreeItem(el);
  prepareSelectedPath(node);

  if (node.type === "directory") {
    return;
  }

  await readFile(node.path);
}

function showUnreadableSelection(path, reason) {
  state.currentFileContent = "";
  els.editor.value = "";
  els.editor.disabled = true;
  els.editorPath.textContent = path;
  els.saveFileBtn.disabled = true;
  els.deleteFileBtn.disabled = false;
  els.renameFileBtn.disabled = false;
  const baseStatus = `无法预览: ${reason || "读取失败"}`;
  els.editorStatus.dataset.base = baseStatus;
  els.editorStatus.textContent = baseStatus;
}

async function readFile(path) {
  try {
    const res = await apiGet("read_file", {
      sandbox_id: state.sandboxId,
      path: path,
      line_offset: 1,
      n_lines: MAX_READ_LINES,
    });
    if (res.previewable === false) {
      showUnreadableSelection(path, res.reason);
      return;
    }
    state.currentFileContent = res.content;
    els.editor.value = res.content;
    els.editor.disabled = false;
    els.editorPath.textContent = res.path;
    els.saveFileBtn.disabled = true;
    els.deleteFileBtn.disabled = false;
    els.renameFileBtn.disabled = false;
    const baseStatus = `${res.total_lines} 行 / ${res.bytes || res.content.length} 字符${res.has_more ? " / 已截断预览" : ""}`;
    els.editorStatus.dataset.base = baseStatus;
    els.editorStatus.textContent = baseStatus;
    switchTab("editor");
  } catch (e) {
    const message = getErrorMessage(e);
    showUnreadableSelection(path, message);
    showToast("读取文件失败: " + message, "error");
  }
}

async function saveCurrentFile() {
  if (!state.selectedPath) return;
  try {
    const res = await apiPost("write_file", {
      sandbox_id: state.sandboxId,
      path: state.selectedPath,
      content: els.editor.value,
    });
    state.currentFileContent = els.editor.value;
    els.saveFileBtn.disabled = true;
    const baseStatus = els.editorStatus.dataset.base || "";
    els.editorStatus.textContent = baseStatus ? `${baseStatus} / 已保存` : "已保存";
    showToast("保存成功");
    await loadFileTree();
    await loadHistory();
  } catch (e) {
    showToast("保存失败: " + e.message, "error");
  }
}

async function deleteSelected() {
  const targetPath = state.selectedPath;
  if (!targetPath) return;
  const confirmed = await confirmModal(`确定要删除 ${targetPath} 吗？`, "删除");
  if (!confirmed) return;
  els.deleteFileBtn.disabled = true;
  els.renameFileBtn.disabled = true;
  setLoading(els.deleteFileBtn, true);
  try {
    await apiPost("delete_file", {
      sandbox_id: state.sandboxId,
      path: targetPath,
      recursive: true,
    });
    if (state.selectedPath === targetPath) {
      state.selectedPath = "";
      state.currentFileContent = "";
      updateEditorState();
    }
    await loadFileTree();
    await loadSandboxes({ reloadCurrent: false });
    await loadHistory();
    await loadOverview({ silent: true });
    showToast("删除成功");
  } catch (e) {
    if (state.selectedPath === targetPath) {
      els.deleteFileBtn.disabled = false;
      els.renameFileBtn.disabled = false;
    }
    showToast("删除失败: " + getErrorMessage(e), "error");
  } finally {
    setLoading(els.deleteFileBtn, false);
  }
}

async function renameSelected() {
  if (!state.selectedPath) return;
  const newName = await promptModal("重命名", state.selectedPath);
  if (!newName || newName === state.selectedPath) return;
  try {
    const res = await apiPost("rename", {
      sandbox_id: state.sandboxId,
      old_path: state.selectedPath,
      new_path: newName,
    });
    state.selectedPath = newName;
    await loadFileTree();
    await loadHistory();
    showToast("重命名成功");
  } catch (e) {
    showToast("重命名失败: " + e.message, "error");
  }
}

async function createNewFile() {
  if (!state.sandboxId) return;
  const path = await promptModal("新建文件路径", "");
  if (!path) return;
  try {
    const res = await apiPost("write_file", {
      sandbox_id: state.sandboxId,
      path: path,
      content: "",
    });
    await loadFileTree();
    await loadHistory();
    state.selectedPath = path;
    await readFile(path);
    showToast("创建成功");
  } catch (e) {
    showToast("创建失败: " + e.message, "error");
  }
}

async function createNewFolder() {
  if (!state.sandboxId) return;
  const path = await promptModal("新建目录路径", "");
  if (!path) return;
  try {
    const res = await apiPost("mkdir", {
      sandbox_id: state.sandboxId,
      path: path,
    });
    await loadFileTree();
    await loadHistory();
    showToast("创建成功");
  } catch (e) {
    showToast("创建失败: " + e.message, "error");
  }
}

function updateEditorState() {
  if (!state.selectedPath) {
    els.editorPath.textContent = state.sandboxId ? "未选择文件" : "未选择沙盒";
    els.editor.value = "";
    els.editor.disabled = true;
    els.saveFileBtn.disabled = true;
    els.deleteFileBtn.disabled = true;
    els.renameFileBtn.disabled = true;
    delete els.editorStatus.dataset.base;
    els.editorStatus.textContent = "";
  }
}

function updateEditorDirtyState() {
  if (!state.selectedPath || els.editor.disabled) return;
  const dirty = els.editor.value !== state.currentFileContent;
  els.saveFileBtn.disabled = !dirty;
  const baseStatus = els.editorStatus.dataset.base || "";
  els.editorStatus.textContent = dirty ? `${baseStatus} / 未保存` : baseStatus;
}

async function runCommand() {
  const command = els.commandInput.value.trim();
  if (!command || !state.sandboxId) return;
  els.terminalOutput.textContent += `\n$ ${command}\n`;
  els.runCommandBtn.disabled = true;
  try {
    const res = await apiPost("execute", {
      sandbox_id: state.sandboxId,
      command: command,
      run_in_background: els.bgTaskCheck.checked,
      description: command,
    });
    if (res.task_id) {
      els.terminalOutput.textContent += `[后台任务] ID: ${res.task_id}\n`;
      switchTab("tasks");
      await loadTasks();
    } else {
      const out = res.stdout || "";
      const err = res.stderr || "";
      els.terminalOutput.textContent += `${out}${out && err ? "\n" : ""}${err ? "[stderr]\n" + err + "\n" : ""}[返回码] ${res.returncode}\n`;
      await loadHistory();
    }
  } catch (e) {
    els.terminalOutput.textContent += `[异常] ${e.message}\n`;
    showToast(e.message, "error");
  } finally {
    els.runCommandBtn.disabled = false;
    els.terminalOutput.scrollTop = els.terminalOutput.scrollHeight;
  }
}

async function loadTasks() {
  try {
    const params = state.sandboxId ? { sandbox_id: state.sandboxId } : {};
    const res = await apiGet("tasks", params);
    const tasks = res.tasks || [];
    els.tasksList.innerHTML = "";
    if (tasks.length === 0) {
      els.tasksList.innerHTML = '<div class="empty">暂无后台任务</div>';
      return;
    }
    for (const task of tasks) {
      const div = document.createElement("div");
      div.className = "task-item";
      div.innerHTML = `
        <div><strong>${escapeHtml(task.description)}</strong></div>
        <div class="task-meta">
          <span>ID: ${task.task_id}</span>
          <span class="status-${task.status}">${task.status}</span>
          <span>返回码: ${task.returncode ?? "-"}</span>
          <span>${new Date(task.start_time * 1000).toLocaleString()}</span>
        </div>
        <div class="task-actions">
          <button class="view-output" data-id="${task.task_id}">查看输出</button>
          ${task.status === "running" ? `<button class="stop-task" data-id="${task.task_id}">停止</button>` : ""}
        </div>
      `;
      els.tasksList.appendChild(div);
    }
    els.tasksList.querySelectorAll(".view-output").forEach((btn) => {
      btn.addEventListener("click", () => viewTaskOutput(btn.dataset.id));
    });
    els.tasksList.querySelectorAll(".stop-task").forEach((btn) => {
      btn.addEventListener("click", () => stopTask(btn.dataset.id));
    });
  } catch (e) {
    els.tasksList.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  }
}

async function viewTaskOutput(taskId) {
  try {
    const res = await apiGet("task_output", { task_id: taskId });
    els.terminalOutput.textContent += `\n=== 任务 ${taskId} [${res.status}] ===\n${res.output}\n`;
    switchTab("terminal");
    els.terminalOutput.scrollTop = els.terminalOutput.scrollHeight;
  } catch (e) {
    showToast("获取任务输出失败: " + e.message, "error");
  }
}

async function stopTask(taskId) {
  try {
    const res = await apiPost("task_stop", { task_id: taskId });
    showToast("已停止任务");
    await loadTasks();
  } catch (e) {
    showToast("停止任务失败: " + e.message, "error");
  }
}

async function loadTodos() {
  if (!state.sandboxId) return;
  try {
    const res = await apiGet("todos", { sandbox_id: state.sandboxId });
    state.todos = res.todos || [];
    renderTodos();
  } catch (e) {
    els.todosList.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  }
}

function renderTodos() {
  els.todosList.innerHTML = "";
  if (state.todos.length === 0) {
    els.todosList.innerHTML = '<div class="empty">暂无待办事项</div>';
    return;
  }
  state.todos.forEach((todo, index) => {
    const div = document.createElement("div");
    div.className = "todo-item";
    div.innerHTML = `
      <input type="text" value="${escapeHtml(todo.title || todo.content || "")}" data-index="${index}" />
      <select data-index="${index}">
        <option value="pending" ${todo.status === "pending" ? "selected" : ""}>待处理</option>
        <option value="in_progress" ${todo.status === "in_progress" ? "selected" : ""}>进行中</option>
        <option value="done" ${todo.status === "done" ? "selected" : ""}>已完成</option>
      </select>
      <button class="remove-todo" data-index="${index}">删除</button>
    `;
    els.todosList.appendChild(div);
  });
  els.todosList.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", (e) => {
      state.todos[e.target.dataset.index].title = e.target.value;
      state.todos[e.target.dataset.index].content = e.target.value;
    });
  });
  els.todosList.querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", (e) => {
      state.todos[e.target.dataset.index].status = e.target.value;
      state.todos[e.target.dataset.index].completed = e.target.value === "done";
    });
  });
  els.todosList.querySelectorAll(".remove-todo").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.todos.splice(btn.dataset.index, 1);
      renderTodos();
    });
  });
}

async function saveTodos() {
  if (!state.sandboxId) return;
  try {
    const res = await apiPost("todos", {
      sandbox_id: state.sandboxId,
      todos: state.todos.map((t) => ({
        title: t.title || t.content || "",
        status: t.status || "pending",
      })),
    });
    showToast("待办保存成功");
    await loadTodos();
  } catch (e) {
    showToast("保存待办失败: " + e.message, "error");
  }
}

function addTodo() {
  state.todos.push({
    id: state.todos.length + 1,
    title: "",
    content: "",
    status: "pending",
    completed: false,
    created_at: new Date().toISOString().slice(0, 19),
  });
  renderTodos();
}

const ACTION_META = {
  write: { label: "写入文件", tone: "write" },
  write_file: { label: "写入文件", tone: "write" },
  append: { label: "追加文件", tone: "write" },
  edit: { label: "编辑文件", tone: "write" },
  delete: { label: "删除文件", tone: "danger" },
  mkdir: { label: "新建目录", tone: "file" },
  rename: { label: "重命名", tone: "file" },
  execute: { label: "执行命令", tone: "command" },
  execute_bg: { label: "后台命令", tone: "command" },
  execute_elevated: { label: "提权命令", tone: "danger" },
  run_test: { label: "运行测试", tone: "command" },
  git_clone: { label: "克隆仓库", tone: "file" },
  download: { label: "下载群文件", tone: "file" },
  upload: { label: "上传群文件", tone: "file" },
  auto_download: { label: "自动下载", tone: "file" },
  list_files: { label: "查看文件", tone: "read" },
  list_tree: { label: "查看树", tone: "read" },
  glob: { label: "匹配文件", tone: "read" },
  file_info: { label: "文件信息", tone: "read" },
  read_range: { label: "读取文件", tone: "read" },
  search_text: { label: "搜索文本", tone: "read" },
  set_todos: { label: "更新待办", tone: "task" },
  think: { label: "思考记录", tone: "task" },
  ask_user: { label: "询问用户", tone: "task" },
  pack_download: { label: "打包下载", tone: "file" },
};

function getActionMeta(action) {
  return ACTION_META[action] || { label: action || "工具调用", tone: "tool" };
}

function formatHistoryTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").slice(0, 19);
}

async function loadHistory() {
  if (!state.sandboxId) return;
  try {
    const res = await apiGet("history", { sandbox_id: state.sandboxId, limit: 100 });
    const records = res.history || [];
    els.historyList.innerHTML = "";
    if (records.length === 0) {
      els.historyList.innerHTML = '<div class="empty">还没有工具活动；机器人读写文件或执行命令后会显示在这里。</div>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const r of records.slice().reverse()) {
      const meta = getActionMeta(r.action);
      const div = document.createElement("div");
      div.className = `history-item activity-${meta.tone}`;
      div.innerHTML = `
        <div class="history-main">
          <span class="history-badge">${escapeHtml(meta.label)}</span>
          <span class="history-action">${escapeHtml(r.action || "")}</span>
          <span class="history-time">${escapeHtml(formatHistoryTime(r.time))}</span>
        </div>
        <div class="history-detail">${escapeHtml(r.detail || "无详情")}</div>
      `;
      fragment.appendChild(div);
    }
    els.historyList.appendChild(fragment);
  } catch (e) {
    els.historyList.innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  }
}

function switchTab(name) {
  if (els.appShell) {
    els.appShell.classList.toggle("overview-mode", name === "overview");
  }
  els.tabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  els.panels.forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${name}Panel`);
  });
  if (name === "overview") loadOverview({ silent: true });
  if (name === "tasks") loadTasks();
  if (name === "history") loadHistory();
}

function promptModal(title, defaultValue = "") {
  return new Promise((resolve) => {
    els.modalTitle.textContent = title;
    els.modalInput.value = defaultValue;
    els.modal.classList.remove("confirm-mode");
    els.modalConfirm.classList.remove("danger");
    els.modalConfirm.textContent = "确认";
    els.modalCancel.textContent = "取消";
    els.modal.classList.remove("hidden");
    els.modalInput.focus();

    function cleanup() {
      els.modal.classList.add("hidden");
      els.modalConfirm.removeEventListener("click", onConfirm);
      els.modalCancel.removeEventListener("click", onCancel);
      els.modalInput.removeEventListener("keydown", onKey);
    }

    function onConfirm() {
      cleanup();
      resolve(els.modalInput.value.trim());
    }

    function onCancel() {
      cleanup();
      resolve(null);
    }

    function onKey(e) {
      if (e.key === "Enter") onConfirm();
      if (e.key === "Escape") onCancel();
    }

    els.modalConfirm.addEventListener("click", onConfirm);
    els.modalCancel.addEventListener("click", onCancel);
    els.modalInput.addEventListener("keydown", onKey);
  });
}

async function confirmModal(title, confirmLabel = "确认") {
  return new Promise((resolve) => {
    els.modalTitle.textContent = title;
    els.modalInput.value = "";
    els.modal.classList.add("confirm-mode");
    els.modalConfirm.classList.add("danger");
    els.modalConfirm.textContent = confirmLabel;
    els.modalCancel.textContent = "取消";
    els.modal.classList.remove("hidden");
    els.modalConfirm.focus();

    function cleanup() {
      els.modal.classList.add("hidden");
      els.modal.classList.remove("confirm-mode");
      els.modalConfirm.classList.remove("danger");
      els.modalConfirm.textContent = "确认";
      els.modalConfirm.removeEventListener("click", onConfirm);
      els.modalCancel.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
    }

    function onConfirm() {
      cleanup();
      resolve(true);
    }

    function onCancel() {
      cleanup();
      resolve(false);
    }

    function onKey(e) {
      if (e.key === "Enter") onConfirm();
      if (e.key === "Escape") onCancel();
    }

    els.modalConfirm.addEventListener("click", onConfirm);
    els.modalCancel.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

// ==================== 事件绑定 ====================

els.sandboxSelect.addEventListener("change", onSandboxChange);
els.refreshBtn.addEventListener("click", refreshLiveData);
els.newFileBtn.addEventListener("click", createNewFile);
els.newFolderBtn.addEventListener("click", createNewFolder);
els.saveFileBtn.addEventListener("click", saveCurrentFile);
els.deleteFileBtn.addEventListener("click", deleteSelected);
els.renameFileBtn.addEventListener("click", renameSelected);
els.editor.addEventListener("input", updateEditorDirtyState);
els.runCommandBtn.addEventListener("click", runCommand);
els.commandInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runCommand();
});
els.refreshTasksBtn.addEventListener("click", loadTasks);
els.addTodoBtn.addEventListener("click", addTodo);
els.saveTodosBtn.addEventListener("click", saveTodos);
els.refreshHistoryBtn.addEventListener("click", loadHistory);

els.tabs.forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

// ==================== 初始化 ====================

initBridge();
