from datetime import date, datetime, timedelta
import os
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, url_for
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
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fazenda-mirim-demo')
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

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@fazendamirim.local')
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
        'transfers_manage', 'feed_manage', 'sales_manage', 'users_manage'
    },
    'gerente': {
        'dashboard', 'units_view', 'lots_manage', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'sales_manage'
    },
    'operador': {
        'dashboard', 'units_view', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage'
    },
    'consulta': {'dashboard', 'units_view'},
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


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()


def active_lot_for_unit(unit_id):
    return Lot.query.filter_by(unit_id=unit_id, status='ativo').order_by(Lot.start_date.desc()).first()


def latest_water(unit_id):
    return WaterMonitoring.query.filter_by(unit_id=unit_id).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.id.desc()).first()


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


@app.route('/units')
@login_required
@requires_permission('units_view')
def units_page():
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
        unit_id = int(request.form['unit_id'])
        lot = active_lot_for_unit(unit_id)
        rec = WaterMonitoring(
            monitor_date=parse_date(request.form['monitor_date'], date.today()),
            shift=request.form['shift'],
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            temperature_c=float(request.form['temperature_c']) if request.form.get('temperature_c') else None,
            dissolved_oxygen=float(request.form['dissolved_oxygen']) if request.form.get('dissolved_oxygen') else None,
            ph=float(request.form['ph']) if request.form.get('ph') else None,
            salinity=float(request.form['salinity']) if request.form.get('salinity') else None,
            transparency_cm=float(request.form['transparency_cm']) if request.form.get('transparency_cm') else None,
            ammonia=float(request.form['ammonia']) if request.form.get('ammonia') else None,
            nitrite=float(request.form['nitrite']) if request.form.get('nitrite') else None,
            observation=request.form.get('observation')
        )
        db.session.add(rec)
        db.session.commit()
        flash('Monitoramento da água lançado.', 'success')
        return redirect(url_for('water_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    records = WaterMonitoring.query.order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.id.desc()).limit(50).all()
    return render_template('water.html', units=units, records=records, today=date.today())


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
            feed_offered_kg=float(request.form['feed_offered_kg'] or 0),
            feed_consumed_kg=float(request.form['feed_consumed_kg'] or 0),
            mortality_qty=int(request.form['mortality_qty'] or 0),
            average_weight_g=float(request.form['average_weight_g']) if request.form.get('average_weight_g') else None,
            estimated_biomass_kg=float(request.form['estimated_biomass_kg']) if request.form.get('estimated_biomass_kg') else None,
            notes=request.form.get('notes')
        )
        db.session.add(rec)
        db.session.commit()
        flash('Manejo diário lançado.', 'success')
        return redirect(url_for('management_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    records = DailyManagement.query.order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).limit(50).all()
    return render_template('management.html', units=units, records=records, today=date.today())


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
