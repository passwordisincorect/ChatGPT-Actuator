from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import RLock
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

from .account_bootstrap import AccountBootstrap, TRANSFER_MARKER
from .browser_accounts import BrowserAccountManager
from .orchestrator_control import OrchestratorControl
from .context_transfer import ContextTransferManager
from .funnel_control import FunnelControl
from .archive_web_import import ArchiveImportManager, ArchiveImportBusy, ArchiveImportError


HOST = "127.0.0.1"
PORT = int(os.environ.get("CHATGPT_ACTUATOR_CONTROL_PORT", "8764"))
from .config import project_root
PROJECT_ROOT = project_root()
TUNNEL_MANAGER = PROJECT_ROOT / "scripts" / "tunnel-manager.ps1"
TUNNEL_PROFILE = os.environ.get("CHATGPT_ACTUATOR_TUNNEL_PROFILE", "chatgpt-actuator")
ADMIN_URL = "http://127.0.0.1:8765"
ADMIN_HEALTH = ADMIN_URL + "/healthz"
DOM_BROKER_URL = ADMIN_URL + "/api/dom"
DOM_BROKER_TOKEN_FILE = PROJECT_ROOT.parent / ".chatgpt-dom-broker-token"
MCP_HEALTH = os.environ.get("CHATGPT_ACTUATOR_MCP_HEALTH", "http://127.0.0.1:8081/healthz")
TOKEN = secrets.token_urlsafe(24)
STARTED_AT = time.time()
ORCHESTRATOR = OrchestratorControl(PROJECT_ROOT)
ACCOUNT_BOOTSTRAP = AccountBootstrap(PROJECT_ROOT)
BROWSER_ACCOUNTS = BrowserAccountManager(PROJECT_ROOT)
FUNNEL = FunnelControl(PROJECT_ROOT)
HISTORY_IMPORT = ArchiveImportManager(PROJECT_ROOT.parent / "ChatGPT-Migration-Bridge")
CONTEXT_TRANSFER = ContextTransferManager(PROJECT_ROOT.parent / "ChatGPT-Migration-Bridge")


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT-Actuator Control Center</title>
<link rel="icon" href="data:,">
<style>
:root{
  font-family:"Segoe UI Variable","Segoe UI",Arial,sans-serif;
  color-scheme:dark;
  --bg:#0a0f18;
  --header:#0d1421;
  --panel:#111927;
  --panel-2:#162235;
  --panel-3:#0d1522;
  --line:#26364d;
  --line-soft:#1e2a3c;
  --text:#edf3f8;
  --muted:#91a0b4;
  --accent:#4f8cff;
  --accent-hover:#6a9cff;
  --success:#55d889;
  --warning:#f1bd63;
  --danger:#ef6666;
  --danger-bg:#4d2027;
  --radius:14px;
  --radius-sm:10px;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.45;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit}
.shell{width:min(1240px,100%);margin:0 auto}
header{background:var(--header);border-bottom:1px solid var(--line);position:relative;z-index:2}
.headrow{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px 20px}
.brand h1{margin:0;font-size:20px;line-height:1.2;letter-spacing:-.015em}
.sub{margin-top:5px;color:var(--muted);font-size:12.5px;max-width:760px}
.lang-switch{display:inline-flex;gap:4px;padding:4px;background:var(--panel-3);border:1px solid var(--line);border-radius:10px}
button.lang-btn{min-width:38px;padding:6px 9px;background:transparent;color:var(--muted);border:0;border-radius:7px}
button.lang-btn.active{background:var(--accent);color:#fff}
main{padding:18px 20px 26px}
#banner{
  min-height:42px;
  display:flex;
  align-items:center;
  padding:10px 13px;
  margin-bottom:14px;
  border:1px solid var(--line);
  border-radius:11px;
  background:var(--panel-3);
  color:#cbd6e3;
  font-size:13px;
  white-space:pre-wrap;
}
.layout-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px;align-items:start}
.span-12{grid-column:span 12}.span-7{grid-column:span 7}.span-5{grid-column:span 5}
.card{
  min-width:0;
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--radius);
  padding:16px;
}
.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:13px}
.section-head h2{font-size:15.5px;line-height:1.3;margin:0;letter-spacing:-.01em}
.section-copy{margin:4px 0 0;color:var(--muted);font-size:12px;max-width:680px}
.service-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.service-panel{
  min-width:0;
  padding:14px;
  border:1px solid var(--line-soft);
  background:var(--panel-2);
  border-radius:var(--radius-sm);
}
.service-panel h3{font-size:14px;margin:0 0 9px}
.row{
  display:grid;
  grid-template-columns:minmax(0,1fr) auto;
  align-items:start;
  gap:14px;
  padding:6px 0;
  border-bottom:1px solid rgba(145,160,180,.10);
  font-size:13px;
}
.row:last-of-type{border-bottom:0}
.label{color:var(--muted);min-width:0}
.row strong{
  color:var(--text);
  text-align:right;
  min-width:0;
  max-width:280px;
  overflow-wrap:anywhere;
  font-weight:650;
}
.ok{color:var(--success)!important}.bad{color:var(--danger)!important}.warn{color:var(--warning)!important}
.dot{font-size:10px;margin-right:5px}
.actions{display:flex;align-items:stretch;gap:8px;flex-wrap:wrap;margin-top:13px}
button,a.button{
  border:1px solid transparent;
  border-radius:9px;
  min-height:36px;
  padding:8px 11px;
  font-weight:650;
  font-size:12.5px;
  line-height:1.2;
  cursor:pointer;
  text-decoration:none;
  color:#fff;
  background:var(--accent);
  transition:background-color .12s ease,border-color .12s ease,color .12s ease;
}
button:hover,a.button:hover{background:var(--accent-hover)}
button.secondary,a.secondary{background:#34445b;border-color:#42556f;color:#eef4fb}
button.secondary:hover,a.secondary:hover{background:#40536e}
button.danger{background:var(--danger-bg);border-color:#7d303a;color:#ffd9dc}
button.danger:hover{background:#642a33}
button:disabled{opacity:.5;cursor:wait}
button:focus-visible,a.button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{
  outline:2px solid #8bb3ff;
  outline-offset:2px;
}
select,input[type=text]{
  width:100%;
  min-width:0;
  min-height:38px;
  padding:8px 10px;
  border:1px solid #42516a;
  border-radius:9px;
  background:var(--panel-3);
  color:var(--text);
}
.account-create{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:10px}
.account-mode{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:11px}
.account-mode label{display:flex;gap:7px;align-items:center;padding:9px 12px;background:var(--panel-3);border:1px solid var(--line);border-radius:9px;font-size:12px;cursor:pointer}
.account-mode input{accent-color:var(--accent)}
.account-identity-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:10px}
.account-identity{min-width:0;padding:12px;border-radius:10px;background:var(--panel-2);border:1px solid var(--line-soft)}
.account-identity .label{display:block;margin-bottom:8px}
.account-identity strong{display:block;font-size:12px;overflow-wrap:anywhere}
.account-check{padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel-3);margin:12px 0}
.account-check-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.account-check-head strong{font-size:13px}
.account-check-time,.account-check-note{font-size:11px;color:var(--muted);margin-top:8px}
.account-check-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:10px;margin-top:8px}
.account-check-entry{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;padding:6px 0;border-top:1px solid var(--line-soft);font-size:11.5px}
.account-check-entry span:last-child{text-align:right}
@media(max-width:550px){.account-identity-grid,.account-check-list{grid-template-columns:1fr}}

.small{font-size:11.5px;color:var(--muted);line-height:1.45}
.mono-path{
  margin-top:10px;
  color:#7f91aa;
  font:11px/1.45 Consolas,"Cascadia Mono",monospace;
  overflow-wrap:anywhere;
}
.notice{
  margin-top:10px;
  padding:9px 10px;
  border-left:3px solid #7a5e2e;
  border-radius:6px;
  background:#17191d;
  color:#d6b36f;
  font-size:11.5px;
  line-height:1.45;
}
.notice.info{border-left-color:#375f9a;background:#111a28;color:#9fb7d6}
.diagnostics-row{display:flex;align-items:center;justify-content:space-between;gap:18px}
.diagnostics-row .section-head{margin:0;min-width:240px}
.diagnostics-row .actions{margin:0;justify-content:flex-end}
.log-panel{
  grid-column:span 12;
  border:1px solid var(--line);
  border-radius:var(--radius);
  background:var(--panel);
  overflow:hidden;
}
.log-panel summary{
  cursor:pointer;
  list-style:none;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:18px;
  min-height:48px;
  padding:13px 16px;
  color:var(--text);
  font-size:13.5px;
  font-weight:650;
}
.log-panel summary::-webkit-details-marker{display:none}
.log-panel summary::after{content:"+";color:var(--muted);font-size:19px;font-weight:400}
.log-panel[open] summary::after{content:"–"}
.summary-hint{margin-left:auto;color:var(--muted);font-size:11.5px;font-weight:400}
pre{
  margin:0 12px 12px;
  max-height:320px;
  overflow:auto;
  padding:12px;
  border:1px solid var(--line-soft);
  border-radius:10px;
  background:#060a11;
  color:#cdd7e5;
  white-space:pre-wrap;
  word-break:break-word;
  font:11.5px/1.5 Consolas,"Cascadia Mono",monospace;
}

#banner{
  position:sticky;
  top:8px;
  z-index:30;
  box-shadow:0 8px 24px rgba(0,0,0,.22);
}
.quick-nav{
  display:flex;
  gap:7px;
  overflow-x:auto;
  scrollbar-width:none;
  margin:-3px 0 14px;
  padding:3px 1px 1px;
}
.quick-nav::-webkit-scrollbar{display:none}
.quick-nav a{
  flex:0 0 auto;
  border:1px solid var(--line);
  background:var(--panel-3);
  color:#c9d4e2;
  text-decoration:none;
  font-size:12px;
  font-weight:650;
  padding:7px 10px;
  border-radius:999px;
}
.quick-nav a:hover{border-color:#486387;background:#142033;color:#fff}
.quick-nav a:focus-visible{outline:2px solid #8bb3ff;outline-offset:2px}
.card,.log-panel{scroll-margin-top:66px}
.compact-details{
  margin-top:10px;
  border-top:1px solid rgba(145,160,180,.12);
  padding-top:8px;
}
.compact-details summary{
  cursor:pointer;
  color:#aebbd0;
  font-size:11.5px;
  font-weight:650;
  list-style:none;
  display:flex;
  align-items:center;
  gap:6px;
  min-height:28px;
}
.compact-details summary::-webkit-details-marker{display:none}
.compact-details summary::before{content:"+";font-size:15px;color:#7f91aa;font-weight:400}
.compact-details[open] summary::before{content:"–"}
.compact-details .row{font-size:12px}
.next-step{
  display:grid;
  grid-template-columns:auto minmax(0,1fr);
  gap:9px 12px;
  align-items:start;
  margin:11px 0 2px;
  padding:10px 11px;
  border:1px solid #2e4562;
  border-radius:9px;
  background:#101b2a;
  font-size:12px;
}
.next-step span{color:#8fa4bf}
.next-step strong{color:#dce8f6;font-weight:650}
.workflow-steps{
  margin:11px 0 4px;
  padding-left:24px;
  color:#cbd6e3;
  font-size:12px;
}
.workflow-steps li{padding:3px 0 3px 3px}
.workflow-steps li::marker{color:#7fa9ef;font-weight:700}
.primary-wide{width:100%;margin-top:10px;min-height:40px}
.fallback-details .actions{margin-top:8px}
.danger-zone{
  margin-top:10px;
  padding:9px 10px;
  border:1px solid #53303a;
  background:#171117;
  border-radius:9px;
}
.danger-zone summary{color:#dba4ab}
.danger-zone .actions{margin-top:8px}
@media (max-width:720px){
  #banner{top:6px}
  .quick-nav{margin-bottom:11px}
  .quick-nav a{padding:7px 9px;font-size:11.5px}
  .next-step{grid-template-columns:1fr;gap:3px}
  .workflow-steps{font-size:11.5px}
}

@media (max-width:980px){
  .span-7,.span-5{grid-column:span 12}
  .service-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .service-panel:last-child{grid-column:1/-1}
}
@media (max-width:720px){
  .headrow{align-items:flex-start;padding:15px 14px}
  .brand h1{font-size:18px;max-width:250px}
  .sub{font-size:11.5px;line-height:1.4}
  main{padding:12px 12px 20px}
  #banner{margin-bottom:11px}
  .layout-grid{gap:11px}
  .card{padding:13px;border-radius:12px}
  .section-head{margin-bottom:8px}
  .section-copy{display:none}
  .service-grid{grid-template-columns:1fr;gap:9px}
  .service-panel:last-child{grid-column:auto}
  .service-panel{padding:11px}
  .row{grid-template-columns:minmax(0,56%) minmax(0,44%);font-size:12.5px;gap:8px;padding:4.5px 0}
  .row strong{max-width:none;overflow-wrap:anywhere;word-break:normal}
  .actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:7px;margin-top:11px}
  .actions button,.actions a.button{width:100%;display:flex;align-items:center;justify-content:center;text-align:center}
  .account-create{grid-template-columns:minmax(0,1fr) auto}
  .diagnostics-row{display:block}
  .diagnostics-row .section-head{margin-bottom:8px}
  .diagnostics-row .actions{justify-content:stretch}
  .summary-hint{display:none}
}
@media (max-width:430px){
  .lang-switch{flex:0 0 auto}
  .account-create{grid-template-columns:1fr}
  .account-create button{width:100%}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
}

[hidden]{display:none!important}
.quick-nav{position:sticky;top:60px;z-index:20;background:var(--bg);padding:8px 0;margin:0 0 16px;gap:8px}
.quick-nav a{border-radius:10px;padding:11px 18px;font-size:14px}
.quick-nav a[aria-current="page"]{background:var(--accent);border-color:var(--accent);color:white}
.account-shared{display:inline-block;margin:0 0 18px;padding:8px 12px;border:1px solid #2c5145;border-radius:8px;color:var(--success);font-size:13px}
.account-workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:24px}
.account-workspace h3{margin:0 0 16px;font-size:15px}
.account-verification{border-left:1px solid var(--line);padding-left:24px}
.field-label{display:block;font-size:13px;color:var(--muted);margin-bottom:8px}
.account-identity-grid{grid-template-columns:1fr}
.account-identity strong{font-size:14px}
.card{padding:22px}
.section-head h2{font-size:19px}
.section-copy{font-size:13px}
.service-panel{padding:18px}
.service-panel h3{font-size:16px}
button:disabled{cursor:not-allowed}
@media(max-width:720px){
  .account-workspace{grid-template-columns:1fr;gap:20px}
  .account-verification{border-left:0;border-top:1px solid var(--line);padding:20px 0 0}
  .quick-nav a{padding:10px 13px;font-size:13px}
  .card{padding:16px}
  .section-copy{display:block}
}

.system-summary{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px 20px;border:1px solid var(--line);border-radius:12px;background:var(--panel);margin-bottom:12px}
.system-summary strong{font-size:18px}.system-summary p{margin:5px 0;color:var(--muted);font-size:13px}
.system-issues{display:grid;gap:8px;margin-bottom:14px}
.system-issue{display:flex;justify-content:space-between;align-items:center;gap:16px;border:1px solid #594a30;background:#191820;border-radius:10px;padding:12px 16px}
.system-issue strong{font-size:13px}.system-issue p{margin:4px 0 0;color:var(--muted);font-size:12px}
#banner{position:relative;top:auto;gap:14px;justify-content:space-between;box-shadow:none}
#banner-message{overflow-wrap:anywhere}#banner button{flex-shrink:0}
.quick-nav{top:0}.busy-button::before{content:"";display:inline-block;width:12px;height:12px;border:2px solid #ffffff70;border-top-color:white;border-radius:50%;margin-right:8px;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:720px){.system-summary,.system-issue{align-items:stretch;flex-direction:column;gap:10px}.system-summary button,.system-issue button{width:100%}}

.history-panel{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}
.history-panel h3{margin:0 0 8px;font-size:17px}
.history-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0}
.history-controls input[type=file]{max-width:100%;font-size:13px;flex:1;min-width:230px}
.history-controls button{min-width:155px}
.history-panel progress{width:100%;height:14px;margin:6px 0}
.history-result{margin:8px 0;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}
#history-state{font-size:13px;font-weight:600}
.embedded header{display:none!important}
.embedded main{padding-top:8px}
</style>
</head>
<body><div style="padding:12px 24px"><a href="/agents" style="color:#8cc6ff">Nhiệm vụ tự động</a></div>
<header>
<div class="headrow shell">
  <div class="brand">
    <h1 data-i18n="title">ChatGPT-Actuator Control Center</h1>
    <div class="sub" data-i18n="subtitle">Independent local sidecar · loopback only · stays alive while Tunnel/Actuator restarts</div>
  </div>
  <div class="lang-switch" aria-label="Language">
    <button id="lang-vi" class="lang-btn" onclick="setLanguage('vi')" aria-label="Tiếng Việt">VI</button>
    <button id="lang-en" class="lang-btn" onclick="setLanguage('en')" aria-label="English">EN</button>
  </div>
</div>
</header>
<main class="shell">
<div id="system-summary" class="system-summary" aria-live="polite">
  <div><strong id="system-summary-title">Đang tải trạng thái...</strong><p id="system-summary-note"></p><span id="last-update" class="small"></span></div>
  <button id="system-check-button" onclick="checkAllAccounts(this)" data-i18n="checkAllAccounts">Kiểm tra hệ thống</button>
</div>
<div id="system-issues" class="system-issues"></div>
<div id="banner" role="status" aria-live="polite" hidden><span id="banner-message"></span><button class="secondary" onclick="dismissNotice()" data-i18n="dismissNotice">Đóng</button></div>
<nav class="quick-nav" aria-label="Đi tới khu vực">
  <a href="#services" data-i18n="navServices">Hệ thống</a>
  <a href="#accounts" data-i18n="navAccounts">Tài khoản</a>
  <a href="#context-transfer" data-i18n="navContext">Chuyển ngữ cảnh</a>
  <a href="#funnel">Tailscale Funnel</a>
  <a href="#diagnostics" data-i18n="navDiagnostics">Chẩn đoán</a>
</nav>
<div class="layout-grid">
  <section id="services" class="card span-12">
    <div class="section-head">
      <div>
        <h2 data-i18n="serviceStack">Hệ thống dịch vụ</h2>
        <p class="section-copy" data-i18n="serviceStackHint">Trạng thái và điều khiển các tiến trình cục bộ cốt lõi.</p>
      </div>
    </div>
    <div class="service-grid">
      <article class="service-panel">
        <h3 data-i18n="connectivity">Connectivity</h3>
        <div class="row"><span class="label" data-i18n="tunnelProcess">Tunnel process</span><strong id="tunnel">...</strong></div>
        <div class="row"><span class="label" data-i18n="mcpHealth">MCP health</span><strong id="mcp">...</strong></div>
        <div class="row"><span class="label" data-i18n="adminHealth">Admin health</span><strong id="admin">...</strong></div>
        <div class="actions">
          <button id="tunnel-start-btn" data-i18n="start" onclick="action('/api/tunnel/start','startTunnel',this)">Start</button>
          <button id="tunnel-stop-btn" class="secondary" data-i18n="stop" onclick="action('/api/tunnel/stop','stopTunnel',this)">Stop</button>
        </div>
        <details class="compact-details">
          <summary data-i18n="technicalDetails">Chi tiết kỹ thuật</summary>
          <div class="row"><span class="label" data-i18n="tunnelPids">Tunnel PID(s)</span><strong id="pids">-</strong></div>
          <div class="row"><span class="label" data-i18n="profile">Profile</span><strong id="profile">...</strong></div>
          <div class="actions">
            <button class="danger" data-i18n="restartTunnel" onclick="action('/api/tunnel/restart','restartTunnel',this)">Restart tunnel</button>
          </div>
        </details>
      </article>

      <article class="service-panel">
        <h3>Actuator</h3>
        <div class="row"><span class="label" data-i18n="controlCenter">Control Center</span><strong id="control-running" class="ok">● Running</strong></div>
        <div class="actions">

          <button class="danger" data-i18n="restartActuator" onclick="action('/api/actuator/restart','restartActuator',this)">Restart Actuator</button>
        </div>
        <details class="compact-details">
          <summary data-i18n="technicalDetails">Chi tiết kỹ thuật</summary>
          <div class="row"><span class="label" data-i18n="version">Version</span><strong id="version">...</strong></div>
          <div class="row"><span class="label" data-i18n="controlUptime">Control uptime</span><strong id="uptime">...</strong></div>
          <div class="notice" data-i18n="restartActuatorNote">Restart Actuator restarts the tunnel process tree because the MCP Actuator is a child of tunnel-client.</div>
        </details>
      </article>

      <article class="service-panel">
        <h3>Orchestrator</h3>
        <div class="row"><span class="label" data-i18n="process">Process</span><strong id="orch-process">...</strong></div>
        <div class="row"><span class="label" data-i18n="health">Health</span><strong id="orch-health">...</strong></div>
        <div class="row"><span class="label" data-i18n="activeWorkers">Active workers</span><strong id="orch-workers">0</strong></div>
        <div class="row"><span class="label" data-i18n="activeJobs">Active jobs</span><strong id="orch-jobs">0</strong></div>
        <div class="actions">
          <button id="orch-start-btn" data-i18n="start" onclick="action('/api/orchestrator/start','startOrchestrator',this)">Start</button>
          <button id="orch-stop-btn" class="secondary" data-i18n="stop" onclick="action('/api/orchestrator/stop','stopOrchestrator',this)">Stop</button>
        </div>
        <details class="compact-details">
          <summary data-i18n="technicalDetails">Chi tiết kỹ thuật</summary>
          <div class="row"><span class="label" data-i18n="version">Version</span><strong id="orch-version">...</strong></div>
          <div class="row"><span class="label" data-i18n="uptime">Uptime</span><strong id="orch-uptime">...</strong></div>
          <div class="row"><span class="label" data-i18n="backend">Backend</span><strong id="orch-backend">...</strong></div>
          <div class="row"><span class="label" data-i18n="activeTransport">Active transport</span><strong id="orch-transport">...</strong></div>
          <div class="actions">
            <button class="danger" data-i18n="restartOrchestrator" onclick="action('/api/orchestrator/restart','restartOrchestrator',this)">Restart Orchestrator</button>
          </div>
        </details>
      </article>
    </div>

    <div class="history-panel" id="history-import">
      <h3 data-i18n="historyTitle">Nhập lịch sử ChatGPT</h3>
      <p class="section-copy" data-i18n="historyHint">Chọn ZIP Export từ ChatGPT. Hệ thống sẽ sao lưu và nhập bổ sung vào kho tìm kiếm trên máy.</p>
      <div class="history-controls">
        <input id="history-zip-file" type="file" accept=".zip,application/zip" onchange="archiveFileSelected()" aria-label="ChatGPT Export ZIP">
        <button id="history-upload-button" type="button" onclick="uploadHistory(this)" data-i18n="historyUpload">Tải ZIP và nhập</button>
      </div>
      <progress id="history-progress" value="0" max="100" hidden></progress>
      <div id="history-state" role="status" aria-live="polite">Chưa chọn file</div>
      <div id="history-result" class="history-result"></div>
      <p class="small" data-i18n="historyLocalNotice">Dữ liệu lưu cục bộ trên máy; không khôi phục lịch sử hay Memory chính thức của tài khoản ChatGPT.</p>
    </div>
  </section>

  <section id="context-transfer" class="card span-12">
    <div class="section-head"><div>
      <h2 data-i18n="contextTitle">Chuyển ngữ cảnh ChatGPT</h2>
      <p class="section-copy" data-i18n="contextHint">Tạo hồ sơ bằng ChatGPT đang đăng nhập (Main = Worker), không sử dụng share link hoặc OpenAI Export Data.</p>
    </div></div>
    <p class="small" data-i18n="contextWarning">Tác vụ sẽ gửi prompt vào hội thoại mới sau khi bạn xác nhận. Hồ sơ AI tạo có thể thiếu dữ liệu; luôn kiểm tra trước khi chuyển.</p>
    <div class="actions">
      <button id="context-start" onclick="contextTransferStart(this)" data-i18n="contextStart">Tạo hồ sơ chuyển tài khoản</button>
      <button id="context-download-md" class="secondary" onclick="contextTransferDownload('md')" disabled data-i18n="contextDownloadMd">Tải Markdown</button>
      <button id="context-download-json" class="secondary" onclick="contextTransferDownload('json')" disabled data-i18n="contextDownloadJson">Tải JSON</button>
    </div>
    <div id="context-status" class="history-result" role="status" aria-live="polite">Chưa chạy</div>
    <p class="small" data-i18n="contextStorage">File được giữ riêng tư tại ChatGPT-Migration-Bridge/private/context-transfer; chưa được mã hóa độc lập.</p>
  </section>

  <section id="accounts" class="card span-12">
    <div class="section-head">
      <div>
        <h2 data-i18n="systemChatGPTAccount">System ChatGPT Account</h2>
        <p class="section-copy" data-i18n="accountOpsHint">Quản lý profile Worker và đồng bộ tài khoản Main/Worker.</p>
      </div>
    </div>
    <p class="small" data-i18n="accountRoles"></p>
    <p class="account-shared" data-i18n="modeSame">Main và Worker dùng chung tài khoản</p>
    <div class="account-workspace">
      <div class="account-setup">
        <h3 data-i18n="accountSetup">1. Chọn và đăng nhập</h3>
    <label class="field-label" for="browser-account-select" data-i18n="chooseProfile">Chọn profile tài khoản</label>
    <div><select id="browser-account-select" aria-label="ChatGPT account"></select></div>
    <div class="actions">
      <button class="secondary" data-i18n="switchSystemAccount" onclick="switchBrowserAccount(this)">Change system account</button>
      <button class="secondary" data-i18n="openWorkerLogin" onclick="openBrowserAccountLogin(this)">Open / sign in Worker</button>
    </div>
    <div class="actions"><button id="sync-worker-btn" class="secondary" data-i18n="syncWorkerWithMain" onclick="syncWorkerWithMain(this)">Sync Worker with Main</button></div>
    <details id="account-advanced" class="compact-details">
      <summary data-i18n="advancedAccount">Đồng bộ nâng cao</summary>
      <div class="actions">
        <button class="secondary" data-i18n="confirmMainAccount" onclick="confirmMainAccount(this)">Confirm Main = this account</button>
      </div>
    </details>
    <details class="compact-details">
      <summary data-i18n="profileManagement">Quản lý profile</summary>
      <div class="account-create">
        <input id="browser-account-new" type="text" maxlength="60" autocomplete="off" placeholder="e.g. Account 2">
        <button data-i18n="addSystemAccount" onclick="createBrowserAccount(this)">Add account</button>
      </div>
      <div class="mono-path" id="browser-account-path">...</div>
    </details>
    <div class="next-step">
      <span data-i18n="nextStep">Bước tiếp theo</span>
      <strong id="account-next-step">...</strong>
    </div>
      </div>
      <div class="account-verification">
        <h3 data-i18n="accountVerify">2. Kiểm tra sẵn sàng</h3>
    <div class="account-identity-grid">
      <div class="account-identity"><span class="label" data-i18n="mainLogin">Tài khoản ChatGPT Main</span><strong id="browser-account-main-login">...</strong></div>
      <div class="account-identity"><span class="label" data-i18n="workerLogin">Tài khoản ChatGPT Worker</span><strong id="browser-account-worker-login">...</strong></div>
    </div>
    <div class="row"><span class="label" data-i18n="systemAccount">Profile hệ thống</span><strong id="browser-account-active">...</strong></div>
    <div class="row"><span class="label" data-i18n="actualSync">Đối chiếu tài khoản</span><strong id="browser-account-login-sync">...</strong></div>
    <div class="account-check" aria-live="polite" id="account-check">
      <div class="account-check-head"><strong data-i18n="preflightTitle">Kiểm tra tổng thể</strong><strong id="account-check-overall" class="warn">Chưa kiểm tra</strong></div>
      <div id="account-check-time" class="account-check-time"></div>
      <div id="account-check-list" class="account-check-list"></div>
      <div id="account-check-note" class="account-check-note" data-i18n="preflightHint">Chỉ kiểm tra điều kiện, không chạy thử tác vụ.</div>
    </div>
    <button id="account-check-button" class="primary-wide" onclick="checkAllAccounts(this)" data-i18n="checkAllAccounts">Kiểm tra toàn bộ Main / Worker</button>
    <details class="compact-details">
      <summary data-i18n="technicalDetails">Chi tiết kỹ thuật</summary>
      <div class="row"><span class="label">Worker Edge</span><strong id="browser-account-running">...</strong></div>
      <div class="row"><span class="label" data-i18n="manualMainMapping">Gán Main (thủ công)</span><strong id="browser-account-main">...</strong></div>
      <div class="row"><span class="label">Config</span><strong id="browser-account-config">...</strong></div>
    </details>
      </div>
    </div>
    <div class="notice info" data-i18n="browserAccountNote">Detection reads only ChatGPT name/email inside each browser. It never copies passwords, cookies, session tokens, or access tokens. Main Edge is never focused or navigated.</div>
  </section>


  <section id="funnel" class="card span-12">
    <div class="section-head"><div><h2>Tailscale Funnel</h2><p class="section-copy" data-i18n="funnelHint">Theo dõi kết nối công khai qua HTTPS.</p></div></div>
    <div class="notice info" data-i18n="funnelShared">Actuator và Orchestrator dùng chung một Funnel. Bật/tắt ở đây ảnh hưởng cả hai.</div>
    <div class="row"><span class="label" data-i18n="funnelState">Funnel công khai</span><strong id="funnel-state">...</strong></div>
    <div class="row"><span class="label">OAuth Gateway</span><strong id="funnel-gateway">...</strong></div>
    <div class="row"><span class="label" data-i18n="funnelPublic">Kiểm tra HTTPS công khai gần nhất</span><strong id="funnel-public">...</strong></div>
    <div class="small" id="funnel-error" role="status"></div>
    <div class="actions">
      <button id="funnel-enable" onclick="funnelAction('enable',this)" data-i18n="funnelEnable">Bật Funnel cho cả hai</button>
      <button id="funnel-disable" class="danger" onclick="funnelAction('disable',this)" data-i18n="funnelDisable">Tắt Funnel cho cả hai</button>
      <button id="funnel-refresh" class="secondary" onclick="refreshFunnel(this)" data-i18n="refreshStatus">Làm mới trạng thái</button>
    </div>
    <div class="account-workspace" style="margin-top:20px">
      <div class="service-panel"><h3>Actuator</h3>
        <div class="row"><span class="label" data-i18n="funnelRoute">Truy cập qua Funnel</span><strong id="funnel-actuator-state">...</strong></div>
        <div class="row"><span class="label" data-i18n="funnelBackend">Cổng dịch vụ nội bộ</span><strong id="funnel-actuator-backend">...</strong></div>
        <label class="field-label" for="funnel-actuator-full-url">Full MCP URL</label><input id="funnel-actuator-full-url" type="text" readonly>
        <button class="secondary primary-wide" onclick="copyFunnelUrl('funnel-actuator-full-url')" data-i18n="copyUrl">Sao chép URL</button>
      </div>
      <div class="service-panel"><h3>Orchestrator</h3>
        <div class="row"><span class="label" data-i18n="funnelRoute">Truy cập qua Funnel</span><strong id="funnel-orchestrator-state">...</strong></div>
        <div class="row"><span class="label" data-i18n="funnelBackend">Cổng dịch vụ nội bộ</span><strong id="funnel-orchestrator-backend">...</strong></div>
        <label class="field-label" for="funnel-orchestrator-url">MCP URL</label><input id="funnel-orchestrator-url" type="text" readonly>
        <button class="secondary primary-wide" onclick="copyFunnelUrl('funnel-orchestrator-url')" data-i18n="copyUrl">Sao chép URL</button>
      </div>
    </div>
    <div class="small" id="funnel-checked" style="margin-top:15px"></div>
    <div class="small" id="funnel-last-action"></div>
    <p class="small" data-i18n="funnelProofNote">Funnel bật và HTTPS phản hồi chưa xác nhận OAuth hoặc một lời gọi MCP thành công.</p>
  </section>

  <section id="diagnostics" class="card span-12">
    <div class="diagnostics-row">
      <div class="section-head">
        <div>
          <h2 data-i18n="diagnostics">Diagnostics</h2>
          <p class="section-copy" data-i18n="diagnosticsHint">Các thao tác bảo trì ít dùng được gom riêng để giảm nhầm lẫn.</p>
        </div>
      </div>
      <div class="actions">
        <button class="secondary" data-i18n="refreshStatus" onclick="refresh()">Refresh status</button>
        <button class="secondary" data-i18n="refreshLogs" onclick="loadLogs()">Refresh logs</button>
      </div>
    </div>
    <details class="danger-zone compact-details">
      <summary data-i18n="dangerActions">Thao tác nguy hiểm / ít dùng</summary>
      <div class="small" data-i18n="restartAllNote">Restart all restarts Tunnel + ChatGPT-Actuator + ChatGPT-Orchestrator.</div>
      <div class="actions">
        <button class="danger" data-i18n="restartAll" onclick="action('/api/all/restart','restartAll',this)">Restart all</button>
      </div>
    </details>
  </section>

  <details id="logs-panel" class="log-panel">
    <summary>
      <span data-i18n="recentLogs">Recent service logs</span>
      <span class="summary-hint" data-i18n="logsHint">Collapsed by default to keep the dashboard focused.</span>
    </summary>
    <pre id="logs">Press Refresh logs.</pre>
  </details>
</div>
</main>
<section class="shell"><details class="log-panel"><summary>UIA / DOM</summary>
<a href="/automation" target="_blank" rel="noopener">Điều khiển lệnh DOM và tiến độ kiểm tra link</a>
<h3>Actuator / Orchestrator — công cụ và phiên</h3>
<style>#operations-tools td,#operations-tools th{padding:8px 12px;text-align:left;border-bottom:1px solid #28364b}#operations-table{overflow:auto;max-height:360px}#operations-table table{border-collapse:collapse;width:100%}</style>
<div id="operations-summary" class="small">Đang đọc danh mục cục bộ…</div><div id="provider-readiness" class="small"></div>
<div id="schema-status" class="small">Đang đọc phiên bản và schema…</div>
<div class="small">Danh mục cục bộ không xác nhận quyền OAuth hoặc kết nối MCP của ứng dụng.</div>
<details><summary>Danh mục tool và scope cần thiết</summary><div id="operations-table"><table><thead><tr><th>Dịch vụ</th><th>Tool</th><th>Scope</th><th>Trạng thái</th></tr></thead><tbody id="operations-tools"></tbody></table></div></details>
<div id="automation-health" class="small">Đang đọc trạng thái worker…</div>
<div class="small">Khôi phục bản trước: Rollback_Update.vbs trong gói cập nhật UIA/DOM.</div>
</details></section>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const I18N={
  vi:{
    funnelBackend:"Cổng dịch vụ nội bộ",
    funnelHint:"Theo dõi kết nối công khai của hai dịch vụ qua HTTPS.",
    funnelShared:"Actuator và Orchestrator dùng chung một Funnel. Bật/tắt ở đây ảnh hưởng cả hai, giữ nguyên các URL hiện tại.",
    funnelState:"Funnel công khai",
    funnelPublic:"Kiểm tra HTTPS công khai gần nhất",
    funnelEnable:"Bật Funnel cho cả hai",
    funnelDisable:"Tắt Funnel cho cả hai",
    funnelRoute:"Truy cập qua Funnel",
    copyUrl:"Sao chép URL",
    funnelProofNote:"Funnel bật và HTTPS phản hồi chưa xác nhận OAuth hoặc một lời gọi MCP thành công.",

    navContext:"Chuyển ngữ cảnh",
    contextTitle:"Chuyển ngữ cảnh ChatGPT",
    contextHint:"Tạo hồ sơ từ ChatGPT đang đăng nhập (Main = Worker); không dùng share link hay OpenAI Export Data.",
    contextWarning:"Bấm nút sẽ gửi prompt vào chat mới sau xác nhận. Luôn kiểm tra nội dung do AI tạo.",
    contextStart:"Tạo hồ sơ chuyển tài khoản",contextDownloadMd:"Tải Markdown",contextDownloadJson:"Tải JSON",
    contextStorage:"Lưu cục bộ trong ChatGPT-Migration-Bridge/private/context-transfer (chưa mã hóa riêng).",
    historyTitle:"Nhập lịch sử ChatGPT",
    historyHint:"Chọn ZIP Export từ ChatGPT. Hệ thống sao lưu và nhập bổ sung vào kho tìm kiếm trên máy.",
    historyUpload:"Tải ZIP và nhập", historySelect:"Chọn một file Export ZIP trước.",
    historyReceiving:"Đang tải ZIP lên", historyProcessing:"Đang nhập dữ liệu và kiểm tra SQLite...",
    historyComplete:"Nhập lịch sử hoàn tất", historyError:"Không thể nhập lịch sử",
    historyWait:"Đang xử lý trong nền. Có thể tiếp tục dùng trang; không khởi động lại dịch vụ lúc này.",
    historyNewChats:"Hội thoại mới", historyNewMessages:"Tin nhắn mới", historyStored:"Tổng hội thoại / tin nhắn",
    historyStatusFail:"Không đọc được trạng thái nhập", historyNetworkFail:"Mất kết nối khi tải ZIP.",
    historyInvalid:"Hãy chọn file .zip không rỗng và nhỏ hơn 2 GiB.",
    historyConfirm:"Nhập Export ZIP này vào kho lịch sử trên máy?",
    historyLocalNotice:"Dữ liệu lưu cục bộ trên máy; không khôi phục lịch sử hay Memory chính thức của tài khoản ChatGPT.",
    dismissNotice:"Đóng",
    accountRoles:"Main: trình duyệt bạn dùng để trò chuyện. Worker: trình duyệt chạy tác vụ tự động.",
    title:"Trung tâm điều khiển ChatGPT-Actuator",
    subtitle:"Quản lý kết nối, tài khoản và kiểm tra hệ thống tại một nơi.",
    loading:"Đang tải trạng thái...",
    serviceStack:"Hệ thống dịch vụ",
    serviceStackHint:"Trạng thái và điều khiển các tiến trình cục bộ cốt lõi.",
    accountOpsHint:"Dùng cùng tài khoản ChatGPT trên hai trình duyệt, rồi bấm Kiểm tra hệ thống.",
    diagnosticsHint:"Các thao tác bảo trì ít dùng được gom riêng để giảm nhầm lẫn.",
    logsHint:"Thu gọn mặc định để dashboard tập trung vào trạng thái chính.",
    navServices:"Hệ thống",
    navAccounts:"Tài khoản",
    navDiagnostics:"Chẩn đoán",
    technicalDetails:"Chi tiết kỹ thuật",
    nextStep:"Bước tiếp theo",
    nextDetect:"Bấm Kiểm tra hệ thống để xác minh đăng nhập và kết nối.",
    nextSignIn:"Mở / đăng nhập Worker cho profile đang chọn.",
    nextSync:"Main và Worker đang khác nhau — đồng bộ Worker với Main.",
    nextConfirm:"Xác nhận profile này là tài khoản Main.",
    nextReady:"Hai tài khoản khớp. Bấm Kiểm tra hệ thống để xác minh kết nối.",
    advancedAccount:"Đồng bộ nâng cao",
    profileManagement:"Quản lý hồ sơ tài khoản",
    dangerActions:"Thao tác nguy hiểm / ít dùng",
    connectivity:"Kết nối",
    tunnelProcess:"Tiến trình Tunnel",
    mcpHealth:"Trạng thái MCP",
    adminHealth:"Trạng thái Admin",
    tunnelPids:"PID Tunnel",
    profile:"Profile",
    start:"Khởi động",
    stop:"Dừng",
    restartTunnel:"Khởi động lại Tunnel",
    version:"Phiên bản",
    controlCenter:"Control Center",
    controlUptime:"Thời gian hoạt động",
    running:"Đang chạy",
    stopped:"Đã dừng",
    healthy:"Hoạt động tốt",
    unavailable:"Không khả dụng",
    unknown:"không rõ",
    restartActuator:"Khởi động lại Actuator",
    openAdmin:"Mở Admin",
    restartActuatorNote:"Khởi động lại Actuator sẽ khởi động lại cả cây tiến trình Tunnel vì MCP Actuator là tiến trình con của tunnel-client.",
    process:"Tiến trình",
    health:"Trạng thái",
    uptime:"Thời gian hoạt động",
    backend:"Backend cấu hình",
    activeTransport:"Transport đang dùng",
    activeWorkers:"Worker đang hoạt động",
    activeJobs:"Job đang chạy",
    idle:"Đang rảnh",
    startOrchestrator:"Khởi động Orchestrator",
    stopOrchestrator:"Dừng Orchestrator",
    restartOrchestrator:"Khởi động lại Orchestrator",
    systemChatGPTAccount:"Tài khoản ChatGPT hệ thống",
    accountSetup:"1. Chọn và đăng nhập",
    accountVerify:"2. Kiểm tra sẵn sàng",
    chooseProfile:"Chọn hồ sơ tài khoản",
    modeSame:"Main và Worker dùng chung tài khoản",
    preflightTitle:"Kiểm tra tổng thể",
    preflightHint:"Kiểm tra kết nối và đăng nhập; chưa chạy thử tác vụ ChatGPT.",
    checkAllAccounts:"Kiểm tra hệ thống",
    systemAccount:"Hồ sơ đang dùng",
    mainLogin:"Tài khoản bạn đang dùng (Main)",
    workerLogin:"Tài khoản chạy tác vụ (Worker)",
    actualSync:"Đối chiếu tài khoản",
    manualMainMapping:"Gán Main",
    detectAccounts:"Phát hiện tài khoản",
    switchSystemAccount:"Dùng hồ sơ đã chọn",
    confirmMainAccount:"Xác nhận Main = tài khoản này",
    syncWorkerWithMain:"Đồng bộ Worker với Main",
    openWorkerLogin:"Mở đăng nhập trình duyệt tác vụ",
    addSystemAccount:"Thêm tài khoản",
    accountAliasPlaceholder:"Ví dụ: Tài khoản 2",
    browserAccountNote:"Phát hiện chỉ đọc tên/email ChatGPT trong từng trình duyệt. Không sao chép mật khẩu, cookie, session token hoặc access token. Main Edge không bị focus hay điều hướng.",
    activeAccountMarker:"đang dùng",
    configured:"Đúng",
    misconfigured:"Sai cấu hình",
    mainNotConfirmed:"Chưa xác nhận",
    mainConfirmed:"Đã gán thủ công",
    sameAccount:"Main = Worker ✓",
    syncRequired:"Cần đồng bộ Main/Worker",
    sameLogin:"Main = Worker ✓",
    differentLogin:"Main ≠ Worker ⚠",
    loginUnknown:"Chưa xác định",
    notDetected:"Chưa phát hiện",
    notSignedIn:"Chưa đăng nhập",
    mainAuthorizationRequired:"Cần bấm Allow remote debugging trên Main Edge",
    noChatGPTTab:"Không có tab ChatGPT để đọc",
    detectionUnavailable:"Không thể tự phát hiện",
    detectCompleted:"Đã cập nhật tài khoản đăng nhập.",
    detectNeedsMainPermission:"Worker đã được kiểm tra. Main Edge đang chờ bạn bấm “Allow remote debugging?”, sau đó bấm Phát hiện tài khoản lần nữa.",
    systemSwitchNext:"Trình duyệt tác vụ đã chuyển hồ sơ. Đăng nhập cùng tài khoản trên trình duyệt bạn dùng, rồi bấm Kiểm tra hệ thống.",
    mainConfirmedDone:"Đã ghi nhận tài khoản Main.",
    workerSyncedDone:"Worker đã đồng bộ theo tài khoản Main.",
    diagnostics:"Chẩn đoán",
    refreshStatus:"Làm mới trạng thái",
    refreshLogs:"Làm mới log",
    restartAll:"Khởi động lại tất cả",
    restartAllNote:"“Khởi động lại tất cả” sẽ khởi động lại Tunnel + ChatGPT-Actuator + ChatGPT-Orchestrator.",
    recentLogs:"Log dịch vụ gần đây",
    pressRefreshLogs:"Nhấn “Làm mới log” để xem.",
    ready:"Sẵn sàng",
    statusError:"Lỗi trạng thái",
    startTunnel:"Khởi động Tunnel",
    stopTunnel:"Dừng Tunnel",
    inProgress:"đang thực hiện...",
    completed:"đã hoàn tất.",
    failed:"thất bại"
  },
  en:{
    funnelBackend:"Local service port",
    funnelHint:"Monitor public HTTPS access for both services.",
    funnelShared:"Actuator and Orchestrator share one Funnel. Enable/disable affects both and preserves existing URLs.",
    funnelState:"Public Funnel",
    funnelPublic:"Latest public HTTPS check",
    funnelEnable:"Enable Funnel for both",
    funnelDisable:"Disable Funnel for both",
    funnelRoute:"Access through Funnel",
    copyUrl:"Copy URL",
    funnelProofNote:"Enabled Funnel and HTTPS responses do not confirm OAuth or a successful MCP call.",

    navContext:"Transfer context",
    contextTitle:"Transfer ChatGPT context",
    contextHint:"Generate a profile from signed-in ChatGPT (Main = Worker); no share links or OpenAI export.",
    contextWarning:"A new prompt will be sent after confirmation. Review AI-generated claims before using them.",
    contextStart:"Create transfer profile",contextDownloadMd:"Download Markdown",contextDownloadJson:"Download JSON",
    contextStorage:"Saved locally under ChatGPT-Migration-Bridge/private/context-transfer (not independently encrypted).",
    historyTitle:"Import ChatGPT history",
    historyHint:"Choose a ChatGPT Export ZIP. The system backs up and incrementally updates your local searchable archive.",
    historyUpload:"Upload ZIP and import", historySelect:"Choose a ChatGPT Export ZIP first.",
    historyReceiving:"Uploading ZIP", historyProcessing:"Importing and verifying SQLite...",
    historyComplete:"History import completed", historyError:"History import failed",
    historyWait:"Processing in the background. Keep services running until completed.",
    historyNewChats:"New conversations", historyNewMessages:"New messages", historyStored:"Total chats / messages",
    historyStatusFail:"Unable to read import status", historyNetworkFail:"Network error while uploading ZIP.",
    historyInvalid:"Choose a nonempty .zip smaller than 2 GiB.",
    historyConfirm:"Import this Export ZIP into the local history archive?",
    historyLocalNotice:"Data stays local. This does not restore native ChatGPT conversation history or Memory.",
    dismissNotice:"Dismiss",
    accountRoles:"Main: the browser you use to chat. Worker: the browser running automated tasks.",
    title:"ChatGPT-Actuator Control Center",
    subtitle:"Manage connections, accounts and system checks in one place.",
    loading:"Loading status...",
    serviceStack:"Service stack",
    serviceStackHint:"Status and controls for the core local processes.",
    accountOpsHint:"Use the same ChatGPT account in both browsers, then click Check system.",
    diagnosticsHint:"Less-frequent maintenance actions are grouped here to reduce mistakes.",
    logsHint:"Collapsed by default so the dashboard stays focused on live status.",
    navServices:"Services",
    navAccounts:"Accounts",
    navDiagnostics:"Diagnostics",
    technicalDetails:"Technical details",
    nextStep:"Next step",
    nextDetect:"Click Check system to verify sign-in and connections.",
    nextSignIn:"Open / sign in Worker for the selected profile.",
    nextSync:"Main and Worker differ — sync Worker to Main.",
    nextConfirm:"Confirm this profile as the Main account.",
    nextReady:"Accounts match. Click Check system to verify connections.",
    advancedAccount:"Advanced account sync",
    profileManagement:"Manage account profiles",
    dangerActions:"Dangerous / infrequent actions",
    connectivity:"Connectivity",
    tunnelProcess:"Tunnel process",
    mcpHealth:"MCP health",
    adminHealth:"Admin health",
    tunnelPids:"Tunnel PID(s)",
    profile:"Profile",
    start:"Start",
    stop:"Stop",
    restartTunnel:"Restart tunnel",
    version:"Version",
    controlCenter:"Control Center",
    controlUptime:"Control uptime",
    running:"Running",
    stopped:"Stopped",
    healthy:"Healthy",
    unavailable:"Unavailable",
    unknown:"unknown",
    restartActuator:"Restart Actuator",
    openAdmin:"Open Admin",
    restartActuatorNote:"Restart Actuator restarts the tunnel process tree because the MCP Actuator is a child of tunnel-client.",
    process:"Process",
    health:"Health",
    uptime:"Uptime",
    backend:"Configured backend",
    activeTransport:"Active transport",
    activeWorkers:"Active workers",
    activeJobs:"Active jobs",
    idle:"Idle",
    startOrchestrator:"Start Orchestrator",
    stopOrchestrator:"Stop Orchestrator",
    restartOrchestrator:"Restart Orchestrator",
    systemChatGPTAccount:"System ChatGPT Account",
    accountSetup:"1. Choose and sign in",
    accountVerify:"2. Check readiness",
    chooseProfile:"Choose an account profile",
    modeSame:"Main and Worker use the same account",
    preflightTitle:"System preflight",
    preflightHint:"Checks connections and sign-in; does not run a ChatGPT task.",
    checkAllAccounts:"Check system",
    systemAccount:"Active account profile",
    mainLogin:"Your account (Main)",
    workerLogin:"Task account (Worker)",
    actualSync:"Account comparison",
    manualMainMapping:"Main mapping",
    detectAccounts:"Detect signed-in accounts",
    switchSystemAccount:"Use selected profile",
    confirmMainAccount:"Confirm Main = this account",
    syncWorkerWithMain:"Sync Worker with Main",
    openWorkerLogin:"Open task browser sign-in",
    addSystemAccount:"Add account",
    accountAliasPlaceholder:"e.g. Account 2",
    browserAccountNote:"Detection reads only the ChatGPT name/email inside each browser. It never copies passwords, cookies, session tokens, or access tokens. Main Edge is never focused or navigated.",
    activeAccountMarker:"active",
    configured:"OK",
    misconfigured:"Misconfigured",
    mainNotConfirmed:"Not confirmed",
    mainConfirmed:"Manually mapped",
    sameAccount:"Main = Worker ✓",
    syncRequired:"Main/Worker sync required",
    sameLogin:"Main = Worker ✓",
    differentLogin:"Main ≠ Worker ⚠",
    loginUnknown:"Unknown",
    notDetected:"Not detected",
    notSignedIn:"Not signed in",
    mainAuthorizationRequired:"Allow remote debugging on Main Edge",
    noChatGPTTab:"No ChatGPT tab available to read",
    detectionUnavailable:"Detection unavailable",
    detectCompleted:"Signed-in account information updated.",
    detectNeedsMainPermission:"Worker was checked. Main Edge is waiting for “Allow remote debugging?”; approve it and run detection again.",
    systemSwitchNext:"Task browser profile switched. Sign in to the same account in your browser, then click Check system.",
    mainConfirmedDone:"Main account confirmation saved.",
    workerSyncedDone:"Worker synced to the confirmed Main account.",
    diagnostics:"Diagnostics",
    refreshStatus:"Refresh status",
    refreshLogs:"Refresh logs",
    restartAll:"Restart all",
    restartAllNote:"Restart all restarts Tunnel + ChatGPT-Actuator + ChatGPT-Orchestrator.",
    recentLogs:"Recent service logs",
    pressRefreshLogs:"Press Refresh logs.",
    ready:"Ready",
    statusError:"Status error",
    startTunnel:"Start tunnel",
    stopTunnel:"Stop tunnel",
    inProgress:"in progress...",
    completed:"completed.",
    failed:"failed"
  }
};
let currentLang="vi";
let lastStatus=null;
let logsLoaded=false;
let lastAccountCheck=null;
let checkingAccounts=false;
const accountMode="same";
try{
  const saved=localStorage.getItem("chatgpt-actuator-control-language");
  if(saved==="vi"||saved==="en") currentLang=saved;
}catch(_e){}
function t(key){return (I18N[currentLang]&&I18N[currentLang][key])||I18N.en[key]||key;}
function applyLanguage(){
  document.documentElement.lang=currentLang;
  document.title=t("title");
  document.querySelectorAll("[data-i18n]").forEach(el=>{el.textContent=t(el.dataset.i18n);});
  $("control-running").textContent="● "+t("running");
  if($("browser-account-new")) $("browser-account-new").placeholder=t("accountAliasPlaceholder");
  $("lang-vi").classList.toggle("active",currentLang==="vi");
  $("lang-en").classList.toggle("active",currentLang==="en");
  $("lang-vi").setAttribute("aria-pressed",String(currentLang==="vi"));
  $("lang-en").setAttribute("aria-pressed",String(currentLang==="en"));
  if(!logsLoaded) $("logs").textContent=t("pressRefreshLogs");
  if(lastStatus) render(lastStatus);
  renderAccountCheck();
  renderSystemSummary();
  renderFunnel();
}
function setLanguage(lang){
  if(lang!=="vi"&&lang!=="en") return;
  currentLang=lang;
  try{localStorage.setItem("chatgpt-actuator-control-language",lang);}catch(_e){}
  applyLanguage();
  refresh();
}
function state(ok,text){return '<span class="'+(ok?'ok':'bad')+'"><span class="dot">●</span>'+text+'</span>'}
let globalBusy=false;
let stateGeneration=0;
let lastUpdatedAt=0;
let statusConnectionError=false;
function updateServiceButtonStates(s){
  if(globalBusy||!s) return;
  const tunnelRunning=!!(s.tunnel&&s.tunnel.running);
  const orchRunning=!!(s.orchestrator&&s.orchestrator.running);
  if($("tunnel-start-btn")) $("tunnel-start-btn").disabled=tunnelRunning;
  if($("tunnel-stop-btn")) $("tunnel-stop-btn").disabled=!tunnelRunning;
  if($("orch-start-btn")) $("orch-start-btn").disabled=orchRunning;
  if($("orch-stop-btn")) $("orch-stop-btn").disabled=!orchRunning;
}
function setBusy(flag,button=null){
  globalBusy=!!flag;
  if(flag){
    stateGeneration++;
    if(button){button.dataset.busyLabel=button.textContent;button.textContent=currentLang==="vi"?"Đang xử lý…":"Working…";button.classList.add("busy-button");}
  }
  document.querySelectorAll("button,input,select").forEach(b=>b.disabled=globalBusy);
  if(!flag){
    document.querySelectorAll(".busy-button").forEach(b=>{b.textContent=b.dataset.busyLabel;b.classList.remove("busy-button");delete b.dataset.busyLabel;});
    if(lastStatus) updateServiceButtonStates(lastStatus);
  }
  renderSystemSummary();
  renderFunnel();
}
async function api(path,opts={}){
  opts.headers=Object.assign({'Accept':'application/json','X-Control-Center-Token':TOKEN},opts.headers||{});
  const r=await fetch(path,opts);
  const b=await r.json().catch(()=>({error:'Invalid JSON response'}));
  if(!r.ok) throw new Error(b.error||('HTTP '+r.status));
  return b;
}
function render(s){
  lastStatus=s;
  lastFunnelStatus=null;
  $("tunnel").innerHTML=state(!!s.tunnel.running,s.tunnel.running?t("running"):t("stopped"));
  $("mcp").innerHTML=state(!!s.tunnel.health_ok,s.tunnel.health_ok?t("healthy"):t("unavailable"));
  $("admin").innerHTML=state(!!s.tunnel.admin_ok,s.tunnel.admin_ok?t("healthy"):t("unavailable"));
  $("pids").textContent=(s.tunnel.pids||[]).join(", ")||"-";
  $("profile").textContent=s.tunnel.profile||"-";
  $("version").textContent=s.actuator.version||t("unknown");
  $("uptime").textContent=s.control_center.uptime_human;
  const o=s.orchestrator||{};
  $("orch-process").innerHTML=state(!!o.running,o.running?t("running"):t("stopped"));
  $("orch-health").innerHTML=state(!!o.health_ok,o.health_ok?t("healthy"):t("unavailable"));
  $("orch-version").textContent=o.version||t("unknown");
  $("orch-uptime").textContent=o.uptime_human||"0s";
  $("orch-backend").textContent=o.backend||t("unknown");
  const br=o.backend_runtime||{};
  const transport=br.selected_backend||"unknown";
  $("orch-transport").textContent=(transport==="cdp"||transport==="cdp_background")?"CDP":(transport==="uia"||transport==="uia_fallback")?"UIA fallback":transport==="mixed"?"CDP + UIA":(transport==="none"||transport==="idle")?t("idle"):transport;
  $("orch-workers").textContent=String(o.active_worker_count??0);
  $("orch-jobs").textContent=String(o.active_job_count??0);
  const ba=s.browser_accounts||{};
  const accountSelect=$("browser-account-select");
  const oldSelected=accountSelect.value;
  accountSelect.innerHTML="";
  (ba.accounts||[]).forEach(item=>{
    const option=document.createElement("option");
    option.value=item.id;
    option.textContent=item.label+(item.active?(" · "+t("activeAccountMarker")):"");
    accountSelect.appendChild(option);
  });
  if((ba.accounts||[]).some(item=>item.id===oldSelected)) accountSelect.value=oldSelected;
  else if(ba.active_account_id) accountSelect.value=ba.active_account_id;
  const activeAccount=(ba.accounts||[]).find(item=>item.active);
  const mainAccount=(ba.accounts||[]).find(item=>item.id===ba.main_confirmed_account_id);
  $("browser-account-active").textContent=activeAccount?activeAccount.label:(ba.active_account_id||"-");
  const mainMapEl=$("browser-account-main");
  mainMapEl.textContent=mainAccount?(mainAccount.label+" · "+t("mainConfirmed")):t("mainNotConfirmed");
  mainMapEl.className=mainAccount?"ok":"warn";

  const detection=ba.identity_detection||{};
  const mainIdentity=detection.main||{};
  const workerIdentity=detection.worker||{};
  function identityText(identity,isMain){
    if(identity.authenticated){
      const parts=[];
      if(identity.name) parts.push(identity.name);
      if(identity.email && identity.email!==identity.name) parts.push(identity.email);
      return parts.join(" · ")||t("unknown");
    }
    if(identity.state==="signed_out") return t("notSignedIn");
    if(isMain && identity.state==="authorization_required") return t("mainAuthorizationRequired");
    if(identity.state==="no_chatgpt_tab") return t("noChatGPTTab");
    if(["cdp_unavailable","cdp_error","cdp_timeout","playwright_unavailable","session_unavailable"].includes(identity.state)) return t("detectionUnavailable");
    return t("notDetected");
  }
  const mainLoginEl=$("browser-account-main-login");
  mainLoginEl.textContent=identityText(mainIdentity,true);
  mainLoginEl.className=mainIdentity.authenticated?"ok":(mainIdentity.state==="signed_out"?"bad":"warn");
  const workerLoginEl=$("browser-account-worker-login");
  workerLoginEl.textContent=identityText(workerIdentity,false);
  workerLoginEl.className=workerIdentity.authenticated?"ok":(workerIdentity.state==="signed_out"?"bad":"warn");

  const loginSyncEl=$("browser-account-login-sync");
  if(detection.same_login_detected===true){
    loginSyncEl.textContent=t("sameLogin");
    loginSyncEl.className="ok";
  }else if(detection.same_login_detected===false){
    loginSyncEl.textContent=t("differentLogin");
    loginSyncEl.className="bad";
  }else{
    loginSyncEl.textContent=t("loginUnknown");
    loginSyncEl.className="warn";
  }

  $("browser-account-running").innerHTML=state(!!ba.worker_edge_running,ba.worker_edge_running?t("running"):t("stopped"));
  $("browser-account-config").innerHTML=state(!!ba.config_matches,ba.config_matches?t("configured"):t("misconfigured"));
  $("browser-account-path").textContent=ba.active_user_data_dir||"-";

  let nextKey="nextDetect";
  if(workerIdentity.state==="signed_out"){
    nextKey="nextSignIn";
  }else if(detection.same_login_detected===false){
    nextKey="nextSync";
  }else if(detection.same_login_detected===true && !ba.main_confirmed_account_id){
    nextKey="nextConfirm";
  }else if(detection.same_login_detected===true && ba.main_confirmed_account_id){
    nextKey="nextReady";
  }else if(workerIdentity.authenticated && !mainIdentity.authenticated){
    nextKey="nextDetect";
  }
  const nextEl=$("account-next-step");
  if(nextEl){
    nextEl.textContent=t(nextKey);
    nextEl.className=nextKey==="nextReady"?"ok":(nextKey==="nextSync"?"warn":"");
  }
  const advanced=$("account-advanced");
  if(advanced && nextKey==="nextConfirm") advanced.open=true;
  updateServiceButtonStates(s);
  renderAccountCheck();
  renderSystemSummary();
  renderFunnel();
}
let refreshing=false;
function renderSchemaStatus(info){const node=$('schema-status');if(!node)return;node.textContent='Backend '+(info.runtime_version||'unknown')+' — schema '+(info.automation_schema_fingerprint||'chưa đọc được')+' — danh mục tool phía ứng dụng: chưa có bằng chứng đồng bộ. Làm mới tools/list trong ứng dụng khi cần.';}
async function refresh(){
  if(globalBusy||refreshing) return;
  refreshing=true;
  const generation=stateGeneration;
  try{
    const s=await api("/api/status");
    if(generation!==stateGeneration) return;
    statusConnectionError=false;lastUpdatedAt=Date.now();render(s);
    const o=s.operations||{};
    renderSchemaStatus(o.schema_status||{});
    const ar=o.actuator||{}, or=o.orchestrator||{};
    $("operations-summary").textContent=`Actuator: ${ar.count??'?'} tool (${ar.state==='inventory_observed'?'đã đọc danh mục':'chưa đọc được danh mục'}); Orchestrator: ${or.count??'?'} tool (${or.state==='inventory_observed'?'đã đọc danh mục':'chưa đọc được danh mục'}).`;
    const provider=$("provider-readiness");provider.replaceChildren();
    const states={verified:'Đã kiểm chứng mẫu',configured_only:'Chỉ có cấu hình',stale_evidence:'Bằng chứng đã cũ',blocked_by_permissions:'Thiếu quyền',blocked_by_configuration:'Cấu hình đang tắt',blocked:'Bị chặn',failed:'Kiểm thử thất bại',unavailable:'Chưa sẵn sàng'};
    for(const group of o.provider_groups||[]){const line=document.createElement('p');line.textContent=group.name+': '+(states[group.state]||group.state)+' — '+group.coverage+(group.checked_at?' — '+new Date(group.checked_at*1000).toLocaleString('vi-VN'):'');provider.append(line)}
    const target=$("operations-tools");target.replaceChildren();
    for(const [service,data] of [['Actuator',ar],['Orchestrator',or]]){
      for(const tool of data.tools||[]){
        const row=document.createElement('tr');
        const stateLabel={registered_not_probed:'Đã đăng ký; chưa kiểm tra backend',blocked_by_policy:'Chặn bởi chính sách'}[tool.state]||'Chưa có bằng chứng';
        for(const value of [service,tool.name,tool.required_scope||'quyền riêng của Orchestrator',stateLabel]){
          const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);
        }
        target.appendChild(row);
      }
    }
    const a=s.automation||{}, records=a.records||[];
    const refs=records.reduce((n,r)=>n+(r.dom?.pinned_refs||0),0);
    const timeouts=records.reduce((n,r)=>n+(r.uia?.timeouts||0),0);
    const native=a.native||{};
    $("automation-health").textContent=`Runtime native: ${native.ready?'sẵn sàng':'chưa sẵn sàng'}; discovery: ${native.mcp_discovery||'chưa có dữ liệu'}. DOM refs: ${refs}; UIA timeout: ${timeouts}. Chỉ số cũ quá 120 giây được bỏ qua.`;
  }catch(e){
    if(generation===stateGeneration){statusConnectionError=true;renderSystemSummary();}
  }finally{refreshing=false;}
}
async function action(path,labelKey,button=null){
  const label=t(labelKey);
  if(!confirm(label+"?")) return;
  setBusy(true,button);
  notifyUser(label+" "+t("inProgress"));
  try{
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    render(r.status);
    notifyUser(label+" "+t("completed"));
    await loadLogs();
  }catch(e){
    notifyUser(label+" "+t("failed")+": "+e.message);
  }finally{
    setBusy(false);
  }
}
async function browserAccountPost(path,labelKey,body,confirmFirst=true,button=null){
  const label=t(labelKey);
  if(confirmFirst && !confirm(label+"?")) return null;
  setBusy(true,button);
  notifyUser(label+" "+t("inProgress"));
  try{
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
    if(r.status) render(r.status);
    if(["/api/browser-accounts/switch","/api/browser-accounts/login",
        "/api/browser-accounts/sync-to-main"].includes(path)){
      lastAccountCheck=null;
      renderAccountCheck();
    }
    const launchError=r.result&&r.result.launch_error?(" · "+r.result.launch_error):"";
    notifyUser(label+" "+t("completed")+launchError);
    return r;
  }catch(e){
    notifyUser(label+" "+t("failed")+": "+e.message);
    return null;
  }finally{
    setBusy(false);
  }
}
const PREFLIGHT_LABELS={
  config:["Cấu hình","Config"],
  main_cdp:["Kết nối trình duyệt bạn dùng","Your browser connection"],
  main_identity:["Đăng nhập của bạn","Your sign-in"],
  worker_cdp:["Kết nối trình duyệt tác vụ","Task browser connection"],
  worker_identity:["Đăng nhập trình duyệt tác vụ","Task browser sign-in"],
  alignment:["Đối chiếu tài khoản","Account matching"],
  worker_preflight:["Điều kiện chạy tác vụ","Task prerequisites"]
};
const PREFLIGHT_REASONS={
  CONFIG_OK:["Hợp lệ","Valid"],CONFIG_MISMATCH:["Sai cấu hình","Misconfigured"],
  CDP_CONNECTED:["Kết nối được","Connected"],CDP_AUTH_REQUIRED:["Cần bạn cho phép kết nối","Connection permission needed"],
  CDP_TIMEOUT:["Hết thời gian chờ","Timed out"],CDP_UNAVAILABLE:["Chưa kết nối được","Connection unavailable"],
  CDP_UNKNOWN:["Chưa xác minh","Unverified"],
  SIGNED_IN:["Đã nhận diện","Identified"],SIGNED_OUT:["Chưa đăng nhập","Signed out"],
  NO_CHATGPT_TAB:["Không có tab ChatGPT","No ChatGPT tab"],IDENTITY_UNKNOWN:["Chưa xác minh","Unverified"],
  ACCOUNTS_MATCH:["Khớp","Match"],ACCOUNTS_DIFFER:["Khác nhau","Different accounts"],
  ALIGNMENT_UNKNOWN:["Chưa đủ thông tin","Insufficient evidence"],
  WORKER_NOT_RUNNING:["Worker Edge chưa chạy","Worker Edge stopped"],
  WORKER_NOT_VERIFIED:["Chưa xác minh phiên Worker","Worker session unverified"],
  ORCH_UNAVAILABLE:["Orchestrator chưa sẵn sàng","Orchestrator unavailable"],
  WORKER_BUSY:["Đang có tác vụ","Worker busy"],STATIC_PREFLIGHT_OK:["Điều kiện tĩnh đạt","Static checks passed"]
};
function preflightText(value){
  const v=PREFLIGHT_REASONS[value]||[value,value];
  return v[currentLang==="vi"?0:1];
}
function renderAccountCheck(){
  const overall=$("account-check-overall"),list=$("account-check-list"),note=$("account-check-note"),timeEl=$("account-check-time");
  if(!overall||!list||!note||!timeEl) return;
  list.replaceChildren();
  renderSystemSummary();
  if(checkingAccounts){
    overall.textContent=currentLang==="vi"?"Đang kiểm tra...":"Checking...";
    overall.className="warn"; timeEl.textContent=""; note.textContent=t("preflightHint");return;
  }
  const report=lastAccountCheck;
  const fresh=accountReportFresh();
  if(!fresh){
    overall.textContent=currentLang==="vi"?"Chưa xác minh":"Not verified";
    overall.className="warn";
    timeEl.textContent=report?(currentLang==="vi"?"Kết quả cũ, hãy kiểm tra lại.":"Previous result expired; check again."):"";
    note.textContent=t("preflightHint");return;
  }
  const labels={ready:["Đạt kiểm tra tĩnh","Static checks passed"],attention:["Cần kiểm tra thêm","Needs attention"],blocked:["Chưa sẵn sàng","Not ready"]};
  overall.textContent=(labels[report.overall]||labels.attention)[currentLang==="vi"?0:1];
  overall.className=report.overall==="ready"?"ok":report.overall==="blocked"?"bad":"warn";
  timeEl.textContent=(currentLang==="vi"?"Kiểm tra lúc: ":"Checked at: ")+new Date(report.checked_at).toLocaleString(currentLang==="vi"?"vi-VN":"en-US");
  (report.checks||[]).forEach(item=>{
    const row=document.createElement("div");row.className="account-check-entry";
    const title=document.createElement("span");
    const label=PREFLIGHT_LABELS[item.id]||[item.id,item.id];
    title.textContent=label[currentLang==="vi"?0:1];
    const status=document.createElement("span");
    status.className=item.state==="ok"?"ok":item.state==="blocked"?"bad":item.state==="warning"?"warn":"";
    status.textContent=preflightText(item.reason_code);
    row.append(title,status);list.appendChild(row);
  });
  note.textContent=t("preflightHint");
}
async function checkAllAccounts(button=null){
  if(checkingAccounts) return;
  checkingAccounts=true;lastAccountCheck=null;setBusy(true,button);renderAccountCheck();
  notifyUser(currentLang==="vi"?"Đang kiểm tra Main/Worker, vui lòng đợi...":"Checking Main/Worker, please wait...");
  try{
    const result=await api("/api/browser-accounts/check-all",{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:accountMode})
    });
    if(result.status)render(result.status);
    lastAccountCheck=result.result||null;
    notifyUser(currentLang==="vi"?"Kiểm tra hoàn tất. Xem kết quả bên dưới.":"Preflight completed. Review the results below.");
  }catch(error){
    lastAccountCheck=null;
    notifyUser(t("failed")+": "+error.message);
  }finally{
    checkingAccounts=false;setBusy(false);renderAccountCheck();
  }
}
async function switchBrowserAccount(button=null){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  const r=await browserAccountPost("/api/browser-accounts/switch","switchSystemAccount",{account_id:accountId},true,button);
  if(r) notifyUser(t("systemSwitchNext"));
}
async function confirmMainAccount(button=null){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  const r=await browserAccountPost("/api/browser-accounts/confirm-main","confirmMainAccount",{account_id:accountId},false,button);
  if(r) notifyUser(t("mainConfirmedDone"));
}
async function syncWorkerWithMain(button=null){
  const r=await browserAccountPost("/api/browser-accounts/sync-to-main","syncWorkerWithMain",{},true,button);
  if(r) notifyUser(t("workerSyncedDone"));
}
async function openBrowserAccountLogin(button=null){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  await browserAccountPost("/api/browser-accounts/login","openWorkerLogin",{account_id:accountId},true,button);
}
async function createBrowserAccount(button=null){
  const input=$("browser-account-new");
  const label=(input.value||"").trim();
  if(!label){input.focus();return;}
  const r=await browserAccountPost("/api/browser-accounts/create","addSystemAccount",{label},false,button);
  if(r){
    input.value="";
    const created=r.result&&r.result.created_account_id;
    if(created) $("browser-account-select").value=created;
  }
}
async function loadLogs(){
  const panel=$("logs-panel");
  if(panel) panel.open=true;
  try{
    const [x,o]=await Promise.all([
      api("/api/logs?lines=80"),
      api("/api/orchestrator/logs?lines=80")
    ]);
    logsLoaded=true;
    $("logs").textContent=
      "--- Actuator/Tunnel stdout ---\n"+x.stdout+
      "\n\n--- Actuator/Tunnel stderr ---\n"+x.stderr+
      "\n\n--- Orchestrator stdout ---\n"+o.stdout+
      "\n\n--- Orchestrator stderr ---\n"+o.stderr;
  }catch(e){
    logsLoaded=true;
    $("logs").textContent=e.message;
  }
}

function notifyUser(message){
  $("banner-message").textContent=message;
  $("banner").hidden=false;
}
function dismissNotice(){$("banner").hidden=true;}
function accountReportFresh(){
  const report=lastAccountCheck;
  if(!report||!lastStatus||statusConnectionError) return false;
  const ba=lastStatus.browser_accounts||{};
  const age=Date.now()-Date.parse(report.checked_at);
  return report.profile_id===ba.active_account_id && report.mode===accountMode && age>=0 && age<120000;
}
function uiText(vi,en){return currentLang==="vi"?vi:en;}
function collectSystemIssues(){
  const issues=[];
  const seen=new Set();
  function add(id,title,help,label,run){
    if(seen.has(id))return;
    seen.add(id);issues.push({id,title,help,label,run});
  }
  const check=()=>checkAllAccounts($("system-check-button"));
  const accountHelp=()=>{location.hash="accounts";$("account-check-button").focus();};
  if(statusConnectionError){
    add("connection",uiText("Không tải được trạng thái hệ thống","System status unavailable"),uiText("Kiểm tra trang điều khiển còn chạy, rồi thử tải lại.","Check that the control center is running, then retry."),uiText("Thử lại","Retry"),refresh);
    return issues;
  }
  if(!lastStatus)return issues;
  const tunnel=lastStatus.tunnel||{},orch=lastStatus.orchestrator||{};
  if(!tunnel.running){
    add("tunnel",uiText("Kết nối đang dừng","Connection stopped"),uiText("Khởi động kết nối để các công cụ hoạt động.","Start the connection to enable tools."),t("startTunnel"),()=>action("/api/tunnel/start","startTunnel",$("system-check-button")));
  }else if(!tunnel.health_ok||!tunnel.admin_ok){
    add("tunnel",uiText("Kết nối chưa hoạt động tốt","Connection needs attention"),uiText("Xem trạng thái dịch vụ và nhật ký để xác định lỗi.","Review service status and logs for the cause."),uiText("Xem chẩn đoán","View diagnostics"),()=>{location.hash="diagnostics";loadLogs();});
  }
  if(!orch.running){
    add("orchestrator",uiText("Bộ chạy tác vụ đang dừng","Task runner stopped"),uiText("Khởi động bộ chạy tác vụ trước khi làm việc.","Start the task runner before working."),t("startOrchestrator"),()=>action("/api/orchestrator/start","startOrchestrator",$("system-check-button")));
  }else if(!orch.health_ok){
    add("orchestrator",uiText("Bộ chạy tác vụ chưa phản hồi tốt","Task runner needs attention"),uiText("Mở chẩn đoán để xem lỗi dịch vụ.","Open diagnostics to review service errors."),uiText("Xem chẩn đoán","View diagnostics"),()=>{location.hash="diagnostics";loadLogs();});
  }
  if(!accountReportFresh()){
    add("verification",uiText("Cần xác minh tài khoản","Account verification needed"),uiText("Một lần kiểm tra sẽ nhận diện đăng nhập và kiểm tra kết nối của cả hai trình duyệt.","One check detects sign-in and checks both browser connections."),t("checkAllAccounts"),check);
    return issues;
  }
  const report=lastAccountCheck;
  (report.checks||[]).filter(c=>c.state!=="ok"&&c.state!=="skipped").forEach(c=>{
    const worker=c.id.startsWith("worker");
    const side=worker?uiText("trình duyệt tác vụ","task browser"):uiText("trình duyệt bạn dùng","your browser");
    const reason=c.reason_code;
    const openLogin=()=>{location.hash="accounts";$("browser-account-select").value=(lastStatus.browser_accounts||{}).active_account_id;openBrowserAccountLogin($("account-check-button"));};
    if(reason==="SIGNED_OUT"||reason==="WORKER_NOT_RUNNING"){
      add(worker?"worker-login":"main-login",uiText("Cần đăng nhập: ","Sign-in needed: ")+side,worker?uiText("Mở trình duyệt tác vụ và đăng nhập cùng tài khoản bạn đang dùng.","Open the task browser and sign in to your account."):uiText("Đăng nhập ChatGPT trên trình duyệt bạn dùng, sau đó kiểm tra lại.","Sign in to ChatGPT in your browser, then recheck."),worker?t("openWorkerLogin"):t("navAccounts"),worker?openLogin:accountHelp);
    }else if(reason==="ACCOUNTS_DIFFER"){
      add("alignment",uiText("Hai trình duyệt đang dùng tài khoản khác nhau","Browsers use different accounts"),uiText("Chuyển trình duyệt tác vụ sang tài khoản bạn dùng, hoặc đăng nhập lại cùng tài khoản.","Switch the task browser to your account, or sign in to the same account."),t("syncWorkerWithMain"),()=>{location.hash="accounts";syncWorkerWithMain($("sync-worker-btn"));});
    }else if(reason==="CDP_AUTH_REQUIRED"){
      add("main-permission",uiText("Cần bạn cho phép kết nối trình duyệt","Browser connection permission needed"),uiText("Trên Edge bạn dùng, bấm “Allow remote debugging” khi được hỏi, rồi kiểm tra lại.","In your Edge browser, allow remote debugging when prompted, then recheck."),t("checkAllAccounts"),check);
    }else if(reason==="CONFIG_MISMATCH"){
      add("config",uiText("Cấu hình trình duyệt tác vụ chưa khớp","Task browser configuration mismatch"),uiText("Xem chi tiết kỹ thuật trong mục Tài khoản để kiểm tra đường dẫn hồ sơ.","Review technical details in Accounts to check the profile path."),uiText("Xem chi tiết","View details"),()=>{location.hash="accounts";const d=$("browser-account-config").closest("details");d.open=true;d.scrollIntoView({block:"nearest"});});
    }else if(reason==="ORCH_UNAVAILABLE"){
      add("orchestrator",uiText("Bộ chạy tác vụ chưa sẵn sàng","Task runner not ready"),uiText("Kiểm tra trạng thái dịch vụ trong mục Hệ thống.","Review service status in System."),t("navServices"),()=>{location.hash="services";});
    }else if(reason==="WORKER_BUSY"){
      add("busy",uiText("Đang có tác vụ chạy","A task is running"),uiText("Chờ tác vụ hiện tại hoàn tất rồi kiểm tra lại.","Wait for the current task to finish, then recheck."),t("navServices"),()=>{location.hash="services";});
    }else{
      add(worker?"worker-check":c.id==="alignment"?"alignment":"main-check",uiText("Chưa xác minh: ","Not verified: ")+(c.id==="alignment"?uiText("tài khoản trùng khớp","account matching"):side),reason==="NO_CHATGPT_TAB"?uiText("Mở một tab ChatGPT đã đăng nhập trên trình duyệt này rồi kiểm tra lại.","Open a signed-in ChatGPT tab in this browser, then recheck."):uiText("Kiểm tra trình duyệt đang mở và đã đăng nhập, rồi kiểm tra lại.","Check that the browser is open and signed in, then recheck."),worker?t("openWorkerLogin"):t("checkAllAccounts"),worker?openLogin:check);
    }
  });
  return issues;
}
function renderSystemSummary(){
  if(!$("system-summary-title"))return;
  const title=$("system-summary-title"),note=$("system-summary-note"),list=$("system-issues");
  list.replaceChildren();
  const issues=collectSystemIssues();
  if(globalBusy){title.textContent=uiText("Đang xử lý…","Working…");title.className="warn";note.textContent=uiText("Vui lòng chờ thao tác hiện tại hoàn tất.","Please wait for the current operation.");}
  else if(!lastStatus&&!statusConnectionError){title.textContent=t("loading");title.className="warn";note.textContent="";}
  else if(issues.length){title.textContent=uiText("Cần xử lý ","Needs attention: ")+issues.length+uiText(" vấn đề", " issue(s)");title.className="warn";note.textContent=uiText("Làm theo hướng dẫn bên dưới rồi kiểm tra lại.","Follow the steps below, then recheck.");}
  else if(accountReportFresh()&&lastAccountCheck.overall==="ready"){
    title.textContent=uiText("Có thể bắt đầu","Ready to begin");title.className="ok";note.textContent=t("preflightHint");
  }else{title.textContent=uiText("Cần kiểm tra thêm","More checks needed");title.className="warn";note.textContent=t("preflightHint");}
  $("last-update").textContent=lastUpdatedAt?uiText("Cập nhật lúc ","Updated at ")+new Date(lastUpdatedAt).toLocaleTimeString(currentLang==="vi"?"vi-VN":"en-US"):"";
  issues.forEach(issue=>{
    const row=document.createElement("div");row.className="system-issue";
    const copy=document.createElement("div"),heading=document.createElement("strong"),help=document.createElement("p"),button=document.createElement("button");
    heading.textContent=issue.title;help.textContent=issue.help;copy.append(heading,help);
    button.textContent=issue.label;button.className="secondary";button.disabled=globalBusy;button.onclick=issue.run;
    row.append(copy,button);list.appendChild(row);
  });
}


let historyUploading=false;
function archiveFileSelected(){
  const file=$("history-zip-file").files[0];
  $("history-state").textContent=file ? file.name+" ("+(file.size/1024/1024).toFixed(1)+" MB)" : t("historySelect");
}
function historyRender(s){
  if(!s)return;
  const phase=s.phase||"idle";
  const labels={idle:t("historySelect"),uploading:t("historyReceiving"),processing:t("historyProcessing"),complete:t("historyComplete"),error:t("historyError")};
  if(!historyUploading)$("history-state").textContent=labels[phase]||phase;
  if(phase==="processing"){
    $("history-progress").hidden=false;$("history-progress").removeAttribute("value");
  }else if(phase==="complete"){
    $("history-progress").hidden=false;$("history-progress").value=100;
  }else if(phase==="idle"){
    $("history-progress").hidden=true;
  }
  const result=$("history-result");
  if(phase==="complete" && s.after){
    const n=s.after;
    result.textContent=t("historyNewChats")+": "+s.added_chats+" · "+t("historyNewMessages")+": "+s.added_messages+" · "+t("historyStored")+": "+n.captured_chats+" / "+n.messages;
  }else if(phase==="error"){
    result.textContent=s.error||s.message||t("historyError");
  }else if(phase==="processing"){
    result.textContent=t("historyWait");
  }
}
async function historyPoll(){
  if(historyUploading)return;
  try {historyRender(await api("/api/history-import/status"));}
  catch(e){$("history-result").textContent=t("historyStatusFail")+": "+e.message;}
}
function uploadHistory(button){
  if(historyUploading||globalBusy)return;
  const file=$("history-zip-file").files[0];
  if(!file){notifyUser(t("historySelect"));return;}
  if(!file.name.toLowerCase().endsWith(".zip") || !file.size || file.size>2147483648){
    notifyUser(t("historyInvalid"));return;
  }
  if(!confirm(t("historyConfirm")))return;
  historyUploading=true;setBusy(true,button);
  const bar=$("history-progress");bar.hidden=false;bar.value=0;
  $("history-result").textContent="";
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/api/history-import/upload");
  xhr.setRequestHeader("X-Control-Center-Token",TOKEN);
  xhr.setRequestHeader("X-Archive-Name",encodeURIComponent(file.name));
  xhr.setRequestHeader("Content-Type","application/zip");
  xhr.upload.onprogress=(e)=>{
    if(e.lengthComputable){bar.value=Math.min(99,Math.round(100*e.loaded/e.total));
    $("history-state").textContent=t("historyReceiving")+" "+bar.value+"%";}
  };
  const done=()=>{historyUploading=false;setBusy(false);historyPoll();};
  xhr.onload=()=>{
    let data={};try{data=JSON.parse(xhr.responseText||"{}");}catch(_e){}
    if(xhr.status!==202){notifyUser(t("historyError")+": "+(data.error||("HTTP "+xhr.status)));}
    else {historyRender(data.result);notifyUser(t("historyProcessing"));}
    done();
  };
  xhr.onerror=()=>{notifyUser(t("historyNetworkFail"));done();};
  xhr.send(file);
}


let lastFunnelStatus=null;
function renderFunnel(){
  const f=lastFunnelStatus||(lastStatus&&lastStatus.funnel)||{};
  const known=typeof f.enabled==="boolean";
  $("funnel-state").textContent=!known?uiText("Chưa xác minh","Not verified"):f.enabled?uiText("Đang bật","Enabled"):uiText("Đang tắt","Disabled");
  $("funnel-state").className=known?(f.enabled?"ok":"warn"):"warn";
  $("funnel-gateway").textContent=f.gateway_ready?t("healthy"):t("unavailable");
  $("funnel-gateway").className=f.gateway_ready?"ok":"bad";
  const publicLabels={public_ready:["HTTPS phản hồi tốt","HTTPS responding"],ipv4_ready_ipv6_unverified:["IPv4 hoạt động; IPv6 chưa xác minh","IPv4 working; IPv6 unverified"]};
  $("funnel-public").textContent=(publicLabels[f.public_state]||["Chưa có kết quả mới xác nhận HTTPS","No recent HTTPS confirmation"])[currentLang==="vi"?0:1]+(f.public_checked_at?" · "+new Date(f.public_checked_at*1000).toLocaleString(currentLang==="vi"?"vi-VN":"en-US"):"");
  $("funnel-error").textContent=f.error?uiText("Không đọc được trạng thái Tailscale. Kiểm tra ứng dụng Tailscale đang chạy.","Unable to read Tailscale status. Check that Tailscale is running."):known&&!f.config_matches?uiText("Cổng 443 có cấu hình khác. Điều khiển đã khóa để tránh ghi đè.","Port 443 has a different configuration. Controls are locked to avoid overwriting it."):"";
  ["actuator","orchestrator"].forEach(service=>{
    const route=$("funnel-"+service+"-state");
    route.textContent=!known?uiText("Chưa xác minh","Not verified"):!f.enabled?uiText("Funnel đã tắt","Funnel disabled"):f.config_matches&&f.gateway_ready?uiText("Đã mở tuyến HTTPS","HTTPS route configured"):uiText("Cần kiểm tra Gateway","Gateway needs attention");
    route.className=f.enabled&&f.config_matches&&f.gateway_ready?"ok":"warn";
    if(service==="orchestrator")$("funnel-orchestrator-url").value=f.orchestrator_url||"";
    const listening=f[service+"_backend_listening"];
    $("funnel-"+service+"-backend").textContent=listening?uiText("Đang nhận kết nối","Accepting connections"):uiText("Chưa nhận kết nối","Not accepting connections");
    $("funnel-"+service+"-backend").className=listening?"ok":"warn";
  });
  $("funnel-actuator-full-url").value=f.actuator_full_url||"";
  $("funnel-enable").disabled=globalBusy||!known||!f.config_matches||!f.gateway_ready||f.enabled;
  $("funnel-disable").disabled=globalBusy||!known||!f.config_matches||!f.enabled;
  $("funnel-checked").textContent=f.checked_at?uiText("Trạng thái Funnel kiểm tra lúc: ","Funnel status checked at: ")+new Date(f.checked_at*1000).toLocaleString(currentLang==="vi"?"vi-VN":"en-US"):"";
  $("funnel-last-action").textContent=f.last_action?uiText("Lần điều khiển gần nhất: ","Last control action: ")+(f.last_action.action==="enable"?t("funnelEnable"):t("funnelDisable"))+" · "+new Date(f.last_action.at*1000).toLocaleString(currentLang==="vi"?"vi-VN":"en-US"):"";
}
async function refreshFunnel(button=null){
  if(globalBusy)return;
  setBusy(true,button);
  try{lastFunnelStatus=await api("/api/funnel/status?fresh=1");renderFunnel();}
  catch(e){notifyUser(t("statusError")+": "+e.message);}
  finally{setBusy(false);}
}
async function funnelAction(action,button=null){
  const warning=action==="disable"?uiText("Tắt Funnel sẽ ngắt kết nối công khai của cả Actuator và Orchestrator. Tiếp tục?","Disable Funnel and interrupt public access to both Actuator and Orchestrator?"):uiText("Bật Funnel HTTPS cho cả Actuator và Orchestrator qua OAuth Gateway hiện tại?","Enable HTTPS Funnel for both services through the existing OAuth Gateway?");
  if(!confirm(warning))return;
  setBusy(true,button);
  const label=t(action==="enable"?"funnelEnable":"funnelDisable");
  notifyUser(label+" "+t("inProgress"));
  try{
    const r=await api("/api/funnel/"+action,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    lastFunnelStatus=r.funnel;renderFunnel();notifyUser(label+" "+t("completed"));
  }catch(e){notifyUser(label+" "+t("failed")+": "+e.message);}
  finally{setBusy(false);}
}
async function copyFunnelUrl(id){
  const input=$(id);
  if(!input.value)return;
  try{await navigator.clipboard.writeText(input.value);notifyUser(uiText("Đã sao chép URL.","URL copied."));}
  catch(e){input.focus();input.select();notifyUser(uiText("URL đã được chọn. Nhấn Ctrl+C để sao chép.","URL selected. Press Ctrl+C to copy."));}
}
function selectView(){
  const selected=["services","accounts","context-transfer","funnel","diagnostics"].includes(location.hash.slice(1))?location.hash.slice(1):"services";
  ["services","accounts","context-transfer","funnel","diagnostics"].forEach(id=>{
    $(id).hidden=id!==selected;
    const link=document.querySelector('.quick-nav a[href="#'+id+'"]');
    if(id===selected) link.setAttribute("aria-current","page");
    else link.removeAttribute("aria-current");
  });
  $("logs-panel").hidden=selected!=="diagnostics";
}
window.addEventListener("hashchange",selectView);
selectView();
applyLanguage();
refresh();
setInterval(refresh,5000);
historyPoll();
setInterval(historyPoll,3000);

let contextTransferJob=null;
async function contextTransferStart(button){
  if(contextTransferJob && ["checking_account","preparing","sending","generating","saving"].includes(contextTransferJob.phase)) return;
  const msg=uiText(
    "Gửi prompt tổng hợp sang CHATGPT đang đăng nhập? Chỉ chạy khi Main và Worker là cùng tài khoản. Tác vụ không tự xóa hay chuyển hội thoại. Tiếp tục?",
    "Send a context-summary prompt to the signed-in ChatGPT account? Requires Main = Worker. Continue?");
  if(!confirm(msg)) return;
  button.disabled=true;
  try{
    const result=await api("/api/context-transfer/start",{
      method:"POST",headers:{"Content-Type":"application/json"},body:"{}"
    });
    contextTransferJob=result.result||null;
    contextTransferRender();
  }catch(e){
    $("context-status").textContent=uiText("Không thể chạy: ","Failed: ")+e.message;
    button.disabled=false;
  }
}
function contextTransferRender(){
  const j=contextTransferJob||{phase:"idle",message:uiText("Chưa chạy","Not started")};
  const busy=["checking_account","preparing","sending","generating","saving"].includes(j.phase);
  $("context-start").disabled=busy;
  $("context-download-md").disabled=j.phase!=="complete";
  $("context-download-json").disabled=j.phase!=="complete";
  const detail=j.account?(" ["+j.account+"]"):"";
  $("context-status").textContent=(j.phase||"idle")+detail+": "+(j.message||"");
}
async function contextTransferPoll(){
  try{contextTransferJob=await api("/api/context-transfer/status");contextTransferRender();}
  catch(e){$("context-status").textContent=uiText("Không đọc được trạng thái: ","Status unavailable: ")+e.message;}
}
async function contextTransferDownload(format){
  const j=contextTransferJob;
  if(!j || j.phase!=="complete" || !j.run_id)return;
  try{
    const query=new URLSearchParams({run_id:j.run_id,format});
    const response=await fetch("/api/context-transfer/download?"+query,{
      headers:{"X-Control-Center-Token":TOKEN,"Accept":"application/octet-stream"}
    });
    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.error||"HTTP "+response.status);}
    const data=await response.blob();
    if(!data.size)throw new Error("File trống");
    const url=URL.createObjectURL(data);
    const a=document.createElement("a");a.href=url;
    a.download="AI-Context-Transfer-"+j.run_id+"."+format;
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(e){$("context-status").textContent=uiText("Tải file thất bại: ","Download failed: ")+e.message;}
}
contextTransferPoll();
setInterval(contextTransferPoll,3000);
(function installEmbeddedLayout(){
  if(window.self===window.top)return;
  document.documentElement.classList.add('embedded');
  const sendHeight=()=>{
    const h=Math.max(document.body.scrollHeight,document.documentElement.scrollHeight);
    window.parent.postMessage({type:'actuator-control-height',height:h},'http://127.0.0.1:8765');
  };
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(sendHeight).observe(document.body);
  window.addEventListener('load',sendHeight);
  requestAnimationFrame(sendHeight);
})();
</script>
</body>
</html>"""


def _http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _read_json_url(url: str, timeout: float = 1.5) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def _dom_broker_call(action: str, args: dict, timeout: float = 45.0) -> dict:
    if not DOM_BROKER_TOKEN_FILE.is_file():
        raise RuntimeError("DOM broker token file is unavailable.")
    token = DOM_BROKER_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("DOM broker token is empty.")
    payload = json.dumps(
        {"action": action, "args": args},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        DOM_BROKER_URL,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Actuator-DOM-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            message = str(body.get("error") or body)
        except Exception:
            message = str(exc)
        raise RuntimeError(f"DOM broker {action} failed: {message}") from exc
    except Exception as exc:
        raise RuntimeError(f"DOM broker {action} failed: {exc}") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError(
            f"DOM broker {action} failed: "
            f"{body.get('error', 'invalid response') if isinstance(body, dict) else 'invalid response'}"
        )
    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"DOM broker {action} returned an invalid result.")
    return result


def _dom_find_first(tab_ref: str, selectors: list[str]) -> str | None:
    for selector in selectors:
        result = _dom_broker_call(
            "find",
            {
                "tab_ref": tab_ref,
                "selector": selector,
                "max_items": 5,
            },
        )
        for item in result.get("matches", []):
            if isinstance(item, dict) and item.get("element_ref"):
                return str(item["element_ref"])
    return None


def _transfer_profile_to_chat(account_id: str) -> dict:
    selected = str(account_id or "").strip()
    browser_status = BROWSER_ACCOUNTS.status()
    if not selected:
        selected = str(browser_status.get("active_account_id") or "").strip()
    if not selected:
        raise RuntimeError("No destination browser account is selected.")

    if selected != str(browser_status.get("active_account_id") or ""):
        _ensure_browser_switch_idle()
        BROWSER_ACCOUNTS.switch_account(selected, launch_background=True)

    profile_status = ACCOUNT_BOOTSTRAP.export()
    prompt = ACCOUNT_BOOTSTRAP.transfer_prompt()

    opened = _dom_broker_call(
        "open_tab",
        {"url": "https://chatgpt.com/", "timeout_ms": 30000},
    )
    tab_ref = str(opened.get("tab_ref") or "")
    if not tab_ref:
        raise RuntimeError("DOM broker did not return a ChatGPT tab reference.")

    composer = None
    composer_selectors = [
        "#prompt-textarea",
        '[data-testid="composer-input"]',
        'div[contenteditable="true"]',
        "textarea",
    ]
    for _ in range(24):
        composer = _dom_find_first(tab_ref, composer_selectors)
        if composer:
            break
        time.sleep(0.35)

    if not composer:
        login = BROWSER_ACCOUNTS.open_login(selected)
        return {
            "ok": False,
            "requires_login": True,
            "account_id": selected,
            "login_opened": True,
            "login": login.get("launch"),
            "message": (
                "Destination account is not ready for ChatGPT composer access. "
                "A visible Worker sign-in window was opened; sign in and retry."
            ),
            "profile": profile_status,
        }

    _dom_broker_call(
        "set_value",
        {
            "element_ref": composer,
            "value": prompt,
            "timeout_ms": 30000,
        },
    )
    verify = _dom_broker_call(
        "read_text",
        {
            "element_ref": composer,
            "max_chars": min(max(len(prompt) + 128, 2048), 60000),
        },
    )
    if TRANSFER_MARKER not in str(verify.get("text") or ""):
        raise RuntimeError("Working-context prompt was not verified in the ChatGPT composer.")

    send_button = None
    send_selectors = [
        '[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send"]',
        'button[type="submit"]',
    ]
    for _ in range(12):
        send_button = _dom_find_first(tab_ref, send_selectors)
        if send_button:
            break
        time.sleep(0.25)
    if not send_button:
        raise RuntimeError("ChatGPT Send button was not found after filling the composer.")

    _dom_broker_call(
        "click",
        {
            "element_ref": send_button,
            "timeout_ms": 30000,
        },
    )

    delivered = False
    for _ in range(30):
        time.sleep(0.25)
        composer_cleared = False
        try:
            after = _dom_broker_call(
                "read_text",
                {
                    "element_ref": composer,
                    "max_chars": 2048,
                },
                timeout=10.0,
            )
            composer_cleared = TRANSFER_MARKER not in str(after.get("text") or "")
        except Exception:
            composer_cleared = True

        rendered = _dom_broker_call(
            "find",
            {
                "tab_ref": tab_ref,
                "text": TRANSFER_MARKER,
                "exact": False,
                "max_items": 5,
            },
            timeout=10.0,
        )
        if composer_cleared and int(rendered.get("count") or 0) > 0:
            delivered = True
            break

    if not delivered:
        raise RuntimeError(
            "The working-context prompt was filled and Send was activated, "
            "but message delivery could not be verified."
        )

    conversation_url = str(opened.get("url") or "https://chatgpt.com/")
    try:
        tabs = _dom_broker_call("list_tabs", {}, timeout=10.0)
        for item in tabs.get("tabs", []):
            if isinstance(item, dict) and item.get("tab_ref") == tab_ref:
                conversation_url = str(item.get("url") or conversation_url)
                break
    except Exception:
        pass

    return {
        "ok": True,
        "requires_login": False,
        "account_id": selected,
        "conversation_url": conversation_url,
        "transfer_marker": TRANSFER_MARKER,
        "profile": profile_status,
        "message": (
            "Created a new destination ChatGPT conversation and sent the portable "
            "identity/workflow/project context. Official ChatGPT Memory was not copied."
        ),
    }


def _powershell_command_unlocked(action: str, *, json_output: bool = False) -> subprocess.CompletedProcess[str]:
    if action not in {"Status", "Start", "Stop", "Restart"}:
        raise ValueError("Unsupported tunnel action.")
    if not TUNNEL_MANAGER.is_file():
        raise FileNotFoundError(f"Tunnel manager not found: {TUNNEL_MANAGER}")
    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(TUNNEL_MANAGER),
        "-Action", action,
        "-ProfileName", TUNNEL_PROFILE,
        "-HealthUrl", MCP_HEALTH,
        "-AdminHealthUrl", ADMIN_HEALTH,
    ]
    if json_output:
        args.append("-Json")
    return subprocess.run(
        args,
        cwd=str(PROJECT_ROOT),
        text=True,
        stdout=subprocess.PIPE if action=="Status" else subprocess.DEVNULL,
        stderr=subprocess.PIPE if action=="Status" else subprocess.DEVNULL,
        timeout=35,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


_node_sessions=None
_node_sessions_lock=RLock()
def _powershell_command(action: str, *, json_output: bool = False):
    global _node_sessions
    if action=='Status':return _powershell_command_unlocked(action,json_output=json_output)
    from .work_sessions import WorkSessions
    from .config import default_config_path
    with _node_sessions_lock:
        if _node_sessions is None:_node_sessions=WorkSessions(default_config_path().parent/'work-sessions.sqlite3')
    lease=_node_sessions.acquire_operation(None,'node.'+action.casefold(),exclusive=True)
    try:
        result=_powershell_command_unlocked(action,json_output=json_output)
        result.stdout=result.stdout or ''
        result.stderr=result.stderr or ''
        return result
    finally:_node_sessions.release_operation(lease)


def _tunnel_status() -> dict:
    result = _powershell_command("Status", json_output=True)
    if result.returncode != 0:
        return {
            "profile": TUNNEL_PROFILE,
            "running": False,
            "pids": [],
            "health_ok": _http_ok(MCP_HEALTH),
            "admin_ok": _http_ok(ADMIN_HEALTH),
            "error": (result.stderr or result.stdout).strip(),
        }
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {
            "profile": TUNNEL_PROFILE,
            "running": False,
            "pids": [],
            "health_ok": _http_ok(MCP_HEALTH),
            "admin_ok": _http_ok(ADMIN_HEALTH),
            "error": "Unable to parse tunnel status.",
        }


def _uptime_human() -> str:
    seconds = max(0, int(time.time() - STARTED_AT))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def status() -> dict:
    admin = _read_json_url('http://127.0.0.1:8766/healthz')
    from .automation_health import snapshot as automation_snapshot
    from .operations_status import snapshot as operations_snapshot
    return {
        "tunnel": _tunnel_status(),
        "actuator": {
            "version": admin.get("runtime_version", "unknown"),
            "admin_health_url": ADMIN_HEALTH,
            "mcp_health_url": MCP_HEALTH,
        },
        "orchestrator": ORCHESTRATOR.status(),
        "account": ACCOUNT_BOOTSTRAP.status(),
        "browser_accounts": BROWSER_ACCOUNTS.status(),
        "funnel": FUNNEL.status(),
        "automation": automation_snapshot(PROJECT_ROOT),
        "operations": operations_snapshot(),
        "control_center": {
            "host": HOST,
            "port": PORT,
            "pid": os.getpid(),
            "uptime_seconds": max(0, int(time.time() - STARTED_AT)),
            "uptime_human": _uptime_human(),
        },
    }


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return "(log file not found)"
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception as exc:
        return f"(unable to read log: {exc})"


def _ensure_browser_switch_idle() -> None:
    orch = ORCHESTRATOR.status()
    jobs = int(orch.get("active_job_count") or 0)
    tasks = int(orch.get("active_task_count") or 0)
    if jobs or tasks:
        raise RuntimeError(
            "Cannot switch ChatGPT account while Orchestrator has an active job/task. "
            "Wait for it to finish, then retry."
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatGPT-Actuator-Control/1.0"

    def log_message(self, format: str, *args) -> None:
        return

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Control-Center-Token", ""), TOKEN)

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0 or length > 65536:
            raise ValueError("Request body is too large.")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid UTF-8 JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request JSON must be an object.")
        return value

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header."})
            return
        parsed = urlparse(self.path)
        if parsed.path == '/automation':
            from .automation_dashboard import HTML
            data = HTML.replace('__TOKEN__', json.dumps(TOKEN)).encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if parsed.path == '/agents':
            from .agents.dashboard import HTML
            data = HTML.replace('__TOKEN__', json.dumps(TOKEN)).encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if parsed.path == '/tasks':
            from .task_dashboard import HTML
            data=HTML.replace('__TOKEN__',json.dumps(TOKEN)).encode('utf-8')
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if parsed.path == "/":
            data = _HTML.replace("__TOKEN__", TOKEN).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok", "pid": os.getpid()})
            return
        if not self._token_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid control-center token."})
            return
        if parsed.path == '/api/agents':
            from .agents.dashboard import snapshot
            try:
                self._json(HTTPStatus.OK, snapshot(PROJECT_ROOT / 'config/missions.sqlite3'))
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Không đọc được hàng đợi nhiệm vụ.'})
            return
        if parsed.path == '/api/agents/health':
            from .agents.diagnostics import collect
            try:
                self._json(HTTPStatus.OK, collect(PROJECT_ROOT))
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Health diagnostics unavailable'})
            return
        if parsed.path == '/api/agents/detail':
            from .agents.dashboard import detail
            try:
                self._json(HTTPStatus.OK, detail(PROJECT_ROOT / 'config/missions.sqlite3',
                    parse_qs(parsed.query).get('id', [''])[0]))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {'error': 'Không đọc được nhiệm vụ; hãy làm mới.'})
            return
        if parsed.path.startswith('/api/automation/'):
            from .automation_dashboard import AutomationDashboard, DashboardError
            try:
                view = AutomationDashboard(PROJECT_ROOT)
                query = parse_qs(parsed.query)
                sid = query.get('session', [''])[0]
                if parsed.path == '/api/automation/sessions': result = {'sessions': view.sessions()}
                elif parsed.path == '/api/automation/operations': result = view.inspect(sid)
                elif parsed.path == '/api/automation/reviews': result = view.reviews(sid, query.get('task', [None])[0])
                elif parsed.path == '/api/automation/browser-default': result = view.browser_default(sid)
                elif parsed.path == '/api/automation/browsers': result = view.browsers(sid, query.get('endpoint'))
                elif parsed.path == '/api/automation/browser-choices': result = view.browser_choices(sid)
                else: self._json(HTTPStatus.NOT_FOUND, {'error': 'Not found'}); return
                self._json(HTTPStatus.OK, result)
            except DashboardError as exc:
                self._json(HTTPStatus.BAD_REQUEST, exc.response())
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, DashboardError('INVALID_REQUEST').response())
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {'error': 'Không đọc được phiên; hãy làm mới và kiểm tra backend.'})
            return
        if parsed.path in ('/api/tasks','/api/task'):
            from .task_dashboard import TaskDashboard
            try:
                view=TaskDashboard(PROJECT_ROOT/'config/work-sessions.sqlite3')
                data=view.list() if parsed.path=='/api/tasks' else view.detail(parse_qs(parsed.query).get('id',[''])[0])
                self._json(HTTPStatus.OK,data)
            except (ValueError,RuntimeError) as exc:self._json(HTTPStatus.BAD_REQUEST,{'error':str(exc)})
            return
        if parsed.path == "/api/context-transfer/status":
            self._json(HTTPStatus.OK, CONTEXT_TRANSFER.status())
            return
        if parsed.path == "/api/context-transfer/download":
            try:
                params = parse_qs(parsed.query)
                run_id = str(params.get("run_id", [""])[0])
                fmt = str(params.get("format", [""])[0])
                data, filename, mime = CONTEXT_TRANSFER.download(run_id, fmt)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except (ValueError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/api/history-import/status":
            self._json(HTTPStatus.OK, HISTORY_IMPORT.snapshot())
            return

        if parsed.path == "/api/funnel/status":
            self._json(HTTPStatus.OK, FUNNEL.status(force=parse_qs(parsed.query).get("fresh") == ["1"]))
            return

        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, status())
            return
        if parsed.path == "/api/browser-accounts":
            try:
                self._json(HTTPStatus.OK, BROWSER_ACCOUNTS.status())
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/api/account/bootstrap":
            try:
                self._json(HTTPStatus.OK, {
                    "text": ACCOUNT_BOOTSTRAP.transfer_prompt(),
                    "status": ACCOUNT_BOOTSTRAP.status(),
                })
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if parsed.path == "/api/logs":
            try:
                lines = int(parse_qs(parsed.query).get("lines", ["80"])[0])
            except ValueError:
                lines = 80
            lines = max(1, min(lines, 500))
            logs = PROJECT_ROOT / "logs"
            self._json(HTTPStatus.OK, {
                "stdout": _tail(logs / "tunnel.stdout.log", lines),
                "stderr": _tail(logs / "tunnel.stderr.log", lines),
            })
            return
        if parsed.path == "/api/orchestrator/logs":
            try:
                lines = int(parse_qs(parsed.query).get("lines", ["80"])[0])
            except ValueError:
                lines = 80
            self._json(HTTPStatus.OK, ORCHESTRATOR.logs(lines))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header."})
            return
        if not self._token_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid control-center token."})
            return
        parsed = urlparse(self.path)
        if parsed.path == '/api/agents/backup':
            origin = self.headers.get('Origin')
            if (origin and origin != 'http://' + self.headers.get('Host', '')) or self.headers.get(
                    'Sec-Fetch-Site') == 'cross-site' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self._json(HTTPStatus.FORBIDDEN, {'error': 'Invalid backup request'})
                return
            try:
                from .agents.backups import AgentBackups
                if self._body_json() != {'action': 'create_and_drill'}:
                    raise ValueError('Invalid backup action')
                backups = AgentBackups(PROJECT_ROOT)
                snapshot = backups.create()
                self._json(HTTPStatus.OK, {'snapshot': snapshot, 'drill': backups.drill(snapshot['id'])})
            except Exception:
                self._json(HTTPStatus.CONFLICT, {'error': 'Backup or isolated restore verification failed'})
            return
        if parsed.path == '/api/agents/control':
            from .agents.dashboard import control
            origin = self.headers.get('Origin')
            if (origin and origin != 'http://' + self.headers.get('Host', '')) or self.headers.get(
                    'Sec-Fetch-Site') == 'cross-site' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self._json(HTTPStatus.FORBIDDEN, {'error': 'Yêu cầu điều khiển không hợp lệ.'})
                return
            try:
                self._json(HTTPStatus.OK, control(PROJECT_ROOT / 'config/missions.sqlite3', self._body_json()))
            except Exception:
                self._json(HTTPStatus.CONFLICT, {'error': 'Trạng thái đã đổi hoặc thao tác bị từ chối; hãy làm mới.'})
            return
        if parsed.path in ('/api/automation/control', '/api/automation/resume', '/api/automation/reset-checkpoint', '/api/automation/browser'):
            from .automation_dashboard import AutomationDashboard, DashboardError
            try:
                payload = self._body_json(); view = AutomationDashboard(PROJECT_ROOT)
                if parsed.path.endswith('/browser'):
                    if set(payload)!={'session_id','action','selection'}:raise ValueError('Explicit browser fields required')
                    result=view.browser_change(payload['session_id'],payload['action'],payload['selection'])
                elif parsed.path.endswith('/control'):
                    if set(payload) - {'session_id', 'action', 'operation_id', 'acknowledge_close'}: raise ValueError('Unexpected fields')
                    result = view.control(payload.get('session_id'), payload.get('action'), payload.get('operation_id'), payload.get('acknowledge_close', False))
                elif parsed.path.endswith('/reset-checkpoint'):
                    if set(payload) != {'session_id','task_key','item_key','expected_revision','acknowledge_inspected'}: raise ValueError('Checkpoint fields required')
                    result = view.reset_checkpoint(payload['session_id'],payload['task_key'],payload['item_key'],payload['expected_revision'],payload['acknowledge_inspected'])
                else:
                    if set(payload) != {'session_id', 'review'} or not isinstance(payload['review'], dict): raise ValueError('Review required')
                    result = view.resume(payload['session_id'], payload['review'])
                self._json(HTTPStatus.OK, result)
            except DashboardError as exc:
                self._json(HTTPStatus.BAD_REQUEST, exc.response())
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, DashboardError('INVALID_REQUEST').response())
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {'error': 'Yêu cầu bị từ chối; làm mới trạng thái. Khôi phục cần hủy trước và xác nhận đóng tab quản lý.'})
            return
        if parsed.path == "/api/context-transfer/start":
            try:
                self._body_json()
                _ensure_browser_switch_idle()
                result = CONTEXT_TRANSFER.start(
                    call=_dom_broker_call,
                    detect_identity=lambda: BROWSER_ACCOUNTS.detect_identities("both"),
                )
                self._json(HTTPStatus.ACCEPTED, {"ok": True, "result": result})
            except (ValueError, RuntimeError) as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        if parsed.path in {"/api/funnel/enable", "/api/funnel/disable"}:
            try:
                self._body_json()
                result=FUNNEL.change(parsed.path.rsplit("/",1)[-1])
                self._json(HTTPStatus.OK, {"ok": True, "funnel": result})
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if parsed.path == "/api/history-import/upload":
            try:
                result = HISTORY_IMPORT.upload(
                    self.rfile, self.headers.get("Content-Length", ""),
                    self.headers.get("X-Archive-Name", ""))
                self._json(HTTPStatus.ACCEPTED, {"ok": True, "result": result})
            except ArchiveImportBusy as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ArchiveImportError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Upload failed."})
            return

        if parsed.path=='/api/tasks/cancel':
            from .task_dashboard import TaskDashboard
            try:self._json(HTTPStatus.OK,TaskDashboard(PROJECT_ROOT/'config/work-sessions.sqlite3').cancel(str(self._body_json().get('attempt_id',''))))
            except (ValueError,RuntimeError) as exc:self._json(HTTPStatus.BAD_REQUEST,{'error':str(exc)})
            return
        if parsed.path in {
            "/api/browser-accounts/create",
            "/api/browser-accounts/switch",
            "/api/browser-accounts/login",
            "/api/browser-accounts/confirm-main",
            "/api/browser-accounts/sync-to-main",
            "/api/browser-accounts/detect",
            "/api/browser-accounts/check-all",
        }:
            try:
                payload = self._body_json()
                if parsed.path == "/api/browser-accounts/check-all":
                    mode = str(payload.get("mode") or "same")
                    result = BROWSER_ACCOUNTS.check_all(
                        mode=mode, orchestrator=ORCHESTRATOR.status()
                    )
                elif parsed.path == "/api/browser-accounts/create":
                    result = BROWSER_ACCOUNTS.create_account(str(payload.get("label") or ""))
                elif parsed.path == "/api/browser-accounts/detect":
                    scope = str(payload.get("scope") or "both")
                    result = BROWSER_ACCOUNTS.detect_identities(scope)
                elif parsed.path == "/api/browser-accounts/sync-to-main":
                    _ensure_browser_switch_idle()
                    result = BROWSER_ACCOUNTS.sync_worker_to_main()
                else:
                    account_id = str(payload.get("account_id") or "").strip()
                    if not account_id:
                        raise ValueError("account_id is required.")
                    if parsed.path == "/api/browser-accounts/confirm-main":
                        result = BROWSER_ACCOUNTS.confirm_main_account(account_id)
                    else:
                        _ensure_browser_switch_idle()
                        if parsed.path == "/api/browser-accounts/switch":
                            result = BROWSER_ACCOUNTS.switch_account(account_id, launch_background=True)
                        else:
                            result = BROWSER_ACCOUNTS.open_login(account_id)
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "result": result,
                    "status": status(),
                })
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if parsed.path in {"/api/account/export", "/api/account/setup", "/api/account/transfer"}:
            try:
                if parsed.path == "/api/account/export":
                    self._body_json()
                    result = ACCOUNT_BOOTSTRAP.export()
                elif parsed.path == "/api/account/setup":
                    self._body_json()
                    result = ACCOUNT_BOOTSTRAP.prepare_destination()
                else:
                    payload = self._body_json()
                    result = _transfer_profile_to_chat(
                        str(payload.get("account_id") or "")
                    )
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "result": result,
                    "status": status(),
                })
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        orchestrator_actions = {
            "/api/orchestrator/start": "Start",
            "/api/orchestrator/stop": "Stop",
            "/api/orchestrator/restart": "Restart",
        }
        if parsed.path in orchestrator_actions:
            try:
                result = ORCHESTRATOR.action(orchestrator_actions[parsed.path])
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "action": result["action"],
                    "output": result["output"],
                    "status": status(),
                })
            except subprocess.TimeoutExpired:
                self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Orchestrator command timed out."})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if parsed.path == "/api/all/restart":
            try:
                orchestrator_result = ORCHESTRATOR.action("Restart")
                result = _powershell_command("Restart")
                if result.returncode != 0:
                    message = (result.stderr or result.stdout or "Actuator tunnel command failed.").strip()
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "error": message,
                        "orchestrator_restarted": True,
                    })
                    return
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "action": "RestartAll",
                    "output": {
                        "orchestrator": orchestrator_result["output"],
                        "actuator": result.stdout.strip(),
                    },
                    "status": status(),
                })
            except subprocess.TimeoutExpired:
                self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Restart all timed out."})
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        actions = {
            "/api/tunnel/start": "Start",
            "/api/tunnel/stop": "Stop",
            "/api/tunnel/restart": "Restart",
            "/api/actuator/restart": "Restart",
        }
        action = actions.get(parsed.path)
        if action is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        try:
            result = _powershell_command(action)
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "Tunnel command failed.").strip()
                self._json(HTTPStatus.BAD_REQUEST, {"error": message})
                return
            self._json(HTTPStatus.OK, {
                "ok": True,
                "action": action,
                "output": result.stdout.strip(),
                "status": status(),
            })
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "Tunnel command timed out."})
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def main() -> None:
    if HOST not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Control Center must remain loopback-only.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ChatGPT-Actuator Control Center: http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
