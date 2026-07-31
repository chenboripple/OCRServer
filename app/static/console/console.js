const { createApp } = Vue;

createApp({
  data() {
    return {
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
        q: ""
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
      taskTableCanScroll: false,
      taskTableAtLeft: true,
      taskTableAtRight: true,
      copiedTip: "",
      timer: null
    };
  },
  computed: {
    taskPages() {
      return Math.max(1, Math.ceil((this.taskData.total || 0) / this.taskPageSize));
    },
    findingPages() {
      return Math.max(1, Math.ceil((this.findingData.total || 0) / this.findingPageSize));
    }
  },
  mounted() {
    this.applyStateFromUrl();
    this.reloadAll();
    window.addEventListener("resize", this.updateTaskTableScrollState);
    this.timer = window.setInterval(this.autoRefresh, 8000);
  },
  beforeUnmount() {
    if (this.timer) {
      window.clearInterval(this.timer);
    }
    window.removeEventListener("resize", this.updateTaskTableScrollState);
  },
  methods: {
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
      this.$nextTick(() => this.updateTaskTableScrollState());
      this.lastRefreshAt = new Date().toISOString();
    },
    async loadDashboard() {
      try {
        const resp = await fetch(this.apiUrl(`/api/console/dashboard?days=${this.dashboardDays}`));
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
        const params = new URLSearchParams();
        params.set("page", String(page));
        params.set("page_size", String(this.taskPageSize));
        for (const [k, v] of Object.entries(this.filters)) {
          if (v !== "") {
            params.set(k, v);
          }
        }
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
        this.$nextTick(() => this.updateTaskTableScrollState());
      } catch (e) {
        this.taskError = `Load tasks failed: ${String(e)}`;
      }
    },
    onTaskTableScroll() {
      this.updateTaskTableScrollState();
    },
    updateTaskTableScrollState() {
      const wrap = this.$refs.taskTableWrap;
      if (!wrap) {
        this.taskTableCanScroll = false;
        this.taskTableAtLeft = true;
        this.taskTableAtRight = true;
        return;
      }
      const maxScrollLeft = Math.max(0, wrap.scrollWidth - wrap.clientWidth);
      const canScroll = maxScrollLeft > 1;
      this.taskTableCanScroll = canScroll;
      if (!canScroll) {
        this.taskTableAtLeft = true;
        this.taskTableAtRight = true;
        return;
      }
      this.taskTableAtLeft = wrap.scrollLeft <= 1;
      this.taskTableAtRight = wrap.scrollLeft >= maxScrollLeft - 1;
    },
    async changeTaskPage(page) {
      if (page < 1 || page > this.taskPages) {
        return;
      }
      await this.loadTasks(page, true, true);
    },
    resetFilters() {
      this.filters = {
        status: "",
        source: "",
        project_id: "",
        mr_iid: "",
        approve: "",
        q: ""
      };
      this.taskPage = 1;
      this.selectedTaskId = "";
      this.findingFilters = { severity: "", category: "", path: "" };
      this.findingPage = 1;
      this.reloadAll();
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

      this.filters.status = read("status");
      this.filters.source = read("source");
      this.filters.project_id = read("project_id");
      this.filters.mr_iid = read("mr_iid");
      this.filters.approve = read("approve");
      this.filters.q = read("q");

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
