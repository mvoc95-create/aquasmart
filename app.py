from datetime import date, datetime, time, timedelta
import base64
import json
import os
import re
import unicodedata
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
from sqlalchemy import case, func, inspect, text
from sqlalchemy.orm import joinedload
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
import io
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


class FarmDocument(db.Model):
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

    lot = active_lot_for_unit(unit_id)
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
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    dialect = db.engine.dialect.name

    for model in (ProtocolDocument, FarmDocument, WaterReferenceConfig):
        table_name = model.__table__.name
        if table_name not in tables:
            model.__table__.create(bind=db.engine)
            tables.add(table_name)

    if 'user' not in tables:
        return
    columns = {col['name'] for col in inspector.get_columns('user')}

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
            sql = 'ALTER TABLE water_monitoring ADD COLUMN monitor_time TIME'
            with db.engine.begin() as conn:
                conn.execute(text(sql))


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()
        get_water_reference_config()


def active_lot_for_unit(unit_id):
    return Lot.query.filter_by(unit_id=unit_id, status='ativo').order_by(Lot.start_date.desc()).first()


def latest_water(unit_id):
    return WaterMonitoring.query.filter_by(unit_id=unit_id).order_by(WaterMonitoring.monitor_date.desc(), WaterMonitoring.monitor_time.desc(), WaterMonitoring.id.desc()).first()


def latest_mgmt(unit_id):
    return DailyManagement.query.filter_by(unit_id=unit_id).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).first()


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
        'water_alerts': len(water_alert_rows),
        'water_alert_rows': water_alert_rows,
        'nursery_ready': nursery_ready,
        'feed_stock_kg': round(total_stock, 1),
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
        all_movements = FeedInventory.query.order_by(FeedInventory.movement_date.desc(), FeedInventory.id.desc()).all()
        grouped = {}
        for movement in all_movements:
            grouped.setdefault(movement.feed_name, 0)
            grouped[movement.feed_name] += movement.quantity_kg if movement.movement_type == 'entrada' else -movement.quantity_kg
        stock_rows = [{'feed_name': name, 'stock_kg': round(value, 1)} for name, value in sorted(grouped.items())]
        movement_rows = all_movements[:100]
        return render_template('dashboard_detail.html', kind=kind, title='Estoque de ração', subtitle='Saldo consolidado por tipo de ração e últimos movimentos lançados.', metric_value=data['feed_stock_kg'], metric_suffix='kg', rows=stock_rows, movement_rows=movement_rows, today=today)

    if kind == 'feed-coverage':
        recent_management = DailyManagement.query.options(joinedload(DailyManagement.unit), joinedload(DailyManagement.lot)).filter(DailyManagement.manage_date >= today - timedelta(days=7)).order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).all()
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
        reference_config=get_water_reference_config(),
        reference_summary=build_reference_summary(),
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
        return redirect(url_for('management_page', unit_id=unit_id))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    selected_unit_id = request.args.get('unit_id', type=int)
    records_query = DailyManagement.query.join(Unit)
    if selected_unit_id:
        records_query = records_query.filter(DailyManagement.unit_id == selected_unit_id)
    records = records_query.order_by(DailyManagement.manage_date.desc(), DailyManagement.id.desc()).limit(100).all()
    edit_id = request.args.get('edit_id', type=int)
    edit_record = db.session.get(DailyManagement, edit_id) if edit_id else None
    return render_template(
        'management.html',
        units=units,
        records=records,
        today=date.today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
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
        document = FarmDocument(
            title=title,
            category=category,
            notes=notes or None,
            original_filename=safe_name,
            mime_type=uploaded_file.mimetype or 'application/octet-stream',
            file_size=len(file_bytes),
            file_data=file_bytes,
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
        documents_query = documents_query.filter(db.or_(
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
