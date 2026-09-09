(() => {
  const clock = document.getElementById("ops-clock");
  const formatClock = () => {
    if (!clock) return;
    clock.textContent = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Singapore",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
  };
  formatClock();
  window.setInterval(formatClock, 1000);

  const seconds = Number(document.body.dataset.refreshSeconds || 0);
  if (!Number.isFinite(seconds) || seconds < 5 || window.location.pathname !== "/") {
    return;
  }
  const statusLabels = {
    SCHEDULED: "等待运行",
    RUNNING: "运行中",
    SUCCESS: "正常",
    DEGRADED: "部分异常",
    SKIPPED: "正常跳过",
    BLOCKED: "被阻断",
    FAILED: "失败",
    MISSED: "未按时运行",
    STALE: "已过期",
    DISABLED: "计划关闭",
    UNKNOWN: "未知",
  };
  const formatTime = (value) => {
    if (!value) return "-";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Singapore",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(value));
  };
  const refresh = async () => {
    try {
      const response = await fetch("/api/overview", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      const warning = document.querySelector("[data-snapshot-warning]");
      if (warning) {
        warning.hidden = payload.snapshot_freshness.status !== "STALE";
        warning.querySelector("[data-snapshot-warning-text]").textContent = payload.snapshot_freshness.reason;
      }
      const snapshot = document.querySelector("[data-snapshot-at]");
      if (snapshot) snapshot.textContent = formatTime(payload.snapshot_at);
      const incidents = document.querySelector("[data-summary='open_incidents']");
      if (incidents) incidents.textContent = payload.summary.open_incidents;
      const jobsTotal = document.querySelector("[data-summary='jobs_total']");
      if (jobsTotal) jobsTotal.textContent = payload.summary.jobs_total;
      const success = document.querySelector("[data-status-count='SUCCESS']");
      if (success) success.textContent = payload.summary.status_counts.SUCCESS || 0;
      const running = document.querySelector("[data-status-count='RUNNING']");
      if (running) running.textContent = payload.summary.status_counts.RUNNING || 0;
      const attention = document.querySelector("[data-summary='attention']");
      if (attention) {
        attention.textContent =
          (payload.summary.status_counts.DEGRADED || 0) +
          (payload.summary.status_counts.BLOCKED || 0) +
          (payload.summary.status_counts.STALE || 0);
      }
      const critical = document.querySelector("[data-summary='critical']");
      if (critical) {
        critical.textContent =
          (payload.summary.status_counts.FAILED || 0) +
          (payload.summary.status_counts.MISSED || 0);
      }
      payload.jobs.forEach((job) => {
        const row = document.querySelector(`[data-job-row="${CSS.escape(job.job_id)}"]`);
        if (!row) return;
        const status = row.querySelector("[data-job-status]");
        if (status) {
          status.textContent = statusLabels[job.status] || job.status;
          status.className = `status status-${String(job.status || "UNKNOWN").toLowerCase()}`;
        }
        const values = {
          "[data-job-session]": job.target_session || "-",
          "[data-job-stage]": job.stage || "-",
          "[data-job-success]": formatTime(job.last_success_at),
          "[data-job-reason]": job.status_reason || "-",
        };
        Object.entries(values).forEach(([selector, value]) => {
          const cell = row.querySelector(selector);
          if (cell) cell.textContent = value;
        });
      });
    } catch (_) {
      // A stale but coherent snapshot is preferable to replacing the page with noise.
    }
  };
  window.setInterval(refresh, seconds * 1000);
})();
