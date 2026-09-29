#!/usr/bin/env python3
"""Redirector de la Mac: cuando la app ya vive en Render, ocupa el puerto 8790 y manda
cada link viejo (http://10.10.20.30:8790/...) a la misma página en el dominio nuevo.

- /p/<token>  -> NUEVO/p/<token>
- /admin, /tarjetas -> NUEVO/admin, NUEVO/tarjetas (sin pasar el PIN por la URL)
- /  -> página que toma el link personal guardado en el celular (localStorage) y lo manda a NUEVO/p/<token>
- /api/... -> error JSON pidiendo recargar (páginas que se quedaron abiertas)
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

NUEVO = os.environ.get('PRISMA_URL_PUBLICA', 'https://entrevistas.orkestalabs.com').rstrip('/')
PUERTO = int(os.environ.get('PRISMA_PUERTO', '8790'))

PAGINA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PrismaOS · Entrevistas</title></head>
<body style="font-family:system-ui;padding:24px;text-align:center">
<p>Las entrevistas se mudaron a <a id="a" href="{nuevo}">{nuevo}</a>. Te llevamos…</p>
<script>
var n = {nuevo_js}, t = null;
try {{ t = localStorage.getItem('prisma_token'); }} catch (e) {{}}
var destino = (t && /^[A-Za-z0-9_-]{{8,64}}$/.test(t)) ? n + '/p/' + t : n + '/';
document.getElementById('a').href = destino;
location.replace(destino);
</script></body></html>"""


class Manejador(BaseHTTPRequestHandler):
    server_version = 'PrismaOS-Redirector/1.0'

    def _redirigir(self, ruta):
        self.send_response(302)
        self.send_header('Location', NUEVO + ruta)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def _responder(self, codigo, cuerpo, tipo):
        datos = cuerpo.encode('utf-8')
        self.send_response(codigo)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(datos)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        ruta = urlparse(self.path).path
        if ruta == '/salud':
            return self._responder(200, json.dumps({'ok': True, 'redirector': NUEVO}), 'application/json')
        if re.fullmatch(r'/p/[A-Za-z0-9_-]{8,64}', ruta) or ruta in ('/admin', '/tarjetas', '/logo.svg'):
            return self._redirigir(ruta)
        if ruta.startswith('/api/'):
            return self._api_movida()
        return self._responder(200, PAGINA.format(nuevo=NUEVO, nuevo_js=json.dumps(NUEVO)), 'text/html; charset=utf-8')

    def do_POST(self):
        return self._api_movida()

    def _api_movida(self):
        return self._responder(410, json.dumps({'error': f'Las entrevistas se mudaron a {NUEVO}. Recarga esta página; tus respuestas están guardadas.'},
                                               ensure_ascii=False), 'application/json; charset=utf-8')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print(f'Redirector en :{PUERTO} -> {NUEVO}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PUERTO), Manejador).serve_forever()
