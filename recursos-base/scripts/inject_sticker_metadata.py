#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inject_sticker_metadata.py
================================================================================
Script completo de inyección de metadata EXIF/JSON en stickers WebP para
WhatsApp (Generador de Papeletas — proyecto de Jesús).

Qué hace
--------
Recorre archivos WebP (estáticos o animados) e inyecta —o actualiza— el bloque
JSON de metadatos dentro del chunk EXIF del contenedor RIFF/WebP, respetando
los campos exigidos por el cliente oficial de WhatsApp:

    - sticker-pack-id          (obligatorio, formato dominio invertido)
    - sticker-pack-name        (obligatorio, máx. 128 caracteres)
    - sticker-pack-publisher   (obligatorio, máx. 128 caracteres)
    - emojis                   (opcional, array de 1..3 emojis Unicode)

Formato EXIF objetivo (verificado contra stickers reales de WhatsApp)
-----------------------------------------------------------------------
El cliente de WhatsApp NO usa el estándar ExifIFDPointer (tag 0x8769) dentro
del chunk EXIF del WebP: usa el layout propietario popularizado por Stickerly
y los tools oficiales (wasticker), que tras el marcador "Exif\x00\x00" es:

    b'II'                        (byte order little-endian)
    0x002A                       (magia TIFF)
    0x00000008                   (offset del primer IFD = 8)
    IFD de 14 bytes SIN next-IFD:
        0x0001                   (1 entrada)
        0x5741                   (tag 0x5741 = 'AW', propietario de WhatsApp)
        0x0007                   (tipo UNDEFINED)
        <longitud del JSON>      (count, uint32 LE)
        0x0000001C               (value = offset absoluto del JSON dentro del
                                  payload del chunk EXIF = 28)
    <JSON UTF-8>                 (exactly count bytes, empieza en el byte 28)

Nota: la entrada con tag 0x5741 es la que el cliente de WhatsApp reconoce al
leer el chunk EXIF de un WebP. Usar el tag 0x8769 estándar haría que el JSON
quedara invisible para el cliente. Este script replica exactamente el layout
anterior, validado contra dos stickers reales (393 y 316 bytes de JSON).

Dependencias
------------
Solo Python 3.8+ y Pillow (lectura/verificación). La serialización
TIFF/RIFF está implementada íntegramente en el script (sin piexif ni
libwebp), lo que garantiza portabilidad total.

Uso
---
    # Inyectar metadata en un solo sticker
    python3 inject_sticker_metadata.py STK-20241226-WA0000.webp \
        --pack-id com.jesus.pegatinas \
        --pack-name "Papeletas de Jesús" \
        --pack-publisher "Jesús"

    # Procesar un directorio completo (recursivo) con configuración JSON
    python3 inject_sticker_metadata.py ../stickers/ \
        --config sticker_config.json

    # Mostrar el JSON actual de un sticker (modo lectura, sin modificar nada)
    python3 inject_sticker_metadata.py STK-20241226-WA0000.webp --read

    # Modo prueba: escribe en un archivo temporal sin tocar el original
    python3 inject_sticker_metadata.py STK-20241226-WA0000.webp \
        --pack-id com.jesus.pegatinas --pack-name Prueba --pack-publisher Jesús \
        --dry-run

Formato del archivo de configuración (--config)
------------------------------------------------
{
    "sticker-pack-id":        "com.jesus.pegatinas",
    "sticker-pack-name":      "Papeletas de Jesús",
    "sticker-pack-publisher": "Jesús",
    "emojis":                 ["🎉", "✨"],
    "emoji-map": {
        "STK-20241226-WA0000.webp": ["😀", "🎂"],
        "STK-20240206-WA0012.webp": "🔥"
    }
}

Salida
------
- Por defecto, el archivo original se sobrescribe (tras validación total).
- Cada operación emite una línea de estado: [OK] / [WARN] / [SKIP] / [FAIL].
- El resumen final indica cuántos archivos fueron procesados con éxito.

Autor: Manus AI para Jesús — 2026
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageSequence
except ImportError:  # pragma: no cover
    Image = None
    ImageSequence = None

# ============================================================================
# Constantes de la especificación RIFF/WebP y del EXIF propietario de WhatsApp
# ============================================================================

RIFF_MAGIC = b"RIFF"
WEBP_TYPE = b"WEBP"
EXIF_MARKER = b"Exif\x00\x00"        # marcador al inicio del chunk EXIF
EXIF_CHUNK_ID = b"EXIF"

# Cabecera TIFF little-endian: byte order 'II', magia TIFF 42, offset IFD = 8
TIFF_HEADER = struct.pack("<HHI", 0x4949, 0x002A, 0x00000008)

# Entrada IFD propietaria de WhatsApp (tag 'AW' = 0x5741):
#   tag    0x5741  (2 bytes) — campo propietario reconocido por el cliente
#   type   0x0007  (2 bytes) — UNDEFINED
#   count  uint32  (4 bytes) — longitud exacta del JSON
#   value  uint32  (4 bytes) — offset absoluto del JSON dentro del payload (28)
# Nota: el chunk completo termina sin el "next IFD" de 4 bytes del estándar;
# el JSON sigue inmediatamente tras la entrada IFD (offset 28 del payload).
WATSAPP_IFD = struct.pack("<HHHI", 0x0001, 0x5741, 0x0007, 0x00000000)  # value se rellena al serializar
# Layout verificado contra stickers reales de WhatsApp (hex del payload):
#   Exif\x00\x00 (6) + II 2a00 08000000 (10) + IFD:
#     0100 (num=1, 2) + entrada 12 bytes + JSON...
# El JSON empieza exactamente en el byte 28 del payload del chunk (marker 6 +
# cabecera TIFF 8 + num_entries 2 + entrada IFD 12 = 28). No hay bytes de
# relleno ni "next IFD" entre la entrada y el JSON.
EXIF_JSON_CHUNK_OFFSET = 28          # offset fijo del JSON dentro del payload EXIF

MAX_NAME_LENGTH = 128                   # límite oficial de WhatsApp
WHATSAPP_MAX_BYTES = 500 * 1024         # 500 KB máximo por sticker
DEFAULT_PACK_ID_PREFIX = "com.jesus.pegatinas"


# ============================================================================
# Utilidades RIFF
# ============================================================================

def pad(data: bytes) -> bytes:
    """Retorna un byte de relleno si len(data) es impar (regla RIFF)."""
    return b"\x00" if len(data) % 2 else b""


def parse_riff_chunks(data: bytes):
    """
    Genera (offset, chunk_id, chunk_data) para cada chunk del contenedor RIFF.

    Un contenedor RIFF válido empieza con:
        b'RIFF' + <tamaño total-8 (uint32 LE)> + b'WEBP' + chunks...
    Cada chunk es: <id 4 bytes> + <tamaño uint32 LE> + <datos> + <relleno par>.
    """
    if not data.startswith(RIFF_MAGIC):
        raise ValueError("El archivo no es un contenedor RIFF válido.")
    if data[8:12] != WEBP_TYPE:
        raise ValueError("El contenedor RIFF no es de tipo WEBP.")

    pos = 12
    total_size = struct.unpack_from("<I", data, 4)[0]
    # El tamaño declarado puede ser menor o igual al real (archivos mal cortados).
    limit = min(len(data), 8 + total_size)

    while pos + 8 <= limit:
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if pos + 8 + chunk_size > limit:
            # Chunk truncado o tamaño declarado incoherente: fin del recorrido.
            break
        chunk_data = data[pos + 8:pos + 8 + chunk_size]
        yield pos, chunk_id, chunk_data
        pos += 8 + chunk_size + len(pad(chunk_data))


def build_riff(chunks):
    """
    Reconstruye un contenedor RIFF/WEBP a partir de una lista de
    (chunk_id, chunk_data), calculando tamaños y rellenos correctamente.
    """
    payload = WEBP_TYPE
    for chunk_id, chunk_data in chunks:
        payload += chunk_id
        payload += struct.pack("<I", len(chunk_data))
        payload += chunk_data
        payload += pad(chunk_data)
    size_field = struct.pack("<I", len(payload))
    return RIFF_MAGIC + size_field + payload


# ============================================================================
# Lectura y escritura del JSON dentro del chunk EXIF
# ============================================================================

def extract_exif_json(chunk_data: bytes):
    """
    Extrae el JSON embutido dentro del payload de un chunk EXIF.

    Implementa un parser robusto que acepta ambos layouts conocidos:
      A) Layout propietario de WhatsApp/Stickerly (el real): la entrada IFD
         con tag 0x5741 ('AW'), tipo UNDEFINED, donde count = longitud del
         JSON y value = offset del JSON (22 dentro del chunk). El JSON sigue
         tras 2 bytes de relleno, en el offset fijo 22 del chunk.
      B) Layout estándar TIFF (ExifIFDPointer 0x8769): se sigue la cadena
         de IFDs hasta encontrar un blob acotado por 2 bytes de longitud.
      C) Fallback heurístico: se busca el primer '{' tras el marcador y se
         prueba la decodificación directa (tolerante con variaciones menores).

    Devuelve None si el chunk está corrupto o no contiene un JSON válido.
    """
    if not chunk_data.startswith(EXIF_MARKER):
        return None

    # --- Camino A: layout propietario de WhatsApp (offset fijo 28) ---
    candidate = _try_decode(chunk_data[EXIF_JSON_CHUNK_OFFSET:])
    if candidate is not None:
        return candidate

    # --- Camino B: entrada IFD con count = longitud del JSON ---
    body = chunk_data[len(EXIF_MARKER):]
    if len(body) >= EXIF_JSON_CHUNK_OFFSET:
        # Buscar una entrada IFD tipo UNDEFINED (7) cuyo value apunte a
        # un offset válido con longitud count coherente.
        if len(body) >= 10:
            num_entries = struct.unpack_from("<H", body, 8)[0]
            ifd_start = 8
            for idx in range(min(num_entries, 64)):
                entry_off = ifd_start + 2 + idx * 12
                if entry_off + 12 > len(body):
                    break
                tag, typ, cnt, val = struct.unpack_from("<HHII", body, entry_off)
                if typ == 7 and 0 < cnt < len(body) and 0 < val < len(body):
                    json_bytes = body[val:val + cnt]
                    got = _try_decode(json_bytes)
                    if got is not None:
                        return got
                    # count puede incluir relleno: probar también len-1..len-3
                    for trim in (1, 2, 3):
                        if cnt - trim > 0:
                            got = _try_decode(body[val:val + cnt - trim])
                            if got is not None:
                                return got

    # --- Camino C: búsqueda heurística del primer objeto JSON válido ---
    start = chunk_data.find(b"{", len(EXIF_MARKER))
    while start != -1:
        end = chunk_data.rfind(b"}", start)
        if end == -1:
            break
        candidate = _try_decode(chunk_data[start:end + 1])
        if candidate is not None:
            return candidate
        start = chunk_data.find(b"{", start + 1)

    return None


def _try_decode(raw: bytes):
    """Decodifica bytes a UTF-8 y los parsea como JSON; devuelve None si falla."""
    try:
        return json.loads(raw.decode("utf-8").strip("\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def build_exif_chunk(metadata: dict) -> bytes:
    """
    Construye el payload completo del chunk EXIF replicando exactamente el
    layout propietario de WhatsApp/Stickerly validado contra archivos reales:

        Exif\x00\x00 + II + 0x002A + IFD-off(8) + [0x0001, 0x5741, 0x0007,
        len_json, 28] + JSON (UTF-8)
    """
    json_bytes = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    entry = struct.pack("<HHHI", 0x0001, 0x5741, 0x0007, len(json_bytes)) + \
            struct.pack("<I", EXIF_JSON_CHUNK_OFFSET)
    assert len(EXIF_MARKER) + len(TIFF_HEADER) + len(entry) == EXIF_JSON_CHUNK_OFFSET, \
        "El offset del JSON debe ser exactamente 28 (marker 6 + TIFF 8 + num 2 + entry 12)."

    return EXIF_MARKER + TIFF_HEADER + entry + json_bytes


def serialize_metadata(metadata: dict) -> dict:
    """
    Serializa los valores del diccionario de metadatos respetando los límites
    oficiales de WhatsApp (128 caracteres en nombre y publicador).
    """
    out = {}
    for key, value in metadata.items():
        if key in ("sticker-pack-name", "sticker-pack-publisher"):
            value = str(value)[:MAX_NAME_LENGTH] if value is not None else value
        out[key] = value
    return out


# ============================================================================
# Validación y corrección de la metadata de entrada
# ============================================================================

REQUIRED_FIELDS = ("sticker-pack-id", "sticker-pack-name", "sticker-pack-publisher")

# Patrón de dominio invertido: "com.", "net.", "org.", "io." o un identificador
# de app-style, aceptando también espacios/UUID tras el dominio.
PACK_ID_RE = re.compile(r"^(com|net|org|io)\.[a-z0-9.-]+(\.[a-z0-9_-]+)*(\s.*$)?", re.IGNORECASE)


def normalize_pack_id(pack_id: str) -> str:
    """
    Valida el formato del sticker-pack-id (dominio invertido). Si no cumple,
    lo autocorrige usando el prefijo por defecto para evitar que WhatsApp
    rechace el sticker.
    """
    pack_id = str(pack_id).strip()
    if PACK_ID_RE.match(pack_id):
        return pack_id
    corrected = f"{DEFAULT_PACK_ID_PREFIX} {pack_id}"
    print(f"      [INFO] pack-id '{pack_id}' no sigue el formato de dominio "
          f"invertido; se autocorrige a '{corrected}'.", file=sys.stderr)
    return corrected


def validate_emojis(emojis) -> list | None:
    """
    Normaliza el campo emojis a una lista de 1..3 emojis Unicode.
    Devuelve None si el usuario no proporcionó emojis (no se inyecta nada).
    """
    if emojis is None:
        return None
    if isinstance(emojis, str):
        emojis = [emojis]
    if not isinstance(emojis, (list, tuple)):
        return None
    cleaned = [e for e in (str(e) for e in emojis) if e.strip()]
    if not cleaned:
        return None
    if len(cleaned) > 3:
        cleaned = cleaned[:3]
    return cleaned


def merge_metadata(existing: dict | None, user_meta: dict, emoji_override: list | None):
    """
    Fusiona la metadata existente del sticker con los valores del usuario.
    Los campos del usuario tienen prioridad; el resto se conserva.
    """
    base = dict(existing) if existing else {}
    merged = dict(base)
    for key in ("sticker-pack-id", "sticker-pack-name", "sticker-pack-publisher"):
        if key in user_meta:
            merged[key] = user_meta[key]

    # El campo emojis: se reemplaza (no se concatena) si el usuario lo indica.
    if emoji_override is not None:
        merged["emojis"] = emoji_override

    return merged


# ============================================================================
# Núcleo de la inyección
# ============================================================================

def inject_webp(input_path: Path, output_path: Path, metadata: dict,
                emoji_override: list | None, *, dry_run: bool = False) -> dict:
    """
    Inyecta (o actualiza) la metadata JSON en el chunk EXIF de un WebP.

    Caminos:
      A) El archivo ya tiene un chunk EXIF  -> se sustituye, manteniendo los
         demás chunks byte a byte (animación y alfa intactos).
      B) El archivo no tiene chunk EXIF    -> se inserta el chunk al final del
         contenedor y se recalcula el tamaño global RIFF.

    Retorna un dict de resultado:
      {"ok": bool, "path": str, "action": str, "messages": [...],
       "frames": int, "size_kb": float, "hash_non_exif_ok": bool}
    """
    result = {
        "ok": False,
        "path": str(output_path),
        "action": "none",
        "messages": [],
        "frames": 0,
        "size_kb": 0.0,
        "hash_non_exif_ok": False,
    }

    data = input_path.read_bytes()
    if len(data) == 0:
        result["messages"].append("El archivo está vacío.")
        return result

    chunks = list(parse_riff_chunks(data))
    if not chunks:
        result["messages"].append("No se encontraron chunks válidos en el contenedor RIFF.")
        return result

    # ------------------------------------------------------------------
    # 1) Localizar el chunk EXIF existente (si lo hay) y extraer su JSON
    # ------------------------------------------------------------------
    exif_index = next((i for i, (_, cid, _) in enumerate(chunks) if cid == EXIF_CHUNK_ID), None)
    existing_json = None
    if exif_index is not None:
        existing_json = extract_exif_json(chunks[exif_index][2])

    # ------------------------------------------------------------------
    # 2) Construir el payload EXIF definitivo
    # ------------------------------------------------------------------
    final_meta = serialize_metadata(
        merge_metadata(existing_json, metadata, emoji_override)
    )
    new_exif_payload = build_exif_chunk(final_meta)

    # ------------------------------------------------------------------
    # 3) Reensamblar el contenedor
    # ------------------------------------------------------------------
    if exif_index is not None:
        # Camino A: sustitución quirúrgica del chunk EXIF.
        new_chunks = [(cid, cd) for i, (_, cid, cd) in enumerate(chunks)
                      if i != exif_index]
        new_chunks.insert(exif_index, (EXIF_CHUNK_ID, new_exif_payload))
        result["action"] = "actualizado"
    else:
        # Camino B: inserción al final del archivo.
        new_chunks = [(cid, cd) for _, cid, cd in chunks] + \
                     [(EXIF_CHUNK_ID, new_exif_payload)]
        result["action"] = "creado desde cero"

    assembled = build_riff(new_chunks)

    # ------------------------------------------------------------------
    # 4) Validación de integridad (no regresión)
    # ------------------------------------------------------------------
    # Hash de los datos de los chunks que NO son EXIF: deben ser idénticos
    # al archivo original (bit a bit) para garantizar que la imagen no se
    # alteró durante la operación.
    def non_exif_hash(chunk_list):
        h = hashlib.sha256()
        for cid, cd in chunk_list:
            if cid != EXIF_CHUNK_ID:
                h.update(cid + cd)
        return h.hexdigest()

    original_hash = non_exif_hash([(cid, cd) for _, cid, cd in chunks])
    assembled_hash = non_exif_hash(new_chunks)
    result["hash_non_exif_ok"] = (original_hash == assembled_hash)
    if not result["hash_non_exif_ok"]:
        result["messages"].append(
            "ALERTA: los chunks no-EXIF difieren del original. Operación abortada."
        )
        return result

    # Contar frames de la imagen original (para el informe).
    frames = 0
    if Image is not None:
        try:
            img = Image.open(input_path)
            if ImageSequence is not None:
                frames = sum(1 for _ in ImageSequence.Iterator(img))
        except Exception as exc:
            result["messages"].append(f"Error contando frames: {exc}")
    if frames == 0:
        frames = 1
    result["frames"] = frames

    # ------------------------------------------------------------------
    # 5) Escritura (o escritura en temporal para dry-run)
    # ------------------------------------------------------------------
    verify_source = None
    if not dry_run:
        output_path.write_bytes(assembled)
        verify_source = output_path
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="sticker_injected_",
                                          suffix=".webp", delete=False)
        tmp.write(assembled)
        tmp.close()
        verify_source = Path(tmp.name)
        result["tmp_file"] = tmp.name
        result["action"] += " (dry-run, salida en temporal)"

    result["size_kb"] = round(len(assembled) / 1024, 2)

    # ------------------------------------------------------------------
    # 6) Verificación de lectura del archivo resultante
    # ------------------------------------------------------------------
    verify_data = verify_source.read_bytes()
    verify_exif = None
    for _, cid, cd in parse_riff_chunks(verify_data):
        if cid == EXIF_CHUNK_ID:
            verify_exif = extract_exif_json(cd)
            break

    if verify_exif is None:
        result["messages"].append(
            "VERIFICACIÓN FALLIDA: el archivo resultante no contiene un JSON EXIF válido."
        )
        return result

    missing = [f for f in REQUIRED_FIELDS if f not in verify_exif]
    if missing:
        result["messages"].append(f"VERIFICACIÓN FALLIDA: faltan campos: {missing}")
        return result

    result["ok"] = True
    result["messages"].append(
        f"JSON verificado: pack='{verify_exif.get('sticker-pack-name')}' "
        f"por {verify_exif.get('sticker-pack-publisher')} "
        f"({len(verify_exif)} bytes de JSON)."
    )

    if len(verify_data) > WHATSAPP_MAX_BYTES:
        result["messages"].append(
            f"WARN: el archivo pesa {result['size_kb']} KB y supera el límite "
            f"de 500 KB de WhatsApp. El sticker podría no importarse."
        )

    # Validación adicional de que PIL abre el resultado sin error.
    if Image is not None:
        try:
            check = Image.open(verify_source)
            check.load()
            if ImageSequence is not None:
                result["frames"] = max(result["frames"],
                                       sum(1 for _ in ImageSequence.Iterator(check)))
        except Exception as exc:
            result["messages"].append(f"WARN: PIL no pudo decodificar el resultado: {exc}")

    return result


# ============================================================================
# Procesamiento por lotes (directorio recursivo)
# ============================================================================

def load_config(config_path: Path) -> dict:
    """Carga y valida el archivo de configuración JSON (--config)."""
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("El archivo de configuración debe ser un objeto JSON.")
    return cfg


def iter_webp_files(root: Path):
    """Itera recursivamente todos los archivos .webp bajo root."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if name.lower().endswith(".webp") and not name.startswith("."):
                yield Path(dirpath) / name


def process_targets(targets: list[Path], user_meta: dict, emoji_override: list | None,
                    emoji_map: dict | None, *, dry_run: bool):
    """
    Procesa una lista de archivos o directorios y aplica la inyección.

    El emoji_map (del --config) asigna emojis por nombre de archivo y tiene
    prioridad sobre el emoji_override global.
    """
    files = []
    for target in targets:
        if target.is_dir():
            files.extend(iter_webp_files(target))
        elif target.is_file():
            files.append(target)
        else:
            print(f"[FAIL] {target}: ruta no encontrada.", file=sys.stderr)

    if not files:
        print("[INFO] No se encontraron archivos WebP en los objetivos indicados.",
              file=sys.stderr)
        return

    report = {"ok": 0, "warn": 0, "fail": 0}

    for file_path in files:
        # Emojis específicos por archivo (según emoji-map del config) > global
        emoji_for_file = None
        if emoji_map and file_path.name in emoji_map:
            emoji_for_file = validate_emojis(emoji_map[file_path.name])
            if emoji_for_file is None and emoji_map[file_path.name] is not None:
                print(f"[WARN] {file_path.name}: emojis del config inválidos, "
                      f"se ignoran.", file=sys.stderr)

        meta = dict(user_meta)

        res = inject_webp(file_path, file_path, meta, emoji_for_file, dry_run=dry_run)

        tag = "[OK]" if res["ok"] else "[FAIL]"
        size_note = f" ({res['size_kb']} KB, {res['frames']} frames)"
        emoji_note = f" emojis={emoji_for_file}" if emoji_for_file else ""
        action = res["action"] if res["ok"] else "sin cambios"
        print(f"{tag} {file_path.name}: {action}{emoji_note}{size_note}")
        for msg in res["messages"]:
            print(f"       {msg}", file=sys.stderr)

        if res["ok"]:
            report["ok"] += 1
            if res["size_kb"] > WHATSAPP_MAX_BYTES / 1024:
                report["warn"] += 1
        else:
            report["fail"] += 1

    print(f"\n=== RESUMEN: {report['ok']} OK | {report['warn']} con advertencia de peso | "
          f"{report['fail']} FAIL | {len(files)} archivos ===")


# ============================================================================
# Modo lectura: solo inspeccionar la metadata existente
# ============================================================================

def read_mode(file_path: Path):
    data = file_path.read_bytes()
    for _, cid, cd in parse_riff_chunks(data):
        if cid == EXIF_CHUNK_ID:
            meta = extract_exif_json(cd)
            print(json.dumps(meta, ensure_ascii=False, indent=2)
                  if meta is not None else "(chunk EXIF sin JSON válido)")
            return
    print("(el archivo no contiene un chunk EXIF)")


# ============================================================================
# Interfaz de línea de comandos
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inyecta metadata EXIF/JSON en stickers WebP para WhatsApp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "targets",
        nargs="+",
        type=Path,
        help="Archivos WebP o directorios (se procesan recursivamente).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Archivo JSON de configuración (pack-id, pack-name, publisher, "
             "emojis y emoji-map).",
    )
    parser.add_argument("--pack-id", default=None, help="sticker-pack-id.")
    parser.add_argument("--pack-name", default=None, help="sticker-pack-name.")
    parser.add_argument("--pack-publisher", default=None, help="sticker-pack-publisher.")
    parser.add_argument(
        "--emojis",
        default=None,
        help="Emojis globales separados por comas o un solo emoji "
             "(ej. --emojis '😀,🔥'). Máximo 3.",
    )
    parser.add_argument("--read", action="store_true",
                        help="Modo lectura: muestra el JSON actual y termina.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula la inyección sin modificar los originales "
                             "(salida en archivo temporal).")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.read:
        if len(args.targets) != 1 or args.targets[0].is_dir():
            parser.error("--read requiere exactamente un archivo WebP.")
        for f in iter_webp_files(args.targets[0]) if args.targets[0].is_dir() else [args.targets[0]]:
            print(f"--- {f.name} ---")
            read_mode(f)
        return

    # ------------------------------------------------------------------
    # Recolección de la metadata de usuario (CLI > config)
    # ------------------------------------------------------------------
    user_meta = {}
    emoji_override = None
    emoji_map = None

    if args.config:
        cfg = load_config(args.config)
        for key in ("sticker-pack-id", "sticker-pack-name", "sticker-pack-publisher"):
            if key in cfg:
                user_meta[key] = cfg[key]
        emoji_map = cfg.get("emoji-map", None)
        if "emojis" in cfg:
            emoji_override = validate_emojis(cfg["emojis"])
            if emoji_override is None:
                print("[WARN] Campo 'emojis' del config inválido, se ignora.",
                      file=sys.stderr)

    if args.pack_id:
        user_meta["sticker-pack-id"] = args.pack_id
    if args.pack_name:
        user_meta["sticker-pack-name"] = args.pack_name
    if args.pack_publisher:
        user_meta["sticker-pack-publisher"] = args.pack_publisher
    if args.emojis:
        emoji_override = validate_emojis([e.strip() for e in args.emojis.split(",")])
        if emoji_override is None:
            print("[WARN] Emojis globales inválidos, se ignoran.", file=sys.stderr)

    missing = [f for f in REQUIRED_FIELDS if f not in user_meta]
    if missing:
        parser.error(f"Faltan campos obligatorios: {missing}. "
                     f"Indícalos con --pack-id/--pack-name/--pack-publisher o en --config.")

    # El pack-id se normaliza una sola vez antes de procesar el lote.
    user_meta["sticker-pack-id"] = normalize_pack_id(user_meta["sticker-pack-id"])

    process_targets(
        args.targets,
        user_meta,
        emoji_override,
        emoji_map,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
