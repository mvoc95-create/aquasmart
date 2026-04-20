from datetime import date, datetime, time, timedelta
import os
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import case, func, inspect, text
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
import io
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fazenda-aqua-smart-demo')
db_url = os.getenv('DATABASE_URL', 'sqlite:///farm_system.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Session security tuned for shared tablets/computers.
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Thresholds
TARGET_NURSERY_DAYS = int(os.getenv('TARGET_NURSERY_DAYS', '20'))
TARGET_OD_MIN = float(os.getenv('TARGET_OD_MIN', '4.5'))
TARGET_PH_MIN = float(os.getenv('TARGET_PH_MIN', '7.5'))
TARGET_PH_MAX = float(os.getenv('TARGET_PH_MAX', '8.5'))
TARGET_TEMP_MIN = float(os.getenv('TARGET_TEMP_MIN', '28'))
TARGET_TEMP_MAX = float(os.getenv('TARGET_TEMP_MAX', '32'))
TARGET_SALINITY_MIN = float(os.getenv('TARGET_SALINITY_MIN', '0'))
TARGET_SALINITY_MAX = float(os.getenv('TARGET_SALINITY_MAX', '40'))

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@fazendaaquasmart.local')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
ADMIN_NAME = os.getenv('ADMIN_NAME', 'Administrador')

ROLE_LABELS = {
    'admin': 'Administrador',
    'gerente': 'Gerente',
    'operador': 'Operador',
    'consulta': 'Consulta',
}

ROLE_PERMISSIONS = {
    'admin': {
        'dashboard', 'units_view', 'lots_manage', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'sales_manage', 'users_manage', 'protocols_manage'
    },
    'gerente': {
        'dashboard', 'units_view', 'lots_manage', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'sales_manage', 'protocols_manage'
    },
    'operador': {
        'dashboard', 'units_view', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'protocols_manage'
    },
    'consulta': {'dashboard', 'units_view', 'protocols_manage'},
}


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Faça login para acessar o sistema.'
login_manager.login_message_category = 'warning'


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True)
    role = db.Column(db.String(20), nullable=False, default='operador')
    password_hash = db.Column(db.String(255), nullable=False)
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    @property
    def is_active(self):
        return self.is_active_user

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    def has_permission(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


class Unit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    area_m2 = db.Column(db.Float, nullable=False)
    phase = db.Column(db.String(30), nullable=False)  # bercario / engorda
    structure_type = db.Column(db.String(30), nullable=False)  # estufa / escavado
    active = db.Column(db.Boolean, default=True, nullable=False)


class Lot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lot_code = db.Column(db.String(50), unique=True, nullable=False)
    phase = db.Column(db.String(30), nullable=False)
    species = db.Column(db.String(50), default='Litopenaeus vannamei')
    start_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    initial_count = db.Column(db.Integer, nullable=False, default=0)
    estimated_weight_g = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='ativo')
    notes = db.Column(db.Text)
    unit = db.relationship('Unit')


class WaterMonitoring(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_date = db.Column(db.Date, nullable=False)
    monitor_time = db.Column(db.Time)
    shift = db.Column(db.String(20), nullable=False)  # manha/tarde/noite
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    temperature_c = db.Column(db.Float)
    dissolved_oxygen = db.Column(db.Float)
    ph = db.Column(db.Float)
    salinity = db.Column(db.Float)
    transparency_cm = db.Column(db.Float)
    ammonia = db.Column(db.Float)
    nitrite = db.Column(db.Float)
    observation = db.Column(db.Text)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')


class DailyManagement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    manage_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    feed_offered_kg = db.Column(db.Float, default=0)
    feed_consumed_kg = db.Column(db.Float, default=0)
    mortality_qty = db.Column(db.Integer, default=0)
    average_weight_g = db.Column(db.Float)
    estimated_biomass_kg = db.Column(db.Float)
    notes = db.Column(db.Text)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')


class ProtocolDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False, default='Geral')
    notes = db.Column(db.Text)
    original_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer, nullable=False, default=0)
    file_data = db.Column(db.LargeBinary, nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_by = db.relationship('User')


class Transfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transfer_date = db.Column(db.Date, nullable=False)
    source_unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    destination_unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    source_lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    destination_lot_code = db.Column(db.String(50), nullable=False)
    transferred_qty = db.Column(db.Integer, nullable=False)
    avg_weight_g = db.Column(db.Float)
    notes = db.Column(db.Text)
    source_unit = db.relationship('Unit', foreign_keys=[source_unit_id])
    destination_unit = db.relationship('Unit', foreign_keys=[destination_unit_id])
    source_lot = db.relationship('Lot')


class FeedInventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_date = db.Column(db.Date, nullable=False)
    feed_name = db.Column(db.String(80), nullable=False)
    movement_type = db.Column(db.String(20), nullable=False)  # entrada / saida
    quantity_kg = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float)
    notes = db.Column(db.Text)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    client_name = db.Column(db.String(120), nullable=False)
    channel = db.Column(db.String(40), nullable=False)
    quantity_kg = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.template_filter('brdate')
def brdate_filter(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime('%d/%m/%Y')


@app.template_filter('phase_label')
def phase_label(value):
    return {'bercario': 'Berçário', 'engorda': 'Engorda'}.get(value, value)


@app.template_filter('shift_label')
def shift_label(value):
    return {'manha': 'Manhã', 'tarde': 'Tarde', 'noite': 'Noite'}.get(value, value)


@app.template_filter('brtime')
def brtime_filter(value):
    if not value:
        return ''
    return value.strftime('%H:%M')


@app.template_filter('status_label')
def status_label(value):
    return {'ativo': 'Ativo', 'encerrado': 'Encerrado'}.get(value, value)


@app.template_filter('money')
def money_filter(value):
    if value is None:
        value = 0
    return f'R$ {value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


@app.context_processor
def inject_layout_context():
    return {
        'today_label': date.today().strftime('%d/%m/%Y'),
        'current_endpoint': request.endpoint,
        'current_user_obj': current_user,
        'role_labels': ROLE_LABELS,
        'can_manage_units': getattr(current_user, 'is_authenticated', False) and getattr(current_user, 'role', None) in {'admin', 'gerente'},
    }


def requires_permission(permission: str):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.has_permission(permission):
                abort(403)
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


def parse_date(value, default=None):
    if not value:
        return default
    return datetime.strptime(value, '%Y-%m-%d').date()


def parse_time(value, default=None):
    if not value:
        return default
    return datetime.strptime(value, '%H:%M').time()





def infer_shift_from_time(monitor_time):
    if not monitor_time:
        return 'manha'
    if monitor_time.hour < 6:
        return 'noite'
    if monitor_time.hour < 12:
        return 'manha'
    if monitor_time.hour < 18:
        return 'tarde'
    return 'noite'


def parse_multi_float_list(values):
    parsed = []
    for value in values:
        value = (value or '').strip()
        if not value:
            parsed.append(None)
            continue
        parsed.append(parse_float(value))
    return parsed


def allowed_protocol_file(filename: str) -> bool:
    ext = (filename.rsplit('.', 1)[-1].lower() if '.' in filename else '')
    return ext in {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'png', 'jpg', 'jpeg', 'webp', 'txt'}


def batch_monitor_slots():
    return ['00:00', '02:00', '04:00', '07:00', '16:00', '18:00']

def parse_float(value, default=None):
    if value is None:
        return default
    value = str(value).strip().replace(',', '.')
    if value == '':
        return default
    return float(value)


def parse_int(value, default=None):
    if value is None:
        return default
    value = str(value).strip()
    if value == '':
        return default
    return int(value)


def combine_monitor_datetime(record):
    return datetime.combine(record.monitor_date, record.monitor_time or time.min)


def suggest_unit_code(name: str) -> str:
    raw = ''.join(ch if ch.isalnum() else '_' for ch in (name or '').upper()).strip('_')
    raw = '_'.join(part for part in raw.split('_') if part)
    return raw[:50] or f'VIVEIRO_{int(datetime.utcnow().timestamp())}'


def user_can_manage_units(user) -> bool:
    return getattr(user, 'is_authenticated', False) and getattr(user, 'role', None) in {'admin', 'gerente'}


def seed_units():
    if Unit.query.count() > 0:
        return
    units = [
        ('BELEM', 'Viveiro Belém', 540, 'engorda', 'escavado'),
        ('NATUBA', 'Viveiro Natuba', 570, 'engorda', 'escavado'),
        ('SANTA_RITA', 'Viveiro Santa Rita', 782, 'engorda', 'escavado'),
        ('SAPE', 'Viveiro Sapé', 810, 'engorda', 'escavado'),
        ('CONDE', 'Viveiro Conde', 580, 'engorda', 'escavado'),
        ('CRUZ_ESPIRITO_SANTO', 'Viveiro Cruz do Espírito Santo', 440, 'engorda', 'escavado'),
        ('LUCENA', 'Viveiro Lucena', 360, 'engorda', 'escavado'),
        ('CAMPINA_GRANDE', 'Viveiro Campina Grande', 840, 'engorda', 'escavado'),
        ('SAO_PAULO_1', 'Estufa São Paulo 1', 710, 'engorda', 'estufa'),
        ('SAO_PAULO_2', 'Estufa São Paulo 2', 710, 'engorda', 'estufa'),
        ('RIO_GRANDE_SUL_1', 'Estufa Rio Grande do Sul 1', 710, 'engorda', 'estufa'),
        ('BERC_SP', 'Berçário São Paulo', 80, 'bercario', 'estufa'),
        ('BERC_RGS', 'Berçário Rio Grande do Sul', 80, 'bercario', 'estufa'),
    ]
    for code, name, area, phase, stype in units:
        db.session.add(Unit(code=code, name=name, area_m2=area, phase=phase, structure_type=stype, active=True))
    db.session.commit()


def seed_admin_user():
    existing = User.query.filter(func.lower(User.username) == ADMIN_EMAIL.lower()).first()
    if existing:
        return
    if User.query.count() > 0:
        return
    admin = User(
        full_name=ADMIN_NAME,
        username=ADMIN_EMAIL,
        email=ADMIN_EMAIL,
        role='admin',
        is_active_user=True,
    )
    admin.set_password(ADMIN_PASSWORD)
    db.session.add(admin)
    db.session.commit()


def run_lightweight_migrations():
    """Creates missing tables and columns without Alembic for easier setup by non-technical users."""
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if 'user' not in tables:
        return
    columns = {col['name'] for col in inspector.get_columns('user')}
    dialect = db.engine.dialect.name

    def add_column_if_missing(name: str, sql_sqlite: str, sql_pg: str | None = None):
        if name in columns:
            return
        sql = sql_sqlite if dialect == 'sqlite' or sql_pg is None else sql_pg
        with db.engine.begin() as conn:
            conn.execute(text(sql))
        columns.add(name)

    add_column_if_missing('email', 'ALTER TABLE user ADD COLUMN email VARCHAR(120)', 'ALTER TABLE "user" ADD COLUMN email VARCHAR(120)')
    add_column_if_missing('last_login_at', 'ALTER TABLE user ADD COLUMN last_login_at DATETIME', 'ALTER TABLE "user" ADD COLUMN last_login_at TIMESTAMP')
    add_column_if_missing('created_at', f"ALTER TABLE user ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE "user" ADD COLUMN created_at TIMESTAMP')
    add_column_if_missing('is_active_user', 'ALTER TABLE user ADD COLUMN is_active_user BOOLEAN DEFAULT 1', 'ALTER TABLE "user" ADD COLUMN is_active_user BOOLEAN DEFAULT TRUE')

    if 'water_monitoring' in tables:
        water_columns = {col['name'] for col in inspector.get_columns('water_monitoring')}
        if 'monitor_time' not in water_columns:
            sql = 'ALTER TABLE water_monitoring ADD COLUMN monitor_time TIME' if dialect == 'sqlite' else 'ALTER TABLE water_monitoring ADD COLUMN monitor_time TIME'
            with db.engine.begin() as conn:
                conn.execute(text(sql))


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()


def active_lot_for_unit(unit_id):
    return Lot.query.filter_by(unit_id=unit_id, status='ativo').order_by(Lot.start_date.desc()).first()


def latest_water(unit_id):
    return WaterMonitoring.query.filter_by(unit_id=unit_id).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.monitor_time.desc(), WaterMonitoring.id.desc()).first()


def latest_mgmt(unit_id):
    return DailyManagement.query.filter_by(unit_id=unit_id).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).first()


def water_status(rec):
    if not rec:
        return 'sem leitura'
    alerts = []
    if rec.dissolved_oxygen is not None and rec.dissolved_oxygen < TARGET_OD_MIN:
        alerts.append('OD baixo')
    if rec.ph is not None and (rec.ph < TARGET_PH_MIN or rec.ph > TARGET_PH_MAX):
        alerts.append('pH fora')
    if rec.temperature_c is not None and (rec.temperature_c < TARGET_TEMP_MIN or rec.temperature_c > TARGET_TEMP_MAX):
        alerts.append('temperatura fora')
    return ' | '.join(alerts) if alerts else 'ok'


def dashboard_data():
    today = date.today()
    units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()

    water_today_unit_ids = {u for (u,) in db.session.query(WaterMonitoring.unit_id).filter(WaterMonitoring.monitor_date == today).distinct().all()}
    mgmt_today_unit_ids = {u for (u,) in db.session.query(DailyManagement.unit_id).filter(DailyManagement.manage_date == today).distinct().all()}

    water_alerts = db.session.query(WaterMonitoring).filter(
        WaterMonitoring.monitor_date == today,
        db.or_(
            WaterMonitoring.dissolved_oxygen < TARGET_OD_MIN,
            WaterMonitoring.ph < TARGET_PH_MIN,
            WaterMonitoring.ph > TARGET_PH_MAX,
            WaterMonitoring.temperature_c < TARGET_TEMP_MIN,
            WaterMonitoring.temperature_c > TARGET_TEMP_MAX,
        )
    ).count()

    nursery_ready = []
    semaforo = []
    for unit in units:
        lot = active_lot_for_unit(unit.id)
        water = latest_water(unit.id)
        mgmt = latest_mgmt(unit.id)
        status = 'verde'
        reasons = []
        if lot:
            if unit.phase == 'bercario':
                days = (today - lot.start_date).days
                if days >= TARGET_NURSERY_DAYS:
                    nursery_ready.append((unit.name, lot.lot_code, days))
                    status = 'amarelo'
                    reasons.append('pronto p/ transferência')
            current_water_status = water_status(water)
            if current_water_status != 'ok':
                status = 'vermelho'
                reasons.append(current_water_status)
            if unit.id not in water_today_unit_ids:
                status = 'amarelo' if status != 'vermelho' else status
                reasons.append('sem água hoje')
            if unit.id not in mgmt_today_unit_ids:
                status = 'amarelo' if status != 'vermelho' else status
                reasons.append('sem manejo hoje')
        else:
            status = 'cinza'
            reasons.append('sem lote')
        semaforo.append({
            'unit': unit,
            'lot': lot,
            'status': status,
            'water': water,
            'mgmt': mgmt,
            'reasons': ', '.join(reasons)
        })

    total_stock = db.session.query(
        func.coalesce(func.sum(case((FeedInventory.movement_type == 'entrada', FeedInventory.quantity_kg), else_=-FeedInventory.quantity_kg)), 0)
    ).scalar() or 0
    avg_daily_feed = db.session.query(func.coalesce(func.avg(DailyManagement.feed_offered_kg), 0)).filter(
        DailyManagement.manage_date >= today - timedelta(days=7)
    ).scalar() or 0
    feed_coverage = round(total_stock / avg_daily_feed, 1) if avg_daily_feed > 0 else None

    return {
        'today': today,
        'units': units,
        'water_pending': sum(1 for s in semaforo if s['lot'] and s['unit'].id not in water_today_unit_ids),
        'management_pending': sum(1 for s in semaforo if s['lot'] and s['unit'].id not in mgmt_today_unit_ids),
        'water_alerts': water_alerts,
        'nursery_ready': nursery_ready,
        'feed_stock_kg': round(total_stock, 1),
        'feed_coverage_days': feed_coverage,
        'semaforo': semaforo,
    }


@app.route('/healthz')
def healthz():
    return {'ok': True}, 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        if not user or not user.check_password(password):
            flash('Usuário ou senha inválidos.', 'danger')
            return render_template('login.html')
        if not user.is_active_user:
            flash('Esse usuário está inativo. Fale com o administrador.', 'warning')
            return render_template('login.html')
        user.last_login_at = datetime.utcnow()
        db.session.commit()
        login_user(user, remember=remember)
        flash(f'Bem-vindo, {user.full_name}.', 'success')
        next_url = request.args.get('next')
        return redirect(next_url or url_for('index'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sessão encerrada com sucesso.', 'success')
    return redirect(url_for('login'))


@app.route('/')
@login_required
@requires_permission('dashboard')
def index():
    return render_template('dashboard.html', data=dashboard_data())


@app.route('/units', methods=['GET', 'POST'])
@login_required
@requires_permission('units_view')
def units_page():
    if request.method == 'POST':
        if not user_can_manage_units(current_user):
            abort(403)
        name = request.form.get('name', '').strip()
        if not name:
            flash('Informe o nome do viveiro/unidade.', 'danger')
            return redirect(url_for('units_page'))
        code = (request.form.get('code') or '').strip().upper() or suggest_unit_code(name)
        if Unit.query.filter(func.lower(Unit.code) == code.lower()).first():
            flash('Já existe uma unidade com esse código.', 'danger')
            return redirect(url_for('units_page'))
        unit = Unit(
            code=code,
            name=name,
            area_m2=float(request.form.get('area_m2') or 0),
            phase=request.form.get('phase') or 'engorda',
            structure_type=request.form.get('structure_type') or 'escavado',
            active=bool(request.form.get('active', '1') == '1')
        )
        db.session.add(unit)
        db.session.commit()
        flash('Unidade cadastrada com sucesso.', 'success')
        return redirect(url_for('units_page'))
    units = Unit.query.order_by(Unit.phase, Unit.name).all()
    return render_template('units.html', units=units)


@app.route('/lots', methods=['GET', 'POST'])
@login_required
@requires_permission('lots_manage')
def lots_page():
    if request.method == 'POST':
        lot = Lot(
            lot_code=request.form['lot_code'],
            phase=request.form['phase'],
            start_date=parse_date(request.form['start_date']),
            unit_id=int(request.form['unit_id']),
            initial_count=int(request.form['initial_count'] or 0),
            estimated_weight_g=float(request.form['estimated_weight_g'] or 0),
            notes=request.form.get('notes')
        )
        db.session.add(lot)
        db.session.commit()
        flash('Lote cadastrado.', 'success')
        return redirect(url_for('lots_page'))
    lots = Lot.query.order_by(Lot.start_date.desc()).all()
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    return render_template('lots.html', lots=lots, units=units)


@app.route('/water', methods=['GET', 'POST'])
@login_required
@requires_permission('water_manage')
def water_page():
    if request.method == 'POST':
        mode = request.form.get('entry_mode', 'single')
        unit_id = int(request.form['unit_id'])
        lot = active_lot_for_unit(unit_id)
        monitor_date = parse_date(request.form.get('monitor_date'), date.today())

        if mode == 'batch':
            slot_times = request.form.getlist('slot_time')
            temperatures = parse_multi_float_list(request.form.getlist('temperature_c'))
            oxygens = parse_multi_float_list(request.form.getlist('dissolved_oxygen'))
            ph_values = parse_multi_float_list(request.form.getlist('ph'))
            salinities = parse_multi_float_list(request.form.getlist('salinity'))
            transparencies = parse_multi_float_list(request.form.getlist('transparency_cm'))
            ammonias = parse_multi_float_list(request.form.getlist('ammonia'))
            nitrites = parse_multi_float_list(request.form.getlist('nitrite'))
            observations = request.form.getlist('observation')

            created = 0
            for idx, slot in enumerate(slot_times):
                values = [temperatures[idx], oxygens[idx], ph_values[idx], salinities[idx], transparencies[idx], ammonias[idx], nitrites[idx], (observations[idx] or '').strip()]
                has_data = any(v not in (None, '') for v in values)
                if not has_data:
                    continue
                monitor_time = parse_time(slot)
                rec = WaterMonitoring(
                    monitor_date=monitor_date,
                    shift=infer_shift_from_time(monitor_time),
                    monitor_time=monitor_time,
                    unit_id=unit_id,
                    lot_id=lot.id if lot else None,
                    temperature_c=temperatures[idx],
                    dissolved_oxygen=oxygens[idx],
                    ph=ph_values[idx],
                    salinity=salinities[idx],
                    transparency_cm=transparencies[idx],
                    ammonia=ammonias[idx],
                    nitrite=nitrites[idx],
                    observation=(observations[idx] or '').strip() or None,
                )
                db.session.add(rec)
                created += 1

            if created == 0:
                flash('Preencha pelo menos um horário no lançamento em lote.', 'warning')
                return redirect(url_for('water_page', unit_id=unit_id))

            db.session.commit()
            flash(f'{created} leituras de água salvas em lote.', 'success')
            return redirect(url_for('water_page', unit_id=unit_id))

        rec = WaterMonitoring(
            monitor_date=monitor_date,
            shift=request.form['shift'],
            monitor_time=parse_time(request.form.get('monitor_time')),
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            temperature_c=parse_float(request.form.get('temperature_c')),
            dissolved_oxygen=parse_float(request.form.get('dissolved_oxygen')),
            ph=parse_float(request.form.get('ph')),
            salinity=parse_float(request.form.get('salinity')),
            transparency_cm=parse_float(request.form.get('transparency_cm')),
            ammonia=parse_float(request.form.get('ammonia')),
            nitrite=parse_float(request.form.get('nitrite')),
            observation=request.form.get('observation')
        )
        db.session.add(rec)
        db.session.commit()
        flash('Monitoramento da água lançado.', 'success')
        return redirect(url_for('water_page', unit_id=unit_id))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    selected_unit_id = request.args.get('unit_id', type=int)
    sort_by = request.args.get('sort_by', 'monitor_date')
    sort_dir = 'asc' if request.args.get('sort_dir', 'desc').lower() == 'asc' else 'desc'

    records_query = WaterMonitoring.query.join(Unit)
    if selected_unit_id:
        records_query = records_query.filter(WaterMonitoring.unit_id == selected_unit_id)

    sort_map = {
        'monitor_date': [WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'monitor_time': [WaterMonitoring.monitor_time, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'shift': [WaterMonitoring.shift, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'unit': [func.lower(Unit.name), WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'od': [WaterMonitoring.dissolved_oxygen, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'ph': [WaterMonitoring.ph, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'temperature': [WaterMonitoring.temperature_c, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'salinity': [WaterMonitoring.salinity, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
    }
    order_columns = sort_map.get(sort_by, sort_map['monitor_date'])
    ordered = [col.asc().nullslast() if sort_dir == 'asc' else col.desc().nullslast() for col in order_columns]
    records = records_query.order_by(*ordered).limit(100).all()

    edit_id = request.args.get('edit_id', type=int)
    edit_record = db.session.get(WaterMonitoring, edit_id) if edit_id else None
    selected_unit = db.session.get(Unit, selected_unit_id) if selected_unit_id else None
    return render_template(
        'water.html',
        units=units,
        records=records,
        today=date.today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
        selected_unit=selected_unit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        sort_indicator=sort_indicator,
        build_sort_url=build_sort_url,
        batch_slots=batch_monitor_slots(),
    )


@app.route('/management', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def management_page():
    if request.method == 'POST':
        unit_id = int(request.form['unit_id'])
        lot = active_lot_for_unit(unit_id)
        rec = DailyManagement(
            manage_date=parse_date(request.form['manage_date'], date.today()),
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            feed_offered_kg=parse_float(request.form.get('feed_offered_kg'), 0) or 0,
            feed_consumed_kg=parse_float(request.form.get('feed_consumed_kg'), 0) or 0,
            mortality_qty=parse_int(request.form.get('mortality_qty'), 0) or 0,
            average_weight_g=parse_float(request.form.get('average_weight_g')),
            estimated_biomass_kg=parse_float(request.form.get('estimated_biomass_kg')),
            notes=request.form.get('notes')
        )
        db.session.add(rec)
        db.session.commit()
        flash('Manejo diário lançado.', 'success')
        return redirect(url_for('management_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    records = DailyManagement.query.order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).limit(50).all()
    edit_id = request.args.get('edit_id', type=int)
    edit_record = db.session.get(DailyManagement, edit_id) if edit_id else None
    return render_template('management.html', units=units, records=records, today=date.today(), edit_record=edit_record)


@app.get('/management/previous-data')
@login_required
@requires_permission('management_manage')
def previous_management_data():
    unit_id = request.args.get('unit_id', type=int)
    manage_date = parse_date(request.args.get('manage_date'), date.today())
    if not unit_id:
        return jsonify({'ok': False, 'message': 'Selecione um viveiro antes de copiar.'}), 400

    previous_record = DailyManagement.query.filter(
        DailyManagement.unit_id == unit_id,
        DailyManagement.manage_date < manage_date
    ).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).first()

    if not previous_record:
        return jsonify({'ok': False, 'message': 'Não encontrei um manejo anterior para esse viveiro.'}), 404

    return jsonify({
        'ok': True,
        'record': {
            'manage_date': previous_record.manage_date.isoformat(),
            'feed_offered_kg': previous_record.feed_offered_kg,
            'feed_consumed_kg': previous_record.feed_consumed_kg,
            'mortality_qty': previous_record.mortality_qty,
            'average_weight_g': previous_record.average_weight_g,
            'estimated_biomass_kg': previous_record.estimated_biomass_kg,
            'notes': previous_record.notes or '',
        },
        'message': f"Dados de {previous_record.manage_date.strftime('%d/%m/%Y')} carregados para conferência."
    })


@app.post('/water/<int:record_id>/edit')
@login_required
@requires_permission('water_manage')
def edit_water_record(record_id):
    rec = db.session.get(WaterMonitoring, record_id)
    if not rec:
        flash('Registro de água não encontrado.', 'warning')
        return redirect(url_for('water_page'))
    unit_id = int(request.form['unit_id'])
    lot = active_lot_for_unit(unit_id)
    rec.monitor_date = parse_date(request.form['monitor_date'], rec.monitor_date)
    rec.shift = request.form['shift']
    rec.monitor_time = parse_time(request.form.get('monitor_time'))
    rec.unit_id = unit_id
    rec.lot_id = lot.id if lot else None
    rec.temperature_c = parse_float(request.form.get('temperature_c'))
    rec.dissolved_oxygen = parse_float(request.form.get('dissolved_oxygen'))
    rec.ph = parse_float(request.form.get('ph'))
    rec.salinity = parse_float(request.form.get('salinity'))
    rec.transparency_cm = parse_float(request.form.get('transparency_cm'))
    rec.ammonia = parse_float(request.form.get('ammonia'))
    rec.nitrite = parse_float(request.form.get('nitrite'))
    rec.observation = request.form.get('observation')
    db.session.commit()
    flash('Registro de água atualizado.', 'success')
    return redirect(request.referrer or url_for('water_page'))


@app.post('/management/<int:record_id>/edit')
@login_required
@requires_permission('management_manage')
def edit_management_record(record_id):
    rec = db.session.get(DailyManagement, record_id)
    if not rec:
        flash('Registro de manejo não encontrado.', 'warning')
        return redirect(url_for('management_page'))
    unit_id = int(request.form['unit_id'])
    lot = active_lot_for_unit(unit_id)
    rec.manage_date = parse_date(request.form['manage_date'], rec.manage_date)
    rec.unit_id = unit_id
    rec.lot_id = lot.id if lot else None
    rec.feed_offered_kg = parse_float(request.form.get('feed_offered_kg'), 0) or 0
    rec.feed_consumed_kg = parse_float(request.form.get('feed_consumed_kg'), 0) or 0
    rec.mortality_qty = parse_int(request.form.get('mortality_qty'), 0) or 0
    rec.average_weight_g = parse_float(request.form.get('average_weight_g'))
    rec.estimated_biomass_kg = parse_float(request.form.get('estimated_biomass_kg'))
    rec.notes = request.form.get('notes')
    db.session.commit()
    flash('Registro de manejo atualizado.', 'success')
    return redirect(request.referrer or url_for('management_page'))


@app.post('/water/<int:record_id>/delete')
@login_required
@requires_permission('water_manage')
def delete_water_record(record_id):
    rec = db.session.get(WaterMonitoring, record_id)
    if not rec:
        flash('Registro de água não encontrado.', 'warning')
        return redirect(url_for('water_page'))
    db.session.delete(rec)
    db.session.commit()
    flash('Registro de água excluído.', 'success')
    return redirect(request.referrer or url_for('water_page'))


@app.post('/management/<int:record_id>/delete')
@login_required
@requires_permission('management_manage')
def delete_management_record(record_id):
    rec = db.session.get(DailyManagement, record_id)
    if not rec:
        flash('Registro de manejo não encontrado.', 'warning')
        return redirect(url_for('management_page'))
    db.session.delete(rec)
    db.session.commit()
    flash('Registro de manejo excluído.', 'success')
    return redirect(request.referrer or url_for('management_page'))


def sort_indicator(column: str, sort_by: str, sort_dir: str) -> str:
    if column != sort_by:
        return '↕'
    return '↑' if sort_dir == 'asc' else '↓'


def next_sort_dir(column: str, sort_by: str, sort_dir: str) -> str:
    if column == sort_by and sort_dir == 'asc':
        return 'desc'
    return 'asc'


def build_sort_url(base_endpoint: str, current_args, column: str):
    args = current_args.to_dict(flat=True)
    current_sort_by = args.get('sort_by', 'monitor_date')
    current_sort_dir = 'asc' if args.get('sort_dir', 'desc').lower() == 'asc' else 'desc'
    args['sort_by'] = column
    args['sort_dir'] = next_sort_dir(column, current_sort_by, current_sort_dir)
    return url_for(base_endpoint, **args)


def chart_parameter_options():
    return {
        'od': {'group': 'water', 'field': 'dissolved_oxygen', 'label': 'OD', 'unit': 'mg/L', 'title': 'OD x tempo', 'threshold_key': 'dissolved_oxygen'},
        'salinity': {'group': 'water', 'field': 'salinity', 'label': 'Salinidade', 'unit': '‰', 'title': 'Salinidade x tempo', 'threshold_key': 'salinity'},
        'temperature': {'group': 'water', 'field': 'temperature_c', 'label': 'Temperatura', 'unit': '°C', 'title': 'Temperatura x tempo', 'threshold_key': 'temperature_c'},
        'ph': {'group': 'water', 'field': 'ph', 'label': 'pH', 'unit': '', 'title': 'pH x tempo', 'threshold_key': 'ph'},
        'feed_offered': {'group': 'management', 'field': 'feed_offered_kg', 'label': 'Ração ofertada', 'unit': 'kg', 'title': 'Ração ofertada x tempo', 'threshold_key': None},
        'feed_consumed': {'group': 'management', 'field': 'feed_consumed_kg', 'label': 'Ração consumida', 'unit': 'kg', 'title': 'Ração consumida x tempo', 'threshold_key': None},
        'mortality': {'group': 'management', 'field': 'mortality_qty', 'label': 'Mortalidade', 'unit': 'un', 'title': 'Mortalidade x tempo', 'threshold_key': None},
        'average_weight': {'group': 'management', 'field': 'average_weight_g', 'label': 'Peso médio', 'unit': 'g', 'title': 'Peso médio x tempo', 'threshold_key': None},
    }


def build_chart_thresholds():
    return {
        'dissolved_oxygen': {'label': 'OD mínimo ideal', 'min': TARGET_OD_MIN, 'max': None},
        'ph': {'label': 'Faixa ideal de pH', 'min': TARGET_PH_MIN, 'max': TARGET_PH_MAX},
        'temperature_c': {'label': 'Faixa ideal de temperatura', 'min': TARGET_TEMP_MIN, 'max': TARGET_TEMP_MAX},
        'salinity': {'label': 'Faixa de salinidade alvo', 'min': TARGET_SALINITY_MIN, 'max': TARGET_SALINITY_MAX},
    }


def build_chart_meta():
    return {
        'water': {
            'od': {'label': 'OD', 'unit': 'mg/L'},
            'salinity': {'label': 'Salinidade', 'unit': '‰'},
            'temperature': {'label': 'Temperatura', 'unit': '°C'},
            'ph': {'label': 'pH', 'unit': ''},
        },
        'management': {
            'feed_offered': {'label': 'Ração ofertada', 'unit': 'kg'},
            'feed_consumed': {'label': 'Ração consumida', 'unit': 'kg'},
            'mortality': {'label': 'Mortalidade', 'unit': 'un'},
            'average_weight': {'label': 'Peso médio', 'unit': 'g'},
        }
    }


def serialize_water_series(records, field):
    pts = []
    for r in records:
        value = getattr(r, field)
        if value is None:
            continue
        label = f"{r.monitor_date.strftime('%d/%m/%Y')} {r.monitor_time.strftime('%H:%M') if r.monitor_time else '--:--'}"
        if r.unit:
            label = f"{r.unit.name} · {label}"
        pts.append({
            'label': label,
            'value': value,
            'unit': r.unit.name if r.unit else '',
            'shift': shift_label(r.shift),
            'time': (r.monitor_time.strftime('%H:%M') if r.monitor_time else '--:--'),
            'date': r.monitor_date.strftime('%d/%m/%Y'),
        })
    return pts


def serialize_management_series(records, field):
    pts = []
    for r in records:
        value = getattr(r, field)
        if value is None:
            continue
        label = r.manage_date.strftime('%d/%m/%Y')
        if r.unit:
            label = f"{r.unit.name} · {label}"
        pts.append({
            'label': label,
            'value': value,
            'unit': r.unit.name if r.unit else '',
            'date': r.manage_date.strftime('%d/%m/%Y'),
        })
    return pts


@app.route('/charts')
@login_required
@requires_permission('dashboard')
def charts_page():
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    unit_id = request.args.get('unit_id', type=int)
    days = request.args.get('days', default=30, type=int)
    parameter_options = chart_parameter_options()
    selected_parameter_key = request.args.get('parameter', 'od')
    if selected_parameter_key not in parameter_options:
        selected_parameter_key = 'od'
    if days not in {7, 15, 30, 60, 90}:
        days = 30
    start_date = date.today() - timedelta(days=days - 1)

    water_query = WaterMonitoring.query.join(Unit).filter(WaterMonitoring.monitor_date >= start_date)
    mgmt_query = DailyManagement.query.join(Unit).filter(DailyManagement.manage_date >= start_date)
    if unit_id:
        water_query = water_query.filter(WaterMonitoring.unit_id == unit_id)
        mgmt_query = mgmt_query.filter(DailyManagement.unit_id == unit_id)

    water_records = water_query.order_by(WaterMonitoring.monitor_date.asc(), WaterMonitoring.monitor_time.asc(), WaterMonitoring.id.asc()).all()
    mgmt_records = mgmt_query.order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all()

    selected_unit = db.session.get(Unit, unit_id) if unit_id else None
    selected_parameter = parameter_options[selected_parameter_key]
    if selected_parameter['group'] == 'water':
        points = serialize_water_series(water_records, selected_parameter['field'])
    else:
        points = serialize_management_series(mgmt_records, selected_parameter['field'])

    chart_payload = {
        'points': points,
        'threshold': build_chart_thresholds().get(selected_parameter['threshold_key']) if selected_parameter['threshold_key'] else None,
        'parameter': {
            'key': selected_parameter_key,
            'label': selected_parameter['label'],
            'title': selected_parameter['title'],
            'unit': selected_parameter['unit'],
            'group': selected_parameter['group'],
        },
        'point_count': len(points),
    }

    return render_template(
        'charts.html',
        units=units,
        selected_unit=selected_unit,
        selected_unit_id=unit_id,
        days=days,
        chart_data=chart_payload,
        parameter_options=parameter_options,
        selected_parameter_key=selected_parameter_key,
    )


@app.route('/protocols', methods=['GET', 'POST'])
@login_required
@requires_permission('protocols_manage')
def protocols_page():
    if request.method == 'POST':
        uploaded_file: FileStorage | None = request.files.get('protocol_file')
        title = (request.form.get('title') or '').strip()
        category = (request.form.get('category') or 'Geral').strip() or 'Geral'
        notes = (request.form.get('notes') or '').strip()

        if not uploaded_file or not uploaded_file.filename:
            flash('Selecione um arquivo para enviar.', 'warning')
            return redirect(url_for('protocols_page'))

        safe_name = secure_filename(uploaded_file.filename)
        if not allowed_protocol_file(safe_name):
            flash('Formato não permitido. Use PDF, Office, imagem ou TXT.', 'danger')
            return redirect(url_for('protocols_page'))

        file_bytes = uploaded_file.read()
        if not file_bytes:
            flash('O arquivo enviado está vazio.', 'warning')
            return redirect(url_for('protocols_page'))
        if len(file_bytes) > 15 * 1024 * 1024:
            flash('O arquivo excede 15 MB. Envie uma versão menor.', 'danger')
            return redirect(url_for('protocols_page'))

        title = title or os.path.splitext(safe_name)[0]
        protocol = ProtocolDocument(
            title=title,
            category=category,
            notes=notes or None,
            original_filename=safe_name,
            mime_type=uploaded_file.mimetype or 'application/octet-stream',
            file_size=len(file_bytes),
            file_data=file_bytes,
            uploaded_by_id=getattr(current_user, 'id', None),
        )
        db.session.add(protocol)
        db.session.commit()
        flash('Protocolo salvo com sucesso.', 'success')
        return redirect(url_for('protocols_page'))

    search = (request.args.get('q') or '').strip()
    category_filter = (request.args.get('category') or '').strip()
    protocols_query = ProtocolDocument.query
    if search:
        protocols_query = protocols_query.filter(db.or_(
            ProtocolDocument.title.ilike(f'%{search}%'),
            ProtocolDocument.category.ilike(f'%{search}%'),
            ProtocolDocument.notes.ilike(f'%{search}%'),
            ProtocolDocument.original_filename.ilike(f'%{search}%'),
        ))
    if category_filter:
        protocols_query = protocols_query.filter(ProtocolDocument.category == category_filter)
    protocols = protocols_query.order_by(ProtocolDocument.uploaded_at.desc(), ProtocolDocument.id.desc()).all()
    categories = [row[0] for row in db.session.query(ProtocolDocument.category).distinct().order_by(ProtocolDocument.category.asc()).all() if row[0]]
    return render_template('protocols.html', protocols=protocols, categories=categories, search=search, category_filter=category_filter)


@app.get('/protocols/<int:protocol_id>/download')
@login_required
@requires_permission('protocols_manage')
def download_protocol(protocol_id):
    protocol = db.session.get(ProtocolDocument, protocol_id)
    if not protocol:
        flash('Protocolo não encontrado.', 'warning')
        return redirect(url_for('protocols_page'))

    return send_file(
        io.BytesIO(protocol.file_data),
        mimetype=protocol.mime_type or 'application/octet-stream',
        as_attachment=True,
        download_name=protocol.original_filename,
    )




@app.get('/protocols/<int:protocol_id>/view')
@login_required
@requires_permission('protocols_manage')
def view_protocol(protocol_id):
    protocol = db.session.get(ProtocolDocument, protocol_id)
    if not protocol:
        flash('Protocolo não encontrado.', 'warning')
        return redirect(url_for('protocols_page'))

    return send_file(
        io.BytesIO(protocol.file_data),
        mimetype=protocol.mime_type or 'application/octet-stream',
        as_attachment=False,
        download_name=protocol.original_filename,
    )

@app.post('/protocols/<int:protocol_id>/delete')
@login_required
@requires_permission('protocols_manage')
def delete_protocol(protocol_id):
    protocol = db.session.get(ProtocolDocument, protocol_id)
    if not protocol:
        flash('Protocolo não encontrado.', 'warning')
        return redirect(url_for('protocols_page'))
    db.session.delete(protocol)
    db.session.commit()
    flash('Protocolo removido.', 'success')
    return redirect(url_for('protocols_page'))


@app.route('/transfers', methods=['GET', 'POST'])
@login_required
@requires_permission('transfers_manage')
def transfers_page():
    if request.method == 'POST':
        src_id = int(request.form['source_unit_id'])
        src_lot = active_lot_for_unit(src_id)
        if not src_lot and not request.form.get('source_lot_id'):
            flash('Selecione um lote de origem válido.', 'danger')
            return redirect(url_for('transfers_page'))
        tr = Transfer(
            transfer_date=parse_date(request.form['transfer_date'], date.today()),
            source_unit_id=src_id,
            destination_unit_id=int(request.form['destination_unit_id']),
            source_lot_id=src_lot.id if src_lot else int(request.form['source_lot_id']),
            destination_lot_code=request.form['destination_lot_code'],
            transferred_qty=int(request.form['transferred_qty']),
            avg_weight_g=float(request.form['avg_weight_g']) if request.form.get('avg_weight_g') else None,
            notes=request.form.get('notes')
        )
        db.session.add(tr)
        db.session.commit()
        flash('Transferência registrada.', 'success')
        return redirect(url_for('transfers_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    rows = Transfer.query.order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).limit(50).all()
    return render_template('transfers.html', units=units, lots=lots, rows=rows, today=date.today())


@app.route('/feed', methods=['GET', 'POST'])
@login_required
@requires_permission('feed_manage')
def feed_page():
    if request.method == 'POST':
        row = FeedInventory(
            movement_date=parse_date(request.form['movement_date'], date.today()),
            feed_name=request.form['feed_name'],
            movement_type=request.form['movement_type'],
            quantity_kg=float(request.form['quantity_kg']),
            unit_cost=float(request.form['unit_cost']) if request.form.get('unit_cost') else None,
            notes=request.form.get('notes')
        )
        db.session.add(row)
        db.session.commit()
        flash('Movimentação de ração lançada.', 'success')
        return redirect(url_for('feed_page'))
    rows = FeedInventory.query.order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).limit(50).all()
    total_stock = db.session.query(
        func.coalesce(func.sum(case((FeedInventory.movement_type == 'entrada', FeedInventory.quantity_kg), else_=-FeedInventory.quantity_kg)), 0)
    ).scalar() or 0
    return render_template('feed.html', rows=rows, today=date.today(), total_stock=round(total_stock, 1))


@app.route('/sales', methods=['GET', 'POST'])
@login_required
@requires_permission('sales_manage')
def sales_page():
    if request.method == 'POST':
        sale = Sale(
            sale_date=parse_date(request.form['sale_date'], date.today()),
            unit_id=int(request.form['unit_id']) if request.form.get('unit_id') else None,
            lot_id=int(request.form['lot_id']) if request.form.get('lot_id') else None,
            client_name=request.form['client_name'],
            channel=request.form['channel'],
            quantity_kg=float(request.form['quantity_kg']),
            unit_price=float(request.form['unit_price']),
            notes=request.form.get('notes')
        )
        db.session.add(sale)
        db.session.commit()
        flash('Venda registrada.', 'success')
        return redirect(url_for('sales_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    lots = Lot.query.order_by(Lot.start_date.desc()).all()
    rows = Sale.query.order_by(Sale.sale_date.desc(), Sale.id.desc()).limit(50).all()
    total_revenue = db.session.query(func.coalesce(func.sum(Sale.quantity_kg * Sale.unit_price), 0)).scalar() or 0
    return render_template('sales.html', units=units, lots=lots, rows=rows, today=date.today(), total_revenue=total_revenue)


@app.route('/users', methods=['GET', 'POST'])
@login_required
@requires_permission('users_manage')
def users_page():
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        if form_mode == 'create':
            username = request.form['username'].strip()
            if User.query.filter(func.lower(User.username) == username.lower()).first():
                flash('Já existe um usuário com esse login.', 'danger')
                return redirect(url_for('users_page'))
            password = request.form['password']
            if len(password) < 6:
                flash('A senha precisa ter pelo menos 6 caracteres.', 'danger')
                return redirect(url_for('users_page'))
            user = User(
                full_name=request.form['full_name'].strip(),
                username=username,
                email=request.form.get('email', '').strip() or None,
                role=request.form['role'],
                is_active_user=bool(request.form.get('is_active_user')),
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Usuário criado com sucesso.', 'success')
            return redirect(url_for('users_page'))

        if form_mode == 'update':
            user = db.session.get(User, int(request.form['user_id']))
            if not user:
                flash('Usuário não encontrado.', 'danger')
                return redirect(url_for('users_page'))
            user.full_name = request.form['full_name'].strip()
            user.email = request.form.get('email', '').strip() or None
            user.role = request.form['role']
            user.is_active_user = bool(request.form.get('is_active_user'))
            new_password = request.form.get('password', '').strip()
            if new_password:
                if len(new_password) < 6:
                    flash('A nova senha precisa ter pelo menos 6 caracteres.', 'danger')
                    return redirect(url_for('users_page'))
                user.set_password(new_password)
            db.session.commit()
            flash('Usuário atualizado.', 'success')
            return redirect(url_for('users_page'))

    users = User.query.order_by(User.role, User.full_name).all()
    return render_template('users.html', users=users, role_labels=ROLE_LABELS)


@app.route('/users/<int:user_id>/toggle', methods=['POST'])
@login_required
@requires_permission('users_manage')
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id and user.is_active_user:
        flash('Você não pode desativar sua própria conta enquanto está logado.', 'warning')
        return redirect(url_for('users_page'))
    user.is_active_user = not user.is_active_user
    db.session.commit()
    flash('Status do usuário alterado.', 'success')
    return redirect(url_for('users_page'))


@app.errorhandler(403)
def forbidden(_error):
    return render_template('error.html', title='Acesso negado', message='Seu perfil não tem permissão para acessar essa área.'), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template('error.html', title='Página não encontrada', message='Não encontramos a página solicitada.'), 404


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8000')), debug=True)
else:
    init_db()
