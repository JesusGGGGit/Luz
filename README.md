# Luz — Seguimiento de consumo eléctrico

Aplicación web en Flask para capturar lecturas de kWh, registrar recibos bimestrales y ver estadísticas con gráficos. Lista para desplegar en Render (plan gratuito) usando Postgres.

## Requisitos
- Python 3.10+
- Para producción: Base de datos PostgreSQL y variables `DATABASE_URL`, `SECRET_KEY`.
- Para local: no necesitas Postgres; si no hay `DATABASE_URL`, la app usa SQLite (`luz.db`).

`DATABASE_URL` para Postgres puede usar `postgresql://` o `postgres://` (la app lo adapta a `postgresql+psycopg2://`). Ejemplos:
- `postgresql://user:pass@localhost:5432/luz`
- `postgres://user:pass@host:5432/db` (Render)

## Correr en local (sin Postgres: SQLite automático)
1. Crear y activar un entorno virtual (opcional):
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   ```
2. Instalar dependencias:
   ```cmd
   pip install -r requirements.txt
   ```
3. (Opcional) variables de entorno en local:
   ```cmd
   set SECRET_KEY=dev-secret
   ```
   Si no defines `DATABASE_URL`, la app creará/usará `luz.db` (SQLite) en el directorio del proyecto.
4. Inicializar tablas (automático al arrancar) y crear usuario administrador:
   ```cmd
   flask --app app create-user
   ```
5. Ejecutar con servidor de desarrollo:
   ```cmd
   flask --app app run --port 5000
   ```
   Abrir http://localhost:5000

Si prefieres usar Postgres en local, define `DATABASE_URL` antes de correr:
```cmd
set DATABASE_URL=postgresql://postgres:postgres@localhost:5432/luz
```

## Despliegue en Render (plan gratuito)
Opción 1: Blueprint con `render.yaml` (recomendado)
1. Haz push de este repo a GitHub.
2. En Render, crea un nuevo Blueprint y apunta al repo. Render detectará `render.yaml` y creará:
   - Servicio web Python
   - Base de datos Postgres gratuita
   - Variables de entorno (SECRET_KEY autogenerada y DATABASE_URL desde la DB)
3. Deploy. La app levantará con `gunicorn` y conectará a Postgres.
    - En el primer arranque si no hay usuarios, se crea un admin automáticamente:
       - Usuario: `ADMIN_USERNAME` (por defecto `admin`).
       - Contraseña: si no defines `ADMIN_PASSWORD`, se genera y se imprime en los logs como:
          `[Bootstrap] Creado usuario admin 'admin' con contraseña: ...`
       - Puedes configurar ambos en las Environment Variables del servicio.

Opción 2: Manual
1. Crear servicio Web en Render (Python), plan free.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn app:app --workers=2 --threads=4 --preload --timeout 120`
4. Añadir Postgres addon y configurar `DATABASE_URL` automáticamente.
5. Añadir `SECRET_KEY` en Environment.
6. Opcional: define `ADMIN_USERNAME` y `ADMIN_PASSWORD` para crear un admin fijo en el primer arranque.

## Importar/Exportar
- Lecturas: exporta CSV en /lecturas/exportar.csv e importa en /lecturas/importar (columnas: created_at ISO, kwh, description).
- Recibos: exporta CSV en /recibos/exportar.csv.

## Notas técnicas
- ORM: SQLAlchemy 2.x, sin migraciones (las tablas se crean en runtime). Para cambios de esquema considera Alembic.
- Auth: flask-login con hash de contraseña de Werkzeug.
- UI: Bootstrap 5 y Chart.js.
- Gráficos: datasets renderizados via JSON y Chart.js en cliente.

## Licencia
MIT
