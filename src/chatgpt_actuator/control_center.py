from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse
import urllib.request

from .account_bootstrap import AccountBootstrap
from .browser_accounts import BrowserAccountManager
from .orchestrator_control import OrchestratorControl


HOST = "127.0.0.1"
PORT = int(os.environ.get("CHATGPT_ACTUATOR_CONTROL_PORT", "8764"))
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TUNNEL_MANAGER = PROJECT_ROOT / "scripts" / "tunnel-manager.ps1"
TUNNEL_PROFILE = os.environ.get("CHATGPT_ACTUATOR_TUNNEL_PROFILE", "chatgpt-actuator")
ADMIN_URL = "http://127.0.0.1:8765"
ADMIN_HEALTH = ADMIN_URL + "/healthz"
MCP_HEALTH = os.environ.get("CHATGPT_ACTUATOR_MCP_HEALTH", "http://127.0.0.1:8081/healthz")
TOKEN = secrets.token_urlsafe(24)
STARTED_AT = time.time()
ORCHESTRATOR = OrchestratorControl(PROJECT_ROOT)
ACCOUNT_BOOTSTRAP = AccountBootstrap(PROJECT_ROOT)
BROWSER_ACCOUNTS = BrowserAccountManager(PROJECT_ROOT)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT-Actuator Control Center</title>
<style>
:root{font-family:Segoe UI,Arial,sans-serif;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e5e7eb}
header{padding:20px 24px;background:#0f172a;border-bottom:1px solid #26364d}
h1{margin:0;font-size:22px}.sub{margin-top:5px;color:#94a3b8;font-size:13px}
main{max-width:1080px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}
.card{background:#151f31;border:1px solid #2b3a50;border-radius:14px;padding:16px}
.card h2{font-size:16px;margin:0 0 12px}.row{display:flex;justify-content:space-between;gap:12px;margin:8px 0}
.label{color:#94a3b8}.ok{color:#86efac}.bad{color:#fca5a5}.warn{color:#fde68a}
.dot{font-size:12px;margin-right:6px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
button,a.button{border:0;border-radius:9px;padding:9px 12px;font-weight:650;cursor:pointer;text-decoration:none;color:white;background:#2563eb}
button.secondary,a.secondary{background:#475569}button.danger{background:#b91c1c}
button:disabled{opacity:.55;cursor:wait}
#banner{padding:11px 13px;border-radius:9px;background:#101827;border:1px solid #334155;margin-bottom:14px;white-space:pre-wrap}
pre{white-space:pre-wrap;word-break:break-word;background:#060b14;border:1px solid #26364d;border-radius:10px;padding:12px;max-height:260px;overflow:auto;font:12px Consolas,monospace}
.small{font-size:12px;color:#94a3b8}.danger-note{font-size:12px;color:#fbbf24;margin-top:10px}
.headrow{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.lang-switch{display:flex;gap:6px}
button.lang-btn{padding:6px 10px;background:#334155;color:#cbd5e1;border:1px solid #475569}
button.lang-btn.active{background:#2563eb;color:#fff;border-color:#3b82f6}
select,input[type=text]{background:#0f172a;color:#e5e7eb;border:1px solid #475569;border-radius:9px;padding:9px 10px;min-width:0}
select{width:100%}.account-create{display:flex;gap:8px;margin-top:10px}.account-create input{flex:1}
</style>
</head>
<body>
<header>
<div class="headrow">
  <div>
    <h1 data-i18n="title">ChatGPT-Actuator Control Center</h1>
    <div class="sub" data-i18n="subtitle">Independent local sidecar · loopback only · stays alive while Tunnel/Actuator restarts</div>
  </div>
  <div class="lang-switch" aria-label="Language">
    <button id="lang-vi" class="lang-btn" onclick="setLanguage('vi')">VI</button>
    <button id="lang-en" class="lang-btn" onclick="setLanguage('en')">EN</button>
  </div>
</div>
</header>
<main>
<div id="banner">Loading status...</div>
<div class="grid">
  <section class="card">
    <h2 data-i18n="connectivity">Connectivity</h2>
    <div class="row"><span class="label" data-i18n="tunnelProcess">Tunnel process</span><strong id="tunnel">...</strong></div>
    <div class="row"><span class="label" data-i18n="mcpHealth">MCP health</span><strong id="mcp">...</strong></div>
    <div class="row"><span class="label" data-i18n="adminHealth">Admin health</span><strong id="admin">...</strong></div>
    <div class="row"><span class="label" data-i18n="tunnelPids">Tunnel PID(s)</span><strong id="pids">-</strong></div>
    <div class="row"><span class="label" data-i18n="profile">Profile</span><strong id="profile">...</strong></div>
    <div class="actions">
      <button data-i18n="start" onclick="action('/api/tunnel/start','startTunnel')">Start</button>
      <button class="secondary" data-i18n="stop" onclick="action('/api/tunnel/stop','stopTunnel')">Stop</button>
      <button class="danger" data-i18n="restartTunnel" onclick="action('/api/tunnel/restart','restartTunnel')">Restart tunnel</button>
    </div>
  </section>

  <section class="card">
    <h2>Actuator</h2>
    <div class="row"><span class="label" data-i18n="version">Version</span><strong id="version">...</strong></div>
    <div class="row"><span class="label" data-i18n="controlCenter">Control Center</span><strong id="control-running" class="ok">● Running</strong></div>
    <div class="row"><span class="label" data-i18n="controlUptime">Control uptime</span><strong id="uptime">...</strong></div>
    <div class="actions">
      <button class="danger" data-i18n="restartActuator" onclick="action('/api/actuator/restart','restartActuator')">Restart Actuator</button>
      <a class="button secondary" data-i18n="openAdmin" href="http://127.0.0.1:8765/" target="_blank" rel="noopener">Open Admin</a>
    </div>
    <div class="danger-note" data-i18n="restartActuatorNote">Restart Actuator restarts the tunnel process tree because the MCP Actuator is a child of tunnel-client.</div>
  </section>

  <section class="card">
    <h2>Orchestrator</h2>
    <div class="row"><span class="label" data-i18n="process">Process</span><strong id="orch-process">...</strong></div>
    <div class="row"><span class="label" data-i18n="health">Health</span><strong id="orch-health">...</strong></div>
    <div class="row"><span class="label" data-i18n="version">Version</span><strong id="orch-version">...</strong></div>
    <div class="row"><span class="label" data-i18n="uptime">Uptime</span><strong id="orch-uptime">...</strong></div>
    <div class="row"><span class="label" data-i18n="backend">Backend</span><strong id="orch-backend">...</strong></div>
    <div class="row"><span class="label" data-i18n="activeTransport">Active transport</span><strong id="orch-transport">...</strong></div>
    <div class="row"><span class="label" data-i18n="activeWorkers">Active workers</span><strong id="orch-workers">0</strong></div>
    <div class="row"><span class="label" data-i18n="activeJobs">Active jobs</span><strong id="orch-jobs">0</strong></div>
    <div class="actions">
      <button data-i18n="start" onclick="action('/api/orchestrator/start','startOrchestrator')">Start</button>
      <button class="secondary" data-i18n="stop" onclick="action('/api/orchestrator/stop','stopOrchestrator')">Stop</button>
      <button class="danger" data-i18n="restartOrchestrator" onclick="action('/api/orchestrator/restart','restartOrchestrator')">Restart Orchestrator</button>
    </div>
  </section>

  <section class="card">
    <h2 data-i18n="systemChatGPTAccount">System ChatGPT Account</h2>
    <div class="row"><span class="label" data-i18n="systemAccount">System profile</span><strong id="browser-account-active">...</strong></div>
    <div class="row"><span class="label" data-i18n="mainLogin">Main ChatGPT login</span><strong id="browser-account-main-login">...</strong></div>
    <div class="row"><span class="label" data-i18n="workerLogin">Worker ChatGPT login</span><strong id="browser-account-worker-login">...</strong></div>
    <div class="row"><span class="label" data-i18n="actualSync">Actual login sync</span><strong id="browser-account-login-sync">...</strong></div>
    <div class="row"><span class="label">Worker Edge</span><strong id="browser-account-running">...</strong></div>
    <div class="row"><span class="label" data-i18n="manualMainMapping">Main mapping</span><strong id="browser-account-main">...</strong></div>
    <div class="row"><span class="label">Config</span><strong id="browser-account-config">...</strong></div>
    <div style="margin-top:12px"><select id="browser-account-select" aria-label="ChatGPT account"></select></div>
    <div class="actions">
      <button data-i18n="detectAccounts" onclick="detectBrowserAccounts()">Detect signed-in accounts</button>
      <button class="secondary" data-i18n="switchSystemAccount" onclick="switchBrowserAccount()">Change system account</button>
      <button class="secondary" data-i18n="confirmMainAccount" onclick="confirmMainAccount()">Confirm Main = this account</button>
      <button class="secondary" data-i18n="syncWorkerWithMain" onclick="syncWorkerWithMain()">Sync Worker with Main</button>
      <button class="secondary" data-i18n="openWorkerLogin" onclick="openBrowserAccountLogin()">Open / sign in Worker</button>
    </div>
    <div class="account-create">
      <input id="browser-account-new" type="text" maxlength="60" autocomplete="off" placeholder="e.g. Account 2">
      <button data-i18n="addSystemAccount" onclick="createBrowserAccount()">Add account</button>
    </div>
    <div class="small" id="browser-account-path" style="margin-top:10px">...</div>
    <div class="danger-note" data-i18n="browserAccountNote">Detection reads only ChatGPT name/email inside each browser. It never copies passwords, cookies, session tokens, or access tokens. Main Edge is never focused or navigated.</div>
  </section>

  <section class="card">
    <h2 data-i18n="accountBootstrap">Account Bootstrap</h2>
    <div class="row"><span class="label" data-i18n="profilePack">Profile pack</span><strong id="account-profile">...</strong></div>
    <div class="row"><span class="label" data-i18n="portableFiles">Portable files</span><strong id="account-files">0/5</strong></div>
    <div class="row"><span class="label" data-i18n="pluginAuthorization">Plugin authorization</span><strong id="account-auth">...</strong></div>
    <div class="row"><span class="label" data-i18n="secrets">Secrets</span><strong id="account-secrets" class="ok">Excluded</strong></div>
    <div class="actions">
      <button data-i18n="exportProfile" onclick="accountAction('/api/account/export','exportProfile')">Export profile</button>
      <button data-i18n="setupNewAccount" onclick="accountAction('/api/account/setup','setupNewAccount')">Setup new account</button>
      <button class="secondary" data-i18n="copyBootstrap" onclick="copyBootstrap()">Copy bootstrap prompt</button>
    </div>
    <div class="small" id="account-path" style="margin-top:10px">...</div>
    <div class="danger-note" data-i18n="accountNote">OAuth, ChatGPT session cookies, API keys, Memory, subscription, and workspace membership are never copied.</div>
  </section>

  <section class="card">
    <h2 data-i18n="diagnostics">Diagnostics</h2>
    <div class="actions">
      <button class="secondary" data-i18n="refreshStatus" onclick="refresh()">Refresh status</button>
      <button class="secondary" data-i18n="refreshLogs" onclick="loadLogs()">Refresh logs</button>
      <button class="danger" data-i18n="restartAll" onclick="action('/api/all/restart','restartAll')">Restart all</button>
    </div>
    <div class="small" style="margin-top:10px" data-i18n="restartAllNote">Restart all restarts Tunnel + ChatGPT-Actuator + ChatGPT-Orchestrator.</div>
  </section>
</div>

<h2 style="font-size:16px;margin-top:20px" data-i18n="recentLogs">Recent service logs</h2>
<pre id="logs">Press Refresh logs.</pre>
</main>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const I18N={
  vi:{
    title:"Trung tâm điều khiển ChatGPT-Actuator",
    subtitle:"Sidecar cục bộ độc lập · chỉ truy cập loopback · vẫn hoạt động khi Tunnel/Actuator khởi động lại",
    loading:"Đang tải trạng thái...",
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
    systemAccount:"Profile hệ thống",
    mainLogin:"Tài khoản ChatGPT Main",
    workerLogin:"Tài khoản ChatGPT Worker",
    actualSync:"Đăng nhập thực tế",
    manualMainMapping:"Gán Main",
    detectAccounts:"Phát hiện tài khoản",
    switchSystemAccount:"Đổi tài khoản hệ thống",
    confirmMainAccount:"Xác nhận Main = tài khoản này",
    syncWorkerWithMain:"Đồng bộ Worker với Main",
    openWorkerLogin:"Mở / đăng nhập Worker",
    addSystemAccount:"Thêm tài khoản",
    accountAliasPlaceholder:"Ví dụ: Tài khoản 2",
    browserAccountNote:"Phát hiện chỉ đọc tên/email ChatGPT trong từng trình duyệt. Không sao chép mật khẩu, cookie, session token hoặc access token. Main Edge không bị focus hay điều hướng.",
    activeAccountMarker:"đang dùng",
    configured:"Đúng",
    misconfigured:"Sai cấu hình",
    mainNotConfirmed:"Chưa xác nhận",
    mainConfirmed:"Đã xác nhận",
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
    systemSwitchNext:"Worker đã chuyển. Hãy đổi Main Edge sang cùng tài khoản rồi bấm “Xác nhận Main = tài khoản này”.",
    mainConfirmedDone:"Đã ghi nhận tài khoản Main.",
    workerSyncedDone:"Worker đã đồng bộ theo tài khoản Main.",
    accountBootstrap:"Di chuyển cấu hình",
    profilePack:"Gói hồ sơ cục bộ",
    portableFiles:"Tệp có thể chuyển",
    pluginAuthorization:"Xác thực plugin",
    secrets:"Thông tin bí mật",
    exportProfile:"Xuất hồ sơ",
    setupNewAccount:"Chuẩn bị tài khoản mới",
    copyBootstrap:"Sao chép bootstrap prompt",
    accountNote:"Không bao giờ sao chép OAuth, cookie phiên ChatGPT, API key, Memory, gói thuê bao hoặc quyền thành viên workspace.",
    excluded:"Đã loại trừ",
    notExported:"Chưa xuất",
    requiredAuthorizations:"cần xác thực",
    bootstrapCopied:"Đã sao chép bootstrap prompt.",
    clipboardFailed:"Không thể sao chép vào clipboard",
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
    title:"ChatGPT-Actuator Control Center",
    subtitle:"Independent local sidecar · loopback only · stays alive while Tunnel/Actuator restarts",
    loading:"Loading status...",
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
    systemAccount:"System profile",
    mainLogin:"Main ChatGPT login",
    workerLogin:"Worker ChatGPT login",
    actualSync:"Actual login sync",
    manualMainMapping:"Main mapping",
    detectAccounts:"Detect signed-in accounts",
    switchSystemAccount:"Change system account",
    confirmMainAccount:"Confirm Main = this account",
    syncWorkerWithMain:"Sync Worker with Main",
    openWorkerLogin:"Open / sign in Worker",
    addSystemAccount:"Add account",
    accountAliasPlaceholder:"e.g. Account 2",
    browserAccountNote:"Detection reads only the ChatGPT name/email inside each browser. It never copies passwords, cookies, session tokens, or access tokens. Main Edge is never focused or navigated.",
    activeAccountMarker:"active",
    configured:"OK",
    misconfigured:"Misconfigured",
    mainNotConfirmed:"Not confirmed",
    mainConfirmed:"Confirmed",
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
    systemSwitchNext:"Worker changed. Switch Main Edge to the same account, then confirm Main.",
    mainConfirmedDone:"Main account confirmation saved.",
    workerSyncedDone:"Worker synced to the confirmed Main account.",
    accountBootstrap:"Configuration migration",
    profilePack:"Local profile pack",
    portableFiles:"Portable files",
    pluginAuthorization:"Plugin authorization",
    secrets:"Secrets",
    exportProfile:"Export profile",
    setupNewAccount:"Prepare new account",
    copyBootstrap:"Copy bootstrap prompt",
    accountNote:"OAuth, ChatGPT session cookies, API keys, Memory, subscription, and workspace membership are never copied.",
    excluded:"Excluded",
    notExported:"Not exported",
    requiredAuthorizations:"authorization(s) required",
    bootstrapCopied:"Bootstrap prompt copied.",
    clipboardFailed:"Could not copy to clipboard",
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
  if(!logsLoaded) $("logs").textContent=t("pressRefreshLogs");
  if(lastStatus) render(lastStatus);
}
function setLanguage(lang){
  if(lang!=="vi"&&lang!=="en") return;
  currentLang=lang;
  try{localStorage.setItem("chatgpt-actuator-control-language",lang);}catch(_e){}
  applyLanguage();
  refresh();
}
function state(ok,text){return '<span class="'+(ok?'ok':'bad')+'"><span class="dot">●</span>'+text+'</span>'}
async function api(path,opts={}){
  opts.headers=Object.assign({'Accept':'application/json','X-Control-Center-Token':TOKEN},opts.headers||{});
  const r=await fetch(path,opts);
  const b=await r.json().catch(()=>({error:'Invalid JSON response'}));
  if(!r.ok) throw new Error(b.error||('HTTP '+r.status));
  return b;
}
function render(s){
  lastStatus=s;
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
  const a=s.account||{};
  $("account-profile").innerHTML=state(!!a.exists,a.exists?t("ready"):t("notExported"));
  $("account-files").textContent=String(a.portable_file_count??0)+"/5";
  $("account-auth").innerHTML='<span class="warn">'+String(a.manual_authorization_count??0)+' '+t("requiredAuthorizations")+'</span>';
  $("account-secrets").textContent=t("excluded");
  $("account-path").textContent=a.profile_root||"-";
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
    if(["cdp_unavailable","cdp_error","playwright_unavailable","session_unavailable"].includes(identity.state)) return t("detectionUnavailable");
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
}
async function refresh(){
  try{
    const s=await api("/api/status");
    render(s);
    $("banner").textContent=t("ready")+" · "+new Date().toLocaleTimeString(currentLang==="vi"?"vi-VN":"en-US");
  }catch(e){
    $("banner").textContent=t("statusError")+": "+e.message;
  }
}
async function action(path,labelKey){
  const label=t(labelKey);
  if(!confirm(label+"?")) return;
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  $("banner").textContent=label+" "+t("inProgress");
  try{
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    render(r.status);
    $("banner").textContent=label+" "+t("completed");
    await loadLogs();
  }catch(e){
    $("banner").textContent=label+" "+t("failed")+": "+e.message;
  }finally{
    document.querySelectorAll("button").forEach(b=>b.disabled=false);
  }
}
async function browserAccountPost(path,labelKey,body,confirmFirst=true){
  const label=t(labelKey);
  if(confirmFirst && !confirm(label+"?")) return null;
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  $("banner").textContent=label+" "+t("inProgress");
  try{
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
    if(r.status) render(r.status);
    const launchError=r.result&&r.result.launch_error?(" · "+r.result.launch_error):"";
    $("banner").textContent=label+" "+t("completed")+launchError;
    return r;
  }catch(e){
    $("banner").textContent=label+" "+t("failed")+": "+e.message;
    return null;
  }finally{
    document.querySelectorAll("button").forEach(b=>b.disabled=false);
  }
}
async function detectBrowserAccounts(){
  const r=await browserAccountPost("/api/browser-accounts/detect","detectAccounts",{scope:"both"},false);
  if(!r) return;
  const d=(r.status&&r.status.browser_accounts&&r.status.browser_accounts.identity_detection)||{};
  const main=d.main||{};
  if(main.state==="authorization_required") $("banner").textContent=t("detectNeedsMainPermission");
  else $("banner").textContent=t("detectCompleted");
}
async function switchBrowserAccount(){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  const r=await browserAccountPost("/api/browser-accounts/switch","switchSystemAccount",{account_id:accountId},true);
  if(r) $("banner").textContent=t("systemSwitchNext");
}
async function confirmMainAccount(){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  const r=await browserAccountPost("/api/browser-accounts/confirm-main","confirmMainAccount",{account_id:accountId},false);
  if(r) $("banner").textContent=t("mainConfirmedDone");
}
async function syncWorkerWithMain(){
  const r=await browserAccountPost("/api/browser-accounts/sync-to-main","syncWorkerWithMain",{},true);
  if(r) $("banner").textContent=t("workerSyncedDone");
}
async function openBrowserAccountLogin(){
  const accountId=$("browser-account-select").value;
  if(!accountId) return;
  await browserAccountPost("/api/browser-accounts/login","openWorkerLogin",{account_id:accountId},true);
}
async function createBrowserAccount(){
  const input=$("browser-account-new");
  const label=(input.value||"").trim();
  if(!label){input.focus();return;}
  const r=await browserAccountPost("/api/browser-accounts/create","addSystemAccount",{label},false);
  if(r){
    input.value="";
    const created=r.result&&r.result.created_account_id;
    if(created) $("browser-account-select").value=created;
  }
}
async function accountAction(path,labelKey){
  const label=t(labelKey);
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  $("banner").textContent=label+" "+t("inProgress");
  try{
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    if(r.status) render(r.status);
    let copied=false;
    if(path==="/api/account/setup" && r.result && r.result.bootstrap_text){
      try{
        await navigator.clipboard.writeText(r.result.bootstrap_text);
        copied=true;
      }catch(_clipboardError){}
    }
    const detail=(r.result&&r.result.message)?(" · "+r.result.message):"";
    $("banner").textContent=label+" "+t("completed")+detail+(copied?(" · "+t("bootstrapCopied")):"");
  }catch(e){
    $("banner").textContent=label+" "+t("failed")+": "+e.message;
  }finally{
    document.querySelectorAll("button").forEach(b=>b.disabled=false);
  }
}
async function copyBootstrap(){
  try{
    const r=await api("/api/account/bootstrap");
    await navigator.clipboard.writeText(r.text||"");
    $("banner").textContent=t("bootstrapCopied");
  }catch(e){
    $("banner").textContent=t("clipboardFailed")+": "+e.message;
  }
}
async function loadLogs(){
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
applyLanguage();
refresh();
setInterval(refresh,5000);
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


def _powershell_command(action: str, *, json_output: bool = False) -> subprocess.CompletedProcess[str]:
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
        capture_output=True,
        timeout=35,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )


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
    admin = _read_json_url(ADMIN_HEALTH)
    return {
        "tunnel": _tunnel_status(),
        "actuator": {
            "version": admin.get("version", "unknown"),
            "admin_health_url": ADMIN_HEALTH,
            "mcp_health_url": MCP_HEALTH,
        },
        "orchestrator": ORCHESTRATOR.status(),
        "account": ACCOUNT_BOOTSTRAP.status(),
        "browser_accounts": BROWSER_ACCOUNTS.status(),
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
                    "text": ACCOUNT_BOOTSTRAP.bootstrap_text(),
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
        if parsed.path in {
            "/api/browser-accounts/create",
            "/api/browser-accounts/switch",
            "/api/browser-accounts/login",
            "/api/browser-accounts/confirm-main",
            "/api/browser-accounts/sync-to-main",
            "/api/browser-accounts/detect",
        }:
            try:
                payload = self._body_json()
                if parsed.path == "/api/browser-accounts/create":
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

        if parsed.path in {"/api/account/export", "/api/account/setup"}:
            try:
                if parsed.path == "/api/account/export":
                    result = ACCOUNT_BOOTSTRAP.export()
                else:
                    result = ACCOUNT_BOOTSTRAP.prepare_destination()
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
