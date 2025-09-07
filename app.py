import os
import secrets
from datetime import datetime
from io import BytesIO

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import create_engine, select, desc, asc, func
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

from models import Base, User, Reading, Bill


def get_database_url() -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set. Please configure it for Postgres.")
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

    engine = create_engine(get_database_url(), pool_pre_ping=True)
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
        return render_template("index.html", latest=latest)

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
                kwh = float(request.form.get("kwh", "0"))
            except ValueError:
                flash("kWh inválido", "danger")
                return redirect(url_for("readings_create"))
            description = request.form.get("description") or None
            with Session(engine) as db:
                reading = Reading(kwh=kwh, description=description, user_id=current_user.id)
                db.add(reading)
                db.commit()
            flash("Lectura guardada", "success")
            return redirect(url_for("readings_list"))
        return render_template("readings_form.html", reading=None)

    @app.route("/lecturas/<int:reading_id>/editar", methods=["GET", "POST"])
    @login_required
    def readings_edit(reading_id: int):
        with Session(engine) as db:
            reading = db.get(Reading, reading_id)
            if not reading or reading.user_id != current_user.id:
                flash("Lectura no encontrada", "warning")
                return redirect(url_for("readings_list"))
            if request.method == "POST":
                try:
                    reading.kwh = float(request.form.get("kwh", "0"))
                except ValueError:
                    flash("kWh inválido", "danger")
                    return redirect(url_for("readings_edit", reading_id=reading_id))
                reading.description = request.form.get("description") or None
                db.commit()
                flash("Lectura actualizada", "success")
                return redirect(url_for("readings_list"))
        return render_template("readings_form.html", reading=reading)

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
        writer.writerow(["created_at", "kwh", "description"])
        with Session(engine) as db:
            data = db.scalars(select(Reading).where(Reading.user_id == current_user.id).order_by(asc(Reading.created_at))).all()
            for r in data:
                writer.writerow([r.created_at.isoformat(), r.kwh, r.description or ""]) 
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
                        created_at = datetime.fromisoformat(row.get("created_at")) if row.get("created_at") else datetime.utcnow()
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
        return render_template("bills_list.html", bills=bills)

    @app.route("/recibos/nuevo", methods=["GET", "POST"])
    @login_required
    def bills_create():
        if request.method == "POST":
            try:
                period_start = datetime.fromisoformat(request.form.get("period_start"))
                period_end = datetime.fromisoformat(request.form.get("period_end"))
                amount_total = float(request.form.get("amount_total"))
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
        return render_template("bills_form.html", bill=None)

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
                    bill.period_start = datetime.fromisoformat(request.form.get("period_start"))
                    bill.period_end = datetime.fromisoformat(request.form.get("period_end"))
                    bill.amount_total = float(request.form.get("amount_total"))
                except Exception:
                    flash("Datos inválidos", "danger")
                    return redirect(url_for("bills_edit", bill_id=bill_id))
                bill.notes = request.form.get("notes") or None
                db.commit()
                flash("Recibo actualizado", "success")
                return redirect(url_for("bills_list"))
        return render_template("bills_form.html", bill=bill)

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
        writer.writerow(["period_start", "period_end", "amount_total", "notes"]) 
        with Session(engine) as db:
            data = db.scalars(select(Bill).where((Bill.user_id == current_user.id) | (Bill.user_id.is_(None))).order_by(asc(Bill.period_start))).all()
            for b in data:
                writer.writerow([b.period_start.date().isoformat(), b.period_end.date().isoformat(), b.amount_total, b.notes or ""]) 
        return send_file(BytesIO(output.getvalue().encode("utf-8")), mimetype="text/csv", as_attachment=True, download_name="recibos.csv")

    # Statistics
    @app.route("/estadisticas")
    @login_required
    def stats():
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
            total_kwh = sum(val for (dt, val) in ts_to_delta if b.period_start <= dt <= b.period_end)
            avg_cost = (b.amount_total / total_kwh) if total_kwh > 0 else None
            period_costs.append({
                "label": f"{b.period_start.strftime('%d %b %y')} - {b.period_end.strftime('%d %b %y')}",
                "total_kwh": round(total_kwh, 4),
                "amount": b.amount_total,
                "avg_cost": avg_cost,
            })
        return render_template("stats.html", readings_data=readings_data, bills_data=bills_data, period_costs=period_costs)

    return app


app = create_app()
