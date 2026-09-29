#!/usr/bin/env python3
"""Descifra los datos para Administración y los deja en un CSV (se abre con Excel).

Solo funciona en la Mac que tiene la llave privada (~/.prismaos/datos_admin_privada.pem).
Uso:
    ~/.prismaos/venv/bin/python herramientas/descifrar_datos_admin.py
    ~/.prismaos/venv/bin/python herramientas/descifrar_datos_admin.py --archivo sobres.json   # sin internet

El CSV resultante tiene datos personales en claro: guárdalo en un lugar seguro y bórralo al terminar.
"""
import argparse
import base64
import csv
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

URL = os.environ.get('PRISMA_URL_PUBLICA', 'https://prismaos-entrevistas.onrender.com').rstrip('/')
LLAVE = os.path.expanduser('~/.prismaos/datos_admin_privada.pem')
COLUMNAS = [('nombre_completo', 'Nombre completo'), ('rfc', 'RFC'), ('curp', 'CURP'), ('nss', 'NSS'),
            ('fecha_nacimiento', 'Fecha de nacimiento'), ('banco', 'Banco'), ('cuenta', 'Cuenta'), ('clabe', 'CLABE'),
            ('calle_numero', 'Calle y número'), ('colonia', 'Colonia'), ('cp', 'CP'), ('municipio', 'Municipio'),
            ('estado', 'Estado'), ('emergencia_nombre', 'Emergencia: nombre'),
            ('emergencia_parentesco', 'Emergencia: parentesco'), ('emergencia_telefono', 'Emergencia: teléfono')]


def bajar_sobres():
    pin = getpass.getpass('PIN del tablero: ')
    req = urllib.request.Request(URL + '/api/admin/datos_cifrados', headers={'X-Pin': pin})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f'El servidor respondió {e.code}: revisa el PIN.')


def abrir(privada, sobre):
    clave = privada.decrypt(base64.b64decode(sobre['clave']),
                            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    texto = AESGCM(clave).decrypt(base64.b64decode(sobre['iv']), base64.b64decode(sobre['datos']), None)
    return json.loads(texto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--archivo', help='JSON con los sobres ya descargado (en lugar de bajarlo del servidor)')
    ap.add_argument('--salida', default=os.path.expanduser(f'~/Desktop/datos-administracion-{datetime.now():%Y-%m-%d_%H%M}.csv'))
    a = ap.parse_args()
    with open(LLAVE, 'rb') as f:
        privada = serialization.load_pem_private_key(f.read(), password=None)
    todo = json.load(open(a.archivo, encoding='utf-8')) if a.archivo else bajar_sobres()
    filas, errores = [], []
    for pid, info in sorted(todo.items(), key=lambda x: x[1]['nombre']):
        ultimo = info['envios'][-1]  # el envío más reciente de cada persona
        try:
            d = abrir(privada, ultimo)
        except Exception:
            errores.append(info['nombre'])
            continue
        filas.append([info['nombre'], info.get('cargo', ''), ultimo['fecha'], len(info['envios'])]
                     + [d.get(k, '') for k, _ in COLUMNAS])
    viejo = os.umask(0o077)
    try:
        with open(a.salida, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['Persona (app)', 'Puesto', 'Enviado', 'Envíos'] + [t for _, t in COLUMNAS])
            w.writerows(filas)
    finally:
        os.umask(viejo)
    print(f'{len(filas)} personas descifradas → {a.salida}')
    if errores:
        print('No se pudieron abrir (¿otra llave?):', ', '.join(errores))
    print('⚠️  Ese archivo tiene datos personales en claro: guárdalo en lugar seguro y bórralo cuando termines.')


if __name__ == '__main__':
    main()
