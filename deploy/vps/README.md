# Jarvis en un VPS con Docker (Hostinger)

Alternativa a Railway: todas las instancias en **un VPS** (Ubuntu 24.04,
Docker + compose v2) detrás del **Traefik v3** que ya existe
(`root-traefik-1`, red `root_default`, resolver `mytlschallenge`,
entrypoint `websecure`).

```
                         Traefik (root_default, :443)
jarvis.aifasttrack.tech ──┬── cookie jv=<slug>  (prio 100) ──► jarvis-<slug>  (/data = volumen jarvis-<slug>-data)
                          ├── rutas de la web    (prio 200) ──► landing
                          └── todo lo demás      (prio 1)   ──► landing
consola-jarvis.aifasttrack.tech ─────────────────────────────► console ──► docker-proxy ──► /var/run/docker.sock
```

- **Una instancia = un contenedor** `jarvis-<slug>` con su volumen
  `jarvis-<slug>-data` en `/data`, `restart: unless-stopped`, healthcheck
  `/health`, sin puertos publicados (solo la red `root_default`).
- **Los contenedores son la fuente de verdad** (no hay base de datos):
  etiquetas `fasttrack.jarvis=instance`, `fasttrack.jarvis.slug=<slug>`,
  `fasttrack.jarvis.railway_id=<id antiguo>` (si vino de Railway) y las
  variables en el entorno del contenedor.
- La consola, la web pública y los pagos de Stripe crean y gestionan los
  contenedores con la API de Docker a través de `docker-proxy`
  (`tecnativa/docker-socket-proxy`): ninguna app monta el socket.
- Se elige con `JARVIS_BACKEND=docker` (por defecto `railway`: en Railway
  no cambia nada).

## Un solo dominio, enrutado por cookie

Ya no hay un dominio por instancia. Todo se sirve en
`https://jarvis.aifasttrack.tech`:

1. El usuario mete su **código de acceso** (o su clave) en la web.
2. La web responde con
   `Set-Cookie: jv=<slug>; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=1 año`
   y manda el navegador a `/#key=<clave>` (mismo dominio).
3. Traefik ve la cookie y envía la petición al contenedor `jarvis-<slug>`,
   que cambia la clave por su sesión (`jarvis_session`) como siempre.

Etiquetas de cada instancia (las pone el backend; `dockerctl print-labels
<slug>` las muestra):

```
traefik.enable=true
traefik.docker.network=root_default
traefik.http.routers.jv-<slug>.rule=Host(`jarvis.aifasttrack.tech`) && HeaderRegexp(`Cookie`, `(^|;\s*)jv=<slug>(;|$)`)
traefik.http.routers.jv-<slug>.priority=100
traefik.http.routers.jv-<slug>.entrypoints=websecure
traefik.http.routers.jv-<slug>.tls.certresolver=mytlschallenge
traefik.http.routers.jv-<slug>.service=jv-<slug>
traefik.http.services.jv-<slug>.loadbalancer.server.port=8000
# acceso directo sin cookie (webhooks, p. ej. WhatsApp de Meta):
traefik.http.routers.jv-<slug>-direct.rule=Host(`jarvis.aifasttrack.tech`) && PathPrefix(`/i/<slug>/`)   (prio 150, quita /i/<slug>)
```

Rutas que son **siempre de la web pública** (router `jarvis-landing-own`,
prioridad 200), aunque el navegador tenga cookie `jv`:

| Ruta | Para qué |
|---|---|
| `/salir` | **Cambiar de usuario / salir**: borra la cookie `jv` (y la sesión) y vuelve a la portada |
| `/entrar` | Enlace personal `https://jarvis.aifasttrack.tech/entrar#key=…`: pone la cookie y abre su Jarvis |
| `/api/acceder` | Código de acceso → cookie |
| `/api/alta`, `/api/cuenta/*`, `/cuenta`, `/gracias` | Alta, portal y recargas de Stripe |
| `/api/stripe/webhook` | Webhook de Stripe (**misma URL que antes**) |
| `/oauth/callback` | Vuelta de Google/Spotify/…; redirige a `/oauth/done`, que con la cookie llega a la instancia |
| `/api/contacto` | Formulario de contacto |

Ninguna choca con rutas de la instancia. Sin cookie (o con la de una
instancia borrada o parada) todo va a la web (prioridad 1).

**Cambiar de usuario**: en la página de cada Jarvis, *Cerrar sesión /
cambiar de usuario* (pie de página) y el enlace de la página «Acceso
restringido» llevan a `/salir`. También vale escribir
`https://jarvis.aifasttrack.tech/salir` a mano.

Notas:

- Traefik solo enruta a contenedores **en marcha y sanos**: mientras una
  instancia arranca (hasta que `/health` responde) el usuario ve la web
  con el aviso «Tu Jarvis se está iniciando».
- Todas las instancias comparten origen: si un navegador usa dos Jarvis,
  la sesión de uno se pierde al entrar en el otro (vuelve a pedir el código).
  Es lo esperado.
- Cualquiera que conozca un slug puede poner la cookie, pero solo llega
  a la página «Acceso restringido»: la instancia sigue exigiendo su clave.

## Puesta en marcha

En el VPS, como root (ejemplo en `/opt/jarvis`):

```sh
git clone -b vps-docker https://github.com/ergos-IA/OpenJarvis.git /opt/jarvis
cd /opt/jarvis

cp deploy/vps/shared.env.example  deploy/vps/shared.env
cp deploy/vps/control.env.example deploy/vps/control.env
# Rellena ambos. El contenedor corre como uid 10001:
chown 10001:10001 deploy/vps/shared.env deploy/vps/control.env
chmod 600 deploy/vps/shared.env deploy/vps/control.env

docker build -t openjarvis:latest -f deploy/railway/Dockerfile .   # ~10-15 min la primera vez
docker compose -f deploy/vps/docker-compose.yml up -d
docker compose -f deploy/vps/docker-compose.yml ps
```

DNS: `jarvis.aifasttrack.tech` y `consola-jarvis.aifasttrack.tech` →
IP del VPS (registros A). Traefik pide los certificados solo.

Comprobar: `https://consola-jarvis.aifasttrack.tech` (contraseña
`ADMIN_PASSWORD`) y `https://jarvis.aifasttrack.tech`.

### Variables

| Dónde | Qué |
|---|---|
| `shared.env` | Lo que en Railway eran *Shared Variables*: claves de IA, `LEGAL_*`, Mailjet, `FASTTRACK_ADMIN_TOKEN`… Cada `${{shared.X}}` del aprovisionamiento se rellena desde aquí al crear/recrear una instancia (y se refresca en cada `recreate`). |
| `control.env` | Solo consola y web: `ADMIN_PASSWORD`, `STRIPE_*`. Nunca llega a las instancias. |
| `docker-compose.yml` | `JARVIS_BACKEND=docker`, `DOCKER_HOST`, `JARVIS_IMAGE`, `PUBLIC_URL`, red y Traefik. |

Opcionales (en `control.env`): `JARVIS_INSTANCE_SHARED=MS_CLIENT_ID,MS_TENANT_ID`
(variables de `shared.env` que reciben **todas** las instancias) y
`JARVIS_INSTANCE_MEMORY_MB=1024` (límite de RAM por instancia). Con 8 GB,
cuenta ~300-600 MB por instancia.

## Operación: `dockerctl`

Se ejecuta dentro del contenedor de la consola (mismo acceso a Docker y
mismas variables):

```sh
alias dockerctl='docker compose -f /opt/jarvis/deploy/vps/docker-compose.yml exec console python3 -m openjarvis.fasttrack.dockerctl'

dockerctl list                                   # instancias, estado, id de Railway
dockerctl create --env-file /run/jarvis/x.env --slug ana [--railway-id <id>]
dockerctl recreate ana        # o: dockerctl recreate --all  (tras una imagen nueva)
dockerctl set ana JARVIS_MODEL=claude-sonnet-5-5 # cambia variables (recrea)
dockerctl delete ana --confirm ana               # contenedor + volumen
dockerctl delete ana --confirm ana --keep-volume # solo el contenedor
dockerctl print-labels ana
```

`recreate` conserva variables, etiquetas y volumen y usa la imagen actual;
para el contenedor viejo **antes** de arrancar el nuevo. Si el nuevo no se
puede crear, vuelve a poner el anterior.

### Ids y Stripe

- En Docker el id de una instancia es su **slug** (`ana` para `jarvis-ana`).
  La consola (`/api/users/{id}/…`) y los pagos aceptan también el nombre del
  contenedor y **el id antiguo de Railway** (etiqueta
  `fasttrack.jarvis.railway_id`), porque las suscripciones y recargas ya
  creadas en Stripe guardan ese id en `metadata.service_id`.
- Las recargas (consola y Stripe) llaman a la instancia por la red interna:
  `http://jarvis-<slug>:8000/api/credits/topup`.

## Actualizar

```sh
sh /opt/jarvis/deploy/vps/update.sh        # git pull vps-docker + build + recrear todo
```

Hace `git pull` de la rama, `docker build -t openjarvis:latest -f
deploy/railway/Dockerfile .` (la imagen anterior queda como
`openjarvis:previous`), recrea web y consola y después
`dockerctl recreate --all`.

## Migrar una instancia desde Railway

> ⚠️ **WhatsApp: nunca la misma instancia en dos sitios a la vez.** La
> sesión de WhatsApp (Baileys) vive en `/data/whatsapp_auth`; si el
> contenedor de Railway y el del VPS arrancan con ella a la vez, WhatsApp
> cierra la sesión (y puede marcar el número). Para la de Railway **antes**
> de arrancar la del VPS y no la vuelvas a encender.

1. **Exporta sus variables** (en tu ordenador, con la CLI de Railway):
   ```sh
   railway variables --service jarvis-ana --kv > ana.env
   ```
   Apunta el **id del servicio** de Railway (Settings → Service ID, o la URL
   del panel): es el que guarda Stripe.
2. **Para el servicio en Railway** (quita el despliegue activo / pon
   réplicas a 0) y espera a que esté parado. No lo borres todavía.
3. **Copia `/data`** al VPS en el volumen `jarvis-ana-data`. Por ejemplo,
   con un servicio temporal en Railway que monte el volumen y lo empaquete
   (`tar czf - -C /data .`), descargándolo, y en el VPS:
   ```sh
   docker volume create jarvis-ana-data
   docker run --rm -v jarvis-ana-data:/data -v "$PWD":/in alpine \
     sh -c 'tar xzf /in/ana-data.tgz -C /data && chown -R 10001:10001 /data'
   ```
4. **Crea la instancia** reutilizando el volumen y con el id de Railway:
   ```sh
   docker compose -f /opt/jarvis/deploy/vps/docker-compose.yml cp ana.env console:/tmp/ana.env
   dockerctl create --env-file /tmp/ana.env --slug ana --railway-id <id-servicio-railway>
   docker compose -f /opt/jarvis/deploy/vps/docker-compose.yml exec console rm /tmp/ana.env
   ```
   Las variables `RAILWAY_*` se descartan; se añaden `PUBLIC_URL`,
   `JARVIS_ROUTING=cookie` y `PORT=8000`.
5. Comprueba con `dockerctl list` que está `SUCCESS` y entra con su código
   en `https://jarvis.aifasttrack.tech`. Su enlace personal pasa a ser
   `https://jarvis.aifasttrack.tech/entrar#key=…` (el antiguo
   `*.up.railway.app` deja de funcionar).
6. Cuando todo vaya bien, borra el servicio de Railway.

## Qué revisar al pasar a este modelo

- **OAuth** (Google, Spotify, Dropbox, Oura, Strava…): la URI de redirección
  sigue siendo `https://jarvis.aifasttrack.tech/oauth/callback`; no cambia
  si ya estaba registrada así. Microsoft (Outlook) usa código de
  dispositivo: no tiene URI.
- **Stripe**: el webhook sigue en `https://jarvis.aifasttrack.tech/api/stripe/webhook`.
- **WhatsApp de Meta (Business API)**: el webhook ya no puede ir al dominio
  propio de la instancia; usa
  `https://jarvis.aifasttrack.tech/i/<slug>/meta/whatsapp/webhook`.
- **Enlaces antiguos** `https://<algo>.up.railway.app/#key=…` (emails ya
  enviados) dejan de valer: el usuario entra con su código o con
  `/entrar#key=…`.
- Recursos: 2 vCPU / 8 GB. La construcción de la imagen (Rust + Node) es lo
  más pesado; hazla fuera de horas punta.
