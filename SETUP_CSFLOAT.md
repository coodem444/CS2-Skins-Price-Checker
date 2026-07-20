# CSFloat + `.env` — puesta en marcha (v3.2)

## 1. Instalar dependencias

```bash
pip install --upgrade -r requirements.txt
```

## 2. Configurar `.env`

```bash
cp .env.example .env
```

Rellena `NOTION_TOKEN` (obligatorio) y, si quieres CSFloat, `CSFLOAT_API_KEY`
(la generas en csfloat.com → tu perfil → pestaña "developer"). `DISCORD_WEBHOOK`
y `NOTION_DATABASE_ID` son opcionales/ya traen un valor por defecto.

**Importante:** regenera tu token de Notion y tu webhook de Discord actuales
antes de rellenar el `.env` — la versión anterior del script los tenía
escritos en claro dentro de `actualizar.py`, así que hay que darlos por
comprometidos.

## 3. Probar sin escribir nada

Pon `DRY_RUN = True` en la sección de configuración de `actualizar.py`,
ejecuta `python actualizar.py` y confirma en la consola que:
- aparece `CSFloat  :  ✅  activo` en el banner inicial,
- ves `🗂 CSFloat cache miss` / `✅ Precio encontrado en CSFloat` por cada skin,
- el resumen final no reporta errores de CSFloat.

Vuelve a poner `DRY_RUN = False` para que escriba en Notion de verdad.

## 4. Qué cambia en Notion

Se añade la columna **Precio CSFloat** (mismo patrón que Precio Steam /
Precio Skinport). **Precio Actual** y **Fuente Precio** ahora también
pueden apuntar a CSFloat si es la fuente más barata.

## 5. Códigos de error de CSFloat (qué hace el script)

| Código | Comportamiento |
|---|---|
| 401 | API key inválida → se desactiva CSFloat el resto de la ejecución, todo lo demás sigue igual |
| 403 | Sin permiso para el recurso → se registra y se continúa |
| 404 | Sin listings activos para esa skin → normal, no es un error |
| 429 | Reintento respetando `Retry-After`, o backoff exponencial si no viene esa cabecera |
| 5xx / timeout | Reintento con backoff exponencial (hasta `CSFLOAT_MAX_REINTENTOS`) |

## 6. Posibles mejoras futuras

- Modularizar `actualizar.py` en varios ficheros si el proyecto sigue creciendo (hoy se mantiene en uno solo a propósito, para no romper el lanzador `.bat` ni la filosofía "sin dependencias raras" del proyecto original).
- Añadir el precio de CSFloat también a los embeds de Discord.
- Sustituir la tasa de cambio USD→EUR fija de reserva por un fichero de caché en disco con fecha, para sobrevivir a reinicios sin perder precisión si Frankfurter falla varias ejecuciones seguidas.
