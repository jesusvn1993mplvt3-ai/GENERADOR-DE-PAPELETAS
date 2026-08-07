# recursos-base

Recursos base del Generador de Papeletas (Jesús).

## Estructura

```
recursos-base/
├── README.md              ← este archivo
├── stickers/              ← 59 stickers WebP (estáticos y animados)
│   └── *.webp
└── scripts/
    └── inject_sticker_metadata.py   ← inyector de metadata EXIF/JSON para WhatsApp
```

## inyectar metadata en los stickers (WhatsApp)

El cliente de WhatsApp requiere que cada sticker WebP contenga un bloque JSON
dentro de su chunk `EXIF` con los campos `sticker-pack-id`, `sticker-pack-name`
y `sticker-pack-publisher` (y opcionalmente `emojis`). El script
`scripts/inject_sticker_metadata.py` realiza esa inyección sin alterar la
imagen: opera a nivel de chunks RIFF, por lo que la animación y la
transparencia se conservan bit a bit.

### Uso rápido

```bash
# Un archivo
python3 recursos-base/scripts/inject_sticker_metadata.py \
    recursos-base/stickers/STK-20241226-WA0000.webp \
    --pack-id com.jesus.pegatinas \
    --pack-name "Papeletas de Jesús" \
    --pack-publisher "Jesús" \
    --emojis "🎉,✨"

# Todo el directorio (recursivo) con archivo de configuración
python3 recursos-base/scripts/inject_sticker_metadata.py recursos-base/stickers/ \
    --config sticker_config.json

# Modo prueba (no toca los originales, salida en temporal)
python3 ... --dry-run

# Solo leer la metadata actual de un sticker
python3 ... STK-20241226-WA0000.webp --read
```

### Formato de sticker_config.json

```json
{
    "sticker-pack-id": "com.jesus.pegatinas",
    "sticker-pack-name": "Papeletas de Jesús",
    "sticker-pack-publisher": "Jesús",
    "emojis": ["🎉", "✨"],
    "emoji-map": {
        "STK-20241226-WA0000.webp": ["😀", "🎂"]
    }
}
```

### Notas importantes

- **Límite de peso**: WhatsApp rechaza stickers de más de 500 KB. Tres stickers
  del conjunto exceden ese límite y el script los marca con `WARN`:
  `STK-20240401-WA0004.webp` (553 KB), `STK-20250806-WA0005.webp` (931 KB) y
  `STK-20260429-WA0016.webp` (547 KB). Es recomendable recomprimirlos antes de
  enviarlos.
- **Formato EXIF**: el script replica el layout propietario de WhatsApp
  (tag `0x5741` / `UNDEFINED` / count / offset), validado contra los stickers
  reales de la colección.
- **Idempotencia**: ejecutarlo varias veces sobre el mismo archivo es seguro;
  el bloque EXIF se sobrescribe, nunca se anida.
- **Dependencias**: solo Python 3.8+ y Pillow.

Autor: Manus AI para Jesús — 2026
