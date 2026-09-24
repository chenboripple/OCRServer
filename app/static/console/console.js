const { createApp } = Vue;

createApp({
  data() {
    return {
      activeTab: "review",
      configLoaded: false,
      lastRefreshAt: null,
      dashboardDays: 14,
      dashboard: {
        overview: {},
        finding_distribution: {},
        stats: {},
        trend: []
      },
      filters: {
        status: "",
        source: "",
        project_id: "",
        mr_iid: "",
        approve: "",
        q: "",
        tag_id: ""
      },
      taskData: { items: [], total: 0, page: 1, page_size: 20 },
      taskPage: 1,
      taskPageSize: 20,
      taskError: "",
      selectedTaskId: "",
      taskDetail: null,
      detailError: "",
      findingFilters: {
        severity: "",
        category: "",
        path: ""
      },
      findingData: { items: [], total: 0, page: 1, page_size: 50 },
      findingPage: 1,
      findingPageSize: 50,
      findingError: "",
      copiedTip: "",
      hoverTooltip: null,
      timer: null,
      // ── 配置页:推送配置 ──
      channels: [],
      channelForm: { open: false, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" },
      channelError: "",
      channelSaving: false,
      channelTesting: "",
      channelTestResult: null,
      // ── 配置页:项目标签 ──
      tags: [],
      tagForm: { name: "" },
      tagSaving: false,
      tagError: "",
      tagEditId: "",
      tagEditName: "",
      projectTagFilter: [],      // 项目清单标签筛选(可多选,任一命中)
      editingTagsFor: "",        // 正在编辑标签的 project_id
      tagEditSelection: [],      // 编辑中的已选 tag_id
      projectTagsSaving: false,
      // ── 配置页:项目清单 ──
      projectData: { items: [], total: 0, page: 1, page_size: 50 },
      projectPage: 1,
      projectPageSize: 50,
      projectQuery: "",
      projectForm: { project_id: "", project_url: "", channel_id: "" },
      projectError: "",
      projectSaving: false,
      projectBindError: ""
    };
  },
  computed: {
    taskPages() {
      return Math.max(1, Math.ceil((this.taskData.total || 0) / this.taskPageSize));
    },
    findingPages() {
      return Math.max(1, Math.ceil((this.findingData.total || 0) / this.findingPageSize));
    },
    projectPages() {
      return Math.max(1, Math.ceil((this.projectData.total || 0) / this.projectPageSize));
    }
  },
  mounted() {
    this.applyStateFromUrl();
    // 标签两个 tab 都要用(看板筛选下拉 + 配置页管理/打标),无条件加载
    this.loadTags();
    if (this.activeTab === "config") {
      this.loadConfig();
    } else {
      this.reloadAll();
    }
    this.timer = window.setInterval(this.autoRefresh, 8000);
  },
  beforeUnmount() {
    if (this.timer) {
      window.clearInterval(this.timer);
    }
  },
  methods: {
    switchTab(tab) {
      this.activeTab = tab;
      this.syncStateToUrl();
      if (tab === "config" && !this.configLoaded) {
        this.loadConfig();
      }
      if (tab === "review" && !this.taskData.items.length) {
        this.reloadAll();
      }
    },
    async loadConfig() {
      await Promise.all([this.loadChannels(), this.loadProjects(1), this.loadTags()]);
      this.configLoaded = true;
    },
    async loadChannels() {
      try {
        const resp = await fetch(this.apiUrl("/api/console/channels"));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const data = await resp.json();
        this.channels = data.items || [];
      } catch (e) {
        this.channelError = `加载推送配置失败: ${String(e)}`;
      }
    },
    async loadProjects(page = 1) {
      this.projectBindError = "";
      this.projectPage = page;
      try {
        const params = new URLSearchParams();
        params.set("page", String(page));
        params.set("page_size", String(this.projectPageSize));
        if (this.projectQuery) {
          params.set("q", this.projectQuery);
        }
        for (const tagId of this.projectTagFilter) {
          params.append("tag_id", tagId);
        }
        const resp = await fetch(this.apiUrl(`/api/console/projects?${params.toString()}`));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.projectData = await resp.json();
      } catch (e) {
        this.projectBindError = `加载项目清单失败: ${String(e)}`;
      }
    },
    startChannelCreate() {
      this.channelForm = { open: true, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" };
      this.channelError = "";
    },
    editChannel(c) {
      this.channelForm = {
        open: true,
        editingId: c.channel_id,
        name: c.name,
        type: c.type,
        webhook_url: "",
        sign_secret: "",
        urlMasked: c.webhook_url_masked,
        secretMasked: c.sign_secret_masked
      };
      this.channelError = "";
    },
    cancelChannelForm() {
      this.channelForm = { open: false, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" };
      this.channelError = "";
    },
    async submitChannel() {
      this.channelError = "";
      const form = this.channelForm;
      if (!form.name || !form.type) {
        this.channelError = "名称与类型必填";
        return;
      }
      if (!form.editingId && !form.webhook_url) {
        this.channelError = "Webhook URL 必填";
        return;
      }
      const payload = { name: form.name, type: form.type };
      // 编辑时留空 = 保持原值,不传该字段
      if (form.webhook_url) {
        payload.webhook_url = form.webhook_url;
      }
      if (form.sign_secret) {
        payload.sign_secret = form.sign_secret;
      }
      this.channelSaving = true;
      try {
        const resp = form.editingId
          ? await fetch(this.apiUrl(`/api/console/channels/${encodeURIComponent(form.editingId)}`), {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload)
            })
          : await fetch(this.apiUrl("/api/console/channels"), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload)
            });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        this.cancelChannelForm();
        await this.loadChannels();
      } catch (e) {
        this.channelError = `保存失败: ${String(e)}`;
      } finally {
        this.channelSaving = false;
      }
    },
    async deleteChannel(c) {
      const bound = c.bound_project_count > 0 ? `删除后 ${c.bound_project_count} 个绑定项目将回退全局配置。` : "";
      if (!window.confirm(`确定删除推送配置「${c.name}」?${bound}`)) {
        return;
      }
      try {
        const resp = await fetch(this.apiUrl(`/api/console/channels/${encodeURIComponent(c.channel_id)}`), { method: "DELETE" });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        await this.loadChannels();
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.channelError = `删除失败: ${String(e)}`;
      }
    },
    async testChannel(c) {
      this.channelTesting = c.channel_id;
      this.channelTestResult = null;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/channels/${encodeURIComponent(c.channel_id)}/test`), { method: "POST" });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.channelTestResult = await resp.json();
      } catch (e) {
        this.channelTestResult = { ok: false, error: String(e) };
      } finally {
        this.channelTesting = "";
      }
      window.setTimeout(() => {
        this.channelTestResult = null;
      }, 5000);
    },
    // ── 配置页:项目标签 ────────────────────────────────
    async loadTags() {
      try {
        const resp = await fetch(this.apiUrl("/api/console/tags"));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const data = await resp.json();
        this.tags = data.items || [];
      } catch (e) {
        this.tagError = `加载标签失败: ${String(e)}`;
      }
    },
    async submitTag() {
      this.tagError = "";
      const name = this.tagForm.name;
      if (!name) {
        this.tagError = "标签名必填";
        return;
      }
      this.tagSaving = true;
      try {
        const resp = await fetch(this.apiUrl("/api/console/tags"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name })
        });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        this.tagForm.name = "";
        await this.loadTags();
      } catch (e) {
        this.tagError = `添加标签失败: ${String(e)}`;
      } finally {
        this.tagSaving = false;
      }
    },
    startTagEdit(t) {
      this.tagEditId = t.tag_id;
      this.tagEditName = t.name;
      this.tagError = "";
    },
    cancelTagEdit() {
      this.tagEditId = "";
      this.tagEditName = "";
      this.tagError = "";
    },
    async saveTagEdit() {
      this.tagError = "";
      if (!this.tagEditName) {
        this.tagError = "标签名必填";
        return;
      }
      this.tagSaving = true;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/tags/${encodeURIComponent(this.tagEditId)}`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: this.tagEditName })
        });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        this.cancelTagEdit();
        await this.loadTags();
        // 标签名变了,项目清单里的 chip 也要刷新
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.tagError = `重命名失败: ${String(e)}`;
      } finally {
        this.tagSaving = false;
      }
    },
    async deleteTag(t) {
      const bound = t.project_count > 0 ? `该标签已用于 ${t.project_count} 个项目,删除后同步解除绑定。` : "";
      if (!window.confirm(`确定删除标签「${t.name}」?${bound}`)) {
        return;
      }
      this.tagError = "";
      try {
        const resp = await fetch(this.apiUrl(`/api/console/tags/${encodeURIComponent(t.tag_id)}`), { method: "DELETE" });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.projectTagFilter = this.projectTagFilter.filter((id) => id !== t.tag_id);
        if (this.filters.tag_id === t.tag_id) {
          this.filters.tag_id = "";
        }
        await this.loadTags();
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.tagError = `删除失败: ${String(e)}`;
      }
    },
    toggleProjectTagFilter(tagId) {
      if (this.projectTagFilter.includes(tagId)) {
        this.projectTagFilter = this.projectTagFilter.filter((id) => id !== tagId);
      } else {
        this.projectTagFilter = [...this.projectTagFilter, tagId];
      }
      this.loadProjects(1);
    },
    clearProjectTagFilter() {
      this.projectTagFilter = [];
      this.loadProjects(1);
    },
    startProjectTagsEdit(p) {
      this.editingTagsFor = p.project_id;
      this.tagEditSelection = (p.tags || []).map((t) => t.tag_id);
      this.projectBindError = "";
    },
    cancelProjectTagsEdit() {
      this.editingTagsFor = "";
      this.tagEditSelection = [];
    },
    toggleTagEditSelection(tagId) {
      if (this.tagEditSelection.includes(tagId)) {
        this.tagEditSelection = this.tagEditSelection.filter((id) => id !== tagId);
      } else {
        this.tagEditSelection = [...this.tagEditSelection, tagId];
      }
    },
    async saveProjectTags(p) {
      this.projectBindError = "";
      this.projectTagsSaving = true;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/projects/${encodeURIComponent(p.project_id)}/tags`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tag_ids: this.tagEditSelection })
        });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        this.cancelProjectTagsEdit();
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.projectBindError = `保存标签失败: ${String(e)}`;
      } finally {
        this.projectTagsSaving = false;
      }
    },
    tagColorClass(name) {
      let hash = 0;
      const s = String(name || "");
      for (let i = 0; i < s.length; i++) {
        hash = (hash * 31 + s.charCodeAt(i)) >>> 0;
      }
      return `tc-${hash % 5}`;
    },
    async submitProject() {
      this.projectError = "";
      if (!this.projectForm.project_id) {
        this.projectError = "Project ID 必填";
        return;
      }
      this.projectSaving = true;
      try {
        const payload = {
          project_id: this.projectForm.project_id,
          project_url: this.projectForm.project_url
        };
        if (this.projectForm.channel_id) {
          payload.channel_id = this.projectForm.channel_id;
        }
        const resp = await fetch(this.apiUrl("/api/console/projects"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        this.projectForm = { project_id: "", project_url: "", channel_id: "" };
        await this.loadProjects(1);
      } catch (e) {
        this.projectError = `添加失败: ${String(e)}`;
      } finally {
        this.projectSaving = false;
      }
    },
    async bindProject(p, channelId) {
      this.projectBindError = "";
      try {
        const resp = await fetch(this.apiUrl(`/api/console/projects/${encodeURIComponent(p.project_id)}/channel`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: channelId || null })
        });
        if (!resp.ok) {
          const detail = await resp.json().catch(() => ({}));
          throw new Error(detail.detail || `HTTP ${resp.status}`);
        }
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.projectBindError = `绑定失败: ${String(e)}`;
        await this.loadProjects(this.projectPage);
      }
    },
    typeLabel(type) {
      return { feishu: "飞书", wechat: "企业微信", dingtalk: "钉钉" }[type] || type;
    },
    apiBase() {
      const path = window.location.pathname || "";
      const marker = "/console";
      const idx = path.lastIndexOf(marker);
      if (idx <= 0) {
        return "";
      }
      return path.slice(0, idx);
    },
    apiUrl(path) {
      const base = this.apiBase();
      return `${base}${path}`;
    },
    async autoRefresh() {
      if (this.activeTab !== "review") {
        return;
      }
      const hasActive = this.taskData.items.some((t) => t.status === "queued" || t.status === "running");
      if (!hasActive) {
        return;
      }
      await this.loadTasks(this.taskPage, false, false);
      if (this.selectedTaskId) {
        await this.loadTaskDetail(this.selectedTaskId, false);
        await this.loadFindings(this.findingPage, false, false);
      }
      this.lastRefreshAt = new Date().toISOString();
    },
    async reloadAll() {
      await Promise.all([this.loadDashboard(), this.loadTasks(this.taskPage, true, false)]);
      if (this.selectedTaskId) {
        const exists = this.taskData.items.some((i) => i.task_id === this.selectedTaskId);
        // Keep detail visible even when current list filters exclude this task.
        await this.loadTaskDetail(this.selectedTaskId, true);
        await this.loadFindings(this.findingPage, true, false);
        if (!exists && this.taskData.items.length > 0) {
          await this.selectTask(this.taskData.items[0].task_id);
        }
      } else if (this.taskData.items.length > 0) {
        await this.selectTask(this.taskData.items[0].task_id);
      }
      this.syncStateToUrl();
      this.lastRefreshAt = new Date().toISOString();
    },
    async loadDashboard() {
      try {
        const params = this.taskQueryParams();
        const resp = await fetch(this.apiUrl(`/api/console/dashboard?${params.toString()}`));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.dashboard = await resp.json();
      } catch (e) {
        console.error(e);
      }
    },
    async loadTasks(page = 1, updateSelection = true, syncUrl = true) {
      this.taskError = "";
      this.taskPage = page;
      try {
        const params = this.taskQueryParams();
        params.set("page", String(page));
        params.set("page_size", String(this.taskPageSize));
        const resp = await fetch(this.apiUrl(`/api/console/tasks?${params.toString()}`));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.taskData = await resp.json();
        if (updateSelection && this.selectedTaskId) {
          const exists = this.taskData.items.some((i) => i.task_id === this.selectedTaskId);
          if (!exists && this.taskData.items.length) {
            await this.selectTask(this.taskData.items[0].task_id);
          }
        }
        if (syncUrl) {
          this.syncStateToUrl();
        }
      } catch (e) {
        this.taskError = `Load tasks failed: ${String(e)}`;
      }
    },
    async changeTaskPage(page) {
      if (page < 1 || page > this.taskPages) {
        return;
      }
      await this.loadTasks(page, true, true);
    },
    taskQueryParams() {
      const params = new URLSearchParams();
      params.set("days", String(this.dashboardDays));
      for (const [k, v] of Object.entries(this.filters)) {
        if (v !== "") {
          params.set(k, v);
        }
      }
      return params;
    },
    async applyFilters() {
      this.taskPage = 1;
      this.selectedTaskId = "";
      this.taskDetail = null;
      this.findingData = { items: [], total: 0, page: 1, page_size: this.findingPageSize };
      await this.reloadAll();
    },
    resetFilters() {
      this.filters = {
        status: "",
        source: "",
        project_id: "",
        mr_iid: "",
        approve: "",
        q: "",
        tag_id: ""
      };
      this.taskPage = 1;
      this.findingFilters = { severity: "", category: "", path: "" };
      this.findingPage = 1;
      this.applyFilters();
    },
    async selectTask(taskId) {
      this.selectedTaskId = taskId;
      this.findingPage = 1;
      await this.loadTaskDetail(taskId, true);
      await this.loadFindings(1, true, true);
      this.syncStateToUrl();
    },
    async loadTaskDetail(taskId, reset = false) {
      this.detailError = "";
      if (reset) {
        this.taskDetail = null;
      }
      try {
        const resp = await fetch(this.apiUrl(`/api/console/tasks/${encodeURIComponent(taskId)}`));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.taskDetail = await resp.json();
      } catch (e) {
        this.detailError = `Load detail failed: ${String(e)}`;
      }
    },
    async loadFindings(page = 1, reset = true, syncUrl = true) {
      if (!this.selectedTaskId) {
        this.findingData = { items: [], total: 0, page: 1, page_size: this.findingPageSize };
        return;
      }
      this.findingError = "";
      if (reset) {
        this.findingData = { items: [], total: 0, page: 1, page_size: this.findingPageSize };
      }
      this.findingPage = page;
      try {
        const params = new URLSearchParams();
        params.set("page", String(page));
        params.set("page_size", String(this.findingPageSize));
        for (const [k, v] of Object.entries(this.findingFilters)) {
          if (v !== "") {
            params.set(k, v);
          }
        }
        const resp = await fetch(this.apiUrl(`/api/console/tasks/${encodeURIComponent(this.selectedTaskId)}/findings?${params.toString()}`));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.findingData = await resp.json();
        if (syncUrl) {
          this.syncStateToUrl();
        }
      } catch (e) {
        this.findingError = `Load findings failed: ${String(e)}`;
      }
    },
    syncStateToUrl() {
      const params = new URLSearchParams();
      if (this.activeTab === "config") {
        params.set("tab", "config");
      }
      for (const [k, v] of Object.entries(this.filters)) {
        if (v !== "") {
          params.set(k, String(v));
        }
      }
      if (this.taskPage > 1) {
        params.set("task_page", String(this.taskPage));
      }
      if (this.dashboardDays !== 14) {
        params.set("days", String(this.dashboardDays));
      }
      if (this.selectedTaskId) {
        params.set("task_id", this.selectedTaskId);
      }
      for (const [k, v] of Object.entries(this.findingFilters)) {
        if (v !== "") {
          params.set(`f_${k}`, String(v));
        }
      }
      if (this.findingPage > 1) {
        params.set("finding_page", String(this.findingPage));
      }

      const query = params.toString();
      const target = query ? `${window.location.pathname}?${query}` : window.location.pathname;
      window.history.replaceState(null, "", target);
    },
    applyStateFromUrl() {
      const params = new URLSearchParams(window.location.search);
      const read = (k, fallback = "") => params.get(k) ?? fallback;

      this.activeTab = read("tab") === "config" ? "config" : "review";

      this.filters.status = read("status");
      this.filters.source = read("source");
      this.filters.project_id = read("project_id");
      this.filters.mr_iid = read("mr_iid");
      this.filters.approve = read("approve");
      this.filters.q = read("q");
      this.filters.tag_id = read("tag_id");

      const taskPage = Number(read("task_page", "1"));
      this.taskPage = Number.isFinite(taskPage) && taskPage > 0 ? taskPage : 1;

      const days = Number(read("days", "14"));
      this.dashboardDays = Number.isFinite(days) && days > 0 ? days : 14;

      this.selectedTaskId = read("task_id");
      this.findingFilters.severity = read("f_severity");
      this.findingFilters.category = read("f_category");
      this.findingFilters.path = read("f_path");

      const findingPage = Number(read("finding_page", "1"));
      this.findingPage = Number.isFinite(findingPage) && findingPage > 0 ? findingPage : 1;
    },
    async copySessionId() {
      if (!this.taskDetail || !this.taskDetail.session_id) {
        return;
      }
      try {
        await navigator.clipboard.writeText(this.taskDetail.session_id);
        this.copiedTip = "session_id copied";
      } catch (_) {
        this.copiedTip = "copy failed";
      }
      window.setTimeout(() => {
        this.copiedTip = "";
      }, 1800);
    },
    showTooltip(event, text) {
      if (!text) {
        return;
      }
      const rect = event.currentTarget.getBoundingClientRect();
      const width = Math.min(520, window.innerWidth * 0.7);
      const left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8);
      const below = rect.bottom + 6;
      const top = below + 240 <= window.innerHeight ? below : Math.max(8, rect.top - 246);
      this.hoverTooltip = { text, left, top, width };
    },
    hideTooltip() {
      this.hoverTooltip = null;
    },
    short(s) {
      if (!s) {
        return "";
      }
      if (s.length <= 8) {
        return s;
      }
      return `${s.slice(0, 8)}...`;
    },
    projectName(task) {
      if (!task) {
        return "-";
      }
      const direct = String(task.project_name || "").trim();
      if (direct) {
        return direct;
      }
      const fromUrl = String(task.project_url || "").trim();
      if (!fromUrl) {
        return String(task.project_id || "-");
      }
      let segment = "";
      try {
        const pathname = new URL(fromUrl).pathname || "";
        const parts = pathname.split("/").filter(Boolean);
        segment = parts.length ? parts[parts.length - 1] : "";
      } catch (_) {
        const cleaned = fromUrl.split("?")[0].split("#")[0];
        const parts = cleaned.split("/").filter(Boolean);
        segment = parts.length ? parts[parts.length - 1] : "";
      }
      let name = segment;
      try {
        name = decodeURIComponent(segment);
      } catch (_) {
        name = segment;
      }
      name = name.replace(/\.git$/i, "");
      return name || String(task.project_id || "-");
    },
    compactTime(s) {
      if (!s) {
        return "-";
      }
      return s.replace("T", " ").slice(0, 19);
    },
    elapsedSeconds(startedAt, finishedAt) {
      if (!startedAt) {
        return "-";
      }
      const start = this.parseTime(startedAt);
      if (!start) {
        return "-";
      }
      const end = this.parseTime(finishedAt) || new Date();
      const diffMs = end.getTime() - start.getTime();
      if (!Number.isFinite(diffMs) || diffMs < 0) {
        return "-";
      }
      return (diffMs / 1000).toFixed(1);
    },
    parseTime(raw) {
      if (!raw) {
        return null;
      }
      const normalized = String(raw).replace(" ", "T");
      const t = new Date(normalized);
      return Number.isNaN(t.getTime()) ? null : t;
    },
    formatTime(s) {
      return s ? this.compactTime(s) : "-";
    },
    pretty(obj) {
      return JSON.stringify(obj, null, 2);
    },
    pct(v) {
      return `${(v * 100).toFixed(1)}%`;
    },
    number(v) {
      try {
        return new Intl.NumberFormat("en-US").format(v || 0);
      } catch (_) {
        return String(v || 0);
      }
    }
  }
}).mount("#app");
