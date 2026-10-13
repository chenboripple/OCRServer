const { createApp } = Vue;

createApp({
  data() {
    return {
      // 界面语言。detectLang() 已按 ?lang= → localStorage → 浏览器语言 解析完毕,
      // 早于 mounted() 里任何 loader,所以 applyStateFromUrl 不需要再管它。
      lang: OcrI18n.detectLang(),
      // 浏览器语言单独留一份:判断 lang 是否为「非默认值」,决定要不要写进 URL。
      // 不能每次重算 —— URL 里已有 ?lang= 时会漂移。
      browserLang: OcrI18n.detectBrowserLang(),
      // 下拉枚举值取自 i18n 词表,与 statusLabel()/severityLabel() 共用一份,避免两处漂移。
      // 提交给 API 的始终是英文枚举,只有可见文字随语言变化。
      statusValues: OcrI18n.STATUS_VALUES,
      severityValues: OcrI18n.SEVERITY_VALUES,
      activeTab: "review",
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
      taskError: null,
      selectedTaskId: "",
      taskDetail: null,
      detailError: null,
      findingFilters: {
        severity: "",
        category: "",
        path: ""
      },
      findingData: { items: [], total: 0, page: 1, page_size: 50 },
      findingPage: 1,
      findingPageSize: 50,
      findingError: null,
      copiedTip: null,
      hoverTooltip: null,
      timer: null,
      // ── 配置页:推送配置 ──
      channels: [],
      channelForm: { open: false, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" },
      channelError: null,
      channelSaving: false,
      channelTesting: "",
      channelTestResult: null,
      // ── 配置页:Git 令牌 ──
      tokens: [],
      tokenForm: { open: false, editingId: "", name: "", token: "", tokenMasked: "" },
      tokenError: null,
      tokenSaving: false,
      tokenTesting: "",
      tokenTestResult: null,
      // ── 配置页:项目标签 ──
      tags: [],
      tagSaving: false,
      tagError: null,
      tagModal: { open: false, editingId: "", name: "" },
      // ── 配置页:Git 用户 ──
      users: [],
      userModal: { open: false, editingId: "", username: "", employeeNumber: "" },
      userSaving: false,
      userError: null,
      // ── 配置页:项目清单 ──
      projectData: { items: [], total: 0, page: 1, page_size: 50 },
      projectPage: 1,
      projectPageSize: 50,
      projectQuery: "",
      projectChannelFilter: "",  // ""=全部 / "none"=未绑定 / channel_id
      projectTagFilter: [],      // 标签筛选(可多选,任一命中)
      projectCreateModal: { open: false },
      projectConfigModal: { open: false, projectId: "", channelId: "", tokenId: "", tagSelection: [] },
      projectConfigSaving: false,
      projectForm: { project_id: "", project_url: "", channel_id: "", git_token_id: "" },
      projectError: null,
      projectSaving: false,
      projectBindError: null,
      // ── 通用确认模态框 ──
      // 存翻译键而非成串文本:弹窗开着时切语言才能跟着变。
      confirmModal: { open: false, titleKey: "", messageKey: "", params: null, noteKey: "", noteParams: null, confirmKey: "", loading: false, action: null }
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
  created() {
    // data() 初始化不会触发 watch,首屏的 <html lang> 与 <title> 要在这里落一次。
    this.applyLang(this.lang);
  },
  watch: {
    lang(value) {
      this.applyLang(value);
      // 语言是视图偏好,不进 URL —— 否则 applyFilters/reloadAll/selectTask 每次触发
      // syncStateToUrl 都会把它写进地址栏。
      try {
        window.localStorage.setItem(OcrI18n.STORAGE_KEY, value);
      } catch (_) {
        // 隐私模式下 localStorage 可能不可用,忽略即可
      }
    }
  },
  mounted() {
    this.applyStateFromUrl();
    // 标签所有 tab 都要用(看板筛选下拉 + 配置页签),无条件加载
    this.loadTags();
    if (this.activeTab === "review") {
      this.reloadAll();
    } else {
      this.loadConfigData();
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
      if (tab === "review") {
        if (!this.taskData.items.length) {
          this.reloadAll();
        }
        return;
      }
      // 任一配置页签都整组刷新:项目清单要显示通道与标签,数据量小,直接全量拉取
      this.loadConfigData();
    },
    async loadConfigData() {
      await Promise.all([this.loadChannels(), this.loadTokens(), this.loadProjects(1), this.loadTags(), this.loadUsers()]);
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
        this.channelError = this.msg("err.loadChannels", { e: String(e) });
      }
    },
    async loadProjects(page = 1) {
      this.projectBindError = null;
      this.projectPage = page;
      try {
        const params = new URLSearchParams();
        params.set("page", String(page));
        params.set("page_size", String(this.projectPageSize));
        if (this.projectQuery) {
          params.set("q", this.projectQuery);
        }
        if (this.projectChannelFilter) {
          params.set("channel", this.projectChannelFilter);
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
        this.projectBindError = this.msg("err.loadProjects", { e: String(e) });
      }
    },
    startChannelCreate() {
      this.channelForm = { open: true, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" };
      this.channelError = null;
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
      this.channelError = null;
    },
    cancelChannelForm() {
      this.channelForm = { open: false, editingId: "", name: "", type: "feishu", webhook_url: "", sign_secret: "", urlMasked: "", secretMasked: "" };
      this.channelError = null;
    },
    async submitChannel() {
      this.channelError = null;
      const form = this.channelForm;
      if (!form.name || !form.type) {
        this.channelError = this.msg("val.nameTypeRequired");
        return;
      }
      if (!form.editingId && !form.webhook_url) {
        this.channelError = this.msg("val.webhookRequired");
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
          throw await this.readError(resp);
        }
        this.cancelChannelForm();
        await this.loadChannels();
      } catch (e) {
        this.channelError = this.fail("err.save", e);
      } finally {
        this.channelSaving = false;
      }
    },
    askDeleteChannel(c) {
      const n = c.bound_project_count || 0;
      this.askConfirm({
        titleKey: "confirm.deleteChannel.title",
        messageKey: "confirm.deleteChannel.message",
        params: { name: c.name },
        noteKey: n > 0 ? "confirm.deleteChannel.bound" : "",
        noteParams: { n },
        confirmKey: "common.delete",
        action: async () => {
          try {
            const resp = await fetch(this.apiUrl(`/api/console/channels/${encodeURIComponent(c.channel_id)}`), { method: "DELETE" });
            if (!resp.ok) {
              throw await this.readError(resp);
            }
            await this.loadChannels();
            await this.loadProjects(this.projectPage);
          } catch (e) {
            this.channelError = this.fail("err.del", e);
          }
        }
      });
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
    // ── 配置页:Git 令牌 ────────────────────────────────
    async loadTokens() {
      try {
        const resp = await fetch(this.apiUrl("/api/console/tokens"));
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const data = await resp.json();
        this.tokens = data.items || [];
      } catch (e) {
        this.tokenError = this.msg("err.loadTokens", { e: String(e) });
      }
    },
    startTokenCreate() {
      this.tokenForm = { open: true, editingId: "", name: "", token: "", tokenMasked: "" };
      this.tokenError = null;
    },
    editToken(t) {
      this.tokenForm = {
        open: true,
        editingId: t.git_token_id,
        name: t.name,
        token: "",
        tokenMasked: t.token_masked
      };
      this.tokenError = null;
    },
    cancelTokenForm() {
      this.tokenForm = { open: false, editingId: "", name: "", token: "", tokenMasked: "" };
      this.tokenError = null;
    },
    async submitToken() {
      this.tokenError = null;
      const form = this.tokenForm;
      if (!form.name) {
        this.tokenError = this.msg("val.nameRequired");
        return;
      }
      if (!form.editingId && !form.token) {
        this.tokenError = this.msg("val.tokenRequired");
        return;
      }
      const payload = { name: form.name };
      // 编辑时留空 = 保持原值,不传该字段
      if (form.token) {
        payload.token = form.token;
      }
      this.tokenSaving = true;
      try {
        const resp = form.editingId
          ? await fetch(this.apiUrl(`/api/console/tokens/${encodeURIComponent(form.editingId)}`), {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload)
            })
          : await fetch(this.apiUrl("/api/console/tokens"), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload)
            });
        if (!resp.ok) {
          throw await this.readError(resp);
        }
        this.cancelTokenForm();
        await this.loadTokens();
      } catch (e) {
        this.tokenError = this.fail("err.save", e);
      } finally {
        this.tokenSaving = false;
      }
    },
    askDeleteToken(tok) {
      const n = tok.bound_project_count || 0;
      this.askConfirm({
        titleKey: "confirm.deleteToken.title",
        messageKey: "confirm.deleteToken.message",
        params: { name: tok.name },
        noteKey: n > 0 ? "confirm.deleteToken.bound" : "",
        noteParams: { n },
        confirmKey: "common.delete",
        action: async () => {
          try {
            const resp = await fetch(this.apiUrl(`/api/console/tokens/${encodeURIComponent(tok.git_token_id)}`), { method: "DELETE" });
            if (!resp.ok) {
              throw await this.readError(resp);
            }
            await this.loadTokens();
            await this.loadProjects(this.projectPage);
          } catch (e) {
            this.tokenError = this.fail("err.del", e);
          }
        }
      });
    },
    async testToken(t) {
      this.tokenTesting = t.git_token_id;
      this.tokenTestResult = null;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/tokens/${encodeURIComponent(t.git_token_id)}/test`), { method: "POST" });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.tokenTestResult = await resp.json();
      } catch (e) {
        this.tokenTestResult = { ok: false, error: String(e) };
      } finally {
        this.tokenTesting = "";
      }
      window.setTimeout(() => {
        this.tokenTestResult = null;
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
        this.tagError = this.msg("err.loadTags", { e: String(e) });
      }
    },
    openTagCreate() {
      this.tagModal = { open: true, editingId: "", name: "" };
      this.tagError = null;
    },
    openTagRename(t) {
      this.tagModal = { open: true, editingId: t.tag_id, name: t.name };
      this.tagError = null;
    },
    cancelTagModal() {
      this.tagModal = { open: false, editingId: "", name: "" };
      this.tagError = null;
    },
    async submitTagModal() {
      this.tagError = null;
      if (!this.tagModal.name) {
        this.tagError = this.msg("val.tagNameRequired");
        return;
      }
      this.tagSaving = true;
      try {
        const editing = this.tagModal.editingId;
        const resp = editing
          ? await fetch(this.apiUrl(`/api/console/tags/${encodeURIComponent(editing)}`), {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: this.tagModal.name })
            })
          : await fetch(this.apiUrl("/api/console/tags"), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: this.tagModal.name })
            });
        if (!resp.ok) {
          throw await this.readError(resp);
        }
        this.cancelTagModal();
        await this.loadTags();
      } catch (e) {
        this.tagError = this.fail("err.saveTag", e);
      } finally {
        this.tagSaving = false;
      }
    },
    askDeleteTag(tag) {
      const n = tag.project_count || 0;
      this.askConfirm({
        titleKey: "confirm.deleteTag.title",
        messageKey: "confirm.deleteTag.message",
        params: { name: tag.name },
        noteKey: n > 0 ? "confirm.deleteTag.bound" : "",
        noteParams: { n },
        confirmKey: "common.delete",
        action: async () => {
          try {
            const resp = await fetch(this.apiUrl(`/api/console/tags/${encodeURIComponent(tag.tag_id)}`), { method: "DELETE" });
            if (!resp.ok) {
              throw await this.readError(resp);
            }
            this.projectTagFilter = this.projectTagFilter.filter((id) => id !== tag.tag_id);
            if (this.filters.tag_id === tag.tag_id) {
              this.filters.tag_id = "";
            }
            await this.loadTags();
            await this.loadProjects(this.projectPage);
          } catch (e) {
            this.tagError = this.fail("err.del", e);
          }
        }
      });
    },
    // ── 配置页:Git 用户 ────────────────────────────────
    async loadUsers() {
      try {
        const resp = await fetch(this.apiUrl("/api/console/users"));
        if (!resp.ok) { throw new Error(`HTTP ${resp.status}`); }
        const data = await resp.json();
        this.users = data.items || [];
      } catch (e) {
        this.userError = this.msg("err.loadUsers", { e: String(e) });
      }
    },
    openUserEdit(u) {
      this.userModal = { open: true, editingId: u.user_id, username: u.username, employeeNumber: u.employee_number || "" };
      this.userError = null;
    },
    cancelUserModal() {
      this.userModal = { open: false, editingId: "", username: "", employeeNumber: "" };
      this.userError = null;
    },
    async submitUserModal() {
      this.userError = null;
      const form = this.userModal;
      this.userSaving = true;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/users/${encodeURIComponent(form.editingId)}`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ employee_number: form.employeeNumber })
        });
        if (!resp.ok) {
          throw await this.readError(resp);
        }
        this.cancelUserModal();
        await this.loadUsers();
      } catch (e) {
        this.userError = this.fail("err.saveEmployee", e);
      } finally {
        this.userSaving = false;
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
    resetProjectFilters() {
      this.projectQuery = "";
      this.projectChannelFilter = "";
      this.projectTagFilter = [];
      this.loadProjects(1);
    },
    tagColorClass(name) {
      let hash = 0;
      const s = String(name || "");
      for (let i = 0; i < s.length; i++) {
        hash = (hash * 31 + s.charCodeAt(i)) >>> 0;
      }
      return `tc-${hash % 5}`;
    },
    // ── 通用确认模态框 ─────────────────────────────────
    // 收翻译键而非成串文本:弹窗开着时切语言,标题/正文/按钮都会跟着变。
    askConfirm({ titleKey, messageKey, params, noteKey, noteParams, confirmKey, action }) {
      this.confirmModal = {
        open: true,
        titleKey,
        messageKey,
        params: params || null,
        noteKey: noteKey || "",
        noteParams: noteParams || null,
        confirmKey: confirmKey || "common.confirm",
        loading: false,
        action
      };
    },
    // 正文 = 主句 +(可选)影响范围补充句。补充句带复数。
    confirmMessage() {
      const m = this.confirmModal;
      if (!m.messageKey) {
        return "";
      }
      const base = this.t(m.messageKey, m.params);
      if (!m.noteKey) {
        return base;
      }
      const n = m.noteParams ? m.noteParams.n : 0;
      return `${base}${this.t("confirm.noteJoin")}${this.tc(m.noteKey, n, m.noteParams)}`;
    },
    closeConfirm() {
      this.confirmModal = { open: false, titleKey: "", messageKey: "", params: null, noteKey: "", noteParams: null, confirmKey: "", loading: false, action: null };
    },
    async confirmModalAction() {
      const action = this.confirmModal.action;
      if (!action) {
        this.closeConfirm();
        return;
      }
      this.confirmModal.loading = true;
      try {
        await action();
      } finally {
        this.closeConfirm();
      }
    },
    openProjectCreate() {
      this.projectForm = { project_id: "", project_url: "", channel_id: "", git_token_id: "" };
      this.projectError = null;
      this.projectCreateModal = { open: true };
    },
    cancelProjectCreate() {
      this.projectCreateModal = { open: false };
      this.projectError = null;
    },
    async submitProject() {
      this.projectError = null;
      if (!this.projectForm.project_id) {
        this.projectError = this.msg("val.projectIdRequired");
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
        if (this.projectForm.git_token_id) {
          payload.git_token_id = this.projectForm.git_token_id;
        }
        const resp = await fetch(this.apiUrl("/api/console/projects"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        if (!resp.ok) {
          throw await this.readError(resp);
        }
        this.cancelProjectCreate();
        await this.loadProjects(1);
      } catch (e) {
        this.projectError = this.fail("err.add", e);
      } finally {
        this.projectSaving = false;
      }
    },
    openProjectConfig(p) {
      this.projectConfigModal = {
        open: true,
        projectId: p.project_id,
        channelId: p.channel ? p.channel.channel_id : "",
        tokenId: p.git_token ? p.git_token.git_token_id : "",
        tagSelection: (p.tags || []).map((t) => t.tag_id)
      };
      this.projectBindError = null;
    },
    cancelProjectConfig() {
      this.projectConfigModal = { open: false, projectId: "", channelId: "", tokenId: "", tagSelection: [] };
      this.projectBindError = null;
    },
    toggleConfigTagSelection(tagId) {
      const sel = this.projectConfigModal.tagSelection;
      this.projectConfigModal.tagSelection = sel.includes(tagId)
        ? sel.filter((id) => id !== tagId)
        : [...sel, tagId];
    },
    async saveProjectConfig() {
      const m = this.projectConfigModal;
      this.projectBindError = null;
      this.projectConfigSaving = true;
      try {
        const resp = await fetch(this.apiUrl(`/api/console/projects/${encodeURIComponent(m.projectId)}/channel`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_id: m.channelId || null })
        });
        if (!resp.ok) {
          throw await this.readError(resp);
        }
        const resp2 = await fetch(this.apiUrl(`/api/console/projects/${encodeURIComponent(m.projectId)}/token`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ git_token_id: m.tokenId || null })
        });
        if (!resp2.ok) {
          throw await this.readError(resp2);
        }
        const resp3 = await fetch(this.apiUrl(`/api/console/projects/${encodeURIComponent(m.projectId)}/tags`), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tag_ids: m.tagSelection })
        });
        if (!resp3.ok) {
          throw await this.readError(resp3);
        }
        this.cancelProjectConfig();
        await this.loadProjects(this.projectPage);
      } catch (e) {
        this.projectBindError = this.fail("err.save", e);
      } finally {
        this.projectConfigSaving = false;
      }
    },
    // ── i18n ───────────────────────────────────────────
    t(key, params) {
      return OcrI18n.translate(this.lang, key, params);
    },
    // 带复数的变体。中文无需区分,英文按 n === 1 选 _one/_other。
    tc(key, n, params) {
      return OcrI18n.translate(this.lang, key, Object.assign({ n: n }, params), n);
    },
    // 消息描述符:存键 + 参数,渲染时才翻译,切换语言即时生效。
    msg(key, params) {
      return { key: key, params: params || null };
    },
    // 带前缀的嵌套描述符:先渲染 cause,再代入 prefix 的 {e} 占位。
    wrap(prefixKey, cause) {
      return { key: prefixKey, cause: cause };
    },
    // 把 readError 产出的描述符或普通异常统一成 cause。
    toCause(e) {
      return e && e.key ? e : this.msg("err.raw", { msg: String((e && e.message) || e) });
    },
    fail(prefixKey, e) {
      return this.wrap(prefixKey, this.toCause(e));
    },
    msgText(m) {
      if (!m || !m.key) {
        return "";
      }
      if (m.cause) {
        return this.t(m.key, { e: this.msgText(m.cause) });
      }
      return this.t(m.key, m.params);
    },
    statusLabel(status) {
      if (!status) return "-";
      const key = `status.${status}`;
      const text = this.t(key);
      return text === key ? status : text;
    },
    severityLabel(severity) {
      const key = `severity.${severity || "unknown"}`;
      const text = this.t(key);
      return text === key ? (severity || "unknown") : text;
    },
    typeLabel(type) {
      const key = `channelType.${type}`;
      const text = this.t(key);
      return text === key ? type : text;
    },
    applyLang(value) {
      document.documentElement.lang = value;
      document.title = this.t("doc.title");
    },
    toggleLang() {
      this.lang = this.lang === "zh-CN" ? "en" : "zh-CN";
    },
    langLabel() {
      // 按钮显示目标语言,且始终用目标语言自身书写,便于识别。
      return this.lang === "zh-CN" ? "English" : "中文";
    },
    // 把响应体解析成消息描述符。三种形态都要兜住:
    // 结构化 {code,...} / FastAPI 422 的对象数组 / 裸字符串。
    async readError(resp) {
      const body = await resp.json().catch(() => null);
      const detail = body ? body.detail : undefined;
      if (Array.isArray(detail)) {
        const msg = detail.map((item) => (item && item.msg) || "").filter(Boolean).join("; ");
        return this.msg("err.validation", { msg: msg || `HTTP ${resp.status}` });
      }
      if (detail && typeof detail === "object" && detail.code) {
        return this.msg(`apierr.${detail.code}`, detail);
      }
      if (typeof detail === "string" && detail) {
        return this.msg("err.raw", { msg: detail });
      }
      return this.msg("err.http", { status: resp.status });
    },
    // 从测试类接口的 {ok, error|error_code} 出参取错误文案。
    testErrorText(result) {
      if (!result || result.ok) return "";
      if (result.error_code) {
        const key = `apierr.${result.error_code}`;
        const text = this.t(key);
        if (text !== key) return text;
      }
      return result.error ? String(result.error) : this.t("err.http", { status: "" });
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
      this.taskError = null;
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
        this.taskError = this.msg("err.loadTasks", { e: String(e) });
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
      this.detailError = null;
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
        this.detailError = this.msg("err.loadDetail", { e: String(e) });
      }
    },
    async loadFindings(page = 1, reset = true, syncUrl = true) {
      if (!this.selectedTaskId) {
        this.findingData = { items: [], total: 0, page: 1, page_size: this.findingPageSize };
        return;
      }
      this.findingError = null;
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
        this.findingError = this.msg("err.loadFindings", { e: String(e) });
      }
    },
    syncStateToUrl() {
      const params = new URLSearchParams();
      if (this.activeTab !== "review") {
        params.set("tab", this.activeTab);
      }
      // 非默认语言才入 URL:让本地化链接可分享,同时不给默认状态的地址栏添噪音。
      // 沿用本文件「默认值省略」的既有约定(见 tab / task_page / days)。
      if (this.lang !== this.browserLang) {
        params.set("lang", this.lang);
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

      // 页签:projects | channels | tokens | tags;旧链接 tab=config 归入 projects
      const tab = read("tab", "review");
      this.activeTab = ["projects", "channels", "tokens", "tags", "users"].includes(tab) ? tab : (tab === "config" ? "projects" : "review");

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
        this.copiedTip = this.msg("detail.copied");
      } catch (_) {
        this.copiedTip = this.msg("detail.copyFailed");
      }
      window.setTimeout(() => {
        this.copiedTip = null;
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
    // 收整个任务对象而非两个时间戳:耗时要不要按「当前时间」继续走,取决于状态。
    // 终态(done/failed/cancelled/superseded)不再推进,缺 finished_at 只能显示 "-",
    // 否则 8 秒轮询会让已取消的任务一直涨(历史库里就有这类 finished_at 为空的行)。
    elapsedSeconds(task) {
      if (!task || !task.started_at) {
        return "-";
      }
      const start = this.parseTime(task.started_at);
      if (!start) {
        return "-";
      }
      const end = this.parseTime(task.finished_at)
        || (OcrI18n.TERMINAL_STATUSES.includes(task.status) ? null : new Date());
      if (!end) {
        return "-";
      }
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
        return new Intl.NumberFormat(this.lang === "zh-CN" ? "zh-CN" : "en-US").format(v || 0);
      } catch (_) {
        return String(v || 0);
      }
    }
  }
}).mount("#app");
