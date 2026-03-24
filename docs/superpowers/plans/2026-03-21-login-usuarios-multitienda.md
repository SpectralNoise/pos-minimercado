# Login, Usuarios y Multi-Tienda SaaS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agregar autenticación HMAC, gestión de usuarios con roles (superadmin/admin/cajero), multi-tenancy por tienda, y un botón de actualizar inventario.

**Architecture:** Tablas `tiendas` y `usuarios` en Turso/SQLite. Tokens HMAC-SHA256 stateless con 12h de expiración. `require_auth()` como middleware en cada endpoint. Frontend con pantalla de login antes de la app, `apiFetch()` helper que inyecta el token, y flujo por rol al entrar.

**Tech Stack:** Python `http.server` (server.py), Vanilla JS (index.html), SQLite/Turso via `_TursoConn`, `hmac`/`hashlib`/`base64`/`secrets`/`time` (stdlib Python).

---

## Archivos modificados

| Archivo | Cambios |
|---------|---------|
| `server.py` | Imports auth, `SECRET_KEY` validation, `AuthContext`, `require_auth()`, CORS fix, `init_db()` (tablas + migraciones + seed), auth endpoints, proteger endpoints existentes, API tiendas, API usuarios |
| `index.html` | `currentUser` global, `apiFetch()`, pantalla login, boot sequence, flujo por rol, barra superior, logout, panel superadmin, pestaña Usuarios, botón actualizar inventario, `abrirCaja()` usa nombre del usuario |

---

## Task 1: Botón "Actualizar inventario"

**Files:**
- Modify: `index.html` (toolbar inventario ~línea 1147, JS ~línea 2237)

- [ ] **Step 1: Agregar botón en la toolbar de inventario**

Busca la línea `<button class="btn-primary" onclick="openProdModal()">+ Nuevo</button>` (~línea 1149) y agrega el botón antes:

```html
<button class="btn-secondary" id="btn-refresh-inv" onclick="recargarInventario()" title="Actualizar inventario">🔄</button>
<button class="btn-primary" onclick="openProdModal()">+ Nuevo</button>
```

- [ ] **Step 2: Agregar función `recargarInventario()`**

Agrega junto a `renderInventario()` (~línea 2237):

```javascript
async function recargarInventario() {
  const btn = document.getElementById('btn-refresh-inv');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/productos');
    if (!res.ok) { showToast('⚠ Error al actualizar'); return; }
    products = await res.json();
    renderInventario();
    showToast('✅ Inventario actualizado');
  } catch (e) {
    showToast('⚠ Error de red');
  } finally {
    if (btn) btn.disabled = false;
  }
}
```

- [ ] **Step 3: Probar**

1. `python3 server.py`
2. Abrir en navegador → Inventario
3. Hacer clic en 🔄 → debe aparecer "✅ Inventario actualizado"

- [ ] **Step 4: Commit**

```bash
git add index.html
git commit -m "feat: botón actualizar inventario"
```

---

## Task 2: Auth infrastructure en server.py

**Files:**
- Modify: `server.py` (imports ~línea 7, env vars ~línea 29, clase Handler ~línea 212)

- [ ] **Step 1: Agregar imports de auth y validar SECRET_KEY**

Al inicio de `server.py`, reemplaza la línea de imports existente y agrega las validaciones:

```python
import os, json, mimetypes, ssl, sqlite3, base64, io, urllib.request, urllib.error, socketserver
import hmac as _hmac, hashlib, time, secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import namedtuple
```

Después del bloque que carga las env vars (después de `TURSO_TOKEN = ...`), agrega:

```python
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError(
        "[SFPOS] SECRET_KEY no configurada.\n"
        "  En Railway: Variables → agregar SECRET_KEY con un valor aleatorio largo.\n"
        "  En local: agregar SECRET_KEY=<valor> en el archivo .env"
    )

AuthContext = namedtuple("AuthContext", ["user_id", "tienda_id", "rol"])
```

- [ ] **Step 2: Agregar `require_auth()` como método de `Handler`**

Agrega el método después de `send_error_json` (~línea 226):

```python
def require_auth(self):
    """Valida el token Bearer. Retorna AuthContext o envía 401 y retorna None."""
    header = self.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        self.send_error_json("No autenticado", 401); return None
    token = header[7:]
    try:
        payload_b64, signature = token.rsplit(".", 1)
        payload = base64.b64decode(payload_b64).decode()
        expected = _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, signature):
            self.send_error_json("Token inválido", 401); return None
        user_id_s, tienda_str, rol, exp_s = payload.split(":")
        if int(exp_s) < int(time.time()):
            self.send_error_json("Sesión expirada", 401); return None
        tienda_id = None if tienda_str == "null" else int(tienda_str)
        conn = get_db()
        try:
            u = conn.execute("SELECT activo FROM usuarios WHERE id=?", (int(user_id_s),)).fetchone()
            if not u or not u["activo"]:
                self.send_error_json("Usuario inactivo", 401); return None
            if tienda_id is not None:
                t = conn.execute("SELECT activo FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
                if not t or not t["activo"]:
                    self.send_error_json("Tienda inactiva", 401); return None
        finally:
            conn.close()
        return AuthContext(int(user_id_s), tienda_id, rol)
    except Exception:
        self.send_error_json("Token inválido", 401); return None
```

- [ ] **Step 3: Agregar helper `_make_token()` como método de `Handler`**

Agrega después de `require_auth()`:

```python
def _make_token(self, user_id, tienda_id, rol):
    """Genera token HMAC-SHA256 con 12h de expiración."""
    exp = int(time.time()) + 43200
    tienda_str = "null" if tienda_id is None else str(tienda_id)
    payload = f"{user_id}:{tienda_str}:{rol}:{exp}"
    payload_b64 = base64.b64encode(payload.encode()).decode()
    sig = _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}", exp
```

- [ ] **Step 4: Corregir CORS para incluir Authorization**

Busca `do_OPTIONS` (~línea 239) y cambia la línea de `Access-Control-Allow-Headers`:

```python
self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
```

- [ ] **Step 5: Agregar `SECRET_KEY` al archivo `.env` local**

Para desarrollo local sin tener que pasar la variable en cada comando, agrega al archivo `.env` (créalo si no existe):

```
SECRET_KEY=dev-secret-key-cambia-en-produccion
```

En Railway, agregar `SECRET_KEY` como variable de entorno con un valor aleatorio largo (ej: `openssl rand -hex 32`).

- [ ] **Step 6: Verificar que el servidor arranca**

```bash
python3 server.py
```

Esperado: servidor arranca normalmente. Sin `SECRET_KEY` en `.env` o en el entorno, debe lanzar `RuntimeError` con el mensaje instructivo.

- [ ] **Step 7: Commit**

```bash
git add server.py
git commit -m "feat: auth infrastructure — SECRET_KEY, AuthContext, require_auth(), CORS fix"
```

---

## Task 3: DB — tablas `tiendas` + `usuarios`, migraciones, seed superadmin

**Files:**
- Modify: `server.py` (función `init_db` ~línea 138)

- [ ] **Step 1: Agregar tablas `tiendas` y `usuarios` al `executescript` en `init_db()`**

Dentro del bloque `conn.executescript("""...""")`, agrega las nuevas tablas **al final** del script (antes del cierre `"""`):

```sql
CREATE TABLE IF NOT EXISTS tiendas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT    NOT NULL,
    slug       TEXT    NOT NULL UNIQUE,
    plan       TEXT    DEFAULT 'basico',
    activo     INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS usuarios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tienda_id     INTEGER REFERENCES tiendas(id) ON DELETE RESTRICT,
    nombre        TEXT    NOT NULL,
    username      TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    rol           TEXT    NOT NULL DEFAULT 'cajero'
                          CHECK (rol IN ('superadmin','admin','cajero')),
    activo        INTEGER DEFAULT 1,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tienda_id, username)
);
```

- [ ] **Step 2: Agregar migraciones `tienda_id` para `productos`, `ventas`, `turnos`**

Después del bloque `try/except` de migración `turno_id`, agrega:

```python
    # Migración: añadir tienda_id a productos, ventas, turnos
    for table in ('productos', 'ventas', 'turnos'):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tienda_id INTEGER REFERENCES tiendas(id)")
            conn.commit()
        except (sqlite3.OperationalError, ValueError) as e:
            if "duplicate column name" not in str(e):
                raise
```

- [ ] **Step 3: Agregar seed del superadmin**

Después de las migraciones, reemplaza el bloque de seed de productos existente:

```python
    # Seed superadmin (solo si no hay usuarios)
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        _pw = secrets.token_urlsafe(12)
        _salt = secrets.token_hex(16)
        _hash = hashlib.pbkdf2_hmac('sha256', _pw.encode(), _salt.encode(), 260000).hex()
        conn.execute(
            "INSERT INTO usuarios (tienda_id, nombre, username, password_hash, salt, rol) VALUES (?,?,?,?,?,?)",
            (None, "Super Admin", "admin", _hash, _salt, "superadmin")
        )
        conn.commit()
        print(f"\n  [SFPOS] ✅ Superadmin creado")
        print(f"  [SFPOS]    Usuario:    admin")
        print(f"  [SFPOS]    Contraseña: {_pw}")
        print(f"  [SFPOS]    ⚠  Guarda esta contraseña — no se volverá a mostrar.\n")

    # Seed productos iniciales (solo si no hay productos)
    if conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO productos (emoji,name,barcode,cat,price,stock,alert) VALUES (?,?,?,?,?,?,?)",
            PRODUCTOS_INICIALES
        )
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Helper para hashear contraseñas (función global, no método)**

Agrega justo antes de la clase `Handler`:

```python
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000).hex()
```

- [ ] **Step 5: Verificar arranque y seed**

```bash
# Borrar DB local para forzar re-seed (solo en local con SQLite)
rm -f pos.db
SECRET_KEY=test123 python3 server.py
```

Esperado: imprime contraseña del superadmin en los logs.

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "feat: tablas tiendas+usuarios, migraciones tienda_id, seed superadmin"
```

---

## Task 4: API — `POST /api/auth/login` + `GET /api/auth/me`

**Files:**
- Modify: `server.py` (do_GET ~línea 247, do_POST ~línea 347, nuevos métodos)

- [ ] **Step 1: Agregar routing en `do_GET`**

En `do_GET`, antes del bloque `elif path == "/api/productos"`:

```python
elif path == "/api/auth/me":
    self._auth_me()
```

- [ ] **Step 2: Agregar routing en `do_POST`**

En `do_POST`, el primer `if` actual es `if self.path == "/api/productos":`. Cambia esa línea a `elif` y agrega el bloque de auth **antes** como el nuevo primer `if`:

```python
def do_POST(self):
    if self.path == "/api/auth/login":
        self._auth_login(); return
    elif self.path == "/api/productos":
        self._create_producto()
    elif self.path == "/api/ventas":
        # ... (sin cambios)
    # ... resto sin cambios
```

**Nota:** El patrón de esta ruta (`if` luego `elif`) es importante: el `if` de `/api/auth/login` debe ser el primero y usar `return` tras la llamada para no caer al `else` final.

- [ ] **Step 3: Implementar `_auth_login()`**

```python
def _auth_login(self):
    try:
        body = self.read_json_body()
        username = body.get("username", "").strip().lower()
        password = body.get("password", "")
        if not username or not password:
            self.send_error_json("Faltan credenciales", 400); return
        conn = get_db()
        try:
            # Superadmin: tienda_id IS NULL
            user = conn.execute(
                "SELECT * FROM usuarios WHERE username=? AND tienda_id IS NULL", (username,)
            ).fetchone()
            if not user:
                # Usuario de tienda — la constraint es UNIQUE(tienda_id, username),
                # por lo que dos tiendas pueden tener el mismo username. En esta fase,
                # el login no tiene selector de tienda, así que se toma el primer match.
                # En producción, los admins deben asegurar usernames globalmente únicos
                # para evitar ambigüedad. Se selecciona el primer resultado por created_at.
                user = conn.execute(
                    "SELECT * FROM usuarios WHERE username=? AND tienda_id IS NOT NULL ORDER BY created_at ASC LIMIT 1",
                    (username,)
                ).fetchone()
            if not user:
                self.send_error_json("Credenciales incorrectas", 401); return
            if not user["activo"]:
                self.send_error_json("Usuario inactivo", 401); return
            expected_hash = _hash_password(password, user["salt"])
            if not _hmac.compare_digest(expected_hash, user["password_hash"]):
                self.send_error_json("Credenciales incorrectas", 401); return
            # Verificar tienda activa (si aplica)
            tienda = None
            if user["tienda_id"] is not None:
                tienda_row = conn.execute("SELECT * FROM tiendas WHERE id=?", (user["tienda_id"],)).fetchone()
                if not tienda_row or not tienda_row["activo"]:
                    self.send_error_json("Tienda inactiva", 401); return
                tienda = {"id": tienda_row["id"], "nombre": tienda_row["nombre"], "slug": tienda_row["slug"]}
            token, exp = self._make_token(user["id"], user["tienda_id"], user["rol"])
            self.send_json({
                "token": token,
                "exp": exp,
                "user": {
                    "id": user["id"],
                    "nombre": user["nombre"],
                    "username": user["username"],
                    "rol": user["rol"],
                    "tienda_id": user["tienda_id"]
                },
                "tienda": tienda
            })
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 4: Implementar `_auth_me()`**

```python
def _auth_me(self):
    ctx = self.require_auth()
    if not ctx: return
    try:
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM usuarios WHERE id=?", (ctx.user_id,)).fetchone()
            tienda = None
            if ctx.tienda_id is not None:
                t = conn.execute("SELECT * FROM tiendas WHERE id=?", (ctx.tienda_id,)).fetchone()
                if t:
                    tienda = {"id": t["id"], "nombre": t["nombre"], "slug": t["slug"]}
            self.send_json({
                "user": {
                    "id": user["id"],
                    "nombre": user["nombre"],
                    "username": user["username"],
                    "rol": user["rol"],
                    "tienda_id": user["tienda_id"]
                },
                "tienda": tienda
            })
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 5: Probar con curl**

```bash
# Login superadmin (usar la contraseña impresa en los logs al arrancar)
curl -s -X POST http://localhost:5051/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<PASSWORD_DE_LOS_LOGS>"}'
# Esperado: {"token":"...","exp":...,"user":{"rol":"superadmin","tienda_id":null},"tienda":null}

# Guardar token y probar /api/auth/me
TOKEN="<token del login>"
curl -s http://localhost:5051/api/auth/me -H "Authorization: Bearer $TOKEN"
# Esperado: {"user":{...},"tienda":null}
```

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "feat: POST /api/auth/login y GET /api/auth/me"
```

---

## Task 5: Proteger endpoints existentes con `require_auth()` y filtro `tienda_id`

**Files:**
- Modify: `server.py` (handlers _list_productos, _create_producto, _update_producto, _delete_producto, _list_ventas en do_GET, _create_venta, _get_turno_activo, _open_turno, _list_turnos, _close_turno)

- [ ] **Step 1: Proteger y filtrar `GET /api/productos`**

Reemplaza el bloque `elif path == "/api/productos":` en `do_GET`:

```python
elif path == "/api/productos":
    ctx = self.require_auth()
    if not ctx: return
    conn = get_db()
    try:
        if ctx.rol == 'superadmin':
            rows = conn.execute(
                "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
                "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                "FROM productos ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
                "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                "FROM productos WHERE tienda_id=? ORDER BY name",
                (ctx.tienda_id,)
            ).fetchall()
        self.send_json([row_to_dict(r) for r in rows])
    finally:
        conn.close()
```

- [ ] **Step 2: Proteger y filtrar `_create_producto()`**

Agrega al inicio de `_create_producto`:

```python
def _create_producto(self):
    ctx = self.require_auth()
    if not ctx: return
    try:
        body = self.read_json_body()
        tienda_id = None if ctx.rol == 'superadmin' else ctx.tienda_id
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO productos (emoji,name,barcode,cat,price,stock,alert,thumbnail,tienda_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (body.get("emoji","📦"), body["name"], body.get("barcode",""),
             body.get("cat","General"), body.get("price",0),
             body.get("stock",0), body.get("alert",5), body.get("thumbnail"), tienda_id)
        )
        row = conn.execute(
            "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
            "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
            "FROM productos WHERE id=?", (c.lastrowid,)
        ).fetchone()
        conn.commit(); conn.close()
        self.send_json(row_to_dict(row), 201)
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 3: Proteger `_update_producto()` y `_delete_producto()` con filtro tienda_id**

Reemplaza `_update_producto` completo — agrega `ctx`, y el UPDATE filtra por `tienda_id` para prevenir que un usuario modifique productos de otra tienda:

```python
def _update_producto(self, prod_id):
    ctx = self.require_auth()
    if not ctx: return
    try:
        body = self.read_json_body()
        conn = get_db()
        tienda_filter = "" if ctx.rol == 'superadmin' else " AND tienda_id=?"
        tienda_params = () if ctx.rol == 'superadmin' else (ctx.tienda_id,)
        if "thumbnail" in body:
            conn.execute(
                f"UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,stock=?,alert=?,thumbnail=? WHERE id=?{tienda_filter}",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("stock",0), body.get("alert",5), body.get("thumbnail"), prod_id) + tienda_params
            )
        else:
            conn.execute(
                f"UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,stock=?,alert=? WHERE id=?{tienda_filter}",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("stock",0), body.get("alert",5), prod_id) + tienda_params
            )
        row = conn.execute(
            "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
            "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
            "FROM productos WHERE id=?", (prod_id,)
        ).fetchone()
        conn.commit(); conn.close()
        self.send_json(row_to_dict(row) if row else {}, 200 if row else 404)
    except Exception as e:
        self.send_error_json(str(e), 500)
```

Reemplaza `_delete_producto` completo:

```python
def _delete_producto(self, prod_id):
    ctx = self.require_auth()
    if not ctx: return
    try:
        conn = get_db()
        if ctx.rol == 'superadmin':
            conn.execute("DELETE FROM productos WHERE id=?", (prod_id,))
        else:
            conn.execute("DELETE FROM productos WHERE id=? AND tienda_id=?", (prod_id, ctx.tienda_id))
        conn.commit(); conn.close()
        self.send_json({"ok": True})
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 4: Proteger `GET /api/ventas` (en `do_GET`)**

Reemplaza el bloque `elif path == "/api/ventas":`:

```python
elif path == "/api/ventas":
    ctx = self.require_auth()
    if not ctx: return
    conn = get_db()
    try:
        if ctx.rol == 'superadmin':
            ventas = conn.execute("SELECT * FROM ventas ORDER BY created_at DESC LIMIT 500").fetchall()
        else:
            ventas = conn.execute(
                "SELECT * FROM ventas WHERE tienda_id=? ORDER BY created_at DESC LIMIT 500",
                (ctx.tienda_id,)
            ).fetchall()
        result = []
        for v in ventas:
            vd = row_to_dict(v)
            items = conn.execute("SELECT * FROM items_venta WHERE venta_id=?", (v["id"],)).fetchall()
            vd["items"] = [row_to_dict(i) for i in items]
            result.append(vd)
        self.send_json(result)
    finally:
        conn.close()
```

- [ ] **Step 5: Proteger `_create_venta()`**

Agrega `ctx = self.require_auth(); if not ctx: return` al inicio, y agrega `tienda_id` al INSERT:

```python
def _create_venta(self):
    ctx = self.require_auth()
    if not ctx: return
    try:
        body = self.read_json_body()
        tienda_id = None if ctx.rol == 'superadmin' else ctx.tienda_id
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO ventas (total, metodo, recibido, turno_id, tienda_id) VALUES (?,?,?,?,?)",
            (body["total"], body.get("metodo","efectivo"),
             body.get("recibido", body["total"]),
             body.get("turno_id"), tienda_id)
        )
        venta_id = c.lastrowid
        for item in body.get("items", []):
            c.execute(
                "INSERT INTO items_venta (venta_id,producto_id,name,emoji,qty,price) VALUES (?,?,?,?,?,?)",
                (venta_id, item.get("id"), item.get("name",""), item.get("emoji",""),
                 item.get("qty",1), item.get("price",0))
            )
            c.execute(
                "UPDATE productos SET stock = MAX(0, stock - ?) WHERE id=?",
                (item.get("qty",1), item.get("id"))
            )
        row = conn.execute("SELECT * FROM ventas WHERE id=?", (venta_id,)).fetchone()
        conn.commit(); conn.close()
        self.send_json({"ok": True, "venta": row_to_dict(row)}, 201)
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 6: Proteger endpoints de turnos**

En `_get_turno_activo`:
```python
def _get_turno_activo(self):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.tienda_id is None:  # superadmin no opera cajas
        self.send_json(None); return
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM turnos WHERE estado='abierto' AND tienda_id=? ORDER BY apertura_at DESC LIMIT 1",
                (ctx.tienda_id,)
            ).fetchone()
            self.send_json(row_to_dict(row) if row else None)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

En `_list_turnos`:
```python
def _list_turnos(self):
    ctx = self.require_auth()
    if not ctx: return
    try:
        conn = get_db()
        try:
            if ctx.rol == 'superadmin':
                rows = conn.execute("SELECT * FROM turnos ORDER BY apertura_at DESC LIMIT 120").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM turnos WHERE tienda_id=? ORDER BY apertura_at DESC LIMIT 120",
                    (ctx.tienda_id,)
                ).fetchall()
            self.send_json([row_to_dict(r) for r in rows])
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

En `_open_turno`, agrega `ctx = self.require_auth(); if not ctx: return` al inicio, y agrega `tienda_id=ctx.tienda_id` al INSERT y al WHERE de la verificación de turno activo:

```python
def _open_turno(self):
    ctx = self.require_auth()
    if not ctx: return
    try:
        body = self.read_json_body()
        cajero = body.get("cajero", "").strip()
        if not cajero:
            self.send_error_json("Falta el nombre del cajero"); return
        conn = get_db()
        try:
            activo = conn.execute(
                "SELECT * FROM turnos WHERE estado='abierto' AND tienda_id=? ORDER BY apertura_at DESC LIMIT 1",
                (ctx.tienda_id,)
            ).fetchone()
            if activo:
                self.send_json({"error": "Ya existe un turno abierto", "turno_activo": row_to_dict(activo)}, 409)
                return
            c = conn.cursor()
            c.execute(
                "INSERT INTO turnos (cajero, monto_inicial, caja_id, tienda_id) VALUES (?,?,?,?)",
                (cajero, body.get("monto_inicial", 0), body.get("caja_id", "caja-1"), ctx.tienda_id)
            )
            turno = conn.execute("SELECT * FROM turnos WHERE id=?", (c.lastrowid,)).fetchone()
            conn.commit()
            self.send_json(row_to_dict(turno), 201)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

En `_close_turno`, agrega `ctx` y filtra por `tienda_id`:

```python
def _close_turno(self, turno_id):
    ctx = self.require_auth()
    if not ctx: return
    try:
        body = self.read_json_body()
        conn = get_db()
        try:
            tienda_filter = "" if ctx.rol == 'superadmin' else " AND tienda_id=?"
            params_filter = () if ctx.rol == 'superadmin' else (ctx.tienda_id,)
            conn.execute(
                f"""UPDATE turnos SET estado='cerrado', cierre_at=CURRENT_TIMESTAMP,
                    efectivo_ventas=?, transferencias=?, total_ventas=?, num_tx=?,
                    monto_contado=?, diferencia=?, resumen_ia=?
                   WHERE id=? AND estado='abierto'{tienda_filter}""",
                (body.get("efectivo_ventas",0), body.get("transferencias",0),
                 body.get("total_ventas",0), body.get("num_tx",0),
                 body.get("monto_contado"), body.get("diferencia"),
                 body.get("resumen_ia"), turno_id) + params_filter
            )
            turno = conn.execute("SELECT * FROM turnos WHERE id=?", (turno_id,)).fetchone()
            if not turno or turno["estado"] != "cerrado":
                self.send_error_json("Turno no encontrado o ya cerrado", 409); return
            conn.commit()
            self.send_json(row_to_dict(turno))
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 7: Probar endpoints protegidos con curl**

```bash
# Sin token → debe retornar 401
curl -s http://localhost:5051/api/productos
# Esperado: {"error":"No autenticado"}

# Con token de superadmin → retorna todos los productos
TOKEN="<token del login en Task 4>"
curl -s http://localhost:5051/api/productos -H "Authorization: Bearer $TOKEN"
# Esperado: lista de productos
```

- [ ] **Step 8: Commit**

```bash
git add server.py
git commit -m "feat: proteger endpoints existentes con require_auth() y filtro tienda_id"
```

---

## Task 6: API gestión de tiendas (superadmin)

**Files:**
- Modify: `server.py` (do_GET, do_POST, do_PUT routing + nuevos métodos)

- [ ] **Step 1: Agregar routing `GET /api/tiendas` y `GET /api/tiendas/:id/usuarios` en `do_GET`**

Agrega antes de `elif path == "/api/productos"`:

```python
elif path == "/api/tiendas":
    self._list_tiendas()
elif path.startswith("/api/tiendas/") and path.endswith("/usuarios"):
    parts = path.split("/")
    if len(parts) == 5:
        self._list_tienda_usuarios(int(parts[3]))
    else:
        self.send_error_json("Ruta no encontrada", 404)
```

- [ ] **Step 2: Agregar routing `POST /api/tiendas` y `POST /api/tiendas/:id/usuarios` en `do_POST`**

En `do_POST`, dentro del bloque `if/elif` ya existente, agrega estas dos ramas **antes del `else` final**:

```python
elif self.path == "/api/tiendas":
    self._create_tienda()
elif self.path.startswith("/api/tiendas/") and self.path.endswith("/usuarios"):
    parts = self.path.split("/")
    if len(parts) == 5:
        self._create_tienda_usuario(int(parts[3]))
    else:
        self.send_error_json("Ruta no encontrada", 404)
```

- [ ] **Step 3: Agregar routing `PUT /api/tiendas/:id` en `do_PUT`**

En `do_PUT`, agrega:

```python
elif len(parts) == 4 and parts[2] == "tiendas":
    self._update_tienda(int(parts[3]))
```

- [ ] **Step 4: Implementar métodos**

```python
def _list_tiendas(self):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol != 'superadmin':
        self.send_error_json("Acceso denegado", 403); return
    try:
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM tiendas ORDER BY created_at DESC").fetchall()
            result = []
            for t in rows:
                d = row_to_dict(t)
                d["num_usuarios"] = conn.execute(
                    "SELECT COUNT(*) FROM usuarios WHERE tienda_id=?", (t["id"],)
                ).fetchone()[0]
                result.append(d)
            self.send_json(result)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _create_tienda(self):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol != 'superadmin':
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        nombre = body.get("nombre","").strip()
        slug = body.get("slug","").strip()
        if not nombre or not slug:
            self.send_error_json("Faltan nombre o slug"); return
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO tiendas (nombre, slug, plan) VALUES (?,?,?)",
                (nombre, slug, body.get("plan","basico"))
            )
            tienda = conn.execute("SELECT * FROM tiendas WHERE id=?", (c.lastrowid,)).fetchone()
            conn.commit()
            self.send_json(row_to_dict(tienda), 201)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _update_tienda(self, tienda_id):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol != 'superadmin':
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        conn = get_db()
        try:
            conn.execute(
                "UPDATE tiendas SET nombre=?, plan=?, activo=? WHERE id=?",
                (body.get("nombre"), body.get("plan","basico"), int(body.get("activo",1)), tienda_id)
            )
            tienda = conn.execute("SELECT * FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
            if not tienda:
                self.send_error_json("Tienda no encontrada", 404); return
            conn.commit()
            self.send_json(row_to_dict(tienda))
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _list_tienda_usuarios(self, tienda_id):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol != 'superadmin':
        self.send_error_json("Acceso denegado", 403); return
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE tienda_id=? ORDER BY nombre",
                (tienda_id,)
            ).fetchall()
            self.send_json([row_to_dict(r) for r in rows])
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _create_tienda_usuario(self, tienda_id):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol != 'superadmin':
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        nombre = body.get("nombre","").strip()
        username = body.get("username","").strip().lower()
        password = body.get("password","")
        rol = body.get("rol","cajero")
        if not nombre or not username or not password:
            self.send_error_json("Faltan campos requeridos"); return
        if rol not in ('admin','cajero','superadmin'):
            self.send_error_json("Rol inválido", 400); return
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO usuarios (tienda_id,nombre,username,password_hash,salt,rol) VALUES (?,?,?,?,?,?)",
                (tienda_id, nombre, username, password_hash, salt, rol)
            )
            user = conn.execute(
                "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                (c.lastrowid,)
            ).fetchone()
            conn.commit()
            self.send_json(row_to_dict(user), 201)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 5: Probar**

```bash
TOKEN="<token superadmin>"

# Crear tienda
curl -s -X POST http://localhost:5051/api/tiendas \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nombre":"Minimercado La 45","slug":"la-45","plan":"basico"}'
# Esperado: {"id":1,"nombre":"Minimercado La 45",...}

# Listar tiendas
curl -s http://localhost:5051/api/tiendas -H "Authorization: Bearer $TOKEN"

# Crear usuario admin en la tienda 1
curl -s -X POST http://localhost:5051/api/tiendas/1/usuarios \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nombre":"María García","username":"maria","password":"clave123","rol":"admin"}'
```

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "feat: API gestión de tiendas (superadmin)"
```

---

## Task 7: API gestión de usuarios (admin + superadmin)

**Files:**
- Modify: `server.py` (do_GET, do_POST, do_PUT routing + nuevos métodos)

- [ ] **Step 1: Agregar routing en `do_GET`, `do_POST`, `do_PUT`**

En `do_GET`, agrega:
```python
elif path == "/api/usuarios":
    self._list_usuarios()
```

En `do_POST`, agrega:
```python
elif self.path == "/api/usuarios":
    self._create_usuario()
```

En `do_PUT`, agrega dos ramas (antes del `else`):
```python
elif len(parts) == 4 and parts[2] == "usuarios":
    self._update_usuario(int(parts[3]))
elif len(parts) == 5 and parts[2] == "usuarios" and parts[4] == "password":
    self._update_usuario_password(int(parts[3]))
```

- [ ] **Step 2: Implementar métodos**

```python
def _list_usuarios(self):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol not in ('admin','superadmin'):
        self.send_error_json("Acceso denegado", 403); return
    try:
        conn = get_db()
        try:
            if ctx.rol == 'superadmin':
                rows = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios ORDER BY nombre"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE tienda_id=? ORDER BY nombre",
                    (ctx.tienda_id,)
                ).fetchall()
            self.send_json([row_to_dict(r) for r in rows])
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _create_usuario(self):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol not in ('admin','superadmin'):
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        nombre = body.get("nombre","").strip()
        username = body.get("username","").strip().lower()
        password = body.get("password","")
        rol = body.get("rol","cajero")
        if not nombre or not username or not password:
            self.send_error_json("Faltan campos requeridos"); return
        # Admin no puede crear superadmins
        if rol == 'superadmin' and ctx.rol != 'superadmin':
            self.send_error_json("No puedes crear usuarios superadmin", 403); return
        if rol not in ('admin','cajero','superadmin'):
            self.send_error_json("Rol inválido", 400); return
        # Admin siempre crea en su propia tienda
        tienda_id = ctx.tienda_id if ctx.rol != 'superadmin' else body.get("tienda_id")
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO usuarios (tienda_id,nombre,username,password_hash,salt,rol) VALUES (?,?,?,?,?,?)",
                (tienda_id, nombre, username, password_hash, salt, rol)
            )
            user = conn.execute(
                "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                (c.lastrowid,)
            ).fetchone()
            conn.commit()
            self.send_json(row_to_dict(user), 201)
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _update_usuario(self, user_id):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol not in ('admin','superadmin'):
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        conn = get_db()
        try:
            target = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
            if not target:
                self.send_error_json("Usuario no encontrado", 404); return
            # Admin solo puede modificar usuarios de su tienda
            if ctx.rol == 'admin' and target["tienda_id"] != ctx.tienda_id:
                self.send_error_json("Acceso denegado", 403); return
            # Bloquear auto-escalación de rol
            if "rol" in body and body["rol"] == "superadmin" and ctx.rol != 'superadmin':
                self.send_error_json("No puedes asignar rol superadmin", 403); return
            # Bloquear auto-cambio de rol propio
            if "rol" in body and user_id == ctx.user_id:
                self.send_error_json("No puedes cambiar tu propio rol", 403); return
            nuevo_rol = body.get("rol", target["rol"])
            nuevo_nombre = body.get("nombre", target["nombre"])
            nuevo_activo = int(body.get("activo", target["activo"]))
            conn.execute(
                "UPDATE usuarios SET nombre=?, rol=?, activo=? WHERE id=?",
                (nuevo_nombre, nuevo_rol, nuevo_activo, user_id)
            )
            user = conn.execute(
                "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                (user_id,)
            ).fetchone()
            conn.commit()
            self.send_json(row_to_dict(user))
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)

def _update_usuario_password(self, user_id):
    ctx = self.require_auth()
    if not ctx: return
    if ctx.rol not in ('admin','superadmin'):
        self.send_error_json("Acceso denegado", 403); return
    try:
        body = self.read_json_body()
        new_password = body.get("password","")
        if not new_password:
            self.send_error_json("Falta la nueva contraseña"); return
        conn = get_db()
        try:
            target = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
            if not target:
                self.send_error_json("Usuario no encontrado", 404); return
            if ctx.rol == 'admin' and target["tienda_id"] != ctx.tienda_id:
                self.send_error_json("Acceso denegado", 403); return
            salt = secrets.token_hex(16)
            password_hash = _hash_password(new_password, salt)
            conn.execute(
                "UPDATE usuarios SET password_hash=?, salt=? WHERE id=?",
                (password_hash, salt, user_id)
            )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()
    except Exception as e:
        self.send_error_json(str(e), 500)
```

- [ ] **Step 3: Probar**

```bash
# Login como admin de tienda (crear primero con el superadmin en Task 6)
curl -s -X POST http://localhost:5051/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"maria","password":"clave123"}'

# Con token de admin: crear un cajero
ADMIN_TOKEN="<token admin>"
curl -s -X POST http://localhost:5051/api/usuarios \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nombre":"Juan Cajero","username":"juan","password":"caja123","rol":"cajero"}'
```

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: API gestión de usuarios (admin y superadmin)"
```

---

## Task 8: Frontend — `currentUser` global + `apiFetch()` helper

**Files:**
- Modify: `index.html` (globals ~línea 1484, cerca de `let products`)

- [ ] **Step 1: Agregar `currentUser` global y `apiFetch()` junto a los globals existentes**

Cerca de `let products = [];` (~línea 1484), agrega:

```javascript
let currentUser = null;
// { id, nombre, username, rol, tienda_id } — tienda_id === null para superadmin

async function apiFetch(url, options = {}) {
  const token = localStorage.getItem('sfpos_token');
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    ['sfpos_token','sfpos_exp','sfpos_user','sfpos_tienda'].forEach(k => localStorage.removeItem(k));
    showLoginScreen('Sesión expirada. Ingresa de nuevo.');
    return null;
  }
  return res;
}
```

- [ ] **Step 2: Actualizar `recargarInventario()` para usar `apiFetch`**

Reemplaza la función del Task 1:

```javascript
async function recargarInventario() {
  const btn = document.getElementById('btn-refresh-inv');
  if (btn) btn.disabled = true;
  try {
    const res = await apiFetch('/api/productos');
    if (!res) return;
    if (!res.ok) { showToast('⚠ Error al actualizar'); return; }
    products = await res.json();
    renderInventario();
    showToast('✅ Inventario actualizado');
  } finally {
    if (btn) btn.disabled = false;
  }
}
```

- [ ] **Step 3: Reemplazar todos los `fetch(` existentes por `apiFetch(`**

Busca todas las llamadas `fetch('/api/` y `fetch("/api/` en `index.html` y reemplázalas por `apiFetch`. Las llamadas a la IA (`/analyze-product`, `/api/ai-cierre`, `/api/ai-pedido`) también deben usar `apiFetch`.

Patrón de búsqueda: `fetch('/api/` y `await fetch(`

**Importante:** Después de cada `await apiFetch(...)`, agrega verificación de null:
```javascript
const res = await apiFetch('/api/...');
if (!res) return;  // ← sesión expirada, apiFetch ya redirigió al login
```

- [ ] **Step 4: Actualizar `loadData()` para usar `apiFetch`**

```javascript
async function loadData() {
  try {
    const [pRes, vRes, tRes] = await Promise.all([
      apiFetch('/api/productos'),
      apiFetch('/api/ventas'),
      apiFetch('/api/turnos/activo')
    ]);
    if (!pRes || !vRes || !tRes) return; // sesión expirada
    products = await pRes.json();
    sales    = (await vRes.json()).map(normalizarVenta);
    const turnoActivo = await tRes.json();
    if (turnoActivo && turnoActivo.id) {
      cajaSession = {
        id:           turnoActivo.id,
        cajero:       turnoActivo.cajero,
        inicio:       new Date(turnoActivo.apertura_at.replace(' ','T')),
        montoInicial: turnoActivo.monto_inicial,
        ventasIds:    sales.filter(s => s.turno_id === turnoActivo.id).map(s => s.id)
      };
      updateCajaBar();
    }
  } catch (err) {
    showToast('⚠ Error cargando datos del servidor');
  }
}
```

- [ ] **Step 5: Commit**

```bash
git add index.html
git commit -m "feat: currentUser global, apiFetch() helper con manejo de 401"
```

---

## Task 9: Frontend — pantalla de login

**Files:**
- Modify: `index.html` (HTML antes de `<div id="app">`, CSS en `<style>`, JS cerca de globals)

- [ ] **Step 1: Agregar HTML de la pantalla de login**

Justo después de `<body>` y antes de `<div id="app">`, inserta:

```html
<!-- PANTALLA DE LOGIN -->
<div id="login-screen" style="display:none; position:fixed; inset:0; z-index:9999; background:var(--bg); flex-direction:column; align-items:center; justify-content:center; padding:24px;">
  <div style="width:100%; max-width:380px;">
    <div style="text-align:center; margin-bottom:32px;">
      <div style="font-size:48px; margin-bottom:8px;">🛒</div>
      <div style="font-family:var(--ff-disp); font-size:32px; font-weight:800; letter-spacing:-.02em; color:var(--text)">SFPOS</div>
      <div style="font-size:13px; color:var(--text-2); margin-top:4px;">Sistema de Punto de Venta</div>
    </div>
    <div style="background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:28px 24px;">
      <div style="margin-bottom:16px;">
        <label style="display:block; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.07em; color:var(--text-2); margin-bottom:6px;">Usuario</label>
        <input type="text" id="login-username" class="form-input" placeholder="Ej: admin" autocomplete="username" autocapitalize="none" style="width:100%; box-sizing:border-box;">
      </div>
      <div style="margin-bottom:20px;">
        <label style="display:block; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.07em; color:var(--text-2); margin-bottom:6px;">Contraseña</label>
        <input type="password" id="login-password" class="form-input" placeholder="••••••••" autocomplete="current-password" style="width:100%; box-sizing:border-box;">
      </div>
      <div id="login-error" style="display:none; color:var(--danger); font-size:13px; margin-bottom:12px; padding:8px 12px; background:var(--danger-bg,#fee2e2); border-radius:8px;"></div>
      <button id="login-btn" class="btn-cobrar" onclick="doLogin()" style="width:100%;">Ingresar</button>
    </div>
    <div style="text-align:center; margin-top:16px; font-size:12px; color:var(--text-3);">SFPOS v1.0 · sfpos.app</div>
  </div>
</div>
```

- [ ] **Step 2: Asegurar que `#app` se oculta al inicio**

Cambia `<div id="app">` a:

```html
<div id="app" style="display:none;">
```

- [ ] **Step 3: Agregar funciones de login/logout**

```javascript
function showLoginScreen(msg = '') {
  document.getElementById('app').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';  // override display:none
  const errEl = document.getElementById('login-error');
  if (msg) { errEl.textContent = msg; errEl.style.display = 'block'; }
  else { errEl.style.display = 'none'; }
  document.getElementById('login-username').focus();
}

async function doLogin() {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  const btn = document.getElementById('login-btn');
  errEl.style.display = 'none';
  if (!username || !password) {
    errEl.textContent = 'Ingresa usuario y contraseña'; errEl.style.display = 'block'; return;
  }
  btn.disabled = true; btn.textContent = 'Ingresando...';
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username, password})
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'Credenciales incorrectas';
      errEl.style.display = 'block'; return;
    }
    localStorage.setItem('sfpos_token',  data.token);
    localStorage.setItem('sfpos_exp',    String(data.exp));
    localStorage.setItem('sfpos_user',   JSON.stringify(data.user));
    localStorage.setItem('sfpos_tienda', JSON.stringify(data.tienda));
    await iniciarApp(data.user, data.tienda);
  } catch(e) {
    errEl.textContent = 'Error de conexión'; errEl.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Ingresar';
  }
}

// Enter en campos de login dispara doLogin
document.getElementById('login-password').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});
document.getElementById('login-username').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('login-password').focus();
});

function doLogout() {
  if (cajaSession) { showToast('⚠ Cierra la caja antes de cerrar sesión'); return; }
  if (!confirm('¿Cerrar sesión?')) return;
  if (_expiryCheckInterval) { clearInterval(_expiryCheckInterval); _expiryCheckInterval = null; }
  const banner = document.getElementById('session-expiry-banner');
  if (banner) banner.style.display = 'none';
  ['sfpos_token','sfpos_exp','sfpos_user','sfpos_tienda'].forEach(k => localStorage.removeItem(k));
  currentUser = null;
  showLoginScreen();
}
```

- [ ] **Step 4: Commit**

```bash
git add index.html
git commit -m "feat: pantalla de login SFPOS con branding"
```

---

## Task 10: Frontend — boot sequence, flujo por rol, barra superior, aviso de expiración

**Files:**
- Modify: `index.html` (HTML nav ~línea 1068, JS boot sequence ~línea 2930)

- [ ] **Step 1: Agregar chip de usuario + botón logout en la barra superior**

En el nav, después de `<div class="mode-switcher">...</div>`, agrega:

```html
<div id="user-chip" style="display:none; align-items:center; gap:8px; font-size:13px; color:var(--text-2);">
  <span id="user-chip-name" style="font-weight:600; color:var(--text);"></span>
  <button onclick="doLogout()" title="Cerrar sesión" style="background:none;border:none;cursor:pointer;font-size:16px;padding:4px;">🚪</button>
</div>
```

- [ ] **Step 2: Agregar banner de sesión próxima a expirar**

Justo después de `<div id="caja-bar">...</div>` (~línea 1088), agrega:

```html
<div id="session-expiry-banner" style="display:none; background:var(--warning-bg,#fef3c7); color:var(--warning,#92400e); padding:8px 20px; font-size:13px; text-align:center;">
  ⏰ Tu sesión expira en menos de 1 hora. Guarda cambios.
</div>
```

- [ ] **Step 3: Agregar pantalla superadmin placeholder**

Después de `<div class="screen" id="screen-reportes">...</div>` (al final de las screens), agrega:

```html
<div class="screen" id="screen-superadmin" style="display:none; flex-direction:column; overflow-y:auto;">
  <div style="padding:32px 24px;">
    <h2 style="font-family:var(--ff-disp); font-size:20px; font-weight:700; margin-bottom:8px;">Panel Superadmin</h2>
    <p style="color:var(--text-2); font-size:14px; margin-bottom:24px;">Gestión de tiendas y usuarios de la plataforma SFPOS.</p>
    <div id="superadmin-content">Cargando...</div>
  </div>
</div>
```

- [ ] **Step 4: Agregar función `iniciarApp()` y reemplazar el boot IIFE**

Reemplaza el bloque de inicio (~línea 2925-2938):

```javascript
// ── BOOT ───────────────────────────────────────────────────────────
async function iniciarApp(user, tienda) {
  currentUser = user;
  // Ocultar switcher modo — lo determina el rol
  document.querySelector('.mode-switcher').style.display = 'none';
  // Mostrar chip de usuario
  const chip = document.getElementById('user-chip');
  document.getElementById('user-chip-name').textContent = user.nombre;
  chip.style.display = 'flex';
  // Mostrar app, ocultar login
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('app').style.display = 'flex';

  if (user.rol === 'superadmin') {
    // Ocultar nav tabs normales, mostrar pantalla superadmin
    document.getElementById('tab-cobro').style.display = 'none';
    document.getElementById('tab-inventario').style.display = 'none';
    document.getElementById('tab-reportes').style.display = 'none';
    document.getElementById('caja-bar').style.display = 'none';
    // Mostrar screen superadmin directamente
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById('screen-superadmin').style.display = 'flex';
    document.getElementById('screen-superadmin').classList.add('active');
    await renderSuperadmin();
  } else {
    // Modo determinado por rol
    const modo = user.rol === 'admin' ? 'admin' : 'cajero';
    setMode(modo);
    updateCajaBar();
    await loadData();
    renderCatPills();
    renderProducts();
    renderCart();
    renderInventario();
    renderReportes();
    updateLowStockBadge();
    // Si admin, agregar pestaña Usuarios
    if (user.rol === 'admin') {
      let tabUsuarios = document.getElementById('tab-usuarios');
      if (!tabUsuarios) {
        tabUsuarios = document.createElement('div');
        tabUsuarios.id = 'tab-usuarios';
        tabUsuarios.className = 'nav-tab';
        tabUsuarios.dataset.screen = 'usuarios';
        tabUsuarios.innerHTML = '<span class="tab-icon">👥</span> Usuarios';
        tabUsuarios.onclick = () => switchScreen('usuarios');
        document.getElementById('tab-reportes').after(tabUsuarios);
      }
      tabUsuarios.style.display = '';
    }
  }
  iniciarCheckExpiracion();
}

let _expiryCheckInterval = null;

function iniciarCheckExpiracion() {
  if (_expiryCheckInterval) clearInterval(_expiryCheckInterval); // evitar intervalos duplicados
  _expiryCheckInterval = setInterval(() => {
    const exp = parseInt(localStorage.getItem('sfpos_exp') || '0');
    const ahora = Math.floor(Date.now() / 1000);
    const banner = document.getElementById('session-expiry-banner');
    if (exp > 0 && exp - ahora < 3600 && exp > ahora) {
      if (banner) banner.style.display = 'block';
    }
  }, 60000); // revisar cada minuto
}

// Secuencia de arranque
(async () => {
  // Fecha en modal apertura
  document.getElementById('apertura-fecha').textContent =
    new Date().toLocaleDateString('es-CO', {weekday:'long', year:'numeric', month:'long', day:'numeric'});

  const token = localStorage.getItem('sfpos_token');
  const exp   = parseInt(localStorage.getItem('sfpos_exp') || '0');
  const ahora = Math.floor(Date.now() / 1000);

  if (!token || exp < ahora) {
    // Sin token o expirado
    ['sfpos_token','sfpos_exp','sfpos_user','sfpos_tienda'].forEach(k => localStorage.removeItem(k));
    showLoginScreen();
    return;
  }

  // Validar token con el servidor.
  // NOTA: usamos fetch() directamente (no apiFetch) porque apiFetch llama showLoginScreen()
  // en 401, lo que crearia un ciclo si la sesión ya expiró. Este es el único lugar donde
  // se maneja el 401 manualmente en el boot.
  try {
    const res = await fetch('/api/auth/me', {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (!res.ok) {
      ['sfpos_token','sfpos_exp','sfpos_user','sfpos_tienda'].forEach(k => localStorage.removeItem(k));
      showLoginScreen('Sesión expirada. Ingresa de nuevo.');
      return;
    }
    const data = await res.json();
    await iniciarApp(data.user, data.tienda);
  } catch(e) {
    showLoginScreen('Error de conexión. Intenta de nuevo.');
  }
})();
```

- [ ] **Step 5: Asegurar que `switchScreen` maneja la pantalla `usuarios`**

En `switchScreen()`, agrega:

```javascript
if (name === 'usuarios') renderPanelUsuarios();
```

- [ ] **Step 6: Agregar `abrirCaja()` pre-rellena nombre desde `currentUser`**

En la función `abrirCaja()`, reemplaza la línea que lee el nombre del cajero:

```javascript
const cajero = currentUser?.nombre || document.getElementById('apertura-cajero').value.trim();
```

Y en el modal de apertura, agrega `readonly` y pre-rellena el campo:

En `iniciarApp()`, antes de `updateCajaBar()`:
```javascript
const campoNombre = document.getElementById('apertura-cajero');
if (campoNombre) {
  campoNombre.value = user.nombre;
  campoNombre.readOnly = true;
}
```

- [ ] **Step 7: Probar flujo completo**

1. `python3 server.py` (con `SECRET_KEY=test123`)
2. Abrir `http://localhost:5051` → debe mostrar pantalla de login
3. Login con superadmin → debe mostrar panel superadmin
4. Login con cajero → debe ir directo a modo Cobro
5. Login con admin → debe ver Inventario, Reportes, Usuarios

- [ ] **Step 8: Commit**

```bash
git add index.html
git commit -m "feat: boot sequence con auth, flujo por rol, barra superior, logout"
```

---

## Task 11: Frontend — panel superadmin

**Files:**
- Modify: `index.html` (función `renderSuperadmin` y modales)

- [ ] **Step 1: Agregar HTML del modal "Nueva tienda"**

Junto a los otros modales (cerca del `modal-apertura`), agrega:

```html
<div class="modal-overlay" id="modal-nueva-tienda">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">Nueva Tienda</span><button class="modal-close" onclick="closeModal('modal-nueva-tienda')">✕</button></div>
    <div class="modal-body">
      <div class="form-group"><label class="form-label">Nombre</label><input type="text" class="form-input" id="tienda-nombre" placeholder="Ej: Minimercado La 45"></div>
      <div class="form-group"><label class="form-label">Slug</label><input type="text" class="form-input" id="tienda-slug" placeholder="Ej: la-45"></div>
      <div class="form-group"><label class="form-label">Plan</label>
        <select class="form-input" id="tienda-plan">
          <option value="basico">Básico</option>
          <option value="pro">Pro</option>
        </select>
      </div>
    </div>
    <div class="modal-footer"><button class="btn-primary" onclick="crearTienda()">Crear Tienda</button></div>
  </div>
</div>

<div class="modal-overlay" id="modal-nuevo-usuario-sa">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">Nuevo Usuario</span><button class="modal-close" onclick="closeModal('modal-nuevo-usuario-sa')">✕</button></div>
    <div class="modal-body">
      <div class="form-group"><label class="form-label">Nombre</label><input type="text" class="form-input" id="sa-user-nombre" placeholder="Ej: María García"></div>
      <div class="form-group"><label class="form-label">Usuario</label><input type="text" class="form-input" id="sa-user-username" placeholder="Ej: maria" autocapitalize="none"></div>
      <div class="form-group"><label class="form-label">Contraseña</label><input type="password" class="form-input" id="sa-user-password"></div>
      <div class="form-group"><label class="form-label">Rol</label>
        <select class="form-input" id="sa-user-rol">
          <option value="cajero">Cajero</option>
          <option value="admin">Admin</option>
        </select>
      </div>
      <input type="hidden" id="sa-user-tienda-id">
    </div>
    <div class="modal-footer"><button class="btn-primary" onclick="crearUsuarioSA()">Crear Usuario</button></div>
  </div>
</div>
```

- [ ] **Step 2: Agregar función `renderSuperadmin()`**

```javascript
async function renderSuperadmin() {
  const content = document.getElementById('superadmin-content');
  content.innerHTML = '<p style="color:var(--text-2)">Cargando tiendas...</p>';
  try {
    const res = await apiFetch('/api/tiendas');
    if (!res) return;
    const tiendas = await res.json();
    content.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
        <h3 style="font-size:15px; font-weight:700;">Tiendas (${tiendas.length})</h3>
        <button class="btn-primary" onclick="openModal('modal-nueva-tienda')">+ Nueva Tienda</button>
      </div>
      <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:14px;">
          <thead>
            <tr style="border-bottom:2px solid var(--border); text-align:left;">
              <th style="padding:8px 12px; color:var(--text-2);">Nombre</th>
              <th style="padding:8px 12px; color:var(--text-2);">Slug</th>
              <th style="padding:8px 12px; color:var(--text-2);">Plan</th>
              <th style="padding:8px 12px; color:var(--text-2); text-align:right;">Usuarios</th>
              <th style="padding:8px 12px; color:var(--text-2);">Estado</th>
              <th style="padding:8px 12px; color:var(--text-2);">Acciones</th>
            </tr>
          </thead>
          <tbody>
            ${tiendas.length === 0
              ? '<tr><td colspan="6" style="padding:20px; text-align:center; color:var(--text-2)">Sin tiendas registradas</td></tr>'
              : tiendas.map(t => `
              <tr style="border-bottom:1px solid var(--border); cursor:pointer;" onclick="verDetalleTienda(${t.id})">
                <td style="padding:10px 12px; font-weight:600;">${esc(t.nombre)}</td>
                <td style="padding:10px 12px; color:var(--text-2); font-size:12px;">${esc(t.slug)}</td>
                <td style="padding:10px 12px;">${esc(t.plan)}</td>
                <td style="padding:10px 12px; text-align:right;">${t.num_usuarios}</td>
                <td style="padding:10px 12px;">${t.activo ? '🟢 Activa' : '🔴 Inactiva'}</td>
                <td style="padding:10px 12px;">
                  <button class="btn-secondary" style="font-size:12px; padding:4px 8px;" onclick="event.stopPropagation(); verDetalleTienda(${t.id})">Ver →</button>
                </td>
              </tr>`).join('')
            }
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    content.innerHTML = '<p style="color:var(--danger)">Error cargando tiendas</p>';
  }
}

async function verDetalleTienda(tiendaId) {
  const content = document.getElementById('superadmin-content');
  content.innerHTML = '<p style="color:var(--text-2)">Cargando...</p>';
  try {
    // Nota: /api/tiendas retorna todas las tiendas (no hay GET /api/tiendas/:id).
    // Para evitar un endpoint extra en esta fase, obtenemos la lista y buscamos localmente.
    const [tRes, uRes] = await Promise.all([
      apiFetch(`/api/tiendas`),
      apiFetch(`/api/tiendas/${tiendaId}/usuarios`)
    ]);
    if (!tRes || !uRes) return;
    const tiendas = await tRes.json();
    const tienda = tiendas.find(t => t.id === tiendaId);
    const usuarios = await uRes.json();
    content.innerHTML = `
      <button class="btn-secondary" onclick="renderSuperadmin()" style="margin-bottom:16px;">← Volver</button>
      <h3 style="font-size:16px; font-weight:700; margin-bottom:4px;">${esc(tienda.nombre)}</h3>
      <p style="color:var(--text-2); font-size:13px; margin-bottom:20px;">${esc(tienda.slug)} · ${esc(tienda.plan)} · ${tienda.activo ? '🟢 Activa' : '🔴 Inactiva'}</p>
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
        <h4 style="font-size:14px; font-weight:700;">Usuarios</h4>
        <button class="btn-primary" onclick="_abrirModalNuevoUsuarioSA(${tiendaId})">+ Nuevo Usuario</button>
      </div>
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <thead><tr style="border-bottom:2px solid var(--border); text-align:left;">
          <th style="padding:8px 12px; color:var(--text-2);">Nombre</th>
          <th style="padding:8px 12px; color:var(--text-2);">Usuario</th>
          <th style="padding:8px 12px; color:var(--text-2);">Rol</th>
          <th style="padding:8px 12px; color:var(--text-2);">Estado</th>
          <th style="padding:8px 12px; color:var(--text-2);">Acciones</th>
        </tr></thead>
        <tbody>
          ${usuarios.map(u => `
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:10px 12px;">${esc(u.nombre)}</td>
            <td style="padding:10px 12px; color:var(--text-2);">${esc(u.username)}</td>
            <td style="padding:10px 12px;">${u.rol}</td>
            <td style="padding:10px 12px;">${u.activo ? '✅ Activo' : '⛔ Inactivo'}</td>
            <td style="padding:10px 12px;">
              <button class="btn-secondary" style="font-size:12px; padding:4px 8px;" onclick="toggleUsuarioActivo(${u.id},${u.activo ? 0 : 1},${tiendaId})">${u.activo ? 'Desactivar' : 'Activar'}</button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  } catch(e) {
    content.innerHTML = '<p style="color:var(--danger)">Error cargando detalle</p>';
  }
}

function _abrirModalNuevoUsuarioSA(tiendaId) {
  document.getElementById('sa-user-tienda-id').value = tiendaId;
  document.getElementById('sa-user-nombre').value = '';
  document.getElementById('sa-user-username').value = '';
  document.getElementById('sa-user-password').value = '';
  openModal('modal-nuevo-usuario-sa');
}

async function crearTienda() {
  const nombre = document.getElementById('tienda-nombre').value.trim();
  const slug   = document.getElementById('tienda-slug').value.trim();
  const plan   = document.getElementById('tienda-plan').value;
  if (!nombre || !slug) { showToast('⚠ Ingresa nombre y slug'); return; }
  const res = await apiFetch('/api/tiendas', {
    method:'POST',
    body: JSON.stringify({nombre, slug, plan})
  });
  if (!res) return;
  if (!res.ok) { const d = await res.json(); showToast('⚠ ' + d.error); return; }
  closeModal('modal-nueva-tienda');
  showToast('✅ Tienda creada');
  await renderSuperadmin();
}

async function crearUsuarioSA() {
  const tiendaId = parseInt(document.getElementById('sa-user-tienda-id').value);
  const nombre   = document.getElementById('sa-user-nombre').value.trim();
  const username = document.getElementById('sa-user-username').value.trim();
  const password = document.getElementById('sa-user-password').value;
  const rol      = document.getElementById('sa-user-rol').value;
  if (!nombre || !username || !password) { showToast('⚠ Completa todos los campos'); return; }
  const res = await apiFetch(`/api/tiendas/${tiendaId}/usuarios`, {
    method:'POST',
    body: JSON.stringify({nombre, username, password, rol})
  });
  if (!res) return;
  if (!res.ok) { const d = await res.json(); showToast('⚠ ' + d.error); return; }
  closeModal('modal-nuevo-usuario-sa');
  showToast('✅ Usuario creado');
  await verDetalleTienda(tiendaId);
}

async function toggleUsuarioActivo(userId, nuevoActivo, tiendaId) {
  const res = await apiFetch(`/api/usuarios/${userId}`, {
    method:'PUT',
    body: JSON.stringify({activo: nuevoActivo})
  });
  if (!res) return;
  if (!res.ok) { showToast('⚠ Error al actualizar usuario'); return; }
  await verDetalleTienda(tiendaId);
}
```

- [ ] **Step 3: Probar panel superadmin**

1. Login como superadmin
2. Debe mostrar tabla de tiendas
3. Crear tienda → aparece en la lista
4. Click en tienda → ver detalle con usuarios
5. Crear usuario → aparece en la lista

- [ ] **Step 4: Commit**

```bash
git add index.html
git commit -m "feat: panel superadmin — gestión de tiendas y usuarios"
```

---

## Task 12: Frontend — panel Usuarios (admin) + `abrirCaja()` usa nombre del usuario

**Files:**
- Modify: `index.html` (nueva screen usuarios, modal nuevo usuario, `abrirCaja()`)

- [ ] **Step 1: Agregar `#screen-usuarios` HTML**

Después de `#screen-reportes`, agrega:

```html
<div class="screen" id="screen-usuarios" style="flex-direction:column; overflow-y:auto; padding:24px;">
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:20px;">
    <span class="page-title">Usuarios</span>
    <button class="btn-primary" onclick="openModal('modal-nuevo-usuario-admin')">+ Nuevo Usuario</button>
  </div>
  <div id="usuarios-list">Cargando...</div>
</div>
```

- [ ] **Step 2: Agregar modal "Nuevo usuario" para admin**

```html
<div class="modal-overlay" id="modal-nuevo-usuario-admin">
  <div class="modal">
    <div class="modal-header"><span class="modal-title">Nuevo Usuario</span><button class="modal-close" onclick="closeModal('modal-nuevo-usuario-admin')">✕</button></div>
    <div class="modal-body">
      <div class="form-group"><label class="form-label">Nombre completo</label><input type="text" class="form-input" id="adm-user-nombre" placeholder="Ej: Carlos Gómez"></div>
      <div class="form-group"><label class="form-label">Usuario</label><input type="text" class="form-input" id="adm-user-username" placeholder="Ej: carlos" autocapitalize="none"></div>
      <div class="form-group"><label class="form-label">Contraseña</label><input type="password" class="form-input" id="adm-user-password"></div>
      <div class="form-group"><label class="form-label">Rol</label>
        <select class="form-input" id="adm-user-rol">
          <option value="cajero">Cajero</option>
          <option value="admin">Admin</option>
        </select>
      </div>
    </div>
    <div class="modal-footer"><button class="btn-primary" onclick="crearUsuarioAdmin()">Crear Usuario</button></div>
  </div>
</div>
```

- [ ] **Step 3: Agregar funciones del panel Usuarios**

```javascript
async function renderPanelUsuarios() {
  const list = document.getElementById('usuarios-list');
  if (!list) return;
  list.innerHTML = 'Cargando...';
  try {
    const res = await apiFetch('/api/usuarios');
    if (!res) return;
    const usuarios = await res.json();
    if (!usuarios.length) {
      list.innerHTML = '<p style="color:var(--text-2)">Sin usuarios registrados.</p>';
      return;
    }
    list.innerHTML = `<div style="overflow-x:auto;">
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <thead><tr style="border-bottom:2px solid var(--border); text-align:left;">
          <th style="padding:8px 12px; color:var(--text-2);">Nombre</th>
          <th style="padding:8px 12px; color:var(--text-2);">Usuario</th>
          <th style="padding:8px 12px; color:var(--text-2);">Rol</th>
          <th style="padding:8px 12px; color:var(--text-2);">Estado</th>
          <th style="padding:8px 12px; color:var(--text-2);">Acciones</th>
        </tr></thead>
        <tbody>
          ${usuarios.map(u => `
          <tr style="border-bottom:1px solid var(--border);">
            <td style="padding:10px 12px; font-weight:600;">${esc(u.nombre)}</td>
            <td style="padding:10px 12px; color:var(--text-2);">${esc(u.username)}</td>
            <td style="padding:10px 12px;">${u.rol}</td>
            <td style="padding:10px 12px;">${u.activo ? '✅ Activo' : '⛔ Inactivo'}</td>
            <td style="padding:10px 12px; display:flex; gap:6px;">
              ${u.id !== currentUser?.id ? `<button class="btn-secondary" style="font-size:12px; padding:4px 8px;" onclick="toggleActivoAdmin(${u.id},${u.activo ? 0 : 1})">${u.activo ? 'Desactivar' : 'Activar'}</button>` : '(yo)'}
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  } catch(e) {
    list.innerHTML = '<p style="color:var(--danger)">Error cargando usuarios</p>';
  }
}

async function crearUsuarioAdmin() {
  const nombre   = document.getElementById('adm-user-nombre').value.trim();
  const username = document.getElementById('adm-user-username').value.trim();
  const password = document.getElementById('adm-user-password').value;
  const rol      = document.getElementById('adm-user-rol').value;
  if (!nombre || !username || !password) { showToast('⚠ Completa todos los campos'); return; }
  const res = await apiFetch('/api/usuarios', {
    method:'POST',
    body: JSON.stringify({nombre, username, password, rol})
  });
  if (!res) return;
  if (!res.ok) { const d = await res.json(); showToast('⚠ ' + d.error); return; }
  closeModal('modal-nuevo-usuario-admin');
  showToast('✅ Usuario creado');
  await renderPanelUsuarios();
}

async function toggleActivoAdmin(userId, nuevoActivo) {
  const res = await apiFetch(`/api/usuarios/${userId}`, {
    method:'PUT',
    body: JSON.stringify({activo: nuevoActivo})
  });
  if (!res) return;
  if (!res.ok) { showToast('⚠ Error al actualizar'); return; }
  await renderPanelUsuarios();
}
```

- [ ] **Step 4: Asegurar que el modal de apertura muestra el nombre del cajero como readonly**

En el HTML del modal de apertura de caja, busca el `<input type="text" ... id="apertura-cajero"` y agrega `readonly` como atributo:

```html
<input type="text" class="form-input" id="apertura-cajero" placeholder="Ej: María García" autocomplete="off" readonly style="background:var(--bg-2,var(--surface)); color:var(--text-2); cursor:default;">
```

- [ ] **Step 5: Probar panel de usuarios**

1. Login como admin
2. Ir a pestaña Usuarios
3. Crear un cajero
4. El cajero aparece en la lista
5. Desactivar usuario → ⛔ Inactivo

- [ ] **Step 6: Push final**

```bash
git add index.html
git commit -m "feat: panel Usuarios para admin + abrirCaja usa nombre del usuario logueado"
git push origin main
```

---

## Verificación final end-to-end

- [ ] Login como superadmin → ver panel tiendas → crear tienda → crear admin → crear cajero
- [ ] Login como admin → ver Inventario, Reportes, Usuarios → agregar producto → crear cajero
- [ ] Login como cajero → directo a Cobro → campo cajero pre-relleno → abrir caja → venta → cerrar caja
- [ ] Recargar página → sesión se restaura automáticamente → estado de caja correcto
- [ ] Botón 🔄 en inventario → actualiza sin recargar página
- [ ] Deploy a Railway con `SECRET_KEY` configurada → datos persisten en Turso
- [ ] Sin `SECRET_KEY` → servidor no arranca, muestra mensaje claro
