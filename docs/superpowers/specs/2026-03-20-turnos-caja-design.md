# Diseño: Gestión de Turnos de Caja (Persistente)

**Fecha:** 2026-03-20
**Proyecto:** SFPos — POS para mini mercados colombianos
**Estado:** Aprobado

## Contexto

Actualmente el turno de caja vive en memoria (`cajaSession` JS). Si el servidor se reinicia o la página se recarga, el turno se pierde. El dueño del negocio necesita ver el historial de turnos en el panel admin para auditar diferencias y ventas por cajero. El negocio opera con un cajero por turno pero en el futuro puede tener múltiples cajas.

## Decisiones

- **Opción elegida:** Sesión persistida desde la apertura (no solo al cierre).
- **Multi-caja:** campo `caja_id` TEXT simple en la tabla; sin tabla `cajas` por ahora — evita migración futura sin sobre-ingenierar hoy.
- **Historial:** solo visible en modo Admin, dentro de la sección Reportes.

## Base de Datos

Nueva tabla `turnos`:

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
  diferencia       REAL     DEFAULT NULL,        -- monto_contado - esperado_en_caja
  num_tx           INTEGER  DEFAULT 0,
  resumen_ia       TEXT     DEFAULT NULL
);
```

Migración: `CREATE TABLE IF NOT EXISTS turnos (...)` se agrega a `init_db()` en `server.py`. Compatible con Turso y SQLite local.

## API

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/api/turnos` | Abre turno. Body: `{cajero, monto_inicial, caja_id?}`. Retorna el turno creado con `id`. |
| `GET` | `/api/turnos/activo` | Retorna el turno con `estado='abierto'` más reciente, o `null`. Usado al cargar la app para recuperar sesión. |
| `PUT` | `/api/turnos/:id/cierre` | Cierra el turno. Body: `{efectivo_ventas, transferencias, total_ventas, num_tx, monto_contado, diferencia, resumen_ia?}`. |
| `GET` | `/api/turnos` | Lista todos los turnos ordenados por `apertura_at DESC`. Solo para admin. |

## Flujos

### Apertura
1. Cajero ingresa nombre y monto inicial → pulsa "Abrir Caja".
2. Frontend hace `POST /api/turnos` → recibe `{ id, cajero, apertura_at, ... }`.
3. `cajaSession` se inicializa con el `id` del turno persisted.
4. La barra de caja se actualiza igual que hoy.

### Recuperación al recargar
1. `loadData()` llama `GET /api/turnos/activo` en paralelo con productos y ventas.
2. Si hay turno abierto: se restaura `cajaSession` (cajero, inicio, montoInicial).
3. Las ventas del turno se re-asocian filtrando por `apertura_at` (ventas posteriores a la apertura).
4. Si no hay turno abierto: UI en estado inicial.

### Cierre
1. Cajero cuenta efectivo físico → pulsa "Cerrar Caja".
2. Frontend calcula totales desde `sales` filtradas por el turno.
3. `PUT /api/turnos/:id/cierre` con todos los campos.
4. `cajaSession = null`, UI vuelve a estado inicial.

### Historial (Admin)
- Nueva sección "Turnos" dentro de la pestaña Reportes, visible solo en modo admin.
- Tabla con columnas: Fecha, Cajero, Caja, Duración, Transacciones, Total vendido, Diferencia, Estado.
- Diferencia: color rojo si negativa (faltante), verde si cero, gris si positiva (sobrante).
- Estado: 🟢 Abierto / ✅ Cerrado.
- Se carga con `GET /api/turnos` al entrar a Reportes.

## Archivos Afectados

| Archivo | Cambio |
|---------|--------|
| `server.py` | Agregar tabla `turnos` a `init_db()`, agregar 4 endpoints nuevos |
| `index.html` | Modificar `abrirCaja()`, `openCierreModal()`, `confirmarCierre()`, `loadData()`, agregar sección historial en reportes |

## No incluido en este spec

- Tabla `cajas` separada (se agrega cuando haya múltiples cajas reales).
- Autenticación / login por cajero (spec separado).
- Exportar historial de turnos a CSV.
