#!/usr/bin/env python3
"""PrismaOS · Entrevistas — servidor local (solo biblioteca estándar de Python).

Cada persona del organigrama tiene un link personal. El entrevistador hace las
preguntas de su área, repregunta con IA (Claude por suscripción, vía `claude -p`)
cuando la respuesta queda corta, y guarda todo en `datos/`. El tablero (/admin)
muestra el avance por área y permite mandar rondas de preguntas nuevas.
"""
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'config')
# En Render los datos viven en el disco persistente (PRISMA_DATOS=/var/data/datos); en la Mac, junto al código.
DAT = os.environ.get('PRISMA_DATOS') or os.path.join(BASE, 'datos')
WEB = os.path.join(BASE, 'web')
PER = os.path.join(DAT, 'personas')
PUERTO = int(os.environ.get('PORT') or os.environ.get('PRISMA_PUERTO', '8790'))
# Detrás del proxy de Render la IP real del visitante viene en los encabezados, no en la conexión.
DETRAS_DE_PROXY = os.environ.get('PRISMA_DETRAS_DE_PROXY') == '1'
URL_PUBLICA = os.environ.get('PRISMA_URL_PUBLICA', '').strip().rstrip('/') or None
# Con ANTHROPIC_API_KEY la IA usa la API de Anthropic (Render); sin ella, `claude -p` con la suscripción (Mac).
USAR_API = bool(os.environ.get('ANTHROPIC_API_KEY'))
MODELOS_API = {'haiku': 'claude-haiku-4-5', 'sonnet': 'claude-sonnet-5', 'opus': 'claude-opus-5'}
MAX_REPREGUNTAS = 8          # repreguntas de IA por persona en la entrevista base
LOCK = threading.RLock()
IA_SEM = threading.BoundedSemaphore(2)
IA = {'ok': None, 'mensaje': 'Sin probar', 'probado': None}
CLAUDE = shutil.which('claude') or '/opt/homebrew/bin/claude'
SISTEMA_IA = ('Eres el asistente de entrevistas de PrismaOS, la app interna de Prismaticoos (productora mexicana de '
              'melodramas y microdramas). Tu trabajo es analizar entrevistas al equipo y responder EXACTAMENTE en el '
              'formato que se te pide (normalmente JSON), en español, sin texto adicional y sin usar herramientas.')

os.makedirs(PER, exist_ok=True)


# ---------------------------------------------------------------- utilidades
def ahora():
    return datetime.now().isoformat(timespec='seconds')


def leer(ruta, defecto=None):
    try:
        with open(ruta, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def escribir(ruta, datos):
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


def slug(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-') or 'persona'


def extraer_json(texto):
    if not texto:
        return None
    t = texto.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', t, re.S)
    if m:
        t = m.group(1).strip()
    for a, b in (('{', '}'), ('[', ']')):
        i, j = t.find(a), t.rfind(b)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                pass
    return None


# ------------------------------------------------------------- configuración
def ajustes():
    a = leer(os.path.join(CFG, 'ajustes.json'), {}) or {}
    # Variables de entorno PRISMA_AJUSTE_<nombre>=0/1 ganan sobre ajustes.json (ej. solo_red_local=0 en Render).
    for k in ('ia_repreguntas_en_vivo', 'resumen_automatico', 'link_general', 'pedir_codigo', 'solo_red_local'):
        v = os.environ.get(f'PRISMA_AJUSTE_{k.upper()}')
        if v in ('0', '1'):
            a[k] = v == '1'
    return {'ia_repreguntas_en_vivo': bool(a.get('ia_repreguntas_en_vivo', False)),
            'resumen_automatico': bool(a.get('resumen_automatico', False)),
            'link_general': bool(a.get('link_general', False)),
            'pedir_codigo': bool(a.get('pedir_codigo', False)),
            'link_datos_personales': str(a.get('link_datos_personales', '') or ''),
            'solo_red_local': bool(a.get('solo_red_local', True))}


def ip_privada(ip):
    ip = (ip or '').replace('::ffff:', '')
    if ip in ('127.0.0.1', '::1', 'localhost'):
        return True
    partes = ip.split('.')
    if len(partes) != 4 or not all(x.isdigit() for x in partes):
        return ip.startswith('fe80:') or ip.startswith('fd') or ip.startswith('fc')
    a, b = int(partes[0]), int(partes[1])
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254)


def acceso():
    ruta = os.path.join(DAT, 'acceso.json')
    a = leer(ruta)
    if not a:
        palabras = ['lente', 'claqueta', 'toma', 'escena', 'corte', 'foco', 'guion', 'set']
        a = {
            'pin_admin': f'{secrets.randbelow(900000) + 100000}',
            'codigo_equipo': f'{secrets.choice(palabras)}{secrets.randbelow(90) + 10}',
            'nota': 'pin_admin abre el tablero (/admin). codigo_equipo lo usa el equipo en el link general. Puedes cambiarlos aquí y reiniciar.',
        }
        escribir(ruta, a)
    return a


def personas():
    base = leer(os.path.join(CFG, 'personas.json'), {'personas': []})['personas']
    extra = leer(os.path.join(DAT, 'personas_extra.json'), [])
    return base + extra


def persona(pid):
    return next((p for p in personas() if p['id'] == pid), None)


def tokens():
    ruta = os.path.join(DAT, 'tokens.json')
    with LOCK:
        t = leer(ruta, {})
        cambio = False
        for p in personas():
            if p['id'] not in t:
                t[p['id']] = secrets.token_urlsafe(9)
                cambio = True
        if cambio:
            escribir(ruta, t)
    return t


def pid_por_token(tok):
    if not tok:
        return None
    for pid, t in tokens().items():
        if hmac.compare_digest(t, tok):
            return pid
    return None


def banco():
    return leer(os.path.join(CFG, 'preguntas.json'))


def personales():
    """Preguntas escritas para cada persona (config/preguntas_personales.json)."""
    return leer(os.path.join(CFG, 'preguntas_personales.json'), {}) or {}


def preguntas_base(p):
    b = banco()
    omitir = set(b['areas'].get(p['area'], {}).get('omitir', []))
    lista = [q for q in b['comunes_inicio'] if q['id'] not in omitir]
    lista += b.get('comunes_admin', [])
    lista += b['por_area'].get(p['area'], [])
    lista += b.get('por_cargo', {}).get(p.get('rol', ''), [])
    lista += personales().get('por_persona', {}).get(p['id'], [])
    lista += [q for q in b['comunes_flujo'] if q['id'] not in omitir]
    if p.get('lider'):
        lista += b['lideres']
    lista += [q for q in b['comunes_cierre'] if q['id'] not in omitir]
    # Preguntas "Para conocerte": una al inicio y una pausa cada 5 preguntas de trabajo
    ligeras = b.get('para_conocerte', [])
    if not ligeras:
        return lista
    cumple = [q for q in ligeras if q['id'] == ligeras[-1]['id']]
    resto = ligeras[1:-1]
    final = [ligeras[0]]
    k = 0
    for i, q in enumerate(lista):
        final.append(q)
        if (i + 1) % 5 == 0 and k < len(resto) and i < len(lista) - 1:
            final.append(resto[k])
            k += 1
    return final[:-1] + cumple + final[-1:] if len(final) > 1 else final + cumple


def nombre_area(area):
    return banco()['areas'].get(area, {}).get('nombre', area)


def contexto(area):
    partes = []
    for nombre in ('general.md', f'{area}.md'):
        try:
            with open(os.path.join(CFG, 'contexto', nombre), encoding='utf-8') as f:
                partes.append(f.read())
        except FileNotFoundError:
            pass
    return '\n\n'.join(partes)[:6000]


# ------------------------------------------------------------- expedientes
def ruta_exp(pid):
    return os.path.join(PER, f'{pid}.json')


def expediente(pid):
    e = leer(ruta_exp(pid))
    if not e:
        e = {'id': pid, 'respuestas': [], 'cola_seguimiento': [], 'pendientes': [],
             'resumen': None, 'resumen_fecha': None, 'repreguntas_ia': 0,
             'iniciado': None, 'actualizado': None}
    return e


def guardar_exp(e):
    e['actualizado'] = ahora()
    escribir(ruta_exp(e['id']), e)


def contestadas(e):
    return {r['qid'] for r in e['respuestas']}


def siguiente(p, e):
    hechas = contestadas(e)
    if e['cola_seguimiento']:
        q = e['cola_seguimiento'][0]
        return {'qid': q['qid'], 'texto': q['texto'], 'ayuda': q.get('ayuda', ''), 'tipo': 'seguimiento'}
    for q in preguntas_base(p):
        if q['id'] not in hechas:
            return {'qid': q['id'], 'texto': q['texto'], 'ayuda': q.get('ayuda', ''),
                    'tipo': 'personal' if q['id'].startswith('P-') else 'conocerte' if q.get('tipo') == 'ligera' else 'base',
                    'formato': q.get('tipo', 'texto'), 'opciones': q.get('opciones', [])}
    for q in e['pendientes']:
        if q['qid'] not in hechas:
            return {'qid': q['qid'], 'texto': q['texto'], 'ayuda': q.get('porque_publico', ''), 'tipo': 'ronda'}
    return None


def estado(p, e):
    hechas = contestadas(e)
    base = preguntas_base(p)
    faltan_base = [q for q in base if q['id'] not in hechas]
    faltan_ronda = [q for q in e['pendientes'] if q['qid'] not in hechas]
    if not e['respuestas']:
        est = 'sin_empezar'
    elif faltan_base or e['cola_seguimiento']:
        est = 'en_curso'
    elif faltan_ronda:
        est = 'preguntas_nuevas'
    else:
        est = 'completa'
    total = len(base)
    return {
        'estado': est,
        'base_total': total,
        'base_hechas': total - len(faltan_base),
        'progreso': round(100 * (total - len(faltan_base)) / total) if total else 0,
        'rondas_pendientes': len(faltan_ronda),
        'respuestas': len(e['respuestas']),
    }


# ------------------------------------------------ datos para Administración (cifrados)
# El celular cifra los datos con la llave PÚBLICA (config/datos_admin_publica.pem) antes de enviarlos.
# Aquí solo se guarda el sobre cifrado; se descifra en la Mac de Administración con la llave privada
# (herramientas/descifrar_datos_admin.py). Cada envío se agrega; nunca se borra uno anterior.
DAD = os.path.join(DAT, 'datos_admin')


def llave_publica_datos():
    try:
        with open(os.path.join(CFG, 'datos_admin_publica.pem'), encoding='ascii') as f:
            return f.read()
    except FileNotFoundError:
        return None


def envios_datos(pid):
    return leer(os.path.join(DAD, f'{pid}.json'), []) or []


# Campos obligatorios (la cuenta es opcional si ponen CLABE). Solo los NOMBRES de lo que falta viajan sin cifrar.
CAMPOS_REQUERIDOS = ('nombre_completo', 'rfc', 'curp', 'nss', 'fecha_nacimiento', 'banco', 'clabe', 'calle_numero',
                     'colonia', 'cp', 'municipio', 'estado', 'emergencia_nombre', 'emergencia_parentesco',
                     'emergencia_telefono')


def estado_datos(pid):
    """None si no ha enviado nada; si no, fecha del último envío y qué campos siguen sin llenar.
    Un campo cuenta como entregado si vino lleno en CUALQUIER envío (al descifrar se juntan todos)."""
    env = envios_datos(pid)
    if not env:
        return None
    faltan = set(CAMPOS_REQUERIDOS)
    for e in env:
        faltan &= set(e.get('faltan', []))  # envíos sin 'faltan' fueron completos
    faltan = [c for c in CAMPOS_REQUERIDOS if c in faltan]
    return {'fecha': env[-1]['fecha'], 'faltan': faltan, 'completo': not faltan}


def guardar_datos_cifrados(pid, sobre, faltan):
    b64 = re.compile(r'[A-Za-z0-9+/=]+')
    partes = {k: sobre.get(k) for k in ('clave', 'iv', 'datos')}
    if sobre.get('v') != 1 or not all(isinstance(v, str) and b64.fullmatch(v) for v in partes.values()):
        return False
    if len(partes['clave']) > 1000 or len(partes['iv']) > 40 or len(partes['datos']) > 20000:
        return False
    if not isinstance(faltan, list) or not set(faltan) <= set(CAMPOS_REQUERIDOS):
        return False
    with LOCK:
        os.makedirs(DAD, exist_ok=True)
        env = envios_datos(pid)
        env.append({'v': 1, **partes, 'faltan': [c for c in CAMPOS_REQUERIDOS if c in faltan], 'fecha': ahora()})
        escribir(os.path.join(DAD, f'{pid}.json'), env)
    return True


def vista_sesion(pid):
    p = persona(pid)
    e = expediente(pid)
    st = estado(p, e)
    return {
        'persona': {'id': p['id'], 'nombre': p['nombre'], 'cargo': p['cargo'], 'area': p['area'],
                    'area_nombre': nombre_area(p['area']), 'equipo': p.get('equipo', '')},
        **st,
        'pregunta': siguiente(p, e),
        'historial': [{'i': i, 'pregunta': r['pregunta'], 'respuesta': r['respuesta'], 'saltada': r.get('saltada', False),
                       'tipo': r['tipo'], 'formato': r.get('formato', 'texto'), 'seleccion': r.get('seleccion', []),
                       'opciones': (r.get('seleccion', []) + r.get('faltan', [])) if r.get('formato') == 'checklist' else []}
                      for i, r in enumerate(e['respuestas'])][-60:],
        'link_datos': ajustes()['link_datos_personales'],
        'formulario_datos': bool(llave_publica_datos()),
        'datos_estado': estado_datos(pid),
        'cerrada': e.get('cerrada'),
        'ia': bool(IA['ok']) and ajustes()['ia_repreguntas_en_vivo'],
    }


# ------------------------------------------------------------------- IA (Claude)
_CLIENTE_API = None


def _llamar_api(prompt, modelo, timeout):
    """Llama a Claude por la API de Anthropic (llave en ANTHROPIC_API_KEY). Devuelve texto o None."""
    global _CLIENTE_API
    import anthropic
    if _CLIENTE_API is None:
        _CLIENTE_API = anthropic.Anthropic()
    try:
        r = _CLIENTE_API.with_options(timeout=timeout).messages.create(
            model=MODELOS_API.get(modelo, modelo), max_tokens=16000,
            system=SISTEMA_IA, messages=[{'role': 'user', 'content': prompt}])
    except anthropic.AuthenticationError:
        IA.update(ok=False, mensaje='La llave de API no es válida (ANTHROPIC_API_KEY).', probado=ahora())
        return None
    except anthropic.PermissionDeniedError:
        IA.update(ok=False, mensaje='La llave de API no tiene permiso para este modelo.', probado=ahora())
        return None
    except anthropic.RateLimitError:
        IA.update(mensaje='Límite de uso de la API alcanzado; intenta en un minuto.', probado=ahora())
        return None
    except anthropic.APIStatusError as ex:
        IA.update(mensaje=f'Error de la API ({ex.status_code}): {ex.message}'[:300], probado=ahora())
        return None
    except anthropic.APIConnectionError:
        IA.update(mensaje='Sin conexión con la API de Anthropic.', probado=ahora())
        return None
    if r.stop_reason == 'refusal':
        IA.update(ok=True, mensaje='Conectado (la última petición fue rechazada por el modelo)', probado=ahora())
        return None
    texto = ''.join(b.text for b in r.content if b.type == 'text').strip()
    IA.update(ok=True, mensaje='Conectado (API de Anthropic)', probado=ahora())
    return texto or None


def llamar_ia(prompt, modelo='haiku', timeout=90, espera=2.0):
    """Llama a Claude (API si hay llave; si no, la suscripción vía `claude -p`). Devuelve texto o None."""
    if IA['ok'] is False:
        return None
    if not IA_SEM.acquire(timeout=espera):
        return None
    if USAR_API:
        try:
            return _llamar_api(prompt, modelo, timeout)
        finally:
            IA_SEM.release()
    try:
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('CLAUDE_CODE') or k in ('CLAUDECODE', 'ANTHROPIC_BASE_URL'))}
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [CLAUDE, '-p', prompt, '--model', modelo, '--output-format', 'json',
                 '--system-prompt', SISTEMA_IA,
                 '--disallowedTools', 'Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task'],
                capture_output=True, text=True, timeout=timeout, env=env, cwd=tmp)
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            d = {}
        if d.get('is_error') or not d.get('result'):
            msg = d.get('result') or (r.stderr or '').strip()[:300] or 'Sin respuesta de Claude'
            IA.update(ok=False, mensaje=msg, probado=ahora())
            return None
        IA.update(ok=True, mensaje='Conectado', probado=ahora())
        return d['result']
    except (subprocess.TimeoutExpired, OSError) as ex:
        IA.update(mensaje=f'Error: {ex}', probado=ahora())
        return None
    finally:
        IA_SEM.release()


def probar_ia():
    IA['ok'] = None
    res = llamar_ia('Responde exactamente: OK', timeout=60, espera=10)
    if res is None and IA['ok'] is not False:
        IA['ok'] = False
    return dict(IA)


def ultimas_qa(e, n=8, recorte=500):
    salida = []
    for r in e['respuestas'][-n:]:
        salida.append(f"P: {r['pregunta']}\nR: {'(no aplica / no sabe)' if r.get('saltada') else r['respuesta'][:recorte]}")
    return '\n\n'.join(salida)


def ia_repregunta(p, e, pregunta, respuesta):
    proximas = []
    hechas = contestadas(e)
    for q in preguntas_base(p):
        if q['id'] not in hechas:
            proximas.append(q['texto'])
        if len(proximas) >= 3:
            break
    prompt = f"""Eres el entrevistador de PrismaOS, la app interna de Prismaticoos (productora mexicana de melodramas y microdramas; plataforma PRISMA+). Entrevistas a {p['nombre']} ({p['cargo']}, área {nombre_area(p['area'])}{', ' + p['equipo'] if p.get('equipo') else ''}) para documentar a fondo cómo trabaja la empresa y que PrismaOS refleje el trabajo real de cada área.

LO QUE YA SABEMOS (no preguntes lo que ya está aquí):
{contexto(p['area'])}

LO ÚLTIMO QUE RESPONDIÓ ESTA PERSONA:
{ultimas_qa(e, 5, 300)}

PREGUNTA ACTUAL: {pregunta}
RESPUESTA: {respuesta[:2500]}

PRÓXIMAS PREGUNTAS DE LA LISTA (no las adelantes): {' | '.join(proximas) or 'ninguna'}

Decide si vale la pena UNA repregunta para aclarar o profundizar. Repregunta solo si la respuesta es vaga, menciona un proceso sin sus pasos, un problema sin ejemplo, una herramienta sin decir para qué, una cifra sin unidad o tiempo, o nombra algo que conviene precisar (quién, cuándo, dónde, con qué). No repreguntes si ya es clara y completa.


IMPORTANTE: en lo que le preguntes a la persona nunca menciones automatizar, que una herramienta o la IA haga su trabajo, ahorrar personal, reemplazos ni sustituciones. Enfócate en entender su trabajo, su experiencia y criterio, y en qué le facilitaría el día a día o la coordinación con otras áreas.
Responde SOLO con JSON: {{"repreguntar": true o false, "pregunta": "una sola pregunta concreta, breve y cálida, en español de México, tuteando"}}"""
    d = extraer_json(llamar_ia(prompt, 'haiku', timeout=60, espera=1.5))
    if isinstance(d, dict) and d.get('repreguntar') and isinstance(d.get('pregunta'), str) and len(d['pregunta']) > 8:
        return d['pregunta'].strip()
    return None


def transcripcion(e):
    return '\n\n'.join(
        f"[{r['tipo']}] P: {r['pregunta']}\nR: {'(no aplica / no sabe)' if r.get('saltada') else r['respuesta']}"
        for r in e['respuestas'] if r.get('formato') != 'checklist' and r['tipo'] != 'conocerte')


def ia_resumen(pid):
    p = persona(pid)
    with LOCK:
        e = expediente(pid)
    if not e['respuestas']:
        return None
    prompt = f"""Resume la entrevista de {p['nombre']} ({p['cargo']}, área {nombre_area(p['area'])}{', ' + p['equipo'] if p.get('equipo') else ''}) de Prismaticoos para la base de conocimiento de PrismaOS. Usa SOLO lo que la persona dijo; no inventes. Español.

ENTREVISTA:
{transcripcion(e)[:45000]}

Responde SOLO con JSON con esta forma (listas vacías si no aplica):
{{"rol": "1-2 frases", "responsabilidades": [], "reporta_a": "", "trabaja_con": [], "recibe": [], "entrega": [], "herramientas": [{{"nombre": "", "uso": ""}}], "procesos": [{{"nombre": "", "pasos": []}}], "tiempos_y_metas": [], "aprobaciones": [], "problemas": [{{"problema": "", "ejemplo": "", "impacto": ""}}], "ideas_de_mejora": [{{"idea": "", "impacto": ""}}], "preguntas_frecuentes": [{{"pregunta": "", "respuesta": ""}}], "glosario": [{{"termino": "", "definicion": ""}}], "datos_sensibles_mencionados": [], "dudas_para_seguir": []}}"""
    d = extraer_json(llamar_ia(prompt, 'sonnet', timeout=240, espera=60))
    if isinstance(d, dict):
        with LOCK:
            e = expediente(pid)
            e['resumen'] = d
            e['resumen_fecha'] = ahora()
            guardar_exp(e)
        exportar()
        return d
    return None


def ia_seguimiento(pid):
    p = persona(pid)
    e = expediente(pid)
    colegas = []
    for q in personas():
        if q['area'] == p['area'] and q['id'] != pid:
            r = expediente(q['id']).get('resumen')
            if r:
                colegas.append(f"- {q['nombre']} ({q['cargo']}): rol={r.get('rol', '')}; procesos={json.dumps(r.get('procesos', []), ensure_ascii=False)[:600]}; problemas={json.dumps(r.get('problemas', []), ensure_ascii=False)[:400]}")
    prompt = f"""Eres el entrevistador de PrismaOS (Prismaticoos, productora mexicana de melodramas y microdramas). Queremos entender a fondo cómo trabaja {p['nombre']} ({p['cargo']}, área {nombre_area(p['area'])}) para que PrismaOS y el asistente interno de la empresa no tengan dudas.

LO QUE YA SABEMOS DE LA EMPRESA:
{contexto(p['area'])}

LO QUE DIJERON SUS COMPAÑEROS DE ÁREA:
{chr(10).join(colegas)[:6000] or '(nadie más ha respondido todavía)'}

SU ENTREVISTA HASTA AHORA:
{transcripcion(e)[:30000]}

Propón entre 3 y 6 preguntas de seguimiento para ESTA persona que llenen huecos importantes: pasos que faltan de un proceso, cifras (tiempos, cantidades, costos), nombres de archivos o carpetas, quién aprueba, contradicciones con sus compañeros, problemas sin ejemplo o ideas de mejora sin detalle.

IMPORTANTE: en lo que le preguntes a la persona nunca menciones automatizar, que una herramienta o la IA haga su trabajo, ahorrar personal, reemplazos ni sustituciones. Enfócate en entender su trabajo, su experiencia y criterio, y en qué le facilitaría el día a día o la coordinación con otras áreas. No repitas lo que ya contestó. No atribuyas datos a ninguna persona salvo que aparezcan textualmente en las respuestas de arriba (lo de "lo que ya sabemos" es contexto de la empresa, no lo dijo nadie en particular). Tono cálido, tuteando, español de México.
Responde SOLO con JSON: {{"preguntas": [{{"pregunta": "", "porque": "qué hueco llena (para el tablero)"}}]}}"""
    d = extraer_json(llamar_ia(prompt, 'sonnet', timeout=180, espera=30))
    if isinstance(d, dict) and isinstance(d.get('preguntas'), list):
        return [q for q in d['preguntas'] if isinstance(q, dict) and q.get('pregunta')]
    return None


# ------------------------------------------------------------------- exportar
_EXPORT_LOCK = threading.Lock()


def exportar():
    """Regenera datos/EXPORT_entrevistas.md y .json (lo que lee Claude)."""
    with _EXPORT_LOCK:
        todo = []
        for p in personas():
            e = expediente(p['id'])
            todo.append({'persona': p, 'estado': estado(p, e), 'expediente': e})
        escribir(os.path.join(DAT, 'EXPORT_entrevistas.json'), {'generado': ahora(), 'personas': todo})
        lineas = [f'# Entrevistas PrismaOS — exportado {ahora()}', '']
        areas = banco()['areas']
        for area, info in areas.items():
            grupo = [t for t in todo if t['persona']['area'] == area]
            if not grupo:
                continue
            lineas += [f"## {info['nombre']}", '']
            for t in grupo:
                p, e, st = t['persona'], t['expediente'], t['estado']
                lineas.append(f"### {p['nombre']} — {p['cargo']}{' · ' + p['equipo'] if p.get('equipo') else ''} ({st['estado']}, {st['progreso']}%)")
                for r in e['respuestas']:
                    etiqueta = {'seguimiento': ' [repregunta]', 'ronda': ' [ronda nueva]', 'personal': ' [personal]', 'conocerte': ' [para conocerte]'}.get(r['tipo'], '')
                    lineas.append(f"- **P{etiqueta}:** {r['pregunta']}")
                    lineas.append(f"  **R:** {'(no aplica / no sabe)' if r.get('saltada') else r['respuesta']}")
                if e.get('resumen'):
                    lineas.append(f"\n<details><summary>Resumen IA ({e.get('resumen_fecha')})</summary>\n\n```json\n{json.dumps(e['resumen'], ensure_ascii=False, indent=1)}\n```\n</details>")
                lineas.append('')
        with open(os.path.join(DAT, 'EXPORT_entrevistas.md'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lineas))
        with open(os.path.join(DAT, 'DOCUMENTO_PRISMATICOOS_para_chat.md'), 'w', encoding='utf-8') as f:
            f.write(documento())


def documento():
    """Documento único para cargar en otro chat: contexto + organigrama + respuestas. No usa IA."""
    ps = personas()
    areas = banco()['areas']
    L = ['# PRISMATICOOS — Documento de conocimiento para asistente de IA', '',
         f'_Generado automáticamente por PrismaOS · Entrevistas el {ahora()[:16].replace("T", " ")}. Fuente: entrevistas al equipo + contexto documentado._', '',
         '## Cómo usar este documento', '',
         'Eres el asistente interno de Prismaticoos. Usa SOLO la información de este documento para responder sobre la empresa, su gente, sus áreas, procesos, herramientas y proyectos. '
         'Las respuestas de las entrevistas son palabras textuales de cada persona sobre su propio trabajo: cuando dos personas describan algo distinto, menciona ambas versiones y quién lo dijo. '
         'Si algo no está aquí, dilo y sugiere a quién preguntar según el organigrama. No inventes datos. No compartas información marcada como interna fuera del equipo.', '']
    base_kb = os.path.join(CFG, 'contexto', 'base_conocimiento.md')
    for ruta, titulo in ((base_kb, None), (os.path.join(CFG, 'contexto', 'general.md'), None)):
        if os.path.exists(ruta):
            with open(ruta, encoding='utf-8') as f:
                texto = f.read().strip()
            L += [re.sub(r'^# ', '## ', texto, count=1), '']
    L += ['## Organigrama 2026', '', '| Persona | Puesto | Área | Equipo |', '|---|---|---|---|']
    for p in ps:
        L.append(f"| {p['nombre']}{' (ext.)' if p.get('externo') else ''} | {p['cargo']} | {areas.get(p['area'], {}).get('nombre', p['area'])} | {p.get('equipo') or '—'} |")
    L.append('')
    total_resp = 0
    for area, info in areas.items():
        grupo = [p for p in ps if p['area'] == area]
        bloques = []
        for p in grupo:
            e = expediente(p['id'])
            utiles = [r for r in e['respuestas'] if not r.get('saltada') and r['respuesta'].strip() and r.get('formato') != 'checklist' and r['tipo'] != 'conocerte']
            if not utiles:
                continue
            total_resp += len(utiles)
            bloques.append(f"### {p['nombre']} — {p['cargo']}{' · ' + p['equipo'] if p.get('equipo') else ''}")
            bloques.append('')
            for r in utiles:
                bloques.append(f"**{r['pregunta']}**")
                bloques.append(r['respuesta'].strip())
                bloques.append('')
            if e.get('resumen'):
                bloques.append('Resumen estructurado (IA):')
                bloques.append('```json')
                bloques.append(json.dumps(e['resumen'], ensure_ascii=False, indent=1))
                bloques.append('```')
                bloques.append('')
        if bloques:
            L += [f"## Área: {info['nombre']}", ''] + bloques
    conoce = []
    for p in ps:
        ks = [r for r in expediente(p['id'])['respuestas'] if r['tipo'] == 'conocerte' and not r.get('saltada') and r['respuesta'].strip()]
        if ks:
            conoce.append(f"### {p['nombre']} — {p['cargo']}")
            conoce += [f"- {r['pregunta']} {r['respuesta'].strip()}" for r in ks]
            conoce.append('')
    if conoce:
        L += ['## Conoce al equipo', '', '_Respuestas opcionales que cada persona quiso compartir._', ''] + conoce
    faltan = [p['nombre'] for p in ps if not expediente(p['id'])['respuestas']]
    L += ['## Estado de las entrevistas', '', f'- Respuestas incluidas: {total_resp}',
          f"- Personas que aún no responden ({len(faltan)}): {', '.join(faltan) if faltan else 'ninguna'}", '']
    return '\n'.join(L)


def exportar_async():
    threading.Thread(target=exportar, daemon=True).start()


# ------------------------------------------------------------------- acciones
def responder(pid, qid, texto, saltar, seleccion=None):
    p = persona(pid)
    with LOCK:
        e = expediente(pid)
        if e.get('cerrada'):
            return vista_sesion(pid)          # entrevista cerrada: no se registra nada más
        q = siguiente(p, e)
        if not q or q['qid'] != qid:
            return vista_sesion(pid)          # doble envío o pregunta vieja: no duplicar
        if not e['iniciado']:
            e['iniciado'] = ahora()
        registro = {'qid': qid, 'tipo': q['tipo'], 'pregunta': q['texto'],
                    'respuesta': '' if saltar else texto.strip(), 'saltada': bool(saltar), 'fecha': ahora()}
        if q.get('formato') == 'checklist':
            txt, sel, faltan = texto_checklist(q, seleccion or [])
            registro.update(respuesta=txt, saltada=False, formato='checklist', seleccion=sel, faltan=faltan)
        e['respuestas'].append(registro)
        if q['tipo'] == 'seguimiento':
            e['cola_seguimiento'].pop(0)
        guardar_exp(e)
    if q['tipo'] in ('base', 'personal') and q.get('formato') != 'checklist' and not saltar and len(texto.strip()) >= 20 and e['repreguntas_ia'] < MAX_REPREGUNTAS and IA['ok'] and ajustes()['ia_repreguntas_en_vivo']:
        nueva = ia_repregunta(p, e, q['texto'], texto)
        if nueva:
            with LOCK:
                e = expediente(pid)
                e['cola_seguimiento'].append({'qid': f"{qid}-S{len(e['respuestas'])}", 'texto': nueva, 'padre': qid})
                e['repreguntas_ia'] += 1
                guardar_exp(e)
    with LOCK:
        e = expediente(pid)
        if siguiente(p, e) is None and not e.get('cerrada'):
            e['cerrada'] = ahora()             # terminó: se cierra el acceso a su nombre
            guardar_exp(e)
        termino = siguiente(p, e) is None or (siguiente(p, e)['tipo'] == 'ronda' and q['tipo'] != 'ronda')
    if termino and IA['ok'] and ajustes()['resumen_automatico']:
        threading.Thread(target=ia_resumen, args=(pid,), daemon=True).start()
    exportar_async()
    return vista_sesion(pid)


def agregar_pendientes(pid, preguntas_nuevas, origen):
    reabrir(pid)                               # enviar preguntas nuevas reabre el acceso
    with LOCK:
        e = expediente(pid)
        for i, q in enumerate(preguntas_nuevas):
            texto = q['pregunta'] if isinstance(q, dict) else str(q)
            texto = texto.strip()
            if not texto:
                continue
            e['pendientes'].append({'qid': f"R{len(e['pendientes']) + 1:02d}-{secrets.token_hex(2)}", 'texto': texto,
                                    'porque': (q.get('porque', '') if isinstance(q, dict) else ''),
                                    'origen': origen, 'creado': ahora()})
        guardar_exp(e)
    exportar_async()


PATRONES_SENSIBLES = [
    re.compile(r'\b[A-ZÑ&]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b', re.I),            # CURP
    re.compile(r'\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b', re.I),                      # RFC con homoclave
    re.compile(r'(?<![\d:.,])\d{11}(?![\d:.,])'),                                # NSS
    re.compile(r'(?<![\d:.,])\d{16}(?![\d:.,])'),                                # tarjeta
    re.compile(r'(?<![\d:.,])\d{18}(?![\d:.,])'),                                # CLABE
    re.compile(r'(?<![\d])\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?![\d])'),          # tarjeta con espacios
    re.compile(r'(?<![\d])\d{3}[ -]?\d{3}[ -]?\d{11}[ -]?\d(?![\d])'),         # CLABE con espacios
]


def tiene_dato_personal(texto):
    return any(p.search(texto or '') for p in PATRONES_SENSIBLES)


AVISO_SENSIBLE = ('Parece que escribiste un dato personal (RFC, CURP, NSS, tarjeta, cuenta o CLABE). Por seguridad no lo guardamos '
                  'aquí: entrégalo a Administración o captúralo en el sistema. Si no era un dato personal, escríbelo de otra forma.')


def texto_checklist(q, seleccion):
    opciones = q.get('opciones', [])
    sel = [o for o in opciones if o in seleccion]
    faltan = [o for o in opciones if o not in sel]
    return f"Entregados: {', '.join(sel) or 'ninguno'} | Faltan: {', '.join(faltan) or 'ninguno'}", sel, faltan


def pregunta_por_id(p, qid):
    return next((q for q in preguntas_base(p) if q['id'] == qid), None)


def editar_respuesta(pid, indice, texto, seleccion=None):
    with LOCK:
        e = expediente(pid)
        if not (0 <= indice < len(e['respuestas'])):
            return False
        r = e['respuestas'][indice]
        if r.get('formato') == 'checklist':
            q = pregunta_por_id(persona(pid), r['qid']) or {'opciones': r.get('seleccion', []) + r.get('faltan', [])}
            txt, sel, faltan = texto_checklist(q, seleccion or [])
            r.update(respuesta=txt, seleccion=sel, faltan=faltan, editada=ahora())
            guardar_exp(e)
        elif r['respuesta'] != texto.strip():
            r.setdefault('versiones', []).append({'respuesta': r['respuesta'], 'fecha': r.get('fecha')})
            r['respuesta'] = texto.strip()
            r['saltada'] = False if texto.strip() else r.get('saltada', False)
            r['editada'] = ahora()
            guardar_exp(e)
    exportar_async()
    return True


from difflib import SequenceMatcher

_INTENTOS = {}


def demasiados_intentos(ip, limite=40, ventana=600):
    ahora_s = time.time()
    lista = [t for t in _INTENTOS.get(ip, []) if ahora_s - t < ventana]
    lista.append(ahora_s)
    _INTENTOS[ip] = lista
    return len(lista) > limite


_FALLOS_PIN = {}
_FALLOS_LOCK = threading.Lock()


def _recientes(clave, ventana=600):
    ahora_s = time.time()
    lista = [t for t in _FALLOS_PIN.get(clave, []) if ahora_s - t < ventana]
    _FALLOS_PIN[clave] = lista
    return lista


def pin_bloqueado(ip):
    with _FALLOS_LOCK:
        return len(_recientes('ip:' + ip)) >= 10 or len(_recientes('*')) >= 300


def fallo_pin(ip):
    with _FALLOS_LOCK:
        for clave in ('ip:' + ip, '*'):
            _recientes(clave).append(time.time())


def _tokens_nombre(texto):
    t = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode().lower()
    return [w for w in re.findall(r'[a-z]+', t) if len(w) >= 2]


def _parece(a, b):
    return a == b or (len(a) >= 4 and len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.84)


def buscar_persona(texto):
    """Devuelve ('ok', persona) | ('ambiguo'|'incompleto'|'nada', None). Nunca revela otros nombres."""
    q = _tokens_nombre(texto)
    if not q:
        return 'nada', None

    def evaluar(igual):
        completos, parciales = [], []
        for p in personas():
            pt = _tokens_nombre(p['nombre'])
            if all(any(igual(x, y) for y in pt) for x in q):
                coinciden = sum(1 for x in q if any(igual(x, y) for y in pt))
                (completos if coinciden >= 2 or len(pt) == 1 or coinciden == len(pt) else parciales).append(p)
        return completos, parciales

    for igual in ((lambda a, b: a == b), _parece):        # primero exacto, luego tolerante a errores
        completos, parciales = evaluar(igual)
        if len(completos) == 1:
            return 'ok', completos[0]
        if len(completos) > 1:
            return 'ambiguo', None
        if parciales:
            return 'incompleto', None
    return 'nada', None


def reclamos():
    return leer(os.path.join(DAT, 'reclamos.json'), {}) or {}


def reclamar(pid, dispositivo):
    """Liga un nombre al primer celular que lo elige. Devuelve True si este dispositivo puede usarlo."""
    with LOCK:
        r = reclamos()
        actual = r.get(pid)
        if actual and not hmac.compare_digest(actual.get('dispositivo', ''), dispositivo):
            return False
        if not actual:
            r[pid] = {'dispositivo': dispositivo, 'fecha': ahora()}
            escribir(os.path.join(DAT, 'reclamos.json'), r)
        return True


def liberar(pid):
    with LOCK:
        r = reclamos()
        r.pop(pid, None)
        escribir(os.path.join(DAT, 'reclamos.json'), r)


def reabrir(pid):
    with LOCK:
        e = expediente(pid)
        if e.get('cerrada'):
            e.setdefault('reaperturas', []).append({'cerrada': e['cerrada'], 'reabierta': ahora()})
            e['cerrada'] = None
            guardar_exp(e)


def envios():
    return leer(os.path.join(DAT, 'envios.json'), {}) or {}


def marcar_envio(pid):
    with LOCK:
        env = envios()
        reg = env.get(pid, {'veces': 0})
        reg['veces'] = reg.get('veces', 0) + 1
        reg['ultimo'] = ahora()
        env[pid] = reg
        escribir(os.path.join(DAT, 'envios.json'), env)


def telefonos():
    return leer(os.path.join(DAT, 'telefonos.json'), {}) or {}


def normalizar_tel(t):
    d = re.sub(r'\D', '', str(t or ''))
    if len(d) == 10:
        d = '52' + d                      # México: 52 + 10 dígitos
    elif len(d) == 13 and d.startswith('521'):
        d = '52' + d[3:]
    return d if 11 <= len(d) <= 15 else ''


def guardar_tel(pid, tel):
    with LOCK:
        t = telefonos()
        n = normalizar_tel(tel)
        if n:
            t[pid] = n
        else:
            t.pop(pid, None)
        escribir(os.path.join(DAT, 'telefonos.json'), t)
    return n


def _norm(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9 ]+', ' ', s).split()


def importar_telefonos(texto):
    ps = personas()
    ok, fallas = [], []
    for linea in texto.splitlines():
        m = re.search(r'(\+?[\d][\d\s\-().]{8,})', linea)
        if not m:
            continue
        tel = m.group(1)
        nombre = linea.replace(tel, ' ')
        palabras = set(_norm(nombre))
        if not palabras:
            fallas.append(linea.strip()); continue
        mejor, puntos = None, 0
        for p in ps:
            pn = set(_norm(p['nombre']))
            comun = len(palabras & pn)
            if comun > puntos:
                mejor, puntos = p, comun
        if mejor and (puntos >= 2 or (puntos == 1 and len(set(_norm(mejor['nombre']))) == 1)) and guardar_tel(mejor['id'], tel):
            ok.append(mejor['nombre'])
        else:
            fallas.append(linea.strip())
    return {'guardados': ok, 'sin_coincidencia': fallas}


def ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def estado_admin():
    toks = tokens()
    env = envios()
    tels = telefonos()
    recl = reclamos()
    filas = []
    for p in personas():
        e = expediente(p['id'])
        st = estado(p, e)
        filas.append({**p, 'area_nombre': nombre_area(p['area']), **st, 'token': toks.get(p['id']),
                      'actualizado': e.get('actualizado'), 'tiene_resumen': bool(e.get('resumen')),
                      'enviado': env.get(p['id'], {}).get('ultimo'), 'envios': env.get(p['id'], {}).get('veces', 0),
                      'telefono': tels.get(p['id'], ''), 'reclamado': recl.get(p['id'], {}).get('fecha'), 'cerrada': e.get('cerrada'),
                      'datos_formulario': estado_datos(p['id']),
                      'datos_admin': next(({'faltan': r.get('faltan', []), 'entregados': r.get('seleccion', [])}
                                           for r in e['respuestas'] if r.get('formato') == 'checklist'), None)})
    tunel = URL_PUBLICA
    try:
        with open(os.path.join(DAT, 'tunel.txt'), encoding='utf-8') as f:
            tunel = tunel or f.read().strip() or None
    except FileNotFoundError:
        pass
    return {'personas': filas, 'areas': [{'id': k, 'nombre': v['nombre']} for k, v in banco()['areas'].items()],
            'red': {'lan': f'http://{ip_local()}:{PUERTO}', 'tunel': tunel},
            'codigo_equipo': acceso()['codigo_equipo'], 'ia': dict(IA), 'ajustes': ajustes(), 'generado': ahora()}


# ------------------------------------------------------------------- HTTP
TIPOS = {'.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.js': 'text/javascript; charset=utf-8',
         '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
         '.md': 'text/markdown; charset=utf-8', '.png': 'image/png'}


class Manejador(BaseHTTPRequestHandler):
    server_version = 'PrismaOS-Entrevistas/1.0'

    def log_message(self, fmt, *args):
        pass

    def _enviar(self, codigo, cuerpo, tipo='application/json; charset=utf-8', extra=None):
        if isinstance(cuerpo, (dict, list)):
            cuerpo = json.dumps(cuerpo, ensure_ascii=False).encode('utf-8')
        elif isinstance(cuerpo, str):
            cuerpo = cuerpo.encode('utf-8')
        self.send_response(codigo)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(cuerpo)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _archivo(self, nombre):
        ruta = os.path.join(WEB, nombre)
        try:
            with open(ruta, 'rb') as f:
                self._enviar(200, f.read(), TIPOS.get(os.path.splitext(ruta)[1], 'application/octet-stream'))
        except FileNotFoundError:
            self._enviar(404, {'error': 'No encontrado'})

    def _cuerpo(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError:
            return {}

    def _ip(self):
        """IP del visitante. Detrás del proxy de Render se toma del encabezado que pone el proxy."""
        if DETRAS_DE_PROXY:
            ip = (self.headers.get('CF-Connecting-IP') or self.headers.get('True-Client-IP')
                  or (self.headers.get('X-Forwarded-For') or '').split(',')[0]).strip()
            if ip:
                return ip
        return self.client_address[0]

    def _es_admin(self, qs):
        # Frena a quien intente adivinar el PIN: tras 10 fallos por IP (o 300 en total) en 10 minutos
        # ya ni se compara, hasta que pase la ventana.
        ip = self._ip()
        if pin_bloqueado(ip):
            return False
        pin = self.headers.get('X-Pin') or (qs.get('pin') or [''])[0]
        if hmac.compare_digest(str(pin), acceso()['pin_admin']):
            return True
        if pin:
            fallo_pin(ip)
        return False

    def _no_admin(self):
        if pin_bloqueado(self._ip()):
            return self._enviar(429, {'error': 'Demasiados intentos de PIN. Espera 10 minutos.'})
        return self._enviar(401, {'error': 'PIN incorrecto'})

    def _red_permitida(self):
        if ajustes()['solo_red_local'] and not ip_privada(self._ip()):
            self._enviar(403, 'Solo disponible desde la red de la oficina de Prismaticoos.', 'text/plain; charset=utf-8')
            return False
        return True

    def do_GET(self):
        if not self._red_permitida():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        ruta = u.path
        if ruta in ('/', '/entrar') or ruta.startswith('/p/'):
            return self._archivo('entrevista.html')
        if ruta == '/admin':
            return self._archivo('admin.html')
        if ruta == '/tarjetas':
            return self._archivo('tarjetas.html')
        if ruta == '/datos':
            return self._archivo('datos.html')
        if ruta == '/api/datos_llave':
            llave = llave_publica_datos()
            return self._enviar(200, {'pem': llave}) if llave else self._enviar(404, {'error': 'Formulario no disponible'})
        if ruta == '/logo.svg':
            return self._archivo('logo.svg')
        if ruta == '/salud':
            return self._enviar(200, {'ok': True})
        if ruta == '/api/sesion':
            pid = pid_por_token((qs.get('t') or [''])[0])
            if not pid:
                return self._enviar(404, {'error': 'Este link no es válido. Pide tu link personal a tu coordinador.'})
            return self._enviar(200, vista_sesion(pid))
        if ruta == '/api/publico':
            aj = ajustes()
            return self._enviar(200, {'link_general': aj['link_general'], 'pedir_codigo': aj['pedir_codigo']})
        if ruta == '/api/areas':
            return self._enviar(200, [{'id': k, 'nombre': v['nombre']} for k, v in banco()['areas'].items()])
        if ruta.startswith('/api/admin/'):
            if not self._es_admin(qs):
                return self._no_admin()
            if ruta == '/api/admin/estado':
                return self._enviar(200, estado_admin())
            if ruta == '/api/admin/persona':
                pid = (qs.get('id') or [''])[0]
                p = persona(pid)
                if not p:
                    return self._enviar(404, {'error': 'No existe'})
                e = expediente(pid)
                return self._enviar(200, {'persona': {**p, 'area_nombre': nombre_area(p['area'])}, 'expediente': e,
                                          **estado(p, e), 'token': tokens().get(pid),
                                          'total_preguntas_base': len(preguntas_base(p))})
            if ruta == '/api/admin/exportar.md':
                exportar()
                with open(os.path.join(DAT, 'EXPORT_entrevistas.md'), 'rb') as f:
                    return self._enviar(200, f.read(), 'text/markdown; charset=utf-8',
                                        {'Content-Disposition': 'attachment; filename="entrevistas-prismaos.md"'})
            if ruta == '/api/admin/documento.md':
                exportar()
                with open(os.path.join(DAT, 'DOCUMENTO_PRISMATICOOS_para_chat.md'), 'rb') as f:
                    return self._enviar(200, f.read(), 'text/markdown; charset=utf-8',
                                        {'Content-Disposition': 'attachment; filename="PRISMATICOOS-documento-para-chat.md"'})
            if ruta == '/api/admin/datos_cifrados':
                # Solo sobres cifrados: sin la llave privada (que vive en la Mac) no se pueden leer.
                todo = {}
                for p in personas():
                    env = envios_datos(p['id'])
                    if env:
                        todo[p['id']] = {'nombre': p['nombre'], 'cargo': p['cargo'], 'envios': env}
                return self._enviar(200, todo)
            if ruta == '/api/admin/exportar.json':
                exportar()
                with open(os.path.join(DAT, 'EXPORT_entrevistas.json'), 'rb') as f:
                    return self._enviar(200, f.read(), 'application/json; charset=utf-8',
                                        {'Content-Disposition': 'attachment; filename="entrevistas-prismaos.json"'})
        return self._enviar(404, {'error': 'No encontrado'})

    def do_POST(self):
        if not self._red_permitida():
            return
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        ruta = u.path
        b = self._cuerpo()
        if ruta == '/api/entrar':
            aj = ajustes()
            if not aj['link_general']:
                return self._enviar(403, {'error': 'El acceso es solo con tu link personal. Pídeselo a tu coordinador.'})
            if aj['pedir_codigo'] and not hmac.compare_digest(str(b.get('codigo', '')).strip().lower(), acceso()['codigo_equipo'].lower()):
                return self._enviar(403, {'error': 'Código incorrecto. Pídeselo a tu coordinador.'})
            disp = str(b.get('dispositivo', ''))
            if b.get('persona') or b.get('nuevo'):
                if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', disp):
                    return self._enviar(400, {'error': 'Tu navegador no permite guardar datos. Abre el link en Safari o Chrome normal (no en modo privado).'})
            if b.get('persona'):
                pid = b['persona']
                tok = tokens().get(pid)
                if not tok:
                    return self._enviar(404, {'error': 'No encontrado'})
                if expediente(pid).get('cerrada'):
                    return self._enviar(409, {'error': 'La entrevista de esta persona ya quedó registrada y está cerrada. Si necesitas agregar algo, avisa a quien te mandó el link.'})
                if not reclamar(pid, disp):
                    return self._enviar(409, {'error': 'Este nombre ya se está usando en otro celular. Si eres tú y cambiaste de celular, avisa a quien mandó el link para que lo libere.'})
                return self._enviar(200, {'token': tok})
            if b.get('nuevo'):
                n = b['nuevo']
                nombre, area, cargo = str(n.get('nombre', '')).strip(), str(n.get('area', '')), str(n.get('cargo', '')).strip()
                if len(nombre) < 3 or area not in banco()['areas'] or len(cargo) < 3:
                    return self._enviar(400, {'error': 'Escribe tu nombre, tu área y tu puesto.'})
                if buscar_persona(nombre)[0] == 'ok':
                    return self._enviar(409, {'error': 'Ya existe una persona con ese nombre en el equipo. Escríbelo arriba en "¿Quién eres?"; si ya contestaste, tu entrevista está registrada.'})
                with LOCK:
                    extra = leer(os.path.join(DAT, 'personas_extra.json'), [])
                    pid = slug(nombre)
                    ids = {p['id'] for p in personas()}
                    while pid in ids:
                        pid = f'{slug(nombre)}-{secrets.token_hex(2)}'
                    extra.append({'id': pid, 'nombre': nombre, 'cargo': cargo, 'area': area, 'equipo': str(n.get('equipo', '')).strip(),
                                  'lider': bool(n.get('lider')), 'externo': False, 'agregado': ahora(), 'autoregistro': True})
                    escribir(os.path.join(DAT, 'personas_extra.json'), extra)
                reclamar(pid, disp)
                return self._enviar(200, {'token': tokens()[pid]})
            if 'buscar' in b:
                if demasiados_intentos(self._ip()):
                    return self._enviar(429, {'error': 'Demasiados intentos. Espera unos minutos o pide ayuda a quien te mandó el link.'})
                estado_b, p = buscar_persona(str(b.get('buscar', ''))[:120])
                if estado_b == 'ok':
                    return self._enviar(200, {'persona': {'id': p['id'], 'nombre': p['nombre'], 'cargo': p['cargo'],
                                                          'area_nombre': nombre_area(p['area']), 'equipo': p.get('equipo', '')}})
                if estado_b == 'ambiguo':
                    return self._enviar(404, {'error': 'Hay más de una persona que coincide. Escribe tu nombre y tus apellidos completos.'})
                if estado_b == 'incompleto':
                    return self._enviar(404, {'error': 'Escribe también tu apellido.'})
                return self._enviar(404, {'error': 'No encontramos ese nombre. Escríbelo con tu nombre y apellido, como aparece en tu contrato. Si eres nuevo, regístrate abajo.'})
            return self._enviar(200, {'ok': True})
        if ruta == '/api/datos_admin':
            pid = pid_por_token(str(b.get('t', '')))
            if not pid:
                return self._enviar(404, {'error': 'Este link no es válido. Abre el formulario desde tu link personal.'})
            if demasiados_intentos('datos:' + pid, limite=20):
                return self._enviar(429, {'error': 'Demasiados envíos seguidos. Espera unos minutos.'})
            if not guardar_datos_cifrados(pid, b.get('sobre') or {}, b.get('faltan', [])):
                return self._enviar(400, {'error': 'No se pudo leer el envío. Recarga la página e intenta de nuevo.'})
            return self._enviar(200, {'ok': True, 'estado': estado_datos(pid)})
        if ruta == '/api/editar':
            pid = pid_por_token(b.get('t', ''))
            if not pid:
                return self._enviar(404, {'error': 'Link no válido'})
            if expediente(pid).get('cerrada'):
                return self._enviar(409, {'error': 'Tu entrevista ya quedó registrada y cerrada; ya no se pueden cambiar las respuestas.'})
            try:
                i = int(b.get('i'))
            except (TypeError, ValueError):
                return self._enviar(400, {'error': 'Respuesta no encontrada'})
            if tiene_dato_personal(str(b.get('respuesta', ''))):
                return self._enviar(400, {'error': AVISO_SENSIBLE})
            sel = b.get('seleccion') if isinstance(b.get('seleccion'), list) else None
            if not editar_respuesta(pid, i, str(b.get('respuesta', ''))[:20000], [str(x) for x in (sel or [])]):
                return self._enviar(400, {'error': 'Respuesta no encontrada'})
            return self._enviar(200, vista_sesion(pid))
        if ruta == '/api/responder':
            pid = pid_por_token(b.get('t', ''))
            if not pid:
                return self._enviar(404, {'error': 'Link no válido'})
            if expediente(pid).get('cerrada'):
                return self._enviar(409, {'error': 'Tu entrevista ya quedó registrada. Cada respuesta se registra una única vez.'})
            texto = str(b.get('respuesta', ''))[:20000]
            saltar = bool(b.get('saltar'))
            sel = [str(x) for x in b['seleccion']] if isinstance(b.get('seleccion'), list) else None
            if sel is None and not saltar and not texto.strip():
                return self._enviar(400, {'error': 'Escribe tu respuesta o marca "No aplica / no sé".'})
            if tiene_dato_personal(texto):
                return self._enviar(400, {'error': AVISO_SENSIBLE})
            return self._enviar(200, responder(pid, str(b.get('qid', '')), texto, saltar, sel))
        if ruta.startswith('/api/admin/'):
            if not self._es_admin(qs):
                return self._no_admin()
            pid = b.get('id', '')
            if ruta == '/api/admin/reabrir':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                reabrir(pid)
                return self._enviar(200, {'ok': True})
            if ruta == '/api/admin/liberar':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                liberar(pid)
                return self._enviar(200, {'ok': True})
            if ruta == '/api/admin/marcar_envio':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                marcar_envio(pid)
                return self._enviar(200, {'ok': True})
            if ruta == '/api/admin/telefono':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                return self._enviar(200, {'telefono': guardar_tel(pid, b.get('telefono', ''))})
            if ruta == '/api/admin/telefonos_lote':
                return self._enviar(200, importar_telefonos(str(b.get('texto', ''))[:200000]))
            if ruta == '/api/admin/probar_ia':
                return self._enviar(200, probar_ia())
            if ruta == '/api/admin/seguimiento':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                qsug = ia_seguimiento(pid)
                if qsug is None:
                    return self._enviar(503, {'error': 'La IA no respondió. ' + ('Revisa la llave ANTHROPIC_API_KEY en Render' if USAR_API else 'Revisa que Claude tenga sesión iniciada (claude auth login)') + ' o escribe las preguntas a mano.'})
                return self._enviar(200, {'preguntas': qsug})
            if ruta == '/api/admin/agregar':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                agregar_pendientes(pid, b.get('preguntas', []), b.get('origen', 'tablero'))
                return self._enviar(200, {'ok': True})
            if ruta == '/api/admin/resumir':
                if not persona(pid):
                    return self._enviar(404, {'error': 'No existe'})
                r = ia_resumen(pid)
                return self._enviar(200, {'resumen': r}) if r else self._enviar(503, {'error': 'La IA no respondió.'})
            if ruta == '/api/admin/persona_nueva':
                n = b
                nombre, area, cargo = str(n.get('nombre', '')).strip(), str(n.get('area', '')), str(n.get('cargo', '')).strip()
                if len(nombre) < 2 or area not in banco()['areas'] or len(cargo) < 2:
                    return self._enviar(400, {'error': 'Faltan nombre, área o puesto.'})
                with LOCK:
                    extra = leer(os.path.join(DAT, 'personas_extra.json'), [])
                    pid = slug(nombre)
                    ids = {p['id'] for p in personas()}
                    while pid in ids:
                        pid = f'{slug(nombre)}-{secrets.token_hex(2)}'
                    extra.append({'id': pid, 'nombre': nombre, 'cargo': cargo, 'area': area,
                                  'equipo': str(n.get('equipo', '')).strip(), 'lider': bool(n.get('lider')),
                                  'externo': bool(n.get('externo')), 'agregado': ahora()})
                    escribir(os.path.join(DAT, 'personas_extra.json'), extra)
                tokens()
                return self._enviar(200, {'id': pid})
        return self._enviar(404, {'error': 'No encontrado'})


def main():
    acceso()
    tokens()
    exportar()
    srv = ThreadingHTTPServer(('0.0.0.0', PUERTO), Manejador)
    print(f'PrismaOS · Entrevistas en http://localhost:{PUERTO}/admin  (equipo: http://{ip_local()}:{PUERTO})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
