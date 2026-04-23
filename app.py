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
    end_date = db.Column(db.Date)
    closed_reason = db.Column(db.String(60))
    notes = db.Column(db.Text)
    larva_supplier = db.Column(db.String(120))
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
    intestinal_score = db.Column(db.Integer)
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
        parts = [part.strip() for part in [self.brand, self.feed_type] if part and part.strip()]
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
    feed_name = db.Column(db.String(80), nullable=False)
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


def water_sheet_supported_times(sheet_type: str):
    return ['07:00', '16:00'] if sheet_type == 'day' else ['18:00', '00:00', '02:00', '04:00']


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
    period_label = 'diurna' if sheet_type == 'day' else 'noturna'
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
        "3. Para cada leitura, informe: row_name, time, dissolved_oxygen, temperature_c, ph, ammonia, nitrite.\n"
        "4. Use null para campos em branco.\n"
        "5. Não invente dados.\n"
        "6. Se tiver dúvida em algum número, prefira null.\n"
        "7. Responda no formato: {\"readings\":[...]}\n"
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
    value = str(value).strip()
    if value == '':
        return default
    return int(value)


def sync_nursery_feed_to_management(entry):
    if not entry:
        return
    management = DailyManagement.query.filter_by(manage_date=entry.feed_date, unit_id=entry.unit_id, lot_id=entry.lot_id).order_by(DailyManagement.id.desc()).first()
    if not management:
        management = DailyManagement(manage_date=entry.feed_date, unit_id=entry.unit_id, lot_id=entry.lot_id, feed_offered_kg=entry.quantity_kg or 0, feed_consumed_kg=entry.quantity_kg or 0, notes=(entry.notes or '').strip() or 'Gerado pela alimentação de berçário.')
        db.session.add(management)
    else:
        management.feed_offered_kg = entry.quantity_kg or 0
        management.feed_consumed_kg = entry.quantity_kg or 0
        extra = f'Score intestinal: {entry.intestinal_score}' if entry.intestinal_score is not None else 'Alimentação de berçário atualizada.'
        management.notes = ((management.notes or '') + ('\n' if management.notes else '') + extra).strip()
    management.updated_at = datetime.utcnow()
    return management

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

    for model in (ProtocolDocument, FarmDocument, WaterReferenceConfig, FeedProduct, LotUnitAllocation, FixedCost, NurseryFeeding):
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


    if 'lot_unit_allocation' in tables:
        allocation_columns = get_columns('lot_unit_allocation')
        add_column_if_missing('lot_unit_allocation', allocation_columns, 'quantity_allocated', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER')

    if 'sale' in tables:
        sale_columns = get_columns('sale')
        add_column_if_missing('sale', sale_columns, 'average_weight_g', 'ALTER TABLE sale ADD COLUMN average_weight_g FLOAT', 'ALTER TABLE sale ADD COLUMN average_weight_g DOUBLE PRECISION')
        add_column_if_missing('sale', sale_columns, 'harvested_units', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER')

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
        add_column_if_missing('daily_management', daily_management_columns, 'created_at', f"ALTER TABLE daily_management ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE daily_management ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('daily_management', daily_management_columns, 'updated_at', f"ALTER TABLE daily_management ADD COLUMN updated_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE daily_management ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    if 'feed_inventory' in tables:
        feed_inventory_columns = get_columns('feed_inventory')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'feed_product_id', 'ALTER TABLE feed_inventory ADD COLUMN feed_product_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN feed_product_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'source_type', "ALTER TABLE feed_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'", "ALTER TABLE feed_inventory ADD COLUMN source_type VARCHAR(30) DEFAULT 'manual'")
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'source_ref_id', 'ALTER TABLE feed_inventory ADD COLUMN source_ref_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN source_ref_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'unit_id', 'ALTER TABLE feed_inventory ADD COLUMN unit_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN unit_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'lot_id', 'ALTER TABLE feed_inventory ADD COLUMN lot_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN lot_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'created_by_id', 'ALTER TABLE feed_inventory ADD COLUMN created_by_id INTEGER', 'ALTER TABLE feed_inventory ADD COLUMN created_by_id INTEGER')
        add_column_if_missing('feed_inventory', feed_inventory_columns, 'created_at', f"ALTER TABLE feed_inventory ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE feed_inventory ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    backfill_lot_allocations_and_status()
    sync_feed_products_from_legacy_movements()


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()
        get_water_reference_config()


def active_lot_allocation_for_unit(unit_id, on_date=None):
    on_date = on_date or date.today()
    return (
        LotUnitAllocation.query.options(joinedload(LotUnitAllocation.lot), joinedload(LotUnitAllocation.unit))
        .join(Lot, Lot.id == LotUnitAllocation.lot_id)
        .filter(
            LotUnitAllocation.unit_id == unit_id,
            LotUnitAllocation.start_date <= on_date,
            or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
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
    ).order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc()).first()


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
        'fixed_cost': fixed_cost,
        'total_cost': round((feed_cost or 0) + fixed_cost, 2),
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
    fixed_cost = calculate_fixed_cost_for_allocation(sale.lot, sale.unit_id, sale.lot.start_date, sale.sale_date)
    total_cost = round(feed_cost + fixed_cost, 2)
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
        'fixed_cost': fixed_cost,
        'total_cost': total_cost,
        'revenue': revenue,
        'profit': round(revenue - total_cost, 2),
        'status': 'Lucro' if revenue >= total_cost else 'Prejuízo',
        'harvested_units': harvested_units,
        'survival_pct': survival_pct,
        'fcr_real': fcr_real,
    }


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
    movement.feed_name = feed_product.full_name
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
    total_offered = round(sum(record.feed_offered_kg or 0 for record in records), 1)
    total_cost = round(sum(record.feed_total_cost or 0 for record in records), 2)
    group_map = defaultdict(lambda: {'offered_kg': 0.0, 'cost_total': 0.0, 'records': 0, 'unit_name': '', 'lot_code': ''})
    for record in records:
        key = (record.unit_id, record.lot_id)
        row = group_map[key]
        row['unit_name'] = record.unit.name if record.unit else '—'
        row['lot_code'] = record.lot.lot_code if record.lot else 'Sem lote'
        row['offered_kg'] += record.feed_offered_kg or 0
        row['cost_total'] += record.feed_total_cost or 0
        row['records'] += 1
    grouped_rows = []
    for row in group_map.values():
        row['offered_kg'] = round(row['offered_kg'], 1)
        row['cost_total'] = round(row['cost_total'], 2)
        row['avg_cost_per_kg'] = round((row['cost_total'] / row['offered_kg']), 2) if row['offered_kg'] > 0 else None
        grouped_rows.append(row)
    grouped_rows.sort(key=lambda item: (-item['cost_total'], item['unit_name']))
    return {
        'total_offered_kg': total_offered,
        'total_cost': total_cost,
        'avg_cost_per_kg': round((total_cost / total_offered), 2) if total_offered > 0 else None,
        'grouped_rows': grouped_rows[:20],
    }


def water_status(rec, config=None):
    if not rec:
        return 'sem leitura'
    alerts = water_alerts_for_record(rec, config)
    return ' | '.join(alert['message'] for alert in alerts) if alerts else 'ok'


def dashboard_data():
    today = date.today()
    config = get_water_reference_config()
    units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()

    water_today_records = WaterMonitoring.query.options(joinedload(WaterMonitoring.unit), joinedload(WaterMonitoring.lot)).filter(WaterMonitoring.monitor_date == today).all()
    water_today_unit_ids = {record.unit_id for record in water_today_records}
    mgmt_today_unit_ids = {u for (u,) in db.session.query(DailyManagement.unit_id).filter(DailyManagement.manage_date == today).distinct().all()}
    water_alert_rows = build_water_alert_rows(water_today_records, config)

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
                    nursery_ready.append({
                        'unit_name': unit.name,
                        'lot_code': lot.lot_code,
                        'days': days,
                        'start_date': lot.start_date,
                    })
                    status = 'amarelo'
                    reasons.append('pronto p/ transferência')
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
        semaforo.append({
            'unit': unit,
            'lot': lot,
            'status': status,
            'water': water,
            'mgmt': mgmt,
            'reasons': ', '.join(dict.fromkeys(reasons)),
        })

    feed_snapshot = build_feed_stock_snapshot()
    total_stock = feed_snapshot['total_stock_kg']
    avg_daily_feed = db.session.query(func.coalesce(func.avg(DailyManagement.feed_offered_kg), 0)).filter(
        DailyManagement.manage_date >= today - timedelta(days=7)
    ).scalar() or 0
    feed_coverage = round(total_stock / avg_daily_feed, 1) if avg_daily_feed > 0 else None

    return {
        'today': today,
        'units': units,
        'water_pending': sum(1 for s in semaforo if s['lot'] and s['unit'].id not in water_today_unit_ids),
        'management_pending': sum(1 for s in semaforo if s['lot'] and s['unit'].id not in mgmt_today_unit_ids),
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
        lot.lot_code = (request.form['lot_code'] or '').strip().upper()
        lot.phase = request.form['phase']
        lot.start_date = parse_date(request.form['start_date'])
        lot.unit_id = int(request.form['unit_id'])
        lot.initial_count = int(request.form['initial_count'] or 0)
        lot.estimated_weight_g = parse_float(request.form.get('estimated_weight_g'), 0) or 0
        lot.status = request.form.get('status') or lot.status or 'ativo'
        lot.larva_supplier = (request.form.get('larva_supplier') or '').strip() or None
        lot.notes = request.form.get('notes')
        if lot.status == 'encerrado' and request.form.get('end_date'):
            lot.end_date = parse_date(request.form.get('end_date'))
        if form_mode != 'edit_lot':
            db.session.add(lot)
            db.session.flush()
            db.session.add(LotUnitAllocation(lot_id=lot.id, unit_id=lot.unit_id, start_date=lot.start_date, quantity_allocated=lot.initial_count, notes='Alocação inicial do lote.'))
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
    sheet_type = request.form.get('sheet_type', 'day')
    sheet_date = parse_date(request.form.get('sheet_date'), date.today())

    if sheet_type not in {'day', 'night'}:
        flash('Tipo de ficha inválido.', 'danger')
        return redirect(url_for('water_page'))

    if not upload or not upload.filename:
        flash('Envie a foto da ficha antes de importar.', 'warning')
        return redirect(url_for('water_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    try:
        file_bytes = upload.read()
        readings = extract_water_sheet_data_with_openai(
            file_bytes=file_bytes,
            filename=upload.filename,
            content_type=upload.mimetype,
            sheet_type=sheet_type,
            sheet_date=sheet_date,
            units=units,
        )
        preview_rows, warnings = build_water_import_preview(readings, units, sheet_type, sheet_date)
    except Exception as exc:
        flash(f'Não consegui ler a ficha automaticamente: {exc}', 'danger')
        return redirect(url_for('water_page'))

    if not preview_rows:
        flash('Não encontrei leituras válidas na ficha para montar a prévia.', 'warning')
        return redirect(url_for('water_page'))

    store_pending_water_import(sheet_type, sheet_date, preview_rows, warnings)
    flash('Prévia da importação gerada. Confira os dados antes de confirmar.', 'success')
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
        feed_consumed_kg = parse_float(request.form.get('feed_consumed_kg'), 0) or 0
        if feed_consumed_kg > feed_offered_kg and feed_offered_kg > 0:
            flash('A ração consumida não pode ser maior que a ofertada.', 'danger')
            return redirect(url_for('management_page', unit_id=unit_id))
        validation_error = validate_feed_usage(feed_product, feed_offered_kg)
        if validation_error:
            flash(validation_error, 'danger')
            return redirect(url_for('management_page', unit_id=unit_id))

        rec = DailyManagement(
            manage_date=manage_date,
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            feed_product_id=feed_product.id if feed_product else None,
            feed_offered_kg=feed_offered_kg,
            feed_consumed_kg=feed_consumed_kg,
            mortality_qty=parse_int(request.form.get('mortality_qty'), 0) or 0,
            average_weight_g=parse_float(request.form.get('average_weight_g')),
            estimated_biomass_kg=parse_float(request.form.get('estimated_biomass_kg')),
            notes=request.form.get('notes'),
            updated_at=datetime.utcnow(),
        )
        db.session.add(rec)
        db.session.flush()
        sync_management_feed_movement(rec, feed_product, feed_offered_kg)
        db.session.commit()
        flash('Manejo diário lançado com baixa automática da ração.', 'success')
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
    cost_summary = management_cost_summary(selected_unit_id)
    return render_template(
        'management.html',
        units=units,
        records=records,
        today=date.today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
        feed_products=feed_products,
        stock_by_product=stock_by_product,
        cost_summary=cost_summary,
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
    feed_consumed_kg = parse_float(request.form.get('feed_consumed_kg'), 0) or 0
    existing_movement = get_management_feed_movement(record_id)

    if feed_consumed_kg > feed_offered_kg and feed_offered_kg > 0:
        flash('A ração consumida não pode ser maior que a ofertada.', 'danger')
        return redirect(request.referrer or url_for('management_page'))

    validation_error = validate_feed_usage(feed_product, feed_offered_kg, existing_movement=existing_movement)
    if validation_error:
        flash(validation_error, 'danger')
        return redirect(request.referrer or url_for('management_page'))

    rec.manage_date = new_manage_date
    rec.unit_id = unit_id
    rec.lot_id = lot.id if lot else None
    rec.feed_product_id = feed_product.id if feed_product else None
    rec.feed_offered_kg = feed_offered_kg
    rec.feed_consumed_kg = feed_consumed_kg
    rec.mortality_qty = parse_int(request.form.get('mortality_qty'), 0) or 0
    rec.average_weight_g = parse_float(request.form.get('average_weight_g'))
    rec.estimated_biomass_kg = parse_float(request.form.get('estimated_biomass_kg'))
    rec.notes = request.form.get('notes')
    rec.updated_at = datetime.utcnow()
    sync_management_feed_movement(rec, feed_product, feed_offered_kg, existing_movement=existing_movement)
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
        'feed_consumed': {'group': 'management', 'field': 'feed_consumed_kg', 'label': 'Ração consumida', 'unit': 'kg', 'title': 'Ração consumida x tempo', 'threshold_key': None},
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
        transfer_date = parse_date(request.form['transfer_date'], date.today())
        src_id = int(request.form['source_unit_id'])
        source_lot_id = int(request.form['source_lot_id']) if request.form.get('source_lot_id') else None
        src_lot = db.session.get(Lot, source_lot_id) if source_lot_id else active_lot_for_unit(src_id, on_date=transfer_date)
        if not src_lot:
            flash('Selecione um lote de origem válido.', 'danger')
            return redirect(url_for('transfers_page'))
        destination_unit_id = int(request.form['destination_unit_id'])
        transferred_qty = int(request.form['transferred_qty'])
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
            tr.transferred_qty = transferred_qty
            tr.avg_weight_g = parse_float(request.form.get('avg_weight_g'))
            tr.notes = request.form.get('notes')
            db.session.commit()
            flash('Transferência atualizada.', 'success')
            return redirect(url_for('transfers_page'))
        existing_allocation = LotUnitAllocation.query.filter(LotUnitAllocation.lot_id == src_lot.id, LotUnitAllocation.unit_id == destination_unit_id, LotUnitAllocation.start_date <= transfer_date, or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= transfer_date)).first()
        if not existing_allocation:
            db.session.add(LotUnitAllocation(lot_id=src_lot.id, unit_id=destination_unit_id, start_date=transfer_date, quantity_allocated=transferred_qty, notes='Transferência bifásica.'))
        else:
            existing_allocation.quantity_allocated = (existing_allocation.quantity_allocated or 0) + transferred_qty
        tr = Transfer(transfer_date=transfer_date, source_unit_id=src_id, destination_unit_id=destination_unit_id, source_lot_id=src_lot.id, destination_lot_code=src_lot.lot_code, transferred_qty=transferred_qty, avg_weight_g=parse_float(request.form.get('avg_weight_g')), notes=request.form.get('notes'))
        db.session.add(tr)
        allocation = LotUnitAllocation.query.filter(LotUnitAllocation.lot_id == src_lot.id, LotUnitAllocation.unit_id == src_id, LotUnitAllocation.start_date <= transfer_date, or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= transfer_date)).order_by(LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc()).first()
        if allocation:
            remaining_qty = max((allocation.quantity_allocated or 0) - transferred_qty, 0)
            if request.form.get('close_source_allocation') == '1' or remaining_qty == 0:
                allocation.end_date = transfer_date
            allocation.quantity_allocated = remaining_qty
        db.session.commit()
        flash('Transferência registrada. O mesmo lote agora pode seguir em múltiplos viveiros.', 'success')
        return redirect(url_for('transfers_page'))
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    rows = Transfer.query.order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).limit(50).all()
    return render_template('transfers.html', units=units, lots=lots, rows=rows, today=date.today(), edit_transfer=edit_transfer)


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
        row.feed_name = feed_product.full_name
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


@app.route('/nursery-feed', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def nursery_feed_page():
    edit_id = parse_int(request.args.get('edit_id'))
    edit_entry = db.session.get(NurseryFeeding, edit_id) if edit_id else None
    nursery_units = Unit.query.filter_by(active=True, phase='bercario').order_by(Unit.name).all()
    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        entry = db.session.get(NurseryFeeding, parse_int(request.form.get('entry_id'))) if form_mode == 'edit' else NurseryFeeding()
        if form_mode == 'edit' and not entry:
            flash('Registro de alimentação do berçário não encontrado.', 'warning')
            return redirect(url_for('nursery_feed_page'))
        feed_date = parse_date(request.form.get('feed_date'), date.today())
        unit_id = int(request.form.get('unit_id'))
        lot = active_lot_for_unit(unit_id, on_date=feed_date)
        entry.feed_date = feed_date
        entry.unit_id = unit_id
        entry.lot_id = lot.id if lot else None
        entry.quantity_kg = parse_float(request.form.get('quantity_kg'), 0) or 0
        entry.intestinal_score = parse_int(request.form.get('intestinal_score'))
        entry.notes = request.form.get('notes')
        entry.updated_at = datetime.utcnow()
        if form_mode != 'edit':
            db.session.add(entry)
        db.session.flush()
        sync_nursery_feed_to_management(entry)
        db.session.commit()
        flash('Alimentação de berçário salva com sucesso.', 'success')
        return redirect(url_for('nursery_feed_page'))
    entries = NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot)).order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc()).limit(60).all()
    return render_template('nursery_feed.html', today=date.today(), nursery_units=nursery_units, entries=entries, edit_entry=edit_entry)

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
        'Peso medio g', 'Unidades despescadas', 'Custo racao viveiro', 'Custo fixo viveiro',
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
