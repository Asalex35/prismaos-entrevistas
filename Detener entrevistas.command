#!/bin/bash
# Apaga SOLO el servidor de entrevistas (puerto 8790); no toca otras apps.
PIDS=$(lsof -nP -tiTCP:8790 -sTCP:LISTEN)
if [ -n "$PIDS" ]; then kill $PIDS; echo "Servidor de entrevistas detenido."; else echo "No estaba encendido."; fi
