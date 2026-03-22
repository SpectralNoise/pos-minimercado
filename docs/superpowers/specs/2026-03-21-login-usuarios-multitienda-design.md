# SFPOS — Login, Usuarios y Multi-Tienda (SaaS)

## Contexto

SFPOS es un POS para mini mercados colombianos. Esta spec diseña el sistema de autenticación, gestión de usuarios y multi-tenancy SaaS: múltiples tiendas en un solo deployment Railway + Turso, con roles superadmin / admin / cajero.

También incluye un botón de "Actualizar inventario" (cambio menor independiente).

---

## Modelo de datos

### Tabla `tiendas`

```sql
CREATE TABLE IF NOT EXISTS tiendas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT    NOT NULL,
    slug       TEXT    NOT NULL UNIQUE,
    plan       TEXT    DEFAULT 'basico',
    activo     INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Tabla `usuarios`

```sql
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
    UNIQUE (tienda_id, username)  -- usernames únicos por tienda, no globalmente
);
```

**Notas:**
- `tienda_id = NULL` identifica al superadmin de plataforma (única excepción).
- `username` es único **dentro de cada tienda** — dos tiendas pueden tener su propio `cajero1`.
- El superadmin (`tienda_id = NULL`) es el único usuario global; su username sigue siendo único en la tabla porque NULL no viola la constraint `UNIQUE(tienda_id, username)` en SQLite (NULLs no se comparan). Para evitar ambigüedad, el servidor debe verificar `WHERE tienda_id IS NULL AND username = ?` explícitamente en el login del superadmin.
- `password_hash = PBKDF2-HMAC-SHA256(password, salt, iterations=260000)` usando `hashlib.pbkdf2_hmac`. La sal es aleatoria de 16 bytes hex.
- `ON DELETE RESTRICT` en `tienda_id` previene borrar una tienda con usuarios activos.

### Usuario inicial (seed)

Al inicializar la DB, si `usuarios` está vacía, se inserta el superadmin con una contraseña generada aleatoriamente:

```python
import secrets, hashlib
default_password = secrets.token_urlsafe(12)   # ej: "Xk9mP2vQrL4n"
salt = secrets.token_hex(16)
password_hash = hashlib.pbkdf2_hmac('sha256', default_password.encode(), salt.encode(), 260000).hex()
print(f"[SFPOS] Contraseña inicial del superadmin: {default_password}")
# INSERT INTO usuarios (tienda_id, nombre, username, password_hash, salt, rol)
# VALUES (NULL, 'Super Admin', 'admin', <password_hash>, <salt>, 'superadmin')
```

La contraseña se imprime **una sola vez** en los logs del servidor al primer arranque. No se documenta en el código fuente ni en el spec.

### Migraciones en tablas existentes

Agregar `tienda_id INTEGER` a `productos`, `ventas` y `turnos` usando el patrón `ALTER TABLE ... ADD COLUMN` ya establecido en `init_db()`:

```python
for table in ('productos', 'ventas', 'turnos'):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN tienda_id INTEGER REFERENCES tiendas(id)")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
```

Los registros existentes quedan con `tienda_id = NULL`. El superadmin los ve (no filtra por tienda). Los admins de tienda no los ven.

**`items_venta` no necesita `tienda_id`** — su aislamiento viene del JOIN con `ventas`, que sí tiene `tienda_id`.

---

## Autenticación — Tokens HMAC

### `SECRET_KEY`

Requerida como env var `SECRET_KEY` en Railway. Si no está configurada, el servidor **falla al arrancar** con un mensaje claro:

```python
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("[SFPOS] SECRET_KEY no configurada. Agrega esta env var en Railway.")
```

### Generación (POST /api/auth/login)

```
exp = int(time.time()) + 43200  # 12 horas
tienda_str = str(user.tienda_id) if user.tienda_id is not None else "null"
payload = f"{user.id}:{tienda_str}:{user.rol}:{exp}"
payload_b64 = base64.b64encode(payload.encode()).decode()
signature = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
token = f"{payload_b64}.{signature}"
```

La serialización de `tienda_id = None` como la cadena literal `"null"` es explícita e intencional. La deserialización convierte `"null"` → Python `None`.

La respuesta incluye `exp` para que el cliente pueda hacer la verificación local de expiración.

Respuesta para cajero/admin:
```json
{
  "token": "...",
  "exp": 1234567890,
  "user": {"id": 1, "nombre": "María", "username": "maria", "rol": "cajero", "tienda_id": 2},
  "tienda": {"id": 2, "nombre": "Minimercado La 45", "slug": "la-45"}
}
```

Respuesta para superadmin (`tienda_id` es `null`, `tienda` es `null`):
```json
{
  "token": "...",
  "exp": 1234567890,
  "user": {"id": 1, "nombre": "Super Admin", "username": "admin", "rol": "superadmin", "tienda_id": null},
  "tienda": null
}
```

### Validación (middleware en cada endpoint protegido)

```python
def require_auth(self) -> AuthContext | None:
    header = self.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        self.send_json({"error": "No autenticado"}, 401); return None
    token = header[7:]
    try:
        payload_b64, signature = token.rsplit(".", 1)
        payload = base64.b64decode(payload_b64).decode()
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            self.send_json({"error": "Token inválido"}, 401); return None
        user_id, tienda_str, rol, exp = payload.split(":")
        if int(exp) < time.time():
            self.send_json({"error": "Sesión expirada"}, 401); return None
        tienda_id = None if tienda_str == "null" else int(tienda_str)
        # Verificar usuario activo (DB lookup) y tienda activa
        conn = get_db()
        user_row = conn.execute("SELECT activo FROM usuarios WHERE id=?", (int(user_id),)).fetchone()
        if not user_row or not user_row["activo"]:
            conn.close(); self.send_json({"error": "Usuario inactivo"}, 401); return None
        if tienda_id is not None:
            tienda_row = conn.execute("SELECT activo FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
            if not tienda_row or not tienda_row["activo"]:
                conn.close(); self.send_json({"error": "Tienda inactiva"}, 401); return None
        conn.close()
        return AuthContext(int(user_id), tienda_id, rol)
    except Exception:
        self.send_json({"error": "Token inválido"}, 401); return None
```

El lookup de `activo` en cada request es necesario para que la desactivación de un usuario o tienda sea efectiva de inmediato, sin esperar a que expire el token. El costo de una query adicional en Turso es aceptable para la escala de un mini mercado.

### Token expiry y tablets

Los tokens expiran en 12 horas. **Las credenciales no se guardan en localStorage por seguridad.** Cuando el token está próximo a expirar (menos de 60 minutos), se muestra un modal de reingreso de contraseña. El refresh automático silencioso (sin reingreso) está fuera de alcance de esta fase.

**Limitación conocida:** Si el cajero no responde al modal de reingreso, la sesión expira y debe loguearse de nuevo. El turno de caja permanece abierto en el servidor hasta que se cierre explícitamente.

---

## API endpoints

### Públicos (sin token)

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/api/auth/login` | Login con username + password |

**POST /api/auth/login** — body: `{username, password}`

Respuesta 200: ver estructura en sección de tokens arriba.
- 401 si credenciales incorrectas o usuario/tienda inactivos.
- Sin rate limiting implementado (limitación conocida — Railway no provee middleware de rate limiting nativo para Python http.server; se acepta el riesgo para esta fase).

### Protegidos — Requieren token válido

| Método | Ruta | Roles permitidos | Descripción |
|--------|------|-----------------|-------------|
| GET | `/api/auth/me` | todos | Valida token, retorna user+tienda |
| GET | `/api/tiendas` | superadmin | Listar tiendas con conteo de usuarios |
| POST | `/api/tiendas` | superadmin | Crear tienda |
| PUT | `/api/tiendas/:id` | superadmin | Editar tienda (nombre, plan, activo) |
| GET | `/api/tiendas/:id/usuarios` | superadmin | Usuarios de una tienda |
| POST | `/api/tiendas/:id/usuarios` | superadmin | Crear usuario en una tienda (cualquier rol) |
| GET | `/api/usuarios` | admin, superadmin | Usuarios de mi tienda |
| POST | `/api/usuarios` | admin | Crear usuario en mi tienda (rol: admin o cajero) |
| PUT | `/api/usuarios/:id` | admin, superadmin | Editar nombre/activo. Ver restricciones abajo. |
| PUT | `/api/usuarios/:id/password` | admin, superadmin | Cambiar contraseña |

**Restricciones en `POST /api/usuarios` (admin de tienda):**
- El servidor rechaza `rol = 'superadmin'` en el body si `ctx.rol != 'superadmin'` → 403.
- El servidor sólo inserta usuarios con `tienda_id = ctx.tienda_id` (ignora cualquier `tienda_id` en el body).

**Restricciones en `PUT /api/usuarios/:id`:**
- El servidor rechaza cualquier intento de cambiar `rol` a `superadmin` si `ctx.rol != 'superadmin'` → 403.
- El servidor rechaza modificar un usuario que pertenece a una tienda diferente a `ctx.tienda_id` (excepto superadmin) → 403.
- Un usuario no puede cambiar su propio `rol` vía este endpoint (previene auto-escalación) → 403 si `body.id == ctx.user_id && 'rol' in body`.

**`GET /api/tiendas`** retorna:
```json
[{"id":1,"nombre":"...","slug":"...","plan":"...","activo":1,"created_at":"...","num_usuarios":3}]
```
El conteo de usuarios se obtiene con `SELECT COUNT(*) FROM usuarios WHERE tienda_id=?`.

### Endpoints existentes modificados

Todos los handlers de `productos`, `ventas`, `turnos` ahora llaman `require_auth()` al inicio. Handlers afectados:

| Handler | Cambio |
|---------|--------|
| `_list_productos` | `WHERE tienda_id = ctx.tienda_id` (superadmin: sin filtro) |
| `_create_producto` | `INSERT` incluye `tienda_id = ctx.tienda_id` |
| `_update_producto` | `WHERE id=? AND tienda_id=?` (superadmin: solo `WHERE id=?`) |
| `_delete_producto` | Ídem `_update_producto` |
| `_list_ventas` | `WHERE tienda_id = ctx.tienda_id` |
| `_create_venta` | `INSERT` incluye `tienda_id = ctx.tienda_id` |
| `_get_turno_activo` | `WHERE estado='abierto' AND tienda_id=?` — crítico para multi-tienda. Si `ctx.tienda_id is None` (superadmin), retorna `null` (el superadmin no opera cajas directamente). |
| `_open_turno` | Verifica turno abierto solo dentro de `ctx.tienda_id`; `INSERT` incluye `tienda_id` |
| `_list_turnos` | `WHERE tienda_id = ctx.tienda_id` |
| `_close_turno` | `WHERE id=? AND tienda_id=? AND estado='abierto'` |

**Superadmin exception:** Si `ctx.rol == 'superadmin'`, se omite el filtro `tienda_id` en todos los queries (ve datos de todas las tiendas incluyendo registros legacy `tienda_id=NULL`).

### CORS

`do_OPTIONS` debe incluir `Authorization` en los headers permitidos:

```python
self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
```

---

## Frontend

### localStorage

```javascript
// Al login exitoso:
localStorage.setItem('sfpos_token', data.token);
localStorage.setItem('sfpos_exp', data.exp);        // timestamp Unix en segundos
localStorage.setItem('sfpos_user', JSON.stringify(data.user));
localStorage.setItem('sfpos_tienda', JSON.stringify(data.tienda)); // null si superadmin
```

### Secuencia de arranque

```
1. Leer sfpos_token, sfpos_exp, sfpos_user de localStorage
2. Si no existen → mostrar pantalla de login (NO llamar loadData)
3. Si exp < Date.now()/1000 → limpiar localStorage → mostrar pantalla de login
4. Si token presente y no expirado → GET /api/auth/me (con token)
   a. 200 → restaurar currentUser → llamar loadData() → inicializar app
   b. 401 → limpiar localStorage → mostrar pantalla de login
```

`loadData()` **nunca se llama** antes de que la autenticación esté confirmada.

### `apiFetch` helper

```javascript
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

Todos los `fetch()` existentes se reemplazan por `apiFetch()`.

### Estado global nuevo

```javascript
let currentUser = null;
// { id, nombre, username, rol, tienda_id }  — tienda_id === null para superadmin
```

### Pantalla de login

- `#screen-login` ocupa pantalla completa. `#app-root` tiene `display:none` mientras no hay sesión.
- Branding: nombre **SFPOS** con tagline.
- Campos: `username` + `password` + botón "Ingresar".
- Error: mensaje inline bajo el formulario (no toast). Sin shake para no afectar UX en tablet.
- Éxito: guardar en localStorage → llamar secuencia de arranque desde paso 4a.

### Flujo por rol al entrar

| Rol | Comportamiento |
|-----|---------------|
| `superadmin` | Muestra `#screen-superadmin`. Oculta toda la UI de cobro/inventario/reportes/tabs. |
| `admin` | Modo admin: tabs Inventario, Reportes, Usuarios. Puede acceder también a Cobro para operar la caja cuando no hay cajero disponible. Switcher Cajero/Admin oculto. |
| `cajero` | Directo a Cobro. Sin tabs admin. Switcher oculto. |

### Cambios en UI existente

- **Switcher Cajero/Admin** se oculta (`display:none`) — el modo lo determina el rol.
- **Barra superior**: chip con `currentUser.nombre` + botón "Cerrar sesión" (🚪).
- **`abrirCaja()`**: el campo "Nombre del cajero" en el modal se pre-rellena con `currentUser.nombre` y se hace `readonly`. El nombre no es editable.
- **Logout**: `confirm()` → si caja abierta → toast "Cierra la caja antes de cerrar sesión" y se bloquea el logout. Sin caja abierta → limpiar localStorage → mostrar pantalla de login.

### Aviso de sesión próxima a expirar

Cuando `sfpos_exp - Date.now()/1000 < 3600` (menos de 1 hora):
- Mostrar un banner no bloqueante: "⏰ Tu sesión expira en menos de 1 hora. Guarda cambios."
- Al expirar → `apiFetch` detecta 401 → limpiar localStorage → pantalla de login.

### Panel Superadmin (`#screen-superadmin`)

Dos subvistas renderizadas dinámicamente:

**Lista de tiendas:**
- Tabla: Nombre | Slug | Plan | Usuarios | Estado | Fecha | Acciones
- Botón "Nueva tienda" → modal: nombre, slug, plan
- Click en fila → subvista detalle de tienda

**Detalle de tienda:**
- Editar nombre, plan, activo (PUT /api/tiendas/:id)
- Tabla de usuarios: Nombre | Usuario | Rol | Estado | Acciones
- Botón "Nuevo usuario" → modal: nombre, username, contraseña, rol
- Botón "Desactivar" por usuario

### Panel Usuarios (admin de tienda)

Nueva pestaña "Usuarios" en modo admin:
- Lista de usuarios de la tienda con rol y estado
- Admin puede crear usuarios con rol `admin` o `cajero` (no `superadmin`)
- Admin puede cambiar contraseñas y desactivar usuarios (no su propio rol)

---

## Botón "Actualizar inventario"

En la barra de herramientas del inventario, junto a la búsqueda:

```html
<button onclick="recargarInventario()" title="Actualizar">🔄</button>
```

```javascript
async function recargarInventario() {
  const res = await apiFetch('/api/productos');
  if (!res) return;
  products = await res.json();
  renderInventario();
  showToast('✅ Inventario actualizado');
}
```

---

## Seguridad — resumen y limitaciones conocidas

| Aspecto | Decisión |
|---------|----------|
| Hashing de contraseñas | PBKDF2-HMAC-SHA256, 260.000 iteraciones, sal aleatoria 16 bytes |
| Tokens | HMAC-SHA256 firmados, 12h de vida, stateless |
| `SECRET_KEY` | Env var requerida; servidor no arranca sin ella |
| Token payload visible | Por diseño (similar a JWT): contiene `user_id`, `tienda_id`, `rol`, `exp`. Aceptable. |
| Escalación de rol | Bloqueada en servidor para `PUT /api/usuarios/:id` |
| Usuario inactivo | Verificado en cada request (DB lookup) |
| Tienda inactiva | Verificado en cada request (DB lookup) |
| Brute-force en login | Sin rate limiting (limitación conocida para esta fase) |
| Credenciales en localStorage | Solo el token (no la contraseña) |
| Contraseña inicial | Generada aleatoriamente al seed, impresa en logs, nunca en código fuente |

---

## Fuera de alcance (esta fase)

- Impersonación de tienda por superadmin
- Recuperación de contraseña vía email
- 2FA / MFA
- Registro self-service de tiendas
- Métricas globales de plataforma en el panel superadmin
- Rate limiting en `/api/auth/login`
- Refresh automático de tokens sin reingreso de contraseña
