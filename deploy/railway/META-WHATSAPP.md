# Guía: WhatsApp oficial (Meta WhatsApp Cloud API)

Ahora mismo usamos **Baileys** (escanear un QR, ver [README.md](README.md)).
Esta guía es para el día que queráis pasar a la **API oficial de Meta**: es
estable, Meta la soporta y no hay riesgo de bloqueo. A cambio pide un número
dedicado y algo más de configuración.

## Diferencias clave

| | Baileys (actual) | Meta Cloud API |
|---|---|---|
| Número | El personal de cada uno | Uno **dedicado** al asistente (no puede estar en la app de WhatsApp) |
| Alta | Escanear un QR | Meta Business + app de desarrollador |
| Oficial | No | Sí |
| Coste | Gratis | Gratis si el usuario escribe primero (ventana de 24 h); los mensajes que inicia el asistente usan plantillas de pago |
| Multipersona | Una instancia por persona | Un número por instancia (o un número compartido con lógica extra) |

## Paso 1: cuenta de Meta Business

1. Ve a [business.facebook.com](https://business.facebook.com) y crea una
   cartera comercial (Business Portfolio) si no tienes una.
2. Para producción Meta pedirá **verificar el negocio** (documentación de la
   empresa). Para pruebas no hace falta.

## Paso 2: app de desarrollador con WhatsApp

1. En [developers.facebook.com/apps](https://developers.facebook.com/apps):
   **Crear app → Otro → Empresa (Business)** y asóciala a tu cartera.
2. En el panel de la app: **Añadir producto → WhatsApp → Configurar**.
3. En **WhatsApp → Configuración de la API (API Setup)** verás:
   - Un **número de prueba** gratuito, con su **Phone number ID**.
   - Un **token de acceso temporal** (caduca en 24 h; solo para probar).
   - Un campo para añadir hasta 5 números destinatarios de prueba. Añade el
     tuyo y confírmalo con el código que te llega.

## Paso 3: token permanente

El token temporal no sirve para un servidor. Crea uno permanente:

1. [business.facebook.com](https://business.facebook.com) → **Configuración
   del negocio → Usuarios → Usuarios del sistema → Añadir** (rol
   Administrador).
2. **Asignar activos**: añade la app y la cuenta de WhatsApp con control
   total.
3. **Generar token**: elige la app, caducidad **Nunca**, permisos
   `whatsapp_business_messaging` y `whatsapp_business_management`.
4. Guárdalo: es tu `WHATSAPP_ACCESS_TOKEN`.

## Paso 4: App Secret

En la app: **Configuración de la app → Básica → Clave secreta de la app →
Mostrar**. Es tu `WHATSAPP_APP_SECRET`. OpenJarvis lo usa para verificar que
los mensajes vienen de verdad de Meta; sin él los rechaza.

## Paso 5: variables en Railway

En el servicio de la persona:

| Variable | Valor |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | token permanente del paso 3 |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone number ID del paso 2 |
| `WHATSAPP_APP_SECRET` | clave secreta del paso 4 |
| `WHATSAPP_VERIFY_TOKEN` | una cadena aleatoria inventada por ti |

## Paso 6: webhook

1. En la app: **WhatsApp → Configuración → Webhook → Editar**.
2. **URL de devolución de llamada**: `https://<tu-instancia>.up.railway.app/webhooks/whatsapp`
3. **Token de verificación**: el mismo valor que `WHATSAPP_VERIFY_TOKEN`.
4. **Verificar y guardar**. Meta llama a la URL y OpenJarvis responde si el
   token coincide (la instancia tiene que estar desplegada).
5. En **Campos del webhook**, suscríbete a `messages`.

## Paso 7: número real (producción)

1. **WhatsApp → Configuración de la API → Añadir número de teléfono**.
2. Usa un número que **no** esté registrado en la app de WhatsApp (si lo
   está, bórralo antes de la app). Sirve un fijo: la verificación puede ser
   por llamada.
3. Define nombre visible y verifica con el código.
4. Añade un método de pago en la cartera (solo se cobra por conversaciones
   iniciadas por el negocio).
5. Cambia `WHATSAPP_PHONE_NUMBER_ID` al ID del número nuevo.

## Paso 8: cambios de código pendientes en este fork

El canal de Meta (`whatsapp`) existe en OpenJarvis, pero antes de usarlo en
producción hay que hacer en este fork:

- [ ] Lista blanca de números (`WHATSAPP_ALLOWED_NUMBERS`) también para el
      canal de Meta. Hoy contestaría a **cualquiera** que escriba al número.
- [ ] Opción `WHATSAPP_PROVIDER=meta|baileys` en `bootstrap_config.py` para
      elegir el canal por variable.
- [ ] Solo procesa texto: audios e imágenes se ignoran (igual que Baileys).

## Comprobación rápida

```bash
# Verificación del webhook (debe devolver "hola")
curl "https://<instancia>/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=<VERIFY_TOKEN>&hub.challenge=hola"
```

Después escribe al número del asistente desde un número autorizado y mira
`railway logs --service jarvis-<nombre>`.
