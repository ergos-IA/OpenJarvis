"""WhatsApp (Baileys) pairing page and API.

``GET /whatsapp`` serves a small self-contained page that shows the QR code to
link a WhatsApp account, and the linking status once paired.  The page itself
is public (it holds no data); it calls the ``/api/whatsapp/*`` endpoints with
the instance API key, which the user types once or receives in the link
fragment (``/whatsapp#key=...`` -- fragments never reach the server or logs).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["whatsapp"])


def _baileys_channel(request: Request) -> Optional[Any]:
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return None
    if getattr(bridge, "channel_id", "") == "whatsapp_baileys":
        return bridge
    channels = getattr(bridge, "_channels", None) or {}
    return channels.get("whatsapp_baileys")


@router.get("/api/whatsapp/status")
async def whatsapp_status(request: Request) -> dict:
    channel = _baileys_channel(request)
    if channel is None:
        return {"enabled": False}
    return {"enabled": True, **channel.pairing_info()}


@router.post("/api/whatsapp/logout")
async def whatsapp_logout(request: Request) -> dict:
    channel = _baileys_channel(request)
    if channel is None:
        raise HTTPException(status_code=404, detail="WhatsApp is not enabled")
    if not channel.logout():
        raise HTTPException(status_code=503, detail="WhatsApp bridge not running")
    return {"status": "ok"}


@router.get("/whatsapp", response_class=HTMLResponse, include_in_schema=False)
async def whatsapp_page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})


_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vincular WhatsApp</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#14171a; --muted:#5f6b76;
          --accent:#128c7e; --border:#e2e6ea; --danger:#c0392b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1214; --card:#181c1f; --fg:#e8ecef; --muted:#9aa5ae;
            --accent:#25d366; --border:#2a3035; --danger:#e57368; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width:min(440px, 100% - 32px); background:var(--card);
         border:1px solid var(--border); border-radius:16px; padding:28px; }
  h1 { font-size:1.35rem; margin:0 0 4px; }
  p { margin:8px 0; color:var(--muted); }
  ol { padding-left:20px; color:var(--muted); margin:12px 0; }
  #qr { background:#fff; border-radius:12px; padding:12px; margin:16px auto;
        width:min(300px, 100%); aspect-ratio:1; display:grid; place-items:center; }
  #qr svg { width:100%; height:100%; }
  .ok { font-size:1.1rem; color:var(--accent); font-weight:600; }
  input { width:100%; padding:10px 12px; border-radius:8px; font:inherit;
          border:1px solid var(--border); background:var(--bg); color:var(--fg); }
  button { margin-top:12px; padding:10px 16px; border-radius:8px; border:0;
           font:inherit; cursor:pointer; background:var(--accent); color:#fff; }
  button.secondary { background:transparent; color:var(--danger);
                     border:1px solid var(--border); }
  .hidden { display:none; }
  small { color:var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Vincular WhatsApp</h1>

  <section id="login" class="hidden">
    <p>Introduce la clave de acceso de tu Jarvis.</p>
    <input id="key" type="password" autocomplete="current-password"
           placeholder="oj_sk_...">
    <button id="save">Entrar</button>
  </section>

  <section id="pairing" class="hidden">
    <ol>
      <li>Abre WhatsApp en tu móvil.</li>
      <li>Ajustes → <b>Dispositivos vinculados</b> → <b>Vincular dispositivo</b>.</li>
      <li>Escanea este código.</li>
    </ol>
    <div id="qr"><small>Generando código…</small></div>
    <small>El código se renueva solo cada pocos segundos.</small>
  </section>

  <section id="connected" class="hidden">
    <p class="ok">✓ WhatsApp vinculado</p>
    <p id="me"></p>
    <p id="howto"></p>
    <button id="logout" class="secondary">Desvincular</button>
  </section>

  <section id="info" class="hidden"><p id="infotext"></p></section>
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

  function show(id) {
    ["login", "pairing", "connected", "info"].forEach(function (s) {
      document.getElementById(s).classList.toggle("hidden", s !== id);
    });
  }
  function info(text) {
    document.getElementById("infotext").textContent = text;
    show("info");
  }

  var lastSvg = "";
  var timer = null;

  function poll() {
    fetch("/api/whatsapp/status", { headers: { Authorization: "Bearer " + key } })
      .then(function (r) {
        if (r.status === 401) { key = ""; show("login"); throw "auth"; }
        return r.json();
      })
      .then(function (s) {
        if (!s.enabled) {
          info("WhatsApp no está activado en esta instancia.");
          return;
        }
        if (s.connected) {
          document.getElementById("me").textContent = s.me ? "Número: +" + s.me : "";
          document.getElementById("howto").textContent = s.mode === "self"
            ? "Escríbele a Jarvis en el chat «Tú» (mensajes para ti mismo) de WhatsApp."
            : "Escríbele a este número desde uno de los números autorizados.";
          show("connected");
        } else if (s.qr_svg) {
          if (s.qr_svg !== lastSvg) {
            document.getElementById("qr").innerHTML = s.qr_svg;
            lastSvg = s.qr_svg;
          }
          show("pairing");
        } else {
          info(s.error ? "Error: " + s.error
                       : "Arrancando WhatsApp… espera unos segundos.");
        }
      })
      .catch(function (e) {
        if (e !== "auth") info("No se puede contactar con el servidor.");
      })
      .then(function () { if (key) timer = setTimeout(poll, 3000); });
  }

  document.getElementById("save").onclick = function () {
    key = document.getElementById("key").value.trim();
    if (!key) return;
    try { localStorage.setItem(STORE, key); } catch (e) {}
    clearTimeout(timer);
    poll();
  };
  document.getElementById("logout").onclick = function () {
    if (!confirm("¿Desvincular WhatsApp de Jarvis?")) return;
    fetch("/api/whatsapp/logout", {
      method: "POST",
      headers: { Authorization: "Bearer " + key },
    }).then(function () {
      lastSvg = "";
      info("Desvinculado. Generando un código nuevo…");
    });
  };

  if (key) poll(); else show("login");
})();
</script>
</body>
</html>
"""
