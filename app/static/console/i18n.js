/* 控制台文案词典与语言探测。
 *
 * 只暴露一个全局 OcrI18n:词典 + 语言探测 + 状态词表。
 * 翻译函数 t()/tc() 在 console.js 里作为 Vue method 实现,靠 lang 的响应式自动重渲染。
 * 必须在 console.js 之前加载(两者都是 defer,按文档顺序执行)。
 */
(function () {
  "use strict";

  var STORAGE_KEY = "ocr-console-lang";
  var LANGS = ["zh-CN", "en"];

  // 与后端 app/storage/repositories/task_repository.py 的 TERMINAL_STATUSES 对齐。
  // 终态任务的耗时不再按当前时间增长;改这里要同步改后端。
  var TERMINAL_STATUSES = ["done", "failed", "cancelled", "superseded"];
  var STATUS_VALUES = ["queued", "running", "done", "failed", "cancelled", "superseded"];
  // 可筛选的严重程度。unknown 只是缺失时的展示回落,不进筛选项。
  var SEVERITY_VALUES = ["critical", "high", "medium", "low"];

  // 词典体内不放注释、值不带裸键:tests/test_console.py 会把它转成 JSON 校验中英键集一致。
  // confirm.noteJoin 是确认弹窗「基句 + 补充句」的连接符 —— 中文紧排,英文两句话之间要空格。
  var MESSAGES = {
    "zh-CN": {
      doc: { title: "OCR Server 控制台" },
      lang: { switchTo: "切换到英文" },

      header: {
        title: "OCR 审核控制台",
        subtitle: "审核请求、分析结果与推送配置的统一入口",
        lastRefresh: "上次刷新："
      },

      nav: {
        review: "审核看板",
        projects: "项目清单",
        channels: "推送配置",
        tokens: "Git 令牌",
        tags: "项目标签",
        users: "Git 用户"
      },

      common: {
        all: "全部",
        refresh: "刷新",
        search: "查询",
        reset: "重置",
        cancel: "取消",
        save: "保存",
        create: "创建",
        add: "添加",
        edit: "编辑",
        delete: "删除",
        close: "关闭",
        confirm: "确定",
        prev: "上一页",
        next: "下一页",
        test: "测试",
        page: "第 {cur} / {total} 页",
        saving: "保存中...",
        processing: "处理中...",
        sending: "发送中...",
        testing: "测试中...",
        name: "名称",
        type: "类型",
        required: "*",
        optional: "(可选)",
        keepBlank: "(留空保持不变)"
      },

      status: {
        queued: "排队中",
        running: "进行中",
        done: "已完成",
        failed: "失败",
        cancelled: "已取消",
        superseded: "已跳过"
      },

      severity: {
        critical: "严重",
        high: "高",
        medium: "中",
        low: "低",
        unknown: "未知"
      },

      approve: { pass: "通过", reject: "拒绝" },

      channelType: { feishu: "飞书", wechat: "企业微信", dingtalk: "钉钉" },

      dashboard: {
        title: "看板",
        days7: "近 7 天",
        days14: "近 14 天",
        days30: "近 30 天",
        totalTasks: "任务总数",
        done: "已完成",
        failed: "失败",
        queuedRunning: "排队 + 进行中",
        approveRate: "通过率",
        totalTokens: "Token 总量"
      },

      filters: {
        status: "状态",
        source: "来源",
        approve: "结论",
        projectId: "项目 ID",
        projectIdPh: "项目 ID",
        tag: "标签",
        mrIid: "MR IID",
        mrIidPh: "MR IID",
        keyword: "关键词",
        keywordPh: "任务 ID、分支、摘要、错误、会话",
        actions: "操作"
      },

      tasks: {
        title: "任务",
        totalCount: "共 {n} 条",
        empty: "暂无任务。",
        files: "文件:",
        tokens: "Token:",
        col: {
          project: "项目",
          mr: "MR",
          status: "状态",
          approve: "结论",
          dispatched: "派发时间",
          started: "开始时间",
          finished: "结束时间",
          elapsed: "耗时(s)",
          task: "任务",
          source: "来源",
          findings: "问题数",
          stats: "统计",
          summary: "摘要/错误"
        }
      },

      detail: {
        title: "任务详情",
        noneSelected: "未选择",
        empty: "从列表中选择一个任务。",
        copySession: "复制 session_id",
        copied: "已复制 session_id",
        copyFailed: "复制失败",
        taskId: "任务 ID",
        sessionId: "会话 ID",
        projectMr: "项目 / MR",
        source: "来源",
        status: "状态",
        approve: "结论",
        created: "创建时间",
        started: "开始时间",
        finished: "结束时间",
        elapsed: "耗时 (s)",
        branches: "分支",
        commit: "提交",
        summary: "摘要",
        error: "错误",
        markdownSummary: "Markdown 摘要",
        warnings: "警告",
        rawResult: "原始 OCR 结果 (JSON)"
      },

      findings: {
        title: "问题列表",
        totalCount: "共 {n} 条",
        severity: "严重程度",
        category: "分类",
        categoryPh: "分类",
        path: "路径",
        pathPh: "文件路径",
        actions: "操作",
        apply: "应用",
        empty: "暂无问题。",
        other: "其他",
        existingCode: "现有代码",
        suggestion: "修改建议"
      },

      projects: {
        title: "项目清单",
        addManual: "手动添加",
        totalCount: "共 {n} 个",
        hint: "审核任务到达时自动登记;也可手动添加。「配置」里可绑定推送通道、Git 令牌与打标签;未绑定推送的项目回退全局 NOTIFY_* 环境变量配置,未绑定令牌的项目回退全局 GITLAB_TOKEN。",
        keyword: "关键词",
        keywordPh: "项目 ID / URL",
        channel: "推送配置",
        channelNone: "未绑定",
        tagAny: "标签(任一命中)",
        clear: "清除",
        actions: "操作",
        config: "配置",
        notBoundChannel: "未绑定(全局配置)",
        notBoundToken: "未绑定(全局 GITLAB_TOKEN)",
        empty: "暂无项目。",
        col: {
          actions: "操作",
          project: "项目",
          url: "项目 URL",
          tags: "标签",
          channel: "推送配置",
          token: "Git 令牌",
          updatedAt: "更新时间"
        }
      },

      channels: {
        title: "推送配置",
        add: "新增推送配置",
        countUnit: "{n} 项",
        hint: "维护可复用的群机器人 webhook(飞书 / 企业微信 / 钉钉),在「项目清单」的配置里绑定到项目。URL 与加签密钥保存后仅展示尾 4 位;编辑时留空表示保持不变。",
        empty: "暂无推送配置。",
        testOk: "✅ 测试消息已发送",
        col: {
          name: "名称",
          type: "类型",
          webhook: "Webhook URL",
          secret: "加签密钥",
          bound: "绑定项目数",
          updatedAt: "更新时间",
          actions: "操作"
        }
      },

      tokens: {
        title: "Git 令牌",
        add: "新增令牌",
        countUnit: "{n} 项",
        hint: "维护可复用的 GitLab 访问令牌(需 read_repository + api scope),在「项目清单」的配置里绑定到项目。绑定后该项目的代码拉取与 MR 评论均使用此令牌,未绑定回退全局 GITLAB_TOKEN。令牌保存后仅展示尾 4 位;编辑时留空表示保持不变。",
        empty: "暂无 Git 令牌。",
        testOk: "✅ 令牌有效({username})",
        col: {
          name: "名称",
          token: "Token",
          bound: "绑定项目数",
          updatedAt: "更新时间",
          actions: "操作"
        }
      },

      tags: {
        title: "项目标签",
        add: "添加标签",
        countUnit: "{n} 个标签",
        hint: "标签在这里提前维护;在「项目清单」的配置里给项目打标签,清单与审核看板均可按标签筛选。",
        projectCount: "{n} 个项目",
        rename: "重命名",
        empty: "暂无标签,点右上角「添加标签」创建。"
      },

      users: {
        title: "Git 用户",
        countUnit: "{n} 人",
        hint: "用户在 MR webhook 到达时自动登记(提交人/处理人,尽力带出邮箱)。「工号」在此维护:保存后飞书推送直接以工号艾特;留空则回退飞书用户映射表(open_id)艾特,再回退 @GitLab用户名 文本。",
        notSet: "未维护",
        empty: "暂无用户,MR webhook 到达后自动登记。",
        col: {
          name: "姓名",
          username: "用户名",
          email: "邮箱",
          employeeNumber: "工号",
          gitlabId: "GitLab ID",
          updatedAt: "更新时间",
          actions: "操作"
        }
      },

      modal: {
        channel: {
          edit: "编辑推送配置",
          create: "新增推送配置",
          namePh: "如:后端一组飞书群",
          secretPh: "机器人安全设置里的加签密钥"
        },
        token: {
          edit: "编辑 Git 令牌",
          create: "新增 Git 令牌",
          namePh: "如:某某项目组 token",
          tokenPh: "glpat-… 或 Personal/Project Access Token"
        },
        tag: {
          rename: "重命名标签",
          create: "添加标签",
          name: "标签名",
          namePh: "如:核心系统 / 支付 / 前端"
        },
        user: {
          title: "维护工号 - {username}",
          label: "工号(留空 = 清空)",
          ph: "如 E00123"
        },
        project: {
          create: "手动添加项目",
          idLabel: "Project ID",
          idPh: "GitLab project id(数字)",
          urlLabel: "项目 URL(可选)",
          bindChannel: "绑定推送配置(可选)",
          bindToken: "绑定 Git 令牌(可选)",
          notNow: "暂不绑定",
          configTitle: "项目配置 - {projectId}",
          channelUnbound: "未绑定(使用全局配置)",
          tokenUnbound: "未绑定(使用全局 GITLAB_TOKEN)",
          tagsMulti: "标签(可多选)",
          tags: "标签",
          noTags: "暂无标签可选,请先在「项目标签」页维护。"
        }
      },

      confirm: {
        noteJoin: "",
        deleteChannel: {
          title: "删除推送配置",
          message: "确定删除「{name}」?",
          bound_one: "删除后 1 个绑定项目将回退全局配置。",
          bound_other: "删除后 {n} 个绑定项目将回退全局配置。"
        },
        deleteToken: {
          title: "删除 Git 令牌",
          message: "确定删除「{name}」?",
          bound_one: "删除后 1 个绑定项目将回退全局 GITLAB_TOKEN。",
          bound_other: "删除后 {n} 个绑定项目将回退全局 GITLAB_TOKEN。"
        },
        deleteTag: {
          title: "删除标签",
          message: "确定删除标签「{name}」?",
          bound_one: "该标签已用于 1 个项目,删除后同步解除绑定。",
          bound_other: "该标签已用于 {n} 个项目,删除后同步解除绑定。"
        }
      },

      val: {
        nameTypeRequired: "名称与类型必填",
        webhookRequired: "Webhook URL 必填",
        nameRequired: "名称必填",
        tokenRequired: "Token 必填",
        tagNameRequired: "标签名必填",
        projectIdRequired: "Project ID 必填"
      },

      err: {
        loadChannels: "加载推送配置失败: {e}",
        loadProjects: "加载项目清单失败: {e}",
        loadTokens: "加载 Git 令牌失败: {e}",
        loadTags: "加载标签失败: {e}",
        loadUsers: "加载 Git 用户失败: {e}",
        loadTasks: "加载任务失败: {e}",
        loadDetail: "加载详情失败: {e}",
        loadFindings: "加载问题失败: {e}",
        save: "保存失败: {e}",
        saveTag: "保存标签失败: {e}",
        saveEmployee: "保存工号失败: {e}",
        add: "添加失败: {e}",
        del: "删除失败: {e}",
        validation: "参数校验失败: {msg}",
        raw: "{msg}",
        http: "HTTP {status}"
      },

      apierr: {
        duplicate_channel_name: "同名推送配置已存在: {name}",
        duplicate_token_name: "同名 Git 令牌已存在: {name}",
        duplicate_tag_name: "同名标签已存在: {name}",
        gitlab_url_missing: "GITLAB_URL 未配置",
        task_not_found: "任务不存在",
        channel_not_found: "推送配置不存在",
        token_not_found: "Git 令牌不存在",
        tag_not_found: "标签不存在",
        user_not_found: "用户不存在",
        project_not_found: "项目不存在"
      },

      note: {
        contentLanguage: "摘要与问题内容由服务端 REVIEW_LANGUAGE 决定,不随界面语言切换。"
      }
    },

    en: {
      doc: { title: "OCR Server Console" },
      lang: { switchTo: "Switch to Chinese" },

      header: {
        title: "OCR Review Console",
        subtitle: "Vue3 portal for requests, analysis results and push config",
        lastRefresh: "Last refresh: "
      },

      nav: {
        review: "Reviews",
        projects: "Projects",
        channels: "Notifications",
        tokens: "Git Tokens",
        tags: "Tags",
        users: "Git Users"
      },

      common: {
        all: "All",
        refresh: "Refresh",
        search: "Search",
        reset: "Reset",
        cancel: "Cancel",
        save: "Save",
        create: "Create",
        add: "Add",
        edit: "Edit",
        delete: "Delete",
        close: "Close",
        confirm: "OK",
        prev: "Prev",
        next: "Next",
        test: "Test",
        page: "Page {cur} / {total}",
        saving: "Saving...",
        processing: "Processing...",
        sending: "Sending...",
        testing: "Testing...",
        name: "Name",
        type: "Type",
        required: "*",
        optional: "(optional)",
        keepBlank: "(leave blank to keep)"
      },

      status: {
        queued: "queued",
        running: "running",
        done: "done",
        failed: "failed",
        cancelled: "cancelled",
        superseded: "superseded"
      },

      severity: {
        critical: "critical",
        high: "high",
        medium: "medium",
        low: "low",
        unknown: "unknown"
      },

      approve: { pass: "pass", reject: "reject" },

      channelType: { feishu: "Feishu", wechat: "WeCom", dingtalk: "DingTalk" },

      dashboard: {
        title: "Dashboard",
        days7: "7 days",
        days14: "14 days",
        days30: "30 days",
        totalTasks: "Total tasks",
        done: "Done",
        failed: "Failed",
        queuedRunning: "Queued + Running",
        approveRate: "Approve Rate",
        totalTokens: "Total Tokens"
      },

      filters: {
        status: "Status",
        source: "Source",
        approve: "Approve",
        projectId: "Project ID",
        projectIdPh: "project id",
        tag: "Tag",
        mrIid: "MR IID",
        mrIidPh: "mr iid",
        keyword: "Keyword",
        keywordPh: "task id, branch, summary, error, session",
        actions: "Actions"
      },

      tasks: {
        title: "Tasks",
        totalCount: "{n} total",
        empty: "No tasks.",
        files: "files:",
        tokens: "tokens:",
        col: {
          project: "Project",
          mr: "MR",
          status: "Status",
          approve: "Approve",
          dispatched: "Dispatched",
          started: "Started",
          finished: "Finished",
          elapsed: "Elapsed(s)",
          task: "Task",
          source: "Source",
          findings: "Findings",
          stats: "Stats",
          summary: "Summary/Error"
        }
      },

      detail: {
        title: "Task Detail",
        noneSelected: "none selected",
        empty: "Select one task from the list.",
        copySession: "Copy session_id",
        copied: "session_id copied",
        copyFailed: "copy failed",
        taskId: "Task ID",
        sessionId: "Session ID",
        projectMr: "Project / MR",
        source: "Source",
        status: "Status",
        approve: "Approve",
        created: "Created",
        started: "Started",
        finished: "Finished",
        elapsed: "Elapsed (s)",
        branches: "Branches",
        commit: "Commit",
        summary: "Summary",
        error: "Error",
        markdownSummary: "Markdown Summary",
        warnings: "Warnings",
        rawResult: "Raw OCR Result (JSON)"
      },

      findings: {
        title: "Findings",
        totalCount: "{n} total",
        severity: "Severity",
        category: "Category",
        categoryPh: "category",
        path: "Path",
        pathPh: "file path",
        actions: "Actions",
        apply: "Apply",
        empty: "No findings.",
        other: "other",
        existingCode: "Existing code",
        suggestion: "Suggestion"
      },

      projects: {
        title: "Projects",
        addManual: "Add manually",
        totalCount: "{n} total",
        hint: "Projects are registered automatically when a review task arrives, and can also be added by hand. Use \"Configure\" to bind a notification channel, a Git token and tags; projects without a channel fall back to the global NOTIFY_* env vars, and projects without a token fall back to the global GITLAB_TOKEN.",
        keyword: "Keyword",
        keywordPh: "project id / url",
        channel: "Notification",
        channelNone: "Unbound",
        tagAny: "Tags (match any)",
        clear: "Clear",
        actions: "Actions",
        config: "Configure",
        notBoundChannel: "Unbound (global config)",
        notBoundToken: "Unbound (global GITLAB_TOKEN)",
        empty: "No projects.",
        col: {
          actions: "Actions",
          project: "Project",
          url: "Project URL",
          tags: "Tags",
          channel: "Notification",
          token: "Git Token",
          updatedAt: "Updated"
        }
      },

      channels: {
        title: "Notifications",
        add: "Add notification config",
        countUnit: "{n} items",
        hint: "Maintain reusable group-bot webhooks (Feishu / WeCom / DingTalk) and bind them to projects from the Projects tab. URL and sign secret are masked to the last 4 characters after saving; leave blank when editing to keep the current value.",
        empty: "No notification configs.",
        testOk: "✅ Test message sent",
        col: {
          name: "Name",
          type: "Type",
          webhook: "Webhook URL",
          secret: "Sign secret",
          bound: "Bound projects",
          updatedAt: "Updated",
          actions: "Actions"
        }
      },

      tokens: {
        title: "Git Tokens",
        add: "Add token",
        countUnit: "{n} items",
        hint: "Maintain reusable GitLab access tokens (need read_repository + api scope) and bind them to projects from the Projects tab. Once bound, the project's code fetch and MR comments both use this token; otherwise it falls back to the global GITLAB_TOKEN. Tokens are masked to the last 4 characters after saving; leave blank when editing to keep the current value.",
        empty: "No Git tokens.",
        testOk: "✅ Token valid ({username})",
        col: {
          name: "Name",
          token: "Token",
          bound: "Bound projects",
          updatedAt: "Updated",
          actions: "Actions"
        }
      },

      tags: {
        title: "Tags",
        add: "Add tag",
        countUnit: "{n} tags",
        hint: "Maintain tags here, then assign them to projects from the Projects tab. Both the project list and the review dashboard can filter by tag.",
        projectCount_one: "1 project",
        projectCount_other: "{n} projects",
        rename: "Rename",
        empty: "No tags yet. Use \"Add tag\" in the top-right to create one."
      },

      users: {
        title: "Git Users",
        countUnit_one: "1 user",
        countUnit_other: "{n} users",
        hint: "Users are registered automatically when an MR webhook arrives (author / assignee, with email on a best-effort basis). Maintain the employee number here: once saved, Feishu notifications mention by employee number; if blank it falls back to the Feishu user map (open_id), then to plain @GitLab-username text.",
        notSet: "Not set",
        empty: "No users yet. They are registered automatically when an MR webhook arrives.",
        col: {
          name: "Name",
          username: "Username",
          email: "Email",
          employeeNumber: "Employee No.",
          gitlabId: "GitLab ID",
          updatedAt: "Updated",
          actions: "Actions"
        }
      },

      modal: {
        channel: {
          edit: "Edit notification config",
          create: "Add notification config",
          namePh: "e.g. Backend team Feishu group",
          secretPh: "Sign secret from the bot's security settings"
        },
        token: {
          edit: "Edit Git token",
          create: "Add Git token",
          namePh: "e.g. Project team token",
          tokenPh: "glpat-… or Personal/Project Access Token"
        },
        tag: {
          rename: "Rename tag",
          create: "Add tag",
          name: "Tag name",
          namePh: "e.g. Core / Payments / Frontend"
        },
        user: {
          title: "Employee No. - {username}",
          label: "Employee No. (blank = clear)",
          ph: "e.g. E00123"
        },
        project: {
          create: "Add project manually",
          idLabel: "Project ID",
          idPh: "GitLab project id (numeric)",
          urlLabel: "Project URL (optional)",
          bindChannel: "Bind notification config (optional)",
          bindToken: "Bind Git token (optional)",
          notNow: "Not now",
          configTitle: "Project config - {projectId}",
          channelUnbound: "Unbound (use global config)",
          tokenUnbound: "Unbound (use global GITLAB_TOKEN)",
          tagsMulti: "Tags (multi-select)",
          tags: "Tags",
          noTags: "No tags available. Add some on the Tags tab first."
        }
      },

      confirm: {
        noteJoin: " ",
        deleteChannel: {
          title: "Delete notification config",
          message: "Delete \"{name}\"?",
          bound_one: "1 bound project will fall back to the global config.",
          bound_other: "{n} bound projects will fall back to the global config."
        },
        deleteToken: {
          title: "Delete Git token",
          message: "Delete \"{name}\"?",
          bound_one: "1 bound project will fall back to the global GITLAB_TOKEN.",
          bound_other: "{n} bound projects will fall back to the global GITLAB_TOKEN."
        },
        deleteTag: {
          title: "Delete tag",
          message: "Delete tag \"{name}\"?",
          bound_one: "This tag is used by 1 project, which will be unbound.",
          bound_other: "This tag is used by {n} projects, which will be unbound."
        }
      },

      val: {
        nameTypeRequired: "Name and type are required",
        webhookRequired: "Webhook URL is required",
        nameRequired: "Name is required",
        tokenRequired: "Token is required",
        tagNameRequired: "Tag name is required",
        projectIdRequired: "Project ID is required"
      },

      err: {
        loadChannels: "Failed to load notification configs: {e}",
        loadProjects: "Failed to load projects: {e}",
        loadTokens: "Failed to load Git tokens: {e}",
        loadTags: "Failed to load tags: {e}",
        loadUsers: "Failed to load Git users: {e}",
        loadTasks: "Failed to load tasks: {e}",
        loadDetail: "Failed to load detail: {e}",
        loadFindings: "Failed to load findings: {e}",
        save: "Save failed: {e}",
        saveTag: "Failed to save tag: {e}",
        saveEmployee: "Failed to save employee number: {e}",
        add: "Failed to add: {e}",
        del: "Delete failed: {e}",
        validation: "Validation failed: {msg}",
        raw: "{msg}",
        http: "HTTP {status}"
      },

      apierr: {
        duplicate_channel_name: "Notification config \"{name}\" already exists",
        duplicate_token_name: "Git token \"{name}\" already exists",
        duplicate_tag_name: "Tag \"{name}\" already exists",
        gitlab_url_missing: "GITLAB_URL is not configured",
        task_not_found: "Task not found",
        channel_not_found: "Notification config not found",
        token_not_found: "Git token not found",
        tag_not_found: "Tag not found",
        user_not_found: "User not found",
        project_not_found: "Project not found"
      },

      note: {
        contentLanguage: "Summary and finding content follow the server's REVIEW_LANGUAGE and do not switch with the UI language."
      }
    }
  };

  function lookup(dict, key) {
    var parts = String(key).split(".");
    var cur = dict;
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== "object") return undefined;
      cur = cur[parts[i]];
    }
    return typeof cur === "string" ? cur : undefined;
  }

  function interpolate(s, params) {
    if (!params) return s;
    return s.replace(/\{(\w+)\}/g, function (m, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m;
    });
  }

  // 按当前语言的复数规则选键:优先 <key>_one / <key>_other,缺失则回落到 <key>。
  function pluralKey(lang, key, n) {
    var suffix = n === 1 ? "_one" : "_other";
    if (lookup(MESSAGES[lang], key + suffix) !== undefined) return key + suffix;
    if (lang === "zh-CN") return key;  // 中文无复数区分
    var other = lookup(MESSAGES[lang], key + "_other");
    return other !== undefined ? key + "_other" : key;
  }

  function translate(lang, key, params, count) {
    var l = LANGS.indexOf(lang) >= 0 ? lang : "zh-CN";
    var k = count == null ? key : pluralKey(l, key, count);
    var s = lookup(MESSAGES[l], k);
    if (s === undefined) {
      var other = l === "zh-CN" ? "en" : "zh-CN";
      s = lookup(MESSAGES[other], k);
      if (s === undefined) {
        if (window.console && console.warn) console.warn("[i18n] missing key:", key);
        return key;
      }
    }
    return interpolate(s, params);
  }

  function normalize(raw) {
    var s = String(raw || "").toLowerCase();
    return s === "zh" || s.indexOf("zh-") === 0 || s.indexOf("zh_") === 0 ? "zh-CN" : "en";
  }

  // 只看浏览器语言,不看 URL / localStorage。用于判断 lang 是否属于「非默认值」,
  // 决定要不要把它写进 URL。
  function detectBrowserLang() {
    return normalize(window.navigator && (navigator.language || navigator.userLanguage));
  }

  // URL ?lang= → localStorage → 浏览器语言 → zh-CN
  function detectLang() {
    try {
      var fromUrl = new URLSearchParams(window.location.search).get("lang");
      if (fromUrl === "zh" || fromUrl === "zh-CN" || fromUrl === "en") return fromUrl === "zh" ? "zh-CN" : fromUrl;
      var stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored === "zh-CN" || stored === "en") return stored;
    } catch (_) {
      // 隐私模式 / file:// 下 localStorage 可能抛错,忽略并继续回落
    }
    return detectBrowserLang();
  }

  window.OcrI18n = {
    STORAGE_KEY: STORAGE_KEY,
    LANGS: LANGS,
    TERMINAL_STATUSES: TERMINAL_STATUSES,
    STATUS_VALUES: STATUS_VALUES,
    SEVERITY_VALUES: SEVERITY_VALUES,
    MESSAGES: MESSAGES,
    translate: translate,
    detectLang: detectLang,
    detectBrowserLang: detectBrowserLang
  };
})();
