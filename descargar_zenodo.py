#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
descargar_zenodo.py
===================

Descargador de archivos grandes (por ejemplo, un .zip de 10 GB alojado en
Zenodo) pensado para conexiones inestables o servidores lentos.

Que hace
--------
1. Divide el archivo en segmentos de tamano fijo (por defecto 8 MiB).
2. Descarga varios segmentos en paralelo, cada uno con su propia conexion
   HTTP y una cabecera ``Range``. Si el servidor limita la velocidad *por
   conexion*, varias conexiones suman ancho de banda.
3. Guarda el avance en un archivo de estado (``<nombre>.estado.json``).
   Si la red se cae, si cierras la terminal o si presionas Ctrl+C, al volver a
   ejecutar el mismo comando se retoman unicamente los segmentos que faltan.
4. Reintenta automaticamente con espera exponencial ante cortes de red.
5. Al terminar, verifica la integridad con MD5 y renombra el archivo.

Requisitos
----------
Python 3.8 o superior. No necesita instalar ninguna libreria externa.

Uso rapido
----------
    python descargar_zenodo.py
    python descargar_zenodo.py -n 6 -o ~/datos
    python descargar_zenodo.py "https://zenodo.org/records/<id>/files/<archivo>?download=1"

Nota sobre el uso responsable
-----------------------------
Zenodo es un servicio publico y gratuito. Usa un numero de conexiones
razonable (4 a 8 suele bastar) para no sobrecargar el servidor. Respeta la
licencia y los terminos de cita del conjunto de datos que descargues.

Codigos de salida
-----------------
0 = descarga completa y verificada
1 = error (red, disco, servidor)
2 = la descarga termino pero el MD5 no coincide
130 = interrumpida por el usuario (el progreso queda guardado)
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
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
# Configuracion general
# --------------------------------------------------------------------------

VERSION = "1.0.0"

# Registro de Zenodo con imagenes multiespectrales de dron (pulse crop dataset).
URL_POR_DEFECTO = "https://zenodo.org/records/8280431/files/Images.zip?download=1"

# Sumas MD5 publicadas por Zenodo para archivos conocidos: (registro, archivo).
MD5_CONOCIDOS = {
    ("8280431", "Images.zip"): "e81c4390d378860ee19bd91ec5b0bfd6",
}

AGENTE = f"descargar-zenodo/{VERSION} (reanudable; python-urllib)"
TAM_LECTURA = 256 * 1024          # bytes leidos por cada llamada a read()
TAM_HASH = 8 * 1024 * 1024        # bytes leidos por bloque al calcular MD5
CODIGOS_REINTENTABLES = {408, 429, 500, 502, 503, 504}


class ErrorFatal(Exception):
    """Error que no tiene sentido reintentar (disco lleno, 404, etc.)."""


# --------------------------------------------------------------------------
# Utilidades de presentacion
# --------------------------------------------------------------------------

def humano(n: float) -> str:
    """
    Convierte bytes a una cadena legible. Usa unidades decimales (1 GB =
    1000 MB), igual que Zenodo, para que las cifras coincidan con la pagina.
    """
    unidades = ["B", "kB", "MB", "GB", "TB"]
    n = float(n)
    for u in unidades:
        if n < 1000 or u == unidades[-1]:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1000
    return f"{n:.2f} TB"


def tiempo(seg: float) -> str:
    """Formatea segundos como HH:MM:SS (o --:-- si no es calculable)."""
    if seg is None or seg != seg or seg == float("inf") or seg < 0:
        return "--:--"
    seg = int(seg)
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Estilo:
    """Colores ANSI sobrios (azul acero, pizarra, ambar). Se desactivan solos."""

    def __init__(self) -> None:
        activo = sys.stderr.isatty() and "NO_COLOR" not in os.environ
        if activo and os.name == "nt":
            os.system("")  # habilita secuencias ANSI en Windows 10+
        self.activo = activo
        try:
            "█░".encode(sys.stderr.encoding or "ascii")
            self.bloques = ("█", "░")
        except (UnicodeEncodeError, LookupError):
            self.bloques = ("#", "-")

    def _c(self, codigo: str, texto: str) -> str:
        return f"\033[{codigo}m{texto}\033[0m" if self.activo else texto

    def azul(self, t: str) -> str:
        return self._c("38;5;74", t)

    def gris(self, t: str) -> str:
        return self._c("38;5;245", t)

    def verde(self, t: str) -> str:
        return self._c("38;5;72", t)

    def ambar(self, t: str) -> str:
        return self._c("38;5;179", t)

    def rojo(self, t: str) -> str:
        return self._c("38;5;167", t)

    def negrita(self, t: str) -> str:
        return self._c("1", t)


ES = Estilo()


def info(msg: str) -> None:
    print(f"{ES.azul('[info]')}  {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"{ES.verde('[ok]')}    {msg}", file=sys.stderr)


def aviso(msg: str) -> None:
    print(f"{ES.ambar('[aviso]')} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{ES.rojo('[error]')} {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Consulta al servidor
# --------------------------------------------------------------------------

def sondear(url: str, timeout: float):
    """
    Consulta el tamano total y si el servidor acepta descargas por rangos.

    Pide solo el primer byte (``Range: bytes=0-0``). Un servidor compatible
    responde 206 con la cabecera ``Content-Range: bytes 0-0/<total>``.

    Devuelve (tamano_total | None, acepta_rangos: bool).
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
        raise ErrorFatal(f"El servidor respondio HTTP {e.code} ({e.reason}) al consultar el archivo.")
    except urllib.error.URLError as e:
        raise ErrorFatal(f"No se pudo conectar con el servidor: {e.reason}")


def md5_desde_zenodo(url: str, nombre: str, timeout: float):
    """
    Intenta obtener el MD5 oficial desde la API publica de Zenodo.
    Devuelve None si no se puede (la descarga continua igualmente).
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
        if isinstance(archivos, dict):  # formato alternativo de la API
            archivos = archivos.get("entries", {}).values()
        for f in archivos:
            if f.get("key") == nombre and str(f.get("checksum", "")).startswith("md5:"):
                return f["checksum"].split(":", 1)[1]
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Estado persistente (para reanudar)
# --------------------------------------------------------------------------

class Estado:
    """
    Guarda que segmentos ya estan completos. Se escribe de forma atomica
    (archivo temporal + os.replace) para que un corte de luz no lo corrompa.
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
# Barra de progreso
# --------------------------------------------------------------------------

class Progreso:
    """Hilo que dibuja la barra: porcentaje, velocidad media reciente y ETA."""

    def __init__(self, total: int, inicial: int, conexiones: int) -> None:
        self.total = total
        self.bytes = inicial
        self.inicial = inicial
        self.conexiones = conexiones
        self._lock = threading.Lock()
        self._muestras = deque(maxlen=12)  # ~6 s de historial
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
            f"ETA {tiempo(eta)}  {ES.gris('conexiones: ' + str(self.conexiones))}"
        )
        if self._interactivo:
            sys.stderr.write("\r" + linea + ("\033[K" if ES.activo else "   "))
            if final:
                sys.stderr.write("\n")
        else:
            sys.stderr.write(linea + "\n")
        sys.stderr.flush()


# --------------------------------------------------------------------------
# Descarga por segmentos
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
        """Espera exponencial con algo de azar; se interrumpe si hay que parar."""
        base = float(retry_after) if retry_after else min(60.0, 2.0 ** intento)
        self.parar.wait(base + random.uniform(0, 1.0))

    def _bajar_chunk(self, idx: int) -> bool:
        """Descarga un segmento; retoma desde el ultimo byte recibido si falla."""
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
                        raise ErrorFatal(
                            f"El servidor dejo de aceptar rangos (HTTP {r.status})."
                        )
                    with open(self.parcial, "r+b") as f:
                        f.seek(pos)
                        while pos <= fin:
                            if self.parar.is_set():
                                return False
                            datos = r.read(min(TAM_LECTURA, fin - pos + 1))
                            if not datos:
                                raise ConnectionError("conexion cerrada antes de tiempo")
                            f.write(datos)
                            pos += len(datos)
                            self.progreso.sumar(len(datos))
            except ErrorFatal:
                raise
            except urllib.error.HTTPError as e:
                if e.code not in CODIGOS_REINTENTABLES:
                    raise ErrorFatal(f"HTTP {e.code} ({e.reason}) en el segmento {idx}.")
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and not str(retry_after).isdigit():
                    retry_after = None
                causa = f"HTTP {e.code}"
            except (OSError, http.client.HTTPException) as e:
                if getattr(e, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                    raise ErrorFatal("No queda espacio en disco.")
                causa = type(e).__name__
            else:
                continue  # el segmento termino bien (pos > fin)

            # Si hubo avance en este intento, no cuenta como intento fallido.
            intento = 0 if pos > pos_antes else intento + 1
            if intento > self.reintentos:
                raise ErrorFatal(
                    f"Se agotaron los reintentos en el segmento {idx} ({causa}). "
                    "Vuelve a ejecutar el mismo comando para reanudar."
                )
            self._espera(intento, retry_after)
        self.estado.marcar(idx)
        return True

    def ejecutar(self) -> str:
        """Devuelve 'completo', 'interrumpido' o lanza ErrorFatal."""
        pendientes = [i for i in range(self.n_chunks) if i not in self.estado.hechos]
        if not pendientes:
            return "completo"
        pool = ThreadPoolExecutor(max_workers=self.conexiones)
        resultado = "completo"
        try:
            futuros = {pool.submit(self._bajar_chunk, i) for i in pendientes}
            while futuros:
                hechos, futuros = wait(futuros, timeout=0.5, return_when=FIRST_EXCEPTION)
                for f in hechos:
                    f.result()  # relanza ErrorFatal si algun hilo fallo
        except KeyboardInterrupt:
            resultado = "interrumpido"
            raise
        finally:
            self.parar.set()
            pool.shutdown(wait=True)
            self.estado.guardar(forzar=True)
        return resultado


# --------------------------------------------------------------------------
# Descarga simple (respaldo si el servidor no admite rangos)
# --------------------------------------------------------------------------

def descarga_simple(url, parcial, total, timeout, progreso) -> None:
    """Una sola conexion, sin reanudacion. Solo se usa si no hay soporte Range."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(parcial, "wb") as f:
        while True:
            datos = r.read(TAM_LECTURA)
            if not datos:
                break
            f.write(datos)
            progreso.sumar(len(datos))


# --------------------------------------------------------------------------
# Verificacion de integridad
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
                sys.stderr.write(f"\r{ES.gris('verificando MD5')} {leido * 100 / total:5.1f}%")
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 40 + "\r")
    return h.hexdigest()


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------

def leer_argumentos(argv=None):
    p = argparse.ArgumentParser(
        prog="descargar_zenodo.py",
        description="Descarga archivos grandes con conexiones paralelas, "
        "reanudacion automatica y verificacion MD5.",
    )
    p.add_argument("url", nargs="?", default=URL_POR_DEFECTO,
                   help="URL directa del archivo (por defecto: Images.zip del registro 8280431)")
    p.add_argument("-o", "--salida", default=".", metavar="CARPETA",
                   help="carpeta de destino (por defecto: la actual)")
    p.add_argument("-n", "--conexiones", type=int, default=4, metavar="N",
                   help="conexiones simultaneas (por defecto: 4; recomendado 4-8)")
    p.add_argument("--segmento-mb", type=int, default=8, metavar="MB",
                   help="tamano de cada segmento en MiB (por defecto: 8)")
    p.add_argument("--reintentos", type=int, default=8, metavar="N",
                   help="reintentos consecutivos por segmento (por defecto: 8)")
    p.add_argument("--timeout", type=float, default=30.0, metavar="SEG",
                   help="tiempo maximo de espera de red en segundos (por defecto: 30)")
    p.add_argument("--nombre", default=None,
                   help="nombre del archivo final (por defecto: el de la URL)")
    p.add_argument("--md5", default=None,
                   help="suma MD5 esperada (por defecto se busca en Zenodo)")
    p.add_argument("--sin-verificar", action="store_true",
                   help="omitir la verificacion MD5 al final")
    p.add_argument("--reiniciar", action="store_true",
                   help="borrar el avance guardado y empezar de cero")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p.parse_args(argv)


def principal(argv=None) -> int:
    a = leer_argumentos(argv)

    if a.conexiones < 1:
        error("--conexiones debe ser al menos 1.")
        return 1
    if a.conexiones > 8:
        aviso("Usar mas de 8 conexiones puede ser bloqueado o mal visto por el servidor. "
              "Zenodo es un servicio compartido: usa el minimo que te funcione.")
    tam_chunk = max(1, a.segmento_mb) * 1024 * 1024

    nombre = a.nombre or os.path.basename(urllib.parse.urlparse(a.url).path) or "descarga.bin"
    carpeta = os.path.abspath(os.path.expanduser(a.salida))
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre)
    parcial = destino + ".part"
    ruta_estado = destino + ".estado.json"

    print(ES.negrita(ES.azul("Descargador reanudable para Zenodo")) + ES.gris(f"  v{VERSION}"),
          file=sys.stderr)
    info(f"Archivo : {nombre}")
    info(f"Destino : {carpeta}")

    # MD5 esperado (argumento > tabla conocida > API de Zenodo).
    esperado = None
    if not a.sin_verificar:
        esperado = (a.md5 or md5_desde_zenodo(a.url, nombre, a.timeout))
        esperado = esperado.lower() if esperado else None
        if esperado:
            info(f"MD5 esperado: {esperado}")
        else:
            aviso("No se encontro un MD5 de referencia; no se podra verificar la integridad.")

    # Si el archivo final ya existe, verificarlo y salir.
    if os.path.exists(destino) and not a.reiniciar:
        ok(f"{nombre} ya existe.")
        if esperado:
            info("Verificando integridad del archivo existente...")
            if calcular_md5(destino) == esperado:
                ok("MD5 correcto. No hay nada que descargar.")
                return 0
            aviso("El MD5 no coincide. Usa --reiniciar para descargarlo de nuevo.")
            return 2
        return 0

    if a.reiniciar:
        for ruta in (parcial, ruta_estado):
            if os.path.exists(ruta):
                os.remove(ruta)
        info("Avance anterior eliminado.")

    # Consultar el servidor.
    try:
        total, acepta_rangos = sondear(a.url, a.timeout)
    except ErrorFatal as e:
        error(str(e))
        return 1
    if not total:
        error("El servidor no informa el tamano del archivo; no se puede planificar la descarga.")
        return 1
    info(f"Tamano  : {humano(total)}  |  Rangos: {'si' if acepta_rangos else 'no'}")

    # Espacio en disco necesario.
    ya = os.path.getsize(parcial) if os.path.exists(parcial) else 0
    libre = shutil.disk_usage(carpeta).free
    if libre + ya < total:
        error(f"Espacio insuficiente: se necesitan {humano(total)} y hay {humano(libre)} libres.")
        return 1

    t0 = time.time()

    # ----- Camino sin soporte de rangos: una sola conexion -----
    if not acepta_rangos:
        aviso("El servidor no admite descarga por rangos: se usara una sola conexion "
              "y no se podra reanudar.")
        progreso = Progreso(total, 0, 1)
        progreso.iniciar()
        try:
            descarga_simple(a.url, parcial, total, a.timeout, progreso)
        except KeyboardInterrupt:
            progreso.detener()
            aviso("Descarga interrumpida.")
            return 130
        except (OSError, http.client.HTTPException) as e:
            progreso.detener()
            error(f"Fallo la descarga: {e}")
            return 1
        progreso.detener()
        bytes_sesion = total
    else:
        # ----- Camino normal: segmentos paralelos con reanudacion -----
        estado = Estado.cargar(ruta_estado)
        valido = (
            estado is not None
            and estado.url == a.url
            and estado.total == total
            and os.path.exists(parcial)
            and os.path.getsize(parcial) == total
        )
        if valido:
            hechos = len(estado.hechos)
            ok(f"Avance previo encontrado: {hechos} segmentos completos. Se retoma desde ahi.")
        else:
            if estado is not None:
                aviso("El avance guardado no coincide con el archivo remoto; se empieza de cero.")
            estado = Estado(ruta_estado, a.url, total, tam_chunk)
            with open(parcial, "wb") as f:
                f.truncate(total)  # reserva el tamano completo (archivo disperso)
            estado.guardar(forzar=True)

        d_inicial = sum(
            min(estado.tam_chunk, total - i * estado.tam_chunk) for i in estado.hechos
        )
        progreso = Progreso(total, d_inicial, a.conexiones)
        desc = Descargador(a.url, parcial, estado, a.conexiones, a.reintentos, a.timeout, progreso)
        info(f"Segmentos: {desc.n_chunks} de {estado.tam_chunk // (1024 * 1024)} MiB  |  "
             f"Conexiones: {a.conexiones}")
        progreso.iniciar()
        try:
            desc.ejecutar()
        except KeyboardInterrupt:
            progreso.detener()
            aviso("Interrumpido. El progreso quedo guardado.")
            aviso("Ejecuta el mismo comando para continuar donde quedo.")
            return 130
        except ErrorFatal as e:
            progreso.detener()
            error(str(e))
            aviso("El progreso quedo guardado; puedes reanudar con el mismo comando.")
            return 1
        progreso.detener()
        bytes_sesion = total - d_inicial

    dur = max(time.time() - t0, 0.001)
    ok(f"Descarga completa en {tiempo(dur)}  (media de esta sesion: {humano(bytes_sesion / dur)}/s)")

    # ----- Verificacion e instalacion del archivo final -----
    if esperado:
        info("Verificando integridad (MD5)...")
        obtenido = calcular_md5(parcial)
        if obtenido != esperado:
            error(f"MD5 distinto. Esperado {esperado}, obtenido {obtenido}.")
            error("El archivo pudo corromperse. Ejecuta de nuevo con --reiniciar.")
            return 2
        ok("MD5 correcto.")
    os.replace(parcial, destino)
    if os.path.exists(ruta_estado):
        os.remove(ruta_estado)
    ok(f"Listo: {destino}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(principal())
    except KeyboardInterrupt:
        sys.exit(130)
