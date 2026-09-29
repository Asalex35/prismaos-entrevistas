# PrismaOS · Entrevistas

Sistema para entrevistar a cada área y a cada persona de Prismaticoos, entender a fondo cómo trabaja la empresa y alimentar PrismaOS, el asistente de IA y las herramientas para hacer más eficientes los flujos.

## Cómo se usa

1. Doble clic en **Abrir Entrevistas PrismaOS.command**. Se abre el tablero (`http://localhost:8790/admin`).
2. El **PIN del tablero** está en `datos/acceso.json` (puedes cambiarlo y reiniciar).
3. En el tablero, cada persona del organigrama (74) tiene su **link personal**: botones **WhatsApp** / **Mensaje**, o abre a la persona y que escanee su QR desde tu pantalla.
4. Cada persona contesta desde su celular: preguntas comunes, las de su área, las de su puesto, las pensadas para ella y las de liderazgo si tiene gente a cargo. Se guarda solo; puede pausar y seguir con el mismo link.
5. **Seguir preguntando:** en el tablero abre a la persona → "Generar preguntas de seguimiento con IA" (o escríbelas a mano) → Enviar. Le aparecen la próxima vez que abra su link.
6. **Documento para mi chat:** botón amarillo del tablero. Descarga un solo archivo con el contexto de la empresa, el organigrama y todas las respuestas, listo para cargar en otro chat (Proyecto de Claude, GPT personalizado, etc.). También se guarda solo en `datos/DOCUMENTO_PRISMATICOOS_para_chat.md`.
7. **Detener entrevistas.command** apaga el servidor.

## Reglas de acceso (config/ajustes.json)

- `link_general: false` → solo se entra con el link personal; nadie puede contestar por otra persona.
- `solo_red_local: true` → solo funciona para equipos conectados a la red Wi-Fi de la oficina.
- `ia_repreguntas_en_vivo: false` y `resumen_automatico: false` → **las entrevistas no gastan tokens**. El equipo solo responde; la IA de tu cuenta solo se usa cuando tú aprietas "Generar preguntas de seguimiento" o "Generar resumen" en el tablero.

La Mac debe estar encendida mientras el equipo contesta (el servidor evita que se duerma).

## IA (suscripción de Claude, sin API)

Los botones de IA del tablero usan `claude -p` con la cuenta de Claude de esta Mac. Si dice "sin conexión", abre Terminal y ejecuta `claude auth login`, luego toca **Probar IA**.

## Conexión con Claude (Claude Code)

- Todo queda en `datos/`: un expediente por persona en `datos/personas/`, un consolidado en `datos/EXPORT_entrevistas.md` y `.json`, y el documento para chat. Claude lo lee desde ahí.
- Preguntas: `config/preguntas.json` (comunes, por área y por puesto) y `config/preguntas_personales.json` (por persona). Organigrama: `config/personas.json`. Contexto que ya sabemos: `config/contexto/`.

## Privacidad

Las respuestas pueden incluir información interna. El tablero pide PIN y los links personales son privados. A cada persona se le pide no escribir contraseñas ni datos bancarios.

## En línea (Render) · entrevistas.orkestalabs.com

El mismo `servidor.py` corre en la Mac o en Render; cambia solo por variables de entorno (ver `render.yaml`):

| Variable | Para qué |
|---|---|
| `PRISMA_DATOS` | Carpeta de datos. En Render: `/var/data/datos` (disco persistente). |
| `PRISMA_DETRAS_DE_PROXY=1` | Toma la IP real del visitante de los encabezados del proxy. |
| `PRISMA_URL_PUBLICA` | Dominio que usan los links de WhatsApp y QR del tablero. |
| `PRISMA_AJUSTE_<AJUSTE>=0/1` | Gana sobre `config/ajustes.json` (en Render: `SOLO_RED_LOCAL=0`, `PEDIR_CODIGO=1`). |
| `ANTHROPIC_API_KEY` | Con llave, la IA del tablero usa la API de Anthropic; sin llave, `claude -p` de la Mac. |

**Los datos nunca van al repositorio** (`datos/` está en `.gitignore`). En Render viven en el disco; respáldalos.

El PIN del tablero se bloquea 10 minutos tras 10 intentos fallidos por IP. En internet conviene un PIN largo (edita `pin_admin` en `datos/acceso.json` y reinicia).

### Mudanza de la Mac a Render (sin perder respuestas)

1. Render funcionando y probado con datos de prueba; la Mac sigue atendiendo.
2. Ventana de ~5 min: detener la Mac → copiar `datos/` completo al disco de Render (`/var/data/datos`) → reiniciar el servicio → verificar mismas personas y respuestas.
3. En la Mac, en el puerto 8790, correr `mudanza/redirector.py`: los links viejos (`10.10.20.30:8790/p/...`) llevan a la misma entrevista en el dominio nuevo.
4. Si algo falla: apagar el redirector y volver a abrir la app en la Mac; sus datos quedan intactos.
