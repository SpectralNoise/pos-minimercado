# Diseño: Gestión de Turnos de Caja (Persistente)

**Fecha:** 2026-03-20
**Proyecto:** SFPos — POS para mini mercados colombianos
**Estado:** Aprobado

## Contexto

Actualmente el turno de caja vive en memoria (`cajaSession` JS). Si el servidor se reinicia o la página se recarga, el turno se pierde. El dueño del negocio necesita ver el historial de turnos en el panel admin para auditar diferencias y ventas por cajero. El negocio opera con un cajero por turno pero en el futuro puede tener múltiples cajas.

## Decisiones

- **Opción elegida:** Sesión persistida desde la apertura (no solo al cierre).
- **Multi-caja:** campo `caja_id` TEXT simple en la tabla; sin tabla `cajas` por ahora.
- **Historial:** solo visible en modo Admin, dentro de la sección Reportes.
- **Asociación venta→turno:** se agrega `turno_id` a `ventas` e `items_venta` para evitar depender de timestamps en la recuperación de sesión.

## Base de Datos

### Tabla `turnos` (nueva)

```sql
CREATE TABLE IF NOT EXISTS turnos (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  caja_id          TEXT     DEFAULT 'caja-1',
  cajero           TEXT     NOT NULL,
  estado           TEXT     DEFAULT 'abierto',   -- 'abierto' | 'cerrado'
  monto_inicial    REAL     DEFAULT 0,
  apertura_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  cierre_at        DATETIME DEFAULT NULL,
  efectivo_ventas  REAL     DEFAULT 0,
  transferencias   REAL     DEFAULT 0,
  total_ventas     REAL     DEFAULT 0,
  monto_contado    REAL     DEFAULT NULL,
  diferencia       REAL     DEFAULT NULL,   -- monto_contado - (monto_inicial + efectivo_ventas)
  num_tx           INTEGER  DEFAULT 0,
  resumen_ia       TEXT     DEFAULT NULL
);
```

### Migración `ventas` (existente)

Agregar columna `turno_id` a la tabla `ventas` existente:

```sql
ALTER TABLE ventas ADD COLUMN turno_id INTEGER REFERENCES turnos(id);
```

Esto se agrega en `init_db()` dentro del bloque `try/except` de migraciones, igual que se hizo con `thumbnail`. Las ventas existentes quedan con `turno_id = NULL` — compatible.

### Fórmula de diferencia

```
esperado_en_caja = monto_inicial + efectivo_ventas
diferencia       = monto_contado - esperado_en_caja
```

El frontend calcula `diferencia` antes de enviar el `PUT /cierre`. El servidor la almacena tal cual (no la recalcula).

## API

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/api/turnos` | Abre turno. Body: `{cajero, monto_inicial, caja_id?}`. Retorna el turno creado. Si ya existe un turno abierto, retorna `409` con `{error, turno_activo}`. |
| `GET` | `/api/turnos/activo` | Retorna el turno con `estado='abierto'` más reciente, o `null`. |
| `PUT` | `/api/turnos/:id/cierre` | Cierra el turno. Body: `{efectivo_ventas, transferencias, total_ventas, num_tx, monto_contado, diferencia, resumen_ia?}`. Requiere routing especial en `do_PUT` (ver sección Implementación). |
| `GET` | `/api/turnos` | Lista todos los turnos ordenados por `apertura_at DESC` con `LIMIT 120`. Solo para admin. |

## Flujos

### Apertura

1. Cajero ingresa nombre y monto inicial → pulsa "Abrir Caja".
2. Frontend hace `POST /api/turnos`.
   - Si retorna `409`: hay un turno abierto del día anterior. Mostrar modal de advertencia: "Hay un turno de [cajero] sin cerrar desde [fecha]. ¿Deseas cerrarlo automáticamente y abrir uno nuevo?" → si acepta, `PUT /api/turnos/:id_viejo/cierre` con `{monto_contado: null, diferencia: null, resumen_ia: null}` (cierre forzado) y luego reintenta `POST /api/turnos`.
3. `cajaSession = { id, cajero, inicio: apertura_at, montoInicial, ventasIds: [] }`.
4. La barra de caja se actualiza igual que hoy.

### Venta (cambio menor)

Al hacer `POST /api/ventas`, si hay un turno activo, el servidor incluye `turno_id` en el `INSERT`. El frontend envía `turno_id: cajaSession?.id` en el body de la venta.

### Recuperación al recargar

1. `loadData()` llama `GET /api/turnos/activo` en paralelo con productos y ventas.
2. Si hay turno abierto:
   - Restaura `cajaSession = { id, cajero, inicio: apertura_at, montoInicial }`.
   - Reconstruye `ventasIds` filtrando `sales` donde `s.turno_id === turnoActivo.id`.
3. Si no hay turno abierto: UI en estado inicial normal.

### Cierre

1. Cajero cuenta efectivo → pulsa "Cerrar Caja".
2. Frontend calcula totales desde `sales.filter(s => cajaSession.ventasIds.includes(s.id))`.
3. Llama `GET /api/ai-cierre` en background (igual que hoy). Si la llamada aún no termina cuando el cajero confirma, `resumen_ia` se omite del body (no se bloquea el cierre).
4. `PUT /api/turnos/:id/cierre` con todos los campos.
5. `cajaSession = null`, UI vuelve a estado inicial.

### Historial (Admin — Reportes)

- Nueva `<section id="turnos-section">` debajo del grid de KPIs existente en la pestaña Reportes, visible solo en modo admin.
- Se carga con `GET /api/turnos` al entrar a Reportes (o al cambiar a modo admin).
- La duración se calcula en el frontend: `(cierre_at ?? now) - apertura_at` → formato `Xh Ym`.
- Tabla con columnas: Fecha, Cajero, Caja, Duración, Transacciones, Total vendido, Diferencia, Estado.
- Diferencia: rojo si negativa (faltante), verde si cero o positiva, gris si `null` (cierre forzado).
- Estado: 🟢 Abierto / ✅ Cerrado.

## Implementación — Notas técnicas

### Routing `do_GET`

`GET /api/turnos/activo` debe coincidir como literal **antes** de cualquier chequeo por prefijo sobre `/api/turnos/`. De lo contrario Python intentará castear `"activo"` como `int` y lanzará `ValueError`. El orden correcto en `do_GET`:

```python
elif path == "/api/turnos/activo":   # primero el literal
    ...
elif path == "/api/turnos":          # luego la lista
    ...
```

### Routing `do_PUT`

El `do_PUT` actual solo maneja `/api/productos/:id`. Agregar rama para `/api/turnos/:id/cierre`:

```python
parts = path.split("/")  # ['', 'api', 'turnos', '7', 'cierre']
if len(parts) == 5 and parts[2] == "turnos" and parts[4] == "cierre":
    turno_id = int(parts[3])
    # ... actualizar turno
```

### Cierre forzado de turno huérfano

Al forzar el cierre de un turno anterior desde el modal de advertencia, enviar:
`{efectivo_ventas: 0, transferencias: 0, total_ventas: 0, num_tx: 0, monto_contado: null, diferencia: null, resumen_ia: null}`. No se intenta calcular totales de una sesión que no está activa en el cliente.

### `lastSale` en memoria

Al construir `lastSale` después de un `POST /api/ventas` exitoso, incluir `turno_id: cajaSession?.id` para que sea consistente con lo que retornaría `normalizarVenta` desde la API.

### `normalizarVenta` en frontend

La función debe preservar `turno_id` del JSON de la API, igual que ya preserva `producto_id`.

## Archivos Afectados

| Archivo | Cambio |
|---------|--------|
| `server.py` | `init_db()`: crear tabla `turnos`, migrar `turno_id` en `ventas`. Agregar 4 endpoints. Incluir `turno_id` en `INSERT INTO ventas`. Routing `do_PUT` para cierre. |
| `index.html` | Modificar `abrirCaja()`, `openCierreModal()`, `confirmarCierre()`, `loadData()`, `normalizarVenta()`, `procesarVenta()`. Agregar sección historial en Reportes. Agregar modal de advertencia turno huérfano. |

## No incluido en este spec

- Tabla `cajas` separada (cuando haya múltiples cajas reales).
- Autenticación / login por cajero (spec separado).
- Exportar historial de turnos a CSV.
- Paginación de `GET /api/turnos` (suficiente con `LIMIT 120` por ahora).
