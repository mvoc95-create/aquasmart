from datetime import date, datetime, time, timedelta
import base64
import json
import os
import re
import unicodedata
from collections import defaultdict
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import case, func, inspect, text, or_
from sqlalchemy.orm import joinedload
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
import io
from openpyxl import Workbook
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:
    A4 = None
    canvas = None
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

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
TARGET_AMMONIA_MAX = float(os.getenv('TARGET_AMMONIA_MAX', '0.5'))
TARGET_NITRITE_MAX = float(os.getenv('TARGET_NITRITE_MAX', '1.0'))
TARGET_HARVEST_WEIGHT_G = float(os.getenv('TARGET_HARVEST_WEIGHT_G', '15'))


# Curva zootécnica padrão usada como base inicial do modelo adaptativo.
# Fonte operacional: tabela padrão enviada pelo usuário (engorda 1,5 g -> 16 g em 70 dias,
# sobrevivência de 100% -> 80% e arraçoamento decrescente de 5,0% -> 2,4% da biomassa).
STANDARD_GROWOUT_START_WEIGHT_G = 1.5
STANDARD_GROWOUT_FINAL_WEIGHT_G = 16.0
STANDARD_GROWOUT_DAYS = 70
STANDARD_GROWOUT_INITIAL_SURVIVAL_PCT = 100.0
STANDARD_GROWOUT_FINAL_SURVIVAL_PCT = 80.0
STANDARD_GROWOUT_FINAL_FCR = 1.42
STANDARD_GROWOUT_FEED_RATE_PCTS = [
    5.0, 5.0, 5.0,
    4.8, 4.8, 4.8, 4.8, 4.8,
    4.7, 4.7, 4.7, 4.7, 4.7,
    4.5, 4.5, 4.5, 4.5,
    4.3, 4.3, 4.3, 4.3, 4.3,
    4.1, 4.1, 4.1, 4.1, 4.1,
    3.9, 3.9, 3.9, 3.9, 3.9,
    3.7, 3.7, 3.7, 3.7, 3.7,
    3.5, 3.5, 3.5, 3.5, 3.5,
    3.4, 3.4, 3.4, 3.4,
    3.2, 3.2, 3.2, 3.2, 3.2,
    3.1, 3.1, 3.1, 3.1, 3.1,
    3.0, 3.0, 3.0, 3.0, 3.0,
    2.8, 2.8, 2.8, 2.8, 2.8,
    2.6, 2.6, 2.6,
    2.4,
]


def optional_env_float(name: str, default=None):
    value = os.getenv(name)
    if value in (None, ''):
        return default
    return float(value)


TARGET_TRANSPARENCY_MIN = optional_env_float('TARGET_TRANSPARENCY_MIN')
TARGET_TRANSPARENCY_MAX = optional_env_float('TARGET_TRANSPARENCY_MAX')

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
        'transfers_manage', 'feed_manage', 'sales_manage', 'users_manage', 'protocols_manage', 'farm_documents_manage'
    },
    'gerente': {
        'dashboard', 'units_view', 'lots_manage', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'sales_manage', 'protocols_manage', 'farm_documents_manage'
    },
    'operador': {
        'dashboard', 'units_view', 'water_manage', 'management_manage',
        'transfers_manage', 'feed_manage', 'protocols_manage', 'farm_documents_manage'
    },
    'consulta': {'dashboard', 'units_view', 'protocols_manage', 'farm_documents_manage'},
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
    phase = db.Column(db.String(30), nullable=False)  # bercario / juvenil / engorda
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
    end_date = db.Column(db.Date)
    closed_reason = db.Column(db.String(60))
    notes = db.Column(db.Text)
    larva_supplier = db.Column(db.String(120))
    entry_pl_stage = db.Column(db.Integer)
    unit = db.relationship('Unit')


class LotUnitAllocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    quantity_allocated = db.Column(db.Integer)
    notes = db.Column(db.Text)
    lot = db.relationship('Lot')
    unit = db.relationship('Unit')


class FixedCost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    monthly_amount = db.Column(db.Float, nullable=False, default=0)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    active = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class NurseryFeeding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    quantity_kg = db.Column(db.Float, nullable=False, default=0)
    intestinal_score = db.Column(db.Float)
    score_adjustment_pct = db.Column(db.Float)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')


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
    nitrate = db.Column(db.Float)
    alkalinity = db.Column(db.Float)
    hardness = db.Column(db.Float)
    observation = db.Column(db.Text)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')


class DailyManagement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    manage_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    feed_product_id = db.Column(db.Integer, db.ForeignKey('feed_product.id'))
    feed_offered_kg = db.Column(db.Float, default=0)
    feed_consumed_kg = db.Column(db.Float, default=0)
    tray_score = db.Column(db.Float)
    feed_unit_cost = db.Column(db.Float)
    feed_total_cost = db.Column(db.Float, default=0)
    mortality_qty = db.Column(db.Integer, default=0)
    average_weight_g = db.Column(db.Float)
    estimated_biomass_kg = db.Column(db.Float)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')
    feed_product = db.relationship('FeedProduct')


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


class FarmDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False, default='Geral')
    notes = db.Column(db.Text)
    original_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer, nullable=False, default=0)
    file_data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_by = db.relationship('User', foreign_keys=[uploaded_by_id])


class WaterReferenceConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    od_min = db.Column(db.Float, default=TARGET_OD_MIN)
    od_max = db.Column(db.Float)
    ph_min = db.Column(db.Float, default=TARGET_PH_MIN)
    ph_max = db.Column(db.Float, default=TARGET_PH_MAX)
    temperature_min = db.Column(db.Float, default=TARGET_TEMP_MIN)
    temperature_max = db.Column(db.Float, default=TARGET_TEMP_MAX)
    salinity_min = db.Column(db.Float, default=TARGET_SALINITY_MIN)
    salinity_max = db.Column(db.Float, default=TARGET_SALINITY_MAX)
    transparency_min = db.Column(db.Float, default=TARGET_TRANSPARENCY_MIN)
    transparency_max = db.Column(db.Float, default=TARGET_TRANSPARENCY_MAX)
    ammonia_min = db.Column(db.Float)
    ammonia_max = db.Column(db.Float, default=TARGET_AMMONIA_MAX)
    nitrite_min = db.Column(db.Float)
    nitrite_max = db.Column(db.Float, default=TARGET_NITRITE_MAX)
    nitrate_min = db.Column(db.Float)
    nitrate_max = db.Column(db.Float)
    alkalinity_min = db.Column(db.Float)
    alkalinity_max = db.Column(db.Float)
    hardness_min = db.Column(db.Float)
    hardness_max = db.Column(db.Float)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    updated_by = db.relationship('User', foreign_keys=[updated_by_id])


class Transfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transfer_date = db.Column(db.Date, nullable=False)
    source_unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    destination_unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    source_lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    destination_lot_code = db.Column(db.String(50), nullable=False)
    source_phase = db.Column(db.String(30))
    destination_phase = db.Column(db.String(30))
    transferred_qty = db.Column(db.Integer, nullable=False)
    avg_weight_g = db.Column(db.Float)
    notes = db.Column(db.Text)
    source_unit = db.relationship('Unit', foreign_keys=[source_unit_id])
    destination_unit = db.relationship('Unit', foreign_keys=[destination_unit_id])
    source_lot = db.relationship('Lot')


class FeedProduct(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(120), nullable=False)
    feed_type = db.Column(db.String(120), nullable=False)
    protein_pct = db.Column(db.Float)
    pellet_size_mm = db.Column(db.Float)
    minimum_stock_kg = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def full_name(self):
        generic_types = {'', 'geral', 'bercario', 'bercário'}
        brand = re.sub(r'\s+', ' ', (self.brand or '').strip())
        feed_type = re.sub(r'\s+', ' ', (self.feed_type or '').strip())
        normalized_brand = normalize_text(brand)
        normalized_type = normalize_text(feed_type)

        # Evita nomes duplicados como:
        # "AQUAVITA 35 ... · AQUAVITA 35 ..." quando marca e tipo foram
        # cadastrados iguais ou quando um campo já contém o outro.
        parts = []
        if brand:
            parts.append(brand)
        should_add_type = (
            feed_type
            and normalized_type not in generic_types
            and normalized_type != normalized_brand
            and normalized_type not in normalized_brand
            and normalized_brand not in normalized_type
        )
        if should_add_type:
            parts.append(feed_type)
        return ' · '.join(parts) if parts else f'Ração #{self.id}'

    @property
    def technical_summary(self):
        details = []
        if self.protein_pct is not None:
            details.append(f'{self.protein_pct:g}% PB')
        if self.pellet_size_mm is not None:
            details.append(f'{self.pellet_size_mm:g} mm')
        return ' · '.join(details)


class FeedInventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_date = db.Column(db.Date, nullable=False)
    feed_name = db.Column(db.String(255), nullable=False)
    feed_product_id = db.Column(db.Integer, db.ForeignKey('feed_product.id'))
    movement_type = db.Column(db.String(20), nullable=False)  # entrada / saida
    quantity_kg = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float)
    notes = db.Column(db.Text)
    source_type = db.Column(db.String(30), nullable=False, default='manual')
    source_ref_id = db.Column(db.Integer)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    feed_product = db.relationship('FeedProduct')
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')
    created_by = db.relationship('User')


class SupplyProduct(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False, default='Insumo geral')
    measure_unit = db.Column(db.String(20), nullable=False, default='kg')
    minimum_stock_qty = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def full_name(self):
        return self.name.strip()

    @property
    def technical_summary(self):
        details = []
        if self.category:
            details.append(self.category)
        if self.measure_unit:
            details.append(f'unidade {self.measure_unit}')
        return ' · '.join(details)


class SupplyInventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    movement_date = db.Column(db.Date, nullable=False)
    supply_product_id = db.Column(db.Integer, db.ForeignKey('supply_product.id'), nullable=False)
    movement_type = db.Column(db.String(20), nullable=False)  # entrada / saida
    quantity = db.Column(db.Float, nullable=False)
    unit_cost = db.Column(db.Float)
    notes = db.Column(db.Text)
    source_type = db.Column(db.String(30), nullable=False, default='manual')
    source_ref_id = db.Column(db.Integer)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    supply_product = db.relationship('SupplyProduct')
    unit = db.relationship('Unit')
    lot = db.relationship('Lot')
    created_by = db.relationship('User')


class ManagementSupplyUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    management_id = db.Column(db.Integer, db.ForeignKey('daily_management.id'), nullable=False)
    supply_product_id = db.Column(db.Integer, db.ForeignKey('supply_product.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit_cost = db.Column(db.Float)
    total_cost = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    management = db.relationship('DailyManagement')
    supply_product = db.relationship('SupplyProduct')


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_date = db.Column(db.Date, nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    client_name = db.Column(db.String(120), nullable=False)
    channel = db.Column(db.String(40), nullable=False)
    quantity_kg = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    average_weight_g = db.Column(db.Float)
    harvested_units = db.Column(db.Integer)
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
    return {'bercario': 'Berçário', 'juvenil': 'Juvenil', 'engorda': 'Engorda'}.get(value, value)


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


def water_sheet_supported_times(sheet_type: str):
    return ['07:00', '16:00', '18:00'] if sheet_type == 'day' else ['18:00', '00:00', '02:00', '04:00']


def water_sheet_type_label(sheet_type: str) -> str:
    return 'diurna' if sheet_type == 'day' else 'noturna'


def detect_water_sheet_type_with_openai(file_bytes: bytes, filename: str, content_type: str):
    if OpenAI is None:
        raise RuntimeError('A biblioteca OpenAI não está instalada no ambiente.')
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('Defina OPENAI_API_KEY para habilitar a leitura automática por foto.')

    mime_type = content_type or 'image/jpeg'
    model = os.getenv('OPENAI_VISION_MODEL', 'gpt-5.4-mini')
    client = OpenAI(api_key=api_key)
    encoded = base64.b64encode(file_bytes).decode('utf-8')
    prompt = (
        'Analise a imagem de uma ficha de monitoramento de água e devolva apenas JSON válido no formato '
        '{"sheet_type":"day"} ou {"sheet_type":"night"}.\n\n'
        'Regras de classificação:\n'
        '1. Se o cabeçalho/título da ficha disser NOITE, classifique como night.\n'
        '2. Se o cabeçalho/título da ficha disser DIA, classifique como day, mesmo que exista uma coluna 18:00.\n'
        '3. A presença isolada da coluna 18:00 não torna a ficha noturna.\n'
        '4. Se a ficha tiver colunas 00:00, 02:00 ou 04:00, isso indica ficha night.\n'
        '5. Em caso de conflito, priorize o título visível da ficha (DIA/NOITE) e depois o conjunto completo de colunas.\n'
        '6. Não explique nada fora do JSON.'
    )

    response = client.responses.create(
        model=model,
        input=[{
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': prompt},
                {'type': 'input_image', 'image_url': f'data:{mime_type};base64,{encoded}'},
            ],
        }],
    )
    raw_text = getattr(response, 'output_text', '') or ''
    payload = extract_json_object(raw_text)
    detected = (payload.get('sheet_type') or '').strip().lower()
    if detected not in {'day', 'night'}:
        raise ValueError('Não consegui identificar se a ficha é diurna ou noturna.')
    return detected


def water_sheet_time_to_date(base_date: date, sheet_type: str, slot_label: str) -> date:
    if sheet_type == 'night' and slot_label in {'00:00', '02:00', '04:00'}:
        return base_date + timedelta(days=1)
    return base_date


def normalize_text(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '')
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return ' '.join(value.split())


def unit_aliases(unit):
    aliases = {unit.name, unit.code}
    name = normalize_text(unit.name)
    if 'santa rita' in name:
        aliases.update({'sta rita', 'st rita', 'stª rita', 'santa rita'})
    if 'cruz do espirito santo' in name or 'esp santo' in name:
        aliases.update({'cr esp santo', 'cr. esp santo', 'esp santo', 'cruz esp santo'})
    if 'campina grande' in name:
        aliases.update({'camp grande', 'camp. grande', 'campina grande'})
    if 'lucena' in name:
        aliases.update({'lucena'})
    if 'belem' in name:
        aliases.update({'belem', 'belém'})
    if 'natuba' in name:
        aliases.update({'natuba'})
    if 'conde' in name:
        aliases.update({'conde'})
    if 'sape' in name:
        aliases.update({'sape', 'sapé'})
    if name.startswith('sp ') or 'sao paulo' in name:
        aliases.update({name.replace('sao paulo', 'sp'), name.replace('sao paulo', 's p')})
    if 'sp1' in name or 'sp 1' in name or 'sao paulo 1' in name:
        aliases.update({'sp1', 'sp 1', 'sao paulo 1'})
    if 'sp2' in name or 'sp 2' in name or 'sao paulo 2' in name:
        aliases.update({'sp2', 'sp 2', 'sao paulo 2'})
    if 'sp3' in name or 'sp 3' in name or 'sao paulo 3' in name:
        aliases.update({'sp3', 'sp 3', 'sao paulo 3'})
    if 'rio grande do sul 1' in name:
        aliases.update({'rio g do sul 1', 'rio g. do sul 1', 'rgs 1', 'rio grande do sul 1'})
    if 'rio grande do sul 2' in name:
        aliases.update({'rio g do sul 2', 'rio g. do sul 2', 'rgs 2', 'rio grande do sul 2'})
    return {normalize_text(alias) for alias in aliases if alias}


def match_unit_from_sheet_row(row_name: str, units):
    normalized_row = normalize_text(row_name)
    if not normalized_row:
        return None
    for unit in units:
        if normalized_row in unit_aliases(unit):
            return unit
    for unit in units:
        aliases = unit_aliases(unit)
        if any(normalized_row == alias or normalized_row in alias or alias in normalized_row for alias in aliases):
            return unit
    return None


def build_water_sheet_prompt(sheet_type: str, sheet_date: date, units):
    period_label = water_sheet_type_label(sheet_type)
    allowed_times = ', '.join(water_sheet_supported_times(sheet_type))
    unit_labels = ', '.join(unit.name for unit in units)
    return (
        "Leia esta ficha de monitoramento de água e devolva apenas JSON válido.\n\n"
        f"Tipo da ficha: {period_label}.\n"
        f"Data escrita na ficha: {sheet_date.strftime('%d/%m/%Y')}.\n"
        f"Horários aceitos: {allowed_times}.\n"
        f"Unidades esperadas no sistema: {unit_labels}.\n\n"
        "Regras:\n"
        "1. Extraia somente linhas que realmente tenham números preenchidos.\n"
        "2. Preserve os horários exatamente como aparecem: HH:MM.\n"
        "3. Para ficha diurna, aceite 07:00, 16:00 e 18:00. Para ficha noturna, aceite 18:00, 00:00, 02:00 e 04:00.\n"
        "4. A presença isolada da coluna 18:00 não muda o tipo da ficha; siga o cabeçalho DIA/NOITE e o conjunto de colunas.\n"
        "5. Para cada leitura, informe: row_name, time, dissolved_oxygen, temperature_c, ph, ammonia, nitrite.\n"
        "6. Use null para campos em branco.\n"
        "7. Não invente dados.\n"
        "8. Se tiver dúvida em algum número, prefira null.\n"
        "9. Responda no formato: {\"readings\":[...]}\n"
    )


def extract_json_object(text_value: str):
    text_value = (text_value or '').strip()
    if not text_value:
        raise ValueError('Resposta vazia do modelo.')
    match = re.search(r'\{.*\}', text_value, flags=re.S)
    if not match:
        raise ValueError('Não encontrei JSON na resposta do modelo.')
    return json.loads(match.group(0))


def extract_water_sheet_data_with_openai(file_bytes: bytes, filename: str, content_type: str, sheet_type: str, sheet_date: date, units):
    if OpenAI is None:
        raise RuntimeError('A biblioteca OpenAI não está instalada no ambiente.')
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('Defina OPENAI_API_KEY para habilitar a leitura automática por foto.')

    mime_type = content_type or 'image/jpeg'
    model = os.getenv('OPENAI_VISION_MODEL', 'gpt-5.4-mini')
    client = OpenAI(api_key=api_key)
    prompt = build_water_sheet_prompt(sheet_type, sheet_date, units)
    encoded = base64.b64encode(file_bytes).decode('utf-8')

    response = client.responses.create(
        model=model,
        input=[{
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': prompt},
                {'type': 'input_image', 'image_url': f'data:{mime_type};base64,{encoded}'},
            ],
        }],
    )

    raw_text = getattr(response, 'output_text', '') or ''
    payload = extract_json_object(raw_text)
    readings = payload.get('readings') or []
    if not isinstance(readings, list):
        raise ValueError('Formato inválido retornado pela IA.')
    return readings


def build_water_import_preview(readings, units, sheet_type: str, sheet_date: date):
    preview_rows = []
    warnings = []
    seen_unknown_rows = []

    for item in readings:
        row_name = (item.get('row_name') or '').strip()
        slot_label = (item.get('time') or '').strip()
        if slot_label not in water_sheet_supported_times(sheet_type):
            warnings.append(f"Horário ignorado por não fazer parte da ficha: {slot_label or 'vazio'}")
            continue

        values = {
            'dissolved_oxygen': parse_float(item.get('dissolved_oxygen')) if item.get('dissolved_oxygen') is not None else None,
            'temperature_c': parse_float(item.get('temperature_c')) if item.get('temperature_c') is not None else None,
            'ph': parse_float(item.get('ph')) if item.get('ph') is not None else None,
            'ammonia': parse_float(item.get('ammonia')) if item.get('ammonia') is not None else None,
            'nitrite': parse_float(item.get('nitrite')) if item.get('nitrite') is not None else None,
        }
        if all(values.get(field) is None for field in ['dissolved_oxygen', 'temperature_c', 'ph', 'ammonia', 'nitrite']):
            continue

        unit = match_unit_from_sheet_row(row_name, units)
        if not unit and row_name:
            seen_unknown_rows.append(row_name)

        slot_date = water_sheet_time_to_date(sheet_date, sheet_type, slot_label)
        preview_rows.append({
            'row_name': row_name,
            'unit_id': unit.id if unit else '',
            'unit_name': unit.name if unit else '',
            'time': slot_label,
            'monitor_date': slot_date.isoformat(),
            'dissolved_oxygen': values['dissolved_oxygen'],
            'temperature_c': values['temperature_c'],
            'ph': values['ph'],
            'ammonia': values['ammonia'],
            'nitrite': values['nitrite'],
            'selected': True,
        })

    if seen_unknown_rows:
        warnings.append('Algumas linhas não bateram com os viveiros cadastrados: ' + ', '.join(sorted(set(seen_unknown_rows))))

    return preview_rows, warnings


def store_pending_water_import(sheet_type: str, sheet_date: date, preview_rows, warnings):
    session['pending_water_import'] = {
        'sheet_type': sheet_type,
        'sheet_date': sheet_date.isoformat(),
        'rows': preview_rows,
        'warnings': warnings,
    }


def pop_pending_water_import():
    return session.pop('pending_water_import', None)


def get_pending_water_import():
    return session.get('pending_water_import')


def upsert_water_reading(unit_id: int, slot_date: date, slot_time, values: dict):
    existing = WaterMonitoring.query.filter(
        WaterMonitoring.unit_id == unit_id,
        WaterMonitoring.monitor_date == slot_date,
        WaterMonitoring.monitor_time == slot_time,
    ).order_by(WaterMonitoring.id.desc()).first()

    lot = active_lot_for_unit(unit_id, on_date=slot_date)
    record = existing or WaterMonitoring(
        unit_id=unit_id,
        monitor_date=slot_date,
        monitor_time=slot_time,
        shift=infer_shift_from_time(slot_time),
        lot_id=lot.id if lot else None,
    )
    record.shift = infer_shift_from_time(slot_time)
    record.lot_id = lot.id if lot else None
    record.temperature_c = values.get('temperature_c')
    record.dissolved_oxygen = values.get('dissolved_oxygen')
    record.ph = values.get('ph')
    record.ammonia = values.get('ammonia')
    record.nitrite = values.get('nitrite')
    record.nitrate = values.get('nitrate')
    record.alkalinity = values.get('alkalinity')
    record.hardness = values.get('hardness')
    note = values.get('observation')
    if note:
        record.observation = note
    if existing is None:
        db.session.add(record)
        return 'created'
    return 'updated'


WATER_PARAMETER_SPECS = [
    {
        'field': 'dissolved_oxygen',
        'label': 'OD',
        'unit': 'mg/L',
        'min_attr': 'od_min',
        'max_attr': 'od_max',
        'short_status_low': 'OD baixo',
        'short_status_high': 'OD alto',
    },
    {
        'field': 'ph',
        'label': 'pH',
        'unit': '',
        'min_attr': 'ph_min',
        'max_attr': 'ph_max',
        'short_status_low': 'pH baixo',
        'short_status_high': 'pH alto',
    },
    {
        'field': 'temperature_c',
        'label': 'Temperatura',
        'unit': '°C',
        'min_attr': 'temperature_min',
        'max_attr': 'temperature_max',
        'short_status_low': 'temperatura baixa',
        'short_status_high': 'temperatura alta',
    },
    {
        'field': 'salinity',
        'label': 'Salinidade',
        'unit': '‰',
        'min_attr': 'salinity_min',
        'max_attr': 'salinity_max',
        'short_status_low': 'salinidade baixa',
        'short_status_high': 'salinidade alta',
    },
    {
        'field': 'transparency_cm',
        'label': 'Transparência',
        'unit': 'cm',
        'min_attr': 'transparency_min',
        'max_attr': 'transparency_max',
        'short_status_low': 'transparência baixa',
        'short_status_high': 'transparência alta',
    },
    {
        'field': 'ammonia',
        'label': 'Amônia',
        'unit': 'mg/L',
        'min_attr': 'ammonia_min',
        'max_attr': 'ammonia_max',
        'short_status_low': 'amônia baixa',
        'short_status_high': 'amônia alta',
    },
    {
        'field': 'nitrite',
        'label': 'Nitrito',
        'unit': 'mg/L',
        'min_attr': 'nitrite_min',
        'max_attr': 'nitrite_max',
        'short_status_low': 'nitrito baixo',
        'short_status_high': 'nitrito alto',
    },
    {
        'field': 'nitrate',
        'label': 'Nitrato',
        'unit': 'mg/L',
        'min_attr': 'nitrate_min',
        'max_attr': 'nitrate_max',
        'short_status_low': 'nitrato baixo',
        'short_status_high': 'nitrato alto',
    },
    {
        'field': 'alkalinity',
        'label': 'Alcalinidade',
        'unit': 'mg/L',
        'min_attr': 'alkalinity_min',
        'max_attr': 'alkalinity_max',
        'short_status_low': 'alcalinidade baixa',
        'short_status_high': 'alcalinidade alta',
    },
    {
        'field': 'hardness',
        'label': 'Dureza',
        'unit': 'mg/L',
        'min_attr': 'hardness_min',
        'max_attr': 'hardness_max',
        'short_status_low': 'dureza baixa',
        'short_status_high': 'dureza alta',
    },
]


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
    value = str(value).strip().replace(',', '.')
    if value == '':
        return default
    return int(float(value))


NURSERY_PROTOCOL_BASE_POPULATION = 160000
NURSERY_PROTOCOL_ROWS = [{'pl_stage': 10,
  'day': 1,
  'population': 160000,
  'survival_pct': 100.0,
  'individual_weight_g': 0.002,
  'daily_growth_g': None,
  'feed_rate_pct': 35.0,
  'total_day_g': 110,
  'feedings_per_day': 12,
  'per_feeding_g': 9,
  'biomass_kg': 0.32,
  'estimated_fcr': 0.35,
  'mixes': [{'label': 'MeM 200-300', 'grams': 112}]},
 {'pl_stage': 11,
  'day': 2,
  'population': 159467,
  'survival_pct': 100.0,
  'individual_weight_g': 0.003,
  'daily_growth_g': 0.001,
  'feed_rate_pct': 34.0,
  'total_day_g': 160,
  'feedings_per_day': 12,
  'per_feeding_g': 13,
  'biomass_kg': 0.48,
  'estimated_fcr': 0.57,
  'mixes': [{'label': 'MeM 200-300', 'grams': 163}]},
 {'pl_stage': 12,
  'day': 3,
  'population': 158933,
  'survival_pct': 99.0,
  'individual_weight_g': 0.004,
  'daily_growth_g': 0.001,
  'feed_rate_pct': 30.0,
  'total_day_g': 190,
  'feedings_per_day': 12,
  'per_feeding_g': 16,
  'biomass_kg': 0.64,
  'estimated_fcr': 0.73,
  'mixes': [{'label': 'MeM 200-300', 'grams': 143}, {'label': 'MeM 300-500', 'grams': 48}]},
 {'pl_stage': 13,
  'day': 4,
  'population': 158400,
  'survival_pct': 99.0,
  'individual_weight_g': 0.006,
  'daily_growth_g': 0.0019,
  'feed_rate_pct': 29.0,
  'total_day_g': 270,
  'feedings_per_day': 12,
  'per_feeding_g': 22,
  'biomass_kg': 0.93,
  'estimated_fcr': 0.79,
  'mixes': [{'label': 'MeM 200-300', 'grams': 135}, {'label': 'MeM 300-500', 'grams': 135}]},
 {'pl_stage': 14,
  'day': 5,
  'population': 157867,
  'survival_pct': 99.0,
  'individual_weight_g': 0.009,
  'daily_growth_g': 0.003,
  'feed_rate_pct': 28.0,
  'total_day_g': 390,
  'feedings_per_day': 12,
  'per_feeding_g': 32,
  'biomass_kg': 1.4,
  'estimated_fcr': 0.81,
  'mixes': [{'label': 'MeM 200-300', 'grams': 98}, {'label': 'MeM 300-500', 'grams': 294}]},
 {'pl_stage': 15,
  'day': 6,
  'population': 157333,
  'survival_pct': 98.0,
  'individual_weight_g': 0.013,
  'daily_growth_g': 0.004,
  'feed_rate_pct': 26.0,
  'total_day_g': 530,
  'feedings_per_day': 12,
  'per_feeding_g': 44,
  'biomass_kg': 2.02,
  'estimated_fcr': 0.82,
  'mixes': [{'label': 'MeM 300-500', 'grams': 526}]},
 {'pl_stage': 16,
  'day': 7,
  'population': 156800,
  'survival_pct': 98.0,
  'individual_weight_g': 0.017,
  'daily_growth_g': 0.004,
  'feed_rate_pct': 24.0,
  'total_day_g': 630,
  'feedings_per_day': 12,
  'per_feeding_g': 52,
  'biomass_kg': 2.64,
  'estimated_fcr': 0.86,
  'mixes': [{'label': 'MeM 300-500', 'grams': 634}]},
 {'pl_stage': 17,
  'day': 8,
  'population': 156267,
  'survival_pct': 98.0,
  'individual_weight_g': 0.022,
  'daily_growth_g': 0.005,
  'feed_rate_pct': 22.0,
  'total_day_g': 750,
  'feedings_per_day': 12,
  'per_feeding_g': 62,
  'biomass_kg': 3.42,
  'estimated_fcr': 0.89,
  'mixes': [{'label': 'MeM 300-500', 'grams': 751}]},
 {'pl_stage': 18,
  'day': 9,
  'population': 155733,
  'survival_pct': 97.0,
  'individual_weight_g': 0.028,
  'daily_growth_g': 0.006,
  'feed_rate_pct': 20.0,
  'total_day_g': 870,
  'feedings_per_day': 12,
  'per_feeding_g': 72,
  'biomass_kg': 4.34,
  'estimated_fcr': 0.9,
  'mixes': [{'label': 'MeM 300-500', 'grams': 868}]},
 {'pl_stage': 19,
  'day': 10,
  'population': 155200,
  'survival_pct': 97.0,
  'individual_weight_g': 0.035,
  'daily_growth_g': 0.007,
  'feed_rate_pct': 18.0,
  'total_day_g': 970,
  'feedings_per_day': 12,
  'per_feeding_g': 81,
  'biomass_kg': 5.41,
  'estimated_fcr': 0.9,
  'mixes': [{'label': 'MeM 300-500', 'grams': 974}]},
 {'pl_stage': 20,
  'day': 11,
  'population': 154667,
  'survival_pct': 97.0,
  'individual_weight_g': 0.043,
  'daily_growth_g': 0.008,
  'feed_rate_pct': 17.0,
  'total_day_g': 1130,
  'feedings_per_day': 12,
  'per_feeding_g': 94,
  'biomass_kg': 6.63,
  'estimated_fcr': 0.91,
  'mixes': [{'label': 'MeM 300-500', 'grams': 1127}]},
 {'pl_stage': 21,
  'day': 12,
  'population': 154133,
  'survival_pct': 96.0,
  'individual_weight_g': 0.052,
  'daily_growth_g': 0.009,
  'feed_rate_pct': 16.0,
  'total_day_g': 1280,
  'feedings_per_day': 12,
  'per_feeding_g': 107,
  'biomass_kg': 7.99,
  'estimated_fcr': 0.91,
  'mixes': [{'label': 'MeM 300-500', 'grams': 1279}]},
 {'pl_stage': 22,
  'day': 13,
  'population': 153600,
  'survival_pct': 96.0,
  'individual_weight_g': 0.062,
  'daily_growth_g': 0.01,
  'feed_rate_pct': 15.0,
  'total_day_g': 1430,
  'feedings_per_day': 12,
  'per_feeding_g': 119,
  'biomass_kg': 9.5,
  'estimated_fcr': 0.92,
  'mixes': [{'label': 'MeM 300-500', 'grams': 1425}]},
 {'pl_stage': 23,
  'day': 14,
  'population': 153067,
  'survival_pct': 96.0,
  'individual_weight_g': 0.073,
  'daily_growth_g': 0.011,
  'feed_rate_pct': 14.0,
  'total_day_g': 1560,
  'feedings_per_day': 12,
  'per_feeding_g': 130,
  'biomass_kg': 11.15,
  'estimated_fcr': 0.92,
  'mixes': [{'label': 'MeM 300-500', 'grams': 1561}]},
 {'pl_stage': 24,
  'day': 15,
  'population': 152000,
  'survival_pct': 95.0,
  'individual_weight_g': 0.085,
  'daily_growth_g': 0.0125,
  'feed_rate_pct': 13.0,
  'total_day_g': 1690,
  'feedings_per_day': 12,
  'per_feeding_g': 141,
  'biomass_kg': 12.97,
  'estimated_fcr': 0.89,
  'mixes': [{'label': 'MeM 300-500', 'grams': 1265}]}]
TABLE_PROTOCOL_BASE_POPULATION = 160000
PRODUCTION_PROTOCOL_ROWS = [{'phase': 'juvenil',
  'phase_day': 1,
  'cumulative_day': 16,
  'stage': 'JUVENILE',
  'population': 152000,
  'weight_g': 0.09,
  'daily_growth_g': None,
  'growth_rate_pct_day': None,
  'biomass_kg': 12.97,
  'feed_rate_pct': 13.0,
  'daily_feed_kg': 1.69,
  'feedings_per_day': 12,
  'survival_pct': 100.0,
  'cumulative_feed_kg': 1.69,
  'estimated_fcr': 0.13,
  'crop_fcr': 1.05,
  'mixes': [{'label': 'Crumbled I', 'kg': 1.69}]},
 {'phase': 'juvenil',
  'phase_day': 2,
  'cumulative_day': 17,
  'stage': 'JUVENILE',
  'population': 151240,
  'weight_g': 0.13,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 36.4,
  'biomass_kg': 20.29,
  'feed_rate_pct': 12.0,
  'daily_feed_kg': 2.43,
  'feedings_per_day': 12,
  'survival_pct': 100.0,
  'cumulative_feed_kg': 4.12,
  'estimated_fcr': 0.2,
  'crop_fcr': 0.79,
  'mixes': [{'label': 'Crumbled I', 'kg': 2.43}]},
 {'phase': 'juvenil',
  'phase_day': 3,
  'cumulative_day': 18,
  'stage': 'JUVENILE',
  'population': 150480,
  'weight_g': 0.18,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 26.7,
  'biomass_kg': 27.53,
  'feed_rate_pct': 11.0,
  'daily_feed_kg': 3.03,
  'feedings_per_day': 12,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 7.15,
  'estimated_fcr': 0.26,
  'crop_fcr': 0.69,
  'mixes': [{'label': 'Crumbled I', 'kg': 3.03}]},
 {'phase': 'juvenil',
  'phase_day': 4,
  'cumulative_day': 19,
  'stage': 'JUVENILE',
  'population': 149720,
  'weight_g': 0.23,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 21.1,
  'biomass_kg': 34.69,
  'feed_rate_pct': 10.0,
  'daily_feed_kg': 3.47,
  'feedings_per_day': 12,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 10.62,
  'estimated_fcr': 0.31,
  'crop_fcr': 0.65,
  'mixes': [{'label': 'Crumbled I', 'kg': 3.47}]},
 {'phase': 'juvenil',
  'phase_day': 5,
  'cumulative_day': 20,
  'stage': 'JUVENILE',
  'population': 148960,
  'weight_g': 0.28,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 17.4,
  'biomass_kg': 41.78,
  'feed_rate_pct': 9.0,
  'daily_feed_kg': 3.76,
  'feedings_per_day': 12,
  'survival_pct': 98.0,
  'cumulative_feed_kg': 14.38,
  'estimated_fcr': 0.34,
  'crop_fcr': 0.63,
  'mixes': [{'label': 'Crumbled I', 'kg': 3.76}]},
 {'phase': 'juvenil',
  'phase_day': 6,
  'cumulative_day': 21,
  'stage': 'JUVENILE',
  'population': 148200,
  'weight_g': 0.33,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 14.8,
  'biomass_kg': 48.8,
  'feed_rate_pct': 8.5,
  'daily_feed_kg': 4.15,
  'feedings_per_day': 12,
  'survival_pct': 98.0,
  'cumulative_feed_kg': 18.53,
  'estimated_fcr': 0.38,
  'crop_fcr': 0.62,
  'mixes': [{'label': 'Crumbled I', 'kg': 4.15}]},
 {'phase': 'juvenil',
  'phase_day': 7,
  'cumulative_day': 22,
  'stage': 'JUVENILE',
  'population': 147440,
  'weight_g': 0.38,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 12.9,
  'biomass_kg': 55.74,
  'feed_rate_pct': 7.6,
  'daily_feed_kg': 4.24,
  'feedings_per_day': 12,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 22.76,
  'estimated_fcr': 0.41,
  'crop_fcr': 0.62,
  'mixes': [{'label': 'Crumbled I', 'kg': 4.24}]},
 {'phase': 'juvenil',
  'phase_day': 8,
  'cumulative_day': 23,
  'stage': 'JUVENILE',
  'population': 146680,
  'weight_g': 0.43,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 11.4,
  'biomass_kg': 62.61,
  'feed_rate_pct': 7.3,
  'daily_feed_kg': 4.57,
  'feedings_per_day': 12,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 27.33,
  'estimated_fcr': 0.44,
  'crop_fcr': 0.63,
  'mixes': [{'label': 'Crumbled I', 'kg': 4.57}]},
 {'phase': 'juvenil',
  'phase_day': 9,
  'cumulative_day': 24,
  'stage': 'JUVENILE',
  'population': 145920,
  'weight_g': 0.48,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 10.3,
  'biomass_kg': 69.4,
  'feed_rate_pct': 6.8,
  'daily_feed_kg': 4.72,
  'feedings_per_day': 12,
  'survival_pct': 96.0,
  'cumulative_feed_kg': 32.05,
  'estimated_fcr': 0.46,
  'crop_fcr': 0.63,
  'mixes': [{'label': 'Crumbled I', 'kg': 3.54}, {'label': 'Crumbled II', 'kg': 1.18}]},
 {'phase': 'juvenil',
  'phase_day': 10,
  'cumulative_day': 25,
  'stage': 'JUVENILE',
  'population': 145160,
  'weight_g': 0.52,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 9.3,
  'biomass_kg': 76.12,
  'feed_rate_pct': 6.4,
  'daily_feed_kg': 4.87,
  'feedings_per_day': 12,
  'survival_pct': 96.0,
  'cumulative_feed_kg': 36.92,
  'estimated_fcr': 0.49,
  'crop_fcr': 0.64,
  'mixes': [{'label': 'Crumbled I', 'kg': 2.44}, {'label': 'Crumbled II', 'kg': 2.44}]},
 {'phase': 'juvenil',
  'phase_day': 11,
  'cumulative_day': 26,
  'stage': 'JUVENILE',
  'population': 144400,
  'weight_g': 0.57,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 8.5,
  'biomass_kg': 82.77,
  'feed_rate_pct': 6.4,
  'daily_feed_kg': 5.3,
  'feedings_per_day': 12,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 42.22,
  'estimated_fcr': 0.51,
  'crop_fcr': 0.65,
  'mixes': [{'label': 'Crumbled I', 'kg': 1.32}, {'label': 'Crumbled II', 'kg': 3.97}]},
 {'phase': 'juvenil',
  'phase_day': 12,
  'cumulative_day': 27,
  'stage': 'JUVENILE',
  'population': 143640,
  'weight_g': 0.62,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 7.8,
  'biomass_kg': 89.34,
  'feed_rate_pct': 6.3,
  'daily_feed_kg': 5.63,
  'feedings_per_day': 12,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 47.85,
  'estimated_fcr': 0.54,
  'crop_fcr': 0.67,
  'mixes': [{'label': 'Crumbled II', 'kg': 5.63}]},
 {'phase': 'juvenil',
  'phase_day': 13,
  'cumulative_day': 28,
  'stage': 'JUVENILE',
  'population': 142880,
  'weight_g': 0.67,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 7.3,
  'biomass_kg': 95.83,
  'feed_rate_pct': 6.2,
  'daily_feed_kg': 5.94,
  'feedings_per_day': 12,
  'survival_pct': 94.0,
  'cumulative_feed_kg': 53.79,
  'estimated_fcr': 0.56,
  'crop_fcr': 0.69,
  'mixes': [{'label': 'Crumbled II', 'kg': 5.94}]},
 {'phase': 'juvenil',
  'phase_day': 14,
  'cumulative_day': 29,
  'stage': 'JUVENILE',
  'population': 142120,
  'weight_g': 0.72,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 6.8,
  'biomass_kg': 102.26,
  'feed_rate_pct': 6.1,
  'daily_feed_kg': 6.24,
  'feedings_per_day': 12,
  'survival_pct': 94.0,
  'cumulative_feed_kg': 60.03,
  'estimated_fcr': 0.59,
  'crop_fcr': 0.7,
  'mixes': [{'label': 'Crumbled II', 'kg': 6.24}]},
 {'phase': 'juvenil',
  'phase_day': 15,
  'cumulative_day': 30,
  'stage': 'JUVENILE',
  'population': 141360,
  'weight_g': 0.77,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 6.3,
  'biomass_kg': 108.61,
  'feed_rate_pct': 6.0,
  'daily_feed_kg': 6.52,
  'feedings_per_day': 12,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 66.54,
  'estimated_fcr': 0.61,
  'crop_fcr': 0.72,
  'mixes': [{'label': 'Crumbled II', 'kg': 6.52}]},
 {'phase': 'juvenil',
  'phase_day': 16,
  'cumulative_day': 31,
  'stage': 'JUVENILE',
  'population': 140600,
  'weight_g': 0.82,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 6.0,
  'biomass_kg': 114.88,
  'feed_rate_pct': 5.9,
  'daily_feed_kg': 6.78,
  'feedings_per_day': 12,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 73.32,
  'estimated_fcr': 0.64,
  'crop_fcr': 0.74,
  'mixes': [{'label': 'Crumbled II', 'kg': 6.78}]},
 {'phase': 'juvenil',
  'phase_day': 17,
  'cumulative_day': 32,
  'stage': 'JUVENILE',
  'population': 139840,
  'weight_g': 0.87,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 5.6,
  'biomass_kg': 121.08,
  'feed_rate_pct': 5.8,
  'daily_feed_kg': 7.02,
  'feedings_per_day': 12,
  'survival_pct': 92.0,
  'cumulative_feed_kg': 80.34,
  'estimated_fcr': 0.66,
  'crop_fcr': 0.76,
  'mixes': [{'label': 'Crumbled II', 'kg': 7.02}]},
 {'phase': 'juvenil',
  'phase_day': 18,
  'cumulative_day': 33,
  'stage': 'JUVENILE',
  'population': 139080,
  'weight_g': 0.91,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 5.3,
  'biomass_kg': 127.21,
  'feed_rate_pct': 5.7,
  'daily_feed_kg': 7.25,
  'feedings_per_day': 12,
  'survival_pct': 92.0,
  'cumulative_feed_kg': 87.6,
  'estimated_fcr': 0.69,
  'crop_fcr': 0.78,
  'mixes': [{'label': 'Crumbled II', 'kg': 7.25}]},
 {'phase': 'juvenil',
  'phase_day': 19,
  'cumulative_day': 34,
  'stage': 'JUVENILE',
  'population': 138320,
  'weight_g': 0.96,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 5.1,
  'biomass_kg': 133.26,
  'feed_rate_pct': 5.6,
  'daily_feed_kg': 7.46,
  'feedings_per_day': 12,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 95.06,
  'estimated_fcr': 0.71,
  'crop_fcr': 0.8,
  'mixes': [{'label': 'Crumbled II', 'kg': 7.46}]},
 {'phase': 'juvenil',
  'phase_day': 20,
  'cumulative_day': 35,
  'stage': 'JUVENILE',
  'population': 137560,
  'weight_g': 1.01,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 4.8,
  'biomass_kg': 139.24,
  'feed_rate_pct': 5.5,
  'daily_feed_kg': 7.66,
  'feedings_per_day': 12,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 102.72,
  'estimated_fcr': 0.74,
  'crop_fcr': 0.82,
  'mixes': [{'label': 'Crumbled II', 'kg': 7.66}]},
 {'phase': 'juvenil',
  'phase_day': 21,
  'cumulative_day': 36,
  'stage': 'JUVENILE',
  'population': 136800,
  'weight_g': 1.06,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 4.6,
  'biomass_kg': 145.14,
  'feed_rate_pct': 5.5,
  'daily_feed_kg': 7.98,
  'feedings_per_day': 12,
  'survival_pct': 90.0,
  'cumulative_feed_kg': 110.7,
  'estimated_fcr': 0.76,
  'crop_fcr': 0.85,
  'mixes': [{'label': 'Crumbled II', 'kg': 7.98}]},
 {'phase': 'juvenil',
  'phase_day': 22,
  'cumulative_day': 37,
  'stage': 'JUVENILE',
  'population': 136040,
  'weight_g': 1.11,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 4.4,
  'biomass_kg': 150.97,
  'feed_rate_pct': 5.5,
  'daily_feed_kg': 8.3,
  'feedings_per_day': 12,
  'survival_pct': 90.0,
  'cumulative_feed_kg': 119.0,
  'estimated_fcr': 0.79,
  'crop_fcr': 0.87,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.3}]},
 {'phase': 'juvenil',
  'phase_day': 23,
  'cumulative_day': 38,
  'stage': 'JUVENILE',
  'population': 135280,
  'weight_g': 1.16,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 4.2,
  'biomass_kg': 156.73,
  'feed_rate_pct': 5.5,
  'daily_feed_kg': 8.62,
  'feedings_per_day': 12,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 127.62,
  'estimated_fcr': 0.81,
  'crop_fcr': 0.89,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.62}]},
 {'phase': 'juvenil',
  'phase_day': 24,
  'cumulative_day': 39,
  'stage': 'JUVENILE',
  'population': 134520,
  'weight_g': 1.21,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 4.0,
  'biomass_kg': 162.41,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 8.12,
  'feedings_per_day': 12,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 135.74,
  'estimated_fcr': 0.84,
  'crop_fcr': 0.91,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.12}]},
 {'phase': 'juvenil',
  'phase_day': 25,
  'cumulative_day': 40,
  'stage': 'JUVENILE',
  'population': 133760,
  'weight_g': 1.26,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.9,
  'biomass_kg': 168.02,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 8.4,
  'feedings_per_day': 12,
  'survival_pct': 88.0,
  'cumulative_feed_kg': 144.14,
  'estimated_fcr': 0.86,
  'crop_fcr': 0.93,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.4}]},
 {'phase': 'juvenil',
  'phase_day': 26,
  'cumulative_day': 41,
  'stage': 'JUVENILE',
  'population': 133000,
  'weight_g': 1.3,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.7,
  'biomass_kg': 173.55,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 8.68,
  'feedings_per_day': 12,
  'survival_pct': 88.0,
  'cumulative_feed_kg': 152.82,
  'estimated_fcr': 0.88,
  'crop_fcr': 0.95,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.68}]},
 {'phase': 'juvenil',
  'phase_day': 27,
  'cumulative_day': 42,
  'stage': 'JUVENILE',
  'population': 132240,
  'weight_g': 1.35,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.6,
  'biomass_kg': 179.01,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 8.95,
  'feedings_per_day': 12,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 161.77,
  'estimated_fcr': 0.9,
  'crop_fcr': 0.97,
  'mixes': [{'label': 'Crumbled II', 'kg': 8.95}]},
 {'phase': 'juvenil',
  'phase_day': 28,
  'cumulative_day': 43,
  'stage': 'JUVENILE',
  'population': 131480,
  'weight_g': 1.4,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.5,
  'biomass_kg': 184.39,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 9.22,
  'feedings_per_day': 12,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 170.99,
  'estimated_fcr': 0.93,
  'crop_fcr': 0.99,
  'mixes': [{'label': 'Crumbled II', 'kg': 9.22}]},
 {'phase': 'juvenil',
  'phase_day': 29,
  'cumulative_day': 44,
  'stage': 'JUVENILE',
  'population': 130720,
  'weight_g': 1.45,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.4,
  'biomass_kg': 189.7,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 9.49,
  'feedings_per_day': 12,
  'survival_pct': 86.0,
  'cumulative_feed_kg': 180.48,
  'estimated_fcr': 0.95,
  'crop_fcr': 1.01,
  'mixes': [{'label': 'Crumbled II', 'kg': 9.49}]},
 {'phase': 'juvenil',
  'phase_day': 30,
  'cumulative_day': 45,
  'stage': 'JUVENILE',
  'population': 129200,
  'weight_g': 1.5,
  'daily_growth_g': 0.0488,
  'growth_rate_pct_day': 3.3,
  'biomass_kg': 193.8,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 9.69,
  'feedings_per_day': 12,
  'survival_pct': 85.0,
  'cumulative_feed_kg': 190.17,
  'estimated_fcr': 0.98,
  'crop_fcr': 1.04,
  'mixes': [{'label': 'Crumbled II', 'kg': 9.69}]},
 {'phase': 'engorda',
  'phase_day': 1,
  'cumulative_day': 46,
  'stage': 'GROW OUT',
  'population': 129200,
  'weight_g': 1.5,
  'daily_growth_g': None,
  'growth_rate_pct_day': None,
  'biomass_kg': 193.8,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 9.69,
  'feedings_per_day': 4,
  'survival_pct': 100.0,
  'cumulative_feed_kg': 9.69,
  'estimated_fcr': 0.05,
  'crop_fcr': 1.09,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 9.69}]},
 {'phase': 'engorda',
  'phase_day': 2,
  'cumulative_day': 47,
  'stage': 'GROW OUT',
  'population': 128831,
  'weight_g': 1.71,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 12.1,
  'biomass_kg': 219.93,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 11.0,
  'feedings_per_day': 4,
  'survival_pct': 100.0,
  'cumulative_feed_kg': 20.69,
  'estimated_fcr': 0.09,
  'crop_fcr': 0.15,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 11.0}]},
 {'phase': 'engorda',
  'phase_day': 3,
  'cumulative_day': 48,
  'stage': 'GROW OUT',
  'population': 128462,
  'weight_g': 1.91,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 10.8,
  'biomass_kg': 245.91,
  'feed_rate_pct': 5.0,
  'daily_feed_kg': 12.3,
  'feedings_per_day': 4,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 32.98,
  'estimated_fcr': 0.13,
  'crop_fcr': 0.18,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 12.3}]},
 {'phase': 'engorda',
  'phase_day': 4,
  'cumulative_day': 49,
  'stage': 'GROW OUT',
  'population': 128093,
  'weight_g': 2.12,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 9.8,
  'biomass_kg': 271.74,
  'feed_rate_pct': 4.8,
  'daily_feed_kg': 13.04,
  'feedings_per_day': 4,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 46.03,
  'estimated_fcr': 0.17,
  'crop_fcr': 0.21,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 13.04}]},
 {'phase': 'engorda',
  'phase_day': 5,
  'cumulative_day': 50,
  'stage': 'GROW OUT',
  'population': 127723,
  'weight_g': 2.33,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 8.9,
  'biomass_kg': 297.41,
  'feed_rate_pct': 4.8,
  'daily_feed_kg': 14.28,
  'feedings_per_day': 4,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 60.3,
  'estimated_fcr': 0.2,
  'crop_fcr': 0.24,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 14.28}]},
 {'phase': 'engorda',
  'phase_day': 6,
  'cumulative_day': 51,
  'stage': 'GROW OUT',
  'population': 127354,
  'weight_g': 2.54,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 8.2,
  'biomass_kg': 322.93,
  'feed_rate_pct': 4.8,
  'daily_feed_kg': 15.5,
  'feedings_per_day': 4,
  'survival_pct': 99.0,
  'cumulative_feed_kg': 75.8,
  'estimated_fcr': 0.23,
  'crop_fcr': 0.27,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 15.5}]},
 {'phase': 'engorda',
  'phase_day': 7,
  'cumulative_day': 52,
  'stage': 'GROW OUT',
  'population': 126985,
  'weight_g': 2.74,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 7.6,
  'biomass_kg': 348.3,
  'feed_rate_pct': 4.8,
  'daily_feed_kg': 16.72,
  'feedings_per_day': 4,
  'survival_pct': 98.0,
  'cumulative_feed_kg': 92.52,
  'estimated_fcr': 0.27,
  'crop_fcr': 0.3,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 16.72}]},
 {'phase': 'engorda',
  'phase_day': 8,
  'cumulative_day': 53,
  'stage': 'GROW OUT',
  'population': 126616,
  'weight_g': 2.95,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 7.0,
  'biomass_kg': 373.52,
  'feed_rate_pct': 4.8,
  'daily_feed_kg': 17.93,
  'feedings_per_day': 4,
  'survival_pct': 98.0,
  'cumulative_feed_kg': 110.45,
  'estimated_fcr': 0.3,
  'crop_fcr': 0.33,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 17.93}]},
 {'phase': 'engorda',
  'phase_day': 9,
  'cumulative_day': 54,
  'stage': 'GROW OUT',
  'population': 126247,
  'weight_g': 3.16,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 6.6,
  'biomass_kg': 398.58,
  'feed_rate_pct': 4.7,
  'daily_feed_kg': 18.73,
  'feedings_per_day': 4,
  'survival_pct': 98.0,
  'cumulative_feed_kg': 129.18,
  'estimated_fcr': 0.32,
  'crop_fcr': 0.35,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 18.73}]},
 {'phase': 'engorda',
  'phase_day': 10,
  'cumulative_day': 55,
  'stage': 'GROW OUT',
  'population': 125878,
  'weight_g': 3.36,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 6.2,
  'biomass_kg': 423.49,
  'feed_rate_pct': 4.7,
  'daily_feed_kg': 19.9,
  'feedings_per_day': 4,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 149.09,
  'estimated_fcr': 0.35,
  'crop_fcr': 0.38,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 19.9}]},
 {'phase': 'engorda',
  'phase_day': 11,
  'cumulative_day': 56,
  'stage': 'GROW OUT',
  'population': 125509,
  'weight_g': 3.57,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 5.8,
  'biomass_kg': 448.24,
  'feed_rate_pct': 4.7,
  'daily_feed_kg': 21.07,
  'feedings_per_day': 4,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 170.15,
  'estimated_fcr': 0.38,
  'crop_fcr': 0.41,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 21.07}]},
 {'phase': 'engorda',
  'phase_day': 12,
  'cumulative_day': 57,
  'stage': 'GROW OUT',
  'population': 125139,
  'weight_g': 3.78,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 5.5,
  'biomass_kg': 472.85,
  'feed_rate_pct': 4.7,
  'daily_feed_kg': 22.22,
  'feedings_per_day': 4,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 192.38,
  'estimated_fcr': 0.41,
  'crop_fcr': 0.43,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 22.22}]},
 {'phase': 'engorda',
  'phase_day': 13,
  'cumulative_day': 58,
  'stage': 'GROW OUT',
  'population': 124770,
  'weight_g': 3.99,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 5.2,
  'biomass_kg': 497.3,
  'feed_rate_pct': 4.7,
  'daily_feed_kg': 23.37,
  'feedings_per_day': 4,
  'survival_pct': 97.0,
  'cumulative_feed_kg': 215.75,
  'estimated_fcr': 0.43,
  'crop_fcr': 0.46,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 23.37}]},
 {'phase': 'engorda',
  'phase_day': 14,
  'cumulative_day': 59,
  'stage': 'GROW OUT',
  'population': 124401,
  'weight_g': 4.19,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.9,
  'biomass_kg': 521.6,
  'feed_rate_pct': 4.5,
  'daily_feed_kg': 23.47,
  'feedings_per_day': 4,
  'survival_pct': 96.0,
  'cumulative_feed_kg': 239.22,
  'estimated_fcr': 0.46,
  'crop_fcr': 0.48,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 23.47}]},
 {'phase': 'engorda',
  'phase_day': 15,
  'cumulative_day': 60,
  'stage': 'GROW OUT',
  'population': 124032,
  'weight_g': 4.4,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.7,
  'biomass_kg': 545.74,
  'feed_rate_pct': 4.5,
  'daily_feed_kg': 24.56,
  'feedings_per_day': 4,
  'survival_pct': 96.0,
  'cumulative_feed_kg': 263.78,
  'estimated_fcr': 0.48,
  'crop_fcr': 0.51,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 24.56}]},
 {'phase': 'engorda',
  'phase_day': 16,
  'cumulative_day': 61,
  'stage': 'GROW OUT',
  'population': 123663,
  'weight_g': 4.61,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.5,
  'biomass_kg': 569.73,
  'feed_rate_pct': 4.5,
  'daily_feed_kg': 25.64,
  'feedings_per_day': 4,
  'survival_pct': 96.0,
  'cumulative_feed_kg': 289.42,
  'estimated_fcr': 0.51,
  'crop_fcr': 0.53,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 25.64}]},
 {'phase': 'engorda',
  'phase_day': 17,
  'cumulative_day': 62,
  'stage': 'GROW OUT',
  'population': 123294,
  'weight_g': 4.81,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.3,
  'biomass_kg': 593.57,
  'feed_rate_pct': 4.5,
  'daily_feed_kg': 26.71,
  'feedings_per_day': 4,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 316.13,
  'estimated_fcr': 0.53,
  'crop_fcr': 0.55,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 26.71}]},
 {'phase': 'engorda',
  'phase_day': 18,
  'cumulative_day': 63,
  'stage': 'GROW OUT',
  'population': 122925,
  'weight_g': 5.02,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.1,
  'biomass_kg': 617.26,
  'feed_rate_pct': 4.3,
  'daily_feed_kg': 26.54,
  'feedings_per_day': 4,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 342.67,
  'estimated_fcr': 0.56,
  'crop_fcr': 0.57,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 26.54}]},
 {'phase': 'engorda',
  'phase_day': 19,
  'cumulative_day': 64,
  'stage': 'GROW OUT',
  'population': 122555,
  'weight_g': 5.23,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 4.0,
  'biomass_kg': 640.79,
  'feed_rate_pct': 4.3,
  'daily_feed_kg': 27.55,
  'feedings_per_day': 4,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 370.23,
  'estimated_fcr': 0.58,
  'crop_fcr': 0.6,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 27.55}]},
 {'phase': 'engorda',
  'phase_day': 20,
  'cumulative_day': 65,
  'stage': 'GROW OUT',
  'population': 122186,
  'weight_g': 5.44,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.8,
  'biomass_kg': 664.17,
  'feed_rate_pct': 4.3,
  'daily_feed_kg': 28.56,
  'feedings_per_day': 4,
  'survival_pct': 95.0,
  'cumulative_feed_kg': 398.79,
  'estimated_fcr': 0.6,
  'crop_fcr': 0.62,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 28.56}]},
 {'phase': 'engorda',
  'phase_day': 21,
  'cumulative_day': 66,
  'stage': 'GROW OUT',
  'population': 121817,
  'weight_g': 5.64,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.7,
  'biomass_kg': 687.4,
  'feed_rate_pct': 4.3,
  'daily_feed_kg': 29.56,
  'feedings_per_day': 4,
  'survival_pct': 94.0,
  'cumulative_feed_kg': 428.34,
  'estimated_fcr': 0.62,
  'crop_fcr': 0.64,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 29.56}]},
 {'phase': 'engorda',
  'phase_day': 22,
  'cumulative_day': 67,
  'stage': 'GROW OUT',
  'population': 121448,
  'weight_g': 5.85,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.5,
  'biomass_kg': 710.47,
  'feed_rate_pct': 4.3,
  'daily_feed_kg': 30.55,
  'feedings_per_day': 4,
  'survival_pct': 94.0,
  'cumulative_feed_kg': 458.89,
  'estimated_fcr': 0.65,
  'crop_fcr': 0.66,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 30.55}]},
 {'phase': 'engorda',
  'phase_day': 23,
  'cumulative_day': 68,
  'stage': 'GROW OUT',
  'population': 121079,
  'weight_g': 6.06,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.4,
  'biomass_kg': 733.39,
  'feed_rate_pct': 4.1,
  'daily_feed_kg': 30.07,
  'feedings_per_day': 4,
  'survival_pct': 94.0,
  'cumulative_feed_kg': 488.96,
  'estimated_fcr': 0.67,
  'crop_fcr': 0.68,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 30.07}]},
 {'phase': 'engorda',
  'phase_day': 24,
  'cumulative_day': 69,
  'stage': 'GROW OUT',
  'population': 120710,
  'weight_g': 6.26,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.3,
  'biomass_kg': 756.16,
  'feed_rate_pct': 4.1,
  'daily_feed_kg': 31.0,
  'feedings_per_day': 4,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 519.97,
  'estimated_fcr': 0.69,
  'crop_fcr': 0.7,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 31.0}]},
 {'phase': 'engorda',
  'phase_day': 25,
  'cumulative_day': 70,
  'stage': 'GROW OUT',
  'population': 120341,
  'weight_g': 6.47,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.2,
  'biomass_kg': 778.78,
  'feed_rate_pct': 4.1,
  'daily_feed_kg': 31.93,
  'feedings_per_day': 4,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 551.9,
  'estimated_fcr': 0.71,
  'crop_fcr': 0.72,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 31.93}]},
 {'phase': 'engorda',
  'phase_day': 26,
  'cumulative_day': 71,
  'stage': 'GROW OUT',
  'population': 119971,
  'weight_g': 6.68,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.1,
  'biomass_kg': 801.24,
  'feed_rate_pct': 4.1,
  'daily_feed_kg': 32.85,
  'feedings_per_day': 4,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 584.75,
  'estimated_fcr': 0.73,
  'crop_fcr': 0.74,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 24.64}, {'label': 'Engorda 2,4 mm', 'kg': 8.21}]},
 {'phase': 'engorda',
  'phase_day': 27,
  'cumulative_day': 72,
  'stage': 'GROW OUT',
  'population': 119602,
  'weight_g': 6.89,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 3.0,
  'biomass_kg': 823.55,
  'feed_rate_pct': 4.1,
  'daily_feed_kg': 33.77,
  'feedings_per_day': 4,
  'survival_pct': 93.0,
  'cumulative_feed_kg': 618.51,
  'estimated_fcr': 0.75,
  'crop_fcr': 0.77,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 16.88}, {'label': 'Engorda 2,4 mm', 'kg': 16.88}]},
 {'phase': 'engorda',
  'phase_day': 28,
  'cumulative_day': 73,
  'stage': 'GROW OUT',
  'population': 119233,
  'weight_g': 7.09,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.9,
  'biomass_kg': 845.7,
  'feed_rate_pct': 3.9,
  'daily_feed_kg': 32.98,
  'feedings_per_day': 4,
  'survival_pct': 92.0,
  'cumulative_feed_kg': 651.49,
  'estimated_fcr': 0.77,
  'crop_fcr': 0.78,
  'mixes': [{'label': 'Engorda J 2,0 mm', 'kg': 8.25}, {'label': 'Engorda 2,4 mm', 'kg': 24.74}]},
 {'phase': 'engorda',
  'phase_day': 29,
  'cumulative_day': 74,
  'stage': 'GROW OUT',
  'population': 118864,
  'weight_g': 7.3,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.8,
  'biomass_kg': 867.71,
  'feed_rate_pct': 3.9,
  'daily_feed_kg': 33.84,
  'feedings_per_day': 4,
  'survival_pct': 92.0,
  'cumulative_feed_kg': 685.33,
  'estimated_fcr': 0.79,
  'crop_fcr': 0.8,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 33.84}]},
 {'phase': 'engorda',
  'phase_day': 30,
  'cumulative_day': 75,
  'stage': 'GROW OUT',
  'population': 118495,
  'weight_g': 7.51,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.8,
  'biomass_kg': 889.56,
  'feed_rate_pct': 3.9,
  'daily_feed_kg': 34.69,
  'feedings_per_day': 4,
  'survival_pct': 92.0,
  'cumulative_feed_kg': 720.03,
  'estimated_fcr': 0.81,
  'crop_fcr': 0.82,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 34.69}]},
 {'phase': 'engorda',
  'phase_day': 31,
  'cumulative_day': 76,
  'stage': 'GROW OUT',
  'population': 118126,
  'weight_g': 7.71,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.7,
  'biomass_kg': 911.26,
  'feed_rate_pct': 3.9,
  'daily_feed_kg': 35.54,
  'feedings_per_day': 4,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 755.57,
  'estimated_fcr': 0.83,
  'crop_fcr': 0.84,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 35.54}]},
 {'phase': 'engorda',
  'phase_day': 32,
  'cumulative_day': 77,
  'stage': 'GROW OUT',
  'population': 117757,
  'weight_g': 7.92,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.6,
  'biomass_kg': 932.8,
  'feed_rate_pct': 3.9,
  'daily_feed_kg': 36.38,
  'feedings_per_day': 4,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 791.95,
  'estimated_fcr': 0.85,
  'crop_fcr': 0.86,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 36.38}]},
 {'phase': 'engorda',
  'phase_day': 33,
  'cumulative_day': 78,
  'stage': 'GROW OUT',
  'population': 117387,
  'weight_g': 8.13,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.5,
  'biomass_kg': 954.19,
  'feed_rate_pct': 3.7,
  'daily_feed_kg': 35.31,
  'feedings_per_day': 4,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 827.25,
  'estimated_fcr': 0.87,
  'crop_fcr': 0.88,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 35.31}]},
 {'phase': 'engorda',
  'phase_day': 34,
  'cumulative_day': 79,
  'stage': 'GROW OUT',
  'population': 117018,
  'weight_g': 8.34,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.5,
  'biomass_kg': 975.43,
  'feed_rate_pct': 3.7,
  'daily_feed_kg': 36.09,
  'feedings_per_day': 4,
  'survival_pct': 91.0,
  'cumulative_feed_kg': 863.34,
  'estimated_fcr': 0.89,
  'crop_fcr': 0.9,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 36.09}]},
 {'phase': 'engorda',
  'phase_day': 35,
  'cumulative_day': 80,
  'stage': 'GROW OUT',
  'population': 116649,
  'weight_g': 8.54,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.4,
  'biomass_kg': 996.52,
  'feed_rate_pct': 3.7,
  'daily_feed_kg': 36.87,
  'feedings_per_day': 4,
  'survival_pct': 90.0,
  'cumulative_feed_kg': 900.21,
  'estimated_fcr': 0.9,
  'crop_fcr': 0.92,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 36.87}]},
 {'phase': 'engorda',
  'phase_day': 36,
  'cumulative_day': 81,
  'stage': 'GROW OUT',
  'population': 116280,
  'weight_g': 8.75,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.4,
  'biomass_kg': 1017.45,
  'feed_rate_pct': 3.7,
  'daily_feed_kg': 37.65,
  'feedings_per_day': 4,
  'survival_pct': 90.0,
  'cumulative_feed_kg': 937.86,
  'estimated_fcr': 0.92,
  'crop_fcr': 0.93,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 37.65}]},
 {'phase': 'engorda',
  'phase_day': 37,
  'cumulative_day': 82,
  'stage': 'GROW OUT',
  'population': 115911,
  'weight_g': 8.96,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.3,
  'biomass_kg': 1038.23,
  'feed_rate_pct': 3.7,
  'daily_feed_kg': 38.41,
  'feedings_per_day': 4,
  'survival_pct': 90.0,
  'cumulative_feed_kg': 976.27,
  'estimated_fcr': 0.94,
  'crop_fcr': 0.95,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 38.41}]},
 {'phase': 'engorda',
  'phase_day': 38,
  'cumulative_day': 83,
  'stage': 'GROW OUT',
  'population': 115542,
  'weight_g': 9.16,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.3,
  'biomass_kg': 1058.86,
  'feed_rate_pct': 3.5,
  'daily_feed_kg': 37.06,
  'feedings_per_day': 4,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 1013.33,
  'estimated_fcr': 0.96,
  'crop_fcr': 0.97,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 37.06}]},
 {'phase': 'engorda',
  'phase_day': 39,
  'cumulative_day': 84,
  'stage': 'GROW OUT',
  'population': 115173,
  'weight_g': 9.37,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.2,
  'biomass_kg': 1079.33,
  'feed_rate_pct': 3.5,
  'daily_feed_kg': 37.78,
  'feedings_per_day': 4,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 1051.11,
  'estimated_fcr': 0.97,
  'crop_fcr': 0.98,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 37.78}]},
 {'phase': 'engorda',
  'phase_day': 40,
  'cumulative_day': 85,
  'stage': 'GROW OUT',
  'population': 114803,
  'weight_g': 9.58,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.2,
  'biomass_kg': 1099.65,
  'feed_rate_pct': 3.5,
  'daily_feed_kg': 38.49,
  'feedings_per_day': 4,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 1089.6,
  'estimated_fcr': 0.99,
  'crop_fcr': 1.0,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 38.49}]},
 {'phase': 'engorda',
  'phase_day': 41,
  'cumulative_day': 86,
  'stage': 'GROW OUT',
  'population': 114434,
  'weight_g': 9.79,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.1,
  'biomass_kg': 1119.82,
  'feed_rate_pct': 3.5,
  'daily_feed_kg': 39.19,
  'feedings_per_day': 4,
  'survival_pct': 89.0,
  'cumulative_feed_kg': 1128.79,
  'estimated_fcr': 1.01,
  'crop_fcr': 1.02,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 39.19}]},
 {'phase': 'engorda',
  'phase_day': 42,
  'cumulative_day': 87,
  'stage': 'GROW OUT',
  'population': 114065,
  'weight_g': 9.99,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.1,
  'biomass_kg': 1139.84,
  'feed_rate_pct': 3.5,
  'daily_feed_kg': 39.89,
  'feedings_per_day': 4,
  'survival_pct': 88.0,
  'cumulative_feed_kg': 1168.69,
  'estimated_fcr': 1.03,
  'crop_fcr': 1.04,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 39.89}]},
 {'phase': 'engorda',
  'phase_day': 43,
  'cumulative_day': 88,
  'stage': 'GROW OUT',
  'population': 113696,
  'weight_g': 10.2,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.0,
  'biomass_kg': 1159.7,
  'feed_rate_pct': 3.4,
  'daily_feed_kg': 39.43,
  'feedings_per_day': 4,
  'survival_pct': 88.0,
  'cumulative_feed_kg': 1208.11,
  'estimated_fcr': 1.04,
  'crop_fcr': 1.05,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 39.43}]},
 {'phase': 'engorda',
  'phase_day': 44,
  'cumulative_day': 89,
  'stage': 'GROW OUT',
  'population': 113327,
  'weight_g': 10.41,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.0,
  'biomass_kg': 1179.41,
  'feed_rate_pct': 3.4,
  'daily_feed_kg': 40.1,
  'feedings_per_day': 4,
  'survival_pct': 88.0,
  'cumulative_feed_kg': 1248.21,
  'estimated_fcr': 1.06,
  'crop_fcr': 1.07,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 40.1}]},
 {'phase': 'engorda',
  'phase_day': 45,
  'cumulative_day': 90,
  'stage': 'GROW OUT',
  'population': 112958,
  'weight_g': 10.61,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 2.0,
  'biomass_kg': 1198.97,
  'feed_rate_pct': 3.4,
  'daily_feed_kg': 40.76,
  'feedings_per_day': 4,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 1288.98,
  'estimated_fcr': 1.08,
  'crop_fcr': 1.09,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 40.76}]},
 {'phase': 'engorda',
  'phase_day': 46,
  'cumulative_day': 91,
  'stage': 'GROW OUT',
  'population': 112589,
  'weight_g': 10.82,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.9,
  'biomass_kg': 1218.37,
  'feed_rate_pct': 3.4,
  'daily_feed_kg': 41.42,
  'feedings_per_day': 4,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 1330.4,
  'estimated_fcr': 1.09,
  'crop_fcr': 1.1,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.42}]},
 {'phase': 'engorda',
  'phase_day': 47,
  'cumulative_day': 92,
  'stage': 'GROW OUT',
  'population': 112219,
  'weight_g': 11.03,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.9,
  'biomass_kg': 1237.62,
  'feed_rate_pct': 3.2,
  'daily_feed_kg': 39.6,
  'feedings_per_day': 4,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 1370.01,
  'estimated_fcr': 1.11,
  'crop_fcr': 1.12,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 39.6}]},
 {'phase': 'engorda',
  'phase_day': 48,
  'cumulative_day': 93,
  'stage': 'GROW OUT',
  'population': 111850,
  'weight_g': 11.24,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.8,
  'biomass_kg': 1256.72,
  'feed_rate_pct': 3.2,
  'daily_feed_kg': 40.21,
  'feedings_per_day': 4,
  'survival_pct': 87.0,
  'cumulative_feed_kg': 1410.22,
  'estimated_fcr': 1.12,
  'crop_fcr': 1.13,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 40.21}]},
 {'phase': 'engorda',
  'phase_day': 49,
  'cumulative_day': 94,
  'stage': 'GROW OUT',
  'population': 111481,
  'weight_g': 11.44,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.8,
  'biomass_kg': 1275.66,
  'feed_rate_pct': 3.2,
  'daily_feed_kg': 40.82,
  'feedings_per_day': 4,
  'survival_pct': 86.0,
  'cumulative_feed_kg': 1451.04,
  'estimated_fcr': 1.14,
  'crop_fcr': 1.15,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 40.82}]},
 {'phase': 'engorda',
  'phase_day': 50,
  'cumulative_day': 95,
  'stage': 'GROW OUT',
  'population': 111112,
  'weight_g': 11.65,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.8,
  'biomass_kg': 1294.45,
  'feed_rate_pct': 3.2,
  'daily_feed_kg': 41.42,
  'feedings_per_day': 4,
  'survival_pct': 86.0,
  'cumulative_feed_kg': 1492.47,
  'estimated_fcr': 1.15,
  'crop_fcr': 1.16,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.42}]},
 {'phase': 'engorda',
  'phase_day': 51,
  'cumulative_day': 96,
  'stage': 'GROW OUT',
  'population': 110743,
  'weight_g': 11.86,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.7,
  'biomass_kg': 1313.09,
  'feed_rate_pct': 3.2,
  'daily_feed_kg': 42.02,
  'feedings_per_day': 4,
  'survival_pct': 86.0,
  'cumulative_feed_kg': 1534.49,
  'estimated_fcr': 1.17,
  'crop_fcr': 1.18,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.02}]},
 {'phase': 'engorda',
  'phase_day': 52,
  'cumulative_day': 97,
  'stage': 'GROW OUT',
  'population': 110374,
  'weight_g': 12.06,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.7,
  'biomass_kg': 1331.58,
  'feed_rate_pct': 3.1,
  'daily_feed_kg': 41.28,
  'feedings_per_day': 4,
  'survival_pct': 85.0,
  'cumulative_feed_kg': 1575.76,
  'estimated_fcr': 1.18,
  'crop_fcr': 1.19,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.28}]},
 {'phase': 'engorda',
  'phase_day': 53,
  'cumulative_day': 98,
  'stage': 'GROW OUT',
  'population': 110005,
  'weight_g': 12.27,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.7,
  'biomass_kg': 1349.91,
  'feed_rate_pct': 3.1,
  'daily_feed_kg': 41.85,
  'feedings_per_day': 4,
  'survival_pct': 85.0,
  'cumulative_feed_kg': 1617.61,
  'estimated_fcr': 1.2,
  'crop_fcr': 1.21,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.85}]},
 {'phase': 'engorda',
  'phase_day': 54,
  'cumulative_day': 99,
  'stage': 'GROW OUT',
  'population': 109635,
  'weight_g': 12.48,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.7,
  'biomass_kg': 1368.09,
  'feed_rate_pct': 3.1,
  'daily_feed_kg': 42.41,
  'feedings_per_day': 4,
  'survival_pct': 85.0,
  'cumulative_feed_kg': 1660.02,
  'estimated_fcr': 1.21,
  'crop_fcr': 1.22,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.41}]},
 {'phase': 'engorda',
  'phase_day': 55,
  'cumulative_day': 100,
  'stage': 'GROW OUT',
  'population': 109266,
  'weight_g': 12.69,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.6,
  'biomass_kg': 1386.12,
  'feed_rate_pct': 3.1,
  'daily_feed_kg': 42.97,
  'feedings_per_day': 4,
  'survival_pct': 85.0,
  'cumulative_feed_kg': 1702.99,
  'estimated_fcr': 1.23,
  'crop_fcr': 1.24,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.97}]},
 {'phase': 'engorda',
  'phase_day': 56,
  'cumulative_day': 101,
  'stage': 'GROW OUT',
  'population': 108897,
  'weight_g': 12.89,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.6,
  'biomass_kg': 1404.0,
  'feed_rate_pct': 3.1,
  'daily_feed_kg': 43.52,
  'feedings_per_day': 4,
  'survival_pct': 84.0,
  'cumulative_feed_kg': 1746.52,
  'estimated_fcr': 1.24,
  'crop_fcr': 1.25,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 43.52}]},
 {'phase': 'engorda',
  'phase_day': 57,
  'cumulative_day': 102,
  'stage': 'GROW OUT',
  'population': 108528,
  'weight_g': 13.1,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.6,
  'biomass_kg': 1421.72,
  'feed_rate_pct': 3.0,
  'daily_feed_kg': 42.65,
  'feedings_per_day': 4,
  'survival_pct': 84.0,
  'cumulative_feed_kg': 1789.17,
  'estimated_fcr': 1.26,
  'crop_fcr': 1.27,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.65}]},
 {'phase': 'engorda',
  'phase_day': 58,
  'cumulative_day': 103,
  'stage': 'GROW OUT',
  'population': 108159,
  'weight_g': 13.31,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.6,
  'biomass_kg': 1439.29,
  'feed_rate_pct': 3.0,
  'daily_feed_kg': 43.18,
  'feedings_per_day': 4,
  'survival_pct': 84.0,
  'cumulative_feed_kg': 1832.35,
  'estimated_fcr': 1.27,
  'crop_fcr': 1.28,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 43.18}]},
 {'phase': 'engorda',
  'phase_day': 59,
  'cumulative_day': 104,
  'stage': 'GROW OUT',
  'population': 107790,
  'weight_g': 13.51,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.5,
  'biomass_kg': 1456.7,
  'feed_rate_pct': 3.0,
  'daily_feed_kg': 43.7,
  'feedings_per_day': 4,
  'survival_pct': 83.0,
  'cumulative_feed_kg': 1876.05,
  'estimated_fcr': 1.29,
  'crop_fcr': 1.3,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 43.7}]},
 {'phase': 'engorda',
  'phase_day': 60,
  'cumulative_day': 105,
  'stage': 'GROW OUT',
  'population': 107421,
  'weight_g': 13.72,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.5,
  'biomass_kg': 1473.96,
  'feed_rate_pct': 3.0,
  'daily_feed_kg': 44.22,
  'feedings_per_day': 4,
  'survival_pct': 83.0,
  'cumulative_feed_kg': 1920.27,
  'estimated_fcr': 1.3,
  'crop_fcr': 1.31,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 44.22}]},
 {'phase': 'engorda',
  'phase_day': 61,
  'cumulative_day': 106,
  'stage': 'GROW OUT',
  'population': 107051,
  'weight_g': 13.93,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.5,
  'biomass_kg': 1491.07,
  'feed_rate_pct': 3.0,
  'daily_feed_kg': 44.73,
  'feedings_per_day': 4,
  'survival_pct': 83.0,
  'cumulative_feed_kg': 1965.0,
  'estimated_fcr': 1.32,
  'crop_fcr': 1.33,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 44.73}]},
 {'phase': 'engorda',
  'phase_day': 62,
  'cumulative_day': 107,
  'stage': 'GROW OUT',
  'population': 106682,
  'weight_g': 14.14,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.5,
  'biomass_kg': 1508.03,
  'feed_rate_pct': 2.8,
  'daily_feed_kg': 42.22,
  'feedings_per_day': 4,
  'survival_pct': 83.0,
  'cumulative_feed_kg': 2007.22,
  'estimated_fcr': 1.33,
  'crop_fcr': 1.34,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.22}]},
 {'phase': 'engorda',
  'phase_day': 63,
  'cumulative_day': 108,
  'stage': 'GROW OUT',
  'population': 106313,
  'weight_g': 14.34,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.4,
  'biomass_kg': 1524.83,
  'feed_rate_pct': 2.8,
  'daily_feed_kg': 42.7,
  'feedings_per_day': 4,
  'survival_pct': 82.0,
  'cumulative_feed_kg': 2049.92,
  'estimated_fcr': 1.34,
  'crop_fcr': 1.35,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.7}]},
 {'phase': 'engorda',
  'phase_day': 64,
  'cumulative_day': 109,
  'stage': 'GROW OUT',
  'population': 105944,
  'weight_g': 14.55,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.4,
  'biomass_kg': 1541.49,
  'feed_rate_pct': 2.8,
  'daily_feed_kg': 43.16,
  'feedings_per_day': 4,
  'survival_pct': 82.0,
  'cumulative_feed_kg': 2093.08,
  'estimated_fcr': 1.36,
  'crop_fcr': 1.37,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 43.16}]},
 {'phase': 'engorda',
  'phase_day': 65,
  'cumulative_day': 110,
  'stage': 'GROW OUT',
  'population': 105575,
  'weight_g': 14.76,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.4,
  'biomass_kg': 1557.98,
  'feed_rate_pct': 2.8,
  'daily_feed_kg': 43.62,
  'feedings_per_day': 4,
  'survival_pct': 82.0,
  'cumulative_feed_kg': 2136.7,
  'estimated_fcr': 1.37,
  'crop_fcr': 1.38,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 43.62}]},
 {'phase': 'engorda',
  'phase_day': 66,
  'cumulative_day': 111,
  'stage': 'GROW OUT',
  'population': 105206,
  'weight_g': 14.96,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.4,
  'biomass_kg': 1574.33,
  'feed_rate_pct': 2.8,
  'daily_feed_kg': 44.08,
  'feedings_per_day': 4,
  'survival_pct': 81.0,
  'cumulative_feed_kg': 2180.79,
  'estimated_fcr': 1.39,
  'crop_fcr': 1.39,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 44.08}]},
 {'phase': 'engorda',
  'phase_day': 67,
  'cumulative_day': 112,
  'stage': 'GROW OUT',
  'population': 104837,
  'weight_g': 15.17,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.4,
  'biomass_kg': 1590.52,
  'feed_rate_pct': 2.6,
  'daily_feed_kg': 41.35,
  'feedings_per_day': 4,
  'survival_pct': 81.0,
  'cumulative_feed_kg': 2222.14,
  'estimated_fcr': 1.4,
  'crop_fcr': 1.4,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.35}]},
 {'phase': 'engorda',
  'phase_day': 68,
  'cumulative_day': 113,
  'stage': 'GROW OUT',
  'population': 104467,
  'weight_g': 15.38,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.3,
  'biomass_kg': 1606.56,
  'feed_rate_pct': 2.6,
  'daily_feed_kg': 41.77,
  'feedings_per_day': 4,
  'survival_pct': 81.0,
  'cumulative_feed_kg': 2263.91,
  'estimated_fcr': 1.41,
  'crop_fcr': 1.42,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 41.77}]},
 {'phase': 'engorda',
  'phase_day': 69,
  'cumulative_day': 114,
  'stage': 'GROW OUT',
  'population': 104098,
  'weight_g': 15.59,
  'daily_growth_g': 0.2071,
  'growth_rate_pct_day': 1.3,
  'biomass_kg': 1622.45,
  'feed_rate_pct': 2.6,
  'daily_feed_kg': 42.18,
  'feedings_per_day': 4,
  'survival_pct': 81.0,
  'cumulative_feed_kg': 2306.09,
  'estimated_fcr': 1.42,
  'crop_fcr': 1.43,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 42.18}]},
 {'phase': 'engorda',
  'phase_day': 70,
  'cumulative_day': 115,
  'stage': 'GROW OUT',
  'population': 103360,
  'weight_g': 16.0,
  'daily_growth_g': 0.4143,
  'growth_rate_pct_day': 2.6,
  'biomass_kg': 1653.76,
  'feed_rate_pct': 2.4,
  'daily_feed_kg': 39.69,
  'feedings_per_day': 4,
  'survival_pct': 80.0,
  'cumulative_feed_kg': 2345.78,
  'estimated_fcr': 1.42,
  'crop_fcr': 1.43,
  'mixes': [{'label': 'Engorda 2,4 mm', 'kg': 39.69}]}]
NURSERY_FEED_TIMES = ['06:00', '08:00', '10:00', '12:00', '14:00', '16:00', '18:00', '20:00', '22:00', '00:00', '02:00', '04:00']


def get_nursery_protocol_row(pl_stage: int | None):
    if pl_stage is None:
        return None
    if pl_stage <= NURSERY_PROTOCOL_ROWS[0]['pl_stage']:
        return NURSERY_PROTOCOL_ROWS[0]
    if pl_stage >= NURSERY_PROTOCOL_ROWS[-1]['pl_stage']:
        return NURSERY_PROTOCOL_ROWS[-1]
    return next((row for row in NURSERY_PROTOCOL_ROWS if row['pl_stage'] == pl_stage), None)


def grams_to_kg(value):
    return round((value or 0) / 1000.0, 3)


def nursery_score_adjustment_pct(score):
    """Sugestão padrão de ajuste da ração com base no score intestinal."""
    if score is None:
        return 0.0
    try:
        score = float(score)
    except (TypeError, ValueError):
        return 0.0
    if 0 <= score <= 1.0:
        return 30.0
    if score <= 2.0:
        return 20.0
    if score <= 3.0:
        return 10.0
    if score <= 3.5:
        return 0.0
    if score <= 4.0:
        return -10.0
    return 0.0


def nursery_adjustment_pct_factor(adjustment_pct):
    try:
        adjustment_pct = float(adjustment_pct or 0)
    except (TypeError, ValueError):
        adjustment_pct = 0.0
    return 1.0 + (adjustment_pct / 100.0)


def nursery_score_factor(score):
    return nursery_adjustment_pct_factor(nursery_score_adjustment_pct(score))


def nursery_record_adjustment_pct(record):
    if record is None:
        return 0.0
    if record.score_adjustment_pct is not None:
        return float(record.score_adjustment_pct)
    return nursery_score_adjustment_pct(record.intestinal_score)


def nursery_score_factor_label(factor):
    pct = int(round((factor - 1.0) * 100))
    if pct > 0:
        return f'+{pct}%'
    if pct < 0:
        return f'{pct}%'
    return 'sem ajuste'


def nursery_adjustment_pct_label(adjustment_pct):
    try:
        adjustment_pct = float(adjustment_pct or 0)
    except (TypeError, ValueError):
        adjustment_pct = 0.0
    if adjustment_pct > 0:
        return f'+{adjustment_pct:g}%'
    if adjustment_pct < 0:
        return f'{adjustment_pct:g}%'
    return 'sem ajuste'


def nursery_cumulative_adjustments(lot_id: int | None, target_date: date):
    if not lot_id or not target_date:
        return {'factor': 1.0, 'events': []}

    records = NurseryFeeding.query.filter(
        NurseryFeeding.lot_id == lot_id,
        NurseryFeeding.feed_date < target_date,
        or_(
            NurseryFeeding.intestinal_score.isnot(None),
            NurseryFeeding.score_adjustment_pct.isnot(None),
        ),
    ).order_by(NurseryFeeding.feed_date.asc(), NurseryFeeding.id.asc()).all()

    cumulative_factor = 1.0
    events = []
    for record in records:
        daily_adjustment_pct = nursery_record_adjustment_pct(record)
        daily_factor = nursery_adjustment_pct_factor(daily_adjustment_pct)
        cumulative_factor *= daily_factor
        events.append({
            'date': record.feed_date,
            'score': record.intestinal_score,
            'adjustment_pct': daily_adjustment_pct,
            'adjustment_label': nursery_adjustment_pct_label(daily_adjustment_pct),
            'factor': daily_factor,
            'factor_label': nursery_score_factor_label(daily_factor),
            'cumulative_factor': cumulative_factor,
            'cumulative_label': nursery_score_factor_label(cumulative_factor),
        })
    return {'factor': cumulative_factor, 'events': events}


def build_even_schedule(total_day_g: int, feedings_per_day: int):
    if not feedings_per_day or total_day_g <= 0:
        return []
    base_value = total_day_g // feedings_per_day
    remainder = total_day_g % feedings_per_day
    return [base_value + (1 if idx < remainder else 0) for idx in range(feedings_per_day)]


def build_nursery_management_note_block(entry, mix_label=None):
    lines = [
        '[Integração berçário]',
        'Integração automática da alimentação de berçário.',
        f'Origem ID: {entry.id}',
    ]
    if mix_label:
        lines.append(f'Produto do mix: {mix_label}')
    if entry.intestinal_score is not None:
        lines.append(f'Score intestinal: {entry.intestinal_score}')
    if entry.score_adjustment_pct is not None:
        lines.append(f'Ajuste de ração para o próximo dia: {nursery_adjustment_pct_label(entry.score_adjustment_pct)}')
    if (entry.notes or '').strip():
        lines.append(f'Observações do berçário: {(entry.notes or '').strip()}')
    lines.append('[/Integração berçário]')
    return '\n'.join(lines)


def nursery_management_source_marker(entry_id):
    return f'Origem ID: {entry_id}'


def nursery_management_records_for_entry(entry):
    query = DailyManagement.query.filter(
        DailyManagement.manage_date == entry.feed_date,
        DailyManagement.unit_id == entry.unit_id,
        DailyManagement.notes.contains(nursery_management_source_marker(entry.id)),
    )
    if entry.lot_id is None:
        query = query.filter(DailyManagement.lot_id.is_(None))
    else:
        query = query.filter(DailyManagement.lot_id == entry.lot_id)
    return query.order_by(DailyManagement.id.asc()).all()


def delete_management_record_with_inventory(record):
    movement = get_management_feed_movement(record.id)
    if movement:
        db.session.delete(movement)
    db.session.delete(record)


def delete_nursery_management_records(entry):
    for record in nursery_management_records_for_entry(entry):
        delete_management_record_with_inventory(record)


def nursery_feed_alias_tokens(label: str):
    normalized = normalize_text(label)
    replacements = {
        'nutrisphera': 'nutrisfera',
        'bercario': 'bercario',
        'bercário': 'bercario',
    }
    tokens = set(normalized.split())
    expanded = set(tokens)
    for token in list(tokens):
        if token in replacements:
            expanded.add(replacements[token])
    if 'triturada' in expanded:
        expanded.add('triturado')
    return expanded


def nursery_protocol_product_names():
    names = set()
    for row in NURSERY_PROTOCOL_ROWS:
        for item in row.get('mixes', []):
            name = (item.get('label') or '').strip()
            if name:
                names.add(normalize_text(name))
    names.add(normalize_text('Ração berçário'))
    return names


def is_auto_nursery_protocol_product(product):
    if not product:
        return False
    brand_norm = normalize_text(product.brand or '')
    feed_type_norm = normalize_text(product.feed_type or '')
    if brand_norm in nursery_protocol_product_names():
        return True
    if brand_norm.startswith('mem ') or brand_norm == 'mem':
        return True
    if feed_type_norm in {'bercario', 'bercário'} and (
        brand_norm in nursery_protocol_product_names() or brand_norm.startswith('mem ') or brand_norm == 'racao bercario'
    ):
        return True
    return False


def numeric_search_text(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '')
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    return value.lower().replace(',', '.')


def numeric_ranges_from_text(text_value: str):
    # Não usa normalize_text aqui, porque ele remove o hífen de faixas como 300-500.
    text_value = numeric_search_text(text_value)
    ranges = []
    for start, end in re.findall(r'(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)', text_value):
        try:
            a = float(start)
            b = float(end)
        except ValueError:
            continue
        ranges.append((min(a, b), max(a, b)))
    return ranges


def numeric_values_from_text(text_value: str):
    # Mantém casas decimais: 0.45 mm também vira 450 micra para casar com 300-500.
    text_value = numeric_search_text(text_value)
    values = []
    for value in re.findall(r'\d+(?:\.\d+)?', text_value):
        try:
            parsed = float(value)
        except ValueError:
            continue
        values.append(parsed)
        if 0 < parsed < 10:
            values.append(parsed * 1000)
    return values


def product_matches_nursery_range(label: str, product_text: str):
    ranges = numeric_ranges_from_text(label)
    if not ranges:
        return False
    values = numeric_values_from_text(product_text)
    return any(start <= value <= end for start, end in ranges for value in values)


def nursery_product_stock(product_id):
    if not product_id:
        return 0
    try:
        return available_stock_for_product(product_id)
    except Exception:
        return 0


def nursery_product_match_score(label: str, product) -> tuple[int, bool]:
    label = (label or '').strip()
    normalized_label = normalize_text(label)
    label_tokens = nursery_feed_alias_tokens(label)
    numeric_tokens = {token for token in label_tokens if token.replace('.', '', 1).isdigit()}

    product_text = f'{product.brand} {product.feed_type} {product.technical_summary or ""}'
    product_tokens = nursery_feed_alias_tokens(product_text)
    product_numbers = {token for token in product_tokens if token.replace('.', '', 1).isdigit()}
    product_is_nursery = 'bercario' in product_tokens or is_auto_nursery_protocol_product(product)

    score = len(label_tokens.intersection(product_tokens))

    if normalized_label == normalize_text(product_text) or normalized_label == normalize_text(product.full_name):
        score += 8

    if product_matches_nursery_range(label, product_text):
        score += 20

    if numeric_tokens and product_numbers and numeric_tokens.intersection(product_numbers):
        score += 4

    if 'nutrisfera' in label_tokens and 'nutrisfera' in product_tokens:
        score += 4

    if 'triturada' in label_tokens.intersection(product_tokens) or 'triturado' in label_tokens.intersection(product_tokens):
        score += 2

    if product_is_nursery and not is_auto_nursery_protocol_product(product):
        score += 8

    if product.active:
        score += 2

    if nursery_product_stock(product.id) > 0:
        score += 2

    return score, product_is_nursery


def find_or_create_nursery_feed_product(label: str, exclude_product_id=None, create_missing=True):
    label = (label or '').strip()
    normalized_label = normalize_text(label)
    if not normalized_label:
        return None

    protocol_label = normalized_label in nursery_protocol_product_names() or normalized_label.startswith('mem ')
    products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()

    scored_real_nursery = []
    scored_any = []

    for product in products:
        if exclude_product_id is not None and product.id == exclude_product_id:
            continue

        score, product_is_nursery = nursery_product_match_score(label, product)
        is_auto_protocol = is_auto_nursery_protocol_product(product)

        if product_is_nursery and not is_auto_protocol:
            scored_real_nursery.append((score, nursery_product_stock(product.id), product.active, product.full_name.lower(), product))

        # Produtos técnicos do protocolo (MeM 200-300, MeM 300-500 etc.) não devem ganhar
        # prioridade sobre as rações reais cadastradas no estoque.
        if not (protocol_label and is_auto_protocol):
            scored_any.append((score, nursery_product_stock(product.id), product.active, product.full_name.lower(), product))

    if protocol_label and scored_real_nursery:
        scored_real_nursery.sort(key=lambda item: (item[0], item[2], item[1], item[3]), reverse=True)
        return scored_real_nursery[0][4]

    if scored_any:
        scored_any.sort(key=lambda item: (item[0], item[2], item[1], item[3]), reverse=True)
        best_score, _, _, _, best_product = scored_any[0]
        if best_score >= (5 if protocol_label else 3):
            return best_product

    if not create_missing:
        return None

    # Se a linha da tabela traz um nome técnico (MeM 300-500), não cria mais produto
    # com esse nome. Usa um nome genérico até o usuário cadastrar a ração real.
    product_name = 'Ração berçário' if protocol_label else label
    product = FeedProduct(brand=product_name, feed_type='', active=True, notes='Criado automaticamente pelo protocolo de berçário.')
    db.session.add(product)
    db.session.flush()
    return product


def resolve_nursery_mix_label(label: str) -> str:
    product = find_or_create_nursery_feed_product(label, create_missing=False)
    if product and not is_auto_nursery_protocol_product(product):
        return product.full_name
    return 'Ração berçário'


def consolidate_feed_mixes(mixes):
    totals = {}
    order = []
    for item in mixes or []:
        label = item.get('label') or 'Ração berçário'
        grams = int(round(item.get('grams') or 0))
        if grams <= 0:
            continue
        if label not in totals:
            totals[label] = 0
            order.append(label)
        totals[label] += grams
    return [{'label': label, 'grams': totals[label]} for label in order]


def scale_nursery_mixes(mixes, quantity_kg):
    target_total_g = int(round((quantity_kg or 0) * 1000))
    if target_total_g <= 0:
        return []
    source_total_g = sum(int(item.get('grams') or 0) for item in mixes)
    if source_total_g <= 0:
        return []
    scaled = []
    distributed = 0
    fractions = []
    for idx, item in enumerate(mixes):
        exact_value = (int(item.get('grams') or 0) * target_total_g) / source_total_g
        whole = int(exact_value)
        distributed += whole
        scaled.append({'label': item.get('label'), 'grams': whole})
        fractions.append((exact_value - whole, idx))
    remainder = target_total_g - distributed
    for _, idx in sorted(fractions, reverse=True)[:remainder]:
        scaled[idx]['grams'] += 1
    return [item for item in scaled if item['grams'] > 0]


def build_nursery_protocol_for_date(lot, unit, target_date: date | None = None, cumulative_factor=1.0, correction_events=None):
    target_date = target_date or date.today()
    if not lot or not unit or not lot.start_date or lot.entry_pl_stage is None:
        return None

    days_since_start = max((target_date - lot.start_date).days, 0)
    min_stage = NURSERY_PROTOCOL_ROWS[0]['pl_stage']
    max_stage = NURSERY_PROTOCOL_ROWS[-1]['pl_stage']
    stage_today = min(max_stage, max(min_stage, lot.entry_pl_stage + days_since_start))
    row = get_nursery_protocol_row(stage_today)
    if not row:
        return None

    factor = (lot.initial_count or 0) / float(NURSERY_PROTOCOL_BASE_POPULATION or 1)

    def scaled(value):
        return int(round((value or 0) * factor))

    base_total_day_g = scaled(row['total_day_g'])
    correction_factor = cumulative_factor or 1.0
    correction_label = nursery_score_factor_label(correction_factor)
    correction_events = correction_events or []

    projected_population = int(round((lot.initial_count or 0) * (row['survival_pct'] / 100.0)))
    biomass_kg = round((projected_population * row['individual_weight_g']) / 1000.0, 2)
    base_mixes = [
        {'label': resolve_nursery_mix_label(item.get('label', 'Ração berçário')), 'grams': scaled(item.get('grams', 0))}
        for item in row.get('mixes', [])
    ]
    base_mixes = consolidate_feed_mixes(base_mixes)
    mixes = [
        {'label': item['label'], 'grams': int(round(item['grams'] * correction_factor))}
        for item in base_mixes
    ]
    mixes = consolidate_feed_mixes(mixes)
    total_day_g = sum(item['grams'] for item in mixes) if mixes else int(round(base_total_day_g * correction_factor))

    feedings_per_day = row['feedings_per_day']
    portion_values = build_even_schedule(total_day_g, feedings_per_day)
    per_feeding_g = int(round(total_day_g / feedings_per_day)) if feedings_per_day else 0
    schedule = []
    for idx, time_label in enumerate(NURSERY_FEED_TIMES[:feedings_per_day]):
        schedule.append({'time': time_label, 'grams': portion_values[idx] if idx < len(portion_values) else per_feeding_g})

    message_lines = [
        f"*{unit.name}* — Lote {lot.lot_code}",
        f"Data: {target_date.strftime('%d/%m/%Y')}",
        f"Estágio do dia: PL{stage_today}",
        f"População estimada: {projected_population:,} PL".replace(',', '.'),
        f"Base: planilha 160.000 PL, recalculada proporcionalmente ao lote",
    ]
    if correction_events:
        last_event = correction_events[-1]
        message_lines.append(f"Correção acumulada ativa: {correction_label} sobre o protocolo base")
        message_lines.append(f"Último score usado: {float(last_event['score']):.1f} em {last_event['date'].strftime('%d/%m/%Y')} ({last_event.get('adjustment_label', last_event['factor_label'])})".replace('.', ','))
    elif correction_factor != 1.0:
        message_lines.append(f"Correção acumulada ativa: {correction_label} sobre o protocolo base")
    message_lines.extend([
        f"Total base: {base_total_day_g:,} g".replace(',', '.'),
        f"Total corrigido do dia: {total_day_g:,} g".replace(',', '.'),
        '',
        '*Mix do dia*',
    ])
    for item in mixes or [{'label': 'Sem mistura cadastrada', 'grams': 0}]:
        message_lines.append(f"- {item['label']}: {item['grams']:,} g".replace(',', '.'))
    message_lines.extend(['', '*Porções a cada 2 horas*'])
    for item in schedule:
        message_lines.append(f"- {item['time']} — {item['grams']:,} g".replace(',', '.'))

    return {
        'unit': unit,
        'lot': lot,
        'target_date': target_date,
        'day_index': days_since_start + 1,
        'stage_today': stage_today,
        'base_row': row,
        'projected_population': projected_population,
        'biomass_kg': biomass_kg,
        'feed_rate_pct': row['feed_rate_pct'],
        'base_total_day_g': base_total_day_g,
        'base_total_day_kg': grams_to_kg(base_total_day_g),
        'score_factor': correction_factor,
        'score_factor_label': correction_label,
        'score_adjustment_pct': int(round((correction_factor - 1.0) * 100)),
        'correction_events': correction_events,
        'correction_source_date': correction_events[-1]['date'] if correction_events else None,
        'intestinal_score': correction_events[-1]['score'] if correction_events else None,
        'total_day_g': total_day_g,
        'total_day_kg': grams_to_kg(total_day_g),
        'mixes': mixes,
        'feedings_per_day': feedings_per_day,
        'per_feeding_g': per_feeding_g,
        'per_feeding_min_g': min((item['grams'] for item in schedule), default=0),
        'per_feeding_max_g': max((item['grams'] for item in schedule), default=0),
        'schedule': schedule,
        'message_text': '\n'.join(message_lines),
    }


def build_nursery_digest_for_date(target_date: date | None = None):
    target_date = target_date or date.today()
    plans = []
    nursery_units = Unit.query.filter_by(active=True, phase='bercario').order_by(Unit.name).all()
    for unit in nursery_units:
        lot = active_lot_for_unit(unit.id, on_date=target_date)
        if not lot or lot.status != 'ativo':
            continue
        entry = NurseryFeeding.query.filter_by(feed_date=target_date, unit_id=unit.id).order_by(NurseryFeeding.id.desc()).first()
        adjustment = nursery_cumulative_adjustments(lot.id, target_date)
        plan = build_nursery_protocol_for_date(
            lot,
            unit,
            target_date=target_date,
            cumulative_factor=adjustment['factor'],
            correction_events=adjustment['events'],
        )
        if plan:
            plan['existing_entry'] = entry
            plans.append(plan)
    return plans


def sync_nursery_feed_to_management(entry):
    if not entry:
        return []

    delete_nursery_management_records(entry)

    unit = db.session.get(Unit, entry.unit_id) if entry.unit_id else None
    lot = db.session.get(Lot, entry.lot_id) if entry.lot_id else None
    adjustment = nursery_cumulative_adjustments(entry.lot_id, entry.feed_date)
    plan = build_nursery_protocol_for_date(
        lot,
        unit,
        target_date=entry.feed_date,
        cumulative_factor=adjustment['factor'],
        correction_events=adjustment['events'],
    ) if unit and lot else None
    scaled_mixes = scale_nursery_mixes(plan.get('mixes', []) if plan else [], entry.quantity_kg)

    if not scaled_mixes:
        fallback_product = find_or_create_nursery_feed_product('Ração berçário')
        scaled_mixes = [{'label': fallback_product.full_name if fallback_product else 'Ração berçário', 'grams': int(round((entry.quantity_kg or 0) * 1000))}]

    created_records = []
    for item in scaled_mixes:
        offered_kg = grams_to_kg(item['grams'])
        feed_product = find_or_create_nursery_feed_product(item['label'])
        management = DailyManagement(
            manage_date=entry.feed_date,
            unit_id=entry.unit_id,
            lot_id=entry.lot_id,
            feed_product_id=feed_product.id if feed_product else None,
            feed_offered_kg=offered_kg,
            feed_consumed_kg=offered_kg,
            mortality_qty=0,
            average_weight_g=None,
            estimated_biomass_kg=None,
            notes=build_nursery_management_note_block(entry, mix_label=item['label']),
            updated_at=datetime.utcnow(),
        )
        db.session.add(management)
        db.session.flush()
        sync_management_feed_movement(management, feed_product, offered_kg)
        created_records.append(management)
    return created_records

def combine_monitor_datetime(record):
    return datetime.combine(record.monitor_date, record.monitor_time or time.min)


def suggest_unit_code(name: str) -> str:
    raw = ''.join(ch if ch.isalnum() else '_' for ch in (name or '').upper()).strip('_')
    raw = '_'.join(part for part in raw.split('_') if part)
    return raw[:50] or f'VIVEIRO_{int(datetime.utcnow().timestamp())}'


def user_can_manage_units(user) -> bool:
    return getattr(user, 'is_authenticated', False) and getattr(user, 'role', None) in {'admin', 'gerente'}


def format_reference_range(min_value, max_value, unit=''):
    def fmt(value):
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(round(value, 2)).replace('.', ',')

    unit_suffix = f' {unit}' if unit else ''
    if min_value is not None and max_value is not None:
        return f'{fmt(min_value)} a {fmt(max_value)}{unit_suffix}'
    if min_value is not None:
        return f'Mín. {fmt(min_value)}{unit_suffix}'
    if max_value is not None:
        return f'Máx. {fmt(max_value)}{unit_suffix}'
    return 'Sem faixa definida'


def format_parameter_value(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(round(value, 2)).replace('.', ',')


def get_water_reference_config():
    config = WaterReferenceConfig.query.order_by(WaterReferenceConfig.id.asc()).first()
    if config:
        return config

    config = WaterReferenceConfig(
        od_min=TARGET_OD_MIN,
        ph_min=TARGET_PH_MIN,
        ph_max=TARGET_PH_MAX,
        temperature_min=TARGET_TEMP_MIN,
        temperature_max=TARGET_TEMP_MAX,
        salinity_min=TARGET_SALINITY_MIN,
        salinity_max=TARGET_SALINITY_MAX,
        transparency_min=TARGET_TRANSPARENCY_MIN,
        transparency_max=TARGET_TRANSPARENCY_MAX,
        ammonia_max=TARGET_AMMONIA_MAX,
        nitrite_max=TARGET_NITRITE_MAX,
    )
    db.session.add(config)
    db.session.commit()
    return config


def water_alerts_for_record(rec, config=None):
    if not rec:
        return []
    config = config or get_water_reference_config()
    alerts = []
    for spec in WATER_PARAMETER_SPECS:
        value = getattr(rec, spec['field'])
        if value is None:
            continue
        min_value = getattr(config, spec['min_attr'])
        max_value = getattr(config, spec['max_attr'])
        if min_value is not None and value < min_value:
            alerts.append({
                'field': spec['field'],
                'label': spec['label'],
                'unit': spec['unit'],
                'value': value,
                'value_label': format_parameter_value(value),
                'min_value': min_value,
                'max_value': max_value,
                'reference_text': format_reference_range(min_value, max_value, spec['unit']),
                'direction': 'low',
                'message': spec['short_status_low'],
            })
        elif max_value is not None and value > max_value:
            alerts.append({
                'field': spec['field'],
                'label': spec['label'],
                'unit': spec['unit'],
                'value': value,
                'value_label': format_parameter_value(value),
                'min_value': min_value,
                'max_value': max_value,
                'reference_text': format_reference_range(min_value, max_value, spec['unit']),
                'direction': 'high',
                'message': spec['short_status_high'],
            })
    return alerts


def build_water_alert_rows(records, config=None):
    config = config or get_water_reference_config()
    rows = []
    for record in records:
        alerts = water_alerts_for_record(record, config)
        for alert in alerts:
            rows.append({
                'unit_name': record.unit.name if record.unit else 'Sem unidade',
                'phase_label': phase_label(record.unit.phase) if record.unit else '',
                'lot_code': record.lot.lot_code if record.lot else 'Sem lote',
                'monitor_date': record.monitor_date,
                'monitor_time': record.monitor_time,
                'shift_label': shift_label(record.shift),
                'parameter_label': alert['label'],
                'reading_value': f"{alert['value_label']} {alert['unit']}".strip(),
                'reference_text': alert['reference_text'],
                'message': alert['message'],
            })
    rows.sort(key=lambda row: (row['monitor_date'], row['monitor_time'] or time.min, row['unit_name'], row['parameter_label']), reverse=True)
    return rows


def build_reference_summary(config=None):
    config = config or get_water_reference_config()
    summary = []
    for spec in WATER_PARAMETER_SPECS:
        summary.append({
            'label': spec['label'],
            'unit': spec['unit'],
            'range_text': format_reference_range(getattr(config, spec['min_attr']), getattr(config, spec['max_attr']), spec['unit']),
        })
    return summary


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
    dialect = db.engine.dialect.name
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    for model in (ProtocolDocument, FarmDocument, WaterReferenceConfig, FeedProduct, SupplyProduct, SupplyInventory, ManagementSupplyUsage, LotUnitAllocation, FixedCost, NurseryFeeding):
        table_name = model.__table__.name
        if table_name not in tables:
            model.__table__.create(bind=db.engine)
            tables.add(table_name)

    def refresh_inspector():
        return inspect(db.engine)

    def get_columns(table_name: str):
        return {col['name'] for col in refresh_inspector().get_columns(table_name)} if table_name in tables else set()

    def run_sql(sql_sqlite: str, sql_pg: str | None = None):
        sql = sql_sqlite if dialect == 'sqlite' or sql_pg is None else sql_pg
        with db.engine.begin() as conn:
            conn.execute(text(sql))

    def add_column_if_missing(table_name: str, columns_cache: set[str], column_name: str, sql_sqlite: str, sql_pg: str | None = None):
        if table_name not in tables or column_name in columns_cache:
            return
        run_sql(sql_sqlite, sql_pg)
        columns_cache.add(column_name)

    if 'user' in tables:
        user_columns = get_columns('user')
        add_column_if_missing('user', user_columns, 'email', 'ALTER TABLE user ADD COLUMN email VARCHAR(120)', 'ALTER TABLE "user" ADD COLUMN email VARCHAR(120)')
        add_column_if_missing('user', user_columns, 'last_login_at', 'ALTER TABLE user ADD COLUMN last_login_at DATETIME', 'ALTER TABLE "user" ADD COLUMN last_login_at TIMESTAMP')
        add_column_if_missing('user', user_columns, 'created_at', f"ALTER TABLE user ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE "user" ADD COLUMN created_at TIMESTAMP')
        add_column_if_missing('user', user_columns, 'is_active_user', 'ALTER TABLE user ADD COLUMN is_active_user BOOLEAN DEFAULT 1', 'ALTER TABLE "user" ADD COLUMN is_active_user BOOLEAN DEFAULT TRUE')

    if 'lot' in tables:
        lot_columns = get_columns('lot')
        add_column_if_missing('lot', lot_columns, 'end_date', 'ALTER TABLE lot ADD COLUMN end_date DATE', 'ALTER TABLE lot ADD COLUMN end_date DATE')
        add_column_if_missing('lot', lot_columns, 'closed_reason', 'ALTER TABLE lot ADD COLUMN closed_reason VARCHAR(60)', 'ALTER TABLE lot ADD COLUMN closed_reason VARCHAR(60)')
        add_column_if_missing('lot', lot_columns, 'larva_supplier', 'ALTER TABLE lot ADD COLUMN larva_supplier VARCHAR(120)', 'ALTER TABLE lot ADD COLUMN larva_supplier VARCHAR(120)')
        add_column_if_missing('lot', lot_columns, 'entry_pl_stage', 'ALTER TABLE lot ADD COLUMN entry_pl_stage INTEGER', 'ALTER TABLE lot ADD COLUMN entry_pl_stage INTEGER')


    if 'lot_unit_allocation' in tables:
        allocation_columns = get_columns('lot_unit_allocation')
        add_column_if_missing('lot_unit_allocation', allocation_columns, 'quantity_allocated', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER')

    if 'transfer' in tables:
        transfer_columns = get_columns('transfer')
        add_column_if_missing('transfer', transfer_columns, 'source_phase', 'ALTER TABLE transfer ADD COLUMN source_phase VARCHAR(30)', 'ALTER TABLE transfer ADD COLUMN source_phase VARCHAR(30)')
        add_column_if_missing('transfer', transfer_columns, 'destination_phase', 'ALTER TABLE transfer ADD COLUMN destination_phase VARCHAR(30)', 'ALTER TABLE transfer ADD COLUMN destination_phase VARCHAR(30)')

    if 'sale' in tables:
        sale_columns = get_columns('sale')
        add_column_if_missing('sale', sale_columns, 'average_weight_g', 'ALTER TABLE sale ADD COLUMN average_weight_g FLOAT', 'ALTER TABLE sale ADD COLUMN average_weight_g DOUBLE PRECISION')
        add_column_if_missing('sale', sale_columns, 'harvested_units', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER')

    if 'nursery_feeding' in tables:
        nursery_feeding_columns = get_columns('nursery_feeding')
        add_column_if_missing('nursery_feeding', nursery_feeding_columns, 'score_adjustment_pct', 'ALTER TABLE nursery_feeding ADD COLUMN score_adjustment_pct FLOAT', 'ALTER TABLE nursery_feeding ADD COLUMN score_adjustment_pct DOUBLE PRECISION')

    if 'water_monitoring' in tables:
        water_columns = get_columns('water_monitoring')
        add_column_if_missing('water_monitoring', water_columns, 'monitor_time', 'ALTER TABLE water_monitoring ADD COLUMN monitor_time TIME')
        add_column_if_missing('water_monitoring', water_columns, 'nitrate', 'ALTER TABLE water_monitoring ADD COLUMN nitrate FLOAT', 'ALTER TABLE water_monitoring ADD COLUMN nitrate DOUBLE PRECISION')
        add_column_if_missing('water_monitoring', water_columns, 'alkalinity', 'ALTER TABLE water_monitoring ADD COLUMN alkalinity FLOAT', 'ALTER TABLE water_monitoring ADD COLUMN alkalinity DOUBLE PRECISION')
        add_column_if_missing('water_monitoring', water_columns, 'hardness', 'ALTER TABLE water_monitoring ADD COLUMN hardness FLOAT', 'ALTER TABLE water_monitoring ADD COLUMN hardness DOUBLE PRECISION')

    if 'water_reference_config' in tables:
        reference_columns = get_columns('water_reference_config')
        add_column_if_missing('water_reference_config', reference_columns, 'nitrate_min', 'ALTER TABLE water_reference_config ADD COLUMN nitrate_min FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN nitrate_min DOUBLE PRECISION')
        add_column_if_missing('water_reference_config', reference_columns, 'nitrate_max', 'ALTER TABLE water_reference_config ADD COLUMN nitrate_max FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN nitrate_max DOUBLE PRECISION')
        add_column_if_missing('water_reference_config', reference_columns, 'alkalinity_min', 'ALTER TABLE water_reference_config ADD COLUMN alkalinity_min FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN alkalinity_min DOUBLE PRECISION')
        add_column_if_missing('water_reference_config', reference_columns, 'alkalinity_max', 'ALTER TABLE water_reference_config ADD COLUMN alkalinity_max FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN alkalinity_max DOUBLE PRECISION')
        add_column_if_missing('water_reference_config', reference_columns, 'hardness_min', 'ALTER TABLE water_reference_config ADD COLUMN hardness_min FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN hardness_min DOUBLE PRECISION')
        add_column_if_missing('water_reference_config', reference_columns, 'hardness_max', 'ALTER TABLE water_reference_config ADD COLUMN hardness_max FLOAT', 'ALTER TABLE water_reference_config ADD COLUMN hardness_max DOUBLE PRECISION')

    if 'protocol_document' in tables:
        protocol_columns = get_columns('protocol_document')
        add_column_if_missing('protocol_document', protocol_columns, 'notes', 'ALTER TABLE protocol_document ADD COLUMN notes TEXT')
        add_column_if_missing('protocol_document', protocol_columns, 'original_filename', 'ALTER TABLE protocol_document ADD COLUMN original_filename VARCHAR(255)', 'ALTER TABLE protocol_document ADD COLUMN original_filename VARCHAR(255)')
        add_column_if_missing('protocol_document', protocol_columns, 'mime_type', 'ALTER TABLE protocol_document ADD COLUMN mime_type VARCHAR(120)', 'ALTER TABLE protocol_document ADD COLUMN mime_type VARCHAR(120)')
        add_column_if_missing('protocol_document', protocol_columns, 'file_size', 'ALTER TABLE protocol_document ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0', 'ALTER TABLE protocol_document ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0')
        add_column_if_missing('protocol_document', protocol_columns, 'uploaded_at', f"ALTER TABLE protocol_document ADD COLUMN uploaded_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE protocol_document ADD COLUMN uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('protocol_document', protocol_columns, 'uploaded_by_id', 'ALTER TABLE protocol_document ADD COLUMN uploaded_by_id INTEGER', 'ALTER TABLE protocol_document ADD COLUMN uploaded_by_id INTEGER')

    if 'farm_document' in tables:
        farm_document_columns = get_columns('farm_document')
        add_column_if_missing('farm_document', farm_document_columns, 'notes', 'ALTER TABLE farm_document ADD COLUMN notes TEXT')
        add_column_if_missing('farm_document', farm_document_columns, 'original_filename', 'ALTER TABLE farm_document ADD COLUMN original_filename VARCHAR(255)', 'ALTER TABLE farm_document ADD COLUMN original_filename VARCHAR(255)')
        add_column_if_missing('farm_document', farm_document_columns, 'mime_type', 'ALTER TABLE farm_document ADD COLUMN mime_type VARCHAR(120)', 'ALTER TABLE farm_document ADD COLUMN mime_type VARCHAR(120)')
        add_column_if_missing('farm_document', farm_document_columns, 'file_size', 'ALTER TABLE farm_document ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0', 'ALTER TABLE farm_document ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0')
        add_column_if_missing('farm_document', farm_document_columns, 'created_at', f"ALTER TABLE farm_document ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE farm_document ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('farm_document', farm_document_columns, 'uploaded_at', f"ALTER TABLE farm_document ADD COLUMN uploaded_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE farm_document ADD COLUMN uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('farm_document', farm_document_columns, 'uploaded_by_id', 'ALTER TABLE farm_document ADD COLUMN uploaded_by_id INTEGER', 'ALTER TABLE farm_document ADD COLUMN uploaded_by_id INTEGER')

    if 'daily_management' in tables:
        daily_management_columns = get_columns('daily_management')
        add_column_if_missing('daily_management', daily_management_columns, 'feed_product_id', 'ALTER TABLE daily_management ADD COLUMN feed_product_id INTEGER', 'ALTER TABLE daily_management ADD COLUMN feed_product_id INTEGER')
        add_column_if_missing('daily_management', daily_management_columns, 'feed_unit_cost', 'ALTER TABLE daily_management ADD COLUMN feed_unit_cost FLOAT', 'ALTER TABLE daily_management ADD COLUMN feed_unit_cost DOUBLE PRECISION')
        add_column_if_missing('daily_management', daily_management_columns, 'feed_total_cost', 'ALTER TABLE daily_management ADD COLUMN feed_total_cost FLOAT DEFAULT 0', 'ALTER TABLE daily_management ADD COLUMN feed_total_cost DOUBLE PRECISION DEFAULT 0')
        add_column_if_missing('daily_management', daily_management_columns, 'tray_score', 'ALTER TABLE daily_management ADD COLUMN tray_score FLOAT', 'ALTER TABLE daily_management ADD COLUMN tray_score DOUBLE PRECISION')
        add_column_if_missing('daily_management', daily_management_columns, 'created_at', f"ALTER TABLE daily_management ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE daily_management ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('daily_management', daily_management_columns, 'updated_at', f"ALTER TABLE daily_management ADD COLUMN updated_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE daily_management ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    if dialect != 'sqlite' and 'nursery_feeding' in tables:
        # Allow decimal intestinal scores such as 2.5 instead of only whole numbers.
        run_sql('', 'ALTER TABLE nursery_feeding ALTER COLUMN intestinal_score TYPE DOUBLE PRECISION USING intestinal_score::double precision')

    if 'feed_inventory' in tables:
        feed_inventory_columns = get_columns('feed_inventory')
        # Versões antigas criavam feed_name como VARCHAR(80). No PostgreSQL isso
        # derruba a tela /feed quando o nome comercial da ração é maior.
        # SQLite não precisa alterar, pois não aplica o limite do VARCHAR.
        if dialect != 'sqlite' and 'feed_name' in feed_inventory_columns:
            run_sql('', 'ALTER TABLE feed_inventory ALTER COLUMN feed_name TYPE VARCHAR(255)')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'feed_product_id', 'ALTER TABLE feed_inventory ADD COLUMN feed_product_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN feed_product_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'source_type', "ALTER TABLE feed_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'", "ALTER TABLE feed_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'")
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'source_ref_id', 'ALTER TABLE feed_inventory ADD COLUMN source_ref_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN source_ref_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'unit_id', 'ALTER TABLE feed_inventory ADD COLUMN unit_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN unit_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'lot_id', 'ALTER TABLE feed_inventory ADD COLUMN lot_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN lot_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'created_by_id', 'ALTER TABLE feed_inventory ADD COLUMN created_by_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN created_by_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'created_at', f"ALTER TABLE feed_inventory ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE feed_inventory ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    if 'supply_product' in tables:
        supply_product_columns = get_columns('supply_product')
        add_column_if_missing('supply_product', supply_product_columns, 'category', "ALTER TABLE supply_product ADD COLUMN category VARCHAR(80) DEFAULT 'Insumo geral'", "ALTER TABLE supply_product ADD COLUMN category VARCHAR(80) DEFAULT 'Insumo geral'")
        add_column_if_missing('supply_product', supply_product_columns, 'measure_unit', "ALTER TABLE supply_product ADD COLUMN measure_unit VARCHAR(20) DEFAULT 'kg'", "ALTER TABLE supply_product ADD COLUMN measure_unit VARCHAR(20) DEFAULT 'kg'")
        add_column_if_missing('supply_product', supply_product_columns, 'minimum_stock_qty', 'ALTER TABLE supply_product ADD COLUMN minimum_stock_qty FLOAT DEFAULT 0', 'ALTER TABLE supply_product ADD COLUMN minimum_stock_qty DOUBLE PRECISION DEFAULT 0')
        add_column_if_missing('supply_product', supply_product_columns, 'notes', 'ALTER TABLE supply_product ADD COLUMN notes TEXT')
        add_column_if_missing('supply_product', supply_product_columns, 'active', 'ALTER TABLE supply_product ADD COLUMN active BOOLEAN DEFAULT 1', 'ALTER TABLE supply_product ADD COLUMN active BOOLEAN DEFAULT TRUE')
        add_column_if_missing('supply_product', supply_product_columns, 'created_at', f"ALTER TABLE supply_product ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE supply_product ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    if 'supply_inventory' in tables:
        supply_inventory_columns = get_columns('supply_inventory')
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'source_type', "ALTER TABLE supply_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'", "ALTER TABLE supply_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'")
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'source_ref_id', 'ALTER TABLE supply_inventory ADD COLUMN source_ref_id INTEGER', 'ALTER TABLE supply_inventory ADD COLUMN source_ref_id INTEGER')
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'unit_id', 'ALTER TABLE supply_inventory ADD COLUMN unit_id INTEGER', 'ALTER TABLE supply_inventory ADD COLUMN unit_id INTEGER')
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'lot_id', 'ALTER TABLE supply_inventory ADD COLUMN lot_id INTEGER', 'ALTER TABLE supply_inventory ADD COLUMN lot_id INTEGER')
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'created_by_id', 'ALTER TABLE supply_inventory ADD COLUMN created_by_id INTEGER', 'ALTER TABLE supply_inventory ADD COLUMN created_by_id INTEGER')
        add_column_if_missing('supply_inventory', supply_inventory_columns, 'created_at', f"ALTER TABLE supply_inventory ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE supply_inventory ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    if 'management_supply_usage' in tables:
        management_supply_columns = get_columns('management_supply_usage')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'notes', 'ALTER TABLE management_supply_usage ADD COLUMN notes TEXT')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'created_at', f"ALTER TABLE management_supply_usage ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE management_supply_usage ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'updated_at', f"ALTER TABLE management_supply_usage ADD COLUMN updated_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE management_supply_usage ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    backfill_lot_allocations_and_status()
    sync_transfer_phase_history()
    sync_feed_products_from_legacy_movements()
    normalize_auto_nursery_feed_product_names()


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()
        get_water_reference_config()
        ensure_alert_rules()


def active_lot_allocation_for_unit(unit_id, on_date=None):
    on_date = on_date or date.today()
    return (
        LotUnitAllocation.query.options(joinedload(LotUnitAllocation.lot), joinedload(LotUnitAllocation.unit))
        .join(Lot, Lot.id == LotUnitAllocation.lot_id)
        .filter(
            LotUnitAllocation.unit_id == unit_id,
            LotUnitAllocation.start_date <= on_date,
            or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
            or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
            Lot.start_date <= on_date,
            Lot.status == 'ativo',
            or_(Lot.end_date.is_(None), Lot.end_date >= on_date),
        )
        .order_by(Lot.start_date.desc(), LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc())
        .first()
    )


def active_lot_for_unit(unit_id, on_date=None):
    allocation = active_lot_allocation_for_unit(unit_id, on_date=on_date)
    return allocation.lot if allocation else None


def latest_water(unit_id):
    return WaterMonitoring.query.filter_by(unit_id=unit_id).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.monitor_time.desc(), WaterMonitoring.id.desc()).first()


def latest_mgmt(unit_id):
    return DailyManagement.query.filter_by(unit_id=unit_id).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).first()


def backfill_lot_allocations_and_status():
    created = 0
    for lot in Lot.query.all():
        exists = LotUnitAllocation.query.filter_by(lot_id=lot.id).first()
        if not exists:
            db.session.add(LotUnitAllocation(
                lot_id=lot.id,
                unit_id=lot.unit_id,
                start_date=lot.start_date,
                end_date=lot.end_date,
                quantity_allocated=lot.initial_count,
                notes='Alocação inicial criada automaticamente.'
            ))
            created += 1
        else:
            for allocation in LotUnitAllocation.query.filter_by(lot_id=lot.id).all():
                if allocation.quantity_allocated is None:
                    allocation.quantity_allocated = lot.initial_count
        if lot.status == 'encerrado' and lot.end_date is None:
            last_sale = Sale.query.filter_by(lot_id=lot.id).order_by(Sale.sale_date.desc(), Sale.id.desc()).first()
            lot.end_date = last_sale.sale_date if last_sale else date.today()
            if not lot.closed_reason:
                lot.closed_reason = 'encerrado_manual'
    if created:
        db.session.flush()



def sync_transfer_phase_history():
    """Backfills historical transfer phases so the timeline does not depend on later unit edits."""
    changed = False
    for transfer in Transfer.query.options(joinedload(Transfer.source_unit), joinedload(Transfer.destination_unit)).all():
        if not transfer.source_phase and transfer.source_unit and transfer.source_unit.phase:
            transfer.source_phase = transfer.source_unit.phase
            changed = True
        if not transfer.destination_phase and transfer.destination_unit and transfer.destination_unit.phase:
            transfer.destination_phase = transfer.destination_unit.phase
            changed = True
    if changed:
        db.session.flush()


def sync_lot_allocations_after_lot_edit(lot: Lot, old_unit_id=None, old_initial_count=None, old_start_date=None):
    """Keeps the live allocation map consistent after editing the core lot fields.

    Lot is the registration/master record. LotUnitAllocation is the operational map used by
    "Alocações atuais", transfers, fixed-cost apportionment and density. Before this sync,
    editing lot.initial_count/unit/start_date did not update the allocation map.
    """
    if not lot:
        return

    transfer_count = Transfer.query.filter_by(source_lot_id=lot.id).count()
    allocations = LotUnitAllocation.query.filter_by(lot_id=lot.id).order_by(LotUnitAllocation.start_date.asc(), LotUnitAllocation.id.asc()).all()
    desired_end_date = lot.end_date if lot.status == 'encerrado' else None
    desired_qty = 0 if lot.status == 'encerrado' else (lot.initial_count or 0)

    if transfer_count == 0:
        # No movement history yet: the initial allocation must mirror the lot registration.
        primary = allocations[0] if allocations else None
        if not primary:
            primary = LotUnitAllocation(lot_id=lot.id)
            db.session.add(primary)
        primary.unit_id = lot.unit_id
        primary.start_date = lot.start_date
        primary.end_date = desired_end_date
        primary.quantity_allocated = desired_qty
        primary.notes = 'Alocação inicial do lote.'
        for extra in allocations[1:]:
            db.session.delete(extra)
        return

    # With transfer history we preserve the movement log, but still fix the original allocation
    # metadata. If the original allocation is still the only live saldo, its quantity is safe to update.
    primary = next((a for a in allocations if a.notes and 'Alocação inicial' in a.notes), None) or (allocations[0] if allocations else None)
    if not primary:
        db.session.add(LotUnitAllocation(
            lot_id=lot.id,
            unit_id=lot.unit_id,
            start_date=lot.start_date,
            end_date=desired_end_date,
            quantity_allocated=desired_qty,
            notes='Alocação inicial do lote.'
        ))
        return

    if primary.start_date == old_start_date or (primary.notes and 'Alocação inicial' in primary.notes):
        primary.start_date = lot.start_date
    if primary.unit_id == old_unit_id or (primary.notes and 'Alocação inicial' in primary.notes):
        primary.unit_id = lot.unit_id

    today = date.today()
    active_allocations = [
        a for a in allocations
        if a.start_date <= today and (a.end_date is None or a.end_date >= today) and (a.quantity_allocated is None or a.quantity_allocated > 0)
    ]
    if len(active_allocations) == 1 and active_allocations[0].id == primary.id:
        primary.quantity_allocated = desired_qty
        primary.end_date = desired_end_date


def close_lot(lot: Lot, close_date: date, reason: str = 'encerrado_manual'):
    if not lot:
        return
    lot.status = 'encerrado'
    lot.end_date = close_date
    lot.closed_reason = reason
    LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date > close_date),
    ).update({'end_date': close_date}, synchronize_session=False)


def lot_current_units(lot: Lot, on_date=None):
    on_date = on_date or date.today()
    allocations = LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit)).filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
        or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
    ).order_by(LotUnitAllocation.start_date.asc(), LotUnitAllocation.id.asc()).all()
    return [allocation.unit for allocation in allocations]


def active_lots_on_date(on_date: date):
    return Lot.query.filter(
        Lot.start_date <= on_date,
        or_(Lot.end_date.is_(None), Lot.end_date >= on_date),
    ).all()


def active_allocations_on_date(on_date: date):
    return LotUnitAllocation.query.join(Lot, Lot.id == LotUnitAllocation.lot_id).filter(
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
        or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
        Lot.status == 'ativo',
        Lot.start_date <= on_date,
        or_(Lot.end_date.is_(None), Lot.end_date >= on_date),
    ).all()


def allocation_density(allocation: LotUnitAllocation):
    if not allocation or not allocation.unit or not allocation.unit.area_m2 or not allocation.quantity_allocated:
        return None
    return round((allocation.quantity_allocated or 0) / allocation.unit.area_m2, 2)


def find_active_allocation(lot_id: int, unit_id: int, on_date: date):
    return LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot_id,
        LotUnitAllocation.unit_id == unit_id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
        or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
    ).order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc()).first()


def active_allocation_rows(on_date=None):
    """Active lot positions by unit. Used by the trifasic transfer screen."""
    on_date = on_date or date.today()
    return (
        LotUnitAllocation.query.options(joinedload(LotUnitAllocation.lot), joinedload(LotUnitAllocation.unit))
        .join(Lot, Lot.id == LotUnitAllocation.lot_id)
        .join(Unit, Unit.id == LotUnitAllocation.unit_id)
        .filter(
            LotUnitAllocation.start_date <= on_date,
            or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
            or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
            Lot.status == 'ativo',
            Lot.start_date <= on_date,
            or_(Lot.end_date.is_(None), Lot.end_date >= on_date),
        )
        .order_by(Lot.lot_code.asc(), Unit.phase.asc(), Unit.name.asc(), LotUnitAllocation.start_date.asc())
        .all()
    )


def sync_lot_phase_from_allocations(lot: Lot, on_date=None):
    """Keeps Lot.phase compatible with the most advanced active phase of its allocations."""
    if not lot:
        return
    on_date = on_date or date.today()
    phase_rank = {'bercario': 1, 'juvenil': 2, 'engorda': 3}
    allocations = (
        LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit))
        .filter(
            LotUnitAllocation.lot_id == lot.id,
            LotUnitAllocation.start_date <= on_date,
            or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
            or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
        )
        .all()
    )
    if not allocations:
        return
    most_advanced = max(
        (allocation.unit.phase for allocation in allocations if allocation.unit and allocation.unit.phase),
        key=lambda phase: phase_rank.get(phase, 0),
        default=lot.phase,
    )
    if most_advanced:
        lot.phase = most_advanced
        preferred = max(
            allocations,
            key=lambda allocation: (
                phase_rank.get(allocation.unit.phase if allocation.unit else '', 0),
                allocation.start_date or date.min,
                allocation.id or 0,
            ),
        )
        if preferred and preferred.unit_id:
            lot.unit_id = preferred.unit_id


def calculate_fixed_cost_for_lot(lot: Lot):
    if not lot:
        return 0.0
    start = lot.start_date
    end = lot.end_date or date.today()
    if end < start:
        return 0.0

    total = 0.0
    cursor = start
    while cursor <= end:
        active_costs = FixedCost.query.filter(
            FixedCost.start_date <= cursor,
            or_(FixedCost.end_date.is_(None), FixedCost.end_date >= cursor),
            FixedCost.active.is_(True),
        ).all()
        if active_costs:
            active_allocations = active_allocations_on_date(cursor)
            divisor = len(active_allocations) or 1
            lot_allocations = [allocation for allocation in active_allocations if allocation.lot_id == lot.id]
            daily_cost = sum((cost.monthly_amount or 0) / 30 for cost in active_costs)
            total += daily_cost * (len(lot_allocations) / divisor)
        cursor += timedelta(days=1)
    return round(total, 2)


def calculate_fixed_cost_for_allocation(lot: Lot, unit_id: int, start_date: date, end_date: date | None = None):
    if not lot or not unit_id:
        return 0.0
    start = max(lot.start_date, start_date or lot.start_date)
    end = end_date or lot.end_date or date.today()
    if end < start:
        return 0.0
    total = 0.0
    cursor = start
    while cursor <= end:
        active_costs = FixedCost.query.filter(
            FixedCost.start_date <= cursor,
            or_(FixedCost.end_date.is_(None), FixedCost.end_date >= cursor),
            FixedCost.active.is_(True),
        ).all()
        if active_costs:
            active_allocations = active_allocations_on_date(cursor)
            divisor = len(active_allocations) or 1
            current_allocation = find_active_allocation(lot.id, unit_id, cursor)
            if current_allocation:
                daily_cost = sum((cost.monthly_amount or 0) / 30 for cost in active_costs)
                total += daily_cost / divisor
        cursor += timedelta(days=1)
    return round(total, 2)


def calculate_feed_cost_for_unit(lot_id: int, unit_id: int, end_date: date | None = None):
    query = db.session.query(func.coalesce(func.sum(DailyManagement.feed_total_cost), 0)).filter(
        DailyManagement.lot_id == lot_id,
        DailyManagement.unit_id == unit_id,
    )
    if end_date:
        query = query.filter(DailyManagement.manage_date <= end_date)
    return round(query.scalar() or 0, 2)


def lot_total_harvested_units(lot_id: int):
    total = db.session.query(func.coalesce(func.sum(Sale.harvested_units), 0)).filter(Sale.lot_id == lot_id).scalar() or 0
    return int(total or 0)


def lot_total_harvested_kg(lot_id: int):
    total = db.session.query(func.coalesce(func.sum(Sale.quantity_kg), 0)).filter(Sale.lot_id == lot_id).scalar() or 0
    return round(total, 2)


def lot_total_revenue(lot_id: int):
    total = db.session.query(func.coalesce(func.sum(Sale.quantity_kg * Sale.unit_price), 0)).filter(Sale.lot_id == lot_id).scalar() or 0
    return round(total, 2)


def build_allocation_rows(lot: Lot, on_date=None):
    on_date = on_date or date.today()
    allocations = LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit)).filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
        or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
    ).order_by(LotUnitAllocation.start_date.asc(), LotUnitAllocation.id.asc()).all()
    rows = []
    for allocation in allocations:
        rows.append({
            'unit_name': allocation.unit.name if allocation.unit else '—',
            'phase': allocation.unit.phase if allocation.unit else lot.phase,
            'allocated_qty': allocation.quantity_allocated or 0,
            'density': allocation_density(allocation),
        })
    return rows


def lot_financial_summary(lot: Lot):
    feed_cost = db.session.query(func.coalesce(func.sum(DailyManagement.feed_total_cost), 0)).filter(DailyManagement.lot_id == lot.id).scalar() or 0
    supply_cost = db.session.query(func.coalesce(func.sum(ManagementSupplyUsage.total_cost), 0)).join(DailyManagement, DailyManagement.id == ManagementSupplyUsage.management_id).filter(DailyManagement.lot_id == lot.id).scalar() or 0
    fixed_cost = calculate_fixed_cost_for_lot(lot)
    current_units = lot_current_units(lot)
    allocation_rows = build_allocation_rows(lot)
    harvested_units = lot_total_harvested_units(lot.id)
    survival_pct = round((harvested_units / lot.initial_count) * 100, 2) if lot.initial_count else None
    total_feed_offered = db.session.query(func.coalesce(func.sum(DailyManagement.feed_offered_kg), 0)).filter(DailyManagement.lot_id == lot.id).scalar() or 0
    harvested_kg = lot_total_harvested_kg(lot.id)
    fcr_real = round(total_feed_offered / harvested_kg, 2) if harvested_kg else None
    return {
        'lot': lot,
        'feed_cost': round(feed_cost, 2),
        'supply_cost': round(supply_cost, 2),
        'fixed_cost': fixed_cost,
        'total_cost': round((feed_cost or 0) + (supply_cost or 0) + fixed_cost, 2),
        'current_units': current_units,
        'allocations': allocation_rows,
        'harvested_units': harvested_units,
        'survival_pct': survival_pct,
        'fcr_real': fcr_real,
    }


def sale_financial_summary(sale: Sale):
    if not sale.lot or not sale.unit_id:
        return None
    allocation = find_active_allocation(sale.lot_id, sale.unit_id, sale.sale_date)
    feed_cost = calculate_feed_cost_for_unit(sale.lot_id, sale.unit_id, sale.sale_date)
    supply_cost = calculate_supply_cost_for_unit(sale.lot_id, sale.unit_id, sale.sale_date)
    fixed_cost = calculate_fixed_cost_for_allocation(sale.lot, sale.unit_id, sale.lot.start_date, sale.sale_date)
    total_cost = round(feed_cost + supply_cost + fixed_cost, 2)
    revenue = round((sale.quantity_kg or 0) * (sale.unit_price or 0), 2)
    harvested_units = sale.harvested_units or 0
    if not harvested_units and sale.average_weight_g:
        harvested_units = int(round((sale.quantity_kg * 1000) / sale.average_weight_g)) if sale.average_weight_g else 0
    lot_harvested_units = lot_total_harvested_units(sale.lot_id)
    survival_pct = round((lot_harvested_units / sale.lot.initial_count) * 100, 2) if sale.lot.initial_count else None
    total_feed_offered = db.session.query(func.coalesce(func.sum(DailyManagement.feed_offered_kg), 0)).filter(
        DailyManagement.lot_id == sale.lot_id,
        DailyManagement.manage_date <= sale.sale_date,
    ).scalar() or 0
    harvested_kg_lot = lot_total_harvested_kg(sale.lot_id)
    fcr_real = round(total_feed_offered / harvested_kg_lot, 2) if harvested_kg_lot else None
    return {
        'sale': sale,
        'allocation_qty': allocation.quantity_allocated if allocation else None,
        'density': allocation_density(allocation) if allocation else None,
        'feed_cost': feed_cost,
        'supply_cost': supply_cost,
        'fixed_cost': fixed_cost,
        'total_cost': total_cost,
        'revenue': revenue,
        'profit': round(revenue - total_cost, 2),
        'status': 'Lucro' if revenue >= total_cost else 'Prejuízo',
        'harvested_units': harvested_units,
        'survival_pct': survival_pct,
        'fcr_real': fcr_real,
    }


def normalize_auto_nursery_feed_product_names():
    auto_products = FeedProduct.query.all()
    for product in auto_products:
        if normalize_text(product.feed_type or '') in {'bercario', 'bercário', 'geral'} and is_auto_nursery_protocol_product(product):
            product.feed_type = ''

    db.session.flush()

    for product in list(FeedProduct.query.all()):
        if not is_auto_nursery_protocol_product(product):
            continue
        canonical = find_or_create_nursery_feed_product(product.brand, exclude_product_id=product.id, create_missing=False)
        if canonical and canonical.id != product.id:
            for movement in FeedInventory.query.filter_by(feed_product_id=product.id).all():
                movement.feed_product_id = canonical.id
                movement.feed_name = feed_inventory_name(canonical)
            for record in DailyManagement.query.filter_by(feed_product_id=product.id).all():
                record.feed_product_id = canonical.id
            product.active = False
            product.notes = ((product.notes or '').strip() + '\nSubstituído automaticamente por produto de berçário cadastrado no estoque.').strip()

    for movement in FeedInventory.query.filter(FeedInventory.feed_name.isnot(None)).all():
        cleaned_name = re.sub(r'\s*·\s*(Berçário|Bercario|Geral)\s*$', '', movement.feed_name or '', flags=re.IGNORECASE).strip()
        if cleaned_name and cleaned_name != movement.feed_name:
            movement.feed_name = cleaned_name

    db.session.commit()


def sync_feed_products_from_legacy_movements():
    existing_products = {
        normalize_text(f'{product.brand} {product.feed_type}'): product
        for product in FeedProduct.query.all()
    }

    legacy_names = [name for (name,) in db.session.query(FeedInventory.feed_name).filter(FeedInventory.feed_name.isnot(None)).distinct().all() if name]
    created = 0
    for feed_name in legacy_names:
        normalized = normalize_text(feed_name)
        if not normalized or normalized in existing_products:
            continue
        product = FeedProduct(brand=feed_name.strip(), feed_type='Geral', active=True)
        db.session.add(product)
        db.session.flush()
        existing_products[normalized] = product
        created += 1

    if created:
        db.session.flush()

    for movement in FeedInventory.query.filter(FeedInventory.feed_product_id.is_(None), FeedInventory.feed_name.isnot(None)).all():
        product = existing_products.get(normalize_text(movement.feed_name))
        if product:
            movement.feed_product_id = product.id

    db.session.commit()


def feed_product_label(product):
    if not product:
        return 'Sem ração vinculada'
    return product.full_name


def feed_inventory_name(feed_product) -> str:
    """Snapshot seguro do nome da ração para o histórico de estoque.

    O produto vinculado pelo feed_product_id continua sendo a fonte principal.
    Este campo é mantido para compatibilidade com movimentos antigos e relatórios,
    então limitamos o texto para nunca estourar o VARCHAR do PostgreSQL.
    """
    value = feed_product.full_name if feed_product else 'Ração'
    value = re.sub(r'\s+', ' ', (value or '').strip()) or 'Ração'
    return value[:255]


def movement_origin_label(value: str) -> str:
    return {
        'manual': 'Manual',
        'manejo': 'Manejo diário',
        'ajuste': 'Ajuste',
    }.get(value or '', 'Manual')


def weighted_feed_unit_cost(feed_product_id: int, up_to_date=None, exclude_movement_id=None):
    query = FeedInventory.query.filter(
        FeedInventory.feed_product_id == feed_product_id,
        FeedInventory.movement_type == 'entrada',
        FeedInventory.unit_cost.isnot(None),
    )
    if up_to_date is not None:
        query = query.filter(FeedInventory.movement_date <= up_to_date)
    if exclude_movement_id is not None:
        query = query.filter(FeedInventory.id != exclude_movement_id)
    entries = query.all()
    total_qty = sum(entry.quantity_kg or 0 for entry in entries)
    if total_qty <= 0:
        return None
    total_value = sum((entry.quantity_kg or 0) * (entry.unit_cost or 0) for entry in entries)
    return round(total_value / total_qty, 4)


def build_feed_stock_snapshot():
    products = {product.id: product for product in FeedProduct.query.order_by(FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()}
    rows_by_product = {}
    total_stock = 0.0

    for product in products.values():
        rows_by_product[product.id] = {
            'product': product,
            'feed_name': product.full_name,
            'brand': product.brand,
            'feed_type': product.feed_type,
            'technical_summary': product.technical_summary,
            'minimum_stock_kg': round(product.minimum_stock_kg or 0, 1),
            'stock_kg': 0.0,
            'avg_unit_cost': weighted_feed_unit_cost(product.id),
            'movement_count': 0,
        }

    for movement in FeedInventory.query.options(joinedload(FeedInventory.feed_product)).order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).all():
        sign = 1 if movement.movement_type == 'entrada' else -1
        stock_change = sign * (movement.quantity_kg or 0)
        total_stock += stock_change
        if movement.feed_product_id:
            row = rows_by_product.setdefault(movement.feed_product_id, {
                'product': movement.feed_product,
                'feed_name': movement.feed_product.full_name if movement.feed_product else movement.feed_name,
                'brand': movement.feed_product.brand if movement.feed_product else movement.feed_name,
                'feed_type': movement.feed_product.feed_type if movement.feed_product else 'Geral',
                'technical_summary': movement.feed_product.technical_summary if movement.feed_product else '',
                'minimum_stock_kg': round((movement.feed_product.minimum_stock_kg if movement.feed_product else 0) or 0, 1),
                'stock_kg': 0.0,
                'avg_unit_cost': weighted_feed_unit_cost(movement.feed_product_id) if movement.feed_product_id else None,
                'movement_count': 0,
            })
            row['stock_kg'] += stock_change
            row['movement_count'] += 1

    snapshot_rows = []
    low_stock_count = 0
    active_product_count = 0
    for row in rows_by_product.values():
        product = row['product']
        if not product:
            continue
        active_product_count += 1 if product.active else 0
        row['stock_kg'] = round(row['stock_kg'], 1)
        minimum_stock = row['minimum_stock_kg'] or 0
        row['status'] = 'baixo' if row['stock_kg'] <= minimum_stock and minimum_stock > 0 else 'ok'
        if row['status'] == 'baixo':
            low_stock_count += 1
        snapshot_rows.append(row)

    snapshot_rows.sort(key=lambda item: (item['product'].active is False, item['brand'].lower(), item['feed_type'].lower()))
    return {
        'rows': snapshot_rows,
        'total_stock_kg': round(total_stock, 1),
        'low_stock_count': low_stock_count,
        'active_product_count': active_product_count,
    }


def available_stock_for_product(feed_product_id: int, exclude_movement_id=None) -> float:
    total = 0.0
    query = FeedInventory.query.filter(FeedInventory.feed_product_id == feed_product_id)
    if exclude_movement_id is not None:
        query = query.filter(FeedInventory.id != exclude_movement_id)
    for movement in query.all():
        total += movement.quantity_kg if movement.movement_type == 'entrada' else -(movement.quantity_kg or 0)
    return round(total, 4)


def get_management_feed_movement(management_id: int):
    return FeedInventory.query.filter_by(source_type='manejo', source_ref_id=management_id).order_by(FeedInventory.id.desc()).first()


def selected_feed_product_from_form():
    feed_product_id = parse_int(request.form.get('feed_product_id'))
    return db.session.get(FeedProduct, feed_product_id) if feed_product_id else None


def validate_feed_usage(feed_product, offered_kg: float, existing_movement=None):
    if offered_kg <= 0:
        return None
    if not feed_product:
        return 'Selecione a ração utilizada no manejo.'
    available_stock = available_stock_for_product(
        feed_product.id,
        exclude_movement_id=existing_movement.id if existing_movement else None,
    )
    if offered_kg > available_stock:
        return f'Estoque insuficiente para {feed_product.full_name}. Disponível: {round(available_stock, 1)} kg.'
    return None


def sync_management_feed_movement(management_record: DailyManagement, feed_product, offered_kg: float, existing_movement=None):
    if offered_kg <= 0 or not feed_product:
        if existing_movement:
            db.session.delete(existing_movement)
        management_record.feed_unit_cost = None
        management_record.feed_total_cost = 0
        management_record.feed_product_id = None
        return

    management_record.feed_product_id = feed_product.id
    unit_cost = weighted_feed_unit_cost(feed_product.id, up_to_date=management_record.manage_date, exclude_movement_id=existing_movement.id if existing_movement else None)
    management_record.feed_unit_cost = unit_cost
    management_record.feed_total_cost = round((unit_cost or 0) * offered_kg, 2)

    movement = existing_movement or FeedInventory(source_type='manejo', source_ref_id=management_record.id)
    movement.movement_date = management_record.manage_date
    movement.feed_product_id = feed_product.id
    movement.feed_name = feed_inventory_name(feed_product)
    movement.movement_type = 'saida'
    movement.quantity_kg = offered_kg
    movement.unit_cost = unit_cost
    movement.unit_id = management_record.unit_id
    movement.lot_id = management_record.lot_id
    movement.created_by_id = getattr(current_user, 'id', None)
    movement.notes = f'Saída automática pelo manejo diário.'
    if not existing_movement:
        db.session.add(movement)


def management_cost_summary(selected_unit_id=None):
    query = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot), joinedload(DailyManagement.feed_product))
    if selected_unit_id:
        query = query.filter(DailyManagement.unit_id == selected_unit_id)
    records = query.order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).all()
    record_ids = [record.id for record in records]
    supply_cost_by_record = {record_id: 0 for record_id in record_ids}
    if record_ids:
        supply_rows = db.session.query(ManagementSupplyUsage.management_id, func.coalesce(func.sum(ManagementSupplyUsage.total_cost), 0)).filter(ManagementSupplyUsage.management_id.in_(record_ids)).group_by(ManagementSupplyUsage.management_id).all()
        for management_id, total in supply_rows:
            supply_cost_by_record[management_id] = total or 0

    total_offered = round(sum(record.feed_offered_kg or 0 for record in records), 1)
    total_feed_cost = round(sum(record.feed_total_cost or 0 for record in records), 2)
    total_supply_cost = round(sum(supply_cost_by_record.get(record.id, 0) for record in records), 2)
    total_cost = round(total_feed_cost + total_supply_cost, 2)
    group_map = defaultdict(lambda: {'offered_kg': 0.0, 'feed_cost_total': 0.0, 'supply_cost_total': 0.0, 'cost_total': 0.0, 'records': 0, 'unit_name': '', 'lot_code': ''})
    for record in records:
        key = (record.unit_id, record.lot_id)
        row = group_map[key]
        row['unit_name'] = record.unit.name if record.unit else '—'
        row['lot_code'] = record.lot.lot_code if record.lot else 'Sem lote'
        row['offered_kg'] += record.feed_offered_kg or 0
        row['feed_cost_total'] += record.feed_total_cost or 0
        row['supply_cost_total'] += supply_cost_by_record.get(record.id, 0)
        row['cost_total'] += (record.feed_total_cost or 0) + supply_cost_by_record.get(record.id, 0)
        row['records'] += 1
    grouped_rows = []
    for row in group_map.values():
        row['offered_kg'] = round(row['offered_kg'], 1)
        row['feed_cost_total'] = round(row['feed_cost_total'], 2)
        row['supply_cost_total'] = round(row['supply_cost_total'], 2)
        row['cost_total'] = round(row['cost_total'], 2)
        row['avg_cost_per_kg'] = round((row['cost_total'] / row['offered_kg']), 2) if row['offered_kg'] > 0 else None
        grouped_rows.append(row)
    grouped_rows.sort(key=lambda item: (-item['cost_total'], item['unit_name']))
    return {
        'total_offered_kg': total_offered,
        'total_feed_cost': total_feed_cost,
        'total_supply_cost': total_supply_cost,
        'total_cost': total_cost,
        'avg_cost_per_kg': round((total_cost / total_offered), 2) if total_offered > 0 else None,
        'grouped_rows': grouped_rows[:20],
    }



def supply_product_label(product):
    if not product:
        return 'Sem insumo vinculado'
    return product.full_name


def weighted_supply_unit_cost(product_id: int, up_to_date=None):
    query = SupplyInventory.query.filter(
        SupplyInventory.supply_product_id == product_id,
        SupplyInventory.movement_type == 'entrada',
        SupplyInventory.unit_cost.isnot(None),
    )
    if up_to_date is not None:
        query = query.filter(SupplyInventory.movement_date <= up_to_date)
    entries = query.all()
    total_qty = sum(entry.quantity or 0 for entry in entries)
    if total_qty <= 0:
        return None
    total_value = sum((entry.quantity or 0) * (entry.unit_cost or 0) for entry in entries)
    return round(total_value / total_qty, 4)


def available_stock_for_supply(product_id: int) -> float:
    total = 0.0
    for movement in SupplyInventory.query.filter(SupplyInventory.supply_product_id == product_id).all():
        total += movement.quantity if movement.movement_type == 'entrada' else -(movement.quantity or 0)
    return round(total, 4)


def build_supply_stock_snapshot():
    products = {product.id: product for product in SupplyProduct.query.order_by(SupplyProduct.name.asc()).all()}
    rows_by_product = {}
    total_stock = 0.0

    for product in products.values():
        rows_by_product[product.id] = {
            'product': product,
            'name': product.name,
            'category': product.category,
            'measure_unit': product.measure_unit,
            'technical_summary': product.technical_summary,
            'minimum_stock_qty': round(product.minimum_stock_qty or 0, 2),
            'stock_qty': 0.0,
            'avg_unit_cost': weighted_supply_unit_cost(product.id),
            'movement_count': 0,
        }

    for movement in SupplyInventory.query.options(joinedload(SupplyInventory.supply_product)).order_by(SupplyInventory.movement_date.desc(), SupplyInventory.id.desc()).all():
        sign = 1 if movement.movement_type == 'entrada' else -1
        stock_change = sign * (movement.quantity or 0)
        total_stock += stock_change
        row = rows_by_product.get(movement.supply_product_id)
        if not row:
            product = movement.supply_product
            if not product:
                continue
            row = rows_by_product[movement.supply_product_id] = {
                'product': product,
                'name': product.name,
                'category': product.category,
                'measure_unit': product.measure_unit,
                'technical_summary': product.technical_summary,
                'minimum_stock_qty': round(product.minimum_stock_qty or 0, 2),
                'stock_qty': 0.0,
                'avg_unit_cost': weighted_supply_unit_cost(product.id),
                'movement_count': 0,
            }
        row['stock_qty'] += stock_change
        row['movement_count'] += 1

    snapshot_rows = []
    low_stock_count = 0
    active_product_count = 0
    for row in rows_by_product.values():
        product = row['product']
        if not product:
            continue
        active_product_count += 1 if product.active else 0
        row['stock_qty'] = round(row['stock_qty'], 2)
        minimum_stock = row['minimum_stock_qty'] or 0
        row['status'] = 'baixo' if row['stock_qty'] <= minimum_stock and minimum_stock > 0 else 'ok'
        if row['status'] == 'baixo':
            low_stock_count += 1
        snapshot_rows.append(row)

    snapshot_rows.sort(key=lambda item: (item['product'].active is False, item['name'].lower()))
    return {
        'rows': snapshot_rows,
        'total_stock_qty': round(total_stock, 2),
        'low_stock_count': low_stock_count,
        'active_product_count': active_product_count,
    }


def management_supply_entries_from_form():
    entries = []
    product_ids = request.form.getlist('supply_product_id[]')
    quantities = request.form.getlist('supply_quantity[]')
    notes_list = request.form.getlist('supply_notes[]')
    usage_ids = request.form.getlist('supply_usage_id[]')
    row_count = max(len(product_ids), len(quantities), len(notes_list), len(usage_ids))
    for idx in range(row_count):
        product_id = parse_int(product_ids[idx] if idx < len(product_ids) else None)
        quantity = parse_float(quantities[idx] if idx < len(quantities) else None)
        notes = (notes_list[idx] if idx < len(notes_list) else '') or ''
        usage_id = parse_int(usage_ids[idx] if idx < len(usage_ids) else None)
        if not product_id and (quantity is None or quantity == 0) and not notes.strip():
            continue
        product = db.session.get(SupplyProduct, product_id) if product_id else None
        entries.append({
            'usage_id': usage_id,
            'product': product,
            'quantity': quantity or 0,
            'notes': notes.strip(),
        })
    return entries


def validate_supply_usage(entries, management_record=None):
    existing_by_product = defaultdict(float)
    if management_record:
        for usage in ManagementSupplyUsage.query.filter_by(management_id=management_record.id).all():
            existing_by_product[usage.supply_product_id] += usage.quantity or 0
    requested_by_product = defaultdict(float)
    for entry in entries:
        product = entry['product']
        quantity = entry['quantity'] or 0
        if quantity <= 0:
            continue
        if not product:
            return 'Selecione o insumo utilizado ou remova a linha vazia.'
        requested_by_product[product.id] += quantity
    for product_id, quantity in requested_by_product.items():
        product = db.session.get(SupplyProduct, product_id)
        available = available_stock_for_supply(product_id) + (existing_by_product.get(product_id) or 0)
        if quantity > available:
            unit_label = product.measure_unit if product else 'un'
            product_name = product.full_name if product else 'insumo'
            return f'Estoque insuficiente para {product_name}. Disponível: {round(available, 2)} {unit_label}.'
    return None


def management_supply_rows_for_form(record=None, blank_rows=2):
    rows = []
    if record:
        usages = ManagementSupplyUsage.query.options(joinedload(ManagementSupplyUsage.supply_product)).filter_by(management_id=record.id).order_by(ManagementSupplyUsage.id.asc()).all()
        for usage in usages:
            rows.append({
                'usage_id': usage.id,
                'product_id': usage.supply_product_id,
                'quantity': usage.quantity,
                'notes': usage.notes or '',
            })
    if not rows:
        rows.append({'usage_id': '', 'product_id': '', 'quantity': '', 'notes': ''})
    while len(rows) < blank_rows:
        rows.append({'usage_id': '', 'product_id': '', 'quantity': '', 'notes': ''})
    return rows


def sync_management_supply_usages(management_record: DailyManagement, entries):
    existing_usages = ManagementSupplyUsage.query.filter_by(management_id=management_record.id).all()
    existing_ids = [usage.id for usage in existing_usages]
    if existing_ids:
        SupplyInventory.query.filter(
            SupplyInventory.source_type == 'manejo_insumo',
            SupplyInventory.source_ref_id.in_(existing_ids),
        ).delete(synchronize_session=False)
        ManagementSupplyUsage.query.filter(ManagementSupplyUsage.id.in_(existing_ids)).delete(synchronize_session=False)
        db.session.flush()

    for entry in entries:
        product = entry['product']
        quantity = entry['quantity'] or 0
        if not product or quantity <= 0:
            continue
        unit_cost = weighted_supply_unit_cost(product.id, up_to_date=management_record.manage_date)
        usage = ManagementSupplyUsage(
            management_id=management_record.id,
            supply_product_id=product.id,
            quantity=quantity,
            unit_cost=unit_cost,
            total_cost=round((unit_cost or 0) * quantity, 2),
            notes=entry['notes'],
            updated_at=datetime.utcnow(),
        )
        db.session.add(usage)
        db.session.flush()
        movement = SupplyInventory(
            movement_date=management_record.manage_date,
            supply_product_id=product.id,
            movement_type='saida',
            quantity=quantity,
            unit_cost=unit_cost,
            unit_id=management_record.unit_id,
            lot_id=management_record.lot_id,
            source_type='manejo_insumo',
            source_ref_id=usage.id,
            created_by_id=getattr(current_user, 'id', None),
            notes=f'Saída automática pelo manejo diário. {entry["notes"]}'.strip(),
        )
        db.session.add(movement)


def management_supply_total_for_record(record_id: int):
    return db.session.query(func.coalesce(func.sum(ManagementSupplyUsage.total_cost), 0)).filter(ManagementSupplyUsage.management_id == record_id).scalar() or 0


def calculate_supply_cost_for_unit(lot_id: int, unit_id: int, end_date: date | None = None):
    query = db.session.query(func.coalesce(func.sum(ManagementSupplyUsage.total_cost), 0)).join(DailyManagement, DailyManagement.id == ManagementSupplyUsage.management_id).filter(
        DailyManagement.lot_id == lot_id,
        DailyManagement.unit_id == unit_id,
    )
    if end_date is not None:
        query = query.filter(DailyManagement.manage_date <= end_date)
    return round(query.scalar() or 0, 2)


def movement_supply_origin_label(value: str) -> str:
    return {
        'manual': 'Manual',
        'manejo_insumo': 'Manejo diário',
        'ajuste': 'Ajuste',
    }.get(value or '', 'Manual')


def water_status(rec, config=None):
    if not rec:
        return 'sem leitura'
    alerts = water_alerts_for_record(rec, config)
    return ' | '.join(alert['message'] for alert in alerts) if alerts else 'ok'


def month_start(value: date):
    return value.replace(day=1)


def safe_round(value, digits=1):
    if value is None:
        return None
    return round(value, digits)


def latest_weight_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]]):
    records = records_by_lot.get(lot.id, [])
    for record in sorted(records, key=lambda item: (item.manage_date, item.id), reverse=True):
        if record.average_weight_g is not None:
            return round(record.average_weight_g, 3)
    if lot.estimated_weight_g is not None:
        return round(lot.estimated_weight_g, 3)
    return None


def latest_biomass_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]], allocations_by_lot: dict[int, list[LotUnitAllocation]]):
    records = records_by_lot.get(lot.id, [])
    for record in sorted(records, key=lambda item: (item.manage_date, item.id), reverse=True):
        if record.estimated_biomass_kg is not None:
            return round(record.estimated_biomass_kg, 1)
    latest_weight = latest_weight_for_lot(lot, records_by_lot)
    if latest_weight is None:
        return None
    qty = sum((allocation.quantity_allocated or 0) for allocation in allocations_by_lot.get(lot.id, [])) or lot.initial_count or 0
    if qty <= 0:
        return None
    return round((qty * latest_weight) / 1000, 1)


def lot_mortality_total(lot_id: int, records_by_lot: dict[int, list[DailyManagement]]):
    return int(sum(record.mortality_qty or 0 for record in records_by_lot.get(lot_id, [])))


def survival_estimate_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]]):
    if not lot.initial_count:
        return None
    losses = lot_mortality_total(lot.id, records_by_lot) + lot_total_harvested_units(lot.id)
    survivors = max(lot.initial_count - losses, 0)
    return round((survivors / lot.initial_count) * 100, 1)


def average_daily_growth(records: list[DailyManagement]):
    weighted_records = [record for record in sorted(records, key=lambda item: (item.manage_date, item.id)) if record.average_weight_g is not None]
    if len(weighted_records) < 2:
        return None
    latest = weighted_records[-1]
    baseline = None
    for candidate in reversed(weighted_records[:-1]):
        days = (latest.manage_date - candidate.manage_date).days
        if days >= 5:
            baseline = candidate
            break
    if baseline is None:
        baseline = weighted_records[-2]
    days = max((latest.manage_date - baseline.manage_date).days, 1)
    return max((latest.average_weight_g - baseline.average_weight_g) / days, 0)


def growth_weekly_pct(records: list[DailyManagement]):
    weighted_records = [record for record in sorted(records, key=lambda item: (item.manage_date, item.id)) if record.average_weight_g is not None]
    if len(weighted_records) < 2:
        return None
    latest = weighted_records[-1]
    baseline = None
    for candidate in reversed(weighted_records[:-1]):
        days = (latest.manage_date - candidate.manage_date).days
        if days >= 5:
            baseline = candidate
            break
    if baseline is None:
        baseline = weighted_records[-2]
    if not baseline.average_weight_g:
        return None
    return round(((latest.average_weight_g - baseline.average_weight_g) / baseline.average_weight_g) * 100, 1)


def phase_growth_baselines():
    rows = DailyManagement.query.join(Lot, Lot.id == DailyManagement.lot_id).filter(DailyManagement.average_weight_g.isnot(None)).order_by(DailyManagement.lot_id, DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all()
    grouped = defaultdict(list)
    for record in rows:
        grouped[record.lot_id].append(record)
    phase_values = defaultdict(list)
    for lot_id, records in grouped.items():
        lot = records[0].lot if records and records[0].lot else None
        if not lot:
            continue
        growth = average_daily_growth(records)
        if growth is not None and growth > 0:
            phase_values[lot.phase].append(growth)
    return {phase: (sum(values) / len(values) if values else None) for phase, values in phase_values.items()}


def phase_fcr_baselines():
    values = defaultdict(list)
    lots = Lot.query.all()
    for lot in lots:
        harvested_kg = lot_total_harvested_kg(lot.id)
        if harvested_kg <= 0:
            continue
        total_feed = db.session.query(func.coalesce(func.sum(DailyManagement.feed_offered_kg), 0)).filter(DailyManagement.lot_id == lot.id).scalar() or 0
        if total_feed > 0:
            values[lot.phase].append(round(total_feed / harvested_kg, 2))
    return {phase: (sum(items) / len(items) if items else None) for phase, items in values.items()}


def predict_lot_metrics(lot: Lot, records_by_lot: dict[int, list[DailyManagement]], allocations_by_lot: dict[int, list[LotUnitAllocation]], phase_growth_map: dict, phase_fcr_map: dict, today: date):
    records = records_by_lot.get(lot.id, [])
    projection_7 = smart_growth_projection(lot, 7)
    projection_14 = smart_growth_projection(lot, 14)
    current_weight = projection_7.get('current_weight_g') or latest_weight_for_lot(lot, records_by_lot) or 0
    current_biomass = latest_biomass_for_lot(lot, records_by_lot, allocations_by_lot)
    if not current_biomass and current_weight:
        current_biomass = round((modeled_live_count_for_lot(lot) * current_weight) / 1000, 2)
    survival_now = survival_estimate_for_lot(lot, records_by_lot)
    standard_survival = standard_survival_pct_for_lot(lot, on_date=today)
    if standard_survival is not None and survival_now is not None:
        survival_now = min(survival_now, standard_survival)
    recent_mortality = sum((record.mortality_qty or 0) for record in records if (today - record.manage_date).days <= 7)
    predicted_survival = survival_now
    if survival_now is not None and lot.initial_count:
        predicted_survival = round(max(survival_now - ((recent_mortality / lot.initial_count) * 100), 0), 1)
    total_feed = sum(record.feed_offered_kg or 0 for record in records)
    partial_fcr = round(total_feed / current_biomass, 2) if current_biomass and current_biomass > 0 else None
    predicted_fcr = partial_fcr if partial_fcr is not None else phase_fcr_map.get(lot.phase)
    if predicted_fcr is not None:
        predicted_fcr = round(predicted_fcr, 2)

    daily_growth = projection_7.get('daily_gain_g') or (phase_growth_map.get(lot.phase) or 0.08)
    harvest_date = None
    if current_weight and daily_growth > 0:
        if current_weight >= TARGET_HARVEST_WEIGHT_G:
            harvest_date = today
        else:
            days_left = int(round((TARGET_HARVEST_WEIGHT_G - current_weight) / daily_growth))
            days_left = max(days_left, 1)
            harvest_date = today + timedelta(days=days_left)

    confidence = int(min(97, max(projection_7.get('model_confidence') or 45, 40)))
    return {
        'lot': lot,
        'current_weight': round(current_weight, 1) if current_weight else None,
        'predicted_7d': projection_7.get('projected_weight_g'),
        'predicted_14d': projection_14.get('projected_weight_g'),
        'predicted_survival': predicted_survival,
        'predicted_fcr': predicted_fcr,
        'harvest_date': harvest_date,
        'confidence': confidence,
        'daily_growth': round(daily_growth, 3),
        'current_biomass': current_biomass,
        'partial_fcr': partial_fcr,
    }


def dashboard_data():
    today = date.today()
    default_start = month_start(today)
    start_date = parse_date(request.args.get('start_date'), default_start)
    end_date = parse_date(request.args.get('end_date'), today)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    selected_lot_id = parse_int(request.args.get('lot_id'))
    selected_unit_id = parse_int(request.args.get('unit_id'))
    selected_phase = (request.args.get('phase') or '').strip()
    selected_status = (request.args.get('status') or 'ativos').strip()
    selected_supplier = (request.args.get('supplier') or '').strip()

    config = get_water_reference_config()
    all_units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()
    all_lots = Lot.query.options(joinedload(Lot.unit)).order_by(Lot.start_date.desc(), Lot.lot_code.asc()).all()
    supplier_options = sorted({lot.larva_supplier for lot in all_lots if lot.larva_supplier})

    def lot_matches_filters(lot: Lot):
        if selected_lot_id and lot.id != selected_lot_id:
            return False
        if selected_phase and lot.phase != selected_phase:
            return False
        if selected_supplier and (lot.larva_supplier or '') != selected_supplier:
            return False
        if selected_status == 'ativos' and lot.status != 'ativo':
            return False
        if selected_status == 'encerrados' and lot.status != 'encerrado':
            return False
        if selected_unit_id:
            current_unit_ids = {unit.id for unit in lot_current_units(lot)}
            if selected_unit_id not in current_unit_ids and selected_unit_id != lot.unit_id:
                return False
        return True

    filtered_lots = [lot for lot in all_lots if lot_matches_filters(lot)]
    filtered_lot_ids = [lot.id for lot in filtered_lots]
    active_lots = [lot for lot in filtered_lots if lot.status == 'ativo' and lot.start_date <= today and (lot.end_date is None or lot.end_date >= today)]
    active_lot_ids = [lot.id for lot in active_lots]

    if filtered_lot_ids:
        mgmt_records = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot)).filter(DailyManagement.lot_id.in_(filtered_lot_ids)).order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all()
        water_records = WaterMonitoring.query.options(joinedload(WaterMonitoring.unit), joinedload(WaterMonitoring.lot)).filter(WaterMonitoring.lot_id.in_(filtered_lot_ids)).order_by(WaterMonitoring.monitor_date.asc(), WaterMonitoring.monitor_time.asc(), WaterMonitoring.id.asc()).all()
        nursery_records = NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot)).filter(NurseryFeeding.lot_id.in_(filtered_lot_ids)).order_by(NurseryFeeding.feed_date.asc(), NurseryFeeding.id.asc()).all()
        sales_records = Sale.query.options(joinedload(Sale.unit), joinedload(Sale.lot)).filter(Sale.lot_id.in_(filtered_lot_ids)).order_by(Sale.sale_date.desc(), Sale.id.desc()).all()
        transfer_records = Transfer.query.options(joinedload(Transfer.source_unit), joinedload(Transfer.destination_unit), joinedload(Transfer.source_lot)).filter(Transfer.source_lot_id.in_(filtered_lot_ids)).order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).all()
        allocation_records = LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit), joinedload(LotUnitAllocation.lot)).filter(LotUnitAllocation.lot_id.in_(filtered_lot_ids)).order_by(LotUnitAllocation.start_date.asc(), LotUnitAllocation.id.asc()).all()
    else:
        mgmt_records = []
        water_records = []
        nursery_records = []
        sales_records = []
        transfer_records = []
        allocation_records = []

    records_by_lot = defaultdict(list)
    for record in mgmt_records:
        records_by_lot[record.lot_id].append(record)

    allocation_records_today = [allocation for allocation in allocation_records if allocation.start_date <= today and (allocation.end_date is None or allocation.end_date >= today) and allocation.lot_id in active_lot_ids]
    if selected_unit_id:
        allocation_records_today = [allocation for allocation in allocation_records_today if allocation.unit_id == selected_unit_id]
    allocations_by_lot = defaultdict(list)
    for allocation in allocation_records_today:
        allocations_by_lot[allocation.lot_id].append(allocation)

    water_today_records = [record for record in water_records if record.monitor_date == today and (not selected_unit_id or record.unit_id == selected_unit_id)]
    water_today_unit_ids = {record.unit_id for record in water_today_records}
    mgmt_today_records = [record for record in mgmt_records if record.manage_date == today and (not selected_unit_id or record.unit_id == selected_unit_id)]
    mgmt_today_unit_ids = {record.unit_id for record in mgmt_today_records}
    water_alert_rows = build_water_alert_rows(water_today_records, config)

    semaforo = []
    nursery_ready = []
    active_unit_ids = {allocation.unit_id for allocation in allocation_records_today}
    active_units = [unit for unit in all_units if unit.id in active_unit_ids]
    for unit in active_units:
        lot = active_lot_for_unit(unit.id)
        if lot and lot.id not in active_lot_ids:
            continue
        water = latest_water(unit.id)
        mgmt = latest_mgmt(unit.id)
        status = 'verde'
        reasons = []
        if lot:
            if unit.phase == 'bercario':
                days = (today - lot.start_date).days
                if days >= TARGET_NURSERY_DAYS:
                    nursery_ready.append({'unit_name': unit.name, 'lot_code': lot.lot_code, 'days': days, 'start_date': lot.start_date})
                    status = 'amarelo'
                    reasons.append('pronto para transferência')
            current_water_status = water_status(water, config)
            if current_water_status != 'ok':
                status = 'vermelho'
                reasons.append(current_water_status)
            if unit.id not in water_today_unit_ids:
                if status != 'vermelho':
                    status = 'amarelo'
                reasons.append('sem água hoje')
            if unit.id not in mgmt_today_unit_ids:
                if status != 'vermelho':
                    status = 'amarelo'
                reasons.append('sem manejo hoje')
        else:
            status = 'cinza'
            reasons.append('sem lote')
        semaforo.append({'unit': unit, 'lot': lot, 'status': status, 'water': water, 'mgmt': mgmt, 'reasons': ', '.join(dict.fromkeys(reasons))})

    feed_snapshot = build_feed_stock_snapshot()
    total_stock = feed_snapshot['total_stock_kg']
    avg_daily_feed = db.session.query(func.coalesce(func.avg(DailyManagement.feed_offered_kg), 0)).filter(DailyManagement.manage_date >= today - timedelta(days=7)).scalar() or 0
    feed_coverage = round(total_stock / avg_daily_feed, 1) if avg_daily_feed > 0 else None

    latest_weight_map = {lot.id: latest_weight_for_lot(lot, records_by_lot) for lot in active_lots}
    latest_biomass_map = {lot.id: latest_biomass_for_lot(lot, records_by_lot, allocations_by_lot) for lot in active_lots}
    survival_map = {lot.id: survival_estimate_for_lot(lot, records_by_lot) for lot in active_lots}
    weekly_growth_values = [value for value in (growth_weekly_pct(records_by_lot.get(lot.id, [])) for lot in active_lots) if value is not None]
    avg_growth_weekly = round(sum(weekly_growth_values) / len(weekly_growth_values), 1) if weekly_growth_values else None
    avg_weight = round(sum(value for value in latest_weight_map.values() if value is not None) / max(len([value for value in latest_weight_map.values() if value is not None]), 1), 1) if any(value is not None for value in latest_weight_map.values()) else None
    avg_survival = round(sum(value for value in survival_map.values() if value is not None) / max(len([value for value in survival_map.values() if value is not None]), 1), 1) if any(value is not None for value in survival_map.values()) else None

    total_feed_offered_active = round(sum(record.feed_offered_kg or 0 for record in mgmt_records if record.lot_id in active_lot_ids), 1)
    total_biomass_active = round(sum(value for value in latest_biomass_map.values() if value is not None), 1)
    partial_fcr = round(total_feed_offered_active / total_biomass_active, 2) if total_biomass_active > 0 else None

    lot_summaries = [lot_financial_summary(lot) for lot in active_lots]
    total_feed_cost = round(sum(summary['feed_cost'] for summary in lot_summaries), 2)
    total_supply_cost = round(sum(summary.get('supply_cost', 0) for summary in lot_summaries), 2)
    total_fixed_cost = round(sum(summary['fixed_cost'] for summary in lot_summaries), 2)
    total_cost_active = round(sum(summary['total_cost'] for summary in lot_summaries), 2)
    estimated_cost_per_kg = round(total_cost_active / total_biomass_active, 2) if total_biomass_active > 0 else None

    sales_in_period = [sale for sale in sales_records if start_date <= sale.sale_date <= end_date and (not selected_unit_id or sale.unit_id == selected_unit_id)]
    sales_summaries = [summary for summary in (sale_financial_summary(sale) for sale in sales_in_period) if summary]
    total_revenue_period = round(sum(summary['revenue'] for summary in sales_summaries), 2)
    total_profit_period = round(sum(summary['profit'] for summary in sales_summaries), 2)

    nursery_today_records = [record for record in nursery_records if record.feed_date == today and (not selected_unit_id or record.unit_id == selected_unit_id)]
    avg_intestinal_score = round(sum(record.intestinal_score or 0 for record in nursery_today_records) / len([record for record in nursery_today_records if record.intestinal_score is not None]), 1) if any(record.intestinal_score is not None for record in nursery_today_records) else None

    latest_water_by_unit = {}
    for record in sorted(water_records, key=lambda item: (item.monitor_date, item.monitor_time or time.min, item.id), reverse=True):
        latest_water_by_unit.setdefault(record.unit_id, record)
    latest_mgmt_by_unit = {}
    for record in sorted(mgmt_records, key=lambda item: (item.manage_date, item.id), reverse=True):
        latest_mgmt_by_unit.setdefault(record.unit_id, record)
    latest_nursery_by_unit = {}
    for record in sorted(nursery_records, key=lambda item: (item.feed_date, item.id), reverse=True):
        latest_nursery_by_unit.setdefault(record.unit_id, record)

    operation_rows = []
    for unit in active_units[:8]:
        lot = active_lot_for_unit(unit.id)
        if not lot or lot.id not in active_lot_ids:
            continue
        water = latest_water_by_unit.get(unit.id)
        mgmt = latest_mgmt_by_unit.get(unit.id)
        nursery_feed = latest_nursery_by_unit.get(unit.id)
        status_label = 'Normal'
        if water and water_alerts_for_record(water, config):
            status_label = 'Atenção'
        elif not water or water.monitor_date != today or not mgmt or mgmt.manage_date != today:
            status_label = 'Pendente'
        operation_rows.append({
            'unit_name': unit.name,
            'lot_code': lot.lot_code,
            'last_monitoring': f"{water.monitor_date.strftime('%d/%m')} {water.monitor_time.strftime('%H:%M') if water and water.monitor_time else ''}".strip() if water else '—',
            'last_management': mgmt.manage_date.strftime('%d/%m') if mgmt else (nursery_feed.feed_date.strftime('%d/%m') if nursery_feed else '—'),
            'consumption_today': round(sum(record.feed_offered_kg or 0 for record in mgmt_today_records if record.unit_id == unit.id), 1),
            'status': status_label,
        })

    phase_growth_map = phase_growth_baselines()
    phase_fcr_map = phase_fcr_baselines()
    prediction_rows = [predict_lot_metrics(lot, records_by_lot, allocations_by_lot, phase_growth_map, phase_fcr_map, today) for lot in active_lots]
    prediction_rows.sort(key=lambda row: (row['harvest_date'] or date.max, row['lot'].lot_code))
    upcoming_harvest_count = sum(1 for row in prediction_rows if row['harvest_date'] and row['harvest_date'] <= today + timedelta(days=14))

    financial_rows = []
    for summary in lot_summaries:
        lot = summary['lot']
        biomass = latest_biomass_map.get(lot.id)
        revenue_realized = lot_total_revenue(lot.id)
        result_value = round(revenue_realized - summary['total_cost'], 2)
        financial_rows.append({
            'lot_code': lot.lot_code,
            'cost_total': summary['total_cost'],
            'biomass': biomass,
            'cost_per_kg': round(summary['total_cost'] / biomass, 2) if biomass else None,
            'result': result_value,
        })
    financial_rows.sort(key=lambda row: row['cost_total'], reverse=True)

    biomass_unit_rows = []
    for unit in active_units:
        lot = active_lot_for_unit(unit.id)
        if not lot or lot.id not in active_lot_ids:
            continue
        unit_biomass = None
        latest_unit_mgmt = latest_mgmt_by_unit.get(unit.id)
        if latest_unit_mgmt and latest_unit_mgmt.estimated_biomass_kg is not None:
            unit_biomass = round(latest_unit_mgmt.estimated_biomass_kg, 1)
        else:
            allocation = next((allocation for allocation in allocations_by_lot.get(lot.id, []) if allocation.unit_id == unit.id), None)
            if allocation and latest_weight_map.get(lot.id) is not None and allocation.quantity_allocated:
                unit_biomass = round((allocation.quantity_allocated * latest_weight_map[lot.id]) / 1000, 1)
        biomass_unit_rows.append({'unit_name': unit.name, 'biomass': unit_biomass or 0})
    biomass_unit_rows.sort(key=lambda row: row['biomass'], reverse=True)

    growth_alerts = []
    for lot in active_lots:
        weekly = growth_weekly_pct(records_by_lot.get(lot.id, []))
        if weekly is not None and avg_growth_weekly is not None and weekly < (avg_growth_weekly * 0.75):
            growth_alerts.append({'lot_code': lot.lot_code, 'value': weekly})

    critical_alerts = []
    for row in water_alert_rows[:3]:
        critical_alerts.append({'level': 'high', 'text': f"{row['unit_name']} · {row['message']}"})
    for record in sorted(nursery_today_records, key=lambda item: (item.intestinal_score or 99, item.unit.name if item.unit else '')):
        if record.intestinal_score is not None and record.intestinal_score <= 1:
            critical_alerts.append({'level': 'medium', 'text': f"{record.unit.name if record.unit else 'Berçário'} · score intestinal {record.intestinal_score}"})
    for item in growth_alerts[:2]:
        critical_alerts.append({'level': 'medium', 'text': f"{item['lot_code']} · crescimento {item['value']}% abaixo da curva esperada"})
    if not critical_alerts:
        critical_alerts.append({'level': 'ok', 'text': 'Nenhum alerta crítico identificado hoje.'})
    critical_alerts = critical_alerts[:4]

    pending_items = []
    water_pending_count = sum(1 for item in semaforo if item['lot'] and item['unit'].id not in water_today_unit_ids)
    management_pending_count = sum(1 for item in semaforo if item['lot'] and item['unit'].id not in mgmt_today_unit_ids)
    if water_pending_count:
        pending_items.append(f'{water_pending_count} unidade(s) sem monitoramento hoje')
    if management_pending_count:
        pending_items.append(f'{management_pending_count} unidade(s) sem manejo hoje')
    if feed_snapshot['low_stock_count']:
        pending_items.append(f"{feed_snapshot['low_stock_count']} item(ns) de ração com estoque baixo")
    missing_supplier_count = sum(1 for lot in active_lots if not lot.larva_supplier)
    if missing_supplier_count:
        pending_items.append(f'{missing_supplier_count} lote(s) sem fornecedor de PL cadastrado')
    if not pending_items:
        pending_items.append('Operação sem pendências críticas no momento.')
    pending_items = pending_items[:4]

    movement_rows = []
    for record in water_today_records[:5]:
        movement_rows.append({'sort_key': datetime.combine(record.monitor_date, record.monitor_time or time.min), 'date_label': f"{record.monitor_date.strftime('%d/%m/%Y')} {record.monitor_time.strftime('%H:%M') if record.monitor_time else ''}".strip(), 'type': 'Monitoramento', 'entity': record.unit.name if record.unit else 'Unidade', 'user': 'Equipe', 'detail': f"OD {record.dissolved_oxygen or '—'} mg/L · Temp. {record.temperature_c or '—'} °C"})
    for record in mgmt_today_records[:5]:
        movement_rows.append({'sort_key': datetime.combine(record.manage_date, time(hour=8)), 'date_label': record.manage_date.strftime('%d/%m/%Y'), 'type': 'Manejo', 'entity': record.unit.name if record.unit else 'Unidade', 'user': 'Equipe', 'detail': f"Ração ofertada {round(record.feed_offered_kg or 0, 1)} kg"})
    for record in nursery_today_records[:5]:
        movement_rows.append({'sort_key': datetime.combine(record.feed_date, time(hour=7, minute=30)), 'date_label': record.feed_date.strftime('%d/%m/%Y'), 'type': 'Berçário', 'entity': record.unit.name if record.unit else 'Berçário', 'user': 'Equipe', 'detail': f"Quantidade {round(record.quantity_kg or 0, 1)} kg · score {record.intestinal_score if record.intestinal_score is not None else '—'}"})
    for record in transfer_records[:3]:
        movement_rows.append({'sort_key': datetime.combine(record.transfer_date, time(hour=7, minute=45)), 'date_label': record.transfer_date.strftime('%d/%m/%Y'), 'type': 'Transferência', 'entity': record.source_lot.lot_code if record.source_lot else 'Lote', 'user': 'Equipe', 'detail': f"{record.transferred_qty} un. para {record.destination_unit.name if record.destination_unit else 'destino'}"})
    for record in sales_in_period[:3]:
        movement_rows.append({'sort_key': datetime.combine(record.sale_date, time(hour=7, minute=30)), 'date_label': record.sale_date.strftime('%d/%m/%Y'), 'type': 'Despesca', 'entity': record.lot.lot_code if record.lot else 'Lote', 'user': 'Equipe', 'detail': f"{round(record.quantity_kg or 0, 1)} kg vendidos"})
    movement_rows.sort(key=lambda row: row['sort_key'], reverse=True)
    movement_rows = movement_rows[:6]

    chart_colors = ['#3b82f6', '#22c55e', '#7c3aed', '#f97316']
    growth_chart_labels = sorted({record.manage_date.strftime('%d/%m') for lot in active_lots[:4] for record in records_by_lot.get(lot.id, []) if record.average_weight_g is not None})[-6:]
    growth_chart_datasets = []
    for idx, lot in enumerate(active_lots[:4]):
        lookup = {record.manage_date.strftime('%d/%m'): round(record.average_weight_g, 2) for record in records_by_lot.get(lot.id, []) if record.average_weight_g is not None}
        if not lookup:
            continue
        growth_chart_datasets.append({'label': lot.lot_code, 'data': [lookup.get(label) for label in growth_chart_labels], 'borderColor': chart_colors[idx % len(chart_colors)], 'backgroundColor': chart_colors[idx % len(chart_colors)]})

    chart_payload = {
        'growth': {'labels': growth_chart_labels, 'datasets': growth_chart_datasets},
        'biomass': {'labels': [row['unit_name'] for row in biomass_unit_rows[:6]], 'data': [row['biomass'] for row in biomass_unit_rows[:6]]},
    }

    prediction_table_rows = []
    for row in prediction_rows[:6]:
        prediction_table_rows.append({
            'lot_code': row['lot'].lot_code,
            'current_weight': row['current_weight'],
            'predicted_7d': row['predicted_7d'],
            'predicted_14d': row['predicted_14d'],
            'predicted_survival': row['predicted_survival'],
            'predicted_fcr': row['predicted_fcr'],
            'harvest_date': row['harvest_date'],
            'confidence': row['confidence'],
        })

    return {
        'today': today,
        'start_date': start_date,
        'end_date': end_date,
        'filters': {
            'lot_id': selected_lot_id,
            'unit_id': selected_unit_id,
            'phase': selected_phase,
            'status': selected_status,
            'supplier': selected_supplier,
        },
        'filter_options': {
            'lots': all_lots,
            'units': all_units,
            'suppliers': supplier_options,
        },
        'summary': {
            'lots_active': len(active_lots),
            'units_active': len(active_units),
            'biomass_estimated': total_biomass_active,
            'feed_today': round(sum(record.feed_offered_kg or 0 for record in mgmt_today_records), 1),
            'cost_accumulated': total_cost_active,
            'revenue_period': total_revenue_period,
            'result_period': total_profit_period,
            'next_harvests': upcoming_harvest_count,
        },
        'alerts': critical_alerts,
        'pendings': pending_items,
        'operation': {
            'feed_today': round(sum(record.feed_offered_kg or 0 for record in mgmt_today_records), 1),
            'monitored_units': len(water_today_unit_ids),
            'monitored_total': len(active_units),
            'nursery_fed_count': len(nursery_today_records),
            'avg_intestinal_score': avg_intestinal_score,
            'rows': operation_rows,
        },
        'production': {
            'avg_weight': avg_weight,
            'weekly_growth_pct': avg_growth_weekly,
            'avg_survival': avg_survival,
            'partial_fcr': partial_fcr,
            'biomass_rows': biomass_unit_rows,
        },
        'financial': {
            'feed_cost': total_feed_cost,
            'supply_cost': total_supply_cost,
            'fixed_cost': total_fixed_cost,
            'estimated_cost_per_kg': estimated_cost_per_kg,
            'month_profit': total_profit_period,
            'rows': financial_rows[:6],
        },
        'predictions': {
            'avg_7d': round(sum(row['predicted_7d'] for row in prediction_rows if row['predicted_7d'] is not None) / max(len([row for row in prediction_rows if row['predicted_7d'] is not None]), 1), 1) if any(row['predicted_7d'] is not None for row in prediction_rows) else None,
            'avg_14d': round(sum(row['predicted_14d'] for row in prediction_rows if row['predicted_14d'] is not None) / max(len([row for row in prediction_rows if row['predicted_14d'] is not None]), 1), 1) if any(row['predicted_14d'] is not None for row in prediction_rows) else None,
            'avg_survival': round(sum(row['predicted_survival'] for row in prediction_rows if row['predicted_survival'] is not None) / max(len([row for row in prediction_rows if row['predicted_survival'] is not None]), 1), 1) if any(row['predicted_survival'] is not None for row in prediction_rows) else None,
            'avg_fcr': round(sum(row['predicted_fcr'] for row in prediction_rows if row['predicted_fcr'] is not None) / max(len([row for row in prediction_rows if row['predicted_fcr'] is not None]), 1), 2) if any(row['predicted_fcr'] is not None for row in prediction_rows) else None,
            'next_harvest_date': prediction_rows[0]['harvest_date'] if prediction_rows else None,
            'avg_confidence': round(sum(row['confidence'] for row in prediction_rows) / len(prediction_rows), 0) if prediction_rows else None,
            'rows': prediction_table_rows,
        },
        'movements': movement_rows,
        'chart_payload': chart_payload,
        'units': all_units,
        'water_pending': water_pending_count,
        'management_pending': management_pending_count,
        'water_alerts': len(water_alert_rows),
        'water_alert_rows': water_alert_rows,
        'nursery_ready': nursery_ready,
        'feed_stock_kg': round(total_stock, 1),
        'feed_low_stock_count': feed_snapshot['low_stock_count'],
        'feed_coverage_days': feed_coverage,
        'avg_daily_feed_kg': round(avg_daily_feed, 1),
        'semaforo': semaforo,
        'reference_summary': build_reference_summary(config),
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
@app.route('/dashboard')
@login_required
@requires_permission('dashboard')
def index():
    return render_template('dashboard.html', data=dashboard_data())


@app.get('/dashboard/detail/<kind>')
@login_required
@requires_permission('dashboard')
def dashboard_detail(kind):
    data = dashboard_data()
    today = data['today']

    if kind == 'water-pending':
        rows = []
        for row in data['semaforo']:
            if row['lot'] and 'sem água hoje' in row['reasons']:
                rows.append({
                    'unit_name': row['unit'].name,
                    'phase_label': phase_label(row['unit'].phase),
                    'lot_code': row['lot'].lot_code,
                    'last_water_date': row['water'].monitor_date if row['water'] else None,
                    'last_water_shift': shift_label(row['water'].shift) if row['water'] else '—',
                    'observation': 'Sem leitura lançada hoje',
                })
        return render_template('dashboard_detail.html', kind=kind, title='Pendências de água', subtitle='Unidades com lote ativo e sem leitura registrada hoje.', metric_value=len(rows), metric_suffix='unidades', rows=rows, today=today)

    if kind == 'water-alerts':
        return render_template('dashboard_detail.html', kind=kind, title='Alertas de água do dia', subtitle='Detalhe dos parâmetros fora da faixa e em qual viveiro isso aconteceu.', metric_value=len(data['water_alert_rows']), metric_suffix='alertas', rows=data['water_alert_rows'], today=today)

    if kind == 'management-pending':
        rows = []
        for row in data['semaforo']:
            if row['lot'] and 'sem manejo hoje' in row['reasons']:
                rows.append({
                    'unit_name': row['unit'].name,
                    'phase_label': phase_label(row['unit'].phase),
                    'lot_code': row['lot'].lot_code,
                    'last_management_date': row['mgmt'].manage_date if row['mgmt'] else None,
                    'observation': 'Sem lançamento operacional hoje',
                })
        return render_template('dashboard_detail.html', kind=kind, title='Pendências de manejo', subtitle='Unidades com lote ativo e sem lançamento de manejo no dia.', metric_value=len(rows), metric_suffix='unidades', rows=rows, today=today)

    if kind == 'nursery-ready':
        rows = data['nursery_ready']
        return render_template('dashboard_detail.html', kind=kind, title='Berçários prontos para transferência', subtitle='Berçários que já atingiram a meta configurada para saída à engorda.', metric_value=len(rows), metric_suffix='berçários', rows=rows, today=today, target_nursery_days=TARGET_NURSERY_DAYS)

    if kind == 'feed-stock':
        snapshot = build_feed_stock_snapshot()
        movement_rows = FeedInventory.query.options(joinedload(FeedInventory.feed_product), joinedload(FeedInventory.unit), joinedload(FeedInventory.lot)).order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).limit(100).all()
        return render_template('dashboard_detail.html', kind=kind, title='Estoque de ração', subtitle='Saldo consolidado por produto, alertas de estoque mínimo e últimos movimentos lançados.', metric_value=data['feed_stock_kg'], metric_suffix='kg', rows=snapshot['rows'], movement_rows=movement_rows, today=today, low_stock_count=snapshot['low_stock_count'])

    if kind == 'feed-coverage':
        recent_management = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot), joinedload(DailyManagement.feed_product)).filter(DailyManagement.manage_date >= today - timedelta(days=7)).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).all()
        return render_template('dashboard_detail.html', kind=kind, title='Cobertura de ração', subtitle='Estimativa baseada no estoque atual e na média recente de oferta de ração.', metric_value=data['feed_coverage_days'] if data['feed_coverage_days'] is not None else 'N/D', metric_suffix='dias', rows=recent_management, today=today, avg_daily_feed_kg=data['avg_daily_feed_kg'], feed_stock_kg=data['feed_stock_kg'])

    abort(404)


@app.post('/water/reference-ranges')
@login_required
@requires_permission('water_manage')
def update_water_reference_ranges():
    config = get_water_reference_config()
    fields = [
        'od_min', 'od_max', 'ph_min', 'ph_max', 'temperature_min', 'temperature_max',
        'salinity_min', 'salinity_max', 'transparency_min', 'transparency_max',
        'ammonia_min', 'ammonia_max', 'nitrite_min', 'nitrite_max',
        'nitrate_min', 'nitrate_max', 'alkalinity_min', 'alkalinity_max', 'hardness_min', 'hardness_max',
    ]
    for field in fields:
        setattr(config, field, parse_float(request.form.get(field)))
    config.updated_at = datetime.utcnow()
    config.updated_by_id = getattr(current_user, 'id', None)
    db.session.commit()
    flash('Faixas de referência da água atualizadas.', 'success')
    return redirect(url_for('water_page', unit_id=request.args.get('unit_id', type=int)))


@app.route('/units', methods=['GET', 'POST'])
@login_required
@requires_permission('units_view')
def units_page():
    edit_id = parse_int(request.args.get('edit_id'))
    edit_unit = db.session.get(Unit, edit_id) if edit_id else None
    if request.method == 'POST':
        if not user_can_manage_units(current_user):
            abort(403)
        form_mode = request.form.get('form_mode', 'create')
        target = db.session.get(Unit, parse_int(request.form.get('unit_id'))) if form_mode == 'edit' else Unit()
        if form_mode == 'edit' and not target:
            flash('Unidade não encontrada para edição.', 'danger')
            return redirect(url_for('units_page'))
        name = request.form.get('name', '').strip()
        if not name:
            flash('Informe o nome do viveiro/unidade.', 'danger')
            return redirect(url_for('units_page'))
        code = (request.form.get('code') or '').strip().upper() or suggest_unit_code(name)
        existing = Unit.query.filter(func.lower(Unit.code) == code.lower(), Unit.id != getattr(target, 'id', 0)).first()
        if existing:
            flash('Já existe uma unidade com esse código.', 'danger')
            return redirect(url_for('units_page', edit_id=getattr(target, 'id', None)))
        target.code = code
        target.name = name
        target.area_m2 = parse_float(request.form.get('area_m2'), 0) or 0
        target.phase = request.form.get('phase') or 'engorda'
        target.structure_type = request.form.get('structure_type') or 'escavado'
        target.active = bool(request.form.get('active', '1') == '1')
        if form_mode != 'edit':
            db.session.add(target)
        db.session.commit()
        flash('Unidade salva com sucesso.' if form_mode == 'edit' else 'Unidade cadastrada com sucesso.', 'success')
        return redirect(url_for('units_page'))
    units = Unit.query.order_by(Unit.phase, Unit.name).all()
    return render_template('units.html', units=units, edit_unit=edit_unit)


@app.route('/lots', methods=['GET', 'POST'])
@login_required
@requires_permission('lots_manage')
def lots_page():
    edit_lot_id = parse_int(request.args.get('edit_lot_id'))
    edit_cost_id = parse_int(request.args.get('edit_cost_id'))
    edit_lot = db.session.get(Lot, edit_lot_id) if edit_lot_id else None
    edit_cost = db.session.get(FixedCost, edit_cost_id) if edit_cost_id else None
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'lot')
        if form_mode in {'fixed_cost', 'edit_fixed_cost'}:
            cost = db.session.get(FixedCost, parse_int(request.form.get('fixed_cost_id'))) if form_mode == 'edit_fixed_cost' else FixedCost()
            if form_mode == 'edit_fixed_cost' and not cost:
                flash('Custo fixo não encontrado.', 'warning')
                return redirect(url_for('lots_page'))
            cost.name = (request.form.get('name') or 'Funcionário').strip()
            cost.monthly_amount = parse_float(request.form.get('monthly_amount'), 0) or 0
            cost.start_date = parse_date(request.form.get('start_date'), date.today())
            cost.end_date = parse_date(request.form.get('end_date')) if request.form.get('end_date') else None
            cost.active = bool(request.form.get('active', '1') == '1')
            cost.notes = request.form.get('notes')
            if form_mode == 'fixed_cost':
                db.session.add(cost)
            db.session.commit()
            flash('Custo fixo salvo com sucesso.', 'success')
            return redirect(url_for('lots_page'))
        if form_mode == 'close_lot':
            lot = db.session.get(Lot, int(request.form['lot_id']))
            if not lot:
                flash('Lote não encontrado.', 'warning')
                return redirect(url_for('lots_page'))
            close_date = parse_date(request.form.get('end_date'), date.today())
            close_lot(lot, close_date, reason='encerrado_manual')
            db.session.commit()
            flash('Lote encerrado manualmente.', 'success')
            return redirect(url_for('lots_page'))
        lot = db.session.get(Lot, parse_int(request.form.get('lot_id'))) if form_mode == 'edit_lot' else Lot()
        if form_mode == 'edit_lot' and not lot:
            flash('Lote não encontrado para edição.', 'warning')
            return redirect(url_for('lots_page'))
        old_unit_id = lot.unit_id if form_mode == 'edit_lot' else None
        old_initial_count = lot.initial_count if form_mode == 'edit_lot' else None
        old_start_date = lot.start_date if form_mode == 'edit_lot' else None
        lot.lot_code = (request.form['lot_code'] or '').strip().upper()
        lot.phase = request.form['phase']
        lot.start_date = parse_date(request.form['start_date'])
        lot.unit_id = int(request.form['unit_id'])
        lot.initial_count = int(request.form['initial_count'] or 0)
        lot.estimated_weight_g = parse_float(request.form.get('estimated_weight_g'), 0) or 0
        lot.status = request.form.get('status') or lot.status or 'ativo'
        lot.larva_supplier = (request.form.get('larva_supplier') or '').strip() or None
        lot.entry_pl_stage = parse_int(request.form.get('entry_pl_stage'))
        lot.notes = request.form.get('notes')
        if lot.status == 'encerrado' and request.form.get('end_date'):
            lot.end_date = parse_date(request.form.get('end_date'))
        if form_mode != 'edit_lot':
            db.session.add(lot)
            db.session.flush()
            db.session.add(LotUnitAllocation(lot_id=lot.id, unit_id=lot.unit_id, start_date=lot.start_date, quantity_allocated=lot.initial_count, notes='Alocação inicial do lote.'))
        else:
            sync_lot_allocations_after_lot_edit(lot, old_unit_id, old_initial_count, old_start_date)
        db.session.commit()
        flash('Lote salvo com sucesso.' if form_mode == 'edit_lot' else 'Lote cadastrado.', 'success')
        return redirect(url_for('lots_page'))
    lots = Lot.query.order_by(Lot.start_date.desc(), Lot.id.desc()).all()
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    fixed_costs = FixedCost.query.order_by(FixedCost.start_date.desc(), FixedCost.id.desc()).all()
    lot_summaries = [lot_financial_summary(lot) for lot in lots]
    return render_template('lots.html', lots=lots, units=units, fixed_costs=fixed_costs, lot_summaries=lot_summaries, today=date.today(), lot_current_units=lot_current_units, edit_lot=edit_lot, edit_cost=edit_cost)


@app.post('/water/import-sheet')
@login_required
@requires_permission('water_manage')
def import_water_sheet():
    upload = request.files.get('sheet_image')
    requested_sheet_type = (request.form.get('sheet_type', 'auto') or 'auto').strip().lower()
    sheet_date = parse_date(request.form.get('sheet_date'), date.today())

    if requested_sheet_type not in {'auto', 'day', 'night'}:
        flash('Tipo de ficha inválido.', 'danger')
        return redirect(url_for('water_page'))

    if not upload or not upload.filename:
        flash('Envie a foto da ficha antes de importar.', 'warning')
        return redirect(url_for('water_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    try:
        file_bytes = upload.read()
        detected_sheet_type = requested_sheet_type
        if requested_sheet_type == 'auto':
            detected_sheet_type = detect_water_sheet_type_with_openai(
                file_bytes=file_bytes,
                filename=upload.filename,
                content_type=upload.mimetype,
            )

        readings = extract_water_sheet_data_with_openai(
            file_bytes=file_bytes,
            filename=upload.filename,
            content_type=upload.mimetype,
            sheet_type=detected_sheet_type,
            sheet_date=sheet_date,
            units=units,
        )
        preview_rows, warnings = build_water_import_preview(readings, units, detected_sheet_type, sheet_date)
    except Exception as exc:
        flash(f'Não consegui ler a ficha automaticamente: {exc}', 'danger')
        return redirect(url_for('water_page'))

    if not preview_rows:
        flash('Não encontrei leituras válidas na ficha para montar a prévia.', 'warning')
        return redirect(url_for('water_page'))

    store_pending_water_import(detected_sheet_type, sheet_date, preview_rows, warnings)
    flash(
        f'Prévia da importação gerada. Ficha identificada como {water_sheet_type_label(detected_sheet_type)}. Confira os dados antes de confirmar.',
        'success'
    )
    return redirect(url_for('water_page', show_import_preview=1))


@app.post('/water/import-sheet/confirm')
@login_required
@requires_permission('water_manage')
def confirm_import_water_sheet():
    pending = get_pending_water_import()
    if not pending:
        flash('A prévia da importação expirou. Gere a leitura da ficha novamente.', 'warning')
        return redirect(url_for('water_page'))

    selected_indices = {int(value) for value in request.form.getlist('selected_indices') if str(value).isdigit()}
    unit_ids = request.form.getlist('unit_id')
    monitor_dates = request.form.getlist('monitor_date')
    slot_times = request.form.getlist('time')
    row_names = request.form.getlist('row_name')
    oxygens = request.form.getlist('dissolved_oxygen')
    temperatures = request.form.getlist('temperature_c')
    ph_values = request.form.getlist('ph')
    ammonias = request.form.getlist('ammonia')
    nitrites = request.form.getlist('nitrite')

    created = 0
    updated = 0
    ignored = 0

    total_rows = len(slot_times)
    for idx in range(total_rows):
        if idx not in selected_indices:
            ignored += 1
            continue

        unit_id = parse_int(unit_ids[idx])
        slot_time = parse_time(slot_times[idx])
        monitor_date = parse_date(monitor_dates[idx], date.today())
        if not unit_id or not slot_time:
            ignored += 1
            continue

        values = {
            'dissolved_oxygen': parse_float(oxygens[idx]),
            'temperature_c': parse_float(temperatures[idx]),
            'ph': parse_float(ph_values[idx]),
            'ammonia': parse_float(ammonias[idx]),
            'nitrite': parse_float(nitrites[idx]),
            'observation': f'Importado de ficha {"diurna" if pending.get("sheet_type") == "day" else "noturna"} em {datetime.utcnow().strftime("%d/%m/%Y %H:%M")}',
        }
        if all(values.get(field) is None for field in ['dissolved_oxygen', 'temperature_c', 'ph', 'ammonia', 'nitrite']):
            ignored += 1
            continue

        result = upsert_water_reading(unit_id, monitor_date, slot_time, values)
        if result == 'created':
            created += 1
        else:
            updated += 1

    db.session.commit()
    pop_pending_water_import()
    flash(f'Importação confirmada. {created} leitura(s) criada(s), {updated} atualizada(s) e {ignored} ignorada(s).', 'success')
    return redirect(url_for('water_page'))


@app.post('/water/import-sheet/cancel')
@login_required
@requires_permission('water_manage')
def cancel_import_water_sheet():
    pop_pending_water_import()
    flash('Prévia da importação cancelada.', 'warning')
    return redirect(url_for('water_page'))


@app.route('/water', methods=['GET', 'POST'])
@login_required
@requires_permission('water_manage')
def water_page():
    if request.method == 'POST':
        mode = request.form.get('entry_mode', 'single')
        unit_id = int(request.form['unit_id'])
        monitor_date = parse_date(request.form.get('monitor_date'), date.today())
        lot = active_lot_for_unit(unit_id, on_date=monitor_date)

        if mode == 'batch':
            slot_times = request.form.getlist('slot_time')
            temperatures = parse_multi_float_list(request.form.getlist('temperature_c'))
            oxygens = parse_multi_float_list(request.form.getlist('dissolved_oxygen'))
            ph_values = parse_multi_float_list(request.form.getlist('ph'))
            salinities = parse_multi_float_list(request.form.getlist('salinity'))
            transparencies = parse_multi_float_list(request.form.getlist('transparency_cm'))
            ammonias = parse_multi_float_list(request.form.getlist('ammonia'))
            nitrites = parse_multi_float_list(request.form.getlist('nitrite'))
            nitrates = parse_multi_float_list(request.form.getlist('nitrate'))
            alkalinities = parse_multi_float_list(request.form.getlist('alkalinity'))
            hardness_values = parse_multi_float_list(request.form.getlist('hardness'))
            observations = request.form.getlist('observation')

            created = 0
            for idx, slot in enumerate(slot_times):
                values = [temperatures[idx], oxygens[idx], ph_values[idx], salinities[idx], transparencies[idx], ammonias[idx], nitrites[idx], nitrates[idx], alkalinities[idx], hardness_values[idx], (observations[idx] or '').strip()]
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
                    nitrate=nitrates[idx],
                    alkalinity=alkalinities[idx],
                    hardness=hardness_values[idx],
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
            nitrate=parse_float(request.form.get('nitrate')),
            alkalinity=parse_float(request.form.get('alkalinity')),
            hardness=parse_float(request.form.get('hardness')),
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
        reference_config=get_water_reference_config(),
        reference_summary=build_reference_summary(),
        pending_water_import=get_pending_water_import(),
    )


@app.route('/management', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def management_page():
    if request.method == 'POST':
        unit_id = int(request.form['unit_id'])
        manage_date = parse_date(request.form['manage_date'], date.today())
        lot = active_lot_for_unit(unit_id, on_date=manage_date)
        feed_product = selected_feed_product_from_form()
        feed_offered_kg = parse_float(request.form.get('feed_offered_kg'), 0) or 0
        tray_score = parse_float(request.form.get('tray_score'))
        if tray_score is not None and not 0 <= tray_score <= 4:
            flash('O score de bandeja deve ficar entre 0 e 4.', 'danger')
            return redirect(url_for('management_page', unit_id=unit_id))
        validation_error = validate_feed_usage(feed_product, feed_offered_kg)
        if validation_error:
            flash(validation_error, 'danger')
            return redirect(url_for('management_page', unit_id=unit_id))
        supply_entries = management_supply_entries_from_form()
        supply_validation_error = validate_supply_usage(supply_entries)
        if supply_validation_error:
            flash(supply_validation_error, 'danger')
            return redirect(url_for('management_page', unit_id=unit_id))

        rec = DailyManagement(
            manage_date=manage_date,
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            feed_product_id=feed_product.id if feed_product else None,
            feed_offered_kg=feed_offered_kg,
            tray_score=tray_score,
            mortality_qty=parse_int(request.form.get('mortality_qty'), 0) or 0,
            average_weight_g=parse_float(request.form.get('average_weight_g')),
            estimated_biomass_kg=parse_float(request.form.get('estimated_biomass_kg')),
            notes=request.form.get('notes'),
            updated_at=datetime.utcnow(),
        )
        db.session.add(rec)
        db.session.flush()
        sync_management_feed_movement(rec, feed_product, feed_offered_kg)
        sync_management_supply_usages(rec, supply_entries)
        db.session.commit()
        flash('Manejo diário lançado com baixa automática da ração e dos insumos.', 'success')
        return redirect(url_for('management_page', unit_id=unit_id))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    selected_unit_id = request.args.get('unit_id', type=int)
    records_query = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot), joinedload(DailyManagement.feed_product)).join(Unit)
    if selected_unit_id:
        records_query = records_query.filter(DailyManagement.unit_id == selected_unit_id)
    records = records_query.order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).limit(100).all()
    edit_id = request.args.get('edit_id', type=int)
    edit_record = db.session.get(DailyManagement, edit_id) if edit_id else None
    feed_snapshot = build_feed_stock_snapshot()
    stock_by_product = {row['product'].id: row['stock_kg'] for row in feed_snapshot['rows']}
    feed_products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()
    supply_snapshot = build_supply_stock_snapshot()
    supply_products = SupplyProduct.query.order_by(SupplyProduct.active.desc(), SupplyProduct.name.asc()).all()
    supply_stock_by_product = {row['product'].id: row['stock_qty'] for row in supply_snapshot['rows']}
    cost_summary = management_cost_summary(selected_unit_id)
    record_ids = [record.id for record in records]
    supply_cost_by_record = {record_id: 0 for record_id in record_ids}
    usages_by_record = defaultdict(list)
    if record_ids:
        usages = ManagementSupplyUsage.query.options(joinedload(ManagementSupplyUsage.supply_product)).filter(ManagementSupplyUsage.management_id.in_(record_ids)).order_by(ManagementSupplyUsage.id.asc()).all()
        for usage in usages:
            supply_cost_by_record[usage.management_id] = round((supply_cost_by_record.get(usage.management_id, 0) + (usage.total_cost or 0)), 2)
            usages_by_record[usage.management_id].append(usage)
    return render_template(
        'management.html',
        units=units,
        records=records,
        today=date.today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
        feed_products=feed_products,
        stock_by_product=stock_by_product,
        supply_products=supply_products,
        supply_stock_by_product=supply_stock_by_product,
        supply_form_rows=management_supply_rows_for_form(),
        edit_supply_form_rows=management_supply_rows_for_form(edit_record) if edit_record else [],
        cost_summary=cost_summary,
        supply_cost_by_record=supply_cost_by_record,
        usages_by_record=usages_by_record,
    )


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
            'feed_product_id': previous_record.feed_product_id,
            'feed_offered_kg': previous_record.feed_offered_kg,
            'tray_score': previous_record.tray_score,
            'mortality_qty': previous_record.mortality_qty,
            'average_weight_g': previous_record.average_weight_g,
            'estimated_biomass_kg': previous_record.estimated_biomass_kg,
            'notes': previous_record.notes or '',
            'supply_rows': [
                {
                    'product_id': usage.supply_product_id,
                    'quantity': usage.quantity,
                    'notes': usage.notes or '',
                }
                for usage in ManagementSupplyUsage.query.filter_by(management_id=previous_record.id).order_by(ManagementSupplyUsage.id.asc()).all()
            ],
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
    rec.monitor_date = parse_date(request.form['monitor_date'], rec.monitor_date)
    lot = active_lot_for_unit(unit_id, on_date=rec.monitor_date)
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
    rec.nitrate = parse_float(request.form.get('nitrate'))
    rec.alkalinity = parse_float(request.form.get('alkalinity'))
    rec.hardness = parse_float(request.form.get('hardness'))
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
    new_manage_date = parse_date(request.form['manage_date'], rec.manage_date)
    lot = active_lot_for_unit(unit_id, on_date=new_manage_date)
    feed_product = selected_feed_product_from_form()
    feed_offered_kg = parse_float(request.form.get('feed_offered_kg'), 0) or 0
    tray_score = parse_float(request.form.get('tray_score'))
    existing_movement = get_management_feed_movement(record_id)

    if tray_score is not None and not 0 <= tray_score <= 4:
        flash('O score de bandeja deve ficar entre 0 e 4.', 'danger')
        return redirect(request.referrer or url_for('management_page'))

    validation_error = validate_feed_usage(feed_product, feed_offered_kg, existing_movement=existing_movement)
    if validation_error:
        flash(validation_error, 'danger')
        return redirect(request.referrer or url_for('management_page'))
    supply_entries = management_supply_entries_from_form()
    supply_validation_error = validate_supply_usage(supply_entries, management_record=rec)
    if supply_validation_error:
        flash(supply_validation_error, 'danger')
        return redirect(request.referrer or url_for('management_page'))

    rec.manage_date = new_manage_date
    rec.unit_id = unit_id
    rec.lot_id = lot.id if lot else None
    rec.feed_product_id = feed_product.id if feed_product else None
    rec.feed_offered_kg = feed_offered_kg
    rec.tray_score = tray_score
    rec.mortality_qty = parse_int(request.form.get('mortality_qty'), 0) or 0
    rec.average_weight_g = parse_float(request.form.get('average_weight_g'))
    rec.estimated_biomass_kg = parse_float(request.form.get('estimated_biomass_kg'))
    rec.notes = request.form.get('notes')
    rec.updated_at = datetime.utcnow()
    sync_management_feed_movement(rec, feed_product, feed_offered_kg, existing_movement=existing_movement)
    sync_management_supply_usages(rec, supply_entries)
    db.session.commit()
    flash('Registro de manejo atualizado com estoque recalculado.', 'success')
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
    linked_movement = get_management_feed_movement(record_id)
    if linked_movement:
        db.session.delete(linked_movement)
    usage_ids = [usage.id for usage in ManagementSupplyUsage.query.filter_by(management_id=record_id).all()]
    if usage_ids:
        SupplyInventory.query.filter(SupplyInventory.source_type == 'manejo_insumo', SupplyInventory.source_ref_id.in_(usage_ids)).delete(synchronize_session=False)
        ManagementSupplyUsage.query.filter(ManagementSupplyUsage.id.in_(usage_ids)).delete(synchronize_session=False)
    db.session.delete(rec)
    db.session.commit()
    flash('Registro de manejo excluído e estoque recalculado.', 'success')
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
        'tray_score': {'group': 'management', 'field': 'tray_score', 'label': 'Score de bandeja', 'unit': '0–4', 'title': 'Score de bandeja x tempo', 'threshold_key': None},
        'mortality': {'group': 'management', 'field': 'mortality_qty', 'label': 'Mortalidade', 'unit': 'un', 'title': 'Mortalidade x tempo', 'threshold_key': None},
        'average_weight': {'group': 'management', 'field': 'average_weight_g', 'label': 'Peso médio', 'unit': 'g', 'title': 'Peso médio x tempo', 'threshold_key': None},
    }


def build_chart_thresholds():
    config = get_water_reference_config()
    return {
        'dissolved_oxygen': {'label': 'Faixa ideal de OD', 'min': config.od_min, 'max': config.od_max},
        'ph': {'label': 'Faixa ideal de pH', 'min': config.ph_min, 'max': config.ph_max},
        'temperature_c': {'label': 'Faixa ideal de temperatura', 'min': config.temperature_min, 'max': config.temperature_max},
        'salinity': {'label': 'Faixa de salinidade alvo', 'min': config.salinity_min, 'max': config.salinity_max},
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
            'tray_score': {'label': 'Score de bandeja', 'unit': '0–4'},
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
    chart_style = request.args.get('chart_style', 'bar')
    if selected_parameter_key not in parameter_options:
        selected_parameter_key = 'od'
    if chart_style not in {'bar', 'line', 'pie', 'doughnut', 'radar'}:
        chart_style = 'bar'
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
        'chart_style': chart_style,
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
        chart_style=chart_style,
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
        protocols_query = protocols_query.filter(or_(
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




@app.route('/farm-documents', methods=['GET', 'POST'])
@login_required
@requires_permission('farm_documents_manage')
def farm_documents_page():
    if request.method == 'POST':
        uploaded_file: FileStorage | None = request.files.get('document_file')
        title = (request.form.get('title') or '').strip()
        category = (request.form.get('category') or 'Geral').strip() or 'Geral'
        notes = (request.form.get('notes') or '').strip()

        if not uploaded_file or not uploaded_file.filename:
            flash('Selecione um arquivo para enviar.', 'warning')
            return redirect(url_for('farm_documents_page'))

        safe_name = secure_filename(uploaded_file.filename)
        if not allowed_protocol_file(safe_name):
            flash('Formato não permitido. Use PDF, Office, imagem ou TXT.', 'danger')
            return redirect(url_for('farm_documents_page'))

        file_bytes = uploaded_file.read()
        if not file_bytes:
            flash('O arquivo enviado está vazio.', 'warning')
            return redirect(url_for('farm_documents_page'))
        if len(file_bytes) > 15 * 1024 * 1024:
            flash('O arquivo excede 15 MB. Envie uma versão menor.', 'danger')
            return redirect(url_for('farm_documents_page'))

        title = title or os.path.splitext(safe_name)[0]
        now = datetime.utcnow()
        document = FarmDocument(
            title=title,
            category=category,
            notes=notes or None,
            original_filename=safe_name,
            mime_type=uploaded_file.mimetype or 'application/octet-stream',
            file_size=len(file_bytes),
            file_data=file_bytes,
            created_at=now,
            uploaded_at=now,
            uploaded_by_id=getattr(current_user, 'id', None),
        )
        db.session.add(document)
        db.session.commit()
        flash('Documento da fazenda salvo com sucesso.', 'success')
        return redirect(url_for('farm_documents_page'))

    search = (request.args.get('q') or '').strip()
    category_filter = (request.args.get('category') or '').strip()
    documents_query = FarmDocument.query
    if search:
        documents_query = documents_query.filter(or_(
            FarmDocument.title.ilike(f'%{search}%'),
            FarmDocument.category.ilike(f'%{search}%'),
            FarmDocument.notes.ilike(f'%{search}%'),
            FarmDocument.original_filename.ilike(f'%{search}%'),
        ))
    if category_filter:
        documents_query = documents_query.filter(FarmDocument.category == category_filter)
    documents = documents_query.order_by(FarmDocument.uploaded_at.desc(), FarmDocument.id.desc()).all()
    categories = [row[0] for row in db.session.query(FarmDocument.category).distinct().order_by(FarmDocument.category.asc()).all() if row[0]]
    return render_template('farm_documents.html', documents=documents, categories=categories, search=search, category_filter=category_filter)


@app.get('/farm-documents/<int:document_id>/download')
@login_required
@requires_permission('farm_documents_manage')
def download_farm_document(document_id):
    document = db.session.get(FarmDocument, document_id)
    if not document:
        flash('Documento não encontrado.', 'warning')
        return redirect(url_for('farm_documents_page'))

    return send_file(
        io.BytesIO(document.file_data),
        mimetype=document.mime_type or 'application/octet-stream',
        as_attachment=True,
        download_name=document.original_filename,
    )


@app.get('/farm-documents/<int:document_id>/view')
@login_required
@requires_permission('farm_documents_manage')
def view_farm_document(document_id):
    document = db.session.get(FarmDocument, document_id)
    if not document:
        flash('Documento não encontrado.', 'warning')
        return redirect(url_for('farm_documents_page'))

    return send_file(
        io.BytesIO(document.file_data),
        mimetype=document.mime_type or 'application/octet-stream',
        as_attachment=False,
        download_name=document.original_filename,
    )


@app.post('/farm-documents/<int:document_id>/delete')
@login_required
@requires_permission('farm_documents_manage')
def delete_farm_document(document_id):
    document = db.session.get(FarmDocument, document_id)
    if not document:
        flash('Documento não encontrado.', 'warning')
        return redirect(url_for('farm_documents_page'))
    db.session.delete(document)
    db.session.commit()
    flash('Documento removido.', 'success')
    return redirect(url_for('farm_documents_page'))


@app.route('/transfers', methods=['GET', 'POST'])
@login_required
@requires_permission('transfers_manage')
def transfers_page():
    edit_id = parse_int(request.args.get('edit_id'))
    edit_transfer = db.session.get(Transfer, edit_id) if edit_id else None

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        transfer_date = parse_date(request.form.get('transfer_date'), date.today())
        destination_unit_id = parse_int(request.form.get('destination_unit_id'))
        destination_unit = db.session.get(Unit, destination_unit_id) if destination_unit_id else None
        transferred_qty = parse_int(request.form.get('transferred_qty')) or 0
        avg_weight_g = parse_float(request.form.get('avg_weight_g'))
        requested_source_phase = (request.form.get('source_phase') or '').strip()
        requested_destination_phase = (request.form.get('destination_phase') or '').strip()

        source_allocation_id = parse_int(request.form.get('source_allocation_id'))
        source_allocation = db.session.get(LotUnitAllocation, source_allocation_id) if source_allocation_id else None

        if source_allocation:
            src_id = source_allocation.unit_id
            src_lot = source_allocation.lot
        else:
            src_id = parse_int(request.form.get('source_unit_id'))
            source_lot_id = parse_int(request.form.get('source_lot_id'))
            src_lot = db.session.get(Lot, source_lot_id) if source_lot_id else active_lot_for_unit(src_id, on_date=transfer_date)
            source_allocation = find_active_allocation(src_lot.id, src_id, transfer_date) if src_lot and src_id else None

        if not src_lot or not src_id or (form_mode != 'edit' and not source_allocation):
            flash('Selecione uma origem ativa com lote e saldo disponível.', 'danger')
            return redirect(url_for('transfers_page'))
        if not destination_unit:
            flash('Selecione uma unidade de destino válida.', 'danger')
            return redirect(url_for('transfers_page'))
        if src_id == destination_unit_id:
            flash('A origem e o destino precisam ser unidades diferentes.', 'danger')
            return redirect(url_for('transfers_page'))
        if transferred_qty <= 0:
            flash('Informe uma quantidade transferida maior que zero.', 'danger')
            return redirect(url_for('transfers_page'))

        valid_phases = {'bercario', 'juvenil', 'engorda'}
        source_phase = requested_source_phase or (source_allocation.unit.phase if source_allocation and source_allocation.unit else None) or (src_lot.phase if src_lot else None)
        destination_phase = requested_destination_phase or (destination_unit.phase if destination_unit else None)
        if source_phase not in valid_phases or destination_phase not in valid_phases:
            flash('Informe corretamente a fase de origem e a fase de destino.', 'danger')
            return redirect(url_for('transfers_page'))

        available_qty = source_allocation.quantity_allocated if source_allocation else None
        if available_qty is not None and transferred_qty > available_qty and form_mode != 'edit':
            flash(f'Quantidade maior que o saldo estimado da origem ({available_qty:,} unidades).'.replace(',', '.'), 'danger')
            return redirect(url_for('transfers_page'))

        if form_mode == 'edit':
            tr = db.session.get(Transfer, parse_int(request.form.get('transfer_id')))
            if not tr:
                flash('Transferência não encontrada.', 'warning')
                return redirect(url_for('transfers_page'))
            tr.transfer_date = transfer_date
            tr.source_unit_id = src_id
            tr.destination_unit_id = destination_unit_id
            tr.source_lot_id = src_lot.id
            tr.destination_lot_code = src_lot.lot_code
            tr.source_phase = source_phase
            tr.destination_phase = destination_phase
            tr.transferred_qty = transferred_qty
            tr.avg_weight_g = avg_weight_g
            tr.notes = request.form.get('notes')
            sync_lot_phase_from_allocations(src_lot, transfer_date)
            db.session.commit()
            flash('Transferência atualizada. Observação: a edição altera o histórico; para corrigir saldo, ajuste a movimentação correspondente.', 'success')
            return redirect(url_for('transfers_page'))

        existing_allocation = find_active_allocation(src_lot.id, destination_unit_id, transfer_date)
        if not existing_allocation:
            db.session.add(LotUnitAllocation(
                lot_id=src_lot.id,
                unit_id=destination_unit_id,
                start_date=transfer_date,
                quantity_allocated=transferred_qty,
                notes='Transferência trifásica entre fases.'
            ))
        else:
            existing_allocation.quantity_allocated = (existing_allocation.quantity_allocated or 0) + transferred_qty
            if existing_allocation.end_date and existing_allocation.end_date <= transfer_date:
                existing_allocation.end_date = None

        tr = Transfer(
            transfer_date=transfer_date,
            source_unit_id=src_id,
            destination_unit_id=destination_unit_id,
            source_lot_id=src_lot.id,
            destination_lot_code=src_lot.lot_code,
            source_phase=source_phase,
            destination_phase=destination_phase,
            transferred_qty=transferred_qty,
            avg_weight_g=avg_weight_g,
            notes=request.form.get('notes')
        )
        db.session.add(tr)

        remaining_qty = None
        if source_allocation.quantity_allocated is not None:
            remaining_qty = max((source_allocation.quantity_allocated or 0) - transferred_qty, 0)
            source_allocation.quantity_allocated = remaining_qty
        should_close_source = request.form.get('close_source_allocation') == '1' or remaining_qty == 0
        if should_close_source:
            source_allocation.end_date = transfer_date

        sync_lot_phase_from_allocations(src_lot, transfer_date)
        db.session.commit()
        flash('Transferência registrada. O lote agora pode seguir no fluxo Berçário → Juvenil → Engorda, inclusive com divisões parciais.', 'success')
        return redirect(url_for('transfers_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    rows = Transfer.query.options(joinedload(Transfer.source_unit), joinedload(Transfer.destination_unit), joinedload(Transfer.source_lot)).order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).limit(80).all()
    allocations = active_allocation_rows(date.today())
    return render_template(
        'transfers.html',
        units=units,
        lots=lots,
        rows=rows,
        allocations=allocations,
        today=date.today(),
        edit_transfer=edit_transfer,
        phase_choices=[('bercario', 'Berçário'), ('juvenil', 'Juvenil'), ('engorda', 'Engorda')],
    )


@app.route('/feed', methods=['GET', 'POST'])
@login_required
@requires_permission('feed_manage')
def feed_page():
    edit_product_id = parse_int(request.args.get('edit_product_id'))
    edit_movement_id = parse_int(request.args.get('edit_movement_id'))
    edit_product = db.session.get(FeedProduct, edit_product_id) if edit_product_id else None
    edit_movement = db.session.get(FeedInventory, edit_movement_id) if edit_movement_id else None
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'movement')
        if form_mode in {'product', 'edit_product'}:
            product = db.session.get(FeedProduct, parse_int(request.form.get('product_id'))) if form_mode == 'edit_product' else FeedProduct(active=True)
            if form_mode == 'edit_product' and not product:
                flash('Produto de ração não encontrado.', 'warning')
                return redirect(url_for('feed_page'))
            brand = (request.form.get('brand') or '').strip()
            feed_type = (request.form.get('feed_type') or '').strip()
            if not brand or not feed_type:
                flash('Informe marca e tipo da ração para cadastrar o produto.', 'danger')
                return redirect(url_for('feed_page'))
            product.brand = brand
            product.feed_type = feed_type
            product.protein_pct = parse_float(request.form.get('protein_pct'))
            product.pellet_size_mm = parse_float(request.form.get('pellet_size_mm'))
            product.minimum_stock_kg = parse_float(request.form.get('minimum_stock_kg'), 0) or 0
            product.notes = request.form.get('product_notes')
            if form_mode != 'edit_product':
                db.session.add(product)
            db.session.commit()
            flash('Produto de ração salvo com sucesso.', 'success')
            return redirect(url_for('feed_page'))
        feed_product_id = parse_int(request.form.get('feed_product_id'))
        feed_product = db.session.get(FeedProduct, feed_product_id) if feed_product_id else None
        if not feed_product:
            flash('Selecione a ração que será movimentada.', 'danger')
            return redirect(url_for('feed_page'))
        movement_type = request.form['movement_type']
        quantity_kg = parse_float(request.form.get('quantity_kg'))
        if quantity_kg is None or quantity_kg <= 0:
            flash('Informe uma quantidade válida em kg.', 'danger')
            return redirect(url_for('feed_page'))
        row = db.session.get(FeedInventory, parse_int(request.form.get('movement_id'))) if form_mode == 'edit_movement' else FeedInventory(source_type='manual', created_by_id=getattr(current_user, 'id', None))
        if form_mode == 'edit_movement' and not row:
            flash('Movimentação não encontrada.', 'warning')
            return redirect(url_for('feed_page'))
        row.movement_date = parse_date(request.form['movement_date'], date.today())
        row.feed_name = feed_inventory_name(feed_product)
        row.feed_product_id = feed_product.id
        row.movement_type = movement_type
        row.quantity_kg = quantity_kg
        row.unit_cost = parse_float(request.form.get('unit_cost'))
        row.notes = request.form.get('notes')
        if form_mode != 'edit_movement':
            db.session.add(row)
        db.session.commit()
        flash('Movimentação de ração salva.', 'success')
        return redirect(url_for('feed_page'))
    snapshot = build_feed_stock_snapshot()
    rows = FeedInventory.query.options(joinedload(FeedInventory.feed_product), joinedload(FeedInventory.unit), joinedload(FeedInventory.lot)).order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).limit(80).all()
    feed_products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()
    stock_by_product = {row['product'].id: row['stock_kg'] for row in snapshot['rows']}
    return render_template('feed.html', rows=rows, today=date.today(), total_stock=snapshot['total_stock_kg'], snapshot_rows=snapshot['rows'], low_stock_count=snapshot['low_stock_count'], active_product_count=snapshot['active_product_count'], feed_products=feed_products, stock_by_product=stock_by_product, movement_origin_label=movement_origin_label, edit_product=edit_product, edit_movement=edit_movement)


@app.post('/feed/products/<int:product_id>/toggle')
@login_required
@requires_permission('feed_manage')
def toggle_feed_product(product_id):
    product = db.session.get(FeedProduct, product_id)
    if not product:
        flash('Produto de ração não encontrado.', 'warning')
        return redirect(url_for('feed_page'))
    product.active = not product.active
    db.session.commit()
    flash('Status do produto atualizado.', 'success')
    return redirect(url_for('feed_page'))


@app.post('/feed/products/<int:product_id>/delete')
@login_required
@requires_permission('feed_manage')
def delete_feed_product(product_id):
    product = db.session.get(FeedProduct, product_id)
    if not product:
        flash('Produto de ração não encontrado.', 'warning')
        return redirect(url_for('feed_page'))

    has_movements = FeedInventory.query.filter_by(feed_product_id=product.id).count() > 0
    has_management = DailyManagement.query.filter_by(feed_product_id=product.id).count() > 0

    if has_movements or has_management:
        canonical = find_or_create_nursery_feed_product(product.full_name, exclude_product_id=product.id, create_missing=False)
        if canonical:
            for movement in FeedInventory.query.filter_by(feed_product_id=product.id).all():
                movement.feed_product_id = canonical.id
                movement.feed_name = feed_inventory_name(canonical)
            for record in DailyManagement.query.filter_by(feed_product_id=product.id).all():
                record.feed_product_id = canonical.id
            db.session.delete(product)
            flash(f'Produto duplicado removido. O histórico foi transferido para {canonical.full_name}.', 'success')
        else:
            product.active = False
            flash('Este produto tem histórico vinculado e não encontrei outro produto compatível para unir. Ele foi inativado para preservar os relatórios.', 'warning')
    else:
        db.session.delete(product)
        flash('Produto de ração excluído.', 'success')

    db.session.commit()
    return redirect(url_for('feed_page'))



@app.route('/supplies', methods=['GET', 'POST'])
@login_required
@requires_permission('feed_manage')
def supplies_page():
    edit_product_id = parse_int(request.args.get('edit_product_id'))
    edit_movement_id = parse_int(request.args.get('edit_movement_id'))
    edit_product = db.session.get(SupplyProduct, edit_product_id) if edit_product_id else None
    edit_movement = db.session.get(SupplyInventory, edit_movement_id) if edit_movement_id else None
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'movement')
        if form_mode in {'product', 'edit_product'}:
            product = db.session.get(SupplyProduct, parse_int(request.form.get('product_id'))) if form_mode == 'edit_product' else SupplyProduct(active=True)
            if form_mode == 'edit_product' and not product:
                flash('Produto/insumo não encontrado.', 'warning')
                return redirect(url_for('supplies_page'))
            name = (request.form.get('name') or '').strip()
            if not name:
                flash('Informe o nome do insumo ou material.', 'danger')
                return redirect(url_for('supplies_page'))
            product.name = name
            product.category = (request.form.get('category') or 'Insumo geral').strip() or 'Insumo geral'
            product.measure_unit = (request.form.get('measure_unit') or 'kg').strip() or 'kg'
            product.minimum_stock_qty = parse_float(request.form.get('minimum_stock_qty'), 0) or 0
            product.notes = request.form.get('product_notes')
            if form_mode != 'edit_product':
                db.session.add(product)
            db.session.commit()
            flash('Insumo/material salvo com sucesso.', 'success')
            return redirect(url_for('supplies_page'))

        product_id = parse_int(request.form.get('supply_product_id'))
        product = db.session.get(SupplyProduct, product_id) if product_id else None
        if not product:
            flash('Selecione o insumo que será movimentado.', 'danger')
            return redirect(url_for('supplies_page'))
        quantity = parse_float(request.form.get('quantity'))
        if quantity is None or quantity <= 0:
            flash(f'Informe uma quantidade válida em {product.measure_unit}.', 'danger')
            return redirect(url_for('supplies_page'))
        movement_type = request.form.get('movement_type') or 'entrada'
        row = db.session.get(SupplyInventory, parse_int(request.form.get('movement_id'))) if form_mode == 'edit_movement' else SupplyInventory(source_type='manual', created_by_id=getattr(current_user, 'id', None))
        if form_mode == 'edit_movement' and not row:
            flash('Movimentação não encontrada.', 'warning')
            return redirect(url_for('supplies_page'))
        if movement_type == 'saida' and quantity > available_stock_for_supply(product.id) + ((row.quantity or 0) if row and row.id and row.supply_product_id == product.id and row.movement_type == 'saida' else 0):
            flash(f'Estoque insuficiente para {product.full_name}.', 'danger')
            return redirect(url_for('supplies_page'))
        row.movement_date = parse_date(request.form.get('movement_date'), date.today())
        row.supply_product_id = product.id
        row.movement_type = movement_type
        row.quantity = quantity
        row.unit_cost = parse_float(request.form.get('unit_cost'))
        row.notes = request.form.get('notes')
        if form_mode != 'edit_movement':
            db.session.add(row)
        db.session.commit()
        flash('Movimentação de insumo/material salva.', 'success')
        return redirect(url_for('supplies_page'))

    snapshot = build_supply_stock_snapshot()
    rows = SupplyInventory.query.options(joinedload(SupplyInventory.supply_product), joinedload(SupplyInventory.unit), joinedload(SupplyInventory.lot)).order_by(SupplyInventory.movement_date.desc(), SupplyInventory.id.desc()).limit(80).all()
    supply_products = SupplyProduct.query.order_by(SupplyProduct.active.desc(), SupplyProduct.name.asc()).all()
    return render_template(
        'supplies.html',
        rows=rows,
        today=date.today(),
        total_stock=snapshot['total_stock_qty'],
        snapshot_rows=snapshot['rows'],
        low_stock_count=snapshot['low_stock_count'],
        active_product_count=snapshot['active_product_count'],
        supply_products=supply_products,
        movement_supply_origin_label=movement_supply_origin_label,
        edit_product=edit_product,
        edit_movement=edit_movement,
    )


@app.post('/supplies/products/<int:product_id>/toggle')
@login_required
@requires_permission('feed_manage')
def toggle_supply_product(product_id):
    product = db.session.get(SupplyProduct, product_id)
    if not product:
        flash('Insumo/material não encontrado.', 'warning')
        return redirect(url_for('supplies_page'))
    product.active = not product.active
    db.session.commit()
    flash('Status do insumo/material atualizado.', 'success')
    return redirect(url_for('supplies_page'))


@app.post('/supplies/products/<int:product_id>/delete')
@login_required
@requires_permission('feed_manage')
def delete_supply_product(product_id):
    product = db.session.get(SupplyProduct, product_id)
    if not product:
        flash('Insumo/material não encontrado.', 'warning')
        return redirect(url_for('supplies_page'))
    has_movements = SupplyInventory.query.filter_by(supply_product_id=product.id).count() > 0
    has_management = ManagementSupplyUsage.query.filter_by(supply_product_id=product.id).count() > 0
    if has_movements or has_management:
        product.active = False
        db.session.commit()
        flash('Esse item possui histórico. Ele foi inativado para preservar relatórios e custo dos lotes.', 'warning')
        return redirect(url_for('supplies_page'))
    db.session.delete(product)
    db.session.commit()
    flash('Insumo/material excluído.', 'success')
    return redirect(url_for('supplies_page'))


@app.get('/managerial-reports')
@login_required
@requires_permission('dashboard')
def managerial_reports_page():
    period_days = request.args.get('days', default=30, type=int)
    if period_days not in {7, 30, 90, 180, 365}:
        period_days = 30
    start_date = date.today() - timedelta(days=period_days - 1)

    feed_snapshot = build_feed_stock_snapshot()
    supply_snapshot = build_supply_stock_snapshot()
    active_lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    lot_summaries = [lot_financial_summary(lot) for lot in active_lots]
    sales_rows = Sale.query.options(joinedload(Sale.lot), joinedload(Sale.unit)).filter(Sale.sale_date >= start_date).order_by(Sale.sale_date.desc()).all()
    sales_summaries = [summary for sale in sales_rows if (summary := sale_financial_summary(sale))]
    water_rows = WaterMonitoring.query.options(joinedload(WaterMonitoring.unit)).filter(WaterMonitoring.monitor_date >= start_date).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.id.desc()).all()
    config = get_water_reference_config()
    water_alert_rows = []
    for record in water_rows:
        alerts = water_alerts_for_record(record, config)
        if alerts:
            water_alert_rows.append({'record': record, 'alerts': alerts})
    management_rows = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot)).filter(DailyManagement.manage_date >= start_date).order_by(DailyManagement.manage_date.desc()).all()
    financial_totals = {
        'feed_cost': round(sum(summary['feed_cost'] for summary in lot_summaries), 2),
        'supply_cost': round(sum(summary.get('supply_cost', 0) for summary in lot_summaries), 2),
        'fixed_cost': round(sum(summary['fixed_cost'] for summary in lot_summaries), 2),
        'total_cost': round(sum(summary['total_cost'] for summary in lot_summaries), 2),
        'revenue_period': round(sum(summary['revenue'] for summary in sales_summaries), 2),
        'profit_period': round(sum(summary['profit'] for summary in sales_summaries), 2),
    }
    return render_template(
        'managerial_reports.html',
        period_days=period_days,
        start_date=start_date,
        feed_snapshot=feed_snapshot,
        supply_snapshot=supply_snapshot,
        lot_summaries=lot_summaries[:12],
        sales_summaries=sales_summaries[:12],
        water_alert_rows=water_alert_rows[:20],
        management_rows=management_rows[:20],
        financial_totals=financial_totals,
    )


@app.get('/managerial-reports/export/<report_key>.xlsx')
@login_required
@requires_permission('dashboard')
def export_managerial_report(report_key):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Relatorio'
    today = date.today()
    if report_key == 'stock':
        ws.title = 'Estoque'
        ws.append(['Categoria', 'Item', 'Grupo', 'Saldo', 'Unidade', 'Estoque mínimo', 'Custo médio'])
        for row in build_feed_stock_snapshot()['rows']:
            ws.append(['Ração', row['name'] if 'name' in row else row['feed_name'], row.get('feed_type') or row.get('category'), row.get('stock_kg', row.get('stock_qty')), 'kg', row.get('minimum_stock_kg', row.get('minimum_stock_qty')), row.get('avg_unit_cost')])
        for row in build_supply_stock_snapshot()['rows']:
            ws.append(['Insumo/material', row['name'], row['category'], row['stock_qty'], row['measure_unit'], row['minimum_stock_qty'], row.get('avg_unit_cost')])
    elif report_key == 'production':
        ws.title = 'Producao'
        ws.append(['Lote', 'Status', 'Fornecedora', 'Unidades atuais', 'Custo ração', 'Custo insumos', 'Custo fixo', 'Custo total', 'FCR real', 'Sobrevivência %'])
        for summary in [lot_financial_summary(lot) for lot in Lot.query.order_by(Lot.start_date.desc()).all()]:
            ws.append([summary['lot'].lot_code, summary['lot'].status, summary['lot'].larva_supplier, ', '.join(item['unit_name'] for item in summary['allocations']), summary['feed_cost'], summary.get('supply_cost', 0), summary['fixed_cost'], summary['total_cost'], summary['fcr_real'], summary['survival_pct']])
    elif report_key == 'financial':
        ws.title = 'Financeiro'
        ws.append(['Data', 'Lote', 'Viveiro', 'Receita', 'Custo ração', 'Custo insumos', 'Custo fixo', 'Custo total', 'Resultado'])
        for sale in Sale.query.options(joinedload(Sale.lot), joinedload(Sale.unit)).order_by(Sale.sale_date.desc()).all():
            summary = sale_financial_summary(sale)
            if not summary:
                continue
            ws.append([sale.sale_date.strftime('%d/%m/%Y'), sale.lot.lot_code if sale.lot else '', sale.unit.name if sale.unit else '', summary['revenue'], summary['feed_cost'], summary.get('supply_cost', 0), summary['fixed_cost'], summary['total_cost'], summary['profit']])
    elif report_key == 'water_quality':
        ws.title = 'Qualidade agua'
        ws.append(['Data', 'Hora', 'Unidade', 'OD', 'Temperatura', 'pH', 'Salinidade', 'Alertas'])
        config = get_water_reference_config()
        rows = WaterMonitoring.query.options(joinedload(WaterMonitoring.unit)).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.id.desc()).all()
        for record in rows:
            alerts = '; '.join(alert['message'] for alert in water_alerts_for_record(record, config))
            ws.append([record.monitor_date.strftime('%d/%m/%Y') if record.monitor_date else '', record.monitor_time.strftime('%H:%M') if record.monitor_time else '', record.unit.name if record.unit else '', record.dissolved_oxygen, record.temperature_c, record.ph, record.salinity, alerts])
    else:
        abort(404)
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f'relatorio_{report_key}_{today.strftime("%Y%m%d")}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/nursery-feed', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def nursery_feed_page():
    selected_date = parse_date(request.args.get('feed_date'), date.today())
    edit_id = parse_int(request.args.get('edit_id'))
    edit_entry = db.session.get(NurseryFeeding, edit_id) if edit_id else None
    nursery_units = Unit.query.filter_by(active=True, phase='bercario').order_by(Unit.name).all()

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        entry = db.session.get(NurseryFeeding, parse_int(request.form.get('entry_id'))) if form_mode == 'edit' else NurseryFeeding()
        if form_mode == 'edit' and not entry:
            flash('Registro de alimentação de berçário não encontrado.', 'warning')
            return redirect(url_for('nursery_feed_page'))

        entry.feed_date = parse_date(request.form['feed_date'])
        entry.unit_id = int(request.form['unit_id'])
        active_lot = active_lot_for_unit(entry.unit_id, on_date=entry.feed_date)
        entry.lot_id = parse_int(request.form.get('lot_id')) or (active_lot.id if active_lot else None)
        submitted_quantity_kg = parse_float(request.form.get('quantity_kg'), 0) or 0
        entry.intestinal_score = parse_float(request.form.get('intestinal_score'))
        entry.score_adjustment_pct = parse_float(request.form.get('score_adjustment_pct'))
        if entry.score_adjustment_pct is None and entry.intestinal_score is not None:
            entry.score_adjustment_pct = nursery_score_adjustment_pct(entry.intestinal_score)
        entry.quantity_kg = submitted_quantity_kg
        entry.notes = request.form.get('notes')
        entry.updated_at = datetime.utcnow()
        if form_mode != 'edit':
            db.session.add(entry)
        db.session.flush()
        sync_nursery_feed_to_management(entry)
        db.session.commit()
        flash('Alimentação de berçário salva e integrada ao manejo diário.', 'success')
        return redirect(url_for('nursery_feed_page', feed_date=entry.feed_date.isoformat()))

    entries = NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot)).order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc()).limit(60).all()
    plans = build_nursery_digest_for_date(selected_date)
    entry_by_unit_id = {entry.unit_id: entry for entry in NurseryFeeding.query.filter_by(feed_date=selected_date).all()}
    for plan in plans:
        plan['existing_entry'] = entry_by_unit_id.get(plan['unit'].id)
    combined_message = '\n\n'.join(plan['message_text'] for plan in plans)
    return render_template('nursery_feed.html', today=date.today(), selected_date=selected_date, nursery_units=nursery_units, entries=entries, edit_entry=edit_entry, plans=plans, combined_message=combined_message)



@app.post('/nursery-feed/<int:entry_id>/delete')
@login_required
@requires_permission('management_manage')
def delete_nursery_feed_entry(entry_id):
    entry = db.session.get(NurseryFeeding, entry_id)
    if not entry:
        flash('Registro de alimentação de berçário não encontrado.', 'warning')
        return redirect(request.referrer or url_for('nursery_feed_page'))
    feed_date = entry.feed_date
    delete_nursery_management_records(entry)
    db.session.delete(entry)
    db.session.commit()
    flash('Lançamento do berçário excluído e removido do manejo diário.', 'success')
    return redirect(url_for('nursery_feed_page', feed_date=feed_date.isoformat()))


@app.get('/api/nursery-feed-digest')
def nursery_feed_digest_api():
    token = os.getenv('NURSERY_DIGEST_TOKEN', '').strip()
    provided = (request.headers.get('X-Nursery-Token') or request.args.get('token') or '').strip()
    if token and provided != token:
        return jsonify({'ok': False, 'message': 'Token inválido.'}), 403

    target_date = parse_date(request.args.get('feed_date'), date.today())
    plans = build_nursery_digest_for_date(target_date)
    return jsonify({
        'ok': True,
        'feed_date': target_date.isoformat(),
        'count': len(plans),
        'messages': [
            {
                'unit': plan['unit'].name,
                'lot': plan['lot'].lot_code,
                'text': plan['message_text'],
            }
            for plan in plans
        ],
        'combined_message': '\n\n'.join(plan['message_text'] for plan in plans),
    })

@app.route('/sales', methods=['GET', 'POST'])
@login_required
@requires_permission('sales_manage')
def sales_page():
    edit_id = parse_int(request.args.get('edit_id'))
    edit_sale = db.session.get(Sale, edit_id) if edit_id else None
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        sale_date = parse_date(request.form['sale_date'], date.today())
        unit_id = int(request.form['unit_id']) if request.form.get('unit_id') else None
        lot_id = int(request.form['lot_id']) if request.form.get('lot_id') else None
        if unit_id and not lot_id:
            allocation = active_lot_allocation_for_unit(unit_id, on_date=sale_date)
            lot_id = allocation.lot_id if allocation else None
        if lot_id and unit_id is None:
            current_units = lot_current_units(db.session.get(Lot, lot_id), on_date=sale_date)
            if len(current_units) == 1:
                unit_id = current_units[0].id
        average_weight_g = parse_float(request.form.get('average_weight_g'))
        harvested_units = parse_int(request.form.get('harvested_units'))
        quantity_kg = parse_float(request.form['quantity_kg'], 0) or 0
        if harvested_units is None and average_weight_g:
            harvested_units = int(round((quantity_kg * 1000) / average_weight_g)) if average_weight_g else None
        sale = db.session.get(Sale, parse_int(request.form.get('sale_id'))) if form_mode == 'edit' else Sale()
        if form_mode == 'edit' and not sale:
            flash('Registro de despesca não encontrado.', 'warning')
            return redirect(url_for('sales_page'))
        sale.sale_date = sale_date
        sale.unit_id = unit_id
        sale.lot_id = lot_id
        sale.client_name = request.form['client_name']
        sale.channel = request.form['channel']
        sale.quantity_kg = quantity_kg
        sale.unit_price = parse_float(request.form['unit_price'], 0) or 0
        sale.average_weight_g = average_weight_g
        sale.harvested_units = harvested_units
        sale.notes = request.form.get('notes')
        if form_mode != 'edit':
            db.session.add(sale)
            if lot_id and unit_id and request.form.get('close_unit_after_sale', '1') == '1':
                allocation = find_active_allocation(lot_id, unit_id, sale_date)
                if allocation:
                    allocation.end_date = sale_date
                    allocation.quantity_allocated = 0
                remaining = LotUnitAllocation.query.filter(LotUnitAllocation.lot_id == lot_id, LotUnitAllocation.start_date <= sale_date, or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date > sale_date)).count()
                if remaining == 0:
                    lot = db.session.get(Lot, lot_id)
                    if lot:
                        close_lot(lot, sale_date, reason='despesca_venda')
            elif lot_id and request.form.get('close_lot_after_sale', '0') == '1':
                lot = db.session.get(Lot, lot_id)
                if lot:
                    close_lot(lot, sale_date, reason='despesca_venda')
        db.session.commit()
        flash('Despesca/venda salva com sucesso.', 'success')
        return redirect(url_for('sales_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    lots = Lot.query.order_by(Lot.start_date.desc()).all()
    rows = Sale.query.options(joinedload(Sale.lot), joinedload(Sale.unit)).order_by(Sale.sale_date.desc(), Sale.id.desc()).limit(50).all()
    row_summaries = [summary for sale in rows if (summary := sale_financial_summary(sale))]
    total_revenue = db.session.query(func.coalesce(func.sum(Sale.quantity_kg * Sale.unit_price), 0)).scalar() or 0
    selected_summary = row_summaries[0] if row_summaries else None
    return render_template('sales.html', units=units, lots=lots, rows=rows, row_summaries=row_summaries, today=date.today(), total_revenue=total_revenue, edit_sale=edit_sale, selected_summary=selected_summary)


@app.get('/sales/export-history.xlsx')
@login_required
@requires_permission('sales_manage')
def export_sales_history():
    rows = Sale.query.options(joinedload(Sale.lot), joinedload(Sale.unit)).order_by(Sale.sale_date.asc(), Sale.id.asc()).all()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Historico despesca'
    headers = [
        'Data', 'Lote', 'Viveiro', 'Cliente', 'Canal', 'Qtd kg', 'Preco kg', 'Faturamento',
        'Peso medio g', 'Unidades despescadas', 'Custo racao viveiro', 'Custo insumos viveiro', 'Custo fixo viveiro',
        'Custo total viveiro', 'Resultado', 'Status', 'FCR real lote', 'Sobrevivencia lote %'
    ]
    ws.append(headers)
    for sale in rows:
        summary = sale_financial_summary(sale)
        if not summary:
            continue
        ws.append([
            sale.sale_date.strftime('%d/%m/%Y') if sale.sale_date else '',
            sale.lot.lot_code if sale.lot else '',
            sale.unit.name if sale.unit else '',
            sale.client_name,
            sale.channel,
            sale.quantity_kg,
            sale.unit_price,
            summary['revenue'],
            sale.average_weight_g,
            summary['harvested_units'],
            summary['feed_cost'],
            summary.get('supply_cost', 0),
            summary['fixed_cost'],
            summary['total_cost'],
            summary['profit'],
            summary['status'],
            summary['fcr_real'],
            summary['survival_pct'],
        ])
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='historico_despesca_lotes.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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



class BiometricsSample(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sample_date = db.Column(db.Date, nullable=False)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    sample_size = db.Column(db.Integer, nullable=False, default=100)
    average_weight_g = db.Column(db.Float, nullable=False)
    cv_pct = db.Column(db.Float)
    estimated_biomass_kg = db.Column(db.Float)
    weekly_gain_g = db.Column(db.Float)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    lot = db.relationship('Lot')
    unit = db.relationship('Unit')


class FinanceEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date)
    entry_type = db.Column(db.String(20), nullable=False)  # pagar/receber
    category = db.Column(db.String(80), nullable=False, default='Geral')
    description = db.Column(db.String(180), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default='aberto')
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'))
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    lot = db.relationship('Lot')
    unit = db.relationship('Unit')


class AlertRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    parameter_key = db.Column(db.String(40), nullable=False)
    min_value = db.Column(db.Float)
    max_value = db.Column(db.Float)
    channel = db.Column(db.String(30), nullable=False, default='whatsapp')
    recipient = db.Column(db.String(120))
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def ensure_alert_rules():
    defaults = [
        ('OD crítico', 'dissolved_oxygen', TARGET_OD_MIN, None),
        ('pH alto', 'ph', None, TARGET_PH_MAX),
        ('pH baixo', 'ph', TARGET_PH_MIN, None),
        ('Amônia alta', 'ammonia', None, TARGET_AMMONIA_MAX),
        ('Nitrito alto', 'nitrite', None, TARGET_NITRITE_MAX),
    ]
    existing = {(row.name, row.parameter_key) for row in AlertRule.query.all()}
    created = False
    for name, parameter_key, min_value, max_value in defaults:
        if (name, parameter_key) in existing:
            continue
        db.session.add(AlertRule(name=name, parameter_key=parameter_key, min_value=min_value, max_value=max_value, recipient='Plantão'))
        created = True
    if created:
        db.session.commit()


def latest_management_for_lot(lot_id):
    return DailyManagement.query.filter(DailyManagement.lot_id == lot_id).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).first()


def latest_biometric_for_lot(lot_id):
    return BiometricsSample.query.filter(BiometricsSample.lot_id == lot_id).order_by(BiometricsSample.sample_date.desc(), BiometricsSample.id.desc()).first()


def merged_weight_observations(lot_id):
    observations_by_date = {}
    for row in DailyManagement.query.filter(DailyManagement.lot_id == lot_id, DailyManagement.average_weight_g.isnot(None)).order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all():
        if not row.manage_date or row.average_weight_g is None:
            continue
        payload = {
            'date': row.manage_date,
            'weight_g': round(row.average_weight_g or 0, 3),
            'source': 'manejo',
            'source_label': 'Manejo diário',
            'source_priority': 1,
            'id': row.id,
        }
        existing = observations_by_date.get(row.manage_date)
        if not existing or (payload['source_priority'], payload['id']) >= (existing['source_priority'], existing['id']):
            observations_by_date[row.manage_date] = payload
    for row in BiometricsSample.query.filter(BiometricsSample.lot_id == lot_id, BiometricsSample.average_weight_g.isnot(None)).order_by(BiometricsSample.sample_date.asc(), BiometricsSample.id.asc()).all():
        if not row.sample_date or row.average_weight_g is None:
            continue
        payload = {
            'date': row.sample_date,
            'weight_g': round(row.average_weight_g or 0, 3),
            'source': 'biometria',
            'source_label': 'Biometria',
            'source_priority': 2,
            'id': row.id,
        }
        existing = observations_by_date.get(row.sample_date)
        if not existing or (payload['source_priority'], payload['id']) >= (existing['source_priority'], existing['id']):
            observations_by_date[row.sample_date] = payload
    return [item for _day, item in sorted(observations_by_date.items(), key=lambda pair: pair[0])]


def lot_density_snapshot(lot: Lot, on_date=None):
    on_date = on_date or date.today()
    allocation = LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit)).filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
    ).order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc()).first()
    qty = allocation.quantity_allocated if allocation and allocation.quantity_allocated else lot.initial_count
    unit = allocation.unit if allocation and allocation.unit else lot.unit
    if not unit or not unit.area_m2 or not qty:
        return None
    return round(qty / unit.area_m2, 2)


def lot_environment_snapshot(lot: Lot, ref_date=None, days=5):
    ref_date = ref_date or date.today()
    unit_id = lot.unit_id
    allocation = LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= ref_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= ref_date),
    ).order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc()).first()
    if allocation and allocation.unit_id:
        unit_id = allocation.unit_id
    rows = WaterMonitoring.query.filter(
        WaterMonitoring.unit_id == unit_id,
        WaterMonitoring.monitor_date >= ref_date - timedelta(days=max(days - 1, 0)),
        WaterMonitoring.monitor_date <= ref_date,
    ).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.monitor_time.desc(), WaterMonitoring.id.desc()).all()

    def avg(field):
        values = [getattr(row, field) for row in rows if getattr(row, field) is not None]
        return round(sum(values) / len(values), 3) if values else None

    return {
        'temperature_c': avg('temperature_c'),
        'dissolved_oxygen': avg('dissolved_oxygen'),
        'ph': avg('ph'),
        'salinity': avg('salinity'),
        'ammonia': avg('ammonia'),
        'nitrite': avg('nitrite'),
        'rows': len(rows),
    }




def is_nursery_lot(lot: Lot):
    phase = normalize_text(getattr(lot, 'phase', '') or '')
    unit_phase = normalize_text(getattr(getattr(lot, 'unit', None), 'phase', '') or '')
    return phase in {'bercario', 'berçario', 'nursery'} or unit_phase in {'bercario', 'berçario', 'nursery'}


def is_growout_lot(lot: Lot):
    phase = normalize_text(getattr(lot, 'phase', '') or '')
    unit_phase = normalize_text(getattr(getattr(lot, 'unit', None), 'phase', '') or '')
    return phase in {'engorda', 'grow out', 'growout', 'juvenil', 'raceway'} or unit_phase in {'engorda', 'grow out', 'growout', 'juvenil', 'raceway'}


def _interpolate_points(points, x):
    """Interpolação linear simples para curvas internas do sistema."""
    if not points:
        return None
    points = sorted(points, key=lambda item: item[0])
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for idx in range(1, len(points)):
        x0, y0 = points[idx - 1]
        x1, y1 = points[idx]
        if x <= x1:
            span = (x1 - x0) or 1
            return y0 + ((y1 - y0) * ((x - x0) / span))
    return points[-1][1]


def _protocol_age_value(row, first_row):
    if row.get('phase') == first_row.get('phase') == 'engorda':
        return max((row.get('phase_day') or 1) - (first_row.get('phase_day') or 1), 0)
    return max((row.get('cumulative_day') or 0) - (first_row.get('cumulative_day') or 0), 0)


def _production_protocol_rows_for_lot(lot=None, start_weight_g=None):
    if lot and is_nursery_lot(lot):
        return []
    if start_weight_g is None and lot is not None:
        start_weight_g = parse_float(getattr(lot, 'estimated_weight_g', None), None)
    phase_key = normalize_text(getattr(lot, 'phase', '') if lot else '')
    if phase_key in {'juvenil', 'raceway'}:
        return PRODUCTION_PROTOCOL_ROWS
    if start_weight_g is not None and start_weight_g > 0 and start_weight_g < 1.5:
        return PRODUCTION_PROTOCOL_ROWS
    return [row for row in PRODUCTION_PROTOCOL_ROWS if row.get('phase') == 'engorda']


def _nearest_protocol_row(rows, age_days=None, weight_g=None):
    if not rows:
        return None
    if weight_g is not None:
        return min(rows, key=lambda row: abs((row.get('weight_g') or 0) - weight_g))
    first = rows[0]
    return min(rows, key=lambda row: abs(_protocol_age_value(row, first) - age_days))


def _protocol_curve_point(rows, age_days: int | float):
    if not rows:
        return None
    age = max(float(age_days or 0), 0.0)
    first = rows[0]
    max_age = _protocol_age_value(rows[-1], first)
    clamped_age = min(age, max_age)

    def value(field, default=None):
        points = [(_protocol_age_value(row, first), row.get(field)) for row in rows if row.get(field) is not None]
        interpolated = _interpolate_points(points, clamped_age) if points else default
        return default if interpolated is None else interpolated

    expected_weight = value('weight_g', rows[0].get('weight_g') or 0)
    survival_pct = value('survival_pct', rows[0].get('survival_pct') or 100)
    feed_rate_pct = value('feed_rate_pct', rows[0].get('feed_rate_pct') or 3)
    estimated_fcr = value('estimated_fcr', rows[0].get('estimated_fcr') or 0)
    base_daily_feed_kg = value('daily_feed_kg', rows[0].get('daily_feed_kg') or 0)
    current_row = _nearest_protocol_row(rows, age_days=clamped_age) or rows[0]
    next_row = None
    for row in rows:
        if _protocol_age_value(row, first) > clamped_age:
            next_row = row
            break
    if next_row:
        next_age = _protocol_age_value(next_row, first)
        daily_gain = ((next_row.get('weight_g') or expected_weight) - expected_weight) / max(next_age - clamped_age, 1)
    else:
        daily_gain = current_row.get('daily_growth_g') or 0.08
    source = 'Tabela operacional 160.000 PL — engorda'
    if any(row.get('phase') == 'juvenil' for row in rows):
        source = 'Tabela operacional 160.000 PL — juvenil + engorda'
    if age > max_age:
        expected_weight += (age - max_age) * max(daily_gain or 0.08, 0.05)
    return {
        'age_days': int(round(age)),
        'expected_weight_g': round(expected_weight, 2),
        'daily_gain_g': round(max(daily_gain or 0, 0.01), 3),
        'survival_pct': round(max(min(survival_pct, 100), 0), 2),
        'feed_rate_pct': round(feed_rate_pct, 2),
        'estimated_fcr': round(estimated_fcr, 2),
        'base_daily_feed_kg_160k': round(base_daily_feed_kg, 2),
        'feedings_per_day': current_row.get('feedings_per_day') or 4,
        'protocol_phase': current_row.get('phase'),
        'mixes': current_row.get('mixes') or [],
        'source': source,
    }


def _protocol_curve_by_weight(rows, weight_g: float | int | None):
    if not rows:
        return None
    weight = max(parse_float(weight_g, 0) or 0, 0)
    points = sorted([(row.get('weight_g') or 0, row) for row in rows if row.get('weight_g') is not None], key=lambda item: item[0])
    if not points:
        return _protocol_curve_point(rows, 0)
    if weight <= points[0][0]:
        age = _protocol_age_value(points[0][1], rows[0])
    elif weight >= points[-1][0]:
        age = _protocol_age_value(points[-1][1], rows[0])
    else:
        age = 0
        for idx in range(1, len(points)):
            w0, row0 = points[idx - 1]
            w1, row1 = points[idx]
            if weight <= w1:
                a0 = _protocol_age_value(row0, rows[0])
                a1 = _protocol_age_value(row1, rows[0])
                span = (w1 - w0) or 1
                age = a0 + ((a1 - a0) * ((weight - w0) / span))
                break
    return _protocol_curve_point(rows, age)


def production_protocol_curve_for_lot(lot: Lot, age_days: int | float):
    rows = _production_protocol_rows_for_lot(lot)
    return _protocol_curve_point(rows, age_days) if rows else None


def nursery_protocol_curve_for_lot(lot: Lot, age_days: int | float):
    if not lot or not lot.entry_pl_stage:
        return None
    min_stage = NURSERY_PROTOCOL_ROWS[0]['pl_stage']
    max_stage = NURSERY_PROTOCOL_ROWS[-1]['pl_stage']
    stage_today = min(max_stage, max(min_stage, lot.entry_pl_stage + int(max(age_days or 0, 0))))
    row = get_nursery_protocol_row(stage_today)
    if not row:
        return None
    return {
        'age_days': int(max(age_days or 0, 0)),
        'expected_weight_g': round(row['individual_weight_g'], 4),
        'daily_gain_g': row.get('daily_growth_g') or 0.001,
        'survival_pct': row.get('survival_pct'),
        'feed_rate_pct': row.get('feed_rate_pct'),
        'estimated_fcr': row.get('estimated_fcr'),
        'source': 'Tabela operacional 160.000 PL — berçário',
    }


def standard_growout_curve_point(age_days: int | float):
    rows = [row for row in PRODUCTION_PROTOCOL_ROWS if row.get('phase') == 'engorda']
    return _protocol_curve_point(rows, age_days)


def standard_growout_curve_by_weight(weight_g: float | int | None):
    weight = max(parse_float(weight_g, 0) or 0, 0)
    rows = PRODUCTION_PROTOCOL_ROWS if 0 < weight < 1.5 else [row for row in PRODUCTION_PROTOCOL_ROWS if row.get('phase') == 'engorda']
    return _protocol_curve_by_weight(rows, weight)


def standard_expected_weight_at_age(lot: Lot, age_days: int):
    """Peso esperado inicial, com a tabela como base e deslocamento pelo primeiro dado real do lote."""
    if is_nursery_lot(lot):
        base = nursery_protocol_curve_for_lot(lot, age_days)
        if base:
            return {
                'expected_weight_g': base['expected_weight_g'],
                'confidence': 45,
                'similar_cases': 0,
                'source': base['source'],
                'standard_feed_rate_pct': base.get('feed_rate_pct'),
                'standard_survival_pct': base.get('survival_pct'),
                'standard_fcr': base.get('estimated_fcr'),
            }

    if is_growout_lot(lot):
        base = production_protocol_curve_for_lot(lot, age_days) or standard_growout_curve_point(age_days)
        expected = base['expected_weight_g']
        observations = merged_weight_observations(lot.id)
        if observations:
            first = observations[0]
            first_age = max((first['date'] - lot.start_date).days, 0)
            first_base = (production_protocol_curve_for_lot(lot, first_age) or standard_growout_curve_point(first_age))['expected_weight_g']
            offset = (first['weight_g'] or first_base) - first_base
            expected = max(0.03, expected + offset)
        return {
            'expected_weight_g': round(expected, 2),
            'confidence': 45,
            'similar_cases': 0,
            'source': base['source'],
            'standard_feed_rate_pct': base['feed_rate_pct'],
            'standard_survival_pct': base['survival_pct'],
            'standard_fcr': base['estimated_fcr'],
        }

    expected = round(max((lot.estimated_weight_g or 0) + max(historical_growth_rate(lot), 0.08) * max(age_days, 0), 0.03), 2)
    return {
        'expected_weight_g': expected,
        'confidence': 35,
        'similar_cases': 0,
        'source': 'Histórico simples do lote',
        'standard_feed_rate_pct': None,
        'standard_survival_pct': None,
        'standard_fcr': None,
    }


def standard_survival_pct_for_lot(lot: Lot, on_date=None):
    if not lot:
        return None
    on_date = on_date or date.today()
    age_days = max((on_date - lot.start_date).days, 0)
    if is_nursery_lot(lot):
        curve = nursery_protocol_curve_for_lot(lot, age_days)
        return curve.get('survival_pct') if curve else None
    if not is_growout_lot(lot):
        return None
    curve = production_protocol_curve_for_lot(lot, age_days) or standard_growout_curve_point(age_days)
    return curve['survival_pct']


def table_final_survival_pct_for_lot(lot: Lot):
    if not lot:
        return None
    if is_nursery_lot(lot):
        return NURSERY_PROTOCOL_ROWS[-1].get('survival_pct')
    rows = _production_protocol_rows_for_lot(lot)
    if rows:
        return rows[-1].get('survival_pct')
    return None


def learned_survival_profile(lot: Lot):
    if not lot:
        return {'survival_pct': None, 'cases': 0, 'confidence': 0}
    target_phase = normalize_text(lot.phase or '')
    target_supplier = normalize_text(lot.larva_supplier or '')
    target_density = lot_density_snapshot(lot) or 0
    candidates = Lot.query.filter(
        Lot.status == 'encerrado',
        Lot.id != lot.id,
        Lot.initial_count.isnot(None),
        Lot.initial_count > 0,
    ).all()
    scored = []
    for hist_lot in candidates:
        summary = lot_financial_summary(hist_lot)
        survival = summary.get('survival_pct')
        if survival is None or survival <= 0:
            continue
        hist_phase = normalize_text(hist_lot.phase or '')
        phase_penalty = 0 if hist_phase == target_phase else 1.8
        supplier_penalty = 0 if target_supplier and normalize_text(hist_lot.larva_supplier or '') == target_supplier else 0.8
        hist_density = lot_density_snapshot(hist_lot) or target_density
        density_penalty = abs((hist_density or 0) - (target_density or 0)) / 30.0 if target_density else 0.2
        distance = phase_penalty + supplier_penalty + density_penalty
        weight = 1 / (1 + distance)
        scored.append((distance, weight, survival))
    if not scored:
        return {'survival_pct': None, 'cases': 0, 'confidence': 0}
    top = sorted(scored, key=lambda item: item[0])[:35]
    total_weight = sum(item[1] for item in top)
    learned = sum(item[2] * item[1] for item in top) / total_weight if total_weight else None
    return {
        'survival_pct': round(learned, 2) if learned is not None else None,
        'cases': len(top),
        'confidence': min(92, 28 + len(top) * 3),
    }


def adaptive_survival_profile_for_lot(lot: Lot, on_date=None):
    standard = standard_survival_pct_for_lot(lot, on_date=on_date)
    if standard is None:
        return {'survival_pct': None, 'standard_survival_pct': None, 'learned_final_survival_pct': None, 'cases': 0, 'confidence': 0, 'source': 'sem curva de sobrevivência'}
    final_standard = table_final_survival_pct_for_lot(lot) or standard
    learned = learned_survival_profile(lot)
    if not learned.get('survival_pct') or not learned.get('cases'):
        return {
            'survival_pct': round(standard, 2),
            'standard_survival_pct': round(standard, 2),
            'learned_final_survival_pct': None,
            'cases': 0,
            'confidence': 45,
            'source': 'sobrevivência da tabela-base',
        }
    standard_loss_now = max(100 - standard, 0)
    standard_loss_final = max(100 - final_standard, 0.1)
    learned_final = max(min(learned['survival_pct'], 100), 0)
    learned_loss_final = max(100 - learned_final, 0)
    mortality_factor = learned_loss_final / standard_loss_final
    learned_at_age = 100 - (standard_loss_now * mortality_factor)
    learned_at_age = max(min(learned_at_age, 100), 30)
    historical_weight = min(0.55, 0.18 + learned['cases'] * 0.035)
    adaptive = (standard * (1 - historical_weight)) + (learned_at_age * historical_weight)
    return {
        'survival_pct': round(max(min(adaptive, 100), 0), 2),
        'standard_survival_pct': round(standard, 2),
        'learned_final_survival_pct': round(learned_final, 2),
        'cases': learned['cases'],
        'confidence': learned['confidence'],
        'source': 'tabela-base calibrada por sobrevivência real da fazenda',
    }


def modeled_live_count_for_lot(lot: Lot, on_date=None):
    """Contagem viva usada em projeções/sugestão quando ainda não há despesca real.

    Usa mortalidade lançada, mas evita superestimar biomassa quando há pouca mortalidade visível,
    aplicando a sobrevivência da tabela-base calibrada pelo histórico real como teto inicial. Biomassa real lançada em biometria
    continua tendo prioridade nas sugestões de ração.
    """
    on_date = on_date or date.today()
    allocations = LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
    ).all()
    base_count = sum((allocation.quantity_allocated or 0) for allocation in allocations) or (parse_int(getattr(lot, 'initial_count', 0), 0) or 0)
    mortality = total_mortality_for_lot(lot.id, up_to_date=on_date)
    harvested = lot_total_harvested_units(lot.id)
    mortality_adjusted = max(base_count - mortality - harvested, 0)
    survival_profile = adaptive_survival_profile_for_lot(lot, on_date=on_date)
    modeled_survival = survival_profile.get('survival_pct')
    if modeled_survival is None or harvested > 0 or not base_count:
        return mortality_adjusted
    modeled_adjusted = int(round(base_count * (modeled_survival / 100)))
    return max(min(mortality_adjusted, modeled_adjusted), 0)


def pellet_hint_for_weight(weight_g):
    weight = parse_float(weight_g, 0) or 0
    curve = standard_growout_curve_by_weight(weight)
    if curve and curve.get('mixes'):
        labels = [item.get('label') for item in curve['mixes'] if item.get('label')]
        if labels:
            return ' / '.join(dict.fromkeys(labels))
    if weight <= 0.8:
        return 'Crumbled I / II'
    if weight <= 1.5:
        return 'Wean 0,8 / 1,3 mm'
    if weight <= 6.5:
        return 'Engorda J 2,0 mm'
    return 'Engorda 2,4 mm'

def build_historical_curve_dataset(phase=None):
    dataset = []
    lots = Lot.query.options(joinedload(Lot.unit)).all()
    for hist_lot in lots:
        if phase and hist_lot.phase != phase:
            continue
        density = lot_density_snapshot(hist_lot)
        supplier_key = normalize_text(hist_lot.larva_supplier or '')
        for obs in merged_weight_observations(hist_lot.id):
            age_days = max((obs['date'] - hist_lot.start_date).days, 0)
            if not obs['weight_g'] or obs['weight_g'] <= 0:
                continue
            dataset.append({
                'lot_id': hist_lot.id,
                'phase': hist_lot.phase,
                'age_days': age_days,
                'weight_g': obs['weight_g'],
                'density': density,
                'supplier_key': supplier_key,
                'source': obs['source'],
            })
    return dataset


def adaptive_expected_weight_at_age(lot: Lot, age_days: int):
    baseline = standard_expected_weight_at_age(lot, age_days)
    fallback = baseline['expected_weight_g']
    dataset = build_historical_curve_dataset(lot.phase)
    target_density = lot_density_snapshot(lot) or 0
    target_supplier = normalize_text(lot.larva_supplier or '')
    scored = []
    for row in dataset:
        if row['phase'] != lot.phase:
            continue
        age_distance = abs((row['age_days'] or 0) - age_days)
        density_distance = abs((row['density'] or target_density or 0) - target_density)
        supplier_penalty = 0 if row['supplier_key'] == target_supplier else 2.0
        same_lot_penalty = 0.15 if row['lot_id'] == lot.id else 0
        distance = (age_distance / 6.0) + (density_distance / 20.0) + supplier_penalty + same_lot_penalty
        weight_score = 1 / (1 + distance)
        scored.append((distance, weight_score, row))
    if not scored:
        return {
            'expected_weight_g': fallback,
            'confidence': baseline['confidence'],
            'similar_cases': 0,
            'source': baseline.get('source'),
            'standard_feed_rate_pct': baseline.get('standard_feed_rate_pct'),
            'standard_survival_pct': baseline.get('standard_survival_pct'),
            'standard_fcr': baseline.get('standard_fcr'),
        }
    top = [item for item in sorted(scored, key=lambda item: item[0])[:45] if item[1] > 0]
    weighted_sum = sum(item[2]['weight_g'] * item[1] for item in top)
    weight_total = sum(item[1] for item in top)
    historical_expected = round(weighted_sum / weight_total, 2) if weight_total else fallback
    # Quanto mais casos reais existem, menor o peso da tabela-base. A tabela nunca some totalmente:
    # ela continua como trilho inicial/segurança para lotes novos ou dados escassos.
    baseline_weight = max(0.15, 0.62 - (len(top) * 0.018))
    expected = round((fallback * baseline_weight) + (historical_expected * (1 - baseline_weight)), 2)
    confidence = min(97, max(baseline['confidence'], 40 + len(top) * 2))
    return {
        'expected_weight_g': expected,
        'confidence': int(confidence),
        'similar_cases': len(top),
        'source': 'Tabela padrão + histórico real da fazenda',
        'standard_feed_rate_pct': baseline.get('standard_feed_rate_pct'),
        'standard_survival_pct': baseline.get('standard_survival_pct'),
        'standard_fcr': baseline.get('standard_fcr'),
    }


def current_weight_for_lot(lot):
    observations = merged_weight_observations(lot.id)
    if observations:
        return observations[-1]['weight_g']
    return lot.estimated_weight_g or 0


def historical_growth_rate(lot):
    observations = merged_weight_observations(lot.id)
    if len(observations) >= 2:
        first = observations[0]
        last = observations[-1]
        days = max((last['date'] - first['date']).days, 1)
        rate = ((last['weight_g'] or 0) - (first['weight_g'] or 0)) / days
        if rate > 0:
            return round(rate, 3)
    age = max((date.today() - lot.start_date).days, 0) if lot and lot.start_date else 0
    if is_nursery_lot(lot):
        curve = nursery_protocol_curve_for_lot(lot, age)
        return round(curve.get('daily_gain_g') or 0.001, 4) if curve else 0.001
    if is_growout_lot(lot):
        curve_now = production_protocol_curve_for_lot(lot, age) or standard_growout_curve_point(age)
        curve_future = production_protocol_curve_for_lot(lot, age + 7) or standard_growout_curve_point(age + 7)
        return round(max((curve_future['expected_weight_g'] - curve_now['expected_weight_g']) / 7, 0.03), 3)
    return 0.08


def smart_growth_projection(lot, days_ahead=7):
    current_age = max((date.today() - lot.start_date).days, 0)
    current_weight = parse_float(current_weight_for_lot(lot), 0) or 0
    curve_now = adaptive_expected_weight_at_age(lot, current_age)
    curve_future = adaptive_expected_weight_at_age(lot, current_age + days_ahead)
    expected_gain = max((curve_future['expected_weight_g'] - curve_now['expected_weight_g']) / max(days_ahead, 1), 0)
    lot_gain = max(historical_growth_rate(lot), 0)
    env = lot_environment_snapshot(lot)
    environment_factor = 1.0
    drivers = []
    if env.get('dissolved_oxygen') is not None and env['dissolved_oxygen'] < TARGET_OD_MIN:
        environment_factor *= 0.88
        drivers.append('OD baixo freando crescimento')
    if env.get('ammonia') is not None and env['ammonia'] > TARGET_AMMONIA_MAX:
        environment_factor *= 0.9
        drivers.append('Amônia alta segurando ganho')
    if env.get('temperature_c') is not None:
        if env['temperature_c'] < TARGET_TEMP_MIN:
            environment_factor *= 0.94
            drivers.append('Temperatura abaixo da faixa ideal')
        elif env['temperature_c'] > TARGET_TEMP_MAX:
            environment_factor *= 0.95
            drivers.append('Temperatura acima da faixa ideal')
        else:
            environment_factor *= 1.03
            drivers.append('Temperatura favorável ao apetite')
    blended_gain = max(((expected_gain * 0.65) + (lot_gain * 0.35)) * environment_factor, 0.02 if current_weight else 0)
    projected_weight = round(current_weight + blended_gain * days_ahead, 2) if current_weight else curve_future['expected_weight_g']
    gap_pct = None
    if current_weight and curve_now['expected_weight_g']:
        gap_pct = round(((current_weight - curve_now['expected_weight_g']) / curve_now['expected_weight_g']) * 100, 2)
    return {
        'current_weight_g': round(current_weight or 0, 2),
        'expected_weight_g': curve_now['expected_weight_g'],
        'daily_gain_g': round(blended_gain, 3),
        'expected_daily_gain_g': round(expected_gain, 3),
        'lot_daily_gain_g': round(lot_gain, 3),
        'projected_weight_g': round(max(projected_weight, current_weight or 0), 2),
        'gap_pct': gap_pct,
        'environment_factor': round(environment_factor, 3),
        'environment': env,
        'drivers': drivers,
        'model_confidence': min(97, max(curve_future['confidence'], curve_now['confidence'])),
        'similar_cases': max(curve_future['similar_cases'], curve_now['similar_cases']),
    }


def total_mortality_for_lot(lot_id: int, up_to_date=None):
    query = db.session.query(func.coalesce(func.sum(DailyManagement.mortality_qty), 0)).filter(DailyManagement.lot_id == lot_id)
    if up_to_date:
        query = query.filter(DailyManagement.manage_date <= up_to_date)
    return int(query.scalar() or 0)


def current_live_count_for_lot(lot):
    allocations = LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= date.today()),
    ).all()
    base_count = sum((allocation.quantity_allocated or 0) for allocation in allocations) or (parse_int(getattr(lot, 'initial_count', 0), 0) or 0)
    mortality = total_mortality_for_lot(lot.id)
    harvested = lot_total_harvested_units(lot.id)
    return max(base_count - mortality - harvested, 0)


def feed_profile_for_weight(weight):
    curve = standard_growout_curve_by_weight(weight)
    base_pct = (curve['feed_rate_pct'] or 3.0) / 100
    return {
        'base_pct': base_pct,
        'min_pct': max(base_pct * 0.84, 0.018),
        'max_pct': min(base_pct * 1.16, 0.14),
        'pellet': pellet_hint_for_weight(weight),
        'curve_source': curve['source'],
        'standard_survival_pct': curve['survival_pct'],
        'standard_fcr': curve['estimated_fcr'],
        'feedings_per_day': curve.get('feedings_per_day') or 4,
        'base_daily_feed_kg_160k': curve.get('base_daily_feed_kg_160k'),
    }


def feed_profile_for_lot(lot, weight):
    profile = feed_profile_for_weight(weight)
    if lot and is_growout_lot(lot):
        age_days = max((date.today() - lot.start_date).days, 0)
        age_curve = production_protocol_curve_for_lot(lot, age_days) or standard_growout_curve_point(age_days)
        age_pct = (age_curve['feed_rate_pct'] or (profile['base_pct'] * 100)) / 100
        # Mescla peso real e idade do ciclo para evitar saltos quando a biometria atrasa.
        base_pct = (profile['base_pct'] * 0.7) + (age_pct * 0.3)
        profile.update({
            'base_pct': base_pct,
            'min_pct': max(base_pct * 0.84, 0.018),
            'max_pct': min(base_pct * 1.16, 0.14),
            'standard_survival_pct': age_curve['survival_pct'],
            'standard_fcr': age_curve['estimated_fcr'],
            'feedings_per_day': age_curve.get('feedings_per_day') or profile.get('feedings_per_day') or 4,
            'base_daily_feed_kg_160k': age_curve.get('base_daily_feed_kg_160k'),
            'curve_source': age_curve.get('source') or profile.get('curve_source'),
        })
    elif lot and is_nursery_lot(lot):
        age_days = max((date.today() - lot.start_date).days, 0)
        age_curve = nursery_protocol_curve_for_lot(lot, age_days)
        if age_curve:
            base_pct = (age_curve.get('feed_rate_pct') or (profile['base_pct'] * 100)) / 100
            profile.update({
                'base_pct': base_pct,
                'min_pct': max(base_pct * 0.84, 0.018),
                'max_pct': min(base_pct * 1.16, 0.40),
                'standard_survival_pct': age_curve.get('survival_pct'),
                'standard_fcr': age_curve.get('estimated_fcr'),
                'feedings_per_day': 12,
                'curve_source': age_curve.get('source'),
            })
    return profile


def learned_feed_profile(lot, weight_g):
    target_density = lot_density_snapshot(lot) or 0
    target_supplier = normalize_text(lot.larva_supplier or '')
    rows = DailyManagement.query.options(joinedload(DailyManagement.lot)).filter(
        DailyManagement.average_weight_g.isnot(None),
        DailyManagement.feed_offered_kg.isnot(None),
        DailyManagement.feed_offered_kg > 0,
    ).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).all()
    scored = []
    for row in rows:
        if not row.lot or row.lot.phase != lot.phase or row.average_weight_g is None:
            continue
        biomass_kg = row.estimated_biomass_kg
        if not biomass_kg:
            seed_count = modeled_live_count_for_lot(row.lot, on_date=row.manage_date) or parse_int(row.lot.initial_count, 0) or 0
            biomass_kg = (seed_count * (row.average_weight_g or 0)) / 1000 if seed_count and row.average_weight_g else 0
        if not biomass_kg:
            continue
        feed_pct = (row.feed_offered_kg or 0) / biomass_kg
        if feed_pct <= 0:
            continue
        row_density = lot_density_snapshot(row.lot) or target_density
        distance = abs((row.average_weight_g or 0) - weight_g) / 2.2
        distance += abs((row_density or 0) - target_density) / 25.0
        if normalize_text(row.lot.larva_supplier or '') != target_supplier:
            distance += 1.2
        scored.append((distance, 1 / (1 + distance), feed_pct))
    if not scored:
        return {'feed_pct': None, 'confidence': 0, 'cases': 0}
    top = sorted(scored, key=lambda item: item[0])[:40]
    total_weight = sum(item[1] for item in top)
    learned_pct = sum(item[2] * item[1] for item in top) / total_weight if total_weight else None
    confidence = min(94, 32 + len(top) * 2)
    return {'feed_pct': learned_pct, 'confidence': int(confidence), 'cases': len(top)}


def feeding_recommendation_for_lot(lot):
    weight = parse_float(current_weight_for_lot(lot), 0) or 0
    if weight <= 0:
        age_days = max((date.today() - lot.start_date).days, 0) if lot and lot.start_date else 0
        baseline = standard_expected_weight_at_age(lot, age_days)
        weight = parse_float(baseline.get('expected_weight_g'), 0) or 0
    live_count = modeled_live_count_for_lot(lot)
    records_by_lot = {lot.id: DailyManagement.query.filter(DailyManagement.lot_id == lot.id).order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all()}
    allocations_by_lot = {lot.id: LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= date.today()),
    ).all()}
    biomass_from_real_data = latest_biomass_for_lot(lot, records_by_lot, allocations_by_lot)
    biomass_source = 'curva padrão + sobrevivência estimada'
    if biomass_from_real_data and biomass_from_real_data > 0:
        biomass_kg = biomass_from_real_data
        if weight > 0:
            live_count = int(round((biomass_kg * 1000) / weight))
        biomass_source = 'biometria/manejo lançado'
    else:
        biomass_kg = (live_count * weight) / 1000 if weight > 0 and live_count > 0 else 0
    profile = feed_profile_for_lot(lot, weight)
    survival_model = adaptive_survival_profile_for_lot(lot)
    growth_projection = smart_growth_projection(lot, 7)
    learned = learned_feed_profile(lot, weight)
    learned_pct = learned['feed_pct'] if learned['feed_pct'] is not None else profile['base_pct']
    historical_weight = min(0.52, 0.22 + (learned['cases'] * 0.012)) if learned['feed_pct'] is not None else 0.0
    blended_pct = (profile['base_pct'] * (1 - historical_weight)) + (learned_pct * historical_weight)
    growth_factor = 1.0
    drivers = []
    if profile.get('curve_source'):
        drivers.append(f"Curva-base: {profile.get('curve_source')}")
        drivers.append('Ração recalculada proporcionalmente à população/biomassa real do lote')
    if biomass_source == 'biometria/manejo lançado':
        drivers.append('Biomassa real lançada priorizada')
    elif survival_model.get('survival_pct') is not None:
        drivers.append(f"Sobrevivência usada na biomassa viva: {survival_model['survival_pct']}%")
        if survival_model.get('cases'):
            drivers.append(f"Sobrevivência calibrada com {survival_model['cases']} ciclo(s) encerrado(s)")
    elif profile.get('standard_survival_pct') is not None:
        drivers.append(f"Sobrevivência inicial estimada: {profile['standard_survival_pct']}%")
    if growth_projection.get('gap_pct') is not None:
        if growth_projection['gap_pct'] <= -5:
            growth_factor *= 1.06
            drivers.append('Lote atrasado puxando oferta para cima')
        elif growth_projection['gap_pct'] >= 8:
            growth_factor *= 0.96
            drivers.append('Lote acima da curva permitindo ajuste fino')
    water_factor = 1.0
    env = growth_projection.get('environment') or {}
    if env.get('dissolved_oxygen') is not None and env['dissolved_oxygen'] < TARGET_OD_MIN:
        water_factor *= 0.92
        drivers.append('OD baixo segurando consumo')
    if env.get('ammonia') is not None and env['ammonia'] > TARGET_AMMONIA_MAX:
        water_factor *= 0.9
        drivers.append('Amônia alta pedindo cautela')
    if env.get('temperature_c') is not None and TARGET_TEMP_MIN <= env['temperature_c'] <= TARGET_TEMP_MAX:
        water_factor *= 1.03
        drivers.append('Temperatura boa sustentando apetite')
    final_pct = blended_pct * growth_factor * water_factor
    final_pct = min(max(final_pct, profile['min_pct']), profile['max_pct'])
    suggested = biomass_kg * final_pct
    feedings_per_day = int(profile.get('feedings_per_day') or 4)
    feed_per_feeding = suggested / feedings_per_day if feedings_per_day else suggested
    gap_pct = growth_projection.get('gap_pct')
    env_attention = False
    if env.get('dissolved_oxygen') is not None and env['dissolved_oxygen'] < TARGET_OD_MIN:
        env_attention = True
    if env.get('ammonia') is not None and env['ammonia'] > TARGET_AMMONIA_MAX:
        env_attention = True
    if gap_pct is not None and gap_pct <= -5:
        attention_level = 'danger'
        attention_label = 'Atrasado na curva'
        action_hint = 'Priorize conferência de bandeja/OD; se a bandeja limpar bem, trabalhe mais perto do topo da faixa segura.'
    elif env_attention:
        attention_level = 'warning'
        attention_label = 'Atenção à água'
        action_hint = 'Mantenha a oferta sugerida ou reduza para a base da faixa se OD cair, amônia subir ou sobrar ração na bandeja.'
    elif gap_pct is not None and gap_pct >= 8:
        attention_level = 'success'
        attention_label = 'Acima da curva'
        action_hint = 'Pode trabalhar no meio ou base da faixa segura, acompanhando bandeja de controle.'
    else:
        attention_level = 'neutral'
        attention_label = 'Dentro da curva'
        action_hint = 'Use o valor sugerido como ponto de partida e ajuste pela bandeja de controle.'
    return {
        'model_name': 'Tabela padrão + modelo adaptativo da fazenda',
        'biomass_kg': round(biomass_kg, 2),
        'biomass_source': biomass_source,
        'live_count': live_count,
        'standard_survival_pct': profile.get('standard_survival_pct'),
        'adaptive_survival_pct': survival_model.get('survival_pct'),
        'learned_final_survival_pct': survival_model.get('learned_final_survival_pct'),
        'survival_model_source': survival_model.get('source'),
        'survival_cases': survival_model.get('cases'),
        'standard_fcr': profile.get('standard_fcr'),
        'base_pct_biomass': round(profile['base_pct'] * 100, 2),
        'learned_pct_biomass': round((learned_pct or profile['base_pct']) * 100, 2),
        'feed_pct_biomass': round(final_pct * 100, 2),
        'suggested_feed_kg': round(suggested, 2),
        'feedings_per_day': feedings_per_day,
        'feed_per_feeding_kg': round(feed_per_feeding, 2),
        'min_feed_kg': round(biomass_kg * max(profile['min_pct'], final_pct * 0.92), 2),
        'max_feed_kg': round(biomass_kg * min(profile['max_pct'], final_pct * 1.08), 2),
        'pellet_hint': profile['pellet'],
        'attention_level': attention_level,
        'attention_label': attention_label,
        'action_hint': action_hint,
        'current_weight_g': round(weight or 0, 2),
        'expected_weight_g': growth_projection.get('expected_weight_g'),
        'growth_gap_pct': growth_projection.get('gap_pct'),
        'historical_confidence': learned['confidence'],
        'historical_cases': learned['cases'],
        'growth_factor_pct': round((growth_factor - 1) * 100, 1),
        'water_factor_pct': round((water_factor - 1) * 100, 1),
        'drivers': drivers + growth_projection.get('drivers', []),
        'daily_gain_g': growth_projection.get('daily_gain_g'),
        'projected_weight_7d_g': growth_projection.get('projected_weight_g'),
        'model_confidence': growth_projection.get('model_confidence'),
        'similar_cases': growth_projection.get('similar_cases'),
    }


def build_growth_analysis(lot):
    if not lot:
        return {'points': [], 'summary': None}
    points = []
    observations = merged_weight_observations(lot.id)
    for obs in observations:
        days = max((obs['date'] - lot.start_date).days, 0)
        curve = adaptive_expected_weight_at_age(lot, days)
        real_weight = round(obs['weight_g'] or 0, 2)
        expected_weight = round(curve['expected_weight_g'] or 0, 2)
        points.append({
            'date': obs['date'].strftime('%d/%m/%Y'),
            'days': days,
            'real': real_weight,
            'expected': expected_weight,
            'deviation': round(real_weight - expected_weight, 2),
            'deviation_pct': round(((real_weight - expected_weight) / expected_weight) * 100, 2) if expected_weight else None,
            'source': obs['source_label'],
            'confidence': curve['confidence'],
        })
    projection_7 = smart_growth_projection(lot, 7)
    projection_14 = smart_growth_projection(lot, 14)
    current_age = max((date.today() - lot.start_date).days, 0)
    curve_today = adaptive_expected_weight_at_age(lot, current_age)
    current_weight = parse_float(current_weight_for_lot(lot), None)
    summary = None
    if points:
        last = points[-1]
        summary = {
            'current_weight_g': last['real'],
            'expected_weight_g': last['expected'],
            'deviation_g': last['deviation'],
            'deviation_pct': last['deviation_pct'],
            'projection_7d_g': projection_7['projected_weight_g'],
            'projection_14d_g': projection_14['projected_weight_g'],
            'daily_gain_g': projection_7['daily_gain_g'],
            'model_confidence': projection_7['model_confidence'],
            'similar_cases': projection_7['similar_cases'],
            'drivers': projection_7['drivers'],
            'summary_source': 'biometria/manejo real + curva-base',
        }
    else:
        displayed_weight = round(current_weight or curve_today['expected_weight_g'] or 0, 2)
        has_manual_weight = current_weight is not None and current_weight > 0
        deviation_g = round(displayed_weight - curve_today['expected_weight_g'], 2) if has_manual_weight else None
        deviation_pct = round((deviation_g / curve_today['expected_weight_g']) * 100, 2) if deviation_g is not None and curve_today['expected_weight_g'] else None
        summary = {
            'current_weight_g': displayed_weight,
            'expected_weight_g': curve_today['expected_weight_g'],
            'deviation_g': deviation_g,
            'deviation_pct': deviation_pct,
            'projection_7d_g': projection_7['projected_weight_g'],
            'projection_14d_g': projection_14['projected_weight_g'],
            'daily_gain_g': projection_7['daily_gain_g'],
            'model_confidence': projection_7['model_confidence'],
            'similar_cases': projection_7['similar_cases'],
            'drivers': projection_7['drivers'] + ['Sem biometria real ainda: usando a tabela-base como trilho inicial'],
            'summary_source': 'curva-base da tabela até entrar biometria real',
        }
    return {'points': points, 'summary': summary}


def supplier_performance_rows():
    supplier_rows = []
    suppliers = [name for (name,) in db.session.query(Lot.larva_supplier).filter(Lot.larva_supplier.isnot(None), Lot.larva_supplier != '').distinct().all()]
    for supplier in suppliers:
        lots = Lot.query.filter(Lot.larva_supplier == supplier).all()
        if not lots:
            continue
        summaries = [lot_financial_summary(lot) for lot in lots]
        survival_values = [row['survival_pct'] for row in summaries if row['survival_pct'] is not None]
        fcr_values = [row['fcr_real'] for row in summaries if row['fcr_real'] is not None]
        sale_weights = [sale.average_weight_g for sale in Sale.query.join(Lot, Lot.id == Sale.lot_id).filter(Lot.larva_supplier == supplier, Sale.average_weight_g.isnot(None)).all() if sale.average_weight_g is not None]
        score = None
        if survival_values or fcr_values:
            survival_component = (sum(survival_values) / len(survival_values)) if survival_values else 0
            fcr_component = (sum(fcr_values) / len(fcr_values)) if fcr_values else 2.2
            score = round((survival_component * 0.6) + ((3 - min(fcr_component, 3)) * 20), 1)
        supplier_rows.append({
            'supplier': supplier,
            'lot_count': len(lots),
            'survival_avg': round(sum(survival_values) / len(survival_values), 2) if survival_values else None,
            'fcr_avg': round(sum(fcr_values) / len(fcr_values), 2) if fcr_values else None,
            'avg_sale_weight_g': round(sum(sale_weights) / len(sale_weights), 2) if sale_weights else None,
            'active_lots': sum(1 for lot in lots if lot.status == 'ativo'),
            'score': score,
        })
    supplier_rows.sort(key=lambda item: ((item['score'] is None), -(item['score'] or 0), (item['fcr_avg'] or 9)))
    return supplier_rows


def shrimp_price_from_weight(weight_g, base_price_10g=22.0):
    if weight_g is None:
        return round(base_price_10g, 2)
    return round(base_price_10g + (round(weight_g) - 10), 2)


def lot_feed_cost_info(lot):
    rows = DailyManagement.query.filter(
        DailyManagement.lot_id == lot.id,
        DailyManagement.feed_unit_cost.isnot(None),
        DailyManagement.feed_unit_cost > 0,
        DailyManagement.feed_offered_kg.isnot(None),
        DailyManagement.feed_offered_kg > 0,
    ).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).limit(15).all()
    if rows:
        total_qty = sum(row.feed_offered_kg or 0 for row in rows)
        if total_qty > 0:
            total_value = sum((row.feed_unit_cost or 0) * (row.feed_offered_kg or 0) for row in rows)
            return {
                'cost': round(total_value / total_qty, 2),
                'source': 'média ponderada das últimas rações lançadas neste lote',
                'rows': len(rows),
            }

    lot_movements = FeedInventory.query.filter(
        FeedInventory.lot_id == lot.id,
        FeedInventory.unit_cost.isnot(None),
        FeedInventory.unit_cost > 0,
    ).order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).limit(20).all()
    if lot_movements:
        total_qty = sum(abs(movement.quantity_kg or 0) for movement in lot_movements)
        if total_qty > 0:
            total_value = sum(abs(movement.quantity_kg or 0) * (movement.unit_cost or 0) for movement in lot_movements)
            return {
                'cost': round(total_value / total_qty, 2),
                'source': 'movimentações de ração vinculadas ao lote',
                'rows': len(lot_movements),
            }

    global_entries = FeedInventory.query.filter(
        FeedInventory.movement_type == 'entrada',
        FeedInventory.unit_cost.isnot(None),
        FeedInventory.unit_cost > 0,
    ).order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).limit(40).all()
    if global_entries:
        total_qty = sum(entry.quantity_kg or 0 for entry in global_entries)
        if total_qty > 0:
            total_value = sum((entry.quantity_kg or 0) * (entry.unit_cost or 0) for entry in global_entries)
            return {
                'cost': round(total_value / total_qty, 2),
                'source': 'média das entradas recentes de estoque, pois o lote ainda não tem consumo com custo',
                'rows': len(global_entries),
            }
    return {'cost': 6.0, 'source': 'valor padrão provisório, sem custo de ração lançado', 'rows': 0}


def recent_feed_cost_per_kg(lot):
    return lot_feed_cost_info(lot)['cost']


def harvest_decision_analysis(lot, base_price_10g=22.0, feed_cost_kg=None):
    if not lot:
        return None
    feed_cost_info = lot_feed_cost_info(lot)
    feed_cost_override = feed_cost_kg is not None
    feed_cost_kg = feed_cost_kg if feed_cost_override else feed_cost_info['cost']
    current_rec = feeding_recommendation_for_lot(lot)
    live_count = current_rec['live_count']
    fixed_cost_total = calculate_fixed_cost_for_lot(lot)
    cycle_days = max((date.today() - lot.start_date).days, 1)
    fixed_cost_day = fixed_cost_total / cycle_days if cycle_days else 0
    scenarios = []
    for days_wait in (0, 7, 14, 21):
        projection = smart_growth_projection(lot, days_wait)
        weight_g = projection['current_weight_g'] if days_wait == 0 else projection['projected_weight_g']
        biomass_kg = round((live_count * (weight_g or 0)) / 1000, 2) if live_count and weight_g else 0
        price_kg = shrimp_price_from_weight(weight_g, base_price_10g)
        revenue = round(biomass_kg * price_kg, 2)
        extra_feed_cost = round((current_rec['suggested_feed_kg'] or 0) * days_wait * feed_cost_kg, 2)
        extra_fixed_cost = round(fixed_cost_day * days_wait, 2)
        scenarios.append({
            'days_wait': days_wait,
            'projected_date': date.today() + timedelta(days=days_wait),
            'weight_g': round(weight_g or 0, 2),
            'price_kg': price_kg,
            'biomass_kg': biomass_kg,
            'revenue': revenue,
            'extra_feed_cost': extra_feed_cost,
            'extra_fixed_cost': extra_fixed_cost,
            'net_value': round(revenue - extra_feed_cost - extra_fixed_cost, 2),
            'daily_gain_g': projection['daily_gain_g'],
            'confidence': projection['model_confidence'],
        })
    base = scenarios[0]
    for scenario in scenarios:
        scenario['incremental_gain'] = round(scenario['net_value'] - base['net_value'], 2)
    best = max(scenarios, key=lambda row: row['net_value']) if scenarios else None
    decision = 'Despescar agora' if best and best['days_wait'] == 0 else f'Esperar {best["days_wait"]} dias' if best else None
    return {
        'base_price_10g': base_price_10g,
        'feed_cost_kg': feed_cost_kg,
        'feed_cost_source': 'informado manualmente' if feed_cost_override else feed_cost_info['source'],
        'feed_cost_rows': feed_cost_info.get('rows', 0),
        'scenarios': scenarios,
        'best': best,
        'decision': decision,
        'current_recommendation': current_rec,
    }


def projected_cashflow_rows(days=90, base_price_10g=22.0):
    rows = []
    horizon = date.today() + timedelta(days=days)
    for lot in Lot.query.filter_by(status='ativo').order_by(Lot.start_date.asc()).all():
        current_weight = current_weight_for_lot(lot)
        projection = smart_growth_projection(lot, 7)
        growth = projection.get('daily_gain_g') or 0
        if not current_weight or growth <= 0:
            continue
        if current_weight >= TARGET_HARVEST_WEIGHT_G:
            harvest_date = date.today()
            projected_weight = current_weight
        else:
            days_to_target = max(int(round((TARGET_HARVEST_WEIGHT_G - current_weight) / growth)), 1)
            harvest_date = date.today() + timedelta(days=days_to_target)
            projected_weight = smart_growth_projection(lot, days_to_target).get('projected_weight_g')
        if harvest_date > horizon:
            continue
        live_count = current_live_count_for_lot(lot)
        biomass_kg = round((live_count * (projected_weight or 0)) / 1000, 2) if live_count and projected_weight else 0
        rows.append({
            'date': harvest_date,
            'lot': lot,
            'weight_g': round(projected_weight or 0, 2),
            'price_kg': shrimp_price_from_weight(projected_weight, base_price_10g),
            'amount': round(biomass_kg * shrimp_price_from_weight(projected_weight, base_price_10g), 2),
            'biomass_kg': biomass_kg,
        })
    rows.sort(key=lambda item: (item['date'], item['lot'].lot_code))
    return rows


def finance_summary(days=90, base_price_10g=22.0):
    start = date.today()
    end = start + timedelta(days=days)
    entries = FinanceEntry.query.order_by(FinanceEntry.due_date.asc().nullslast(), FinanceEntry.entry_date.desc()).all()
    payable_open = sum((entry.amount or 0) for entry in entries if entry.entry_type == 'pagar' and entry.status == 'aberto')
    receivable_open = sum((entry.amount or 0) for entry in entries if entry.entry_type == 'receber' and entry.status == 'aberto')
    projected_entries = [entry for entry in entries if (entry.due_date or entry.entry_date) and start <= (entry.due_date or entry.entry_date) <= end]
    projected_balance = sum((entry.amount or 0) * (1 if entry.entry_type == 'receber' else -1) for entry in projected_entries)
    projected_harvests = projected_cashflow_rows(days=days, base_price_10g=base_price_10g)
    projected_harvest_receipts = sum(row['amount'] for row in projected_harvests)
    return {
        'entries': entries,
        'payable_open': round(payable_open, 2),
        'receivable_open': round(receivable_open, 2),
        'projected_balance': round(projected_balance, 2),
        'projected_entries': projected_entries,
        'projected_harvests': projected_harvests,
        'projected_harvest_receipts': round(projected_harvest_receipts, 2),
        'projected_balance_with_harvests': round(projected_balance + projected_harvest_receipts, 2),
        'period_days': days,
        'base_price_10g': base_price_10g,
    }


def assistant_answer(question: str):
    q = normalize_text(question or '')
    if not q:
        return 'Pergunte sobre arraçoamento, fornecedor de PL, crescimento, despesca ideal ou caixa projetado.'
    if 'arraço' in q or 'racao' in q or 'ração' in q:
        rows = []
        for lot in Lot.query.filter_by(status='ativo').all():
            rec = feeding_recommendation_for_lot(lot)
            rows.append((rec['suggested_feed_kg'], lot.lot_code, rec))
        if not rows:
            return 'Ainda não há lotes ativos para recomendar arraçoamento.'
        top = sorted(rows, reverse=True)[0]
        rec = top[2]
        return f"Maior oferta sugerida hoje: lote {top[1]} com {rec['suggested_feed_kg']:.2f} kg/dia. O modelo está em {rec['feed_pct_biomass']:.2f}% da biomassa, baseado no histórico da fazenda e na curva atual do lote."
    if 'fornecedor' in q or 'pl' in q:
        rows = supplier_performance_rows()
        if not rows:
            return 'Ainda não há dados suficientes de fornecedores de PL para comparar.'
        top = rows[0]
        return f"Melhor fornecedor até agora: {top['supplier']}, score {top['score'] or 0}, sobrevivência média de {top['survival_avg'] or 0}% e FCR médio de {top['fcr_avg'] or 0}."
    if 'despesca' in q or 'vender' in q or 'colher' in q:
        analyses = []
        for lot in Lot.query.filter_by(status='ativo').all():
            analysis = harvest_decision_analysis(lot)
            if analysis and analysis['best']:
                analyses.append((analysis['best']['incremental_gain'], lot.lot_code, analysis))
        if not analyses:
            return 'Ainda não há dados suficientes para sugerir despesca.'
        best = sorted(analyses, reverse=True)[0]
        decision = best[2]['decision']
        return f"Melhor oportunidade agora: lote {best[1]}. Recomendação: {decision}. Ganho incremental estimado: {money_filter(best[0])}."
    if 'fcr' in q or 'pior lote' in q or 'viveiro pior' in q:
        rows = []
        for lot in Lot.query.order_by(Lot.start_date.desc()).all():
            summary = lot_financial_summary(lot)
            if summary['fcr_real'] is not None:
                rows.append((summary['fcr_real'], lot.lot_code))
        if not rows:
            return 'Ainda não há FCR real calculado para responder isso.'
        worst = sorted(rows, reverse=True)[0]
        return f'O lote com pior FCR real hoje é {worst[1]} com FCR {worst[0]:.2f}.'
    if 'atrasad' in q or 'crescimento' in q or 'curva' in q:
        scored = []
        for lot in Lot.query.filter_by(status='ativo').all():
            analysis = build_growth_analysis(lot)
            if analysis['summary']:
                summary = analysis['summary']
                scored.append((summary['deviation_g'], lot.lot_code, summary))
        if not scored:
            return 'Ainda não há dados de crescimento suficientes para comparar lotes.'
        lag = sorted(scored)[0]
        return f"O lote mais atrasado é {lag[1]}: peso real {lag[2]['current_weight_g']:.2f} g contra esperado {lag[2]['expected_weight_g']:.2f} g, desvio de {lag[2]['deviation_g']:.2f} g."
    if 'caixa' in q or 'financeiro' in q or 'receber' in q or 'pagar' in q:
        fin = finance_summary()
        return f"Em aberto hoje: contas a receber {money_filter(fin['receivable_open'])} e contas a pagar {money_filter(fin['payable_open'])}. Saldo projetado dos próximos {fin['period_days']} dias: {money_filter(fin['projected_balance'])}. Com despescas projetadas, o saldo vai para {money_filter(fin['projected_balance_with_harvests'])}."
    return 'Consigo responder melhor perguntas como: qual lote está mais atrasado, quanto ofertar hoje, qual fornecedor de PL foi melhor, qual lote compensa despescAR primeiro ou como está o caixa projetado.'


def build_pdf_response(title, headers, rows, filename):
    if canvas is None or A4 is None:
        flash('PDF indisponível porque a dependência reportlab não está instalada.', 'warning')
        return redirect(url_for('managerial_reports_page'))
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 40
    pdf.setFont('Helvetica-Bold', 14)
    pdf.drawString(40, y, title)
    y -= 24
    pdf.setFont('Helvetica', 8)
    pdf.drawString(40, y, f'Gerado em {datetime.now().strftime("%d/%m/%Y %H:%M")}')
    y -= 22
    col_width = max((width - 80) / max(len(headers), 1), 60)
    pdf.setFont('Helvetica-Bold', 8)
    x = 40
    for header in headers:
        pdf.drawString(x, y, str(header)[:22])
        x += col_width
    y -= 14
    pdf.setFont('Helvetica', 8)
    for row in rows:
        if y < 40:
            pdf.showPage()
            y = height - 40
            pdf.setFont('Helvetica', 8)
        x = 40
        for cell in row:
            pdf.drawString(x, y, str(cell if cell is not None else '')[:22])
            x += col_width
        y -= 12
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/feeding-planner')
@login_required
@requires_permission('dashboard')
def feeding_planner_page():
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    rows = []
    for lot in lots:
        rec = feeding_recommendation_for_lot(lot)
        projection = smart_growth_projection(lot, 7)
        rows.append({'lot': lot, 'rec': rec, 'projection': projection})
    rows.sort(key=lambda item: (item['rec'].get('growth_gap_pct') is None, item['rec'].get('growth_gap_pct', 999)))
    total_suggested = sum((row['rec'].get('suggested_feed_kg') or 0) for row in rows)
    attention_lots = sum(1 for row in rows if row['rec'].get('attention_level') in {'danger', 'warning'})
    survival_values = [row['rec'].get('adaptive_survival_pct') for row in rows if row['rec'].get('adaptive_survival_pct') is not None]
    model_summary = {
        'active_lots': len(rows),
        'avg_confidence': round(sum((row['rec'].get('model_confidence') or 0) for row in rows) / len(rows), 1) if rows else 0,
        'historical_cases': sum((row['rec'].get('historical_cases') or 0) for row in rows),
        'survival_cases': sum((row['rec'].get('survival_cases') or 0) for row in rows),
        'avg_survival_pct': round(sum(survival_values) / len(survival_values), 1) if survival_values else None,
        'total_suggested_feed_kg': round(total_suggested, 2),
        'attention_lots': attention_lots,
    }
    return render_template('feeding_planner.html', rows=rows, model_summary=model_summary)


def sync_biometrics_to_management(lot, unit_id, sample_date, average_weight_g, estimated_biomass_kg, notes=None):
    target_unit_id = unit_id or lot.unit_id
    row = DailyManagement.query.filter(
        DailyManagement.lot_id == lot.id,
        DailyManagement.manage_date == sample_date,
        DailyManagement.unit_id == target_unit_id,
    ).order_by(DailyManagement.id.desc()).first()
    if not row:
        row = DailyManagement(
            manage_date=sample_date,
            lot_id=lot.id,
            unit_id=target_unit_id,
            average_weight_g=average_weight_g,
            estimated_biomass_kg=estimated_biomass_kg,
            notes=None,
        )
        db.session.add(row)
    row.average_weight_g = average_weight_g
    row.estimated_biomass_kg = estimated_biomass_kg
    sync_note = f'Biometria sincronizada em {sample_date.strftime("%d/%m/%Y")}'
    note_text = ' | '.join(part for part in [notes, sync_note] if part)
    row.notes = note_text or row.notes
    row.updated_at = datetime.utcnow()
    lot.estimated_weight_g = average_weight_g
    return row


@app.route('/biometrics', methods=['GET', 'POST'])
@login_required
@requires_permission('dashboard')
def biometrics_page():
    lots = Lot.query.order_by(Lot.start_date.desc()).all()
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()

    def unit_lot_options_payload():
        allocations = (
            LotUnitAllocation.query.options(joinedload(LotUnitAllocation.lot))
            .join(Lot, Lot.id == LotUnitAllocation.lot_id)
            .filter(
                Lot.status == 'ativo',
                or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
            )
            .order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc())
            .all()
        )
        payload = {}
        for allocation in allocations:
            if not allocation.lot:
                continue
            payload.setdefault(str(allocation.unit_id), []).append({
                'lot_id': allocation.lot_id,
                'lot_code': allocation.lot.lot_code,
                'start_date': allocation.start_date.isoformat() if allocation.start_date else '',
                'end_date': allocation.end_date.isoformat() if allocation.end_date else '',
            })
        return payload

    if request.method == 'POST':
        submitted_lot_id = parse_int(request.form.get('lot_id'))
        unit_id = parse_int(request.form.get('unit_id'))
        sample_date = parse_date(request.form.get('sample_date'), default=date.today())
        average_weight_g = parse_float(request.form.get('average_weight_g'))
        sample_size = parse_int(request.form.get('sample_size'), 100) or 100
        cv_pct = parse_float(request.form.get('cv_pct'))
        estimated_biomass_kg = parse_float(request.form.get('estimated_biomass_kg'))
        notes = (request.form.get('notes') or '').strip()

        active_lot = active_lot_for_unit(unit_id, on_date=sample_date) if unit_id else None
        lot = active_lot or db.session.get(Lot, submitted_lot_id)
        if lot:
            lot_id = lot.id
            if not unit_id:
                current_units = lot_current_units(lot, on_date=sample_date)
                if len(current_units) == 1:
                    unit_id = current_units[0].id
        else:
            lot_id = submitted_lot_id

        if not lot or average_weight_g is None:
            flash('Informe viveiro com lote ativo e peso médio para salvar a biometria.', 'warning')
            return redirect(url_for('biometrics_page'))

        if not estimated_biomass_kg:
            live_count = current_live_count_for_lot(lot)
            estimated_biomass_kg = round((live_count * average_weight_g) / 1000, 2) if live_count and average_weight_g else None
        last = latest_biometric_for_lot(lot_id)
        weekly_gain = None
        if last and last.average_weight_g is not None:
            days = max((sample_date - last.sample_date).days, 1)
            weekly_gain = round(((average_weight_g - last.average_weight_g) / days) * 7, 3)
        row = BiometricsSample(sample_date=sample_date, lot_id=lot_id, unit_id=unit_id, sample_size=sample_size, average_weight_g=average_weight_g, cv_pct=cv_pct, estimated_biomass_kg=estimated_biomass_kg, weekly_gain_g=weekly_gain, notes=notes or None)
        db.session.add(row)
        sync_biometrics_to_management(lot, unit_id, sample_date, average_weight_g, estimated_biomass_kg, notes)
        db.session.commit()
        flash('Biometria registrada e sincronizada com o manejo diário.', 'success')
        return redirect(url_for('biometrics_page', history_unit_id=unit_id) if unit_id else url_for('biometrics_page'))

    history_unit_id = parse_int(request.args.get('history_unit_id'))
    rows_query = BiometricsSample.query.options(joinedload(BiometricsSample.lot), joinedload(BiometricsSample.unit))
    if history_unit_id:
        rows_query = rows_query.filter(BiometricsSample.unit_id == history_unit_id)
    rows = rows_query.order_by(BiometricsSample.sample_date.desc(), BiometricsSample.id.desc()).limit(60).all()

    enriched_rows = []
    for row in rows:
        if not row.lot:
            continue
        age_days = max((row.sample_date - row.lot.start_date).days, 0)
        expected = adaptive_expected_weight_at_age(row.lot, age_days)
        linked_management = DailyManagement.query.filter(DailyManagement.lot_id == row.lot_id, DailyManagement.manage_date == row.sample_date).first()
        enriched_rows.append({
            'row': row,
            'expected_weight_g': expected['expected_weight_g'],
            'deviation_g': round((row.average_weight_g or 0) - expected['expected_weight_g'], 3),
            'deviation_pct': round((((row.average_weight_g or 0) - expected['expected_weight_g']) / expected['expected_weight_g']) * 100, 2) if expected['expected_weight_g'] else None,
            'linked_management': bool(linked_management),
        })
    active_summaries = []
    for lot in Lot.query.filter_by(status='ativo').all():
        latest = latest_biometric_for_lot(lot.id)
        analysis = build_growth_analysis(lot)
        active_summaries.append({
            'lot': lot,
            'latest': latest,
            'summary': analysis['summary'],
        })
    return render_template(
        'biometrics.html',
        lots=lots,
        units=units,
        rows=enriched_rows,
        active_summaries=active_summaries,
        today=date.today(),
        history_unit_id=history_unit_id,
        unit_lot_options=unit_lot_options_payload(),
    )


@app.route('/growth-analysis')
@login_required
@requires_permission('dashboard')
def growth_analysis_page():
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    lot_id = request.args.get('lot_id', type=int)
    selected_lot = db.session.get(Lot, lot_id) if lot_id else (lots[0] if lots else None)
    analysis = build_growth_analysis(selected_lot) if selected_lot else {'points': [], 'summary': None}
    projection_21 = smart_growth_projection(selected_lot, 21) if selected_lot else None
    return render_template('growth_analysis.html', lots=lots, selected_lot=selected_lot, points=analysis['points'], summary=analysis['summary'], projection_21=projection_21)


@app.route('/pl-suppliers')
@login_required
@requires_permission('dashboard')
def pl_suppliers_page():
    rows = supplier_performance_rows()
    return render_template('pl_suppliers.html', rows=rows)


@app.route('/harvest-decision', methods=['GET', 'POST'])
@login_required
@requires_permission('dashboard')
def harvest_decision_page():
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    lot_id = request.values.get('lot_id', type=int)
    selected_lot = db.session.get(Lot, lot_id) if lot_id else (lots[0] if lots else None)
    base_price_10g = parse_float(request.values.get('base_price_10g'), 22.0) or 22.0
    feed_cost_kg = parse_float(request.values.get('feed_cost_kg'))
    analysis = harvest_decision_analysis(selected_lot, base_price_10g=base_price_10g, feed_cost_kg=feed_cost_kg) if selected_lot else None
    return render_template('harvest_decision.html', lots=lots, selected_lot=selected_lot, analysis=analysis)


@app.route('/finance', methods=['GET', 'POST'])
@login_required
@requires_permission('dashboard')
def finance_page():
    lots = Lot.query.order_by(Lot.start_date.desc()).all()
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    if request.method == 'POST':
        entry = FinanceEntry(
            entry_date=parse_date(request.form.get('entry_date'), default=date.today()),
            due_date=parse_date(request.form.get('due_date')),
            entry_type=(request.form.get('entry_type') or 'pagar').strip(),
            category=(request.form.get('category') or 'Geral').strip(),
            description=(request.form.get('description') or '').strip() or 'Lançamento',
            amount=parse_float(request.form.get('amount'), 0) or 0,
            status=(request.form.get('status') or 'aberto').strip(),
            lot_id=parse_int(request.form.get('lot_id')),
            unit_id=parse_int(request.form.get('unit_id')),
            notes=(request.form.get('notes') or '').strip() or None,
        )
        db.session.add(entry)
        db.session.commit()
        flash('Lançamento financeiro salvo.', 'success')
        return redirect(url_for('finance_page'))
    days = request.args.get('days', type=int) or 90
    base_price_10g = parse_float(request.args.get('base_price_10g'), 22.0) or 22.0
    summary = finance_summary(days=days, base_price_10g=base_price_10g)
    return render_template('finance.html', lots=lots, units=units, summary=summary, days=days)


@app.route('/assistant', methods=['GET', 'POST'])
@login_required
@requires_permission('dashboard')
def assistant_page():
    answer = None
    question = ''
    suggested_questions = [
        'Qual lote está mais atrasado?',
        'Quanto ofertar hoje?',
        'Qual fornecedor de PL está melhor?',
        'Qual lote compensa despescAR primeiro?',
        'Como está o caixa projetado?'
    ]
    if request.method == 'POST':
        question = (request.form.get('question') or '').strip()
        answer = assistant_answer(question)
    return render_template('assistant.html', question=question, answer=answer, suggested_questions=suggested_questions)


@app.post('/api/v1/sensor-reading')
@login_required
@requires_permission('water_manage')
def api_sensor_reading():
    payload = request.get_json(silent=True) or {}
    unit_id = payload.get('unit_id')
    if not unit_id:
        return jsonify({'ok': False, 'error': 'unit_id é obrigatório'}), 400
    reading_time = parse_time(payload.get('monitor_time')) if payload.get('monitor_time') else datetime.now().time().replace(second=0, microsecond=0)
    reading_date = parse_date(payload.get('monitor_date')) if payload.get('monitor_date') else date.today()
    rec = WaterMonitoring(
        monitor_date=reading_date,
        monitor_time=reading_time,
        shift=infer_shift_from_time(reading_time),
        unit_id=unit_id,
        temperature_c=payload.get('temperature_c'),
        dissolved_oxygen=payload.get('dissolved_oxygen'),
        ph=payload.get('ph'),
        salinity=payload.get('salinity'),
        transparency_cm=payload.get('transparency_cm'),
        ammonia=payload.get('ammonia'),
        nitrite=payload.get('nitrite'),
        nitrate=payload.get('nitrate'),
        alkalinity=payload.get('alkalinity'),
        hardness=payload.get('hardness'),
        observation=payload.get('observation'),
    )
    db.session.add(rec)
    db.session.commit()
    config = get_water_reference_config()
    alerts = water_alerts_for_record(rec, config)
    rule_alerts = []
    for rule in AlertRule.query.filter_by(active=True).all():
        value = getattr(rec, rule.parameter_key, None)
        if value is None:
            continue
        if rule.min_value is not None and value < rule.min_value:
            rule_alerts.append({'rule': rule.name, 'message': f'{rule.parameter_key} abaixo do mínimo ({value})'})
        if rule.max_value is not None and value > rule.max_value:
            rule_alerts.append({'rule': rule.name, 'message': f'{rule.parameter_key} acima do máximo ({value})'})
    return jsonify({'ok': True, 'record_id': rec.id, 'alerts': alerts, 'rule_alerts': rule_alerts})


@app.get('/managerial-reports/export/<report_key>.pdf')
@login_required
@requires_permission('dashboard')
def export_managerial_report_pdf(report_key):
    today = date.today()
    if report_key == 'stock':
        headers = ['Categoria', 'Item', 'Saldo', 'Unidade', 'Minimo']
        rows = []
        for row in build_feed_stock_snapshot()['rows']:
            rows.append(['Ração', row.get('name') or row.get('feed_name'), row.get('stock_kg'), 'kg', row.get('minimum_stock_kg')])
        for row in build_supply_stock_snapshot()['rows']:
            rows.append(['Insumo', row['name'], row.get('stock_qty'), row.get('measure_unit'), row.get('minimum_stock_qty')])
        return build_pdf_response('Relatório de estoque', headers, rows, f'relatorio_estoque_{today.strftime("%Y%m%d")}.pdf')
    elif report_key == 'production':
        headers = ['Lote', 'Fornecedor', 'Custo total', 'FCR', 'Sobrevivencia']
        rows = []
        for summary in [lot_financial_summary(lot) for lot in Lot.query.order_by(Lot.start_date.desc()).all()]:
            rows.append([summary['lot'].lot_code, summary['lot'].larva_supplier or '-', summary['total_cost'], summary['fcr_real'], summary['survival_pct']])
        return build_pdf_response('Relatório de produção', headers, rows, f'relatorio_producao_{today.strftime("%Y%m%d")}.pdf')
    elif report_key == 'financial':
        headers = ['Data', 'Tipo', 'Categoria', 'Descrição', 'Valor', 'Status']
        rows = [[(row.due_date or row.entry_date).strftime('%d/%m/%Y') if (row.due_date or row.entry_date) else '', row.entry_type, row.category, row.description, row.amount, row.status] for row in FinanceEntry.query.order_by(FinanceEntry.entry_date.desc()).all()]
        return build_pdf_response('Relatório financeiro', headers, rows, f'relatorio_financeiro_{today.strftime("%Y%m%d")}.pdf')
    elif report_key == 'water_quality':
        config = get_water_reference_config()
        headers = ['Data', 'Unidade', 'OD', 'pH', 'Temp', 'Amônia', 'Alertas']
        rows = []
        for record in WaterMonitoring.query.options(joinedload(WaterMonitoring.unit)).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.id.desc()).limit(150).all():
            rows.append([record.monitor_date.strftime('%d/%m/%Y') if record.monitor_date else '', record.unit.name if record.unit else '', record.dissolved_oxygen, record.ph, record.temperature_c, record.ammonia, '; '.join(alert['message'] for alert in water_alerts_for_record(record, config))])
        return build_pdf_response('Relatório de água', headers, rows, f'relatorio_agua_{today.strftime("%Y%m%d")}.pdf')
    abort(404)


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
