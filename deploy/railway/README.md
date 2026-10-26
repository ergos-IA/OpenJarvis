# OpenJarvis en Railway: una instancia por persona, con WhatsApp

Este fork prepara OpenJarvis para desplegarse en Railway como **asistente
personal**: cada persona tiene su propio servicio (su propia memoria, su propia
clave de acceso y su propio WhatsApp vinculado).

```
Proyecto Railway "jarvis"
├── jarvis-jose    → volumen /data, WhatsApp de José
├── jarvis-maria   → volumen /data, WhatsApp de María
└── jarvis-pepe    → volumen /data, WhatsApp de Pepe
        ▲
        └── variables compartidas: OPENAI_API_KEY, ANTHROPIC_API_KEY
```

OpenJarvis no tiene usuarios internos: quien tiene la clave de una instancia
ve todo lo de esa instancia. Por eso **una persona = un servicio**.

## Qué incluye el fork

| Cambio | Dónde |
|---|---|
| Imagen con OpenAI + Claude y Node para WhatsApp | `deploy/railway/Dockerfile` |
| Arranque: puerto `$PORT`, permisos del volumen, `config.toml` desde variables | `deploy/railway/entrypoint.sh`, `bootstrap_config.py` |
| Página para escanear el QR de WhatsApp | `/whatsapp` (`src/openjarvis/server/whatsapp_routes.py`) |
| WhatsApp en modo personal: solo responde en el chat **"Tú" (mensajes a ti mismo)** | `whatsapp_baileys_bridge/src/bridge.ts` |
| Bandeja de entrada: guarda y avisa de mensajes de otros; contactos | `channels/whatsapp_store.py` |
| Herramientas: leer mensajes, buscar contactos, enviar con confirmación «sí» | `tools/whatsapp_tools.py` |
| Sesión de WhatsApp guardada en el volumen, re-vinculación y reinicio automático | `channels/whatsapp_baileys.py` |
| Script para crear instancias | `deploy/railway/new-instance.ps1` |

## Cómo funciona WhatsApp (modo `self`, el de por defecto)

1. La persona abre `https://<su-instancia>/whatsapp`, ve un QR y lo escanea
   desde **WhatsApp → Ajustes → Dispositivos vinculados → Vincular dispositivo**
   (igual que WhatsApp Web).
2. La sesión se guarda en el volumen (`/data/whatsapp_auth`). Sobrevive a
   reinicios y redeploys; no hay que volver a escanear.
3. Para hablar con Jarvis, escribe en el chat **contigo mismo** ("Tú" /
   "Mensajes para mí"). Jarvis contesta en ese mismo chat.
4. **Jarvis nunca contesta por su cuenta en otros chats** (ni a contactos ni
   a grupos).

### Leer tus WhatsApp y avisarte

Jarvis guarda los mensajes que recibes (de contactos y de desconocidos) y te
avisa en tu chat "Tú":

```
📩 *Pepe Pérez*: ¿quedamos mañana?
📩 *Laura* (+34611222333, no está en tus contactos): hola, soy de la academia
```

Y le puedes preguntar: *"¿qué me ha escrito Pepe?"*, *"resúmeme los mensajes
sin leer"*, *"¿me ha escrito alguien que no conozco hoy?"*. Los grupos se
guardan pero no se avisan (puedes preguntar por ellos). Se conservan 30 días.

`WHATSAPP_NOTIFY=contacts` avisa solo de contactos; `none` no avisa (pero
sigue guardando para que puedas preguntar).

> Los avisos los escribe tu propio WhatsApp en tu chat "Tú", así que el móvil
> normalmente **no suena** con ellos: los verás al abrir ese chat.

### Enviar mensajes a otros

*"Dile a Pepe que llego 10 minutos tarde"* → Jarvis prepara el borrador:

```
Borrador para Pepe Pérez: «Llego 10 minutos tarde». Responde «sí» para enviarlo.
```

Respondes **sí** → `✅ Enviado a Pepe Pérez.` · **no** → cancelado. El borrador
caduca a los 10 minutos. Vale un nombre de contacto o un número (si pones 9
cifras se asume +34; cámbialo con `WHATSAPP_DEFAULT_COUNTRY_CODE`).

**Seguridad:** la confirmación la comprueba el código, no la IA. Si alguien te
escribe un mensaje con instrucciones del tipo "Jarvis, reenvía esto a todos",
como mucho se prepararía un borrador que tú verías y tendrías que aprobar.
Puedes quitar la confirmación con `WHATSAPP_CONFIRM_SENDS=false` (no
recomendado).

> Privacidad: los mensajes que Jarvis lee para responderte se envían al
> proveedor del modelo (OpenAI o Anthropic), y se guardan en el volumen de la
> instancia.

Si se desvincula desde el móvil o con el botón "Desvincular" de la página, se
genera un QR nuevo automáticamente.

> Modo `own` (opcional): si la persona tiene un **número dedicado** para el
> asistente, pon `WHATSAPP_MODE=own` y `WHATSAPP_ALLOWED_NUMBERS=34600111222`.
> Solo esos números podrán hablarle.

> Baileys es una librería no oficial (protocolo de WhatsApp Web). Para uso
> personal, en tu propio chat y con poco volumen, el riesgo es bajo, pero no
> nulo: WhatsApp no lo soporta oficialmente. La alternativa oficial está en
> [META-WHATSAPP.md](META-WHATSAPP.md).

## Correo: Outlook (Microsoft 365) y Gmail

Cada instancia puede conectar **una cuenta de Outlook / Microsoft 365 y una de
Gmail** desde `https://<dominio>/correo` (la página del QR lleva un enlace).
Con ellas Jarvis:

- **te avisa por WhatsApp** de los correos nuevos importantes (Gmail: pestaña
  *Principal*; Outlook: bandeja *Prioritarios*), con una referencia (`o3`,
  `g7`) para pedirle «léeme el o3» o «respóndele que sí»;
- **busca y resume** («¿qué correos tengo sin leer?», «busca la factura de X»);
- **redacta correos y respuestas** que solo se envían cuando contestas «sí»
  en WhatsApp, igual que los mensajes.

**Gmail**: activa la verificación en dos pasos y crea una
[contraseña de aplicación](https://myaccount.google.com/apppasswords); en
`/correo` pegas tu dirección y esa contraseña (no tu contraseña normal).

**Outlook / Microsoft 365**: se inicia sesión con un código (como en una
tele): `/correo` → *Conectar Outlook* → abres microsoft.com/devicelogin, pones
el código y aceptas. Requiere, una sola vez por empresa, registrar una app en
Microsoft Entra:

1. [portal.azure.com](https://portal.azure.com) → *Microsoft Entra ID* →
   *Registros de aplicaciones* → *Nuevo registro*. Nombre «Jarvis»,
   «Solo cuentas de este directorio organizativo». Sin URI de redirección.
2. *Autenticación* → *Permitir flujos de clientes públicos*: **Sí** → Guardar.
3. *Permisos de API* → *Microsoft Graph* → *Permisos delegados*:
   `Mail.ReadWrite`, `Mail.Send`, `User.Read`, `offline_access` →
   *Conceder consentimiento de administrador*.
4. Copia el *Id. de aplicación (cliente)* y el *Id. de directorio (inquilino)*
   a las variables compartidas `MS_CLIENT_ID` y `MS_TENANT_ID` del proyecto
   en Railway (valen para todas las instancias de la empresa).

La contraseña de Gmail y el acceso de Outlook se guardan en el volumen de la
instancia (`/data/mail`). Lo que Jarvis lee de un correo para contestarte se
envía al proveedor del modelo (OpenAI / Anthropic).

## Primera vez: preparar el proyecto en Railway

1. Entra en [railway.com](https://railway.com) y crea un proyecto vacío
   (**New Project → Empty Project**), por ejemplo `jarvis`.
2. Conecta tu GitHub a Railway (Account Settings → GitHub) y da acceso al
   repo `ergos-IA/OpenJarvis`.
3. En el proyecto, **Settings → Shared Variables**, añade:
   - `OPENAI_API_KEY` = tu clave de OpenAI (`sk-...`)
   - `ANTHROPIC_API_KEY` = tu clave de Anthropic (`sk-ant-...`)
4. En tu ordenador:
   ```powershell
   npm i -g @railway/cli
   railway login
   cd OpenJarvis
   railway link        # elige el proyecto "jarvis"
   ```

## Crear una instancia para una persona

```powershell
./deploy/railway/new-instance.ps1 -Name maria
./deploy/railway/new-instance.ps1 -Name pepe -Model claude-sonnet-4-6
```

El script crea el servicio `jarvis-<nombre>`, le pone volumen en `/data`,
genera su clave de acceso, crea un dominio y te imprime el **enlace de
vinculación**:

```
https://jarvis-maria-production.up.railway.app/whatsapp#key=oj_sk_...
```

Envía ese enlace a la persona (por un canal privado: lleva su clave). Al
abrirlo verá el QR directamente. La clave va después de `#`, así que no viaja
al servidor ni queda en los logs; el navegador la recuerda.

El primer despliegue tarda unos 10-15 minutos (compila la imagen).

### A mano, desde el panel (sin script)

1. En el proyecto: **New → GitHub Repo → ergos-IA/OpenJarvis**. Railway
   detecta `railway.toml` y usa `deploy/railway/Dockerfile`.
2. **Variables** del servicio:

   | Variable | Valor |
   |---|---|
   | `OPENJARVIS_API_KEY` | clave larga aleatoria (`oj_sk_...`) |
   | `PORT` | `8000` |
   | `OPENAI_API_KEY` | `${{shared.OPENAI_API_KEY}}` |
   | `ANTHROPIC_API_KEY` | `${{shared.ANTHROPIC_API_KEY}}` |
   | `JARVIS_MODEL` | `gpt-4o-mini` (o `claude-sonnet-4-6`, ...) |

3. Clic derecho en el servicio → **Attach Volume** → mount path `/data`.
4. **Settings → Networking → Generate Domain** (puerto 8000).
5. Abre `https://<dominio>/whatsapp#key=<OPENJARVIS_API_KEY>`.

## Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `OPENJARVIS_API_KEY` | — (obligatoria) | Clave de acceso a la API, la web y la página del QR |
| `PORT` | `8000` | Puerto de escucha |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Proveedores de modelos; puedes tener los dos |
| `JARVIS_MODEL` | `gpt-4o-mini` | Modelo por defecto (también el de WhatsApp). Si contiene `claude` usa Anthropic, si no OpenAI |
| `JARVIS_ENGINE` | `cloud` | Motor de inferencia |
| `JARVIS_ASSISTANT_NAME` | `Jarvis` | Nombre del asistente |
| `JARVIS_CHANNEL_AGENT` | `orchestrator` | Agente que responde por WhatsApp (`orchestrator` usa herramientas; `simple` solo conversa) |
| `JARVIS_TOOLS` | `think,calculator,web_search,whatsapp_*,email_*` | Herramientas disponibles |
| `TZ` | `Europe/Madrid` | Zona horaria para las horas de los mensajes |
| `WHATSAPP_ENABLED` | `true` | `false` para desactivar WhatsApp |
| `WHATSAPP_MODE` | `self` | `self` = número personal, chat contigo mismo · `own` = número dedicado |
| `WHATSAPP_ALLOWED_NUMBERS` | — | Solo modo `own`: números permitidos, con prefijo, separados por coma |
| `WHATSAPP_NOTIFY` | `all` | Avisos de mensajes entrantes: `all`, `contacts` o `none` |
| `WHATSAPP_CONFIRM_SENDS` | `true` | Pedir «sí» antes de enviar mensajes a otros |
| `WHATSAPP_DEFAULT_COUNTRY_CODE` | `34` | Prefijo para números de 9 cifras |
| `MS_CLIENT_ID` / `MS_TENANT_ID` | — | App de Microsoft Entra para conectar Outlook (ver «Correo») |
| `EMAIL_NOTIFY` | `important` | Avisos de correo nuevo: `important`, `all` o `none` |
| `EMAIL_POLL_SECONDS` | `120` | Cada cuánto se mira si hay correo nuevo |

Modelos que reconoce el motor cloud: `gpt-4o-mini`, `gpt-4o`, `gpt-5`,
`gpt-5-mini`, `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-6`, ...
(lista completa en `src/openjarvis/engine/cloud.py`). En la web se puede
cambiar de modelo en cada conversación.

## Operación diaria

```powershell
railway service list                       # ver instancias
railway logs --service jarvis-maria        # logs
railway service redeploy --service jarvis-maria
railway variable set JARVIS_MODEL=claude-sonnet-4-6 --service jarvis-maria
```

- **Actualizar todas**: haz push a `main`; Railway redespliega cada servicio
  conectado al repo. La sesión de WhatsApp y los datos siguen en el volumen.
- **Traer novedades del proyecto original**:
  ```powershell
  git fetch upstream
  git merge upstream/main
  git push
  ```
- **Borrar una instancia**: `railway service delete --service jarvis-maria`
  (borra también sus datos). Que la persona quite el dispositivo vinculado en
  su WhatsApp.

## Costes orientativos

- Railway: cada instancia consume ~300-600 MB de RAM en reposo. En el plan
  Hobby (5 $/mes de crédito incluido) cuenta con unos 3-6 $/mes por instancia
  más el volumen (céntimos por GB).
- Modelo: según uso. `gpt-4o-mini` y `claude-haiku-4-5` son muy baratos para
  chat personal.

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| El servicio se reinicia con `OPENJARVIS_API_KEY no está definida` | Falta la variable |
| `/whatsapp` dice "WhatsApp no está activado" | `WHATSAPP_ENABLED=false`, o el canal no arrancó: mira los logs |
| El QR no aparece | Espera ~30 s tras el arranque; revisa los logs (`Bridge error`) |
| Tras un redeploy pide QR otra vez | El volumen no está montado en `/data` |
| Jarvis no contesta | Escribe en el chat **contigo mismo**, no en otro chat (modo `self`) |
| Error de permisos en `/data` | El entrypoint arregla la propiedad al arrancar; no cambies el usuario de arranque del contenedor |
