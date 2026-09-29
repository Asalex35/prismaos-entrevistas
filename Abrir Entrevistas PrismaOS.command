#!/bin/bash
# Arranca el servidor de entrevistas (si no está corriendo) y abre el tablero.
cd "$(dirname "$0")"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=/usr/bin/python3
mkdir -p datos
if curl -s -m 2 http://localhost:8790/salud >/dev/null; then
  echo "El servidor ya estaba corriendo."
else
  nohup "$PY" servidor.py >> datos/servidor.log 2>&1 &
  SRV=$!
  # Evita que la Mac se duerma mientras el servidor esté encendido
  nohup caffeinate -i -w $SRV >/dev/null 2>&1 &
  sleep 2
fi
open "http://localhost:8790/admin"
echo
echo "Tablero:  http://localhost:8790/admin"
echo "El PIN del tablero y el código del equipo están en: datos/acceso.json"
echo "Puedes cerrar esta ventana; el servidor sigue encendido."
