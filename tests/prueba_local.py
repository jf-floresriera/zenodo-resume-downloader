#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prueba local de extremo a extremo (no necesita internet).

Levanta un servidor HTTP con soporte de ``Range`` y velocidad limitada,
genera un archivo de prueba y comprueba que el descargador:

  1. se puede interrumpir a mitad de camino (SIGINT),
  2. reanuda sin volver a descargar los segmentos ya completos,
  3. entrega un archivo identico al original (MD5).

Uso:
    python tests/prueba_local.py
"""

import hashlib
import http.server
import os
import re
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(RAIZ, "descargar_zenodo.py")
TAM_ARCHIVO = 40 * 1024 * 1024
PAUSA_POR_BLOQUE = 0.1  # ~2.5 MB/s por conexion (bloques de 256 KiB)


def crear_servidor(ruta_archivo):
    tam = os.path.getsize(ruta_archivo)

    class Manejador(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            rango = self.headers.get("Range")
            ini, fin = 0, tam - 1
            codigo = 200
            if rango:
                m = re.match(r"bytes=(\d+)-(\d*)", rango)
                ini = int(m.group(1))
                fin = int(m.group(2)) if m.group(2) else tam - 1
                fin = min(fin, tam - 1)
                codigo = 206
            self.send_response(codigo)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(fin - ini + 1))
            if codigo == 206:
                self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
            self.end_headers()
            with open(ruta_archivo, "rb") as f:
                f.seek(ini)
                restante = fin - ini + 1
                while restante > 0:
                    bloque = f.read(min(256 * 1024, restante))
                    try:
                        self.wfile.write(bloque)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    restante -= len(bloque)
                    time.sleep(PAUSA_POR_BLOQUE)

    class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    return Servidor(("127.0.0.1", 0), Manejador)


def md5(ruta):
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        origen = os.path.join(tmp, "origen.bin")
        with open(origen, "wb") as f:
            f.write(os.urandom(TAM_ARCHIVO))
        esperado = md5(origen)

        servidor = crear_servidor(origen)
        puerto = servidor.server_address[1]
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{puerto}/origen.bin"
        salida = os.path.join(tmp, "descargas")

        base = [sys.executable, SCRIPT, url, "-o", salida, "-n", "4",
                "--segmento-mb", "2", "--lang", "es", "--md5", esperado]

        # 1) Primera ejecucion: se interrumpe con Ctrl+C a mitad de camino.
        p = subprocess.Popen(base, stderr=subprocess.PIPE, text=True)
        time.sleep(3.0)
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=30)
        assert p.returncode == 130, f"se esperaba codigo 130, salio {p.returncode}\n{err}"
        estado = os.path.join(salida, "origen.bin.estado.json")
        assert os.path.exists(estado), "no se guardo el archivo de estado"
        print("[ok] interrupcion limpia (codigo 130) y estado guardado")

        # 2) Segunda ejecucion: debe reanudar y terminar.
        r = subprocess.run(base, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"la reanudacion fallo:\n{r.stderr}"
        assert "Avance previo encontrado" in r.stderr, "no detecto el avance previo"
        print("[ok] reanudacion detectada")

        # 3) Integridad.
        final = os.path.join(salida, "origen.bin")
        assert os.path.exists(final), "no existe el archivo final"
        assert md5(final) == esperado, "el MD5 del archivo final no coincide"
        assert not os.path.exists(final + ".part"), "quedo el .part"
        assert not os.path.exists(estado), "quedo el archivo de estado"
        print("[ok] archivo final identico al original (MD5)")

        # 4) Un MD5 incorrecto debe detectarse.
        r = subprocess.run(base[:-1] + ["0" * 32, "--reiniciar"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 2, f"debia salir con 2 por MD5 incorrecto, salio {r.returncode}"
        print("[ok] un MD5 incorrecto se detecta (codigo 2)")

        servidor.shutdown()
        print("\nTodas las pruebas pasaron.")


if __name__ == "__main__":
    main()
