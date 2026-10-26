# Apps OAuth de los conectores (guía para el operador)

Los conectores Google, Strava, Spotify y Dropbox usan un botón «Conectar con…».
Para que funcionen hay que crear **una app OAuth por proveedor** (una sola vez,
para todos los clientes) y poner su ID y secreto como variables compartidas en
Railway. Cada instancia de Jarvis las lee del entorno; si faltan, el conector
aparece como «no disponible» en la página de conectores.

Todas las apps usan la misma URL de retorno (el relé central de la web pública
reenvía a cada instancia):

```
https://jarvis.aifasttrack.tech/oauth/callback
```

## Variables en Railway (Shared Variables del proyecto)

| Proveedor | Variables |
|-----------|-----------|
| Google  | `OPENJARVIS_GOOGLE_CLIENT_ID`, `OPENJARVIS_GOOGLE_CLIENT_SECRET` |
| Strava  | `OPENJARVIS_STRAVA_CLIENT_ID`, `OPENJARVIS_STRAVA_CLIENT_SECRET` |
| Spotify | `OPENJARVIS_SPOTIFY_CLIENT_ID`, `OPENJARVIS_SPOTIFY_CLIENT_SECRET` |
| Dropbox | `OPENJARVIS_DROPBOX_CLIENT_ID` (= *App key*), `OPENJARVIS_DROPBOX_CLIENT_SECRET` (= *App secret*) |

Defínelas como *Shared Variables* y referéncialas en cada servicio de instancia
(`${{shared.OPENJARVIS_GOOGLE_CLIENT_ID}}`…). Tras cambiarlas hay que
redesplegar las instancias. `PUBLIC_URL` debe seguir siendo
`https://jarvis.aifasttrack.tech` (es el valor por defecto).

---

## 1. Google (Calendar, Drive, Contactos, Tasks)

1. En <https://console.cloud.google.com> crea un proyecto, p. ej. «Jarvis AI Fast Track».
2. **APIs y servicios → Biblioteca**: habilita *Google Calendar API*,
   *Google Drive API*, *People API* y *Google Tasks API*.
3. **Google Auth Platform** (antes «Pantalla de consentimiento OAuth»):
   - *Branding*: nombre «Jarvis», email de soporte, logo, dominio
     `aifasttrack.tech` (verificado en Search Console), enlaces a la política
     de privacidad y condiciones.
   - *Audience*: tipo de usuario **Externo**.
   - *Data Access* → añade los scopes:
     - `openid`, `.../auth/userinfo.email` (no sensibles)
     - `.../auth/calendar.events` (sensible)
     - `.../auth/calendar.calendarlist.readonly` (sensible; necesario para ver todos los calendarios del usuario, `calendar.events` no permite listarlos)
     - `.../auth/contacts.readonly` (sensible)
     - `.../auth/tasks` (sensible)
     - `.../auth/drive.readonly` (**restringido**)
   - No añadas scopes de Gmail: el correo va por contraseña de aplicación.
4. **Clients → Create client** → tipo **Aplicación web**:
   - *URIs de redirección autorizados*: `https://jarvis.aifasttrack.tech/oauth/callback`
   - Copia el *Client ID* y el *Client secret* a Railway.

### Modo «Testing» vs producción y verificación

- **Testing** (estado inicial): solo pueden conectar los usuarios que añadas en
  *Audience → Test users* (máximo **100**, y cuentan aunque luego los borres).
  Las autorizaciones y los *refresh tokens* **caducan a los 7 días**: el
  cliente tendría que reconectar Google cada semana. Sirve para probar, no
  para clientes reales.
- **Producción sin verificar** («Publish app»): cualquiera puede conectar, pero
  ve el aviso «**Google no ha verificado esta aplicación**» (hay que pulsar
  *Avanzado → Ir a Jarvis*) y hay un tope de **100 usuarios nuevos en total**
  durante la vida del proyecto. Los tokens ya no caducan a los 7 días.
- **Verificación**: los scopes *sensibles* requieren verificación de marca y
  de uso (vídeo de demostración del flujo OAuth, justificación de cada scope,
  política de privacidad que cumpla la *Google API Services User Data Policy*);
  suele tardar de días a pocas semanas. El scope *restringido*
  `drive.readonly` además exige una **evaluación de seguridad anual (CASA)**
  por un laboratorio autorizado, con coste. Si eso no compensa, la
  alternativa es quitar `drive.readonly` de `providers/google.py` (se pierden
  las herramientas de Drive) y verificar solo los sensibles.
- La guía al usuario ya menciona el aviso de «app no verificada»; quítalo de
  `steps` en `providers/google.py` cuando la app esté verificada.

## 2. Strava

1. Entra en <https://www.strava.com/settings/api> con la cuenta de la empresa
   y crea la app («Jarvis», categoría, web `https://jarvis.aifasttrack.tech`,
   icono).
2. **Authorization Callback Domain**: `jarvis.aifasttrack.tech` (solo el
   dominio, sin `https://` ni ruta).
3. Copia *Client ID* y *Client Secret* a Railway.
4. Límites: una app nueva está en modo «un solo deportista» (solo tu cuenta).
   Desde el panel de la API se puede subir a **10 deportistas**; para más hay
   que enviar la app a revisión de Strava (formulario enlazado en
   *Getting Started*). Límites de uso: 200 peticiones/15 min y 2.000/día
   (compartidos entre todos los clientes, no por usuario).
5. Revisa el *Strava API Agreement* (uso de los datos con IA y que solo se
   muestren al propio deportista) antes de pedir la revisión.

## 3. Spotify

1. En <https://developer.spotify.com/dashboard> → **Create app**:
   - *Redirect URI*: `https://jarvis.aifasttrack.tech/oauth/callback`
   - API: *Web API*.
2. Copia *Client ID* y *Client secret* a Railway.
3. **Modo desarrollo** (en el que se quedará la app): el dueño de la app debe
   tener **Spotify Premium** activo (si caduca, la app deja de funcionar);
   máximo **5 usuarios**, y cada uno hay que añadirlo a mano en
   *User Management* (nombre + email de su cuenta Spotify). Un usuario no
   añadido recibe error 403 y el conector le dirá que nos escriba.
4. El modo ampliado (*Extended Quota*) solo se concede a empresas con más de
   250.000 usuarios activos al mes, así que en la práctica Spotify queda como
   conector para unos pocos clientes.
5. Controlar la música (poner, pausar, siguiente) requiere que **el cliente**
   tenga Premium; consultar lo que escucha funciona con cuenta gratuita.

## 4. Dropbox

1. En <https://www.dropbox.com/developers/apps> → **Create app** →
   *Scoped access* → **Full Dropbox** → nombre «Jarvis AI Fast Track»
   (único en Dropbox).
2. Pestaña **Permissions**: marca `account_info.read`, `files.metadata.read`
   y `files.content.read` y pulsa **Submit** (si no, la autorización falla con
   «invalid scope»).
3. Pestaña **Settings** → *OAuth 2 → Redirect URIs*:
   `https://jarvis.aifasttrack.tech/oauth/callback`.
4. Copia *App key* y *App secret* a Railway (como CLIENT_ID / CLIENT_SECRET).
5. La app empieza en estado *Development*, con un número limitado de usuarios
   que pueden conectarla; cuando se acerque al límite, pulsa
   **Apply for production** en *Settings* (revisión de Dropbox).

---

## Comprobación rápida

1. Redespliega una instancia con las variables puestas.
2. En su página de conectores, el conector debe aparecer como disponible.
3. Pulsa «Conectar con…», autoriza con una cuenta de prueba y comprueba que
   aparece la cuenta (email o nombre) junto al conector.
4. Por WhatsApp: «¿Qué tengo mañana?», «¿Cuánto he corrido esta semana?»,
   «¿Qué estoy escuchando?», «Busca en Dropbox la factura».
