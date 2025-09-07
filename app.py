import os
import secrets
from datetime import datetime, date
from datetime import datetime, date, timedelta
from io import BytesIO

from flask import Flask, flash, redirect, render_template, request, send_file, url_for, jsonify
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import create_engine, select, desc, asc, func
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

from models import Base, User, Reading, Bill
from models import Base, User, Reading, Bill
from zoneinfo import ZoneInfo


def get_database_url() -> str:
    """Return database URL; default to local SQLite when not configured.

    Production (Render): expects DATABASE_URL for Postgres and adapts scheme.
    Local: if DATABASE_URL is missing, use SQLite file 'luz.db' in the project directory.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        # Local development fallback to SQLite
        sqlite_path = os.path.join(os.path.dirname(__file__), "luz.db")
        db_url = f"sqlite:///{sqlite_path}"
        print(f"[DB] Usando SQLite local en {sqlite_path}")
        return db_url
    # Render provides postgres URLs that may start with postgres://. SQLAlchemy needs postgresql+psycopg2://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return db_url


def create_app() -> Flask:
    # Load variables from .env if present
    load_dotenv()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", os.urandom(24))

    db_url = get_database_url()
    # For SQLite in multithreaded servers, allow connections across threads
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)
    Base.metadata.create_all(engine)
    # Bootstrap admin user if none exists (useful for Render where CLI isn't available)
    try:
        with Session(engine) as db:
            users_count = db.scalar(select(func.count()).select_from(User)) or 0
            if users_count == 0:
                username = os.getenv("ADMIN_USERNAME", "admin")
                password = os.getenv("ADMIN_PASSWORD")
                if not password:
                    password = secrets.token_urlsafe(12)
                    print(f"[Bootstrap] Creado usuario admin '{username}' con contraseña: {password}")
                user = User(username=username, password_hash=generate_password_hash(password))
                db.add(user)
                db.commit()
                print("[Bootstrap] Usuario admin creado.")
    except Exception as e:
        print(f"[Bootstrap] No se pudo crear usuario admin: {e}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    login_manager = LoginManager(app)
    login_manager.login_view = "login"

    @login_manager.user_loader
    def load_user(user_id):
        with Session(engine) as db:
            return db.get(User, int(user_id))
    # ----- Reading periods (month/day templates) -----
    # Labels without year for UI; we compute concrete dates per selected year.
    PERIOD_DEFS: list[tuple[tuple[int,int], tuple[int,int], str]] = [
        ((2,22), (4,20), "22 FEB a 20 ABR"),
        ((4,20), (6,21), "20 ABR a 21 JUN"),
        ((6,21), (8,21), "21 JUN a 21 AGO"),
        ((8,21), (10,19), "21 AGO a 19 OCT"),
        ((10,19), (12,18), "19 OCT a 18 DIC"),
        ((12,18), (2,19), "18 DIC a 19 FEB"),
        ((2,19), (4,18), "19 FEB a 18 ABR"),
        ((4,18), (6,18), "18 ABR a 18 JUN"),
        ((6,18), (8,20), "18 JUN a 20 AGO"),
        ((8,20), (10,18), "20 AGO a 18 OCT"),
        ((10,18), (12,18), "18 OCT a 18 DIC"),
    ]

    def build_periods_for_year(y: int) -> list[tuple[str, str]]:
        periods: list[tuple[str, str]] = []
        for (sm, sd), (em, ed), _label in PERIOD_DEFS:
            sy = y
            ey = y
            # if end month is earlier in the year than start month => crosses new year
            if em < sm:
                ey = y + 1
            s = date(sy, sm, sd)
            e = date(ey, em, ed)
            periods.append((s.isoformat() + "|" + e.isoformat(), _label))
        return periods

    def default_period_value_for_today() -> tuple[int, str]:
        # Use local date for Monterrey, NL
        today = datetime.now(ZoneInfo("America/Monterrey")).date()
        y = today.year
        candidates = build_periods_for_year(y) + build_periods_for_year(y-1)
        # find period that contains today
        for val, _lbl in candidates:
            s_str, e_str = val.split("|")
            s = date.fromisoformat(s_str)
            e = date.fromisoformat(e_str)
            if s <= today <= e:
                # normalize to return year matching the start date's year unless it is from previous year
                return (s.year, f"{s.isoformat()}|{e.isoformat()}")
        # fallback to first of current year
        vals = build_periods_for_year(y)
        return (y, vals[0][0])

    def period_for_date(d: date) -> tuple[date, date, str, int]:
        """Return (start_date, end_date, label, start_year) for a given date using PERIOD_DEFS."""
        for y in (d.year, d.year - 1):
            for val, label in build_periods_for_year(y):
                s_str, e_str = val.split("|")
                s = date.fromisoformat(s_str)
                e = date.fromisoformat(e_str)
                if s <= d <= e:
                    return s, e, label, s.year
        # fallback
        s_str, e_str = build_periods_for_year(d.year)[0][0].split("|")
        s = date.fromisoformat(s_str)
        e = date.fromisoformat(e_str)
        # find label
        lbl = next((lbl for val, lbl in build_periods_for_year(d.year) if val == f"{s.isoformat()}|{e.isoformat()}"), "")
        return s, e, lbl, s.year


    # CLI helper to create admin user
    @app.cli.command("create-user")
    def create_user():  # type: ignore
        username = input("Username: ").strip()
        password = input("Password: ").strip()
        with Session(engine) as db:
            if db.scalar(select(User).where(User.username == username)):
                print("User already exists")
                return
            user = User(username=username, password_hash=generate_password_hash(password))
            db.add(user)
            db.commit()
            print("User created")

    @app.route("/")
    @login_required
    def index():
        with Session(engine) as db:
            latest = db.scalars(select(Reading).where(Reading.user_id == current_user.id).order_by(desc(Reading.created_at)).limit(10)).all()
        # reading period options for quick capture
        default_year, selected_val = default_period_value_for_today()
        reading_periods = build_periods_for_year(default_year)
        return render_template(
            "index.html",
            latest=latest,
            reading_periods=reading_periods,
            selected_reading_period=selected_val,
            reading_year=default_year,
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            with Session(engine) as db:
                user = db.scalar(select(User).where(User.username == username))
                if user and check_password_hash(user.password_hash, password):
                    login_user(user)
                    return redirect(url_for("index"))
            flash("Usuario o contraseña inválidos", "danger")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # Readings CRUD
    @app.route("/lecturas", methods=["GET"])  # list
    @login_required
    def readings_list():
        with Session(engine) as db:
            q = select(Reading).where(Reading.user_id == current_user.id).order_by(desc(Reading.created_at))
            readings = db.scalars(q).all()
        return render_template("readings_list.html", readings=readings)

    @app.route("/lecturas/nuevo", methods=["GET", "POST"])  # create
    @login_required
    def readings_create():
        if request.method == "POST":
            try:
                kwh_str = request.form.get("kwh", "").strip()
                if not kwh_str:
                    raise ValueError("kWh es requerido")
                kwh = float(kwh_str)
                if kwh < 0:
                    raise ValueError("kWh no puede ser negativo")
            except ValueError as e:
                flash(f"kWh inválido: {str(e)}", "danger")
                return redirect(url_for("readings_create"))
            description = request.form.get("description", "").strip() or None
            # Require period fields
            period_val = request.form.get("period_option")
            period_year_raw = request.form.get("period_year")
            if not period_val or not period_year_raw:
                flash("Selecciona el periodo y el año", "warning")
                return redirect(url_for("readings_create"))
            # Validate period belongs to selected year (best-effort)
            try:
                py = int(period_year_raw)
                valid_vals = {val for (val, _lbl) in build_periods_for_year(py)}
                if period_val not in valid_vals:
                    # Allow previous year's Dec->Feb as well
                    valid_prev = {val for (val, _lbl) in build_periods_for_year(py-1)}
                    if period_val not in valid_prev:
                        flash("Periodo inválido para el año seleccionado", "danger")
                        return redirect(url_for("readings_create"))
            except Exception:
                flash("Año inválido", "danger")
                return redirect(url_for("readings_create"))
            
            # Calcular la fecha basada en el periodo seleccionado
            try:
                period_start, period_end = _parse_period_value(period_val)
                # Usar una fecha dentro del periodo (por ejemplo, el punto medio)
                import math
                period_duration = (period_end - period_start).days
                mid_point_days = math.floor(period_duration / 2)
                target_date = period_start + timedelta(days=mid_point_days)
                print(f"DEBUG - Setting reading date to {target_date} (mid-point of period)")
            except Exception as e:
                print(f"DEBUG - Error calculating period date: {e}, using current time")
                target_date = _now_mty_naive()
            
            with Session(engine) as db:
                reading = Reading(kwh=kwh, description=description, user_id=current_user.id, created_at=target_date)
                db.add(reading)
                db.commit()
            flash("Lectura guardada", "success")
            return redirect(url_for("readings_list"))
        # GET: provide period options
        default_year, selected_val = default_period_value_for_today()
        reading_periods = build_periods_for_year(default_year)
        return render_template("readings_form.html", reading=None, reading_periods=reading_periods, selected_reading_period=selected_val, reading_year=default_year)

    @app.route("/lecturas/<int:reading_id>/editar", methods=["GET", "POST"])
    @login_required
    def readings_edit(reading_id: int):
        with Session(engine) as db:
            reading = db.get(Reading, reading_id)
            if not reading or reading.user_id != current_user.id:
                flash("Lectura no encontrada", "warning")
                return redirect(url_for("readings_list"))
            if request.method == "POST":
                # Debug: mostrar valores recibidos
                print(f"DEBUG - Form data: {dict(request.form)}")
                print(f"DEBUG - KWH raw: '{request.form.get('kwh', '')}'")
                
                try:
                    kwh_str = request.form.get("kwh", "").strip()
                    print(f"DEBUG - KWH cleaned: '{kwh_str}'")
                    if not kwh_str:
                        raise ValueError("kWh es requerido")
                    new_kwh = float(kwh_str)
                    if new_kwh < 0:
                        raise ValueError("kWh no puede ser negativo")
                    
                    print(f"DEBUG - Updating reading.kwh from {reading.kwh} to {new_kwh}")
                    reading.kwh = new_kwh
                    
                except ValueError as e:
                    flash(f"kWh inválido: {str(e)}", "danger")
                    return redirect(url_for("readings_edit", reading_id=reading_id))
                
                # Procesar periodo y año para actualizar la fecha
                period_val = request.form.get("period_option")
                period_year_raw = request.form.get("period_year")
                if period_val and period_year_raw:
                    try:
                        py = int(period_year_raw)
                        # Validar que el periodo sea válido para el año
                        valid_vals = {val for (val, _lbl) in build_periods_for_year(py)}
                        if period_val in valid_vals:
                            # Usar la fecha de inicio del periodo seleccionado
                            period_start, period_end = _parse_period_value(period_val)
                            # Actualizar la fecha de la lectura al inicio del periodo
                            reading.created_at = period_start
                            print(f"DEBUG - Updated reading.created_at to {period_start}")
                        else:
                            # Verificar año anterior por si es periodo dic-feb
                            valid_prev = {val for (val, _lbl) in build_periods_for_year(py-1)}
                            if period_val in valid_prev:
                                period_start, period_end = _parse_period_value(period_val)
                                reading.created_at = period_start
                                print(f"DEBUG - Updated reading.created_at to {period_start} (previous year)")
                    except Exception as e:
                        print(f"DEBUG - Error updating period: {e}")
                        # No fallar si hay error con el periodo, solo mantener fecha actual
                
                reading.description = request.form.get("description", "").strip() or None
                db.commit()
                flash("Lectura actualizada", "success")
                return redirect(url_for("readings_list"))
        # Preselect period/year based on created_at
        ca = reading.created_at.date() if reading else date.today()
        # Find containing period across year and previous year
        def _find(cad: date) -> tuple[int, str]:
            for y in (cad.year, cad.year-1):
                for val, _lbl in build_periods_for_year(y):
                    s_str, e_str = val.split("|")
                    s = date.fromisoformat(s_str); e = date.fromisoformat(e_str)
                    if s <= cad <= e:
                        return (s.year, val)
            return (cad.year, build_periods_for_year(cad.year)[0][0])
        ry, rsel = _find(ca)
        rperiods = build_periods_for_year(ry)
        return render_template("readings_form.html", reading=reading, reading_periods=rperiods, selected_reading_period=rsel, reading_year=ry)

    @app.route("/lecturas/<int:reading_id>/eliminar", methods=["POST"])  # delete
    @login_required
    def readings_delete(reading_id: int):
        with Session(engine) as db:
            reading = db.get(Reading, reading_id)
            if reading and reading.user_id == current_user.id:
                db.delete(reading)
                db.commit()
                flash("Lectura eliminada", "success")
        return redirect(url_for("readings_list"))

    # CSV import/export for readings
    @app.route("/lecturas/exportar.csv")
    @login_required
    def readings_export_csv():
        import csv, io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["created_at", "kwh", "description", "period_start", "period_end", "period_label", "period_year"])
        with Session(engine) as db:
            data = db.scalars(select(Reading).where(Reading.user_id == current_user.id).order_by(asc(Reading.created_at))).all()
            for r in data:
                ps, pe, lbl, py = period_for_date(r.created_at.date())
                writer.writerow([r.created_at.isoformat(), r.kwh, r.description or "", ps.isoformat(), pe.isoformat(), lbl, py]) 
        return send_file(BytesIO(output.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="lecturas.csv")

    @app.route("/lecturas/importar", methods=["GET", "POST"])
    @login_required
    def readings_import_csv():
        if request.method == "POST":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("Selecciona un archivo CSV", "warning")
                return redirect(request.url)
            import csv, io
            try:
                stream = io.StringIO(file.stream.read().decode("utf-8"))
                reader = csv.DictReader(stream)
                count = 0
                with Session(engine) as db:
                    for row in reader:
                        created_at = (
                            datetime.fromisoformat(row.get("created_at"))
                            if row.get("created_at")
                            else datetime.now(ZoneInfo("America/Monterrey")).replace(tzinfo=None)
                        )
                        kwh = float(row.get("kwh"))
                        description = row.get("description") or None
                        db.add(Reading(created_at=created_at, kwh=kwh, description=description, user_id=current_user.id))
                        count += 1
                    db.commit()
                flash(f"Importadas {count} lecturas", "success")
                return redirect(url_for("readings_list"))
            except Exception as e:
                flash(f"Error al importar: {e}", "danger")
                return redirect(request.url)
        return render_template("readings_import.html")

    # Bills CRUD
    @app.route("/recibos")
    @login_required
    def bills_list():
        with Session(engine) as db:
            bills = db.scalars(
                select(Bill).where((Bill.user_id == current_user.id) | (Bill.user_id.is_(None))).order_by(desc(Bill.period_end))
            ).all()
            # Readings grouped by period (label) and year (start year)
            readings = db.scalars(
                select(Reading).where(Reading.user_id == current_user.id).order_by(asc(Reading.created_at))
            ).all()
        # Map existing user's bills by exact period dates
        user_bills_map = {}
        for b in bills:
            if b.user_id == current_user.id:
                user_bills_map[(b.period_start.date(), b.period_end.date())] = b
        groups: dict[tuple[date, date, str, int], dict] = {}
        for r in readings:
            s, e, lbl, yr = period_for_date(r.created_at.date())
            key = (s, e, lbl, yr)
            g = groups.setdefault(key, {
                "start": s, "end": e, "label": lbl, "year": yr,
                "first": None, "last": None, "count": 0,
            })
            if g["first"] is None:
                g["first"] = r
            g["last"] = g["last"] or r
            g["last"] = r
            g["count"] += 1
        grouped_periods = []
        for (s, e, lbl, yr), g in groups.items():
            first = g["first"]; last = g["last"]
            est = 0.0
            if first and last:
                est = max(0.0, float(last.kwh - first.kwh))
            val = f"{s.isoformat()}|{e.isoformat()}"
            bill = user_bills_map.get((s, e))
            grouped_periods.append({
                "year": yr,
                "label": lbl,
                "start": s,
                "end": e,
                "kwh_est": round(est, 2),
                "count": g["count"],
                "val": val,
                "bill_id": bill.id if bill else None,
            })
        # Sort latest periods first by end date desc
        grouped_periods.sort(key=lambda x: x["end"], reverse=True)
        return render_template("bills_list.html", bills=bills, grouped_periods=grouped_periods)

    # Predefined fixed bill periods (day month year)
    FIXED_BILL_PERIODS = [
        ("2024-10-18|2024-12-18", "18 OCT a 18 DIC"),
        ("2024-08-20|2024-10-18", "20 AGO a 18 OCT"),
        ("2024-06-18|2024-08-20", "18 JUN a 20 AGO"),
        ("2024-04-18|2024-06-18", "18 ABR a 18 JUN"),
        ("2024-02-19|2024-04-18", "19 FEB a 18 ABR"),
        ("2023-12-18|2024-02-19", "18 DIC a 19 FEB"),
        ("2023-10-19|2023-12-18", "19 OCT a 18 DIC"),
        ("2023-08-21|2023-10-19", "21 AGO a 19 OCT"),
        ("2023-06-21|2023-08-21", "21 JUN a 21 AGO"),
        ("2023-04-20|2023-06-21", "20 ABR a 21 JUN"),
        ("2023-02-22|2023-04-20", "22 FEB a 20 ABR"),
    ]

    def _parse_period_value(val: str) -> tuple[datetime, datetime]:
        s, e = val.split("|")
        # Interpret as dates at midnight UTC-like (naive)
        return datetime.fromisoformat(s), datetime.fromisoformat(e)

    @app.get("/recibos/period-info")
    @login_required
    def bill_period_info():
        val = request.args.get("val", "").strip()
        if not val:
            return jsonify({"error": "val requerido"}), 400
        try:
            ps, pe = _parse_period_value(val)
            pe_next = pe + timedelta(days=1)
            with Session(engine) as db:
                rows = db.scalars(
                    select(Reading)
                    .where(
                        (Reading.user_id == current_user.id)
                        & (Reading.created_at >= ps)
                        & (Reading.created_at < pe_next)
                    )
                    .order_by(asc(Reading.created_at))
                ).all()
            est = 0.0
            if len(rows) >= 2:
                est = max(0.0, float(rows[-1].kwh - rows[0].kwh))
            data = {
                "kwh_est": est,
                "readings": [
                    {
                        "created_at": r.created_at.isoformat(timespec="minutes"),
                        "kwh": r.kwh,
                        "description": r.description or "",
                    }
                    for r in rows
                ],
            }
            return jsonify(data)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/recibos/nuevo", methods=["GET", "POST"])
    @login_required
    def bills_create():
        if request.method == "POST":
            try:
                # If checkbox is checked, mode value will be "manual", otherwise it won't be present
                mode = "manual" if request.form.get("mode") == "manual" else "fixed"
                period_start = None
                period_end = None
                
                if mode == "manual":
                    ms = (request.form.get("manual_start") or "").strip()
                    me = (request.form.get("manual_end") or "").strip()
                    if not ms or not me:
                        raise ValueError("Inicio y fin requeridos")
                    # Parse as date-only at midnight
                    period_start = datetime.fromisoformat(ms)
                    period_end = datetime.fromisoformat(me)
                else:
                    period_val = request.form.get("period_option")
                    if not period_val:
                        raise ValueError("Periodo requerido")
                    period_start, period_end = _parse_period_value(period_val)
                
                # Validate amount_total separately
                amount_str = (request.form.get("amount_total") or "").strip()
                if not amount_str:
                    raise ValueError("Monto total requerido")
                amount_total = float(amount_str)
                
            except ValueError as e:
                flash(f"Datos inválidos: {str(e)}", "danger")
                return redirect(url_for("bills_create"))
            except Exception:
                flash("Datos inválidos", "danger")
                return redirect(url_for("bills_create"))
            notes = request.form.get("notes") or None
            with Session(engine) as db:
                bill = Bill(period_start=period_start, period_end=period_end, amount_total=amount_total, notes=notes, user_id=current_user.id)
                db.add(bill)
                db.commit()
            flash("Recibo guardado", "success")
            return redirect(url_for("bills_list"))
        # GET: show readings for the selected period (?period=) or first period by default
        manual_mode = (request.args.get("mode") or "").strip().lower() == "manual"
        period_from_url = request.args.get("period")  # Only lock if period comes from URL
        selected_val = None if manual_mode else (period_from_url or (FIXED_BILL_PERIODS[0][0] if FIXED_BILL_PERIODS else None))
        period_readings = []
        period_kwh_est = 0.0
        if selected_val:
            try:
                ps, pe = _parse_period_value(selected_val)
                pe_next = pe + timedelta(days=1)
                with Session(engine) as db2:
                    period_readings = db2.scalars(
                        select(Reading)
                        .where(
                            (Reading.user_id == current_user.id)
                            & (Reading.created_at >= ps)
                            & (Reading.created_at < pe_next)
                        )
                        .order_by(asc(Reading.created_at))
                    ).all()
                    if len(period_readings) >= 2:
                        period_kwh_est = max(0.0, float(period_readings[-1].kwh - period_readings[0].kwh))
            except Exception:
                pass
        return render_template(
            "bills_form.html",
            bill=None,
            bill_periods=FIXED_BILL_PERIODS,
            selected_period=selected_val,
            manual_mode=manual_mode,
            lock_period=(bool(period_from_url) and not manual_mode),  # Only lock if period came from URL
            period_readings=period_readings,
            period_kwh_est=period_kwh_est,
        )

    @app.route("/recibos/<int:bill_id>/editar", methods=["GET", "POST"])
    @login_required
    def bills_edit(bill_id: int):
        with Session(engine) as db:
            bill = db.get(Bill, bill_id)
            if not bill:
                flash("Recibo no encontrado", "warning")
                return redirect(url_for("bills_list"))
            if request.method == "POST":
                try:
                    # Handle both manual and fixed mode in edit
                    mode = "manual" if request.form.get("mode") == "manual" else "fixed"
                    
                    if mode == "manual":
                        ms = (request.form.get("manual_start") or "").strip()
                        me = (request.form.get("manual_end") or "").strip()
                        if not ms or not me:
                            raise ValueError("Inicio y fin requeridos")
                        bill.period_start = datetime.fromisoformat(ms)
                        bill.period_end = datetime.fromisoformat(me)
                    else:
                        period_val = request.form.get("period_option")
                        if not period_val:
                            raise ValueError("Periodo requerido")
                        ps, pe = _parse_period_value(period_val)
                        bill.period_start = ps
                        bill.period_end = pe
                    
                    # Validate amount_total separately
                    amount_str = (request.form.get("amount_total") or "").strip()
                    if not amount_str:
                        raise ValueError("Monto total requerido")
                    bill.amount_total = float(amount_str)
                    
                except ValueError as e:
                    flash(f"Datos inválidos: {str(e)}", "danger")
                    return redirect(url_for("bills_edit", bill_id=bill_id))
                except Exception:
                    flash("Datos inválidos", "danger")
                    return redirect(url_for("bills_edit", bill_id=bill_id))
                bill.notes = request.form.get("notes") or None
                db.commit()
                flash("Recibo actualizado", "success")
                return redirect(url_for("bills_list"))
        # Preselect matching period if exists
        selected_val = None
        manual_mode = False
        try:
            ps = bill.period_start.date().isoformat()
            pe = bill.period_end.date().isoformat()
            candidate = f"{ps}|{pe}"
            if any(val == candidate for (val, _label) in FIXED_BILL_PERIODS):
                selected_val = candidate
            else:
                # If it doesn't match any fixed period, treat as manual
                manual_mode = True
        except Exception:
            selected_val = None
            manual_mode = True
        # Gather readings inside the bill period for current user
        period_readings = []
        period_kwh_est = 0.0
        try:
            with Session(engine) as db2:
                pe_next = bill.period_end + timedelta(days=1)
                period_readings = db2.scalars(
                    select(Reading)
                    .where(
                        (Reading.user_id == current_user.id)
                        & (Reading.created_at >= bill.period_start)
                        & (Reading.created_at < pe_next)
                    )
                    .order_by(asc(Reading.created_at))
                ).all()
                if len(period_readings) >= 2:
                    period_kwh_est = max(0.0, float(period_readings[-1].kwh - period_readings[0].kwh))
                elif len(period_readings) == 1:
                    period_kwh_est = 0.0
        except Exception:
            period_readings = []
            period_kwh_est = 0.0
        return render_template(
            "bills_form.html",
            bill=bill,
            bill_periods=FIXED_BILL_PERIODS,
            selected_period=selected_val,
            manual_mode=manual_mode,
            lock_period=False,  # Allow changing mode when editing
            period_readings=period_readings,
            period_kwh_est=period_kwh_est,
        )
        

    @app.route("/recibos/<int:bill_id>/eliminar", methods=["POST"])  # delete
    @login_required
    def bills_delete(bill_id: int):
        with Session(engine) as db:
            bill = db.get(Bill, bill_id)
            if bill:
                db.delete(bill)
                db.commit()
                flash("Recibo eliminado", "success")
        return redirect(url_for("bills_list"))

    @app.route("/recibos/exportar.csv")
    @login_required
    def bills_export_csv():
        import csv, io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["period_start", "period_end", "period_label", "amount_total", "notes"]) 
        with Session(engine) as db:
            data = db.scalars(select(Bill).where((Bill.user_id == current_user.id) | (Bill.user_id.is_(None))).order_by(asc(Bill.period_start))).all()
            for b in data:
                # Find label based on PERIOD_DEFS (no year)
                _, _, lbl, _ = period_for_date(b.period_start.date())
                writer.writerow([b.period_start.date().isoformat(), b.period_end.date().isoformat(), lbl, b.amount_total, b.notes or ""]) 
        return send_file(BytesIO(output.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="recibos.csv")

    # Statistics
    @app.route("/estadisticas")
    @login_required
    def stats():
        def calculate_tiered_cost(kwh):
            """Calculate CFE tiered cost with IVA"""
            if kwh <= 0:
                return 0
            cost = 0
            remaining = kwh
            
            # Primeros 300 kWh a $0.816 cada uno
            if remaining > 0:
                tier1 = min(remaining, 300)
                cost += tier1 * 0.816
                remaining -= tier1
            
            # Siguientes 300 kWh (301-600) a $0.944 cada uno
            if remaining > 0:
                tier2 = min(remaining, 300)
                cost += tier2 * 0.944
                remaining -= tier2
            
            # Siguientes 300 kWh (601-900) a $1.219 cada uno
            if remaining > 0:
                tier3 = min(remaining, 300)
                cost += tier3 * 1.219
                remaining -= tier3
            
            # Resto (901+) a $3.248 cada uno
            if remaining > 0:
                cost += remaining * 3.248
            
            return cost * 1.16  # Agregar 16% de IVA
        
        with Session(engine) as db:
            readings = db.scalars(select(Reading).where(Reading.user_id == current_user.id).order_by(asc(Reading.created_at))).all()
            bills = db.scalars(select(Bill).where((Bill.user_id == current_user.id) | (Bill.user_id.is_(None))).order_by(asc(Bill.period_start))).all()
        
        # Convert absolute meter readings to consumption deltas
        delta_labels: list[str] = []
        delta_values: list[float] = []
        if readings:
            prev = readings[0]
            # start with first delta as 0 for display alignment
            delta_labels.append(prev.created_at.strftime("%Y-%m-%d %H:%M"))
            delta_values.append(0.0)
            for r in readings[1:]:
                delta = r.kwh - prev.kwh
                if delta < 0:
                    # meter rollover or reset; ignore negative by treating as 0
                    delta = 0.0
                delta_labels.append(r.created_at.strftime("%Y-%m-%d %H:%M"))
                delta_values.append(round(float(delta), 4))
                prev = r
        
        readings_data = {"labels": delta_labels, "kwh": delta_values}
        bills_data = {
            "labels": [f"{b.period_start.strftime('%Y-%m-%d')} a {b.period_end.strftime('%Y-%m-%d')}" for b in bills],
            "amounts": [b.amount_total for b in bills],
        }
        
        # Compute average cost per kWh per bill period by summing deltas assigned to reading timestamps inside the period
        period_costs = []
        # Map reading timestamp -> delta
        reading_deltas = list(zip(delta_labels, delta_values))
        # Also map timestamp strings back to datetime for comparison
        ts_to_delta = []
        if readings:
            # rebuild with actual datetimes aligned to delta_values (skip first with 0)
            ts_to_delta.append((readings[0].created_at, 0.0))
            for i in range(1, len(readings)):
                dt = readings[i].created_at
                val = max(0.0, float(readings[i].kwh - readings[i-1].kwh))
                ts_to_delta.append((dt, val))
        
        for b in bills:
            pe_next = b.period_end + timedelta(days=1)
            total_kwh = sum(val for (dt, val) in ts_to_delta if (b.period_start <= dt < pe_next))
            avg_cost = (b.amount_total / total_kwh) if total_kwh > 0 else None
            estimated_cfe_cost = calculate_tiered_cost(total_kwh) if total_kwh > 0 else None
            
            period_costs.append({
                "label": f"{b.period_start.strftime('%d %b %y')} - {b.period_end.strftime('%d %b %y')}",
                "total_kwh": round(total_kwh, 4),
                "amount": b.amount_total,
                "avg_cost": avg_cost,
                "estimated_cfe_cost": estimated_cfe_cost,
            })
        
        # Calculate summary statistics
        total_consumption = sum(row["total_kwh"] for row in period_costs)
        total_spending = sum(row["amount"] for row in period_costs)
        
        # Calculate monthly averages (assuming periods are roughly monthly)
        num_periods = len(period_costs) if period_costs else 1
        avg_monthly_consumption = total_consumption / num_periods if num_periods > 0 else 0
        avg_monthly_spending = total_spending / num_periods if num_periods > 0 else 0
        avg_cost_per_kwh = total_spending / total_consumption if total_consumption > 0 else 0
        
        return render_template("stats.html", 
                               readings_data=readings_data, 
                               bills_data=bills_data, 
                               period_costs=period_costs,
                               total_consumption=total_consumption,
                               total_spending=total_spending,
                               avg_monthly_consumption=avg_monthly_consumption,
                               avg_monthly_spending=avg_monthly_spending,
                               avg_cost_per_kwh=avg_cost_per_kwh)

    return app


app = create_app()
