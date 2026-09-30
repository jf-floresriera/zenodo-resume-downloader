#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
descargar_zenodo.py  --  Resumable parallel downloader / Descargador reanudable
==============================================================================

EN  Downloads very large files (e.g. a 10 GB .zip hosted on Zenodo) over slow
    or unstable connections.

    1. Splits the file into fixed-size segments (8 MiB by default).
    2. Downloads several segments in parallel, one HTTP connection each, using
       the ``Range`` header. If the server throttles *per connection*, several
       connections add up.
    3. Saves progress in ``<name>.estado.json``. After a network failure, a
       closed terminal or Ctrl+C, run the same command again and only the
       missing segments are fetched.
    4. Retries automatically with exponential backoff.
    5. Verifies the MD5 checksum and only then renames the file.

ES  Descarga archivos muy grandes (por ejemplo, un .zip de 10 GB alojado en
    Zenodo) con conexiones lentas o inestables.

    1. Divide el archivo en segmentos de tamaño fijo (8 MiB por defecto).
    2. Descarga varios segmentos en paralelo, una conexión HTTP por segmento,
       con la cabecera ``Range``. Si el servidor limita la velocidad *por
       conexión*, varias conexiones se suman.
    3. Guarda el avance en ``<nombre>.estado.json``. Tras un corte de red, el
       cierre de la terminal o Ctrl+C, ejecuta el mismo comando y solo se
       descargan los segmentos que faltan.
    4. Reintenta automáticamente con espera exponencial.
    5. Verifica el MD5 y solo entonces renombra el archivo.

Requirements / Requisitos
-------------------------
Python 3.8+. No third-party packages / Sin librerías externas.

Quick use / Uso rápido
----------------------
    python descargar_zenodo.py
    python descargar_zenodo.py -n 6 -o ~/data
    python descargar_zenodo.py --lang en
    python descargar_zenodo.py "https://zenodo.org/records/<id>/files/<file>?download=1"

Responsible use / Uso responsable
---------------------------------
EN  Zenodo is a free public service run by CERN. Use a reasonable number of
    connections (4-8) and respect the license and citation terms of each dataset.
ES  Zenodo es un servicio público y gratuito operado por el CERN. Usa un número
    razonable de conexiones (4-8) y respeta la licencia y la cita de cada dataset.

Exit codes / Códigos de salida
------------------------------
0 = complete and verified / completa y verificada
1 = error / error
2 = MD5 mismatch / el MD5 no coincide
130 = interrupted by the user (progress saved) / interrumpida (avance guardado)
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import locale
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION

# --------------------------------------------------------------------------
# Configuration / Configuración
# --------------------------------------------------------------------------

VERSION = "1.1.0"

# Zenodo record with multispectral drone images (pulse crop dataset).
# Registro de Zenodo con imágenes multiespectrales de dron.
URL_POR_DEFECTO = "https://zenodo.org/records/8280431/files/Images.zip?download=1"

# MD5 sums published by Zenodo for known files: (record, file).
# Sumas MD5 publicadas por Zenodo para archivos conocidos: (registro, archivo).
MD5_CONOCIDOS = {
    ("8280431", "Images.zip"): "e81c4390d378860ee19bd91ec5b0bfd6",
}

AGENTE = f"descargar-zenodo/{VERSION} (resumable; python-urllib)"
TAM_LECTURA = 256 * 1024          # bytes per read() call / bytes por read()
TAM_HASH = 8 * 1024 * 1024        # bytes per MD5 block / bytes por bloque MD5
CODIGOS_REINTENTABLES = {408, 429, 500, 502, 503, 504}


class ErrorFatal(Exception):
    """EN: error not worth retrying (disk full, 404...). ES: error sin reintento."""


# --------------------------------------------------------------------------
# Messages / Mensajes (ES + EN)
# --------------------------------------------------------------------------

MSG = {
    "es": {
        "titulo": "Descargador reanudable para Zenodo",
        "archivo": "Archivo : {v}",
        "destino": "Destino : {v}",
        "md5_esperado": "MD5 esperado: {v}",
        "sin_md5": "No se encontró un MD5 de referencia; no se podrá verificar la integridad.",
        "ya_existe": "{v} ya existe.",
        "verif_existente": "Verificando integridad del archivo existente...",
        "nada_que_hacer": "MD5 correcto. No hay nada que descargar.",
        "md5_no_coincide_existente": "El MD5 no coincide. Usa --reiniciar para descargarlo de nuevo.",
        "avance_borrado": "Avance anterior eliminado.",
        "tamano": "Tamaño  : {t}  |  Rangos: {r}",
        "si": "sí",
        "no": "no",
        "sin_rangos": "El servidor no admite descarga por rangos: se usará una sola conexión y no se podrá reanudar.",
        "sin_tamano": "El servidor no informa el tamaño del archivo; no se puede planificar la descarga.",
        "espacio": "Espacio insuficiente: se necesitan {n} y hay {l} libres.",
        "avance_previo": "Avance previo encontrado: {n} segmentos completos. Se retoma desde ahí.",
        "avance_distinto": "El avance guardado no coincide con el archivo remoto; se empieza de cero.",
        "segmentos": "Segmentos: {n} de {m} MiB  |  Conexiones: {c}",
        "interrumpido": "Interrumpido. El progreso quedó guardado.",
        "reanudar": "Ejecuta el mismo comando para continuar donde quedó.",
        "interrumpido_simple": "Descarga interrumpida.",
        "fallo": "Falló la descarga: {v}",
        "puedes_reanudar": "El progreso quedó guardado; puedes reanudar con el mismo comando.",
        "completa": "Descarga completa en {t}  (media de esta sesión: {v}/s)",
        "verificando": "Verificando integridad (MD5)...",
        "md5_distinto": "MD5 distinto. Esperado {e}, obtenido {o}.",
        "md5_corrupto": "El archivo pudo corromperse. Ejecuta de nuevo con --reiniciar.",
        "md5_correcto": "MD5 correcto.",
        "listo": "Listo: {v}",
        "verificando_barra": "verificando MD5",
        "conexiones_barra": "conexiones: {n}",
        "conexiones_min": "--conexiones debe ser al menos 1.",
        "conexiones_muchas": "Usar más de 8 conexiones puede ser bloqueado o mal visto por el servidor. Zenodo es un servicio compartido: usa el mínimo que te funcione.",
        "e_http_sondeo": "El servidor respondió HTTP {c} ({r}) al consultar el archivo.",
        "e_conexion": "No se pudo conectar con el servidor: {r}",
        "e_sin_rangos_mid": "El servidor dejó de aceptar rangos (HTTP {c}).",
        "e_http_segmento": "HTTP {c} ({r}) en el segmento {i}.",
        "e_disco": "No queda espacio en disco.",
        "e_reintentos": "Se agotaron los reintentos en el segmento {i} ({c}). Vuelve a ejecutar el mismo comando para reanudar.",
        "e_cerrada": "conexión cerrada antes de tiempo",
        "h_desc": "Descarga archivos grandes con conexiones paralelas, reanudación automática y verificación MD5.",
        "h_url": "URL directa del archivo (por defecto: Images.zip del registro 8280431)",
        "h_salida": "carpeta de destino (por defecto: la actual)",
        "h_conex": "conexiones simultáneas (por defecto: 4; recomendado 4-8)",
        "h_seg": "tamaño de cada segmento en MiB (por defecto: 8)",
        "h_reint": "reintentos consecutivos por segmento (por defecto: 8)",
        "h_timeout": "tiempo máximo de espera de red en segundos (por defecto: 30)",
        "h_nombre": "nombre del archivo final (por defecto: el de la URL)",
        "h_md5": "suma MD5 esperada (por defecto se busca en Zenodo)",
        "h_sinverif": "omitir la verificación MD5 al final",
        "h_reiniciar": "borrar el avance guardado y empezar de cero",
        "h_lang": "idioma de los mensajes: auto, es o en (por defecto: auto)",
    },
    "en": {
        "titulo": "Resumable downloader for Zenodo",
        "archivo": "File    : {v}",
        "destino": "Output  : {v}",
        "md5_esperado": "Expected MD5: {v}",
        "sin_md5": "No reference MD5 found; integrity cannot be verified.",
        "ya_existe": "{v} already exists.",
        "verif_existente": "Verifying the existing file...",
        "nada_que_hacer": "MD5 is correct. Nothing to download.",
        "md5_no_coincide_existente": "MD5 does not match. Use --reiniciar to download it again.",
        "avance_borrado": "Previous progress removed.",
        "tamano": "Size    : {t}  |  Ranges: {r}",
        "si": "yes",
        "no": "no",
        "sin_rangos": "The server does not support range requests: a single connection will be used and it cannot be resumed.",
        "sin_tamano": "The server does not report the file size; the download cannot be planned.",
        "espacio": "Not enough disk space: {n} needed, {l} free.",
        "avance_previo": "Previous progress found: {n} segments complete. Resuming from there.",
        "avance_distinto": "Saved progress does not match the remote file; starting from scratch.",
        "segmentos": "Segments: {n} of {m} MiB  |  Connections: {c}",
        "interrumpido": "Interrupted. Progress has been saved.",
        "reanudar": "Run the same command to continue where it stopped.",
        "interrumpido_simple": "Download interrupted.",
        "fallo": "Download failed: {v}",
        "puedes_reanudar": "Progress has been saved; you can resume with the same command.",
        "completa": "Download complete in {t}  (this session's average: {v}/s)",
        "verificando": "Verifying integrity (MD5)...",
        "md5_distinto": "MD5 mismatch. Expected {e}, got {o}.",
        "md5_corrupto": "The file may be corrupted. Run again with --reiniciar.",
        "md5_correcto": "MD5 is correct.",
        "listo": "Done: {v}",
        "verificando_barra": "verifying MD5",
        "conexiones_barra": "connections: {n}",
        "conexiones_min": "--conexiones must be at least 1.",
        "conexiones_muchas": "Using more than 8 connections may get you blocked or frowned upon. Zenodo is a shared service: use the minimum that works for you.",
        "e_http_sondeo": "The server answered HTTP {c} ({r}) when querying the file.",
        "e_conexion": "Could not connect to the server: {r}",
        "e_sin_rangos_mid": "The server stopped accepting ranges (HTTP {c}).",
        "e_http_segmento": "HTTP {c} ({r}) on segment {i}.",
        "e_disco": "No space left on disk.",
        "e_reintentos": "Retries exhausted on segment {i} ({c}). Run the same command again to resume.",
        "e_cerrada": "connection closed early",
        "h_desc": "Downloads large files with parallel connections, automatic resume and MD5 verification.",
        "h_url": "direct file URL (default: Images.zip from record 8280431)",
        "h_salida": "destination folder (default: current folder)",
        "h_conex": "simultaneous connections (default: 4; recommended 4-8)",
        "h_seg": "size of each segment in MiB (default: 8)",
        "h_reint": "consecutive retries per segment (default: 8)",
        "h_timeout": "network timeout in seconds (default: 30)",
        "h_nombre": "final file name (default: taken from the URL)",
        "h_md5": "expected MD5 sum (default: looked up on Zenodo)",
        "h_sinverif": "skip the MD5 verification at the end",
        "h_reiniciar": "delete saved progress and start over",
        "h_lang": "message language: auto, es or en (default: auto)",
    },
}

IDIOMA = "es"


def t(clave: str, **kw) -> str:
    """EN: translated message. ES: mensaje traducido al idioma activo."""
    return MSG[IDIOMA][clave].format(**kw)


def detectar_idioma(pedido: str) -> str:
    """
    EN: Picks 'es' or 'en'. An explicit --lang wins; otherwise the system
        locale is used, and English is the fallback.
    ES: Elige 'es' o 'en'. Un --lang explícito manda; si no, se usa el idioma
        del sistema y el inglés es el respaldo.
    """
    if pedido in ("es", "en"):
        return pedido
    candidatos = [os.environ.get(v, "") for v in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")]
    try:
        locale.setlocale(locale.LC_CTYPE, "")
        candidatos.append(locale.getlocale(locale.LC_CTYPE)[0] or "")
    except (locale.Error, ValueError):
        pass
    for c in candidatos:
        c = c.lower()
        if c.startswith(("es", "spanish")):
            return "es"
        if c.startswith(("en", "english")):
            return "en"
    return "en"


# --------------------------------------------------------------------------
# Presentation helpers / Utilidades de presentación
# --------------------------------------------------------------------------

def humano(n: float) -> str:
    """
    EN: Human-readable size using decimal units (1 GB = 1000 MB), as Zenodo does.
    ES: Tamaño legible con unidades decimales (1 GB = 1000 MB), igual que Zenodo.
    """
    unidades = ["B", "kB", "MB", "GB", "TB"]
    n = float(n)
    for u in unidades:
        if n < 1000 or u == unidades[-1]:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1000
    return f"{n:.2f} TB"


def tiempo(seg) -> str:
    """EN: seconds as HH:MM:SS. ES: segundos como HH:MM:SS (--:-- si no hay dato)."""
    if seg is None or seg != seg or seg == float("inf") or seg < 0:
        return "--:--"
    seg = int(seg)
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Estilo:
    """
    EN: Sober ANSI colors (steel blue, slate, amber); disabled automatically
        when output is not a terminal or NO_COLOR is set.
    ES: Colores ANSI sobrios (azul acero, pizarra, ámbar); se desactivan solos
        si la salida no es una terminal o existe NO_COLOR.
    """

    def __init__(self) -> None:
        try:  # never crash on a console that cannot print accents
            sys.stderr.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        activo = sys.stderr.isatty() and "NO_COLOR" not in os.environ
        if activo and os.name == "nt":
            os.system("")  # enables ANSI sequences on Windows 10+
        self.activo = activo
        try:
            "█░".encode(sys.stderr.encoding or "ascii")
            self.bloques = ("█", "░")
        except (UnicodeEncodeError, LookupError):
            self.bloques = ("#", "-")

    def _c(self, codigo: str, texto: str) -> str:
        return f"\033[{codigo}m{texto}\033[0m" if self.activo else texto

    def azul(self, x): return self._c("38;5;74", x)
    def gris(self, x): return self._c("38;5;245", x)
    def verde(self, x): return self._c("38;5;72", x)
    def ambar(self, x): return self._c("38;5;179", x)
    def rojo(self, x): return self._c("38;5;167", x)
    def negrita(self, x): return self._c("1", x)


ES = Estilo()


def info(msg: str) -> None:
    print(f"{ES.azul('[info]')}  {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"{ES.verde('[ok]')}    {msg}", file=sys.stderr)


def aviso(msg: str) -> None:
    label = "[aviso]" if IDIOMA == "es" else "[warn]"
    print(f"{ES.ambar(label)} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{ES.rojo('[error]')} {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Server queries / Consultas al servidor
# --------------------------------------------------------------------------

def sondear(url: str, timeout: float):
    """
    EN: Gets the total size and whether the server supports range requests by
        asking for the first byte only (``Range: bytes=0-0``).
    ES: Obtiene el tamaño total y si el servidor acepta rangos pidiendo solo el
        primer byte (``Range: bytes=0-0``).

    Returns / Devuelve: (total_size | None, supports_ranges: bool)
    """
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range")
            if r.status == 206 and cr:
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                if m:
                    return int(m.group(1)), True
            cl = r.headers.get("Content-Length")
            return (int(cl) if cl else None), False
    except urllib.error.HTTPError as e:
        raise ErrorFatal(t("e_http_sondeo", c=e.code, r=e.reason))
    except urllib.error.URLError as e:
        raise ErrorFatal(t("e_conexion", r=e.reason))


def md5_desde_zenodo(url: str, nombre: str, timeout: float):
    """
    EN: Tries to get the official MD5 from Zenodo's public API (None on failure;
        the download goes on anyway).
    ES: Intenta obtener el MD5 oficial desde la API pública de Zenodo (None si
        falla; la descarga continúa igualmente).
    """
    partes = urllib.parse.urlparse(url)
    m = re.search(r"/records?/(\d+)/files/", partes.path)
    if not m or not partes.netloc.endswith("zenodo.org"):
        return None
    registro = m.group(1)
    if (registro, nombre) in MD5_CONOCIDOS:
        return MD5_CONOCIDOS[(registro, nombre)]
    api = f"https://{partes.netloc}/api/records/{registro}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": AGENTE})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            datos = json.load(r)
        archivos = datos.get("files", [])
        if isinstance(archivos, dict):
            archivos = archivos.get("entries", {}).values()
        for f in archivos:
            if f.get("key") == nombre and str(f.get("checksum", "")).startswith("md5:"):
                return f["checksum"].split(":", 1)[1]
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Persistent state / Estado persistente (resume)
# --------------------------------------------------------------------------

class Estado:
    """
    EN: Records which segments are complete. Written atomically (temp file +
        os.replace) so a power cut cannot corrupt it.
    ES: Guarda qué segmentos están completos. Se escribe de forma atómica
        (archivo temporal + os.replace) para que un corte de luz no lo dañe.
    """

    def __init__(self, ruta: str, url: str, total: int, tam_chunk: int) -> None:
        self.ruta = ruta
        self.url = url
        self.total = total
        self.tam_chunk = tam_chunk
        self.hechos: set = set()
        self._lock = threading.Lock()
        self._ultimo = 0.0

    @classmethod
    def cargar(cls, ruta: str):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                d = json.load(f)
            e = cls(ruta, d["url"], int(d["total"]), int(d["tam_chunk"]))
            e.hechos = set(int(i) for i in d["hechos"])
            return e
        except (OSError, ValueError, KeyError):
            return None

    def marcar(self, idx: int) -> None:
        with self._lock:
            self.hechos.add(idx)
        self.guardar()

    def guardar(self, forzar: bool = False) -> None:
        ahora = time.time()
        with self._lock:
            if not forzar and ahora - self._ultimo < 2.0:
                return
            self._ultimo = ahora
            datos = {
                "version": VERSION,
                "url": self.url,
                "total": self.total,
                "tam_chunk": self.tam_chunk,
                "hechos": sorted(self.hechos),
            }
            tmp = self.ruta + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(datos, f)
            os.replace(tmp, self.ruta)


# --------------------------------------------------------------------------
# Progress bar / Barra de progreso
# --------------------------------------------------------------------------

class Progreso:
    """EN: Thread that draws the bar (percent, recent speed, ETA). ES: Hilo que dibuja la barra."""

    def __init__(self, total: int, inicial: int, conexiones: int) -> None:
        self.total = total
        self.bytes = inicial
        self.inicial = inicial
        self.conexiones = conexiones
        self._lock = threading.Lock()
        self._muestras = deque(maxlen=12)  # ~6 s of history
        self._fin = threading.Event()
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._interactivo = sys.stderr.isatty()

    def sumar(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def iniciar(self) -> None:
        self._hilo.start()

    def detener(self) -> None:
        self._fin.set()
        self._hilo.join(timeout=2)
        self._dibujar(final=True)

    def _velocidad(self) -> float:
        if len(self._muestras) < 2:
            return 0.0
        t0, b0 = self._muestras[0]
        t1, b1 = self._muestras[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def _bucle(self) -> None:
        ultima_linea_plana = 0.0
        while not self._fin.wait(0.5):
            with self._lock:
                self._muestras.append((time.time(), self.bytes))
            if self._interactivo:
                self._dibujar()
            elif time.time() - ultima_linea_plana >= 15:
                ultima_linea_plana = time.time()
                self._dibujar(final=True)

    def _dibujar(self, final: bool = False) -> None:
        with self._lock:
            b = self.bytes
        frac = min(1.0, b / self.total) if self.total else 0.0
        vel = self._velocidad()
        eta = (self.total - b) / vel if vel > 0 else None
        ancho = 28
        lleno = int(ancho * frac)
        barra = ES.bloques[0] * lleno + ES.bloques[1] * (ancho - lleno)
        linea = (
            f"{ES.azul(barra)} {frac * 100:5.1f}%  "
            f"{humano(b)} / {humano(self.total)}  "
            f"{ES.negrita(humano(vel) + '/s'):>10}  "
            f"ETA {tiempo(eta)}  {ES.gris(t('conexiones_barra', n=self.conexiones))}"
        )
        if self._interactivo:
            sys.stderr.write("\r" + linea + ("\033[K" if ES.activo else "   "))
            if final:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(linea + "\n")
        sys.stderr.flush()


# --------------------------------------------------------------------------
# Segmented download / Descarga por segmentos
# --------------------------------------------------------------------------

class Descargador:
    def __init__(self, url, parcial, estado, conexiones, reintentos, timeout, progreso):
        self.url = url
        self.parcial = parcial
        self.estado = estado
        self.total = estado.total
        self.tam = estado.tam_chunk
        self.conexiones = conexiones
        self.reintentos = reintentos
        self.timeout = timeout
        self.progreso = progreso
        self.parar = threading.Event()

    @property
    def n_chunks(self) -> int:
        return (self.total + self.tam - 1) // self.tam

    def _espera(self, intento: int, retry_after=None) -> None:
        """EN: exponential backoff with jitter. ES: espera exponencial con algo de azar."""
        base = float(retry_after) if retry_after else min(60.0, 2.0 ** intento)
        self.parar.wait(base + random.uniform(0, 1.0))

    def _bajar_chunk(self, idx: int) -> bool:
        """
        EN: Downloads one segment; after a failure it resumes from the last byte
            received instead of restarting the segment.
        ES: Descarga un segmento; tras un fallo retoma desde el último byte
            recibido en vez de reiniciar el segmento.
        """
        ini = idx * self.tam
        fin = min(ini + self.tam, self.total) - 1
        pos = ini
        intento = 0
        while pos <= fin:
            if self.parar.is_set():
                return False
            pos_antes = pos
            retry_after = None
            try:
                req = urllib.request.Request(
                    self.url,
                    headers={"User-Agent": AGENTE, "Range": f"bytes={pos}-{fin}"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    if r.status != 206:
                        raise ErrorFatal(t("e_sin_rangos_mid", c=r.status))
                    with open(self.parcial, "r+b") as f:
                        f.seek(pos)
                        while pos <= fin:
                            if self.parar.is_set():
                                return False
                            datos = r.read(min(TAM_LECTURA, fin - pos + 1))
                            if not datos:
                                raise ConnectionError(t("e_cerrada"))
                            f.write(datos)
                            pos += len(datos)
                            self.progreso.sumar(len(datos))
            except ErrorFatal:
                raise
            except urllib.error.HTTPError as e:
                if e.code not in CODIGOS_REINTENTABLES:
                    raise ErrorFatal(t("e_http_segmento", c=e.code, r=e.reason, i=idx))
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and not str(retry_after).isdigit():
                    retry_after = None
                causa = f"HTTP {e.code}"
            except (OSError, http.client.HTTPException) as e:
                if getattr(e, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                    raise ErrorFatal(t("e_disco"))
                causa = type(e).__name__
            else:
                continue  # segment finished (pos > fin) / el segmento terminó

            # Progress during the attempt does not count as a failed attempt.
            intento = 0 if pos > pos_antes else intento + 1
            if intento > self.reintentos:
                raise ErrorFatal(t("e_reintentos", i=idx, c=causa))
            self._espera(intento, retry_after)
        self.estado.marcar(idx)
        return True

    def ejecutar(self) -> str:
        """EN: returns 'completo' or raises. ES: devuelve 'completo' o lanza excepción."""
        pendientes = [i for i in range(self.n_chunks) if i not in self.estado.hechos]
        if not pendientes:
            return "completo"
        pool = ThreadPoolExecutor(max_workers=self.conexiones)
        try:
            futuros = {pool.submit(self._bajar_chunk, i) for i in pendientes}
            while futuros:
                hechos, futuros = wait(futuros, timeout=0.5, return_when=FIRST_EXCEPTION)
                for f in hechos:
                    f.result()  # re-raises ErrorFatal from any worker
        finally:
            self.parar.set()
            pool.shutdown(wait=True)
            self.estado.guardar(forzar=True)
        return "completo"


def descarga_simple(url, parcial, total, timeout, progreso) -> None:
    """EN: single connection, no resume (fallback without Range). ES: respaldo sin rangos."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(parcial, "wb") as f:
        while True:
            datos = r.read(TAM_LECTURA)
            if not datos:
                break
            f.write(datos)
            progreso.sumar(len(datos))


# --------------------------------------------------------------------------
# Integrity check / Verificación de integridad
# --------------------------------------------------------------------------

def nuevo_md5():
    try:
        return hashlib.md5(usedforsecurity=False)  # Python 3.9+
    except TypeError:
        return hashlib.md5()


def calcular_md5(ruta: str) -> str:
    h = nuevo_md5()
    total = os.path.getsize(ruta)
    leido = 0
    ultimo = 0.0
    with open(ruta, "rb") as f:
        while True:
            bloque = f.read(TAM_HASH)
            if not bloque:
                break
            h.update(bloque)
            leido += len(bloque)
            if time.time() - ultimo > 0.5 and sys.stderr.isatty():
                ultimo = time.time()
                sys.stderr.write(f"\r{ES.gris(t('verificando_barra'))} {leido * 100 / total:5.1f}%")
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 40 + "\r")
    return h.hexdigest()


# --------------------------------------------------------------------------
# Main program / Programa principal
# --------------------------------------------------------------------------

def leer_argumentos(argv=None):
    global IDIOMA
    # First pass: only --lang, so --help can be shown in the right language.
    previo = argparse.ArgumentParser(add_help=False)
    previo.add_argument("--lang", default="auto", choices=["auto", "es", "en"])
    conocido, _ = previo.parse_known_args(argv)
    IDIOMA = detectar_idioma(conocido.lang)

    p = argparse.ArgumentParser(prog="descargar_zenodo.py", description=t("h_desc"))
    p.add_argument("url", nargs="?", default=URL_POR_DEFECTO, help=t("h_url"))
    p.add_argument("-o", "--salida", default=".", metavar="DIR", help=t("h_salida"))
    p.add_argument("-n", "--conexiones", type=int, default=4, metavar="N", help=t("h_conex"))
    p.add_argument("--segmento-mb", type=int, default=8, metavar="MB", help=t("h_seg"))
    p.add_argument("--reintentos", type=int, default=8, metavar="N", help=t("h_reint"))
    p.add_argument("--timeout", type=float, default=30.0, metavar="SEC", help=t("h_timeout"))
    p.add_argument("--nombre", default=None, metavar="NAME", help=t("h_nombre"))
    p.add_argument("--md5", default=None, metavar="SUM", help=t("h_md5"))
    p.add_argument("--sin-verificar", action="store_true", help=t("h_sinverif"))
    p.add_argument("--reiniciar", action="store_true", help=t("h_reiniciar"))
    p.add_argument("--lang", default="auto", choices=["auto", "es", "en"], help=t("h_lang"))
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p.parse_args(argv)


def principal(argv=None) -> int:
    a = leer_argumentos(argv)

    if a.conexiones < 1:
        error(t("conexiones_min"))
        return 1
    if a.conexiones > 8:
        aviso(t("conexiones_muchas"))
    tam_chunk = max(1, a.segmento_mb) * 1024 * 1024

    nombre = a.nombre or os.path.basename(urllib.parse.urlparse(a.url).path) or "descarga.bin"
    carpeta = os.path.abspath(os.path.expanduser(a.salida))
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre)
    parcial = destino + ".part"
    ruta_estado = destino + ".estado.json"

    print(ES.negrita(ES.azul(t("titulo"))) + ES.gris(f"  v{VERSION}"), file=sys.stderr)
    info(t("archivo", v=nombre))
    info(t("destino", v=carpeta))

    # Expected MD5: argument > known table > Zenodo API.
    esperado = None
    if not a.sin_verificar:
        esperado = a.md5 or md5_desde_zenodo(a.url, nombre, a.timeout)
        esperado = esperado.lower() if esperado else None
        if esperado:
            info(t("md5_esperado", v=esperado))
        else:
            aviso(t("sin_md5"))

    # Final file already there: verify and exit.
    if os.path.exists(destino) and not a.reiniciar:
        ok(t("ya_existe", v=nombre))
        if esperado:
            info(t("verif_existente"))
            if calcular_md5(destino) == esperado:
                ok(t("nada_que_hacer"))
                return 0
            aviso(t("md5_no_coincide_existente"))
            return 2
        return 0

    if a.reiniciar:
        for ruta in (parcial, ruta_estado):
            if os.path.exists(ruta):
                os.remove(ruta)
        info(t("avance_borrado"))

    try:
        total, acepta_rangos = sondear(a.url, a.timeout)
    except ErrorFatal as e:
        error(str(e))
        return 1
    if not total:
        error(t("sin_tamano"))
        return 1
    info(t("tamano", t=humano(total), r=t("si") if acepta_rangos else t("no")))

    ya = os.path.getsize(parcial) if os.path.exists(parcial) else 0
    libre = shutil.disk_usage(carpeta).free
    if libre + ya < total:
        error(t("espacio", n=humano(total), l=humano(libre)))
        return 1

    t0 = time.time()

    if not acepta_rangos:
        aviso(t("sin_rangos"))
        progreso = Progreso(total, 0, 1)
        progreso.iniciar()
        try:
            descarga_simple(a.url, parcial, total, a.timeout, progreso)
        except KeyboardInterrupt:
            progreso.detener()
            aviso(t("interrumpido_simple"))
            return 130
        except (OSError, http.client.HTTPException) as e:
            progreso.detener()
            error(t("fallo", v=e))
            return 1
        progreso.detener()
        bytes_sesion = total
    else:
        estado = Estado.cargar(ruta_estado)
        valido = (
            estado is not None
            and estado.url == a.url
            and estado.total == total
            and os.path.exists(parcial)
            and os.path.getsize(parcial) == total
        )
        if valido:
            ok(t("avance_previo", n=len(estado.hechos)))
        else:
            if estado is not None:
                aviso(t("avance_distinto"))
            estado = Estado(ruta_estado, a.url, total, tam_chunk)
            with open(parcial, "wb") as f:
                f.truncate(total)  # reserves the full size (sparse file)
            estado.guardar(forzar=True)

        d_inicial = sum(
            min(estado.tam_chunk, total - i * estado.tam_chunk) for i in estado.hechos
        )
        progreso = Progreso(total, d_inicial, a.conexiones)
        desc = Descargador(a.url, parcial, estado, a.conexiones, a.reintentos, a.timeout, progreso)
        info(t("segmentos", n=desc.n_chunks, m=estado.tam_chunk // (1024 * 1024), c=a.conexiones))
        progreso.iniciar()
        try:
            desc.ejecutar()
        except KeyboardInterrupt:
            progreso.detener()
            aviso(t("interrumpido"))
            aviso(t("reanudar"))
            return 130
        except ErrorFatal as e:
            progreso.detener()
            error(str(e))
            aviso(t("puedes_reanudar"))
            return 1
        progreso.detener()
        bytes_sesion = total - d_inicial

    dur = max(time.time() - t0, 0.001)
    ok(t("completa", t=tiempo(dur), v=humano(bytes_sesion / dur)))

    if esperado:
        info(t("verificando"))
        obtenido = calcular_md5(parcial)
        if obtenido != esperado:
            error(t("md5_distinto", e=esperado, o=obtenido))
            error(t("md5_corrupto"))
            return 2
        ok(t("md5_correcto"))
    os.replace(parcial, destino)
    if os.path.exists(ruta_estado):
        os.remove(ruta_estado)
    ok(t("listo", v=destino))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(principal())
    except KeyboardInterrupt:
        sys.exit(130)
