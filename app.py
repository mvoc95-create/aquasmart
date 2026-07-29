from __future__ import annotations

import hmac
import os
import secrets
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from services.auth_service import ROLE_ADMIN, ROLE_TECHNICIAN, UserService
from services.color_classifier import active_profile, build_profile_from_samples, classify_by_color
from services.image_processor import CropConfig, process_rack
from services.models import HemolysisClass
from services.repository import Repository

load_dotenv()


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-change-me"),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "12")) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    )
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    repository = Repository(app.instance_path)
    user_service = UserService(app.instance_path)

    def auth_required() -> bool:
        return os.getenv("AUTH_REQUIRED", "false").lower() == "true"

    def setup_pending() -> bool:
        return auth_required() and not user_service.has_users()

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def validate_csrf() -> None:
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="Token CSRF inválido")

    def refresh_session_user() -> dict | None:
        current = session.get("user")
        if not current:
            return None
        profile = user_service.get_user(str(current.get("id", "")))
        if not profile or not profile.get("active"):
            session.clear()
            return None
        session["user"] = profile
        return profile

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if auth_required():
                if setup_pending():
                    return redirect(url_for("setup_admin"))
                if not refresh_session_user():
                    return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if auth_required():
                if setup_pending():
                    return redirect(url_for("setup_admin"))
                user = refresh_session_user()
                if not user:
                    return redirect(url_for("login", next=request.path))
                if user.get("role") != ROLE_ADMIN:
                    abort(403)
            return view(*args, **kwargs)

        return wrapped

    @app.context_processor
    def inject_globals():
        return {
            "auth_required": auth_required(),
            "current_user": session.get("user"),
            "repository_backend": repository.backend_name,
            "user_backend": user_service.backend_name,
            "hemolysis_classes": [item.value for item in HemolysisClass],
            "role_admin": ROLE_ADMIN,
            "role_technician": ROLE_TECHNICIAN,
            "csrf_token": csrf_token,
        }

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "repository": repository.backend_name,
            "users": user_service.backend_name,
            "setup_pending": setup_pending(),
        }

    @app.route("/setup", methods=["GET", "POST"])
    def setup_admin():
        if not auth_required():
            flash("Ative AUTH_REQUIRED para utilizar o cadastro de usuários.", "error")
            return redirect(url_for("index"))
        if user_service.has_users():
            return redirect(url_for("login"))

        if request.method == "POST":
            validate_csrf()
            configured_key = os.getenv("INITIAL_SETUP_KEY", "")
            supplied_key = request.form.get("setup_key", "")
            if not configured_key:
                flash("INITIAL_SETUP_KEY não foi configurada no Render.", "error")
                return render_template("setup.html")
            if not hmac.compare_digest(configured_key, supplied_key):
                flash("Chave de configuração inicial inválida.", "error")
                return render_template("setup.html")

            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            if password != confirmation:
                flash("As senhas não coincidem.", "error")
                return render_template("setup.html")
            try:
                profile = user_service.create_user(
                    email=request.form.get("email", ""),
                    password=password,
                    full_name=request.form.get("full_name", ""),
                    role=ROLE_ADMIN,
                    created_by=None,
                )
                session["user"] = profile
                flash("Administrador inicial criado. O cadastro de usuários agora é feito no sistema.", "success")
                return redirect(url_for("index"))
            except Exception as exc:
                app.logger.exception("Erro ao criar administrador inicial")
                flash(str(exc), "error")

        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if setup_pending():
            return redirect(url_for("setup_admin"))
        if request.method == "POST":
            validate_csrf()
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            try:
                session["user"] = user_service.authenticate(email, password)
                flash("Login realizado.", "success")
                return redirect(request.args.get("next") or url_for("index"))
            except Exception as exc:
                flash(str(exc), "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        config = CropConfig.from_env()
        return render_template(
            "index.html",
            tube_count=config.tube_count,
            guide_defaults={
                "left": f"{config.rack_left:.6f}",
                "right": f"{config.rack_right:.6f}",
                "top": f"{config.rack_top:.6f}",
                "bottom": f"{config.rack_bottom:.6f}",
            },
        )

    @app.post("/analyze")
    @login_required
    def analyze():
        validate_csrf()
        uploaded = request.files.get("rack_photo")
        if not uploaded or not uploaded.filename:
            flash("Selecione uma foto da rack.", "error")
            return redirect(url_for("index"))
        if uploaded.mimetype and not uploaded.mimetype.startswith("image/"):
            flash("Envie um arquivo de imagem.", "error")
            return redirect(url_for("index"))

        try:
            raw = uploaded.read()
            config = CropConfig.from_form(request.form, base=CropConfig.from_env())
            processed = process_rack(raw, config)
            profile = repository.get_active_calibration_profile()
            analysis, model = classify_by_color(
                color_metrics=processed.tube_colors,
                tube_count=config.tube_count,
                profile=profile,
            )
            user = session.get("user") or {}
            analysis_id = repository.save_analysis(
                analysis=analysis,
                model=model,
                montage_bytes=processed.montage_bytes,
                preview_bytes=processed.preview_bytes,
                user_id=user.get("id"),
                color_metrics=processed.tube_colors,
            )
            return redirect(url_for("analysis_detail", analysis_id=analysis_id))
        except Exception as exc:
            app.logger.exception("Erro ao analisar rack")
            flash(f"Não foi possível analisar a imagem: {exc}", "error")
            return redirect(url_for("index"))

    @app.get("/analysis/<analysis_id>")
    @login_required
    def analysis_detail(analysis_id: str):
        analysis = repository.get_analysis(analysis_id)
        if not analysis:
            return "Análise não encontrada", 404
        return render_template("result.html", analysis=analysis)

    @app.get("/analysis/<analysis_id>/image/<kind>.jpg")
    @login_required
    def analysis_image(analysis_id: str, kind: str):
        raw = repository.get_image(analysis_id, kind)
        if raw is None:
            return "Imagem não encontrada", 404
        return Response(raw, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})

    @app.post("/analysis/<analysis_id>/confirm")
    @login_required
    def confirm_analysis(analysis_id: str):
        validate_csrf()
        analysis = repository.get_analysis(analysis_id)
        if not analysis:
            return "Análise não encontrada", 404

        allowed = {item.value for item in HemolysisClass}
        classifications: dict[int, str] = {}
        for tube in analysis["tubes"]:
            position = int(tube["position"])
            value = request.form.get(f"tube_{position}", "")
            if value not in allowed:
                flash(f"Classificação inválida no tubo {position}.", "error")
                return redirect(url_for("analysis_detail", analysis_id=analysis_id))
            classifications[position] = value

        user = session.get("user") or {}
        include_calibration = request.form.get("include_calibration") == "on"
        repository.confirm_analysis(
            analysis_id,
            classifications,
            user.get("id"),
            calibration_eligible=include_calibration,
        )
        flash("Classificações confirmadas e registradas na auditoria.", "success")
        return redirect(url_for("analysis_detail", analysis_id=analysis_id))

    @app.get("/calibration")
    @admin_required
    def calibration():
        profile = active_profile(repository.get_active_calibration_profile())
        counts = repository.calibration_counts()
        minimum = int(os.getenv("MIN_CALIBRATION_SAMPLES", "5"))
        return render_template(
            "calibration.html", profile=profile, counts=counts, minimum=minimum
        )

    @app.post("/calibration/recalculate")
    @admin_required
    def recalculate_calibration():
        validate_csrf()
        try:
            current = repository.get_active_calibration_profile()
            profile, counts = build_profile_from_samples(
                repository.get_calibration_samples(), base_profile=current
            )
            user = session.get("user") or {}
            repository.save_calibration_profile(profile, user.get("id"))
            flash(
                "Calibração interna atualizada com "
                + ", ".join(f"{key}: {value}" for key, value in counts.items())
                + ".",
                "success",
            )
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("calibration"))

    @app.get("/history")
    @login_required
    def history():
        return render_template("history.html", analyses=repository.list_analyses())

    @app.get("/users")
    @admin_required
    def users():
        return render_template("users.html", users=user_service.list_users())

    @app.post("/users/create")
    @admin_required
    def create_user():
        validate_csrf()
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        if password != confirmation:
            flash("As senhas não coincidem.", "error")
            return redirect(url_for("users"))
        try:
            current = session.get("user") or {}
            user_service.create_user(
                email=request.form.get("email", ""),
                password=password,
                full_name=request.form.get("full_name", ""),
                role=request.form.get("role", ROLE_TECHNICIAN),
                created_by=current.get("id"),
            )
            flash("Usuário criado com sucesso.", "success")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("users"))

    @app.post("/users/<user_id>/update")
    @admin_required
    def update_user(user_id: str):
        validate_csrf()
        current = session.get("user") or {}
        existing = user_service.get_user(user_id)
        if not existing:
            flash("Usuário não encontrado.", "error")
            return redirect(url_for("users"))

        requested_role = request.form.get("role", ROLE_TECHNICIAN)
        requested_active = request.form.get("active") == "on"
        if user_id == current.get("id") and (
            requested_role != ROLE_ADMIN or not requested_active
        ):
            flash("Você não pode remover seu próprio acesso de administrador.", "error")
            return redirect(url_for("users"))
        if (
            existing.get("role") == ROLE_ADMIN
            and existing.get("active")
            and (requested_role != ROLE_ADMIN or not requested_active)
            and user_service.count_active_admins() <= 1
        ):
            flash("O sistema precisa manter pelo menos um administrador ativo.", "error")
            return redirect(url_for("users"))

        try:
            updated = user_service.update_user(
                user_id,
                full_name=request.form.get("full_name", ""),
                role=requested_role,
                active=requested_active,
            )
            if user_id == current.get("id"):
                session["user"] = updated
            flash("Usuário atualizado.", "success")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("users"))

    @app.post("/users/<user_id>/password")
    @admin_required
    def reset_user_password(user_id: str):
        validate_csrf()
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("password_confirmation", "")
        if new_password != confirmation:
            flash("As senhas não coincidem.", "error")
            return redirect(url_for("users"))
        try:
            user_service.reset_password(user_id, new_password)
            flash("Senha redefinida com sucesso.", "success")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("users"))

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("403.html"), 403

    @app.errorhandler(413)
    def too_large(_error):
        flash("A imagem ultrapassa o limite permitido.", "error")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
