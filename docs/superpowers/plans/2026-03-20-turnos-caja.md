# Turnos de Caja — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persistir los turnos de caja en base de datos desde la apertura, permitir recuperación automática al recargar, y mostrar historial de turnos en el panel admin.

**Architecture:** Nueva tabla `turnos` en SQLite/Turso. El servidor expone 4 endpoints. El frontend llama la API al abrir/cerrar caja y recupera el turno activo al cargar la página. El historial se renderiza en la sección Reportes (solo admin).

**Tech Stack:** Python `http.server` (server.py), Vanilla JS (index.html), SQLite/Turso via `_TursoConn`.

---

## Archivos modificados

| Archivo | Cambios |
|---------|---------|
| `server.py` | `init_db()`: tabla `turnos` + migración `turno_id` en `ventas`. Métodos `_open_turno`, `_get_turno_activo`, `_list_turnos`, `_close_turno`. Routing en `do_GET`, `do_POST`, `do_PUT`. `_create_venta()`: incluir `turno_id`. |
| `index.html` | `abrirCaja()` → llama API. `loadData()` → recupera turno activo. `normalizarVenta()` → preserva `turno_id`. Venta → envía `turno_id`. `cerrarCaja()` → llama API. Modal advertencia turno huérfano. Sección historial en Reportes. |

---

## Task 1: Tabla `turnos` y migración `turno_id` en `ventas`

**Files:**
- Modify: `server.py` (función `init_db`, líneas ~132-182)

- [ ] **Step 1: Agregar `CREATE TABLE IF NOT EXISTS turnos` al script de `init_db`**

En `server.py`, dentro de `init_db()`, agrega la tabla al `executescript`:

```python
def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS productos ( ... );  -- sin cambios
        CREATE TABLE IF NOT EXISTS ventas ( ... );     -- sin cambios
        CREATE TABLE IF NOT EXISTS items_venta ( ... );-- sin cambios
        CREATE TABLE IF NOT EXISTS turnos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            caja_id          TEXT     DEFAULT 'caja-1',
            cajero           TEXT     NOT NULL,
            estado           TEXT     DEFAULT 'abierto',
            monto_inicial    REAL     DEFAULT 0,
            apertura_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            cierre_at        DATETIME DEFAULT NULL,
            efectivo_ventas  REAL     DEFAULT 0,
            transferencias   REAL     DEFAULT 0,
            total_ventas     REAL     DEFAULT 0,
            monto_contado    REAL     DEFAULT NULL,
            diferencia       REAL     DEFAULT NULL,
            num_tx           INTEGER  DEFAULT 0,
            resumen_ia       TEXT     DEFAULT NULL
        );
    """)
```

- [ ] **Step 2: Agregar migración `turno_id` en `ventas` junto al bloque de `thumbnail`**

Después del bloque `try/except` existente para `thumbnail`, agregar:

```python
    try:
        conn.execute("ALTER TABLE ventas ADD COLUMN turno_id INTEGER REFERENCES turnos(id)")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
```

- [ ] **Step 3: Verificar arranque local**

```bash
python3 server.py
```
Esperado: servidor arranca sin errores. Las tablas se crean automáticamente.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: tabla turnos y migración turno_id en ventas"
```

---

## Task 2: API — `POST /api/turnos` (apertura)

**Files:**
- Modify: `server.py` (do_POST routing + nuevo método `_open_turno`)

- [ ] **Step 1: Agregar routing en `do_POST`**

Busca `do_POST` y agrega antes del `else`:

```python
elif self.path == "/api/turnos":
    self._open_turno()
```

- [ ] **Step 2: Implementar `_open_turno`**

Agregar método a la clase `Handler`:

```python
def _open_turno(self):
    try:
        body = self.read_json_body()
        cajero = body.get("cajero", "").strip()
        if not cajero:
            self.send_error_json("Falta el nombre del cajero"); return
        conn = get_db()
        # Verificar si ya existe un turno abierto
        activo = conn.execute(
            "SELECT * FROM turnos WHERE estado='abierto' ORDER BY apertura_at DESC LIMIT 1"
        ).fetchone()
        if activo:
            conn.close()
            self.send_json({"error": "Ya existe un turno abierto", "turno_activo": row_to_dict(activo)}, 409)
            return
        c = conn.cursor()
        c.execute(
            "INSERT INTO turnos (cajero, monto_inicial, caja_id) VALUES (?,?,?)",
            (cajero, body.get("monto_inicial", 0), body.get("caja_id", "caja-1"))
        )
        turno = conn.execute("SELECT * FROM turnos WHERE id=?", (c.lastrowid,)).fetchone()
        conn.commit(); conn.close()
        self.send_json(row_to_dict(turno), 201)
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 3: Probar con curl**

```bash
curl -s -X POST http://localhost:5051/api/turnos \
  -H "Content-Type: application/json" \
  -d '{"cajero":"María","monto_inicial":50000}'
```
Esperado: `{"id":1,"cajero":"María","estado":"abierto","monto_inicial":50000.0,...}`

- [ ] **Step 4: Probar 409 (segundo turno abierto)**

```bash
curl -s -X POST http://localhost:5051/api/turnos \
  -H "Content-Type: application/json" \
  -d '{"cajero":"Carlos","monto_inicial":20000}'
```
Esperado: HTTP 409, `{"error":"Ya existe un turno abierto","turno_activo":{...}}`

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat: POST /api/turnos — apertura de caja con validación 409"
```

---

## Task 3: API — `GET /api/turnos/activo` y `GET /api/turnos`

**Files:**
- Modify: `server.py` (do_GET routing + métodos `_get_turno_activo`, `_list_turnos`)

- [ ] **Step 1: Agregar routing en `do_GET` — ORDEN IMPORTANTE**

`/api/turnos/activo` debe ir **antes** de cualquier prefijo `/api/turnos`. Agregar en `do_GET` antes del `else` final:

```python
elif path == "/api/turnos/activo":
    self._get_turno_activo()
elif path == "/api/turnos":
    self._list_turnos()
```

- [ ] **Step 2: Implementar `_get_turno_activo`**

```python
def _get_turno_activo(self):
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM turnos WHERE estado='abierto' ORDER BY apertura_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.send_json(row_to_dict(row) if row else None)
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 3: Implementar `_list_turnos`**

```python
def _list_turnos(self):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM turnos ORDER BY apertura_at DESC LIMIT 120"
        ).fetchall()
        conn.close()
        self.send_json([row_to_dict(r) for r in rows])
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 4: Probar**

```bash
curl -s http://localhost:5051/api/turnos/activo
# Esperado: el turno abierto en Task 2 (o null si no hay)

curl -s http://localhost:5051/api/turnos
# Esperado: lista de turnos
```

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat: GET /api/turnos/activo y GET /api/turnos"
```

---

## Task 4: API — `PUT /api/turnos/:id/cierre`

**Files:**
- Modify: `server.py` (do_PUT routing + método `_close_turno`)

- [ ] **Step 1: Agregar routing en `do_PUT`**

El `do_PUT` actual hace `parts = self.path.split("/")`. Agregar rama para 5 partes:

```python
def do_PUT(self):
    parts = self.path.split("/")
    if len(parts) == 4 and parts[2] == "productos":
        self._update_producto(parts[3])
    elif len(parts) == 5 and parts[2] == "turnos" and parts[4] == "cierre":
        self._close_turno(int(parts[3]))
    else:
        self.send_error_json("Ruta no encontrada", 404)
```

- [ ] **Step 2: Implementar `_close_turno`**

```python
def _close_turno(self, turno_id):
    try:
        body = self.read_json_body()
        conn = get_db()
        conn.execute(
            """UPDATE turnos SET
                estado='cerrado',
                cierre_at=CURRENT_TIMESTAMP,
                efectivo_ventas=?,
                transferencias=?,
                total_ventas=?,
                num_tx=?,
                monto_contado=?,
                diferencia=?,
                resumen_ia=?
               WHERE id=? AND estado='abierto'""",
            (body.get("efectivo_ventas", 0),
             body.get("transferencias", 0),
             body.get("total_ventas", 0),
             body.get("num_tx", 0),
             body.get("monto_contado"),
             body.get("diferencia"),
             body.get("resumen_ia"),
             turno_id)
        )
        turno = conn.execute("SELECT * FROM turnos WHERE id=?", (turno_id,)).fetchone()
        conn.commit(); conn.close()
        self.send_json(row_to_dict(turno) if turno else {})
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 3: Probar**

Usar el `id` del turno abierto en Task 2 (probablemente `1`):

```bash
curl -s -X PUT http://localhost:5051/api/turnos/1/cierre \
  -H "Content-Type: application/json" \
  -d '{"efectivo_ventas":45000,"transferencias":12000,"total_ventas":57000,"num_tx":8,"monto_contado":95000,"diferencia":0}'
```
Esperado: turno con `estado: "cerrado"` y `cierre_at` poblado.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: PUT /api/turnos/:id/cierre — cierre de turno"
```

---

## Task 5: API — incluir `turno_id` en `POST /api/ventas`

**Files:**
- Modify: `server.py` (método `_create_venta`, línea ~418)

- [ ] **Step 1: Modificar `INSERT INTO ventas` para incluir `turno_id`**

Cambiar el `INSERT` de ventas para recibir y guardar `turno_id`:

```python
def _create_venta(self):
    try:
        body = self.read_json_body()
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO ventas (total, metodo, recibido, turno_id) VALUES (?,?,?,?)",
            (body["total"], body.get("metodo","efectivo"),
             body.get("recibido", body["total"]),
             body.get("turno_id"))          # None si no hay turno activo
        )
        # ... resto sin cambios
```

- [ ] **Step 2: Verificar que `GET /api/ventas` devuelva `turno_id`**

El `SELECT * FROM ventas` existente ya traerá la nueva columna automáticamente.

```bash
curl -s http://localhost:5051/api/ventas | python3 -m json.tool | grep turno_id
```
Esperado: campo `turno_id` presente (puede ser `null` en ventas antiguas).

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: ventas incluyen turno_id en DB"
```

---

## Task 6: Frontend — `abrirCaja()` llama API

**Files:**
- Modify: `index.html` (función `abrirCaja`, línea ~2638)

- [ ] **Step 1: Reemplazar `abrirCaja()` para llamar `POST /api/turnos`**

```javascript
async function abrirCaja() {
  const cajero = document.getElementById('apertura-cajero').value.trim();
  const monto  = parseMoney('apertura-monto');
  if (!cajero) { showToast('⚠ Ingresa el nombre del cajero'); return; }

  try {
    const res = await fetch('/api/turnos', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ cajero, monto_inicial: monto })
    });
    const data = await res.json();

    // Turno huérfano del día anterior
    if (res.status === 409) {
      const t = data.turno_activo;
      const fecha = new Date(t.apertura_at.replace(' ','T'))
        .toLocaleDateString('es-CO', {day:'2-digit',month:'2-digit'});
      const confirmar = confirm(
        `Hay un turno de "${t.cajero}" sin cerrar desde ${fecha}.\n¿Cerrarlo automáticamente y abrir uno nuevo?`
      );
      if (!confirmar) return;
      // Cierre forzado
      await fetch(`/api/turnos/${t.id}/cierre`, {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({efectivo_ventas:0,transferencias:0,total_ventas:0,num_tx:0,monto_contado:null,diferencia:null})
      });
      // Reintentar apertura
      return abrirCaja();
    }

    if (!res.ok) { showToast('⚠ Error al abrir caja: ' + data.error); return; }

    cajaSession = {
      id: data.id,
      cajero: data.cajero,
      inicio: new Date(data.apertura_at.replace(' ','T')),
      montoInicial: data.monto_inicial,
      ventasIds: []
    };
    updateCajaBar();
    closeModal('modal-apertura');
    showToast(`✅ Caja abierta — ${cajero}`);
  } catch (err) {
    showToast('⚠ Error al abrir caja');
  }
}
```

- [ ] **Step 2: Probar en navegador**

1. Abrir la app en `http://localhost:5051`
2. Modo Cajero → "Abrir Caja" → ingresar nombre y monto → confirmar
3. Verificar barra de caja activa
4. `curl http://localhost:5051/api/turnos/activo` → debe mostrar el turno

- [ ] **Step 3: Commit**

```bash
git add index.html
git commit -m "feat: abrirCaja llama POST /api/turnos con manejo de turno huérfano"
```

---

## Task 7: Frontend — recuperación de turno al recargar

**Files:**
- Modify: `index.html` (función `loadData`, línea ~1506; `normalizarVenta`, línea ~1494)

- [ ] **Step 1: Actualizar `normalizarVenta` para preservar `turno_id`**

```javascript
function normalizarVenta(v) {
  return {
    id:       v.id,
    turno_id: v.turno_id ?? null,      // ← agregar esta línea
    total:    v.total,
    method:   v.metodo,
    recibido: v.recibido ?? v.total,
    cambio:   (v.recibido ?? v.total) - v.total,
    date:     new Date((v.created_at || '').replace(' ', 'T')),
    items:    (v.items || []).map(i => ({ id: i.producto_id, name: i.name, emoji: i.emoji, qty: i.qty, price: i.price }))
  };
}
```

- [ ] **Step 2: Actualizar `loadData` para recuperar turno activo**

```javascript
async function loadData() {
  try {
    const [pRes, vRes, tRes] = await Promise.all([
      fetch('/api/productos'),
      fetch('/api/ventas'),
      fetch('/api/turnos/activo')
    ]);
    products = await pRes.json();
    sales    = (await vRes.json()).map(normalizarVenta);
    const turnoActivo = await tRes.json();

    // Recuperar sesión si hay turno abierto
    if (turnoActivo && turnoActivo.id) {
      cajaSession = {
        id:           turnoActivo.id,
        cajero:       turnoActivo.cajero,
        inicio:       new Date(turnoActivo.apertura_at.replace(' ','T')),
        montoInicial: turnoActivo.monto_inicial,
        ventasIds:    sales.filter(s => s.turno_id === turnoActivo.id).map(s => s.id)
      };
    }
  } catch (err) {
    showToast('⚠ Error cargando datos del servidor');
  }
}
```

- [ ] **Step 3: Probar recuperación**

1. Abrir caja en el navegador
2. Hacer una venta
3. Recargar la página (`F5`)
4. La barra de caja debe mostrar el turno activo automáticamente

- [ ] **Step 4: Commit**

```bash
git add index.html
git commit -m "feat: recuperación automática de turno activo al recargar"
```

---

## Task 8: Frontend — ventas envían `turno_id`; `lastSale` incluye `turno_id`

**Files:**
- Modify: `index.html` (función de procesamiento de venta, línea ~2005-2036)

- [ ] **Step 1: Agregar `turno_id` al body del `POST /api/ventas`**

Busca el fetch de `/api/ventas` en la función de cobro (alrededor de línea 2005). Agrega `turno_id: cajaSession?.id ?? null` al body:

```javascript
body: JSON.stringify({
  total,
  metodo: currentPayMethod,
  recibido,
  turno_id: cajaSession?.id ?? null,      // ← agregar
  items: cart.map(i => ({ id: i.id, name: i.name, emoji: i.emoji, qty: i.qty, price: i.price }))
})
```

- [ ] **Step 2: Agregar `turno_id` a `lastSale`**

```javascript
lastSale = {
  id:       data.venta.id,
  turno_id: cajaSession?.id ?? null,      // ← agregar
  items:    cart.map(i => ({ name: i.name, emoji: i.emoji, qty: i.qty, price: i.price })),
  total,
  method:   currentPayMethod,
  recibido,
  cambio:   recibido - total,
  date:     new Date()
};
```

- [ ] **Step 3: Verificar que ventas nuevas se asocian al turno**

1. Con un turno abierto, hacer una venta
2. `curl http://localhost:5051/api/ventas` → la venta reciente debe tener `turno_id` poblado

- [ ] **Step 4: Commit**

```bash
git add index.html
git commit -m "feat: ventas se asocian al turno activo via turno_id"
```

---

## Task 9: Frontend — `cerrarCaja()` llama API

**Files:**
- Modify: `index.html` (función `cerrarCaja`, línea ~2737)

- [ ] **Step 1: Reemplazar `cerrarCaja()` para llamar `PUT /api/turnos/:id/cierre`**

```javascript
async function cerrarCaja() {
  const contado = parseMoney('cierre-contado');
  if (isNaN(contado) || contado === 0 && document.getElementById('cierre-contado').value === '') {
    showToast('⚠ Ingresa el efectivo contado'); return;
  }

  const sesVentas    = sales.filter(s => cajaSession.ventasIds.includes(s.id));
  const efectivo     = sesVentas.filter(s=>s.method==='efectivo').reduce((a,s)=>a+s.total,0);
  const transferencias = sesVentas.filter(s=>s.method==='transferencia').reduce((a,s)=>a+s.total,0);
  const total_ventas = efectivo + transferencias;
  const esperado     = cajaSession.montoInicial + efectivo;
  const diferencia   = contado - esperado;

  // Capturar resumen IA si ya está disponible
  const iaTexto = document.getElementById('cierre-ia-texto');
  const resumen_ia = (iaTexto && iaTexto.textContent && !iaTexto.textContent.startsWith('⏳'))
    ? iaTexto.textContent : null;

  try {
    await fetch(`/api/turnos/${cajaSession.id}/cierre`, {
      method: 'PUT',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        efectivo_ventas: efectivo,
        transferencias,
        total_ventas,
        num_tx: sesVentas.length,
        monto_contado: contado,
        diferencia,
        resumen_ia
      })
    });
  } catch (e) {
    // No bloquear cierre aunque falle la persistencia
    console.warn('No se pudo guardar el cierre en DB:', e);
  }

  cajaSession = null;
  updateCajaBar();
  closeModal('modal-cierre');
  showToast('🔒 Caja cerrada. Buen turno.');
}
```

- [ ] **Step 2: Probar cierre completo**

1. Abrir caja, hacer 2-3 ventas, cerrar caja con monto contado
2. `curl http://localhost:5051/api/turnos` → el turno debe tener `estado: "cerrado"` con todos los campos poblados
3. Recargar página → NO debe mostrar turno activo (cajaSession null)

- [ ] **Step 3: Commit**

```bash
git add index.html
git commit -m "feat: cerrarCaja llama PUT /api/turnos/:id/cierre"
```

---

## Task 10: Frontend — historial de turnos en Admin/Reportes

**Files:**
- Modify: `index.html` (sección Reportes en HTML + función `renderReportes` o nueva `renderTurnos`)

- [ ] **Step 1: Agregar HTML del historial debajo del grid de KPIs en `screen-reportes`**

Busca `<div id="screen-reportes"` y dentro de él, después del grid de KPIs existente, agregar:

```html
<!-- Historial de turnos — solo admin -->
<section id="turnos-section" style="display:none; padding:0 24px 32px;">
  <h3 style="font-family:var(--ff-disp);font-size:14px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--text-2);margin-bottom:12px;">Historial de Turnos</h3>
  <div style="overflow-x:auto;">
    <table id="turnos-table" style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="border-bottom:2px solid var(--border);text-align:left;">
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;">Fecha</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;">Cajero</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;">Caja</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;">Duración</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;text-align:right;">Tx</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;text-align:right;">Total</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;text-align:right;">Diferencia</th>
          <th style="padding:8px 12px;color:var(--text-2);font-weight:600;">Estado</th>
        </tr>
      </thead>
      <tbody id="turnos-tbody"></tbody>
    </table>
  </div>
</section>
```

- [ ] **Step 2: Agregar función `renderTurnos()`**

```javascript
async function renderTurnos() {
  const section = document.getElementById('turnos-section');
  if (currentMode !== 'admin') { section.style.display = 'none'; return; }

  try {
    const res = await fetch('/api/turnos');
    const turnos = await res.json();
    section.style.display = '';

    const tbody = document.getElementById('turnos-tbody');
    if (!turnos.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="padding:20px;text-align:center;color:var(--text-2)">Sin turnos registrados</td></tr>';
      return;
    }

    tbody.innerHTML = turnos.map(t => {
      const apertura = new Date(t.apertura_at.replace(' ','T'));
      const cierre   = t.cierre_at ? new Date(t.cierre_at.replace(' ','T')) : new Date();
      const durMs    = cierre - apertura;
      const durH     = Math.floor(durMs / 3600000);
      const durM     = Math.floor((durMs % 3600000) / 60000);
      const durStr   = `${durH}h ${durM}m`;
      const fecha    = apertura.toLocaleDateString('es-CO', {day:'2-digit',month:'2-digit'});

      let difStr = '—', difColor = 'var(--text-2)';
      if (t.diferencia !== null) {
        difStr  = (t.diferencia >= 0 ? '+' : '') + fmt(t.diferencia);
        difColor = t.diferencia < 0 ? 'var(--danger)' : 'var(--success)';
      }
      const estado = t.estado === 'abierto'
        ? '<span style="color:var(--success)">🟢 Abierto</span>'
        : '<span style="color:var(--text-2)">✅ Cerrado</span>';

      return `<tr style="border-bottom:1px solid var(--border);">
        <td style="padding:10px 12px;">${fecha}</td>
        <td style="padding:10px 12px;">${esc(t.cajero)}</td>
        <td style="padding:10px 12px;color:var(--text-2);font-size:12px;">${esc(t.caja_id)}</td>
        <td style="padding:10px 12px;color:var(--text-2);">${durStr}</td>
        <td style="padding:10px 12px;text-align:right;">${t.num_tx}</td>
        <td style="padding:10px 12px;text-align:right;font-family:var(--ff-mono);">${fmt(t.total_ventas)}</td>
        <td style="padding:10px 12px;text-align:right;font-family:var(--ff-mono);color:${difColor};">${difStr}</td>
        <td style="padding:10px 12px;">${estado}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error('Error cargando turnos:', e);
  }
}
```

- [ ] **Step 3: Llamar `renderTurnos()` desde `renderReportes()`**

Agrega `renderTurnos()` al final de `renderReportes()`. Así se ejecuta cada vez que se muestra la sección de reportes, sin necesidad de encontrar el listener de la pestaña:

```javascript
function renderReportes() {
  // ... código existente sin cambios ...
  renderTurnos();  // ← agregar al final
}
```

- [ ] **Step 4: Probar historial**

1. Cambiar a modo Admin → ir a Reportes
2. Tabla de turnos debe mostrar los turnos creados en Tasks anteriores
3. Cambiar a modo Cajero → la sección no debe ser visible

- [ ] **Step 5: Commit y push**

```bash
git add index.html
git commit -m "feat: historial de turnos en panel admin — Reportes"
git push origin main
```

---

## Verificación final end-to-end

- [ ] Abrir caja → hacer ventas → recargar → turno sigue activo con ventas previas
- [ ] Cerrar caja → recargar → sin turno activo
- [ ] Abrir nueva caja sin cerrar la anterior → aparece warning → cierre forzado → nueva apertura
- [ ] En modo Admin/Reportes → tabla muestra historial con diferencias coloreadas
- [ ] Deploy a Railway → datos persisten en Turso entre deploys
