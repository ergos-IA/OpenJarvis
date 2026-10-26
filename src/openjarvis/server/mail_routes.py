"""Email account linking page and API.

``GET /correo`` serves a self-contained page to link Gmail (address + app
password) and Outlook / Microsoft 365 (device-code sign-in).  Like
``/whatsapp`` the page is public and holds no data; it calls ``/api/mail/*``
with the instance API key (typed once or passed as ``/correo#key=...``).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from openjarvis.mail.service import get_mail_service

router = APIRouter(tags=["mail"])


class GmailLink(BaseModel):
    email: str
    app_password: str


@router.on_event("startup")
async def _start_mail_poller() -> None:
    get_mail_service().start()


@router.get("/api/mail/status")
async def mail_status() -> dict:
    return get_mail_service().status()


@router.post("/api/mail/gmail")
async def mail_link_gmail(req: GmailLink) -> dict:
    svc = get_mail_service()
    try:
        await asyncio.to_thread(svc.connect_gmail, req.email, req.app_password)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Gmail rechazó el acceso: {str(exc)[:200]}",
        ) from exc
    return svc.status()


@router.delete("/api/mail/gmail")
async def mail_unlink_gmail() -> dict:
    svc = get_mail_service()
    svc.disconnect_gmail()
    return svc.status()


@router.post("/api/mail/outlook/start")
async def mail_outlook_start() -> dict:
    svc = get_mail_service()
    if svc.ms_auth is None:
        raise HTTPException(
            status_code=404, detail="Outlook no está configurado (falta MS_CLIENT_ID)"
        )
    try:
        await asyncio.to_thread(svc.ms_auth.start_device_flow)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Microsoft: {str(exc)[:200]}"
        ) from exc
    return svc.status()


@router.delete("/api/mail/outlook")
async def mail_unlink_outlook() -> dict:
    svc = get_mail_service()
    svc.disconnect_outlook()
    return svc.status()


@router.get("/correo", response_class=HTMLResponse, include_in_schema=False)
async def mail_page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})


_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conectar correo</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#14171a; --muted:#5f6b76;
          --accent:#2463eb; --ok:#128c7e; --border:#e2e6ea; --danger:#c0392b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1214; --card:#181c1f; --fg:#e8ecef; --muted:#9aa5ae;
            --accent:#6b9bff; --ok:#25d366; --border:#2a3035; --danger:#e57368; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width:min(480px, 100% - 32px); margin:24px 0; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:16px; padding:22px; margin-bottom:16px; }
  h1 { font-size:1.35rem; margin:0 0 12px; }
  h2 { font-size:1.1rem; margin:0 0 6px; }
  p, li { color:var(--muted); margin:6px 0; }
  ol { padding-left:20px; margin:8px 0; }
  a { color:var(--accent); }
  .ok { color:var(--ok); font-weight:600; }
  .err { color:var(--danger); }
  .code { font:600 1.6rem/1.2 ui-monospace, monospace; letter-spacing:.12em;
          text-align:center; padding:12px; border:1px dashed var(--border);
          border-radius:10px; margin:10px 0; user-select:all; }
  input { width:100%; padding:10px 12px; border-radius:8px; font:inherit;
          border:1px solid var(--border); background:var(--bg); color:var(--fg);
          margin-top:8px; }
  button { margin-top:12px; padding:10px 16px; border-radius:8px; border:0;
           font:inherit; cursor:pointer; background:var(--accent); color:#fff; }
  button.secondary { background:transparent; color:var(--danger);
                     border:1px solid var(--border); }
  button:disabled { opacity:.6; cursor:default; }
  .hidden { display:none; }
</style>
</head>
<body>
<main>
  <section id="login" class="card hidden">
    <h1>Conectar correo</h1>
    <p>Introduce la clave de acceso de tu Jarvis.</p>
    <input id="key" type="password" autocomplete="current-password"
           placeholder="oj_sk_...">
    <button id="save">Entrar</button>
  </section>

  <div id="app" class="hidden">
    <h1>Conectar correo</h1>

    <section class="card">
      <h2>Outlook / Microsoft 365</h2>
      <div id="o-unavailable" class="hidden">
        <p>Outlook aún no está configurado en esta instancia
           (falta la app de Microsoft).</p>
      </div>
      <div id="o-off" class="hidden">
        <p>Inicia sesión con tu cuenta de Microsoft para que Jarvis pueda
           leer y (con tu «sí») enviar correos.</p>
        <button id="o-start">Conectar Outlook</button>
      </div>
      <div id="o-pending" class="hidden">
        <ol>
          <li>Abre <a id="o-link" target="_blank" rel="noopener"></a></li>
          <li>Escribe este código:</li>
        </ol>
        <div id="o-code" class="code"></div>
        <p>Inicia sesión con tu cuenta y acepta los permisos.
           Esta página se actualizará sola.</p>
      </div>
      <div id="o-on" class="hidden">
        <p class="ok">✓ Conectado</p>
        <p id="o-email"></p>
        <button id="o-out" class="secondary">Desconectar</button>
      </div>
      <p id="o-error" class="err hidden"></p>
    </section>

    <section class="card">
      <h2>Gmail</h2>
      <div id="g-off" class="hidden">
        <ol>
          <li>Activa la <b>verificación en dos pasos</b> en tu cuenta de Google.</li>
          <li>Crea una <a href="https://myaccount.google.com/apppasswords"
              target="_blank" rel="noopener">contraseña de aplicación</a>
              (nombre: «Jarvis»).</li>
          <li>Pégala aquí con tu dirección de Gmail.</li>
        </ol>
        <input id="g-email" type="email" autocomplete="off"
               placeholder="tu.correo@gmail.com">
        <input id="g-pass" type="password" autocomplete="off"
               placeholder="contraseña de aplicación (16 letras)">
        <button id="g-save">Conectar Gmail</button>
      </div>
      <div id="g-on" class="hidden">
        <p class="ok">✓ Conectado</p>
        <p id="g-email-on"></p>
        <button id="g-out" class="secondary">Desconectar</button>
      </div>
      <p id="g-error" class="err hidden"></p>
    </section>

    <p id="notify"></p>
  </div>
</main>
<script>
(function () {
  var STORE = "openjarvis.apiKey";
  var key = "";
  var m = location.hash.match(/key=([^&]+)/);
  if (m) {
    key = decodeURIComponent(m[1]);
    try { localStorage.setItem(STORE, key); } catch (e) {}
    history.replaceState(null, "", location.pathname);
  } else {
    try { key = localStorage.getItem(STORE) || ""; } catch (e) {}
  }
  var $ = function (id) { return document.getElementById(id); };
  function vis(id, on) { $(id).classList.toggle("hidden", !on); }
  function err(id, text) { $(id).textContent = text || ""; vis(id, !!text); }

  function api(method, path, body) {
    var opts = { method: method, headers: { Authorization: "Bearer " + key } };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) {
        key = ""; vis("app", false); vis("login", true); throw "auth";
      }
      return r.json().then(function (d) {
        if (!r.ok) throw (d && d.detail) || "Error";
        return d;
      });
    });
  }

  var timer = null;
  function render(s) {
    vis("login", false); vis("app", true);
    var o = s.outlook || {}, g = s.gmail || {};
    vis("o-unavailable", o.state === "unavailable");
    vis("o-off", o.state === "disconnected" || o.state === "error");
    vis("o-pending", o.state === "pending");
    vis("o-on", o.state === "connected");
    if (o.state === "pending") {
      $("o-link").textContent = o.verification_uri;
      $("o-link").href = o.verification_uri;
      $("o-code").textContent = o.user_code;
    }
    $("o-email").textContent = o.email || "";
    err("o-error", o.error || o.warning || "");
    vis("g-off", g.state !== "connected");
    vis("g-on", g.state === "connected");
    $("g-email-on").textContent = g.email || "";
    if (g.warning) err("g-error", g.warning);
    $("notify").textContent = s.notify === "none" ? "" :
      "Jarvis te avisará por WhatsApp de los correos nuevos " +
      (s.notify === "important" ? "importantes (Principal / Prioritarios)." : ".");
  }
  function poll() {
    clearTimeout(timer);
    api("GET", "/api/mail/status").then(function (s) {
      render(s);
      timer = setTimeout(poll, (s.outlook || {}).state === "pending" ? 3000 : 15000);
    }).catch(function (e) { if (e !== "auth") timer = setTimeout(poll, 5000); });
  }

  $("save").onclick = function () {
    key = $("key").value.trim();
    if (!key) return;
    try { localStorage.setItem(STORE, key); } catch (e) {}
    poll();
  };
  $("o-start").onclick = function () {
    err("o-error", "");
    this.disabled = true;
    var b = this;
    api("POST", "/api/mail/outlook/start").then(render)
      .catch(function (e) { if (e !== "auth") err("o-error", e); })
      .then(function () { b.disabled = false; poll(); });
  };
  $("o-out").onclick = function () {
    if (!confirm("¿Desconectar Outlook de Jarvis?")) return;
    api("DELETE", "/api/mail/outlook").then(render);
  };
  $("g-save").onclick = function () {
    err("g-error", "");
    var b = this;
    b.disabled = true; b.textContent = "Comprobando…";
    api("POST", "/api/mail/gmail",
        { email: $("g-email").value.trim(), app_password: $("g-pass").value })
      .then(function (s) { $("g-pass").value = ""; render(s); })
      .catch(function (e) { if (e !== "auth") err("g-error", e); })
      .then(function () { b.disabled = false; b.textContent = "Conectar Gmail"; });
  };
  $("g-out").onclick = function () {
    if (!confirm("¿Desconectar Gmail de Jarvis?")) return;
    api("DELETE", "/api/mail/gmail").then(render);
  };

  if (key) poll(); else vis("login", true);
})();
</script>
</body>
</html>
"""
