#!/bin/bash
# Doble clic: baja los datos cifrados de Render y los descifra en un CSV en el Escritorio.
# Solo funciona en la Mac que tiene la llave privada (~/.prismaos/datos_admin_privada.pem).
cd "$(dirname "$0")"
"$HOME/.prismaos/venv/bin/python" descifrar_datos_admin.py
echo
read -n 1 -s -r -p "Presiona una tecla para cerrar…"
