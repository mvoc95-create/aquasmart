from datetime import date, datetime, time, timedelta
import base64
import json
import os
import re
import unicodedata
from zoneinfo import ZoneInfo
from collections import defaultdict, OrderedDict
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for, has_app_context
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, case, func, inspect, text, or_
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

# Timezone used by farm operations and on-screen dates/times.
# Render usually runs in UTC, so keeping this explicit avoids a 3-hour offset in Brazil.
APP_TIMEZONE_NAME = os.getenv('APP_TIMEZONE') or os.getenv('TZ') or 'America/Recife'
try:
    APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TIMEZONE_NAME = 'America/Recife'
    APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)


def local_now():
    return datetime.now(APP_TIMEZONE)


def local_today():
    return local_now().date()


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
    # Custo de aquisição das PLs/larvas vinculado ao lote.
    # larva_unit_cost = R$/milheiro; larva_total_cost = custo total já calculado/ajustado.
    larva_unit_cost = db.Column(db.Float)
    larva_total_cost = db.Column(db.Float, default=0)
    unit = db.relationship('Unit')


class LotUnitAllocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    quantity_allocated = db.Column(db.Integer)
    # Fase operacional da alocação.
    # Ex.: uma estufa cadastrada como "engorda" pode receber um lote como Juvenil.
    # A alimentação e o mapa de saldos devem seguir esta fase da transferência, não apenas
    # a fase fixa cadastrada na unidade física.
    operational_phase = db.Column(db.String(30))
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
    # Ajuste incremental informado no lançamento: +10 aplica 10% sobre a correção ativa;
    # 0 zera a correção; vazio mantém o fator ativo anterior.
    score_adjustment_pct = db.Column(db.Float)
    # Fator de correção que fica ativo após este lançamento. Ex.: 1.10, 1.21 etc.
    active_feed_factor = db.Column(db.Float)
    # JSON com os aditivos/insumos de água marcados no lançamento do berçário.
    # Mantém o histórico do que foi realmente utilizado para refazer Manejo + Estoque.
    water_items_json = db.Column(db.Text)
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




class OperationalTask(db.Model):
    """Agenda operacional diária para o Painel TV e para a aba Rotina do Dia."""
    id = db.Column(db.Integer, primary_key=True)
    operation_date = db.Column(db.Date, nullable=False)
    scheduled_time = db.Column(db.Time)
    category = db.Column(db.String(30), nullable=False, default='rotina')
    priority = db.Column(db.String(20), nullable=False, default='media')
    priority_order = db.Column(db.Integer, nullable=False, default=3)
    title = db.Column(db.String(160), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey('unit.id'))
    feed_product_id = db.Column(db.Integer, db.ForeignKey('feed_product.id'))
    supply_product_id = db.Column(db.Integer, db.ForeignKey('supply_product.id'))
    ration_label = db.Column(db.String(120))
    quantity = db.Column(db.Float)
    measure_unit = db.Column(db.String(20), default='kg')
    frequency = db.Column(db.String(40))
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    unit = db.relationship('Unit')
    feed_product = db.relationship('FeedProduct')
    supply_product = db.relationship('SupplyProduct')

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
    close_source_after_transfer = db.Column(db.Boolean, nullable=False, default=False)
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


class FeedingProtocolRow(db.Model):
    """Linha editável da tabela base de alimentação.

    A tabela padrão continua vindo do PDF enviado, mas é materializada no banco para
    que o operador possa alterar taxa, peso e mix sem mexer no código.
    """
    id = db.Column(db.Integer, primary_key=True)
    protocol_key = db.Column(db.String(40), nullable=False, default='full_cycle')
    phase = db.Column(db.String(30), nullable=False, index=True)  # bercario / juvenil / engorda
    phase_day = db.Column(db.Integer, nullable=False, index=True)
    cycle_day = db.Column(db.Integer)
    stage_label = db.Column(db.String(50))
    population = db.Column(db.Integer, nullable=False, default=0)
    survival_pct = db.Column(db.Float, nullable=False, default=100)
    individual_weight_g = db.Column(db.Float, nullable=False, default=0)
    biomass_kg = db.Column(db.Float, nullable=False, default=0)
    feed_rate_pct = db.Column(db.Float, nullable=False, default=0)
    total_day_g = db.Column(db.Integer, nullable=False, default=0)
    feedings_per_day = db.Column(db.Integer, nullable=False, default=8)
    water_items_json = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    feeds = db.relationship('FeedingProtocolFeed', backref='row', cascade='all, delete-orphan', lazy=True)


class FeedingProtocolFeed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    row_id = db.Column(db.Integer, db.ForeignKey('feeding_protocol_row.id'), nullable=False, index=True)
    protocol_label = db.Column(db.String(160), nullable=False, index=True)
    grams = db.Column(db.Integer, nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class FeedingProtocolFeedMap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    protocol_label = db.Column(db.String(160), unique=True, nullable=False, index=True)
    feed_product_id = db.Column(db.Integer, db.ForeignKey('feed_product.id'))
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    feed_product = db.relationship('FeedProduct')


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


VALID_OPERATIONAL_PHASES = {'bercario', 'juvenil', 'engorda'}


def normalize_phase_value(value):
    text = (value or '').strip().lower()
    text = text.replace('ç', 'c').replace('á', 'a').replace('ã', 'a').replace('é', 'e')
    if text in VALID_OPERATIONAL_PHASES:
        return text
    return None


def allocation_operational_phase(allocation):
    if not allocation:
        return None
    explicit_phase = normalize_phase_value(getattr(allocation, 'operational_phase', None))
    if explicit_phase:
        return explicit_phase
    unit = getattr(allocation, 'unit', None)
    return normalize_phase_value(getattr(unit, 'phase', None))


def feeding_entry_operational_phase(entry):
    if not entry:
        return None
    allocation = find_active_allocation(entry.lot_id, entry.unit_id, entry.feed_date) if entry.lot_id and entry.unit_id and entry.feed_date else None
    phase = allocation_operational_phase(allocation)
    if phase:
        return phase
    unit = entry.unit if getattr(entry, 'unit', None) else (db.session.get(Unit, entry.unit_id) if entry.unit_id else None)
    return normalize_phase_value(getattr(unit, 'phase', None))


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
        'today_label': local_today().strftime('%d/%m/%Y'),
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


def parse_sheet_date(value, default=None):
    """Aceita datas vindas da IA em ISO ou no padrão brasileiro da ficha."""
    if not value:
        return default
    raw = str(value).strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return default


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
        "5. Para cada leitura, informe: row_name, time, dissolved_oxygen, temperature_c, transparency_cm.\n"
        "6. Use null para campos em branco. Não extraia pH, TAN, nitrito, nitrato, alcalinidade ou dureza nesta aba.\n"
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
            'transparency_cm': parse_float(item.get('transparency_cm')) if item.get('transparency_cm') is not None else None,
        }
        if all(values.get(field) is None for field in ['dissolved_oxygen', 'temperature_c', 'transparency_cm']):
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
            'transparency_cm': values['transparency_cm'],
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
    for field in (
        'temperature_c', 'dissolved_oxygen', 'ph', 'salinity', 'transparency_cm',
        'ammonia', 'nitrite', 'nitrate', 'alkalinity', 'hardness'
    ):
        if field in values:
            setattr(record, field, values.get(field))
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
    # A leitura por IA às vezes devolve valores como "<0.25" ou "0.25 mg/L".
    # Mantemos apenas o número para evitar erro no lançamento.
    value = re.sub(r'[^0-9.\-]+', '', value)
    if value in {'', '-', '.', '-.'}:
        return default
    return float(value)


def parse_int(value, default=None):
    if value is None:
        return default
    value = str(value).strip().replace(',', '.')
    if value == '':
        return default
    return int(float(value))


# Protocolos de alimentação importados da planilha "PROTOCOLOS Alimentacao.xlsx".
# SP1 = PROTOCOLO DE ALIMENTAÇÃO SÃO PAULO; RG1 = PROTOCOLO DE ALIMENTAÇÃO RIO GRANDE DO SUL.
# As rações trituradas foram vinculadas ao estoque real conforme orientação:
# - Triturada 500-900 -> AQUAVITA 40#1
# - Triturada 800-1200 -> AQUAVITA 40#2 (equivalente da SAMARIA 40#2)
# - Probiótico pastilha -> AQUAPRO ECO
NURSERY_PROTOCOLS = {'sp1': {'name': 'PROTOCOLO DE ALIMENTAÇÃO SÃO PAULO',
         'sheet_name': 'Protocolo de Alimentacao SP1 -',
         'base_population': 265000,
         'rows': [{'pl_stage': 11,
                   'day': 1,
                   'population': 265000,
                   'survival_pct': 100.0,
                   'individual_weight_g': 0.003003,
                   'daily_growth_g': None,
                   'feed_rate_pct': 22.0,
                   'total_day_g': 175.0751,
                   'feedings_per_day': 12,
                   'per_feeding_g': 14.5896,
                   'biomass_kg': 0.7958,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 175.0751}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 4.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 12,
                   'day': 2,
                   'population': 262350,
                   'survival_pct': 99.0,
                   'individual_weight_g': 0.004,
                   'daily_growth_g': 0.000997,
                   'feed_rate_pct': 21.0,
                   'total_day_g': 220.374,
                   'feedings_per_day': 12,
                   'per_feeding_g': 18.3645,
                   'biomass_kg': 1.0494,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 220.374}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 13,
                   'day': 3,
                   'population': 259700,
                   'survival_pct': 98.0,
                   'individual_weight_g': 0.005848,
                   'daily_growth_g': 0.001848,
                   'feed_rate_pct': 20.0,
                   'total_day_g': 303.7427,
                   'feedings_per_day': 12,
                   'per_feeding_g': 25.3119,
                   'biomass_kg': 1.5187,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 227.807},
                             {'label': 'NutriSphera 450', 'grams': 75.9357}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 300.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 14,
                   'day': 4,
                   'population': 258375,
                   'survival_pct': 97.5,
                   'individual_weight_g': 0.00885,
                   'daily_growth_g': 0.003002,
                   'feed_rate_pct': 19.0,
                   'total_day_g': 434.4358,
                   'feedings_per_day': 12,
                   'per_feeding_g': 36.203,
                   'biomass_kg': 2.2865,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 217.2179},
                             {'label': 'NutriSphera 450', 'grams': 217.2179}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 400.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 15,
                   'day': 5,
                   'population': 257050,
                   'survival_pct': 97.0,
                   'individual_weight_g': 0.012821,
                   'daily_growth_g': 0.003971,
                   'feed_rate_pct': 18.0,
                   'total_day_g': 593.1923,
                   'feedings_per_day': 12,
                   'per_feeding_g': 49.4327,
                   'biomass_kg': 3.2955,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 148.2981},
                             {'label': 'NutriSphera 450', 'grams': 444.8942}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 16,
                   'day': 6,
                   'population': 256388,
                   'survival_pct': 96.75,
                   'individual_weight_g': 0.016949,
                   'daily_growth_g': 0.004129,
                   'feed_rate_pct': 17.0,
                   'total_day_g': 738.7436,
                   'feedings_per_day': 12,
                   'per_feeding_g': 61.562,
                   'biomass_kg': 4.3456,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 738.7436}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 600.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 17,
                   'day': 7,
                   'population': 255725,
                   'survival_pct': 96.5,
                   'individual_weight_g': 0.021739,
                   'daily_growth_g': 0.00479,
                   'feed_rate_pct': 16.0,
                   'total_day_g': 889.4783,
                   'feedings_per_day': 12,
                   'per_feeding_g': 74.1232,
                   'biomass_kg': 5.5592,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 889.4783}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 700.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 18,
                   'day': 8,
                   'population': 255062,
                   'survival_pct': 96.25,
                   'individual_weight_g': 0.027778,
                   'daily_growth_g': 0.006039,
                   'feed_rate_pct': 15.0,
                   'total_day_g': 1062.7604,
                   'feedings_per_day': 12,
                   'per_feeding_g': 88.5634,
                   'biomass_kg': 7.0851,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1062.7604}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 800.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 19,
                   'day': 9,
                   'population': 254400,
                   'survival_pct': 96.0,
                   'individual_weight_g': 0.034483,
                   'daily_growth_g': 0.006705,
                   'feed_rate_pct': 14.0,
                   'total_day_g': 1228.1379,
                   'feedings_per_day': 12,
                   'per_feeding_g': 102.3448,
                   'biomass_kg': 8.7724,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1228.1379}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 900.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 20,
                   'day': 10,
                   'population': 253738,
                   'survival_pct': 95.75,
                   'individual_weight_g': 0.043478,
                   'daily_growth_g': 0.008996,
                   'feed_rate_pct': 13.0,
                   'total_day_g': 1434.1685,
                   'feedings_per_day': 12,
                   'per_feeding_g': 119.514,
                   'biomass_kg': 11.0321,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1434.1685}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 21,
                   'day': 11,
                   'population': 253075,
                   'survival_pct': 95.5,
                   'individual_weight_g': 0.052632,
                   'daily_growth_g': 0.009153,
                   'feed_rate_pct': 12.0,
                   'total_day_g': 1598.3684,
                   'feedings_per_day': 12,
                   'per_feeding_g': 133.1974,
                   'biomass_kg': 13.3197,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1598.3684}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 22,
                   'day': 12,
                   'population': 252412,
                   'survival_pct': 95.25,
                   'individual_weight_g': 0.0625,
                   'daily_growth_g': 0.009868,
                   'feed_rate_pct': 11.0,
                   'total_day_g': 1735.3359,
                   'feedings_per_day': 12,
                   'per_feeding_g': 144.6113,
                   'biomass_kg': 15.7758,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1735.3359}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 23,
                   'day': 13,
                   'population': 251750,
                   'survival_pct': 95.0,
                   'individual_weight_g': 0.071429,
                   'daily_growth_g': 0.008929,
                   'feed_rate_pct': 10.0,
                   'total_day_g': 1798.2143,
                   'feedings_per_day': 12,
                   'per_feeding_g': 149.8512,
                   'biomass_kg': 17.9821,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1798.2143}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 24,
                   'day': 14,
                   'population': 251088,
                   'survival_pct': 94.75,
                   'individual_weight_g': 0.083333,
                   'daily_growth_g': 0.011905,
                   'feed_rate_pct': 9.0,
                   'total_day_g': 1883.1562,
                   'feedings_per_day': 12,
                   'per_feeding_g': 156.9297,
                   'biomass_kg': 20.924,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1412.3672},
                             {'label': 'AQUAVITA 40#1', 'grams': 470.789}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 25,
                   'day': 15,
                   'population': 250425,
                   'survival_pct': 94.5,
                   'individual_weight_g': 0.13,
                   'daily_growth_g': 0.046667,
                   'feed_rate_pct': 8.0,
                   'total_day_g': 2604.42,
                   'feedings_per_day': 12,
                   'per_feeding_g': 217.035,
                   'biomass_kg': 32.5553,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1302.21},
                             {'label': 'AQUAVITA 40#1', 'grams': 1302.21}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 26,
                   'day': 16,
                   'population': 249762,
                   'survival_pct': 94.25,
                   'individual_weight_g': 0.18,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 8.5,
                   'total_day_g': 3821.3663,
                   'feedings_per_day': 12,
                   'per_feeding_g': 318.4472,
                   'biomass_kg': 44.9573,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 955.3416},
                             {'label': 'AQUAVITA 40#1', 'grams': 2866.0247}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 27,
                   'day': 17,
                   'population': 249100,
                   'survival_pct': 94.0,
                   'individual_weight_g': 0.23,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.0,
                   'total_day_g': 2864.65,
                   'feedings_per_day': 12,
                   'per_feeding_g': 238.7208,
                   'biomass_kg': 57.293,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 477.4417},
                             {'label': 'AQUAVITA 40#1', 'grams': 2387.2083}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 28,
                   'day': 18,
                   'population': 248438,
                   'survival_pct': 93.75,
                   'individual_weight_g': 0.28,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.0,
                   'total_day_g': 4173.75,
                   'feedings_per_day': 12,
                   'per_feeding_g': 347.8125,
                   'biomass_kg': 69.5625,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 695.625},
                             {'label': 'AQUAVITA 40#1', 'grams': 3478.125}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 29,
                   'day': 19,
                   'population': 247775,
                   'survival_pct': 93.5,
                   'individual_weight_g': 0.33,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 7.0,
                   'total_day_g': 5723.6025,
                   'feedings_per_day': 12,
                   'per_feeding_g': 476.9669,
                   'biomass_kg': 81.7657,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 953.9338},
                             {'label': 'AQUAVITA 40#1', 'grams': 4769.6687}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 30,
                   'day': 20,
                   'population': 247112,
                   'survival_pct': 93.25,
                   'individual_weight_g': 0.38,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.5,
                   'total_day_g': 6103.6788,
                   'feedings_per_day': 12,
                   'per_feeding_g': 508.6399,
                   'biomass_kg': 93.9027,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6103.6788}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 31,
                   'day': 21,
                   'population': 246450,
                   'survival_pct': 93.0,
                   'individual_weight_g': 0.43,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.0,
                   'total_day_g': 6358.41,
                   'feedings_per_day': 12,
                   'per_feeding_g': 529.8675,
                   'biomass_kg': 105.9735,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6358.41}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 32,
                   'day': 22,
                   'population': 245788,
                   'survival_pct': 92.75,
                   'individual_weight_g': 0.48,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.5,
                   'total_day_g': 6488.79,
                   'feedings_per_day': 12,
                   'per_feeding_g': 540.7325,
                   'biomass_kg': 117.978,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6488.79}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 33,
                   'day': 23,
                   'population': 245125,
                   'survival_pct': 92.5,
                   'individual_weight_g': 0.52,
                   'daily_growth_g': 0.04,
                   'feed_rate_pct': 5.25,
                   'total_day_g': 6691.9125,
                   'feedings_per_day': 12,
                   'per_feeding_g': 557.6594,
                   'biomass_kg': 127.465,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6691.9125}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 34,
                   'day': 24,
                   'population': 244462,
                   'survival_pct': 92.25,
                   'individual_weight_g': 0.57,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.0,
                   'total_day_g': 6967.1812,
                   'feedings_per_day': 12,
                   'per_feeding_g': 580.5984,
                   'biomass_kg': 139.3436,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6967.1812}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 35,
                   'day': 25,
                   'population': 243800,
                   'survival_pct': 92.0,
                   'individual_weight_g': 0.62,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 7179.91,
                   'feedings_per_day': 12,
                   'per_feeding_g': 598.3258,
                   'biomass_kg': 151.156,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 5384.9325},
                             {'label': 'AQUAVITA 40#2', 'grams': 1794.9775}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 36,
                   'day': 26,
                   'population': 243138,
                   'survival_pct': 91.75,
                   'individual_weight_g': 0.67,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 7737.8509,
                   'feedings_per_day': 12,
                   'per_feeding_g': 644.8209,
                   'biomass_kg': 162.9021,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 3868.9255},
                             {'label': 'AQUAVITA 40#2', 'grams': 3868.9255}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 37,
                   'day': 27,
                   'population': 242475,
                   'survival_pct': 91.5,
                   'individual_weight_g': 0.72,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 8292.645,
                   'feedings_per_day': 12,
                   'per_feeding_g': 691.0538,
                   'biomass_kg': 174.582,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 2073.1613},
                             {'label': 'AQUAVITA 40#2', 'grams': 6219.4838}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 38,
                   'day': 28,
                   'population': 241812,
                   'survival_pct': 91.25,
                   'individual_weight_g': 0.77,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 8844.2922,
                   'feedings_per_day': 12,
                   'per_feeding_g': 737.0243,
                   'biomass_kg': 186.1956,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 8844.2922}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 39,
                   'day': 29,
                   'population': 241150,
                   'survival_pct': 91.0,
                   'individual_weight_g': 0.82,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 9392.7925,
                   'feedings_per_day': 12,
                   'per_feeding_g': 782.7327,
                   'biomass_kg': 197.743,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 9392.7925}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 40,
                   'day': 30,
                   'population': 240488,
                   'survival_pct': 90.75,
                   'individual_weight_g': 0.87,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 9938.1459,
                   'feedings_per_day': 12,
                   'per_feeding_g': 828.1788,
                   'biomass_kg': 209.2241,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 9938.1459}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 41,
                   'day': 30,
                   'population': 239825,
                   'survival_pct': 90.5,
                   'individual_weight_g': 0.91,
                   'daily_growth_g': 0.04,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 10366.4356,
                   'feedings_per_day': 12,
                   'per_feeding_g': 863.8696,
                   'biomass_kg': 218.2407,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 10366.4356}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 42,
                   'day': 30,
                   'population': 239162,
                   'survival_pct': 90.25,
                   'individual_weight_g': 0.96,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 10905.81,
                   'feedings_per_day': 12,
                   'per_feeding_g': 908.8175,
                   'biomass_kg': 229.596,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 10905.81}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 43,
                   'day': 30,
                   'population': 238500,
                   'survival_pct': 90.0,
                   'individual_weight_g': 1.01,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.75,
                   'total_day_g': 11442.0375,
                   'feedings_per_day': 12,
                   'per_feeding_g': 953.5031,
                   'biomass_kg': 240.885,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 11442.0375}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]}]},
 'rg1': {'name': 'PROTOCOLO DE ALIMENTAÇÃO RIO GRANDE DO SUL',
         'sheet_name': 'Protocolo de Alimentacao RG1 -',
         'base_population': 288000,
         'rows': [{'pl_stage': 13,
                   'day': 1,
                   'population': 288000,
                   'survival_pct': 100.0,
                   'individual_weight_g': 0.003003,
                   'daily_growth_g': None,
                   'feed_rate_pct': 25.0,
                   'total_day_g': 216.2162,
                   'feedings_per_day': 12,
                   'per_feeding_g': 18.018,
                   'biomass_kg': 0.8649,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 216.2162}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 4.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 14,
                   'day': 2,
                   'population': 285120,
                   'survival_pct': 99.0,
                   'individual_weight_g': 0.004,
                   'daily_growth_g': 0.000997,
                   'feed_rate_pct': 23.75,
                   'total_day_g': 270.864,
                   'feedings_per_day': 12,
                   'per_feeding_g': 22.572,
                   'biomass_kg': 1.1405,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 270.864}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 15,
                   'day': 3,
                   'population': 282240,
                   'survival_pct': 98.0,
                   'individual_weight_g': 0.005848,
                   'daily_growth_g': 0.001848,
                   'feed_rate_pct': 22.5625,
                   'total_day_g': 372.4,
                   'feedings_per_day': 12,
                   'per_feeding_g': 31.0333,
                   'biomass_kg': 1.6505,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 372.4}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 300.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 16,
                   'day': 4,
                   'population': 280800,
                   'survival_pct': 97.5,
                   'individual_weight_g': 0.00885,
                   'daily_growth_g': 0.003002,
                   'feed_rate_pct': 21.4344,
                   'total_day_g': 532.6347,
                   'feedings_per_day': 12,
                   'per_feeding_g': 44.3862,
                   'biomass_kg': 2.485,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 532.6347}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 400.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 17,
                   'day': 5,
                   'population': 279360,
                   'survival_pct': 97.0,
                   'individual_weight_g': 0.012821,
                   'daily_growth_g': 0.003971,
                   'feed_rate_pct': 20.3627,
                   'total_day_g': 729.2964,
                   'feedings_per_day': 12,
                   'per_feeding_g': 60.7747,
                   'biomass_kg': 3.5815,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 729.2964}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 18,
                   'day': 6,
                   'population': 278640,
                   'survival_pct': 96.75,
                   'individual_weight_g': 0.016949,
                   'daily_growth_g': 0.004129,
                   'feed_rate_pct': 19.3445,
                   'total_day_g': 913.5861,
                   'feedings_per_day': 12,
                   'per_feeding_g': 76.1322,
                   'biomass_kg': 4.7227,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 913.5861}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 600.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 19,
                   'day': 7,
                   'population': 277920,
                   'survival_pct': 96.5,
                   'individual_weight_g': 0.021739,
                   'daily_growth_g': 0.00479,
                   'feed_rate_pct': 18.3773,
                   'total_day_g': 1110.3084,
                   'feedings_per_day': 12,
                   'per_feeding_g': 92.5257,
                   'biomass_kg': 6.0417,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 1110.3084}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 700.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 20,
                   'day': 8,
                   'population': 277200,
                   'survival_pct': 96.25,
                   'individual_weight_g': 0.027778,
                   'daily_growth_g': 0.006039,
                   'feed_rate_pct': 17.4584,
                   'total_day_g': 1344.2993,
                   'feedings_per_day': 12,
                   'per_feeding_g': 112.0249,
                   'biomass_kg': 7.7,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 1344.2993}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 800.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 21,
                   'day': 9,
                   'population': 276480,
                   'survival_pct': 96.0,
                   'individual_weight_g': 0.034483,
                   'daily_growth_g': 0.006705,
                   'feed_rate_pct': 16.5855,
                   'total_day_g': 1581.2283,
                   'feedings_per_day': 12,
                   'per_feeding_g': 131.769,
                   'biomass_kg': 9.5338,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 1185.9212},
                             {'label': 'NutriSphera 450', 'grams': 395.3071}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 900.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'},
                                   {'label': 'Análise de trato',
                                    'source_label': 'Análise de trato',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:00',
                                    'priority': 'media'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 22,
                   'day': 10,
                   'population': 275760,
                   'survival_pct': 95.75,
                   'individual_weight_g': 0.043478,
                   'daily_growth_g': 0.008996,
                   'feed_rate_pct': 15.7562,
                   'total_day_g': 1889.1041,
                   'feedings_per_day': 12,
                   'per_feeding_g': 157.4253,
                   'biomass_kg': 11.9896,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 944.5521},
                             {'label': 'NutriSphera 450', 'grams': 944.5521}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 23,
                   'day': 11,
                   'population': 275040,
                   'survival_pct': 95.5,
                   'individual_weight_g': 0.052632,
                   'daily_growth_g': 0.009153,
                   'feed_rate_pct': 14.9684,
                   'total_day_g': 2166.7975,
                   'feedings_per_day': 12,
                   'per_feeding_g': 180.5665,
                   'biomass_kg': 14.4758,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 225', 'grams': 541.6994},
                             {'label': 'NutriSphera 450', 'grams': 1625.0981}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 24,
                   'day': 12,
                   'population': 274320,
                   'survival_pct': 95.25,
                   'individual_weight_g': 0.0625,
                   'daily_growth_g': 0.009868,
                   'feed_rate_pct': 14.22,
                   'total_day_g': 2438.0194,
                   'feedings_per_day': 12,
                   'per_feeding_g': 203.1683,
                   'biomass_kg': 17.145,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 2438.0194}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 25,
                   'day': 13,
                   'population': 273600,
                   'survival_pct': 95.0,
                   'individual_weight_g': 0.071429,
                   'daily_growth_g': 0.008929,
                   'feed_rate_pct': 13.509,
                   'total_day_g': 2640.045,
                   'feedings_per_day': 12,
                   'per_feeding_g': 220.0037,
                   'biomass_kg': 19.5429,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 2640.045}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 26,
                   'day': 14,
                   'population': 272880,
                   'survival_pct': 94.75,
                   'individual_weight_g': 0.083333,
                   'daily_growth_g': 0.011905,
                   'feed_rate_pct': 12.8336,
                   'total_day_g': 2918.3497,
                   'feedings_per_day': 12,
                   'per_feeding_g': 243.1958,
                   'biomass_kg': 22.74,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 2188.7623},
                             {'label': 'AQUAVITA 40#1', 'grams': 729.5874}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 27,
                   'day': 15,
                   'population': 272160,
                   'survival_pct': 94.5,
                   'individual_weight_g': 0.13,
                   'daily_growth_g': 0.046667,
                   'feed_rate_pct': 12.1919,
                   'total_day_g': 4313.5827,
                   'feedings_per_day': 12,
                   'per_feeding_g': 359.4652,
                   'biomass_kg': 35.3808,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 2156.7914},
                             {'label': 'AQUAVITA 40#1', 'grams': 2156.7914}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 28,
                   'day': 16,
                   'population': 271440,
                   'survival_pct': 94.25,
                   'individual_weight_g': 0.18,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 11.5823,
                   'total_day_g': 5659.0097,
                   'feedings_per_day': 12,
                   'per_feeding_g': 471.5841,
                   'biomass_kg': 48.8592,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'NutriSphera 450', 'grams': 1414.7524},
                             {'label': 'AQUAVITA 40#1', 'grams': 4244.2573}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 29,
                   'day': 17,
                   'population': 270720,
                   'survival_pct': 94.0,
                   'individual_weight_g': 0.23,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 11.0032,
                   'total_day_g': 6851.1878,
                   'feedings_per_day': 12,
                   'per_feeding_g': 570.9323,
                   'biomass_kg': 62.2656,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6851.1878}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 30,
                   'day': 18,
                   'population': 270000,
                   'survival_pct': 93.75,
                   'individual_weight_g': 0.28,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 10.453,
                   'total_day_g': 7902.4743,
                   'feedings_per_day': 12,
                   'per_feeding_g': 658.5395,
                   'biomass_kg': 75.6,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 7902.4743}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 31,
                   'day': 19,
                   'population': 269280,
                   'survival_pct': 93.5,
                   'individual_weight_g': 0.33,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 9.9304,
                   'total_day_g': 8824.3544,
                   'feedings_per_day': 12,
                   'per_feeding_g': 735.3629,
                   'biomass_kg': 88.8624,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 8824.3544}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 32,
                   'day': 20,
                   'population': 268560,
                   'survival_pct': 93.25,
                   'individual_weight_g': 0.38,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 9.4338,
                   'total_day_g': 9627.4979,
                   'feedings_per_day': 12,
                   'per_feeding_g': 802.2915,
                   'biomass_kg': 102.0528,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 9627.4979}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 33,
                   'day': 21,
                   'population': 267840,
                   'survival_pct': 93.0,
                   'individual_weight_g': 0.43,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 8.9621,
                   'total_day_g': 10321.8135,
                   'feedings_per_day': 12,
                   'per_feeding_g': 860.1511,
                   'biomass_kg': 115.1712,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 10321.8135}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 34,
                   'day': 22,
                   'population': 267120,
                   'survival_pct': 92.75,
                   'individual_weight_g': 0.48,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 8.514,
                   'total_day_g': 10916.4986,
                   'feedings_per_day': 12,
                   'per_feeding_g': 909.7082,
                   'biomass_kg': 128.2176,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 10916.4986}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'}]},
                  {'pl_stage': 35,
                   'day': 23,
                   'population': 266400,
                   'survival_pct': 92.5,
                   'individual_weight_g': 0.52,
                   'daily_growth_g': 0.04,
                   'feed_rate_pct': 8.0883,
                   'total_day_g': 11204.6137,
                   'feedings_per_day': 12,
                   'per_feeding_g': 933.7178,
                   'biomass_kg': 138.528,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 11204.6137}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 36,
                   'day': 24,
                   'population': 265680,
                   'survival_pct': 92.25,
                   'individual_weight_g': 0.57,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 7.6839,
                   'total_day_g': 11636.3466,
                   'feedings_per_day': 12,
                   'per_feeding_g': 969.6955,
                   'biomass_kg': 151.4376,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 11636.3466}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 37,
                   'day': 25,
                   'population': 264960,
                   'survival_pct': 92.0,
                   'individual_weight_g': 0.62,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 7.2997,
                   'total_day_g': 11991.6388,
                   'feedings_per_day': 12,
                   'per_feeding_g': 999.3032,
                   'biomass_kg': 164.2752,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 8993.7291},
                             {'label': 'AQUAVITA 40#2', 'grams': 2997.9097}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 38,
                   'day': 26,
                   'population': 264240,
                   'survival_pct': 91.75,
                   'individual_weight_g': 0.67,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.9347,
                   'total_day_g': 12277.318,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1023.1098,
                   'biomass_kg': 177.0408,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 6138.659},
                             {'label': 'AQUAVITA 40#2', 'grams': 6138.659}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 39,
                   'day': 27,
                   'population': 263520,
                   'survival_pct': 91.5,
                   'individual_weight_g': 0.72,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.588,
                   'total_day_g': 12499.7068,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1041.6422,
                   'biomass_kg': 189.7344,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#1', 'grams': 3124.9267},
                             {'label': 'AQUAVITA 40#2', 'grams': 9374.7801}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 40,
                   'day': 28,
                   'population': 262800,
                   'survival_pct': 91.25,
                   'individual_weight_g': 0.77,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 6.2586,
                   'total_day_g': 12664.6572,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1055.3881,
                   'biomass_kg': 202.356,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12664.6572}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 41,
                   'day': 29,
                   'population': 262080,
                   'survival_pct': 91.0,
                   'individual_weight_g': 0.82,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.9457,
                   'total_day_g': 12777.5824,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1064.7985,
                   'biomass_kg': 214.9056,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12777.5824}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Calda iodada',
                                    'source_label': 'Calda iodada',
                                    'category': 'aditivo',
                                    'quantity': 24.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '08:30',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]},
                  {'pl_stage': 42,
                   'day': 30,
                   'population': 261360,
                   'survival_pct': 90.75,
                   'individual_weight_g': 0.87,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.6484,
                   'total_day_g': 12843.4866,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1070.2905,
                   'biomass_kg': 227.3832,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12843.4866}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'AQUAPRO ECO',
                                    'source_label': 'Probiótico pastilha',
                                    'category': 'aditivo',
                                    'quantity': 2.0,
                                    'measure_unit': 'un',
                                    'scheduled_time': '08:00',
                                    'priority': 'alta'}]},
                  {'pl_stage': 43,
                   'day': 30,
                   'population': 260640,
                   'survival_pct': 90.5,
                   'individual_weight_g': 0.91,
                   'daily_growth_g': 0.04,
                   'feed_rate_pct': 5.366,
                   'total_day_g': 12727.1343,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1060.5945,
                   'biomass_kg': 237.1824,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12727.1343}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 44,
                   'day': 30,
                   'population': 259920,
                   'survival_pct': 90.25,
                   'individual_weight_g': 0.96,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 5.0977,
                   'total_day_g': 12719.8709,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1059.9892,
                   'biomass_kg': 249.5232,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12719.8709}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'}]},
                  {'pl_stage': 45,
                   'day': 30,
                   'population': 259200,
                   'survival_pct': 90.0,
                   'individual_weight_g': 1.01,
                   'daily_growth_g': 0.05,
                   'feed_rate_pct': 4.8428,
                   'total_day_g': 12678.0292,
                   'feedings_per_day': 12,
                   'per_feeding_g': 1056.5024,
                   'biomass_kg': 261.792,
                   'estimated_fcr': None,
                   'mixes': [{'label': 'AQUAVITA 40#2', 'grams': 12678.0292}],
                   'water_items': [{'label': 'Melaço',
                                    'source_label': 'Melaço (gr)',
                                    'category': 'aditivo',
                                    'quantity': 1000.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:30',
                                    'priority': 'alta'},
                                   {'label': 'LOTHAR',
                                    'source_label': 'LOTHAR (gr)',
                                    'category': 'aditivo',
                                    'quantity': 500.0,
                                    'measure_unit': 'g',
                                    'scheduled_time': '07:45',
                                    'priority': 'alta'},
                                   {'label': 'Biometria',
                                    'source_label': 'Biometria',
                                    'category': 'rotina',
                                    'quantity': None,
                                    'measure_unit': 'un',
                                    'scheduled_time': '09:30',
                                    'priority': 'media'}]}]}}


# Novo padrão operacional informado pela fazenda: Protocolo Alimentação Berçário e Pré-Engorda RG1 — Lote 2 Aquatec.
# Ele substitui o RG1 antigo como referência padrão para berçário e juvenil.
def build_rg1_lote2_aquatec_rows():
    # Mantém o início da tabela antiga antes do primeiro estágio do novo padrão (PL13).
    # Essas linhas ficam como pré-berçário/arranque para casos em que o lote entra como PL10, PL11 ou PL12.
    # A partir de PL13 o sistema passa a usar exatamente o padrão novo RG1 Lote 2 Aquatec.
    legacy_rows = [
        {
            'pl_stage': 10,
            'day': 1,
            'population': 160000,
            'base_population': 160000,
            'survival_pct': 100.0,
            'individual_weight_g': 0.002,
            'daily_growth_g': None,
            'feed_rate_pct': 35.0,
            'total_day_g': 110,
            'feedings_per_day': 12,
            'per_feeding_g': 9,
            'biomass_kg': 0.32,
            'estimated_fcr': 0.35,
            'mixes': [{'label': 'MeM 200-300', 'grams': 112}],
            'water_items': [],
        },
        {
            'pl_stage': 11,
            'day': 2,
            'population': 159467,
            'base_population': 160000,
            'survival_pct': 100.0,
            'individual_weight_g': 0.003,
            'daily_growth_g': 0.001,
            'feed_rate_pct': 34.0,
            'total_day_g': 160,
            'feedings_per_day': 12,
            'per_feeding_g': 13,
            'biomass_kg': 0.48,
            'estimated_fcr': 0.57,
            'mixes': [{'label': 'MeM 200-300', 'grams': 163}],
            'water_items': [],
        },
        {
            'pl_stage': 12,
            'day': 3,
            'population': 158933,
            'base_population': 160000,
            'survival_pct': 99.0,
            'individual_weight_g': 0.004,
            'daily_growth_g': 0.001,
            'feed_rate_pct': 30.0,
            'total_day_g': 190,
            'feedings_per_day': 12,
            'per_feeding_g': 16,
            'biomass_kg': 0.64,
            'estimated_fcr': 0.73,
            'mixes': [{'label': 'MeM 200-300', 'grams': 143}, {'label': 'MeM 300-500', 'grams': 48}],
            'water_items': [],
        },
    ]
    raw_rows = [
        # day, PL, população, sobrevivência %, PL/g, peso g, biomassa kg, taxa %, total g, NS225, NS450, T500-900, T800-1200, refeições/dia
        (1, 13, 288000, 100.00, 333, 0.003000, 0.86, 25.00, 216, 216, 0, 0, 0, 12),
        (2, 14, 285120, 99.00, 250, 0.004000, 1.14, 23.75, 271, 271, 0, 0, 0, 12),
        (3, 15, 282240, 98.00, 171, 0.010000, 1.65, 22.56, 372, 372, 0, 0, 0, 12),
        (4, 16, 280800, 97.50, 113, 0.010000, 2.48, 21.43, 533, 533, 0, 0, 0, 12),
        (5, 17, 279360, 97.00, 78, 0.010000, 3.58, 20.36, 729, 729, 0, 0, 0, 12),
        (6, 18, 278640, 96.75, 59, 0.020000, 4.72, 19.34, 914, 914, 0, 0, 0, 12),
        (7, 19, 277920, 96.50, 46, 0.020000, 6.04, 18.38, 1110, 1110, 0, 0, 0, 12),
        (8, 20, 277200, 96.25, 36, 0.030000, 7.70, 17.46, 1344, 1344, 0, 0, 0, 12),
        (9, 21, 276480, 96.00, 29, 0.030000, 9.53, 16.59, 1581, 1186, 395, 0, 0, 12),
        (10, 22, 275760, 95.75, 23, 0.040000, 11.99, 15.76, 1889, 945, 945, 0, 0, 12),
        (11, 23, 275040, 95.50, 19, 0.050000, 14.48, 14.97, 2167, 542, 1625, 0, 0, 12),
        (12, 24, 274320, 95.25, 16, 0.060000, 17.15, 14.22, 2438, 0, 2438, 0, 0, 12),
        (13, 25, 273600, 95.00, 14, 0.070000, 19.54, 13.51, 2640, 0, 2640, 0, 0, 12),
        (14, 26, 272880, 94.75, 12, 0.080000, 20.89, 12.83, 2681, 0, 2011, 670, 0, 12),
        (15, 27, 348571, 94.50, 13.06, 0.076548, 26.68, 12.19, 3253, 0, 1627, 1627, 0, 12),
        (16, 28, 328528, 94.25, 5.6, 0.180000, 59.14, 11.58, 6849, 0, 1712, 5137, 0, 8),
        (17, 29, 327657, 94.00, 4.3, 0.230000, 75.36, 11.00, 8292, 0, 0, 8292, 0, 8),
        (18, 30, 326785, 93.75, 3.6, 0.280000, 91.50, 10.45, 9564, 0, 0, 9564, 0, 8),
        (19, 31, 325914, 93.50, 3.0, 0.330000, 107.55, 9.93, 10680, 0, 0, 10680, 0, 8),
        (20, 32, 325042, 93.25, 2.6, 0.380000, 123.52, 9.43, 11652, 0, 0, 11652, 0, 8),
        (21, 33, 324171, 93.00, 2.3, 0.430000, 139.39, 8.96, 12493, 0, 0, 12493, 0, 8),
        (22, 34, 323300, 92.75, 2.1, 0.480000, 155.16, 8.51, 13212, 0, 0, 13212, 0, 8),
        (23, 35, 322428, 92.50, 1.9, 0.520000, 167.66, 8.09, 13561, 0, 0, 13561, 0, 8),
        (24, 36, 321557, 92.25, 1.8, 0.570000, 183.29, 7.68, 14084, 0, 0, 14084, 0, 6),
        (25, 37, 320685, 92.00, 1.6, 0.620000, 198.82, 7.30, 14514, 0, 0, 10885, 3628, 6),
        (26, 38, 319814, 91.75, 1.5, 0.670000, 214.28, 6.93, 14859, 0, 0, 7430, 7430, 6),
        (27, 39, 318942, 91.50, 1.4, 0.720000, 229.64, 6.59, 15129, 0, 0, 3782, 11346, 6),
        (28, 40, 318071, 91.25, 1.3, 0.770000, 244.91, 6.26, 15328, 0, 0, 0, 15328, 6),
        (29, 41, 317200, 91.00, 1.2, 0.820000, 260.10, 5.95, 15465, 0, 0, 0, 15465, 4),
        (30, 42, 316328, 90.75, 1.1, 0.870000, 275.21, 5.65, 15545, 0, 0, 0, 15545, 4),
        (30, 43, 315457, 90.50, 1.1, 0.910000, 287.07, 5.37, 15404, 0, 0, 0, 15404, 4),
        (30, 44, 314585, 90.25, 1.0, 0.960000, 302.00, 5.10, 15395, 0, 0, 0, 15395, 4),
        (30, 45, 313714, 90.00, 1.0, 1.010000, 316.85, 4.84, 15344, 0, 0, 0, 15344, 4),
    ]
    rows = list(legacy_rows)
    for day, pl_stage, population, survival_pct, pls_per_g, weight_g, biomass_kg, feed_rate_pct, total_day_g, ns225, ns450, t500, t800, feedings in raw_rows:
        mixes = []
        if ns225:
            mixes.append({'label': 'NutriSphera 225', 'grams': ns225})
        if ns450:
            mixes.append({'label': 'NutriSphera 450', 'grams': ns450})
        if t500:
            mixes.append({'label': 'AQUAVITA 40#1', 'grams': t500})
        if t800:
            mixes.append({'label': 'AQUAVITA 40#2', 'grams': t800})
        rows.append({
            'pl_stage': pl_stage,
            'day': day,
            'population': population,
            'base_population': 288000,
            'survival_pct': survival_pct,
            'pls_per_g': pls_per_g,
            'individual_weight_g': weight_g,
            'daily_growth_g': None,
            'feed_rate_pct': feed_rate_pct,
            'total_day_g': total_day_g,
            'feedings_per_day': feedings,
            'per_feeding_g': round(total_day_g / feedings, 4) if feedings else 0,
            'biomass_kg': biomass_kg,
            'estimated_fcr': None,
            'mixes': mixes,
            'water_items': [],
        })
    return rows

NURSERY_PROTOCOLS['rg1']['name'] = 'Protocolo Alimentação Berçário e Pré-Engorda RG1 — Lote 2 Aquatec'
NURSERY_PROTOCOLS['rg1']['sheet_name'] = 'RG1 Lote 2 Aquatec'
NURSERY_PROTOCOLS['rg1']['base_population'] = 288000
NURSERY_PROTOCOLS['rg1']['rows'] = build_rg1_lote2_aquatec_rows()

DEFAULT_NURSERY_PROTOCOL_KEY = 'rg1'
NURSERY_PROTOCOL_BASE_POPULATION = NURSERY_PROTOCOLS[DEFAULT_NURSERY_PROTOCOL_KEY]['base_population']
NURSERY_PROTOCOL_ROWS = NURSERY_PROTOCOLS[DEFAULT_NURSERY_PROTOCOL_KEY]['rows']

NURSERY_FEED_STOCK_ALIASES = {
    # Rações finas do protocolo: equivalências com os nomes reais do estoque.
    # Isso evita que uma linha "225" seja vinculada por engano ao produto "450".
    'nutrisphera 225': ['NutriSphera 225', 'NUTRISFERA - BERÇÁRIO 225', 'NUTRISFERA BERÇÁRIO 225', 'NUTRISFERA BERCARIO 225'],
    'nutrisfera 225': ['NutriSphera 225', 'NUTRISFERA - BERÇÁRIO 225', 'NUTRISFERA BERÇÁRIO 225', 'NUTRISFERA BERCARIO 225'],
    'nutrisphera 450': ['NutriSphera 450', 'NUTRISFERA - BERÇÁRIO 450', 'NUTRISFERA BERÇÁRIO 450', 'NUTRISFERA BERCARIO 450'],
    'nutrisfera 450': ['NutriSphera 450', 'NUTRISFERA - BERÇÁRIO 450', 'NUTRISFERA BERÇÁRIO 450', 'NUTRISFERA BERCARIO 450'],
    'mem 200 300': ['MeM 200-300', 'MEM 200-300', 'MeM 200/300', 'MEM 200/300'],
    'mem 300 500': ['MeM 300-500', 'MEM 300-500', 'MeM 300/500', 'MEM 300/500'],
    'triturada 1': ['AQUAVITA 40#1', 'AQUAVITA 40 #1', 'AQUAVITA 40 1'],
    'triturada 2': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'SAMARIA 40#2', 'SAMARIA 40 #2', 'SAMARIA 40 2', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02'],
    'triturada 500 900': ['AQUAVITA 40#1', 'AQUAVITA 40 #1', 'AQUAVITA 40 1'],
    'triturada 500 900 um': ['AQUAVITA 40#1', 'AQUAVITA 40 #1', 'AQUAVITA 40 1'],
    'triturada 800 1200': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'SAMARIA 40#2', 'SAMARIA 40 #2', 'SAMARIA 40 2', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02'],
    'triturada 800 1200 um': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'SAMARIA 40#2', 'SAMARIA 40 #2', 'SAMARIA 40 2', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02'],
    'aquavita 40 1': ['AQUAVITA 40#1', 'AQUAVITA 40 #1', 'AQUAVITA 40/1'],
    'aquavita 40 1 0 5 1 0mm': ['AQUAVITA 40#1', 'AQUAVITA 40 #1', 'AQUAVITA 40/1'],
    'aquavita 40 2': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02', 'SAMARIA 40#2', 'SAMARIA 40 #2'],
    'aquavita 40 2 1 0 1 8mm': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02', 'SAMARIA 40#2', 'SAMARIA 40 #2'],
    'samaria 40 2': ['AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'AquaVita 40/2 (1,0-1,8mm)', 'SAMARIA 40#2', 'SAMARIA 40 #2', 'SAMARIA 40 2', 'JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02'],
    # Mantém compatibilidade com nomes técnicos antigos que já possam estar no histórico.
    # Correção: SM Starter 400/#02 é equivalente da AQUAVITA 40#2, nunca da 40#1.
    'juvenil 40 sm starter 400': ['JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02', 'AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'SAMARIA 40#2'],
    'juvenil 40 sm starter 400 e 02': ['JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - SM starter 400', 'JUVENIL 40 - #02', 'AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'SAMARIA 40#2'],
    'juvenil 40 02': ['JUVENIL 40 - SM starter 400 E #02', 'JUVENIL 40 - #02', 'JUVENIL 40 - SM starter 400', 'AQUAVITA 40#2', 'AQUAVITA 40 #2', 'AQUAVITA 40/2', 'SAMARIA 40#2', 'SAMARIA 40 #2'],
    'aquavita juv 38': ['AQUAVITA 38', 'AQUAVITA JUV. 38', 'AQUAVITA JUV 38', 'AquaVita JUV. 38 (1,5mm)', 'AQUAVITA 38 JUNIOR'],
    'aquavita 38': ['AQUAVITA 38', 'AQUAVITA JUV. 38', 'AQUAVITA JUV 38', 'AquaVita JUV. 38 (1,5mm)', 'AQUAVITA 38 JUNIOR'],
    'aquavita juv 38 1 5mm': ['AQUAVITA 38', 'AQUAVITA JUV. 38', 'AQUAVITA JUV 38', 'AQUAVITA 38 JUNIOR'],
    'aquavita 35': ['AQUAVITA 35', 'AQUAVITA B SAL 35', 'AQUAVITA PREMIUM B. SAL. 35', 'AquaVita 35 (2mm)'],
    'aquavita 35 2mm': ['AQUAVITA 35', 'AQUAVITA B SAL 35', 'AQUAVITA PREMIUM B. SAL. 35'],
    'irca carcimax 30': ['IRCA CarciMax 30', 'CARCIMAX 30', 'IRCA CarciMax 30 2,4mm'],
    'irca carcimax 30 2 4mm': ['IRCA CarciMax 30', 'CARCIMAX 30'],
}

NURSERY_WATER_SUPPLY_ALIASES = {
    'melaco': ['Melaço', 'Melaco'],
    'melaço': ['Melaço', 'Melaco'],
    'lothar': ['LOTHAR'],
    'aquapro eco': ['AQUAPRO ECO', 'Probiótico pastilha', 'Probiotico pastilha', 'Probiótico', 'Probiotico'],
    'probiotico pastilha': ['AQUAPRO ECO', 'Probiótico pastilha', 'Probiotico pastilha'],
    'probiótico pastilha': ['AQUAPRO ECO', 'Probiótico pastilha', 'Probiotico pastilha'],
    'calda iodada': ['Calda iodada', 'CALDA IODADA', 'Iodo'],
}

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

# Novo protocolo único do ciclo completo, importado do PDF "PROTOCOLOS Alimentacao".
# A tabela cobre Berçário, Juvenil/Pré-cria e Engorda, mas a mudança de fase na fazenda
# NÃO deve escolher a linha da ração pelo dia da fase. A linha de ração é associada
# pela idade/estágio do PL (PL11, PL12, J43, E67...). Assim, quando a transferência
# real acontece antes ou depois da tabela-modelo, o mix continua seguindo o tamanho
# real do camarão e não confunde 40/2 com rações de camarões maiores. A fase
# operacional segue sendo usada para frequência e para resolver estágios repetidos
# no protocolo (ex.: J43 versus PL43, E67 versus J67).
# Atualização solicitada: foram usados apenas os campos de alimentação da tabela
# (taxa de alimentação, total do dia e mix de ração). A lógica de horários, número
# de tratos, probióticos, LOTHAR e demais manejos/controles não foi alterada.
FULL_CYCLE_FEEDING_FREQUENCIES = {'bercario': 12, 'juvenil': 8, 'engorda': 6}
FULL_CYCLE_PROTOCOL_FLEXIBLE_PHASE_TRANSITIONS = True
FULL_CYCLE_PROTOCOL_BASE_POPULATION = 350000
FULL_CYCLE_PROTOCOL_COMPACT_ROWS = [('bercario', 1, 1, 'PL11', 350000, 100.0, 0.003, 1.05, 50.0, 526, [('NutriSphera 150', 526)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 1000.0, 'g', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('bercario', 2, 2, 'PL12', 346500, 99.0, 0.004, 1.39, 45.0, 624, [('NutriSphera 150', 624)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 1000.0, 'g', '07:45')]),
 ('bercario', 3, 3, 'PL13', 343000, 98.0, 0.01, 2.01, 40.0, 802, [('NutriSphera 150', 802)], [('Melaço', 300.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 4, 4, 'PL14', 341250, 97.5, 0.01, 3.02, 35.0, 1057, [('NutriSphera 150', 846), ('NutriSphera 225', 211)], [('Melaço', 400.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario',
  5,
  5,
  'PL15',
  339500,
  97.0,
  0.01,
  4.35,
  30.0,
  1306,
  [('NutriSphera 150', 783), ('NutriSphera 225', 523)],
  [('Melaço', 500.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 6, 6, 'PL16', 338625, 96.75, 0.02, 5.74, 25.0, 1435, [('NutriSphera 150', 574), ('NutriSphera 225', 861)], [('Melaço', 600.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 7, 7, 'PL17', 337750, 96.5, 0.02, 7.34, 20.0, 1468, [('NutriSphera 150', 294), ('NutriSphera 225', 1174)], [('Melaço', 700.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 8, 8, 'PL18', 336875, 96.25, 0.03, 9.36, 17.5, 1638, [('NutriSphera 225', 1638)], [('Melaço', 800.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('CALDA IODADA', 24.0, 'g', '08:15')]),
 ('bercario',
  9,
  9,
  'PL19',
  336000,
  96.0,
  0.03,
  11.59,
  15.0,
  1738,
  [('NutriSphera 225', 1390), ('NutriSphera 450', 348)],
  [('Melaço', 900.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 10, 10, 'PL20', 335125, 95.75, 0.04, 14.57, 13.5, 1967, [('NutriSphera 225', 1180), ('NutriSphera 450', 787)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 11, 11, 'PL21', 334250, 95.5, 0.05, 17.59, 12.0, 2111, [('NutriSphera 225', 844), ('NutriSphera 450', 1267)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario',
  12,
  12,
  'PL22',
  333375,
  95.25,
  0.06,
  20.84,
  11.0,
  2292,
  [('NutriSphera 225', 458), ('NutriSphera 450', 1834)],
  [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 13, 13, 'PL23', 332500, 95.0, 0.07, 23.75, 10.0, 2375, [('NutriSphera 450', 1900), ('AquaVita 40/1 (0,5-1,0mm)', 475)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 14, 14, 'PL24', 331625, 94.75, 0.08, 27.64, 9.0, 2487, [('NutriSphera 450', 1492), ('AquaVita 40/1 (0,5-1,0mm)', 995)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario',
  15,
  15,
  'PL25',
  330750,
  94.5,
  0.13,
  43.0,
  8.5,
  3655,
  [('NutriSphera 450', 1462), ('AquaVita 40/1 (0,5-1,0mm)', 2193)],
  [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('CALDA IODADA', 24.0, 'g', '08:15')]),
 ('bercario',
  16,
  16,
  'PL26',
  329875,
  94.25,
  0.18,
  59.38,
  8.0,
  4750,
  [('NutriSphera 450', 950), ('AquaVita 40/1 (0,5-1,0mm)', 3800)],
  [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 17, 17, 'PL27', 329000, 94.0, 0.23, 75.67, 7.5, 5675, [('AquaVita 40/1 (0,5-1,0mm)', 5675)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 18, 18, 'PL28', 328125, 93.75, 0.28, 91.88, 7.0, 6431, [('AquaVita 40/1 (0,5-1,0mm)', 6431)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 19, 19, 'PL29', 327250, 93.5, 0.33, 107.99, 6.5, 7020, [('AquaVita 40/1 (0,5-1,0mm)', 7020)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 20, 20, 'PL30', 326375, 93.25, 0.38, 124.02, 6.0, 7441, [('AquaVita 40/1 (0,5-1,0mm)', 7441)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 21, 21, 'PL31', 325500, 93.0, 0.43, 139.97, 5.5, 7698, [('AquaVita 40/1 (0,5-1,0mm)', 7698)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 22, 22, 'PL32', 324625, 92.75, 0.48, 155.82, 5.0, 7791, [('AquaVita 40/1 (0,5-1,0mm)', 7791)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('CALDA IODADA', 24.0, 'g', '08:15')]),
 ('bercario', 23, 23, 'PL33', 323750, 92.5, 0.52, 168.35, 4.75, 7997, [('AquaVita 40/1 (0,5-1,0mm)', 7997)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 24, 24, 'PL34', 322875, 92.25, 0.57, 184.04, 4.5, 8282, [('AquaVita 40/1 (0,5-1,0mm)', 6625), ('AquaVita 40/2 (1,0-1,8mm)', 1657)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 25, 25, 'PL35', 322000, 92.0, 0.62, 199.64, 4.25, 8485, [('AquaVita 40/1 (0,5-1,0mm)', 5091), ('AquaVita 40/2 (1,0-1,8mm)', 3394)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario',
  26,
  26,
  'PL36',
  321125,
  91.75,
  0.67,
  215.15,
  4.12,
  8864,
  [('AquaVita 40/1 (0,5-1,0mm)', 3546), ('AquaVita 40/2 (1,0-1,8mm)', 5318)],
  [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 27, 27, 'PL37', 320250, 91.5, 0.72, 230.58, 4.0, 9223, [('AquaVita 40/1 (0,5-1,0mm)', 1845), ('AquaVita 40/2 (1,0-1,8mm)', 7378)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 28, 28, 'PL38', 319375, 91.25, 0.77, 245.92, 3.85, 9468, [('AquaVita 40/2 (1,0-1,8mm)', 9468)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 29, 29, 'PL39', 318500, 91.0, 0.82, 261.17, 3.65, 9533, [('AquaVita 40/2 (1,0-1,8mm)', 9533)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('CALDA IODADA', 24.0, 'g', '08:15')]),
 ('bercario', 30, 30, 'PL40', 317625, 90.75, 0.87, 276.33, 3.5, 9672, [('AquaVita 40/2 (1,0-1,8mm)', 9672)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45'), ('AQUAPRO ECO', 2.0, 'un', '08:00')]),
 ('bercario', 31, 31, 'PL41', 316750, 90.5, 0.91, 288.24, 3.5, 10088, [('AquaVita 40/2 (1,0-1,8mm)', 10088)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 32, 32, 'PL42', 315875, 90.25, 0.96, 303.24, 3.5, 10613, [('AquaVita 40/2 (1,0-1,8mm)', 10613)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('bercario', 33, 33, 'PL43', 315000, 90.0, 1.01, 318.15, 3.5, 11135, [('AquaVita 40/2 (1,0-1,8mm)', 11135)], [('Melaço', 1000.0, 'g', '07:30'), ('LOTHAR', 500.0, 'g', '07:45')]),
 ('juvenil', 1, 34, 'J43', 315000, 100.0, 1.01, 318.15, 4.75, 15112, [('AquaVita 40/2 (1,0-1,8mm)', 15112)], [('Melaço', 10.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45'), ('AQUAPRO ECO', 40.0, 'un', '08:00')]),
 ('juvenil', 2, 35, 'J44', 311850, 99.0, 1.111, 346.47, 4.75, 16457, [('AquaVita 40/2 (1,0-1,8mm)', 16457)], [('Melaço', 10.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('juvenil', 3, 36, 'J45', 308700, 98.0, 1.222, 377.26, 4.75, 17920, [('AquaVita 40/2 (1,0-1,8mm)', 17920)], [('Melaço', 4.0, 'kg', '07:30'), ('LOTHAR', 3.0, 'kg', '07:45')]),
 ('juvenil', 4, 37, 'J46', 307125, 97.5, 1.344, 412.87, 4.75, 19611, [('AquaVita 40/2 (1,0-1,8mm)', 19611)], [('Melaço', 4.0, 'kg', '07:30'), ('LOTHAR', 3.0, 'kg', '07:45')]),
 ('juvenil', 5, 38, 'J47', 305550, 97.0, 1.479, 451.83, 4.75, 21462, [('AquaVita 40/2 (1,0-1,8mm)', 21462)], [('Melaço', 4.0, 'kg', '07:30'), ('LOTHAR', 3.0, 'kg', '07:45'), ('AQUAPRO ECO', 20.0, 'un', '08:00')]),
 ('juvenil', 6, 39, 'J48', 304763, 96.75, 1.627, 495.73, 4.75, 23547, [('AquaVita 40/2 (1,0-1,8mm)', 18838), ('AquaVita JUV. 38 (1,5mm)', 4709)], [('Melaço', 5.0, 'kg', '07:30'), ('LOTHAR', 4.0, 'kg', '07:45')]),
 ('juvenil', 7, 40, 'J49', 303975, 96.5, 1.789, 543.9, 4.75, 25835, [('AquaVita 40/2 (1,0-1,8mm)', 15501), ('AquaVita JUV. 38 (1,5mm)', 10334)], [('Melaço', 5.0, 'kg', '07:30'), ('LOTHAR', 4.0, 'kg', '07:45')]),
 ('juvenil',
  8,
  41,
  'J50',
  303188,
  96.25,
  1.968,
  596.73,
  4.75,
  28345,
  [('AquaVita 40/2 (1,0-1,8mm)', 11338), ('AquaVita JUV. 38 (1,5mm)', 17007)],
  [('Melaço', 6.0, 'kg', '07:30'), ('LOTHAR', 4.0, 'kg', '07:45'), ('CALDA IODADA', 240.0, 'g', '08:15')]),
 ('juvenil',
  9,
  42,
  'J51',
  302400,
  96.0,
  2.126,
  642.8,
  4.25,
  27319,
  [('AquaVita 40/2 (1,0-1,8mm)', 5464), ('AquaVita JUV. 38 (1,5mm)', 21855)],
  [('Melaço', 5.0, 'kg', '07:30'), ('LOTHAR', 4.0, 'kg', '07:45'), ('AQUAPRO ECO', 10.0, 'un', '08:00')]),
 ('juvenil', 10, 43, 'J52', 301613, 95.75, 2.296, 692.42, 4.25, 29428, [('AquaVita JUV. 38 (1,5mm)', 29428)], [('Melaço', 6.0, 'kg', '07:30'), ('LOTHAR', 4.0, 'kg', '07:45')]),
 ('juvenil', 11, 44, 'J53', 300825, 95.5, 2.479, 745.86, 4.25, 31699, [('AquaVita JUV. 38 (1,5mm)', 31699)], [('Melaço', 6.0, 'kg', '07:30'), ('LOTHAR', 5.0, 'kg', '07:45')]),
 ('juvenil', 12, 45, 'J54', 300038, 95.25, 2.678, 803.42, 4.25, 34145, [('AquaVita JUV. 38 (1,5mm)', 34145)], [('Melaço', 7.0, 'kg', '07:30'), ('LOTHAR', 5.0, 'kg', '07:45'), ('AQUAPRO ECO', 10.0, 'un', '08:00')]),
 ('juvenil', 13, 46, 'J55', 299250, 95.0, 2.892, 865.41, 4.25, 36780, [('AquaVita JUV. 38 (1,5mm)', 36780)], [('Melaço', 7.0, 'kg', '07:30'), ('LOTHAR', 6.0, 'kg', '07:45')]),
 ('juvenil', 14, 47, 'J56', 298463, 94.75, 3.123, 932.19, 4.25, 39618, [('AquaVita JUV. 38 (1,5mm)', 39618)], [('Melaço', 8.0, 'kg', '07:30'), ('LOTHAR', 6.0, 'kg', '07:45')]),
 ('juvenil', 15, 48, 'J57', 297675, 94.5, 3.373, 1004.1, 4.25, 42674, [('AquaVita JUV. 38 (1,5mm)', 42674)], [('Melaço', 9.0, 'kg', '07:30'), ('LOTHAR', 6.0, 'kg', '07:45')]),
 ('juvenil', 16, 49, 'J58', 296888, 94.25, 3.576, 1061.53, 3.75, 39808, [('AquaVita JUV. 38 (1,5mm)', 39808)], [('Melaço', 8.0, 'kg', '07:30'), ('LOTHAR', 6.0, 'kg', '07:45'), ('AQUAPRO ECO', 5.0, 'un', '08:00')]),
 ('juvenil', 17, 50, 'J59', 296100, 94.0, 3.79, 1122.24, 3.75, 42084, [('AquaVita JUV. 38 (1,5mm)', 42084)], [('Melaço', 8.0, 'kg', '07:30'), ('LOTHAR', 6.0, 'kg', '07:45')]),
 ('juvenil', 18, 51, 'J60', 295313, 93.75, 4.017, 1186.41, 3.75, 44490, [('AquaVita JUV. 38 (1,5mm)', 44490)], [('Melaço', 9.0, 'kg', '07:30'), ('LOTHAR', 7.0, 'kg', '07:45')]),
 ('juvenil',
  19,
  52,
  'J61',
  294525,
  93.5,
  4.259,
  1254.24,
  3.75,
  47034,
  [('AquaVita JUV. 38 (1,5mm)', 37627), ('AquaVita 35 (2mm)', 9407)],
  [('Melaço', 9.0, 'kg', '07:30'), ('LOTHAR', 7.0, 'kg', '07:45'), ('AQUAPRO ECO', 5.0, 'un', '08:00')]),
 ('juvenil', 20, 53, 'J62', 293738, 93.25, 4.514, 1325.94, 3.75, 49723, [('AquaVita JUV. 38 (1,5mm)', 29834), ('AquaVita 35 (2mm)', 19889)], [('Melaço', 10.0, 'kg', '07:30'), ('LOTHAR', 7.0, 'kg', '07:45')]),
 ('juvenil', 21, 54, 'J63', 292950, 93.0, 4.785, 1401.73, 3.75, 52565, [('AquaVita JUV. 38 (1,5mm)', 21026), ('AquaVita 35 (2mm)', 31539)], [('Melaço', 11.0, 'kg', '07:30'), ('LOTHAR', 8.0, 'kg', '07:45')]),
 ('juvenil',
  22,
  55,
  'J64',
  292163,
  92.75,
  5.072,
  1481.84,
  3.75,
  55569,
  [('AquaVita JUV. 38 (1,5mm)', 11114), ('AquaVita 35 (2mm)', 44455)],
  [('Melaço', 11.0, 'kg', '07:30'), ('LOTHAR', 8.0, 'kg', '07:45'), ('CALDA IODADA', 240.0, 'g', '08:15')]),
 ('juvenil', 23, 56, 'J65', 291375, 92.5, 5.275, 1536.96, 3.5, 53794, [('AquaVita 35 (2mm)', 53794)], [('Melaço', 11.0, 'kg', '07:30'), ('LOTHAR', 8.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('juvenil', 24, 57, 'J66', 290588, 92.25, 5.486, 1594.12, 3.5, 55794, [('AquaVita 35 (2mm)', 55794)], [('Melaço', 11.0, 'kg', '07:30'), ('LOTHAR', 8.0, 'kg', '07:45')]),
 ('juvenil', 25, 58, 'J67', 289800, 92.0, 5.705, 1653.39, 3.5, 57869, [('AquaVita 35 (2mm)', 57869)], [('Melaço', 12.0, 'kg', '07:30'), ('LOTHAR', 9.0, 'kg', '07:45')]),
 ('juvenil', 26, 59, 'J68', 289013, 91.75, 5.933, 1714.86, 3.5, 60020, [('AquaVita 35 (2mm)', 60020)], [('Melaço', 12.0, 'kg', '07:30'), ('LOTHAR', 9.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('juvenil', 27, 60, 'J69', 288225, 91.5, 6.171, 1778.59, 3.5, 62251, [('AquaVita 35 (2mm)', 62251)], [('Melaço', 12.0, 'kg', '07:30'), ('LOTHAR', 9.0, 'kg', '07:45')]),
 ('juvenil', 28, 61, 'J70', 287438, 91.25, 6.418, 1844.68, 3.5, 64564, [('AquaVita 35 (2mm)', 64564)], [('Melaço', 13.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('juvenil', 29, 62, 'J71', 286650, 91.0, 6.674, 1913.21, 3.5, 66962, [('AquaVita 35 (2mm)', 66962)], [('Melaço', 13.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('juvenil', 30, 63, 'J72', 285863, 90.75, 6.908, 1974.73, 3.25, 64179, [('AquaVita 35 (2mm)', 64179)], [('Melaço', 13.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('juvenil', 31, 64, 'J73', 285075, 90.5, 7.15, 2038.22, 3.25, 66242, [('AquaVita 35 (2mm)', 66242)], [('Melaço', 13.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('juvenil', 32, 65, 'J74', 284288, 90.25, 7.4, 2103.73, 3.25, 68371, [('AquaVita 35 (2mm)', 68371)], [('Melaço', 14.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('juvenil', 33, 66, 'J75', 283500, 90.0, 7.659, 2171.33, 3.25, 70568, [('AquaVita 35 (2mm)', 70568)], [('Melaço', 14.0, 'kg', '07:30'), ('LOTHAR', 11.0, 'kg', '07:45')]),
 ('engorda',
  1,
  67,
  'E67',
  283500,
  100.0,
  7.659,
  2171.33,
  4.75,
  103138,
  [('AquaVita 35 (2mm)', 82510), ('IRCA CarciMax 30 2,4mm', 20628)],
  [('Melaço', 10.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45'), ('AQUAPRO ECO', 40.0, 'un', '08:00')]),
 ('engorda', 2, 68, 'E68', 282083, 99.5, 7.85, 2214.48, 4.75, 105188, [('AquaVita 35 (2mm)', 63113), ('IRCA CarciMax 30 2,4mm', 42075)], [('Melaço', 10.0, 'kg', '07:30'), ('LOTHAR', 10.0, 'kg', '07:45')]),
 ('engorda', 3, 69, 'E69', 280665, 99.0, 8.047, 2258.44, 4.75, 107276, [('AquaVita 35 (2mm)', 42910), ('IRCA CarciMax 30 2,4mm', 64366)], [('Melaço', 21.0, 'kg', '07:30'), ('LOTHAR', 16.0, 'kg', '07:45')]),
 ('engorda', 4, 70, 'E70', 279248, 98.5, 8.248, 2303.21, 4.75, 109402, [('AquaVita 35 (2mm)', 21880), ('IRCA CarciMax 30 2,4mm', 87522)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 16.0, 'kg', '07:45')]),
 ('engorda', 5, 71, 'E71', 277830, 98.0, 8.454, 2348.8, 4.75, 111568, [('IRCA CarciMax 30 2,4mm', 111568)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45'), ('AQUAPRO ECO', 20.0, 'un', '08:00')]),
 ('engorda', 6, 72, 'E72', 276413, 97.5, 8.665, 2395.24, 4.75, 113774, [('IRCA CarciMax 30 2,4mm', 113774)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 7, 73, 'E73', 274995, 97.0, 8.882, 2442.53, 4.75, 116020, [('IRCA CarciMax 30 2,4mm', 116020)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 8, 74, 'E74', 273578, 96.5, 9.104, 2490.69, 4.75, 118308, [('IRCA CarciMax 30 2,4mm', 118308)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45'), ('CALDA IODADA', 240.0, 'g', '08:15')]),
 ('engorda', 9, 75, 'E75', 272160, 96.0, 9.332, 2539.73, 4.25, 107939, [('IRCA CarciMax 30 2,4mm', 107939)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 16.0, 'kg', '07:45'), ('AQUAPRO ECO', 10.0, 'un', '08:00')]),
 ('engorda', 10, 76, 'E76', 270743, 95.5, 9.565, 2589.66, 4.25, 110061, [('IRCA CarciMax 30 2,4mm', 110061)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 11, 77, 'E77', 269325, 95.0, 9.804, 2640.51, 4.25, 112222, [('IRCA CarciMax 30 2,4mm', 112222)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 12, 78, 'E78', 267908, 94.5, 10.049, 2692.28, 4.25, 114422, [('IRCA CarciMax 30 2,4mm', 114422)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45'), ('AQUAPRO ECO', 10.0, 'un', '08:00')]),
 ('engorda', 13, 79, 'E79', 266490, 94.0, 10.301, 2744.98, 4.25, 116662, [('IRCA CarciMax 30 2,4mm', 116662)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 14, 80, 'E80', 265073, 93.5, 10.558, 2798.64, 4.25, 118942, [('IRCA CarciMax 30 2,4mm', 118942)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 15, 81, 'E81', 263655, 93.0, 10.822, 2853.27, 4.25, 121264, [('IRCA CarciMax 30 2,4mm', 121264)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 16, 82, 'E82', 262238, 92.5, 11.093, 2908.88, 3.75, 109083, [('IRCA CarciMax 30 2,4mm', 109083)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 16.0, 'kg', '07:45'), ('AQUAPRO ECO', 5.0, 'un', '08:00')]),
 ('engorda', 17, 83, 'E83', 260820, 92.0, 11.37, 2965.48, 3.75, 111206, [('IRCA CarciMax 30 2,4mm', 111206)], [('Melaço', 22.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 18, 84, 'E84', 259403, 91.5, 11.654, 3023.1, 3.75, 113366, [('IRCA CarciMax 30 2,4mm', 113366)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45')]),
 ('engorda', 19, 85, 'E85', 257985, 91.0, 11.945, 3081.74, 3.75, 115565, [('IRCA CarciMax 30 2,4mm', 115565)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45'), ('AQUAPRO ECO', 5.0, 'un', '08:00')]),
 ('engorda', 20, 86, 'E86', 256568, 90.5, 12.244, 3141.43, 3.75, 117804, [('IRCA CarciMax 30 2,4mm', 117804)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 21, 87, 'E87', 255150, 90.0, 12.55, 3202.18, 3.75, 120082, [('IRCA CarciMax 30 2,4mm', 120082)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 22, 88, 'E88', 253733, 89.5, 12.864, 3264.0, 3.75, 122400, [('IRCA CarciMax 30 2,4mm', 122400)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45'), ('CALDA IODADA', 240.0, 'g', '08:15')]),
 ('engorda', 23, 89, 'E89', 252315, 89.0, 13.186, 3326.9, 3.5, 116442, [('IRCA CarciMax 30 2,4mm', 116442)], [('Melaço', 23.0, 'kg', '07:30'), ('LOTHAR', 17.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('engorda', 24, 90, 'E90', 250898, 88.5, 13.515, 3390.92, 3.5, 118682, [('IRCA CarciMax 30 2,4mm', 118682)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 25, 91, 'E91', 249480, 88.0, 13.853, 3456.06, 3.5, 120962, [('IRCA CarciMax 30 2,4mm', 120962)], [('Melaço', 24.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45')]),
 ('engorda', 26, 92, 'E92', 248063, 87.5, 14.199, 3522.33, 3.5, 123282, [('IRCA CarciMax 30 2,4mm', 123282)], [('Melaço', 25.0, 'kg', '07:30'), ('LOTHAR', 18.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('engorda', 27, 93, 'E93', 246645, 87.0, 14.554, 3589.76, 3.5, 125642, [('IRCA CarciMax 30 2,4mm', 125642)], [('Melaço', 25.0, 'kg', '07:30'), ('LOTHAR', 19.0, 'kg', '07:45')]),
 ('engorda', 28, 94, 'E94', 245228, 86.5, 14.918, 3658.35, 3.5, 128042, [('IRCA CarciMax 30 2,4mm', 128042)], [('Melaço', 26.0, 'kg', '07:30'), ('LOTHAR', 19.0, 'kg', '07:45')]),
 ('engorda', 29, 95, 'E95', 243810, 86.0, 15.291, 3728.14, 3.5, 130485, [('IRCA CarciMax 30 2,4mm', 130485)], [('Melaço', 26.0, 'kg', '07:30'), ('LOTHAR', 20.0, 'kg', '07:45')]),
 ('engorda', 30, 96, 'E96', 242393, 85.5, 15.673, 3799.12, 3.25, 123472, [('IRCA CarciMax 30 2,4mm', 123472)], [('Melaço', 25.0, 'kg', '07:30'), ('LOTHAR', 19.0, 'kg', '07:45'), ('AQUAPRO ECO', 4.0, 'un', '08:00')]),
 ('engorda', 31, 97, 'E97', 240975, 85.0, 16.065, 3871.33, 3.25, 125818, [('IRCA CarciMax 30 2,4mm', 125818)], [('Melaço', 25.0, 'kg', '07:30'), ('LOTHAR', 19.0, 'kg', '07:45')]),
 ('engorda', 32, 98, 'E98', 239558, 84.5, 16.467, 3944.77, 3.25, 128205, [('IRCA CarciMax 30 2,4mm', 128205)], [('Melaço', 26.0, 'kg', '07:30'), ('LOTHAR', 19.0, 'kg', '07:45')]),
 ('engorda', 33, 99, 'E99', 238140, 84.0, 16.879, 4019.47, 3.25, 130633, [('IRCA CarciMax 30 2,4mm', 130633)], [('Melaço', 26.0, 'kg', '07:30'), ('LOTHAR', 20.0, 'kg', '07:45')])]


def _stage_number_from_label(stage_label, fallback):
    match = re.search(r'(\d+)', str(stage_label or ''))
    return int(match.group(1)) if match else fallback


def build_full_cycle_protocol_rows():
    rows = []
    previous_weight = None
    cumulative_feed_kg = 0.0
    for phase, phase_day, cycle_day, stage_label, population, survival_pct, weight_g, biomass_kg, feed_rate_pct, total_day_g, mixes, water_items in FULL_CYCLE_PROTOCOL_COMPACT_ROWS:
        feedings_per_day = FULL_CYCLE_FEEDING_FREQUENCIES.get(phase, 8)
        total_day_g = int(round(total_day_g or 0))
        cumulative_feed_kg += total_day_g / 1000.0
        stage_number = _stage_number_from_label(stage_label, cycle_day)
        daily_growth_g = None if previous_weight is None else round(max((weight_g or 0) - previous_weight, 0), 4)
        previous_weight = weight_g or previous_weight
        rows.append({
            'phase': phase,
            'phase_day': int(phase_day or cycle_day or 1),
            'cycle_day': int(cycle_day or phase_day or 1),
            'day': int(cycle_day or phase_day or 1),
            'stage_label': stage_label,
            'stage_number': stage_number,
            'pl_stage': stage_number,
            'population': int(round(population or 0)),
            'base_population': FULL_CYCLE_PROTOCOL_BASE_POPULATION,
            'survival_pct': float(survival_pct or 0),
            'individual_weight_g': float(weight_g or 0),
            'weight_g': float(weight_g or 0),
            'biomass_kg': float(biomass_kg or 0),
            'feed_rate_pct': float(feed_rate_pct or 0),
            'total_day_g': total_day_g,
            'daily_feed_kg': round(total_day_g / 1000.0, 3),
            'feedings_per_day': feedings_per_day,
            'per_feeding_g': round(total_day_g / feedings_per_day, 2) if feedings_per_day else total_day_g,
            'daily_growth_g': daily_growth_g,
            'estimated_fcr': None,
            'cumulative_feed_kg': round(cumulative_feed_kg, 3),
            'mixes': [{'label': label, 'grams': int(round(grams or 0))} for label, grams in mixes if int(round(grams or 0)) > 0],
            'water_items': [
                {
                    'label': label,
                    'source_label': label,
                    'category': 'aditivo',
                    'quantity': float(quantity),
                    'measure_unit': measure_unit,
                    'scheduled_time': scheduled_time,
                    'priority': 'alta',
                }
                for label, quantity, measure_unit, scheduled_time in water_items
                if quantity not in (None, '', 0)
            ],
        })
    return rows


FULL_CYCLE_PROTOCOL_ROWS = build_full_cycle_protocol_rows()
NURSERY_PROTOCOLS['full_cycle'] = {
    'name': 'Protocolo Alimentação Ciclo Completo — Berçário, Juvenil e Engorda',
    'sheet_name': 'PROTOCOLOS Alimentacao - alimentação atualizada',
    'base_population': FULL_CYCLE_PROTOCOL_BASE_POPULATION,
    'rows': FULL_CYCLE_PROTOCOL_ROWS,
}
# Mantém compatibilidade com unidades antigas SP/RG que já estavam usando chaves específicas.
NURSERY_PROTOCOLS['sp1'] = NURSERY_PROTOCOLS['full_cycle']
NURSERY_PROTOCOLS['rg1'] = NURSERY_PROTOCOLS['full_cycle']
DEFAULT_NURSERY_PROTOCOL_KEY = 'full_cycle'
NURSERY_PROTOCOL_BASE_POPULATION = FULL_CYCLE_PROTOCOL_BASE_POPULATION
NURSERY_PROTOCOL_ROWS = FULL_CYCLE_PROTOCOL_ROWS
TABLE_PROTOCOL_BASE_POPULATION = FULL_CYCLE_PROTOCOL_BASE_POPULATION
PRODUCTION_PROTOCOL_ROWS = [
    {
        'phase': row['phase'],
        'phase_day': row['phase_day'],
        'cumulative_day': row['cycle_day'],
        'stage': row['stage_label'],
        'population': row['population'],
        'weight_g': row['individual_weight_g'],
        'daily_growth_g': row['daily_growth_g'],
        'biomass_kg': row['biomass_kg'],
        'feed_rate_pct': row['feed_rate_pct'],
        'daily_feed_kg': row['daily_feed_kg'],
        'feedings_per_day': row['feedings_per_day'],
        'survival_pct': row['survival_pct'],
        'cumulative_feed_kg': row['cumulative_feed_kg'],
        'estimated_fcr': row['estimated_fcr'],
        'mixes': [{'label': item['label'], 'kg': round((item['grams'] or 0) / 1000.0, 3)} for item in row.get('mixes', [])],
    }
    for row in FULL_CYCLE_PROTOCOL_ROWS
]


NURSERY_FEED_TIMES = ['08:00', '10:00', '12:00', '14:00', '16:00', '18:00', '20:00', '22:00', '00:00', '02:00', '04:00', '06:00']



def default_feeding_protocol_rows_for_seed():
    """Retorna a tabela padrão do PDF em formato pronto para gravar/editar."""
    rows = []
    for row in FULL_CYCLE_PROTOCOL_ROWS:
        rows.append({
            'phase': normalize_phase_value(row.get('phase')) or 'bercario',
            'phase_day': int(row.get('phase_day') or row.get('day') or 1),
            'cycle_day': int(row.get('cycle_day') or row.get('day') or 1),
            'stage_label': row.get('stage_label') or row.get('stage') or '',
            'population': int(row.get('population') or 0),
            'survival_pct': float(row.get('survival_pct') or 0),
            'individual_weight_g': float(row.get('individual_weight_g') or row.get('weight_g') or 0),
            'biomass_kg': float(row.get('biomass_kg') or 0),
            'feed_rate_pct': float(row.get('feed_rate_pct') or 0),
            'total_day_g': int(round(row.get('total_day_g') or 0)),
            'feedings_per_day': int(row.get('feedings_per_day') or feedings_per_day_for_phase(row.get('phase'))),
            'mixes': [{'label': item.get('label'), 'grams': int(round(item.get('grams') or 0))} for item in row.get('mixes', []) if int(round(item.get('grams') or 0)) > 0],
            'water_items': row.get('water_items', []),
        })
    return rows


def protocol_feed_label_key(label: str) -> str:
    return normalize_text(label or '')


def ensure_feeding_protocol_seeded(force: bool = False):
    """Materializa a tabela padrão do PDF no banco.

    Se já existir tabela editada pelo usuário, não sobrescreve. O parâmetro force é
    usado apenas pelo botão "restaurar padrão" da tela de cadastro.
    """
    if not has_app_context():
        return
    try:
        existing_count = FeedingProtocolRow.query.count()
    except Exception:
        return

    if existing_count and not force:
        return

    if force:
        FeedingProtocolFeed.query.delete()
        FeedingProtocolRow.query.delete()
        db.session.flush()

    label_order = OrderedDict()
    for row_data in default_feeding_protocol_rows_for_seed():
        row = FeedingProtocolRow(
            protocol_key='full_cycle',
            phase=row_data['phase'],
            phase_day=row_data['phase_day'],
            cycle_day=row_data['cycle_day'],
            stage_label=row_data['stage_label'],
            population=row_data['population'],
            survival_pct=row_data['survival_pct'],
            individual_weight_g=row_data['individual_weight_g'],
            biomass_kg=row_data['biomass_kg'],
            feed_rate_pct=row_data['feed_rate_pct'],
            total_day_g=row_data['total_day_g'],
            feedings_per_day=row_data['feedings_per_day'],
            water_items_json=json.dumps(row_data.get('water_items') or [], ensure_ascii=False),
            active=True,
            updated_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.flush()
        for item in row_data.get('mixes', []):
            label = (item.get('label') or '').strip()
            grams = int(round(item.get('grams') or 0))
            if not label or grams <= 0:
                continue
            if label not in label_order:
                label_order[label] = len(label_order)
            db.session.add(FeedingProtocolFeed(
                row_id=row.id,
                protocol_label=label,
                grams=grams,
                sort_order=label_order[label],
                updated_at=datetime.utcnow(),
            ))
            if not FeedingProtocolFeedMap.query.filter_by(protocol_label=label).first():
                db.session.add(FeedingProtocolFeedMap(protocol_label=label, updated_at=datetime.utcnow()))
    db.session.commit()


def feeding_protocol_row_to_dict(row: FeedingProtocolRow) -> dict:
    stage_number = _stage_number_from_label(row.stage_label, row.cycle_day or row.phase_day or 1)
    mixes = []
    for feed in sorted(row.feeds or [], key=lambda item: (item.sort_order or 0, item.protocol_label or '')):
        grams = int(round(feed.grams or 0))
        if grams > 0:
            mixes.append({'label': feed.protocol_label, 'grams': grams})
    water_items = []
    if row.water_items_json:
        try:
            water_items = json.loads(row.water_items_json) or []
        except Exception:
            water_items = []
    total_day_g = int(round(row.total_day_g or 0))
    feedings_per_day = int(row.feedings_per_day or feedings_per_day_for_phase(row.phase))
    return {
        'phase': normalize_phase_value(row.phase) or 'bercario',
        'phase_day': int(row.phase_day or 1),
        'cycle_day': int(row.cycle_day or row.phase_day or 1),
        'day': int(row.cycle_day or row.phase_day or 1),
        'stage_label': row.stage_label or f"Dia {row.phase_day or 1}",
        'stage_number': stage_number,
        'pl_stage': stage_number,
        'population': int(row.population or 0),
        'base_population': FULL_CYCLE_PROTOCOL_BASE_POPULATION,
        'survival_pct': float(row.survival_pct or 0),
        'individual_weight_g': float(row.individual_weight_g or 0),
        'weight_g': float(row.individual_weight_g or 0),
        'biomass_kg': float(row.biomass_kg or 0),
        'feed_rate_pct': float(row.feed_rate_pct or 0),
        'total_day_g': total_day_g,
        'daily_feed_kg': round(total_day_g / 1000.0, 3),
        'feedings_per_day': feedings_per_day,
        'per_feeding_g': round(total_day_g / feedings_per_day, 2) if feedings_per_day else total_day_g,
        'mixes': mixes,
        'water_items': water_items,
    }


def editable_feeding_protocol_rows():
    """Carrega a tabela editável. Fallback seguro para a tabela hard-coded."""
    if not has_app_context():
        return []
    try:
        ensure_feeding_protocol_seeded()
        rows = (
            FeedingProtocolRow.query.options(joinedload(FeedingProtocolRow.feeds))
            .filter_by(active=True)
            .order_by(
                case((FeedingProtocolRow.phase == 'bercario', 1), (FeedingProtocolRow.phase == 'juvenil', 2), (FeedingProtocolRow.phase == 'engorda', 3), else_=4),
                FeedingProtocolRow.phase_day.asc(),
                FeedingProtocolRow.id.asc(),
            )
            .all()
        )
        return [feeding_protocol_row_to_dict(row) for row in rows]
    except Exception:
        return []


def get_protocol_feed_labels(rows=None):
    rows = rows or editable_feeding_protocol_rows() or FULL_CYCLE_PROTOCOL_ROWS
    labels = OrderedDict()
    for row in rows:
        for item in row.get('mixes', []):
            label = (item.get('label') or '').strip()
            if label and label not in labels:
                labels[label] = len(labels)
    return list(labels.keys())


def protocol_feed_mapping_is_compatible(label: str, product) -> bool:
    """Confere se um vínculo manual da tabela base aponta para a família correta.

    Um vínculo salvo errado, como "AquaVita 40/1" apontando para "AquaVita 35",
    não deve ser obedecido pela integração automática do Manejo Diário.
    """
    if not product:
        return False
    product_text = f'{product.full_name} {product.brand or ""} {product.feed_type or ""} {product.technical_summary or ""}'
    try:
        label_sizes = nursery_feed_size_tokens(label)
        product_sizes = nursery_feed_size_tokens(product_text)
        if label_sizes and product_sizes and not label_sizes.intersection(product_sizes):
            return False
        # Quando a linha do protocolo tem tamanho explícito, o produto também precisa
        # carregar esse tamanho/grupo ou bater por alias forte. Isso bloqueia marca genérica.
        if label_sizes and not product_sizes:
            alias_norms = [normalize_text(item) for item in nursery_feed_stock_alias_labels(label)]
            product_norms = [
                normalize_text(product.full_name),
                normalize_text(f'{product.brand} {product.feed_type}'),
                normalize_text(f'{product.full_name} {product.technical_summary or ""}'),
            ]
            alias_norms = [item for item in alias_norms if item]
            product_norms = [item for item in product_norms if item]
            if not any(alias_norm == prod_norm or alias_norm in prod_norm for alias_norm in alias_norms for prod_norm in product_norms):
                return False
    except Exception:
        return True
    return True


def _protocol_feed_map_product(mapping, requested_label: str):
    if not mapping or not mapping.feed_product_id:
        return None
    product = db.session.get(FeedProduct, mapping.feed_product_id)
    if protocol_feed_mapping_is_compatible(requested_label, product):
        return product
    return None


def get_protocol_feed_map(label: str):
    label = (label or '').strip()
    if not label or not has_app_context():
        return None
    try:
        # 1) Regra principal: obedecer ao vínculo EXATO salvo na aba
        #    "Cadastro de ração" da Tabela Base de Alimentação.
        mapping = FeedingProtocolFeedMap.query.filter_by(protocol_label=label).first()
        product = _protocol_feed_map_product(mapping, label)
        if product:
            return product

        # 2) Se o nome da linha mudou, mas é a mesma família/granulometria, reaproveita
        #    o vínculo já salvo na Tabela Base. Ex.: coluna "AquaVita 40/2" vinculada
        #    no cadastro também resolve uma linha antiga "SAMARIA 40#2".
        label_sizes = nursery_feed_size_tokens(label)
        if label_sizes:
            compatible_maps = []
            for candidate in FeedingProtocolFeedMap.query.filter(FeedingProtocolFeedMap.feed_product_id.isnot(None)).all():
                candidate_sizes = nursery_feed_size_tokens(candidate.protocol_label or '')
                if candidate_sizes and not label_sizes.intersection(candidate_sizes):
                    continue
                candidate_product = _protocol_feed_map_product(candidate, label)
                if candidate_product:
                    compatible_maps.append((
                        2 if normalize_text(candidate.protocol_label or '') in [normalize_text(item) for item in nursery_feed_stock_alias_labels(label)] else 1,
                        candidate.updated_at or datetime.min,
                        candidate_product,
                    ))
            if compatible_maps:
                compatible_maps.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return compatible_maps[0][2]
    except Exception:
        return None
    return None


def phase_day_from_dates(start_date, target_date):
    return max(inclusive_day_count(start_date, target_date), 1)

def feedings_per_day_for_phase(phase: str | None, fallback=None) -> int:
    phase = normalize_phase_value(phase)
    if phase in FULL_CYCLE_FEEDING_FREQUENCIES:
        return FULL_CYCLE_FEEDING_FREQUENCIES[phase]
    return int(fallback or 8)


def build_feeding_time_labels(feedings_per_day: int, first_time_label='08:00'):
    feedings_per_day = max(int(feedings_per_day or 0), 0)
    if feedings_per_day <= 0:
        return []
    first = parse_time(first_time_label) or time(hour=8)
    start_minutes = first.hour * 60 + first.minute
    interval_minutes = 24 * 60 / feedings_per_day
    labels = []
    for idx in range(feedings_per_day):
        total_minutes = int(round(start_minutes + idx * interval_minutes)) % (24 * 60)
        labels.append(f'{total_minutes // 60:02d}:{total_minutes % 60:02d}')
    return labels


def feeding_interval_label(feedings_per_day: int) -> str:
    if not feedings_per_day:
        return '—'
    interval_hours = 24 / float(feedings_per_day)
    if abs(interval_hours - round(interval_hours)) < 0.001:
        return f'{int(round(interval_hours))}h'
    return f'{interval_hours:.1f}h'.replace('.', ',')


def inclusive_day_count(start_date, end_date) -> int:
    """Conta dias operacionais incluindo o dia de entrada/transferência."""
    if not start_date or not end_date:
        return 0
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    if start_date > end_date:
        return 0
    return max((end_date - start_date).days + 1, 1)


def pluralize_day_pt(days: int) -> str:
    days = int(days or 0)
    return f"{days} dia" if days == 1 else f"{days} dias"


def nursery_phase_start_date(lot, unit, allocation, transfer_marker, target_date):
    """Data mais confiável para contar há quantos dias o lote está na fase atual.

    A alocação ativa é a fonte principal porque é criada/atualizada na transferência
    trifásica. A transferência fica como fallback para bancos antigos.
    """
    if allocation and getattr(allocation, 'start_date', None):
        return allocation.start_date
    if transfer_marker and getattr(transfer_marker, 'transfer_date', None):
        return transfer_marker.transfer_date
    return getattr(lot, 'start_date', None) or target_date


def get_nursery_protocol_meta(protocol_key: str | None = None):
    key = (protocol_key or DEFAULT_NURSERY_PROTOCOL_KEY or 'sp1').lower()
    return NURSERY_PROTOCOLS.get(key) or NURSERY_PROTOCOLS[DEFAULT_NURSERY_PROTOCOL_KEY]


def get_nursery_protocol_rows(protocol_key: str | None = None):
    editable_rows = editable_feeding_protocol_rows()
    if editable_rows:
        return editable_rows
    return get_nursery_protocol_meta(protocol_key).get('rows', [])


def get_nursery_protocol_base_population(protocol_key: str | None = None):
    editable_rows = editable_feeding_protocol_rows()
    if editable_rows:
        first_population = editable_rows[0].get('population')
        if first_population:
            return first_population
    return get_nursery_protocol_meta(protocol_key).get('base_population') or NURSERY_PROTOCOL_BASE_POPULATION


def nursery_protocol_key_for_unit(unit) -> str:
    # O protocolo novo é único para o ciclo completo. Mantemos a função para compatibilidade
    # com telas antigas, mas todas as unidades agora usam a mesma tabela base.
    return DEFAULT_NURSERY_PROTOCOL_KEY


def row_stage_number(row):
    return int(row.get('stage_number') or row.get('pl_stage') or _stage_number_from_label(row.get('stage_label'), row.get('cycle_day') or row.get('day') or 1) or 1)


def get_nursery_protocol_row(pl_stage: int | None, protocol_key: str | None = None):
    return get_nursery_protocol_row_by_pl_age(pl_stage, protocol_key=protocol_key)


def get_nursery_protocol_row_by_pl_age(pl_age: int | None, operational_phase: str | None = None, protocol_key: str | None = None):
    """Escolhe a linha pela idade/estágio do PL, não pelo dia dentro da fase.

    Quando há estágios repetidos em mais de uma fase (ex.: PL43/J43 ou J67/E67),
    a fase operacional resolve a prioridade. Se a fase não tiver aquela idade,
    o sistema cai para a linha exata de outra fase para manter o protocolo
    contínuo em transferências antecipadas ou tardias.
    """
    if pl_age is None:
        return None
    try:
        pl_age = int(round(float(pl_age)))
    except (TypeError, ValueError):
        return None

    rows = get_nursery_protocol_rows(protocol_key)
    if not rows:
        return None

    phase = normalize_phase_value(operational_phase)
    phase_rank = {'bercario': 1, 'juvenil': 2, 'engorda': 3}
    ordered_rows = sorted(rows, key=lambda row: (row_stage_number(row), phase_rank.get(normalize_phase_value(row.get('phase')) or '', 9), row.get('phase_day') or 1))

    exact_rows = [row for row in ordered_rows if row_stage_number(row) == pl_age]
    if exact_rows:
        if phase:
            exact_phase = [row for row in exact_rows if normalize_phase_value(row.get('phase')) == phase]
            if exact_phase:
                return exact_phase[0]
        return exact_rows[0]

    phase_rows = [row for row in ordered_rows if normalize_phase_value(row.get('phase')) == phase] if phase else []
    nearest_pool = phase_rows or ordered_rows
    if not nearest_pool:
        return None
    if pl_age <= row_stage_number(nearest_pool[0]):
        return nearest_pool[0]
    if pl_age >= row_stage_number(nearest_pool[-1]):
        return nearest_pool[-1]
    return min(nearest_pool, key=lambda row: abs(row_stage_number(row) - pl_age))


def get_nursery_protocol_row_by_cycle_day(cycle_day: int | None, protocol_key: str | None = None):
    if cycle_day is None:
        return None
    rows = get_nursery_protocol_rows(protocol_key)
    if not rows:
        return None
    ordered_rows = sorted(rows, key=lambda row: row.get('cycle_day') or row.get('day') or 1)
    if cycle_day <= (ordered_rows[0].get('cycle_day') or ordered_rows[0].get('day') or 1):
        return ordered_rows[0]
    if cycle_day >= (ordered_rows[-1].get('cycle_day') or ordered_rows[-1].get('day') or 1):
        return ordered_rows[-1]
    return min(ordered_rows, key=lambda row: abs((row.get('cycle_day') or row.get('day') or 1) - cycle_day))




def get_nursery_protocol_row_by_phase_day(phase: str | None, phase_day: int | None, protocol_key: str | None = None):
    phase = normalize_phase_value(phase) or 'bercario'
    rows = [row for row in get_nursery_protocol_rows(protocol_key) if normalize_phase_value(row.get('phase')) == phase]
    if not rows:
        return None
    ordered_rows = sorted(rows, key=lambda row: row.get('phase_day') or row.get('day') or 1)
    phase_day = int(phase_day or 1)
    first_day = ordered_rows[0].get('phase_day') or ordered_rows[0].get('day') or 1
    last_day = ordered_rows[-1].get('phase_day') or ordered_rows[-1].get('day') or first_day
    if phase_day <= first_day:
        return ordered_rows[0]
    if phase_day >= last_day:
        return ordered_rows[-1]
    exact = next((row for row in ordered_rows if int(row.get('phase_day') or row.get('day') or 1) == phase_day), None)
    return exact or min(ordered_rows, key=lambda row: abs((row.get('phase_day') or row.get('day') or 1) - phase_day))


def get_first_nursery_protocol_row_for_phase(phase: str | None, protocol_key: str | None = None):
    return get_nursery_protocol_row_by_phase_day(phase, 1, protocol_key=protocol_key)

def nursery_cycle_day_for_lot(lot, target_date: date | None, protocol_key: str | None = None) -> int:
    target_date = target_date or local_today()
    rows = get_nursery_protocol_rows(protocol_key)
    if not lot or not getattr(lot, 'start_date', None) or not rows:
        return 1
    ordered_rows = sorted(rows, key=lambda row: row.get('cycle_day') or row.get('day') or 1)
    first_stage = ordered_rows[0].get('stage_number') or ordered_rows[0].get('pl_stage') or 11
    first_cycle_day = ordered_rows[0].get('cycle_day') or ordered_rows[0].get('day') or 1
    entry_stage = getattr(lot, 'entry_pl_stage', None) or first_stage
    stage_offset = max(int(entry_stage or first_stage) - int(first_stage or 0), 0)
    days_since_start = max((target_date - lot.start_date).days, 0)
    cycle_day = first_cycle_day + stage_offset + days_since_start
    last_cycle_day = ordered_rows[-1].get('cycle_day') or ordered_rows[-1].get('day') or cycle_day
    return max(first_cycle_day, min(int(cycle_day), int(last_cycle_day)))


def nursery_pl_age_for_lot(lot, target_date: date | None, protocol_key: str | None = None) -> int:
    """Idade operacional do PL para escolher a linha da tabela de alimentação.

    Diferente do dia da fase: se o lote mudou de berçário para juvenil ou engorda,
    a idade continua avançando a partir da entrada real do lote. Isso evita que
    a tela de uma fase use o mix errado apenas porque a transferência ocorreu em
    data diferente da tabela-modelo.
    """
    target_date = target_date or local_today()
    rows = get_nursery_protocol_rows(protocol_key)
    if not lot or not getattr(lot, 'start_date', None) or not rows:
        return row_stage_number(rows[0]) if rows else 1
    ordered_rows = sorted(rows, key=lambda row: row_stage_number(row))
    first_stage = row_stage_number(ordered_rows[0]) or 11
    entry_stage = getattr(lot, 'entry_pl_stage', None) or first_stage
    try:
        entry_stage = int(entry_stage)
    except (TypeError, ValueError):
        entry_stage = int(first_stage)
    days_since_start = max((target_date - lot.start_date).days, 0)
    pl_age = entry_stage + days_since_start
    min_stage = row_stage_number(ordered_rows[0])
    max_stage = row_stage_number(ordered_rows[-1])
    return max(min_stage, min(int(pl_age), max_stage))





def feeding_table_expected_weight_for_lot(lot: Lot, target_date: date | None = None, age_days: int | None = None, operational_phase: str | None = None):
    """Peso esperado pela idade de PL da tabela base de alimentação editável.

    A biometria deve comparar o peso real contra a linha operacional da tabela
    de alimentação (PL/J/E), não contra um cálculo genérico por dias de fase.
    Assim, se o lote entrou em PL11 e hoje está com 38 dias de fazenda, a linha
    buscada é PL49/J49/E49 conforme a fase operacional disponível.
    """
    if not lot:
        return None
    if target_date is None:
        if age_days is None:
            target_date = local_today()
        elif getattr(lot, 'start_date', None):
            target_date = lot.start_date + timedelta(days=max(int(age_days or 0), 0))
        else:
            target_date = local_today()
    if age_days is None and getattr(lot, 'start_date', None):
        age_days = max((target_date - lot.start_date).days, 0)

    phase = normalize_phase_value(operational_phase)
    if not phase and getattr(lot, 'id', None):
        allocation = (active_allocations_for_lot(lot, on_date=target_date) or [None])[-1]
        phase = allocation_operational_phase(allocation) or normalize_phase_value(getattr(lot, 'phase', None))
    phase = phase or normalize_phase_value(getattr(lot, 'phase', None))

    pl_age = nursery_pl_age_for_lot(lot, target_date, protocol_key=DEFAULT_NURSERY_PROTOCOL_KEY)
    row = get_nursery_protocol_row_by_pl_age(pl_age, operational_phase=phase, protocol_key=DEFAULT_NURSERY_PROTOCOL_KEY)
    if not row:
        return None

    expected_weight = parse_float(row.get('individual_weight_g') or row.get('weight_g'), None)
    if expected_weight is None:
        return None
    return {
        'expected_weight_g': round(expected_weight, 4),
        'confidence': 70,
        'similar_cases': 0,
        'source': 'Tabela base de alimentação por idade de PL',
        'pl_age': pl_age,
        'stage_label': row.get('stage_label') or f'PL{pl_age}',
        'standard_feed_rate_pct': row.get('feed_rate_pct'),
        'standard_survival_pct': row.get('survival_pct'),
        'standard_fcr': row.get('estimated_fcr'),
    }

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


def nursery_next_active_feed_factor(previous_factor, adjustment_pct):
    """Calcula o fator ativo depois de um novo lançamento.

    Regras de operação:
    - ajuste vazio/None: mantém a correção ativa anterior;
    - ajuste 0%: zera e volta para a tabela base;
    - ajuste positivo/negativo: aplica sobre a correção já ativa.
      Ex.: fator 1,10 com novo +10% vira 1,21.
    """
    try:
        factor = float(previous_factor or 1.0)
    except (TypeError, ValueError):
        factor = 1.0
    if adjustment_pct is None:
        return round(factor, 6)
    try:
        adjustment_pct = float(adjustment_pct)
    except (TypeError, ValueError):
        return round(factor, 6)
    if abs(adjustment_pct) < 0.000001:
        return 1.0
    next_factor = factor * nursery_adjustment_pct_factor(adjustment_pct)
    return round(max(next_factor, 0.0), 6)


def nursery_adjustment_pct_from_form(raw_adjustment, intestinal_score):
    """Interpreta o campo de ajuste incremental da tela.

    Campo vazio mantém a correção ativa. Quando há score, o sistema só cria
    ajuste automático se a sugestão for diferente de 0. Assim score na faixa
    "mantém" não zera a correção por acidente. Para zerar, o usuário digita 0.
    """
    raw_text = str(raw_adjustment or '').strip()
    if raw_text:
        return parse_float(raw_text)
    if intestinal_score is not None:
        suggested = nursery_score_adjustment_pct(intestinal_score)
        if abs(suggested) >= 0.000001:
            return suggested
    return None


def apply_nursery_adjustment_state_from_request(entry):
    previous_adjustment = nursery_cumulative_adjustments(entry.lot_id, entry.feed_date)
    entry.intestinal_score = parse_float(request.form.get('intestinal_score'))
    entry.score_adjustment_pct = nursery_adjustment_pct_from_form(
        request.form.get('score_adjustment_pct'),
        entry.intestinal_score,
    )
    entry.active_feed_factor = nursery_next_active_feed_factor(
        previous_adjustment['factor'],
        entry.score_adjustment_pct,
    )
    return previous_adjustment


def nursery_cumulative_adjustments(lot_id: int | None, target_date: date):
    """Retorna a correção de ração ativa para a data informada.

    A correção fica gravada como fator ativo no último lançamento anterior do
    lote. Um novo +10% não substitui o fator: ele aplica sobre o valor já
    corrigido. Ex.: +10% ativo e novo +10% = +21% sobre a tabela base.
    Campo vazio mantém o fator; 0% zera a correção ativa.
    """
    if not lot_id or not target_date:
        return {'factor': 1.0, 'events': []}

    record = NurseryFeeding.query.filter(
        NurseryFeeding.lot_id == lot_id,
        NurseryFeeding.feed_date < target_date,
        or_(
            NurseryFeeding.active_feed_factor.isnot(None),
            NurseryFeeding.score_adjustment_pct.isnot(None),
            NurseryFeeding.intestinal_score.isnot(None),
        ),
    ).order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc()).first()

    if not record:
        return {'factor': 1.0, 'events': []}

    adjustment_pct = record.score_adjustment_pct
    if adjustment_pct is not None:
        # Ajuste manual/por score é sempre incremental sobre a correção que
        # estava ativa antes deste lançamento. Recalcular aqui evita que um
        # "Salvar todas as rações do dia" posterior sobrescreva um ajuste
        # negativo já informado no card individual.
        previous = nursery_cumulative_adjustments(lot_id, record.feed_date)
        factor = nursery_next_active_feed_factor(previous.get('factor', 1.0), adjustment_pct)
    elif record.active_feed_factor is not None:
        factor = float(record.active_feed_factor or 1.0)
    else:
        # Compatibilidade com lançamentos antigos sem active_feed_factor: aplica
        # o percentual sugerido pelo score sobre a correção anterior, mantendo a
        # mesma regra incremental usada nos lançamentos atuais.
        adjustment_pct = nursery_record_adjustment_pct(record)
        previous = nursery_cumulative_adjustments(lot_id, record.feed_date)
        factor = nursery_next_active_feed_factor(previous.get('factor', 1.0), adjustment_pct)

    return {
        'factor': factor,
        'events': [{
            'date': record.feed_date,
            'score': record.intestinal_score,
            'adjustment_pct': adjustment_pct,
            'adjustment_label': nursery_adjustment_pct_label(adjustment_pct) if adjustment_pct is not None else 'mantido',
            'factor': factor,
            'factor_label': nursery_score_factor_label(factor),
            'cumulative_factor': factor,
            'cumulative_label': nursery_score_factor_label(factor),
        }],
    }


def build_even_schedule(total_day_g: int, feedings_per_day: int):
    if not feedings_per_day or total_day_g <= 0:
        return []
    base_value = total_day_g // feedings_per_day
    remainder = total_day_g % feedings_per_day
    return [base_value + (1 if idx < remainder else 0) for idx in range(feedings_per_day)]


def entry_phase_label(entry):
    phase = feeding_entry_operational_phase(entry)
    if phase == 'juvenil':
        return 'juvenil'
    if phase == 'engorda':
        return 'engorda'
    return 'berçário'


def build_nursery_management_note_block(entry, mix_label=None):
    phase_label = entry_phase_label(entry)
    lines = [
        f'[Integração {phase_label}]',
        f'Integração automática da alimentação de {phase_label}.',
        f'Origem ID: {entry.id}',
    ]
    if mix_label:
        lines.append(f'Produto do mix: {mix_label}')
    if entry.intestinal_score is not None:
        lines.append(f'Score intestinal: {entry.intestinal_score}')
    if entry.score_adjustment_pct is not None:
        lines.append(f'Ajuste de ração para o próximo dia: {nursery_adjustment_pct_label(entry.score_adjustment_pct)}')
    if (entry.notes or '').strip():
        lines.append(f'Observações do {phase_label}: {(entry.notes or '').strip()}')
    lines.append(f'[/Integração {phase_label}]')
    return '\n'.join(lines)


def build_nursery_water_management_note_block(entry, water_items=None):
    phase_label = entry_phase_label(entry)
    water_items = water_items or []
    lines = [
        f'[Integração {phase_label}]',
        f'Integração automática dos aditivos/insumos de água do {phase_label}.',
        f'Origem ID: {entry.id}',
        f'Tipo: manejo da água / insumos marcados na Alimentação {phase_label}',
    ]
    for item in water_items:
        qty = item.get('stock_quantity') if item.get('stock_quantity') is not None else item.get('quantity')
        unit_label = item.get('stock_measure_unit') or item.get('measure_unit') or ''
        if qty is not None:
            lines.append(f"Item utilizado: {item.get('label')} — {format_decimal_pt(qty)} {unit_label}".strip())
        else:
            lines.append(f"Item utilizado: {item.get('label')}")
    if (entry.notes or '').strip():
        lines.append(f'Observações do {phase_label}: {(entry.notes or '').strip()}')
    lines.append(f'[/Integração {phase_label}]')
    return '\n'.join(lines)


def nursery_water_item_form_key(item, idx):
    """Chave estável do checkbox de aditivo do protocolo do dia."""
    label = normalize_text(item.get('label') or item.get('source_label') or 'item').replace(' ', '_')
    scheduled = (item.get('scheduled_time') or '').replace(':', '')
    unit_label = normalize_text(item.get('measure_unit') or '')
    quantity = item.get('quantity')
    if quantity is None:
        quantity_label = 'rotina'
    else:
        try:
            quantity_label = f"{float(quantity):g}".replace('.', '_')
        except (TypeError, ValueError):
            quantity_label = str(quantity).replace('.', '_')
    return f'{idx}:{scheduled}:{label}:{quantity_label}:{unit_label}'


def nursery_water_item_is_stock_item(item):
    return (
        (item.get('category') or 'aditivo') in ('aditivo', 'troca_agua')
        and item.get('quantity') is not None
        and (item.get('quantity') or 0) > 0
    )


def nursery_entry_water_items(entry):
    if not entry or not (entry.water_items_json or '').strip():
        return []
    try:
        payload = json.loads(entry.water_items_json or '[]')
    except (TypeError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def nursery_entry_water_item_keys(entry):
    return {str(item.get('form_key')) for item in nursery_entry_water_items(entry) if item.get('form_key')}


def nursery_water_items_for_form(plan, entry=None):
    water_items = []
    stored_keys = nursery_entry_water_item_keys(entry) if entry else set()
    has_saved_selection = bool(entry and (entry.water_items_json or '').strip())
    for idx, raw_item in enumerate(plan.get('water_items', []) if plan else []):
        item = dict(raw_item)
        item['form_key'] = nursery_water_item_form_key(item, idx)
        item['is_stock_item'] = nursery_water_item_is_stock_item(item)
        item['is_selected'] = (item['form_key'] in stored_keys) if has_saved_selection else item['is_stock_item']
        item['stock_product_label'] = ''
        if item['is_stock_item']:
            product = find_or_create_supply_product_for_protocol(item.get('label'), measure_unit=item.get('measure_unit') or 'g', create_missing=False)
            item['stock_product_label'] = product.full_name if product else nursery_water_supply_alias_labels(item.get('label'))[0]
        water_items.append(item)
    return water_items


def selected_nursery_water_items_for_plan(plan, selected_keys=None):
    """Retorna os aditivos/insumos de água escolhidos para um plano de alimentação.

    Quando selected_keys é None, a seleção segue o padrão operacional da tela:
    todo item de estoque do protocolo do dia já vem marcado para baixar no Manejo.
    Isso permite o botão "Salvar todas as rações do dia" fazer exatamente o mesmo
    lançamento que seria feito clicando em cada card individual, sem mexer na lógica
    de probiótico, LOTHAR, melaço, calda iodada ou controles.
    """
    selected_items = []
    selected_keys = {str(key) for key in selected_keys} if selected_keys is not None else None
    for item in nursery_water_items_for_form(plan, entry=None):
        if not item.get('is_stock_item'):
            continue
        if selected_keys is not None:
            is_selected = item.get('form_key') in selected_keys
        else:
            is_selected = bool(item.get('is_selected'))
        if not is_selected:
            continue
        selected_items.append({
            'form_key': item.get('form_key'),
            'label': item.get('label'),
            'source_label': item.get('source_label'),
            'category': item.get('category') or 'aditivo',
            'quantity': item.get('quantity'),
            'measure_unit': item.get('measure_unit') or 'g',
            'scheduled_time': item.get('scheduled_time'),
            'priority': item.get('priority') or 'alta',
        })
    return selected_items


def selected_nursery_water_items_from_request(plan):
    selected_keys = set(request.form.getlist('water_item_keys'))
    return selected_nursery_water_items_for_plan(plan, selected_keys=selected_keys)


def nursery_water_supply_entries(water_items):
    entries = []
    for item in water_items or []:
        if not nursery_water_item_is_stock_item(item):
            continue
        product = find_or_create_supply_product_for_protocol(
            item.get('label') or item.get('source_label') or 'Manejo da água',
            measure_unit=item.get('measure_unit') or 'g',
            create_missing=True,
        )
        if not product:
            continue
        stock_quantity = convert_quantity_between_units(
            item.get('quantity'),
            item.get('measure_unit') or product.measure_unit,
            product.measure_unit,
        )
        if stock_quantity is None:
            continue
        enriched = dict(item)
        enriched['stock_quantity'] = stock_quantity
        enriched['stock_measure_unit'] = product.measure_unit
        entries.append({
            'product': product,
            'quantity': stock_quantity,
            'notes': f"Alimentação fases iniciais: {item.get('label')} ({format_decimal_pt(item.get('quantity'))} {item.get('measure_unit') or ''})".strip(),
            'item': enriched,
        })
    return entries


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

    usage_ids = [usage.id for usage in ManagementSupplyUsage.query.filter_by(management_id=record.id).all()]
    if usage_ids:
        SupplyInventory.query.filter(
            SupplyInventory.source_type == 'manejo_insumo',
            SupplyInventory.source_ref_id.in_(usage_ids),
        ).delete(synchronize_session=False)
        ManagementSupplyUsage.query.filter(ManagementSupplyUsage.id.in_(usage_ids)).delete(synchronize_session=False)

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


def nursery_feed_stock_alias_labels(label: str):
    """Retorna nomes equivalentes do estoque para itens técnicos da tabela do berçário."""
    normalized = normalize_text(label)
    aliases = []
    for key, values in NURSERY_FEED_STOCK_ALIASES.items():
        if normalized == normalize_text(key):
            # Quando existe uma equivalência operacional cadastrada, ela vem antes
            # do texto original. Ex.: SAMARIA 40#2 deve procurar AquaVita 40#2
            # antes de aceitar uma Samaria antiga no estoque.
            aliases.extend(values)
            break
    aliases.append(label)
    # A tabela nova já usa os nomes reais do estoque para as trituradas, mas mantemos
    # esta ponte para compatibilidade com linhas antigas chamadas "Triturada 1/2".
    return [item for item in dict.fromkeys(aliases) if (item or '').strip()]


def nursery_feed_size_tokens(value: str):
    """Extrai identificadores de tamanho/grupo usados nas rações do protocolo.

    Esta função é a trava principal contra baixa de estoque na ração errada.
    Ela separa rações que compartilham a mesma marca, mas não são equivalentes:
    AquaVita 40#1, AquaVita/Samaria 40#2, AquaVita JUV. 38, AquaVita 35
    e IRCA/CarciMax 30. Assim, a busca nunca deve escolher AquaVita 35 só
    porque o texto "AquaVita" aparece no protocolo da 40#1.
    """
    normalized = normalize_text(value)
    tokens = nursery_feed_alias_tokens(value)
    size_tokens = set()

    if 'nutrisfera' in tokens or 'nutrisphera' in tokens:
        size_tokens.update(token for token in tokens if token in {'225', '450'})

    # Nomes técnicos antigos da tabela.
    if 'triturada' in tokens or 'triturado' in tokens:
        if all(token in tokens for token in {'500', '900'}) or 'triturada 1' in normalized:
            size_tokens.add('40_1')
        if all(token in tokens for token in {'800', '1200'}) or 'triturada 2' in normalized:
            size_tokens.add('40_2')

    # AquaVita 40/1: 0,5-1,0 mm. Evita casar com 40/2, 38 ou 35.
    if (
        'aquavita 40 1' in normalized
        or 'aquavita 40 01' in normalized
        or ('aquavita' in tokens and '40' in tokens and ('#1' in value or '/1' in value or '01' in tokens))
    ):
        if 'aquavita 40 2' not in normalized and 'aquavita 40 02' not in normalized:
            size_tokens.add('40_1')

    # AquaVita 40/2 / Samaria 40#2 / SM Starter 400 #02: 1,0-1,8 mm.
    if (
        'aquavita 40 2' in normalized
        or 'aquavita 40 02' in normalized
        or 'samaria 40 2' in normalized
        or 'samaria 40 02' in normalized
        or ('juvenil' in tokens and '40' in tokens and ('02' in tokens or '2' in tokens))
        or ('starter' in tokens and '400' in tokens)
    ):
        size_tokens.discard('40_1')
        size_tokens.add('40_2')

    # Rações maiores do juvenil/engorda. Estas precisam ser incompatíveis com 40#1/40#2.
    if ('aquavita' in tokens and '38' in tokens) or ('juv' in tokens and '38' in tokens):
        size_tokens.add('38_15')
    if 'aquavita' in tokens and '35' in tokens:
        size_tokens.add('35_20')
    if (('irca' in tokens or 'carcimax' in tokens) and '30' in tokens) or 'carcimax 30' in normalized:
        size_tokens.add('30_24')

    return size_tokens


def nursery_feed_size_compatible(label: str, product_text: str) -> bool:
    label_sizes = nursery_feed_size_tokens(label)
    product_sizes = nursery_feed_size_tokens(product_text)
    if label_sizes and product_sizes and not label_sizes.intersection(product_sizes):
        return False
    return True


def find_nursery_feed_product_by_alias(label: str, products, exclude_product_id=None):
    alias_norms = [normalize_text(item) for item in nursery_feed_stock_alias_labels(label)]
    alias_norms = [item for item in alias_norms if item]
    if not alias_norms:
        return None

    best = None
    for product in products:
        if exclude_product_id is not None and product.id == exclude_product_id:
            continue
        product_display_text = f'{product.full_name} {product.brand or ""} {product.feed_type or ""} {product.technical_summary or ""}'
        if not nursery_feed_size_compatible(label, product_display_text):
            continue
        product_norms = [
            normalize_text(product.full_name),
            normalize_text(product.brand or ''),
            normalize_text(f'{product.brand} {product.feed_type} {product.technical_summary or ""}'),
        ]
        product_norms = [item for item in product_norms if item]
        for alias_idx, alias_norm in enumerate(alias_norms):
            # Correspondência exata ou alias contido no nome completo do produto.
            # Não aceitamos mais o inverso (ex.: produto "AQUAVITA" dentro de
            # alias "AQUAVITA 40#1"), porque isso fazia a AquaVita 35 vencer
            # por saldo de estoque mesmo quando a linha do protocolo era 40#1.
            match_quality = 0
            for prod_norm in product_norms:
                if alias_norm == prod_norm:
                    match_quality = max(match_quality, 3)
                elif alias_norm in prod_norm:
                    match_quality = max(match_quality, 2)
            if match_quality:
                # Avalia todos os aliases do mesmo produto, não só o primeiro que bate.
                # Assim "SAMARIA 40#2" pode preferir "AquaVita 40/2 (1,0-1,8mm)"
                # quando essa equivalência estiver antes na lista operacional.
                candidate = (match_quality, -alias_idx, product.active, nursery_product_stock(product.id), product.full_name.lower(), product)
                if best is None or candidate > best:
                    best = candidate
    return best[-1] if best else None


def nursery_protocol_product_names():
    names = set()
    for protocol in NURSERY_PROTOCOLS.values():
        for row in protocol.get('rows', []):
            for item in row.get('mixes', []):
                name = (item.get('label') or '').strip()
                if name:
                    names.add(normalize_text(name))
    for alias, values in NURSERY_FEED_STOCK_ALIASES.items():
        names.add(normalize_text(alias))
        for value in values:
            names.add(normalize_text(value))
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
    product_display_text = f'{product.full_name} {product_text}'
    if not nursery_feed_size_compatible(label, product_display_text):
        return -999, False
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

    mapped_product = get_protocol_feed_map(label)
    if mapped_product and (exclude_product_id is None or mapped_product.id != exclude_product_id):
        return mapped_product

    protocol_label = normalized_label in nursery_protocol_product_names() or normalized_label.startswith('mem ')
    products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()

    alias_product = find_nursery_feed_product_by_alias(label, products, exclude_product_id=exclude_product_id)
    if alias_product:
        return alias_product

    scored_real_nursery = []
    scored_any = []

    for product in products:
        if exclude_product_id is not None and product.id == exclude_product_id:
            continue

        score, product_is_nursery = nursery_product_match_score(label, product)
        is_auto_protocol = is_auto_nursery_protocol_product(product)

        if score < 0:
            continue

        if product_is_nursery and not is_auto_protocol:
            scored_real_nursery.append((score, nursery_product_stock(product.id), product.active, product.full_name.lower(), product))

        # Produtos técnicos do protocolo não devem ganhar
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

    # Se a linha da tabela traz um nome técnico antigo, não cria mais produto
    # com esse nome. Para nomes reais definidos pelo estoque/protocolo, cria o produto correto.
    real_protocol_stock_labels = {
        normalize_text('NutriSphera 225'),
        normalize_text('NUTRISFERA - BERÇÁRIO 225'),
        normalize_text('NUTRISFERA BERCARIO 225'),
        normalize_text('NutriSphera 450'),
        normalize_text('NUTRISFERA - BERÇÁRIO 450'),
        normalize_text('NUTRISFERA BERCARIO 450'),
        normalize_text('AQUAVITA 40#1'),
        normalize_text('AQUAVITA 40/1'),
        normalize_text('AQUAVITA 40#2'),
        normalize_text('AQUAVITA 40/2'),
        normalize_text('AquaVita 40/2 (1,0-1,8mm)'),
        normalize_text('SAMARIA 40#2'),
        normalize_text('JUVENIL 40 - SM starter 400 E #02'),
        normalize_text('JUVENIL 40 - SM starter 400'),
        normalize_text('JUVENIL 40 - #02'),
        normalize_text('MeM 200-300'),
        normalize_text('MEM 200-300'),
        normalize_text('MeM 300-500'),
        normalize_text('MEM 300-500'),
    }
    product_name = label if normalized_label in real_protocol_stock_labels else ('Ração berçário' if protocol_label else label)
    product = FeedProduct(brand=product_name, feed_type='', active=True, notes='Criado automaticamente pelo protocolo de berçário.')
    db.session.add(product)
    db.session.flush()
    return product


def repair_protocol_feed_maps():
    """Limpa vínculos incompatíveis da tabela base e tenta religar ao produto correto.

    O problema visto no Manejo Diário normalmente nasce de uma dessas duas fontes:
    1) busca por similaridade ampla demais;
    2) vínculo antigo da Tabela Base salvo em produto de outra granulometria.
    Esta rotina corrige a segunda fonte na inicialização, sem criar produto novo.
    """
    if not has_app_context():
        return
    changed = False
    try:
        mappings = FeedingProtocolFeedMap.query.order_by(FeedingProtocolFeedMap.id.asc()).all()
    except Exception:
        return

    for mapping in mappings:
        label = (mapping.protocol_label or '').strip()
        current_product = db.session.get(FeedProduct, mapping.feed_product_id) if mapping.feed_product_id else None
        if mapping.feed_product_id and not protocol_feed_mapping_is_compatible(label, current_product):
            mapping.feed_product_id = None
            mapping.updated_at = datetime.utcnow()
            changed = True

        if not mapping.feed_product_id:
            # Primeiro tenta reaproveitar o vínculo de uma coluna equivalente da Tabela Base
            # antes de cair na busca por nome/estoque.
            product = get_protocol_feed_map(label) or find_or_create_nursery_feed_product(label, create_missing=False)
            if product and protocol_feed_mapping_is_compatible(label, product):
                mapping.feed_product_id = product.id
                mapping.updated_at = datetime.utcnow()
                changed = True

    if changed:
        db.session.commit()


def auto_nursery_mix_label_from_management(record) -> str | None:
    notes = record.notes or ''
    if '[Integração' not in notes or 'Produto do mix:' not in notes:
        return None
    match = re.search(r'^Produto do mix:\s*(.+?)\s*$', notes, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def repair_auto_nursery_management_feed_links():
    """Reaponta baixas antigas do Manejo Diário para a ração correta do mix.

    Só mexe em lançamentos criados pela integração da alimentação, identificados
    pelas notas com "Produto do mix:". Manejos manuais ficam intactos.
    """
    if not has_app_context():
        return
    try:
        records = DailyManagement.query.filter(
            DailyManagement.feed_offered_kg > 0,
            DailyManagement.notes.contains('Produto do mix:'),
        ).order_by(DailyManagement.id.asc()).all()
    except Exception:
        return

    changed = False
    for record in records:
        label = auto_nursery_mix_label_from_management(record)
        if not label:
            continue
        product = find_or_create_nursery_feed_product(label, create_missing=False)
        if not product or not protocol_feed_mapping_is_compatible(label, product):
            continue
        if record.feed_product_id == product.id:
            continue
        movement = get_management_feed_movement(record.id)
        sync_management_feed_movement(record, product, record.feed_offered_kg or 0, existing_movement=movement)
        record.updated_at = datetime.utcnow()
        changed = True

    if changed:
        db.session.commit()


def resolve_nursery_mix_label(label: str) -> str:
    label = (label or 'Ração berçário').strip()
    mapped_product = get_protocol_feed_map(label)
    if mapped_product:
        return mapped_product.full_name
    product = find_or_create_nursery_feed_product(label, create_missing=False)
    if product and not is_auto_nursery_protocol_product(product):
        return product.full_name
    # Mantém o nome real vindo da planilha para o operador saber exatamente qual ração usar,
    # mesmo quando ainda não há produto vinculado no estoque. A baixa só acontece se o estoque estiver vinculado.
    return label


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
    target_date = target_date or local_today()
    if not lot or not unit or not lot.start_date:
        return None

    protocol_key = nursery_protocol_key_for_unit(unit)
    protocol_meta = get_nursery_protocol_meta(protocol_key)
    protocol_rows = get_nursery_protocol_rows(protocol_key)
    if not protocol_rows:
        return None

    allocation = find_active_allocation(lot.id, unit.id, target_date) if lot.id and unit.id else None
    operational_phase = allocation_operational_phase(allocation) or normalize_phase_value(getattr(unit, 'phase', None)) or normalize_phase_value(getattr(lot, 'phase', None)) or 'bercario'

    transfer_marker = Transfer.query.filter(
        Transfer.source_lot_id == lot.id,
        Transfer.destination_unit_id == unit.id,
        Transfer.transfer_date <= target_date,
    ).order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).first()

    # A fase real vem da alocação/transferência, mas a linha da ração é escolhida
    # pela idade do PL/estágio do protocolo. Isso impede que o mix seja definido
    # apenas pelo dia dentro da fase e confunda 40/2 com rações de camarão maior.
    phase_start_date = nursery_phase_start_date(lot, unit, allocation, transfer_marker, target_date)
    phase_day = phase_day_from_dates(phase_start_date, target_date)
    pl_age_today = nursery_pl_age_for_lot(lot, target_date, protocol_key=protocol_key)
    row = get_nursery_protocol_row_by_pl_age(pl_age_today, operational_phase, protocol_key=protocol_key)
    if not row:
        cycle_day_fallback = nursery_cycle_day_for_lot(lot, target_date, protocol_key=protocol_key)
        row = get_nursery_protocol_row_by_cycle_day(cycle_day_fallback, protocol_key=protocol_key)
    if not row:
        row = get_nursery_protocol_row_by_phase_day(operational_phase, phase_day, protocol_key=protocol_key)
    if not row:
        return None

    cycle_day = int(row.get('cycle_day') or row.get('day') or phase_day)
    protocol_phase = normalize_phase_value(row.get('phase')) or operational_phase
    feedings_per_day = feedings_per_day_for_phase(operational_phase, fallback=row.get('feedings_per_day'))
    stage_label = row.get('stage_label') or (f"PL{row.get('pl_stage')}" if row.get('pl_stage') else f"Dia {phase_day}")

    marker_population_reference = None
    marker_label = None
    factor = None

    if allocation and allocation.quantity_allocated:
        marker_date = allocation.start_date if allocation.start_date and allocation.start_date <= target_date else target_date
        marker_pl_age = nursery_pl_age_for_lot(lot, marker_date, protocol_key=protocol_key)
        marker_row = get_nursery_protocol_row_by_pl_age(marker_pl_age, operational_phase, protocol_key=protocol_key) or row
        marker_population_reference = marker_row.get('population') or get_nursery_protocol_base_population(protocol_key) or 1
        factor = (allocation.quantity_allocated or 0) / float(marker_population_reference or 1)
        marker_stage_label = marker_row.get('stage_label') or f"dia 1 da fase {phase_label(operational_phase)}"
        marker_label = (
            f"alocação ativa em {unit.name}: "
            f"{format_integer_pt(allocation.quantity_allocated or 0)} PL desde {marker_date.strftime('%d/%m/%Y')} "
            f"na fase {phase_label(operational_phase)}, idade PL{marker_pl_age}, linha-base {marker_stage_label}"
        )
    elif transfer_marker and transfer_marker.transferred_qty:
        marker_date = transfer_marker.transfer_date or target_date
        marker_pl_age = nursery_pl_age_for_lot(lot, marker_date, protocol_key=protocol_key)
        marker_row = get_nursery_protocol_row_by_pl_age(marker_pl_age, operational_phase, protocol_key=protocol_key) or row
        marker_population_reference = marker_row.get('population') or get_nursery_protocol_base_population(protocol_key) or 1
        factor = (transfer_marker.transferred_qty or 0) / float(marker_population_reference or 1)
        marker_stage_label = marker_row.get('stage_label') or f"dia 1 da fase {phase_label(operational_phase)}"
        marker_label = (
            f"transferência real #{transfer_marker.id}: "
            f"{format_integer_pt(transfer_marker.transferred_qty or 0)} PL para {phase_label(operational_phase)}, "
            f"idade PL{marker_pl_age}, linha-base {marker_stage_label}"
        )
    else:
        marker_pl_age = nursery_pl_age_for_lot(lot, target_date, protocol_key=protocol_key)
        marker_row = get_nursery_protocol_row_by_pl_age(marker_pl_age, operational_phase, protocol_key=protocol_key) or row or protocol_rows[0]
        marker_population_reference = marker_row.get('population') or get_nursery_protocol_base_population(protocol_key) or 1
        factor = (lot.initial_count or 0) / float(marker_population_reference or 1)

    def scaled(value):
        return int(round((value or 0) * factor))

    base_total_day_g = scaled(row['total_day_g'])
    correction_factor = cumulative_factor or 1.0
    correction_label = nursery_score_factor_label(correction_factor)
    correction_events = correction_events or []

    row_population = row.get('population')
    if not row_population:
        row_population = (get_nursery_protocol_base_population(protocol_key) or 0) * (row['survival_pct'] / 100.0)
    projected_population = int(round((row_population or 0) * factor))
    biomass_kg = round((projected_population * row['individual_weight_g']) / 1000.0, 2)
    base_mixes = [
        {'label': resolve_nursery_mix_label(item.get('label', 'Ração protocolo')), 'grams': scaled(item.get('grams', 0))}
        for item in row.get('mixes', [])
    ]
    base_mixes = consolidate_feed_mixes(base_mixes)
    mixes = [
        {'label': item['label'], 'grams': int(round(item['grams'] * correction_factor))}
        for item in base_mixes
    ]
    mixes = consolidate_feed_mixes(mixes)
    total_day_g = sum(item['grams'] for item in mixes) if mixes else int(round(base_total_day_g * correction_factor))

    portion_values = build_even_schedule(total_day_g, feedings_per_day)
    per_feeding_g = int(round(total_day_g / feedings_per_day)) if feedings_per_day else 0
    schedule_times = build_feeding_time_labels(feedings_per_day, first_time_label='08:00')
    schedule = []
    for idx, time_label in enumerate(schedule_times):
        schedule.append({'time': time_label, 'grams': portion_values[idx] if idx < len(portion_values) else per_feeding_g})

    phase_name = phase_label(operational_phase)
    protocol_phase_name = phase_label(protocol_phase)
    interval_label = feeding_interval_label(feedings_per_day)

    # Na Engorda, após a transferência, não entra manejo de água do protocolo
    # (probiótico/AQUAPRO, LOTHAR, melaço etc.). A ração continua seguindo a
    # tabela proporcionalmente, sem reiniciar o ciclo.
    raw_water_items = row.get('water_items', [])
    water_items = [] if operational_phase == 'engorda' else raw_water_items

    days_at_farm = inclusive_day_count(lot.start_date, target_date)
    phase_start_date = nursery_phase_start_date(lot, unit, allocation, transfer_marker, target_date)
    days_in_phase = inclusive_day_count(phase_start_date, target_date)
    phase_duration_label = {
        'bercario': 'no berçário',
        'juvenil': 'no juvenil',
        'engorda': 'na engorda',
    }.get(operational_phase, f"na fase {phase_name}")

    message_lines = [
        f"*{unit.name}* — Lote {lot.lot_code}",
        f"Data: {target_date.strftime('%d/%m/%Y')}",
        f"Tempo: {pluralize_day_pt(days_at_farm)} na fazenda · {pluralize_day_pt(days_in_phase)} {phase_duration_label}",
        f"Total do dia: {total_day_g:,} g".replace(',', '.'),
        '',
        '*Mix do dia*',
    ]
    for item in mixes or [{'label': 'Sem mistura cadastrada', 'grams': 0}]:
        message_lines.append(f"- {item['label']}: {item['grams']:,} g".replace(',', '.'))
    if water_items:
        message_lines.extend(['', '*Manejo da água / controles*'])
        for item in water_items:
            quantity = item.get('quantity')
            if quantity is None:
                message_lines.append(f"- {item.get('label')}")
            else:
                message_lines.append(f"- {item.get('label')}: {format_decimal_pt(quantity)} {item.get('measure_unit', '')}".strip())
    message_lines.extend(['', f'*Porções em 24h · início 08:00 · intervalo {interval_label}*'])
    for item in schedule:
        message_lines.append(f"- {item['time']} — {item['grams']:,} g".replace(',', '.'))

    return {
        'unit': unit,
        'lot': lot,
        'target_date': target_date,
        'day_index': phase_day,
        'phase_day': phase_day,
        'cycle_day': cycle_day,
        'pl_age_today': pl_age_today,
        'stage_today': row.get('stage_number') or row.get('pl_stage') or pl_age_today or cycle_day,
        'stage_label': stage_label,
        'operational_phase': operational_phase,
        'operational_phase_label': phase_name,
        'protocol_phase': protocol_phase,
        'protocol_phase_label': protocol_phase_name,
        'flexible_phase_transition': FULL_CYCLE_PROTOCOL_FLEXIBLE_PHASE_TRANSITIONS,
        'phase_mismatch': operational_phase != protocol_phase,
        'protocol_key': protocol_key,
        'protocol_name': protocol_meta.get('name', protocol_key.upper()),
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
        'water_items': water_items,
        'feedings_per_day': feedings_per_day,
        'feeding_interval_label': interval_label,
        'per_feeding_g': per_feeding_g,
        'per_feeding_min_g': min((item['grams'] for item in schedule), default=0),
        'per_feeding_max_g': max((item['grams'] for item in schedule), default=0),
        'schedule': schedule,
        'message_text': '\n'.join(message_lines),
    }


def build_stage_feed_digest_for_date(target_date: date | None = None, phase: str = 'bercario'):
    target_date = target_date or local_today()
    phase = normalize_phase_value(phase) or 'bercario'
    plans = []
    seen = set()
    allocations = active_allocations_for_operational_phase(phase, on_date=target_date)
    for allocation in allocations:
        unit = allocation.unit
        lot = allocation.lot
        if not unit or not unit.active or not lot or lot.status != 'ativo':
            continue
        key = (unit.id, lot.id)
        if key in seen:
            continue
        seen.add(key)
        entry = NurseryFeeding.query.filter_by(feed_date=target_date, unit_id=unit.id, lot_id=lot.id).order_by(NurseryFeeding.id.desc()).first()
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
            plan['allocation'] = allocation
            plans.append(plan)
    return plans



FEED_PREPARATION_PHASES = [
    ('bercario', 'Berçário'),
    ('juvenil', 'Juvenil'),
    ('engorda', 'Engorda'),
]


def kg_from_grams(value):
    return round((value or 0) / 1000.0, 3)


def add_feed_preparation_total(bucket, label, grams, phase=None, unit_name=None, lot_code=None, target_date=None):
    label = re.sub(r'\s+', ' ', (label or 'Ração sem identificação').strip()) or 'Ração sem identificação'
    row = bucket.setdefault(label, {
        'label': label,
        'total_g': 0,
        'phase_totals': defaultdict(int),
        'units': set(),
        'lots': set(),
        'dates': set(),
    })
    grams = int(round(grams or 0))
    row['total_g'] += grams
    if phase:
        row['phase_totals'][phase] += grams
    if unit_name:
        row['units'].add(unit_name)
    if lot_code:
        row['lots'].add(lot_code)
    if target_date:
        row['dates'].add(target_date)
    return row


def add_preparation_additive_total(bucket, item, phase=None, unit_name=None, lot_code=None, target_date=None):
    if not item:
        return None
    label = re.sub(r'\s+', ' ', (item.get('label') or item.get('source_label') or 'Aditivo sem identificação').strip()) or 'Aditivo sem identificação'
    unit = (item.get('measure_unit') or '').strip() or 'un'
    key = (label, unit)
    row = bucket.setdefault(key, {
        'label': label,
        'measure_unit': unit,
        'total_quantity': 0.0,
        'phase_totals': defaultdict(float),
        'units': set(),
        'lots': set(),
        'dates': set(),
    })
    qty = parse_float(item.get('quantity'), 0) or 0
    row['total_quantity'] += qty
    if phase:
        row['phase_totals'][phase] += qty
    if unit_name:
        row['units'].add(unit_name)
    if lot_code:
        row['lots'].add(lot_code)
    if target_date:
        row['dates'].add(target_date)
    return row


def feed_preparation_unit_key(plan):
    unit = plan.get('unit')
    lot = plan.get('lot')
    return (getattr(unit, 'id', None), getattr(lot, 'id', None))


def format_feed_preparation_mix_line(mixes):
    parts = []
    for item in mixes or []:
        grams = int(round(item.get('grams') or 0))
        if grams <= 0:
            continue
        parts.append(f"{item.get('label')}: {format_decimal_pt(kg_from_grams(grams), 3)} kg")
    return '; '.join(parts) or 'Sem mix cadastrado'


def build_feed_preparation_whatsapp_text(plan_data):
    start_date = plan_data['start_date']
    end_date = plan_data['end_date']
    lines = [
        f"*Preparo da ração — {start_date.strftime('%d/%m')} a {end_date.strftime('%d/%m/%Y')}*",
        f"Total da semana: *{format_decimal_pt(plan_data['grand_total_kg'], 3)} kg* de ração",
        '',
        'Separar/preparar as rações da semana conforme abaixo. As rações preparadas devem ficar identificadas por produto, fase e viveiro/lote para evitar troca na hora do trato.',
    ]

    if plan_data.get('feed_totals'):
        lines.extend(['', '*Total por ração*'])
        for row in plan_data['feed_totals']:
            lines.append(f"- {row['label']}: {format_decimal_pt(row['total_kg'], 3)} kg")

    if plan_data.get('additive_totals'):
        lines.extend(['', '*Aditivos/insumos previstos no protocolo*'])
        for row in plan_data['additive_totals']:
            lines.append(f"- {row['label']}: {format_decimal_pt(row['total_quantity'], 2)} {row['measure_unit']}")

    if plan_data.get('phase_totals'):
        lines.extend(['', '*Total por fase*'])
        for phase in plan_data['phase_totals']:
            if phase['total_kg'] <= 0:
                continue
            lines.append(f"- {phase['label']}: {format_decimal_pt(phase['total_kg'], 3)} kg")

    if plan_data.get('unit_totals'):
        lines.extend(['', '*Separação por viveiro/lote*'])
        for row in plan_data['unit_totals']:
            if row['total_kg'] <= 0:
                continue
            lot_part = f" · Lote {row['lot_code']}" if row.get('lot_code') else ''
            lines.append(f"- {row['phase_label']} · {row['unit_name']}{lot_part}: {format_decimal_pt(row['total_kg'], 3)} kg")
            if row.get('mix_line'):
                lines.append(f"  Mix: {row['mix_line']}")

    if plan_data.get('daily_totals'):
        lines.extend(['', '*Conferência por dia*'])
        for day in plan_data['daily_totals']:
            lines.append(f"- {weekday_label_pt(day['date'])} {day['date'].strftime('%d/%m')}: {format_decimal_pt(day['total_kg'], 3)} kg")

    lines.extend([
        '',
        'Obs.: este texto vem da mesma programação das abas Alimentação Berçário, Juvenil e Engorda. Conferir estoque físico antes de preparar.'
    ])
    return '\n'.join(lines)


def build_feed_preparation_plan(start_date: date | None = None, days: int = 7):
    start_date = start_date or local_today()
    try:
        days = int(days or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 31))
    end_date = start_date + timedelta(days=days - 1)

    phase_meta = {key: {'phase': key, 'label': label, 'total_g': 0, 'feeds': defaultdict(int), 'units_count': 0} for key, label in FEED_PREPARATION_PHASES}
    feed_totals_map = OrderedDict()
    additive_totals_map = OrderedDict()
    unit_totals_map = OrderedDict()
    daily_rows = []
    daily_totals = []
    grand_total_g = 0
    plan_count = 0

    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        day_total_g = 0
        day_units = set()
        for phase_key, phase_label_text in FEED_PREPARATION_PHASES:
            phase_plans = build_stage_feed_digest_for_date(target_date, phase=phase_key)
            for plan in phase_plans:
                unit = plan.get('unit')
                lot = plan.get('lot')
                unit_name = getattr(unit, 'name', 'Sem unidade')
                lot_code = getattr(lot, 'lot_code', '')
                total_day_g = int(round(plan.get('total_day_g') or 0))
                mixes = [item for item in plan.get('mixes', []) if int(round(item.get('grams') or 0)) > 0]
                additives = selected_nursery_water_items_for_plan(plan)
                plan_count += 1
                grand_total_g += total_day_g
                day_total_g += total_day_g
                if getattr(unit, 'id', None):
                    day_units.add(unit.id)
                phase_meta[phase_key]['total_g'] += total_day_g

                unit_key = (phase_key, getattr(unit, 'id', None), getattr(lot, 'id', None))
                unit_row = unit_totals_map.setdefault(unit_key, {
                    'phase': phase_key,
                    'phase_label': phase_label_text,
                    'unit_name': unit_name,
                    'lot_code': lot_code,
                    'total_g': 0,
                    'feeds': defaultdict(int),
                    'dates': set(),
                })
                unit_row['total_g'] += total_day_g
                unit_row['dates'].add(target_date)

                for item in mixes:
                    grams = int(round(item.get('grams') or 0))
                    label = item.get('label') or 'Ração sem identificação'
                    add_feed_preparation_total(feed_totals_map, label, grams, phase_key, unit_name, lot_code, target_date)
                    phase_meta[phase_key]['feeds'][label] += grams
                    unit_row['feeds'][label] += grams

                for additive in additives:
                    add_preparation_additive_total(additive_totals_map, additive, phase_key, unit_name, lot_code, target_date)

                daily_rows.append({
                    'date': target_date,
                    'weekday': weekday_label_pt(target_date),
                    'phase': phase_key,
                    'phase_label': phase_label_text,
                    'unit_name': unit_name,
                    'lot_code': lot_code,
                    'stage_label': plan.get('stage_label'),
                    'total_g': total_day_g,
                    'total_kg': kg_from_grams(total_day_g),
                    'mixes': [{'label': item.get('label'), 'grams': int(round(item.get('grams') or 0)), 'kg': kg_from_grams(item.get('grams') or 0)} for item in mixes],
                    'additives': additives,
                })
        daily_totals.append({
            'date': target_date,
            'weekday': weekday_label_pt(target_date),
            'total_g': day_total_g,
            'total_kg': kg_from_grams(day_total_g),
            'units_count': len(day_units),
        })

    feed_totals = []
    for row in feed_totals_map.values():
        phase_breakdown = []
        for phase_key, phase_label_text in FEED_PREPARATION_PHASES:
            grams = row['phase_totals'].get(phase_key, 0)
            if grams:
                phase_breakdown.append({'phase': phase_key, 'label': phase_label_text, 'kg': kg_from_grams(grams), 'grams': grams})
        feed_totals.append({
            'label': row['label'],
            'total_g': row['total_g'],
            'total_kg': kg_from_grams(row['total_g']),
            'phase_breakdown': phase_breakdown,
            'units_count': len(row['units']),
            'lots_count': len(row['lots']),
        })
    feed_totals.sort(key=lambda item: (-item['total_g'], item['label'].lower()))

    additive_totals = []
    for row in additive_totals_map.values():
        additive_totals.append({
            'label': row['label'],
            'measure_unit': row['measure_unit'],
            'total_quantity': round(row['total_quantity'], 3),
            'units_count': len(row['units']),
            'lots_count': len(row['lots']),
        })
    additive_totals.sort(key=lambda item: (-item['total_quantity'], item['label'].lower()))

    phase_totals = []
    for phase_key, phase_label_text in FEED_PREPARATION_PHASES:
        meta = phase_meta[phase_key]
        phase_totals.append({
            'phase': phase_key,
            'label': phase_label_text,
            'total_g': meta['total_g'],
            'total_kg': kg_from_grams(meta['total_g']),
            'feed_count': len(meta['feeds']),
        })

    unit_totals = []
    phase_order = {key: idx for idx, (key, _) in enumerate(FEED_PREPARATION_PHASES)}
    for row in unit_totals_map.values():
        mixes = [
            {'label': label, 'grams': grams, 'kg': kg_from_grams(grams)}
            for label, grams in sorted(row['feeds'].items(), key=lambda item: (-item[1], item[0].lower()))
        ]
        unit_totals.append({
            'phase': row['phase'],
            'phase_label': row['phase_label'],
            'unit_name': row['unit_name'],
            'lot_code': row['lot_code'],
            'total_g': row['total_g'],
            'total_kg': kg_from_grams(row['total_g']),
            'days_count': len(row['dates']),
            'mixes': mixes,
            'mix_line': format_feed_preparation_mix_line(mixes),
        })
    unit_totals.sort(key=lambda item: (phase_order.get(item['phase'], 9), item['unit_name'].lower(), item.get('lot_code') or ''))

    plan_data = {
        'start_date': start_date,
        'end_date': end_date,
        'days': days,
        'plan_count': plan_count,
        'grand_total_g': grand_total_g,
        'grand_total_kg': kg_from_grams(grand_total_g),
        'feed_totals': feed_totals,
        'additive_totals': additive_totals,
        'phase_totals': phase_totals,
        'unit_totals': unit_totals,
        'daily_totals': daily_totals,
        'daily_rows': daily_rows,
    }
    plan_data['whatsapp_text'] = build_feed_preparation_whatsapp_text(plan_data)
    return plan_data

def build_nursery_digest_for_date(target_date: date | None = None):
    return build_stage_feed_digest_for_date(target_date, phase='bercario')


def build_juvenile_digest_for_date(target_date: date | None = None):
    return build_stage_feed_digest_for_date(target_date, phase='juvenil')


def build_growout_digest_for_date(target_date: date | None = None):
    return build_stage_feed_digest_for_date(target_date, phase='engorda')


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

    # Defesa adicional: mesmo que exista lançamento antigo com água marcada,
    # ao ressincronizar uma alimentação de Engorda o sistema não deve baixar
    # probiótico/AQUAPRO, LOTHAR nem melaço no Manejo Diário.
    if feeding_entry_operational_phase(entry) == 'engorda':
        selected_water_items = []
    else:
        selected_water_items = nursery_entry_water_items(entry)
    supply_entries = nursery_water_supply_entries(selected_water_items)
    if supply_entries:
        supply_management = DailyManagement(
            manage_date=entry.feed_date,
            unit_id=entry.unit_id,
            lot_id=entry.lot_id,
            feed_offered_kg=0,
            feed_consumed_kg=0,
            mortality_qty=0,
            average_weight_g=None,
            estimated_biomass_kg=None,
            notes=build_nursery_water_management_note_block(entry, [entry_item['item'] for entry_item in supply_entries]),
            updated_at=datetime.utcnow(),
        )
        db.session.add(supply_management)
        db.session.flush()
        sync_management_supply_usages(supply_management, supply_entries)
        created_records.append(supply_management)

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


PHYSICAL_WATER_FIELDS = {'dissolved_oxygen', 'temperature_c', 'transparency_cm'}
QUALITY_WATER_FIELDS = {'ph', 'salinity', 'ammonia', 'nitrite', 'nitrate', 'alkalinity', 'hardness'}


def physical_water_filter():
    return or_(
        WaterMonitoring.dissolved_oxygen.isnot(None),
        WaterMonitoring.temperature_c.isnot(None),
        WaterMonitoring.transparency_cm.isnot(None),
    )


def quality_water_filter():
    return or_(
        WaterMonitoring.ph.isnot(None),
        WaterMonitoring.salinity.isnot(None),
        WaterMonitoring.ammonia.isnot(None),
        WaterMonitoring.nitrite.isnot(None),
        WaterMonitoring.nitrate.isnot(None),
        WaterMonitoring.alkalinity.isnot(None),
        WaterMonitoring.hardness.isnot(None),
    )


def build_reference_summary(config=None, fields=None):
    config = config or get_water_reference_config()
    selected_fields = set(fields or []) if fields else None
    summary = []
    for spec in WATER_PARAMETER_SPECS:
        if selected_fields and spec['field'] not in selected_fields:
            continue
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

    for model in (ProtocolDocument, FarmDocument, WaterReferenceConfig, FeedProduct, SupplyProduct, SupplyInventory, ManagementSupplyUsage, LotUnitAllocation, FixedCost, NurseryFeeding, OperationalTask, FeedingProtocolRow, FeedingProtocolFeed, FeedingProtocolFeedMap):
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
        add_column_if_missing('lot', lot_columns, 'larva_unit_cost', 'ALTER TABLE lot ADD COLUMN larva_unit_cost FLOAT', 'ALTER TABLE lot ADD COLUMN larva_unit_cost DOUBLE PRECISION')
        add_column_if_missing('lot', lot_columns, 'larva_total_cost', 'ALTER TABLE lot ADD COLUMN larva_total_cost FLOAT DEFAULT 0', 'ALTER TABLE lot ADD COLUMN larva_total_cost DOUBLE PRECISION DEFAULT 0')


    if 'lot_unit_allocation' in tables:
        allocation_columns = get_columns('lot_unit_allocation')
        add_column_if_missing('lot_unit_allocation', allocation_columns, 'quantity_allocated', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER', 'ALTER TABLE lot_unit_allocation ADD COLUMN quantity_allocated INTEGER')
        add_column_if_missing('lot_unit_allocation', allocation_columns, 'operational_phase', 'ALTER TABLE lot_unit_allocation ADD COLUMN operational_phase VARCHAR(30)', 'ALTER TABLE lot_unit_allocation ADD COLUMN operational_phase VARCHAR(30)')

    if 'transfer' in tables:
        transfer_columns = get_columns('transfer')
        add_column_if_missing('transfer', transfer_columns, 'source_phase', 'ALTER TABLE transfer ADD COLUMN source_phase VARCHAR(30)', 'ALTER TABLE transfer ADD COLUMN source_phase VARCHAR(30)')
        add_column_if_missing('transfer', transfer_columns, 'destination_phase', 'ALTER TABLE transfer ADD COLUMN destination_phase VARCHAR(30)', 'ALTER TABLE transfer ADD COLUMN destination_phase VARCHAR(30)')
        add_column_if_missing('transfer', transfer_columns, 'close_source_after_transfer', 'ALTER TABLE transfer ADD COLUMN close_source_after_transfer BOOLEAN DEFAULT 0', 'ALTER TABLE transfer ADD COLUMN close_source_after_transfer BOOLEAN DEFAULT FALSE')

    if 'sale' in tables:
        sale_columns = get_columns('sale')
        add_column_if_missing('sale', sale_columns, 'average_weight_g', 'ALTER TABLE sale ADD COLUMN average_weight_g FLOAT', 'ALTER TABLE sale ADD COLUMN average_weight_g DOUBLE PRECISION')
        add_column_if_missing('sale', sale_columns, 'harvested_units', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER', 'ALTER TABLE sale ADD COLUMN harvested_units INTEGER')

    if 'nursery_feeding' in tables:
        nursery_feeding_columns = get_columns('nursery_feeding')
        add_column_if_missing('nursery_feeding', nursery_feeding_columns, 'score_adjustment_pct', 'ALTER TABLE nursery_feeding ADD COLUMN score_adjustment_pct FLOAT', 'ALTER TABLE nursery_feeding ADD COLUMN score_adjustment_pct DOUBLE PRECISION')
        add_column_if_missing('nursery_feeding', nursery_feeding_columns, 'active_feed_factor', 'ALTER TABLE nursery_feeding ADD COLUMN active_feed_factor FLOAT', 'ALTER TABLE nursery_feeding ADD COLUMN active_feed_factor DOUBLE PRECISION')
        add_column_if_missing('nursery_feeding', nursery_feeding_columns, 'water_items_json', 'ALTER TABLE nursery_feeding ADD COLUMN water_items_json TEXT', 'ALTER TABLE nursery_feeding ADD COLUMN water_items_json TEXT')

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


    if 'operational_task' in tables:
        operational_task_columns = get_columns('operational_task')
        add_column_if_missing('operational_task', operational_task_columns, 'supply_product_id', 'ALTER TABLE operational_task ADD COLUMN supply_product_id INTEGER', 'ALTER TABLE operational_task ADD COLUMN supply_product_id INTEGER')

    if 'management_supply_usage' in tables:
        management_supply_columns = get_columns('management_supply_usage')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'notes', 'ALTER TABLE management_supply_usage ADD COLUMN notes TEXT')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'created_at', f"ALTER TABLE management_supply_usage ADD COLUMN created_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE management_supply_usage ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        add_column_if_missing('management_supply_usage', management_supply_columns, 'updated_at', f"ALTER TABLE management_supply_usage ADD COLUMN updated_at DATETIME DEFAULT '{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}'", 'ALTER TABLE management_supply_usage ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')

    backfill_lot_allocations_and_status()
    sync_transfer_phase_history()
    sync_transfer_close_source_flags()
    rebuild_all_lot_allocations_from_transfer_history()
    db.session.commit()
    sync_feed_products_from_legacy_movements()
    normalize_auto_nursery_feed_product_names()
    repair_protocol_feed_maps()
    repair_auto_nursery_management_feed_links()
    cleanup_ghost_feed_products()


def init_db():
    with app.app_context():
        db.create_all()
        run_lightweight_migrations()
        seed_units()
        seed_admin_user()
        get_water_reference_config()
        ensure_feeding_protocol_seeded()
        ensure_alert_rules()


def active_lot_allocation_for_unit(unit_id, on_date=None):
    on_date = on_date or local_today()
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
                operational_phase=normalize_phase_value(lot.phase) or (lot.unit.phase if lot.unit else None),
                notes='Alocação inicial criada automaticamente.'
            ))
            created += 1
        else:
            for allocation in LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit)).filter_by(lot_id=lot.id).all():
                if allocation.quantity_allocated is None:
                    allocation.quantity_allocated = lot.initial_count
                if not normalize_phase_value(getattr(allocation, 'operational_phase', None)):
                    allocation.operational_phase = normalize_phase_value(lot.phase) or (allocation.unit.phase if allocation.unit else None)
        if lot.status == 'encerrado' and lot.end_date is None:
            last_sale = Sale.query.filter_by(lot_id=lot.id).order_by(Sale.sale_date.desc(), Sale.id.desc()).first()
            lot.end_date = last_sale.sale_date if last_sale else local_today()
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


def sync_transfer_close_source_flags():
    """Preserva a intenção antiga de "encerrar origem" antes de recalcular saldos.

    Versões anteriores guardavam essa decisão apenas em LotUnitAllocation.end_date.
    A partir de agora o flag fica no próprio histórico da transferência para que edições
    e recálculos futuros não tragam de volta saldo residual que já saiu da unidade.
    """
    changed = False
    for transfer in Transfer.query.all():
        if getattr(transfer, 'close_source_after_transfer', False):
            continue
        closed_source = LotUnitAllocation.query.filter(
            LotUnitAllocation.lot_id == transfer.source_lot_id,
            LotUnitAllocation.unit_id == transfer.source_unit_id,
            LotUnitAllocation.end_date == transfer.transfer_date,
        ).first()
        if closed_source:
            transfer.close_source_after_transfer = True
            changed = True
    if changed:
        db.session.flush()


def rebuild_lot_allocations_from_transfer_history(lot: Lot):
    """Recalcula os saldos vivos do lote a partir do cadastro inicial + histórico de transferências.

    A tabela Transfer é o histórico oficial do que aconteceu. A tela "Saldos vivos estimados"
    lê LotUnitAllocation, então sempre que uma transferência é editada precisamos reconstruir
    essa tabela derivada para evitar saldo fantasma na origem ou no destino.

    Regra importante: quando existe uma transferência com contagem real, ela precisa virar
    o novo marco de população. Se o lote foi cadastrado originalmente já no destino
    (caso comum quando o sistema começou a ser usado depois da estocagem no berçário),
    a primeira transferência informa onde o lote estava antes. Nessa situação, a quantidade
    transferida substitui a expectativa anterior em vez de manter o saldo inicial fantasma.

    A fase operacional da alocação vem da transferência. Isso permite que uma unidade física
    cadastrada como Engorda seja usada temporariamente como Juvenil, e a tela Alimentação
    Juvenil passe a enxergar o lote corretamente.
    """
    if not lot or not lot.id:
        return []

    warnings = []
    initial_qty = max(int(lot.initial_count or 0), 0)

    transfers = (
        Transfer.query
        .filter_by(source_lot_id=lot.id)
        .order_by(Transfer.transfer_date.asc(), Transfer.id.asc())
        .all()
    )
    valid_transfers = [
        transfer for transfer in transfers
        if max(int(transfer.transferred_qty or 0), 0) > 0
        and transfer.source_unit_id
        and transfer.destination_unit_id
        and transfer.source_unit_id != transfer.destination_unit_id
    ]
    first_valid_transfer = valid_transfers[0] if valid_transfers else None

    # Remove o mapa derivado antigo. Ele será recriado abaixo com base no histórico oficial.
    LotUnitAllocation.query.filter_by(lot_id=lot.id).delete(synchronize_session=False)

    def phase_for_unit(unit_id, fallback=None):
        fallback = normalize_phase_value(fallback)
        if fallback:
            return fallback
        unit = db.session.get(Unit, unit_id) if unit_id else None
        return normalize_phase_value(unit.phase if unit else None)

    def add_allocation(unit_id, start_date, end_date, qty, notes, operational_phase=None):
        if not unit_id or not start_date or not qty or qty <= 0:
            return
        if end_date and end_date < start_date:
            return
        db.session.add(LotUnitAllocation(
            lot_id=lot.id,
            unit_id=unit_id,
            start_date=start_date,
            end_date=end_date,
            quantity_allocated=int(qty),
            operational_phase=phase_for_unit(unit_id, operational_phase),
            notes=notes,
        ))

    state = {}
    inferred_first_source = False
    initial_unit_id = lot.unit_id
    initial_phase = phase_for_unit(lot.unit_id, lot.phase)
    initial_note = 'Alocação inicial do lote.'

    if first_valid_transfer and first_valid_transfer.source_unit_id and first_valid_transfer.source_unit_id != lot.unit_id:
        # O lote estava cadastrado em uma unidade diferente da origem real da primeira
        # transferência. Usa a origem da transferência como ponto inicial operacional.
        initial_unit_id = first_valid_transfer.source_unit_id
        initial_phase = phase_for_unit(first_valid_transfer.source_unit_id, first_valid_transfer.source_phase)
        inferred_first_source = True
        initial_note = 'Origem inicial reconstruída pela primeira transferência real.'

    if initial_unit_id and (initial_qty > 0 or first_valid_transfer):
        seed_qty = initial_qty
        if seed_qty <= 0 and first_valid_transfer:
            seed_qty = max(int(first_valid_transfer.transferred_qty or 0), 0)
        if seed_qty > 0:
            state[initial_unit_id] = {
                'qty': seed_qty,
                'start_date': lot.start_date or (first_valid_transfer.transfer_date if first_valid_transfer else local_today()),
                'phase': initial_phase,
                'notes': initial_note,
                'inferred_first_source': inferred_first_source,
            }

    for transfer in transfers:
        qty_requested = max(int(transfer.transferred_qty or 0), 0)
        if qty_requested <= 0 or not transfer.source_unit_id or not transfer.destination_unit_id:
            continue
        if transfer.source_unit_id == transfer.destination_unit_id:
            warnings.append(f'Transferência #{transfer.id} ignorada porque origem e destino são iguais.')
            continue

        transfer_date = transfer.transfer_date or local_today()
        close_date = transfer_date - timedelta(days=1)
        source_phase = phase_for_unit(transfer.source_unit_id, transfer.source_phase)
        destination_phase = phase_for_unit(transfer.destination_unit_id, transfer.destination_phase)
        source_state = state.get(transfer.source_unit_id)
        inferred_missing_source = False

        if not source_state:
            # Não ignora a transferência. Em versões anteriores, isso mantinha o saldo inicial
            # no destino e fazia dashboard/biomassa/sobrevivência continuarem usando a expectativa.
            # Aqui a própria transferência vira o marco real do lote.
            source_state = {
                'qty': max(qty_requested, initial_qty if not state else 0),
                'start_date': lot.start_date or transfer_date,
                'phase': source_phase,
                'notes': 'Origem reconstruída automaticamente por transferência real sem saldo anterior.',
                'inferred_first_source': True,
            }
            state[transfer.source_unit_id] = source_state
            inferred_missing_source = True
        elif not source_state.get('phase'):
            source_state['phase'] = source_phase

        available_qty = int(source_state['qty']) if source_state else 0
        received_qty = qty_requested

        if available_qty > 0 and qty_requested > available_qty and not source_state.get('inferred_first_source'):
            # A contagem da transferência é tratada como dado real de campo.
            # Em berçário/juvenil, a população inicial vem de estimativa por peso do laboratório;
            # se a transferência real vier maior que o saldo teórico, ela recalibra o lote em vez de ser limitada.
            received_qty = qty_requested
            source_state['qty'] = qty_requested
            available_qty = qty_requested
            warnings.append(
                f'Transferência #{transfer.id} informou {qty_requested:,} un., acima do saldo estimado; a contagem real recalibrou o lote.'.replace(',', '.')
            )

        # Se a origem foi reconstruída/inferida pela própria transferência, considera que a
        # contagem informada substitui a expectativa anterior. Assim 11.000 transferidos
        # deixam de competir com 265.000 esperados no dashboard.
        effective_close_source = bool(
            transfer.close_source_after_transfer
            or inferred_missing_source
            or (source_state.get('inferred_first_source') and first_valid_transfer and transfer.id == first_valid_transfer.id)
        )
        removed_from_source = available_qty if effective_close_source else min(received_qty, available_qty)

        # Fecha o trecho anterior da origem e abre novo trecho apenas se ainda restou saldo.
        add_allocation(
            transfer.source_unit_id,
            source_state['start_date'],
            close_date,
            available_qty,
            source_state.get('notes') or 'Saldo anterior à transferência.',
            source_state.get('phase') or source_phase,
        )
        remaining_qty = max(available_qty - removed_from_source, 0)
        if remaining_qty > 0:
            state[transfer.source_unit_id] = {
                'qty': remaining_qty,
                'start_date': transfer_date,
                'phase': source_state.get('phase') or source_phase,
                'notes': 'Saldo recalculado após transferência parcial.',
                'inferred_first_source': False,
            }
        else:
            state.pop(transfer.source_unit_id, None)

        # Se o destino já tinha saldo desse lote, fecha o trecho anterior e reabre somado.
        destination_state = state.get(transfer.destination_unit_id)
        if destination_state:
            add_allocation(
                transfer.destination_unit_id,
                destination_state['start_date'],
                close_date,
                destination_state['qty'],
                destination_state.get('notes') or 'Saldo anterior à nova entrada.',
                destination_state.get('phase') or destination_phase,
            )
            new_destination_qty = int(destination_state['qty']) + received_qty
        else:
            new_destination_qty = received_qty

        state[transfer.destination_unit_id] = {
            'qty': new_destination_qty,
            'start_date': transfer_date,
            'phase': destination_phase,
            'notes': 'Saldo recalculado automaticamente a partir das transferências reais.',
            'inferred_first_source': False,
        }

    final_end_date = lot.end_date if lot.status == 'encerrado' else None
    for unit_id, payload in state.items():
        add_allocation(
            unit_id,
            payload['start_date'],
            final_end_date,
            payload['qty'],
            payload.get('notes') or 'Saldo recalculado automaticamente.',
            payload.get('phase'),
        )

    db.session.flush()
    return warnings

def rebuild_all_lot_allocations_from_transfer_history():
    warnings = []
    for lot in Lot.query.order_by(Lot.start_date.asc(), Lot.id.asc()).all():
        warnings.extend(rebuild_lot_allocations_from_transfer_history(lot))
        sync_lot_phase_from_allocations(lot, local_today())
    return warnings


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
        primary.operational_phase = normalize_phase_value(lot.phase) or (lot.unit.phase if lot.unit else None)
        primary.notes = 'Alocação inicial do lote.'
        for extra in allocations[1:]:
            db.session.delete(extra)
        return

    # If there is movement history, the safest correction is to rebuild the derived saldo map.
    # This also fixes edits to initial_count/unit/start_date after transfers already exist.
    rebuild_lot_allocations_from_transfer_history(lot)
    return


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
    on_date = on_date or local_today()
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
    on_date = on_date or local_today()
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
        .order_by(Lot.lot_code.asc(), LotUnitAllocation.operational_phase.asc(), Unit.name.asc(), LotUnitAllocation.start_date.asc())
        .all()
    )


def active_allocations_for_operational_phase(phase: str, on_date=None):
    """Active allocations by operational phase, not only by the fixed unit phase.

    This is what makes Alimentação Juvenil find a lot transferred as Juvenil into an
    estufa/viveiro whose master registration is still "engorda".
    """
    phase = normalize_phase_value(phase)
    if not phase:
        return []
    on_date = on_date or local_today()
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
            or_(
                LotUnitAllocation.operational_phase == phase,
                and_(
                    or_(LotUnitAllocation.operational_phase.is_(None), LotUnitAllocation.operational_phase == ''),
                    Unit.phase == phase,
                ),
            ),
        )
        .order_by(Unit.name.asc(), Lot.start_date.desc(), LotUnitAllocation.start_date.desc(), LotUnitAllocation.id.desc())
        .all()
    )


def active_units_for_operational_phase(phase: str, on_date=None):
    seen = set()
    units = []
    for allocation in active_allocations_for_operational_phase(phase, on_date=on_date):
        if allocation.unit and allocation.unit.id not in seen:
            seen.add(allocation.unit.id)
            units.append(allocation.unit)
    return units


def unit_is_active_in_operational_phase(unit_id: int, phase: str, on_date=None):
    phase = normalize_phase_value(phase)
    if not unit_id or not phase:
        return False
    return any(allocation.unit_id == unit_id for allocation in active_allocations_for_operational_phase(phase, on_date=on_date))


def sync_lot_phase_from_allocations(lot: Lot, on_date=None):
    """Keeps Lot.phase compatible with the most advanced active phase of its allocations."""
    if not lot:
        return
    on_date = on_date or local_today()
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
        (allocation_operational_phase(allocation) for allocation in allocations if allocation_operational_phase(allocation)),
        key=lambda phase: phase_rank.get(phase, 0),
        default=lot.phase,
    )
    if most_advanced:
        lot.phase = most_advanced
        preferred = max(
            allocations,
            key=lambda allocation: (
                phase_rank.get(allocation_operational_phase(allocation) or '', 0),
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
    end = lot.end_date or local_today()
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
    end = end_date or lot.end_date or local_today()
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


def lot_total_feed_offered_kg(lot_id: int, start_date: date | None = None, end_date: date | None = None, include_unsynced_nursery: bool = True):
    """Total de ração ofertada do lote em kg.

    DailyManagement é a fonte principal porque as telas Berçário/Juvenil/Engorda
    sincronizam automaticamente seus mixes para o Manejo Diário. O bloco final
    soma apenas lançamentos antigos de NurseryFeeding que por algum motivo ainda
    não possuem manejo integrado, evitando dupla contagem.
    """
    if not lot_id:
        return 0.0
    query = db.session.query(func.coalesce(func.sum(DailyManagement.feed_offered_kg), 0)).filter(DailyManagement.lot_id == lot_id)
    if start_date:
        query = query.filter(DailyManagement.manage_date >= start_date)
    if end_date:
        query = query.filter(DailyManagement.manage_date <= end_date)
    total = float(query.scalar() or 0)

    if include_unsynced_nursery:
        nursery_query = NurseryFeeding.query.filter(NurseryFeeding.lot_id == lot_id)
        if start_date:
            nursery_query = nursery_query.filter(NurseryFeeding.feed_date >= start_date)
        if end_date:
            nursery_query = nursery_query.filter(NurseryFeeding.feed_date <= end_date)
        for entry in nursery_query.all():
            if not entry.id:
                continue
            sync_query = db.session.query(func.coalesce(func.sum(DailyManagement.feed_offered_kg), 0)).filter(
                DailyManagement.lot_id == lot_id,
                DailyManagement.unit_id == entry.unit_id,
                DailyManagement.manage_date == entry.feed_date,
                DailyManagement.notes.contains(nursery_management_source_marker(entry.id)),
            )
            synced_total = float(sync_query.scalar() or 0)
            if synced_total <= 0:
                total += float(entry.quantity_kg or 0)
    return round(total, 3)


def lot_initial_weight_g_for_fcr(lot: Lot):
    """Peso inicial usado no FCR parcial.

    Não usa Lot.estimated_weight_g como primeira opção porque essa coluna é
    atualizada pela última biometria; usar ela como peso inicial derruba o ganho
    de biomassa e distorce o FCR parcial.
    """
    if not lot:
        return 0.0
    table_start = feeding_table_expected_weight_for_lot(lot, target_date=lot.start_date, age_days=0)
    if table_start and table_start.get('expected_weight_g'):
        return float(table_start['expected_weight_g'])
    observations = merged_weight_observations(lot.id) if getattr(lot, 'id', None) else []
    if observations and observations[0].get('weight_g'):
        return float(observations[0]['weight_g'])
    return float(parse_float(getattr(lot, 'estimated_weight_g', None), 0) or 0)


def lot_partial_fcr_snapshot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]] | None = None, allocations_by_lot: dict[int, list[LotUnitAllocation]] | None = None, on_date: date | None = None):
    """FCR parcial = ração acumulada / biomassa produzida.

    Biomassa produzida considera biomassa atual + biomassa já despescada -
    biomassa inicial estimada pela tabela de idade PL. Isso mantém o indicador
    correto em lote ativo, lote com despesca parcial e lote dividido em viveiros.
    """
    if not lot:
        return {'total_feed_kg': 0.0, 'initial_biomass_kg': 0.0, 'current_biomass_kg': 0.0, 'harvested_kg': 0.0, 'biomass_gain_kg': 0.0, 'fcr': None}
    on_date = on_date or local_today()
    total_feed = lot_total_feed_offered_kg(lot.id, start_date=lot.start_date, end_date=on_date)
    initial_weight_g = lot_initial_weight_g_for_fcr(lot)
    initial_biomass = ((lot.initial_count or 0) * (initial_weight_g or 0)) / 1000.0
    current_biomass = latest_biomass_for_lot(lot, records_by_lot or defaultdict(list), allocations_by_lot or defaultdict(list)) or 0.0
    harvested_kg = lot_total_harvested_kg(lot.id)
    biomass_gain = max((current_biomass or 0) + (harvested_kg or 0) - initial_biomass, 0)
    return {
        'total_feed_kg': round(total_feed, 3),
        'initial_biomass_kg': round(initial_biomass, 3),
        'current_biomass_kg': round(current_biomass or 0, 3),
        'harvested_kg': round(harvested_kg or 0, 3),
        'biomass_gain_kg': round(biomass_gain, 3),
        'fcr': round(total_feed / biomass_gain, 2) if biomass_gain > 0 else None,
    }


def build_allocation_rows(lot: Lot, on_date=None):
    on_date = on_date or local_today()
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


def lot_larva_cost(lot: Lot):
    """Custo total das PLs/larvas do lote.

    Prioriza o valor total informado no cadastro. Se o operador informar apenas
    o custo por milheiro, calcula automaticamente pela quantidade inicial.
    """
    if not lot:
        return 0.0
    total = lot.larva_total_cost or 0
    if total > 0:
        return round(total, 2)
    unit_cost = lot.larva_unit_cost or 0
    if unit_cost > 0 and lot.initial_count:
        return round((lot.initial_count / 1000) * unit_cost, 2)
    return 0.0


def calculate_larva_cost_for_unit(lot: Lot, unit_id: int, on_date: date | None = None):
    """Rateia o custo das PLs para uma unidade/viveiro no momento da venda.

    Quando um lote está dividido em mais de um viveiro, usa a quantidade alocada
    para evitar jogar 100% do custo de larva em uma única despesca parcial.
    """
    if not lot or not unit_id:
        return 0.0
    total = lot_larva_cost(lot)
    if total <= 0:
        return 0.0
    on_date = on_date or local_today()
    allocation = find_active_allocation(lot.id, unit_id, on_date)
    if allocation and allocation.quantity_allocated:
        active_allocations = LotUnitAllocation.query.filter(
            LotUnitAllocation.lot_id == lot.id,
            LotUnitAllocation.start_date <= on_date,
            or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
            or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
        ).all()
        denominator = sum((item.quantity_allocated or 0) for item in active_allocations) or lot.initial_count or 0
        if denominator > 0:
            return round(total * ((allocation.quantity_allocated or 0) / denominator), 2)
    if lot.unit_id == unit_id:
        return round(total, 2)
    return 0.0


def lot_financial_summary(lot: Lot):
    feed_cost = db.session.query(func.coalesce(func.sum(DailyManagement.feed_total_cost), 0)).filter(DailyManagement.lot_id == lot.id).scalar() or 0
    supply_cost = db.session.query(func.coalesce(func.sum(ManagementSupplyUsage.total_cost), 0)).join(DailyManagement, DailyManagement.id == ManagementSupplyUsage.management_id).filter(DailyManagement.lot_id == lot.id).scalar() or 0
    larva_cost = lot_larva_cost(lot)
    fixed_cost = calculate_fixed_cost_for_lot(lot)
    current_units = lot_current_units(lot)
    allocation_rows = build_allocation_rows(lot)
    harvested_units = lot_total_harvested_units(lot.id)
    if lot.initial_count:
        if harvested_units > 0:
            survival_pct = round((harvested_units / lot.initial_count) * 100, 2)
        else:
            survival_pct = round((allocation_live_count_for_lot(lot) / lot.initial_count) * 100, 2)
    else:
        survival_pct = None
    total_feed_offered = lot_total_feed_offered_kg(lot.id)
    harvested_kg = lot_total_harvested_kg(lot.id)
    fcr_real = round(total_feed_offered / harvested_kg, 2) if harvested_kg else None
    return {
        'lot': lot,
        'feed_cost': round(feed_cost, 2),
        'supply_cost': round(supply_cost, 2),
        'larva_cost': larva_cost,
        'fixed_cost': fixed_cost,
        'total_cost': round((feed_cost or 0) + (supply_cost or 0) + larva_cost + fixed_cost, 2),
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
    larva_cost = calculate_larva_cost_for_unit(sale.lot, sale.unit_id, sale.sale_date)
    fixed_cost = calculate_fixed_cost_for_allocation(sale.lot, sale.unit_id, sale.lot.start_date, sale.sale_date)
    total_cost = round(feed_cost + supply_cost + larva_cost + fixed_cost, 2)
    revenue = round((sale.quantity_kg or 0) * (sale.unit_price or 0), 2)
    harvested_units = sale.harvested_units or 0
    if not harvested_units and sale.average_weight_g:
        harvested_units = int(round((sale.quantity_kg * 1000) / sale.average_weight_g)) if sale.average_weight_g else 0
    lot_harvested_units = lot_total_harvested_units(sale.lot_id)
    survival_pct = round((lot_harvested_units / sale.lot.initial_count) * 100, 2) if sale.lot.initial_count else None
    total_feed_offered = lot_total_feed_offered_kg(sale.lot_id, end_date=sale.sale_date)
    harvested_kg_lot = lot_total_harvested_kg(sale.lot_id)
    fcr_real = round(total_feed_offered / harvested_kg_lot, 2) if harvested_kg_lot else None
    return {
        'sale': sale,
        'allocation_qty': allocation.quantity_allocated if allocation else None,
        'density': allocation_density(allocation) if allocation else None,
        'feed_cost': feed_cost,
        'supply_cost': supply_cost,
        'larva_cost': larva_cost,
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


def feed_product_alias_keys(product):
    """Chaves de comparação para evitar duplicar produtos do estoque.

    A migração antiga criava produto a partir do nome salvo no movimento
    (feed_inventory.feed_name). O problema era comparar esse nome contra
    "marca + tipo"; assim, uma ração real como "AQUAVITA 35 + IRCA 30..."
    virava um novo produto fantasma com tipo "Geral" quando uma saída de
    manejo era lançada ou quando a aplicação reiniciava.
    """
    keys = set()
    if not product:
        return keys
    for value in (
        product.full_name,
        product.brand,
        f'{product.brand} {product.feed_type}',
        f'{product.full_name} {product.technical_summary}',
    ):
        normalized = normalize_text(value or '')
        if normalized:
            keys.add(normalized)
    return keys


def build_feed_product_alias_map():
    aliases = {}
    products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.id.asc()).all()
    for product in products:
        for key in feed_product_alias_keys(product):
            aliases.setdefault(key, product)
    return aliases


def sync_feed_products_from_legacy_movements():
    """Vincula movimentos antigos ao produto correto sem criar fantasmas.

    Só cria produto quando o movimento é realmente legado, isto é, quando
    ainda não tem feed_product_id. Movimentos novos do manejo diário já são
    vinculados por ID e não devem gerar novos itens no estoque consolidado.
    """
    existing_products = build_feed_product_alias_map()

    legacy_movements = FeedInventory.query.filter(
        FeedInventory.feed_product_id.is_(None),
        FeedInventory.feed_name.isnot(None),
    ).all()

    created = 0
    for movement in legacy_movements:
        feed_name = (movement.feed_name or '').strip()
        normalized = normalize_text(feed_name)
        if not normalized:
            continue
        product = existing_products.get(normalized)
        if not product:
            product = FeedProduct(brand=feed_name[:120], feed_type='Geral', active=True, notes='Criado automaticamente para vincular movimentação antiga de estoque.')
            db.session.add(product)
            db.session.flush()
            for key in feed_product_alias_keys(product):
                existing_products.setdefault(key, product)
            created += 1
        movement.feed_product_id = product.id

    if created or legacy_movements:
        db.session.commit()


def cleanup_ghost_feed_products():
    """Remove ou mescla rações fantasma criadas pela migração anterior.

    Critério seguro: produtos com tipo "Geral" ou vazio, cujo nome/marca já
    corresponde a uma ração real existente. Se houver referências, elas são
    movidas para o produto canônico; se não houver, o item fantasma é excluído.
    """
    products = FeedProduct.query.order_by(FeedProduct.id.asc()).all()
    alias_map = {}
    for product in products:
        # Preferimos produto não genérico como canônico.
        type_norm = normalize_text(product.feed_type or '')
        priority = 0 if type_norm not in {'', 'geral'} else 1
        for key in feed_product_alias_keys(product):
            current = alias_map.get(key)
            if current is None:
                alias_map[key] = product
                continue
            current_priority = 0 if normalize_text(current.feed_type or '') not in {'', 'geral'} else 1
            if (priority, product.id) < (current_priority, current.id):
                alias_map[key] = product

    changed = False
    for product in list(FeedProduct.query.order_by(FeedProduct.id.asc()).all()):
        type_norm = normalize_text(product.feed_type or '')
        if type_norm not in {'', 'geral'}:
            continue
        canonical = None
        for key in feed_product_alias_keys(product):
            candidate = alias_map.get(key)
            if candidate and candidate.id != product.id:
                canonical = candidate
                break
        if not canonical:
            continue

        FeedInventory.query.filter_by(feed_product_id=product.id).update({'feed_product_id': canonical.id, 'feed_name': feed_inventory_name(canonical)}, synchronize_session=False)
        DailyManagement.query.filter_by(feed_product_id=product.id).update({'feed_product_id': canonical.id}, synchronize_session=False)
        OperationalTask.query.filter_by(feed_product_id=product.id).update({'feed_product_id': canonical.id}, synchronize_session=False)
        db.session.delete(product)
        changed = True

    if changed:
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


def nursery_water_supply_alias_labels(label: str):
    normalized = normalize_text(label)
    aliases = [label]
    for key, values in NURSERY_WATER_SUPPLY_ALIASES.items():
        key_norm = normalize_text(key)
        if normalized == key_norm or normalized in key_norm or key_norm in normalized:
            aliases.extend(values)
    return [item for item in dict.fromkeys(aliases) if (item or '').strip()]


def find_or_create_supply_product_for_protocol(label: str, measure_unit='g', create_missing=True):
    aliases = [normalize_text(item) for item in nursery_water_supply_alias_labels(label)]
    aliases = [item for item in aliases if item]
    if not aliases:
        return None
    products = SupplyProduct.query.order_by(SupplyProduct.active.desc(), SupplyProduct.name.asc()).all()
    best = None
    for product in products:
        product_norms = [
            normalize_text(product.full_name),
            normalize_text(product.name or ''),
            normalize_text(f'{product.name} {product.category} {product.measure_unit or ""}'),
        ]
        product_norms = [item for item in product_norms if item]
        if any(alias == prod or alias in prod or prod in alias for alias in aliases for prod in product_norms):
            candidate = (product.active, product.full_name.lower(), product)
            if best is None or candidate > best:
                best = candidate
    if best:
        return best[2]
    if not create_missing:
        return None
    canonical = nursery_water_supply_alias_labels(label)[0]
    product = SupplyProduct(
        name=canonical[:160],
        category='Manejo da água - protocolo berçário',
        measure_unit=canonical_measure_unit(measure_unit or 'g'),
        active=True,
        notes='Criado automaticamente ao importar manejo da água do protocolo de berçário.',
    )
    db.session.add(product)
    db.session.flush()
    return product


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


def active_allocations_for_lot(lot: Lot, on_date=None):
    """Alocações vivas do lote na data, já refletindo transferências reais."""
    if not lot or not lot.id:
        return []
    on_date = on_date or local_today()
    return LotUnitAllocation.query.options(joinedload(LotUnitAllocation.unit)).filter(
        LotUnitAllocation.lot_id == lot.id,
        LotUnitAllocation.start_date <= on_date,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= on_date),
        or_(LotUnitAllocation.quantity_allocated.is_(None), LotUnitAllocation.quantity_allocated > 0),
    ).order_by(LotUnitAllocation.start_date.asc(), LotUnitAllocation.id.asc()).all()


def latest_transfer_for_lot(lot: Lot, on_date=None):
    if not lot or not lot.id:
        return None
    on_date = on_date or local_today()
    return Transfer.query.filter(
        Transfer.source_lot_id == lot.id,
        Transfer.transfer_date <= on_date,
    ).order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).first()


def latest_real_population_marker_date(lot: Lot, on_date=None):
    """Última data em que a população deixou de ser só estimativa e virou contagem real/operacional."""
    latest_transfer = latest_transfer_for_lot(lot, on_date=on_date)
    return latest_transfer.transfer_date if latest_transfer and latest_transfer.transfer_date else (lot.start_date if lot else None)


def _sum_mortality_after_marker(lot_id: int, unit_id: int | None, start_date: date | None, up_to_date: date):
    query = DailyManagement.query.filter(
        DailyManagement.lot_id == lot_id,
        DailyManagement.manage_date <= up_to_date,
    )
    if unit_id:
        query = query.filter(DailyManagement.unit_id == unit_id)
    if start_date:
        # A contagem da transferência já inclui perdas até aquele momento; não desconta de novo.
        query = query.filter(DailyManagement.manage_date > start_date)
    return int(sum(row.mortality_qty or 0 for row in query.all()))


def _sum_harvested_after_marker(lot_id: int, unit_id: int | None, start_date: date | None, up_to_date: date):
    query = Sale.query.filter(
        Sale.lot_id == lot_id,
        Sale.sale_date <= up_to_date,
    )
    if unit_id:
        query = query.filter(Sale.unit_id == unit_id)
    if start_date:
        query = query.filter(Sale.sale_date > start_date)
    total = 0
    for sale in query.all():
        harvested_units = sale.harvested_units
        if harvested_units is None and sale.average_weight_g:
            harvested_units = int(round(((sale.quantity_kg or 0) * 1000) / sale.average_weight_g))
        total += harvested_units or 0
    return int(total or 0)


def allocation_live_count_for_lot(lot: Lot, on_date=None):
    """População viva atual baseada no último saldo real por unidade, menos perdas posteriores."""
    if not lot or not lot.id:
        return 0
    on_date = on_date or local_today()
    allocations = active_allocations_for_lot(lot, on_date=on_date)
    if allocations:
        total = 0
        for allocation in allocations:
            base_qty = allocation.quantity_allocated
            if base_qty is None:
                base_qty = lot.initial_count or 0
            mortality = _sum_mortality_after_marker(lot.id, allocation.unit_id, allocation.start_date, on_date)
            harvested = _sum_harvested_after_marker(lot.id, allocation.unit_id, allocation.start_date, on_date)
            total += max(int(base_qty or 0) - mortality - harvested, 0)
        return int(total)
    mortality = total_mortality_for_lot(lot.id, up_to_date=on_date)
    harvested = lot_total_harvested_units(lot.id)
    return max(int(lot.initial_count or 0) - mortality - harvested, 0)


def latest_weight_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]] | None = None):
    # Usa a linha única de observações: manejo < biometria < transferência real no mesmo dia.
    observations = merged_weight_observations(lot.id) if lot and lot.id else []
    if observations:
        return round(observations[-1]['weight_g'], 3)
    if lot and lot.estimated_weight_g is not None:
        return round(lot.estimated_weight_g, 3)
    return None


def latest_biomass_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]], allocations_by_lot: dict[int, list[LotUnitAllocation]] | None = None):
    records = records_by_lot.get(lot.id, []) if records_by_lot else []
    marker_date = latest_real_population_marker_date(lot)
    for record in sorted(records, key=lambda item: (item.manage_date, item.id), reverse=True):
        if record.estimated_biomass_kg is not None and (not marker_date or record.manage_date >= marker_date):
            return round(record.estimated_biomass_kg, 1)

    latest_weight = latest_weight_for_lot(lot, records_by_lot)
    if latest_weight is None:
        return None
    qty = allocation_live_count_for_lot(lot)
    if qty <= 0:
        return None
    return round((qty * latest_weight) / 1000, 1)


def lot_mortality_total(lot_id: int, records_by_lot: dict[int, list[DailyManagement]]):
    return int(sum(record.mortality_qty or 0 for record in records_by_lot.get(lot_id, [])))


def survival_estimate_for_lot(lot: Lot, records_by_lot: dict[int, list[DailyManagement]] | None = None, on_date=None):
    if not lot or not lot.initial_count:
        return None
    on_date = on_date or local_today()
    survivors = allocation_live_count_for_lot(lot, on_date=on_date)
    return round((survivors / lot.initial_count) * 100, 1)


def _weight_observations_from_records(records: list[DailyManagement]):
    observations = []
    for record in sorted(records, key=lambda item: (item.manage_date, item.id)):
        if record.average_weight_g is None or not record.manage_date:
            continue
        observations.append({
            'date': record.manage_date,
            'weight_g': record.average_weight_g,
        })
    return observations


def average_daily_growth(records: list[DailyManagement], lot_id: int | None = None):
    observations = merged_weight_observations(lot_id) if lot_id else _weight_observations_from_records(records)
    observations = [obs for obs in observations if obs.get('weight_g') is not None]
    if len(observations) < 2:
        return None
    latest = observations[-1]
    baseline = None
    for candidate in reversed(observations[:-1]):
        days = (latest['date'] - candidate['date']).days
        if days >= 5:
            baseline = candidate
            break
    if baseline is None:
        baseline = observations[-2]
    days = max((latest['date'] - baseline['date']).days, 1)
    return max(((latest['weight_g'] or 0) - (baseline['weight_g'] or 0)) / days, 0)


def growth_weekly_pct(records: list[DailyManagement], lot_id: int | None = None):
    observations = merged_weight_observations(lot_id) if lot_id else _weight_observations_from_records(records)
    observations = [obs for obs in observations if obs.get('weight_g') is not None]
    if len(observations) < 2:
        return None
    latest = observations[-1]
    baseline = None
    for candidate in reversed(observations[:-1]):
        days = (latest['date'] - candidate['date']).days
        if days >= 5:
            baseline = candidate
            break
    if baseline is None:
        baseline = observations[-2]
    if not baseline['weight_g']:
        return None
    return round((((latest['weight_g'] or 0) - baseline['weight_g']) / baseline['weight_g']) * 100, 1)


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
        growth = average_daily_growth(records, lot_id=lot.id)
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
        total_feed = lot_total_feed_offered_kg(lot.id)
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
    survival_now = survival_estimate_for_lot(lot, records_by_lot, on_date=today)
    standard_survival = standard_survival_pct_for_lot(lot, on_date=today)
    # Sem transferência real, usa a tabela como teto para não superestimar lotes jovens.
    # Com transferência, a quantidade real informada passa a mandar na sobrevivência.
    if not latest_transfer_for_lot(lot, on_date=today) and standard_survival is not None and survival_now is not None:
        survival_now = min(survival_now, standard_survival)
    recent_mortality = sum((record.mortality_qty or 0) for record in records if (today - record.manage_date).days <= 7)
    predicted_survival = survival_now
    if survival_now is not None and lot.initial_count:
        predicted_survival = round(max(survival_now - ((recent_mortality / lot.initial_count) * 100), 0), 1)
    partial_snapshot = lot_partial_fcr_snapshot(lot, records_by_lot, allocations_by_lot, on_date=today)
    partial_fcr = partial_snapshot['fcr']
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
    today = local_today()

    selected_lot_id = parse_int(request.args.get('lot_id'))
    selected_unit_id = parse_int(request.args.get('unit_id'))
    selected_phase = (request.args.get('phase') or '').strip()
    selected_status = (request.args.get('status') or 'ativos').strip()
    selected_supplier = (request.args.get('supplier') or '').strip()

    config = get_water_reference_config()
    all_units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()
    all_lots = Lot.query.options(joinedload(Lot.unit)).order_by(Lot.start_date.desc(), Lot.lot_code.asc()).all()
    selected_lot = next((lot for lot in all_lots if lot.id == selected_lot_id), None)

    # Quando um lote é selecionado, o dashboard passa a enxergar o ciclo inteiro
    # dele por padrão. Isso evita que custo, venda, gráfico e indicadores fiquem
    # presos no mês atual enquanto o operador está analisando um lote específico.
    default_start = selected_lot.start_date if selected_lot and selected_lot.start_date else month_start(today)
    default_end = selected_lot.end_date if selected_lot and selected_lot.end_date else today
    start_date = parse_date(request.args.get('start_date'), default_start)
    end_date = parse_date(request.args.get('end_date'), default_end)
    if selected_lot and selected_lot.start_date:
        start_date = selected_lot.start_date
        if end_date < start_date:
            end_date = default_end if default_end >= start_date else start_date
    elif end_date < start_date:
        start_date, end_date = end_date, start_date

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
    growth_map = {lot.id: growth_weekly_pct(records_by_lot.get(lot.id, []), lot.id) for lot in active_lots}

    def active_qty_for_lot(lot: Lot):
        live_count = allocation_live_count_for_lot(lot, on_date=today)
        if live_count is not None:
            return live_count
        allocated = sum((allocation.quantity_allocated or 0) for allocation in allocations_by_lot.get(lot.id, []))
        return allocated if allocated > 0 else (lot.initial_count or 0)

    def weighted_average(weight_value_pairs, digits=1):
        valid_pairs = [(float(value), float(weight)) for value, weight in weight_value_pairs if value is not None and weight and float(weight) > 0]
        if not valid_pairs:
            return None
        total_weight = sum(weight for _, weight in valid_pairs)
        if total_weight <= 0:
            return None
        weighted_sum = sum(value * weight for value, weight in valid_pairs)
        return round(weighted_sum / total_weight, digits)

    lot_active_qty_map = {lot.id: active_qty_for_lot(lot) for lot in active_lots}
    avg_growth_weekly = weighted_average([(growth_map.get(lot.id), lot_active_qty_map.get(lot.id)) for lot in active_lots], digits=1)
    avg_weight = weighted_average([(latest_weight_map.get(lot.id), lot_active_qty_map.get(lot.id)) for lot in active_lots], digits=1)
    avg_survival = weighted_average([(survival_map.get(lot.id), lot.initial_count or 0) for lot in active_lots], digits=1)

    partial_fcr_snapshots = [lot_partial_fcr_snapshot(lot, records_by_lot, allocations_by_lot, on_date=today) for lot in active_lots]
    total_feed_offered_active = round(sum(item['total_feed_kg'] for item in partial_fcr_snapshots), 1)
    total_biomass_active = round(sum(value for value in latest_biomass_map.values() if value is not None), 1)
    biomass_gain_active = round(sum(item['biomass_gain_kg'] for item in partial_fcr_snapshots), 1)
    partial_fcr = round(total_feed_offered_active / biomass_gain_active, 2) if biomass_gain_active > 0 else None
    selected_lot_feed_total_kg = round(lot_total_feed_offered_kg(selected_lot.id), 1) if selected_lot else None

    lot_summaries = [lot_financial_summary(lot) for lot in active_lots]
    total_feed_cost = round(sum(summary['feed_cost'] for summary in lot_summaries), 2)
    total_supply_cost = round(sum(summary.get('supply_cost', 0) for summary in lot_summaries), 2)
    total_larva_cost = round(sum(summary.get('larva_cost', 0) for summary in lot_summaries), 2)
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
        allocation = next((allocation for allocation in allocations_by_lot.get(lot.id, []) if allocation.unit_id == unit.id), None)
        latest_unit_mgmt = latest_mgmt_by_unit.get(unit.id)
        if (
            latest_unit_mgmt
            and latest_unit_mgmt.estimated_biomass_kg is not None
            and (not allocation or not allocation.start_date or latest_unit_mgmt.manage_date >= allocation.start_date)
        ):
            unit_biomass = round(latest_unit_mgmt.estimated_biomass_kg, 1)
        else:
            live_qty = allocation_live_count_for_lot(lot)
            if allocation and allocation.quantity_allocated:
                live_qty = max((allocation.quantity_allocated or 0) - _sum_mortality_after_marker(lot.id, allocation.unit_id, allocation.start_date, today) - _sum_harvested_after_marker(lot.id, allocation.unit_id, allocation.start_date, today), 0)
            if latest_weight_map.get(lot.id) is not None and live_qty:
                unit_biomass = round((live_qty * latest_weight_map[lot.id]) / 1000, 1)
        biomass_unit_rows.append({'unit_name': unit.name, 'biomass': unit_biomass or 0})
    biomass_unit_rows.sort(key=lambda row: row['biomass'], reverse=True)

    growth_alerts = []
    for lot in active_lots:
        weekly = growth_weekly_pct(records_by_lot.get(lot.id, []), lot.id)
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
    chart_lots = sorted(active_lots, key=lambda lot: lot.start_date, reverse=True)[:4]
    growth_observations_by_lot = {lot.id: merged_weight_observations(lot.id) for lot in chart_lots}
    growth_dates = sorted({obs['date'] for lot in chart_lots for obs in growth_observations_by_lot.get(lot.id, []) if start_date <= obs['date'] <= end_date})
    if not growth_dates:
        growth_dates = sorted({obs['date'] for lot in chart_lots for obs in growth_observations_by_lot.get(lot.id, [])})
    growth_dates = growth_dates[-6:]
    growth_chart_labels = [point.strftime('%d/%m') for point in growth_dates]
    growth_chart_datasets = []
    for idx, lot in enumerate(chart_lots):
        lot_observations = growth_observations_by_lot.get(lot.id, [])
        if not lot_observations:
            continue
        lookup = {obs['date']: round(obs['weight_g'], 2) for obs in lot_observations}
        growth_chart_datasets.append({
            'label': lot.lot_code,
            'data': [lookup.get(point) for point in growth_dates],
            'borderColor': chart_colors[idx % len(chart_colors)],
            'backgroundColor': chart_colors[idx % len(chart_colors)]
        })

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
            'feed_total_lot': selected_lot_feed_total_kg,
            'feed_total_lot_label': 'Ração total do lote' if selected_lot else 'Ração hoje',
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
            'larva_cost': total_larva_cost,
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
        if field in request.form:
            setattr(config, field, parse_float(request.form.get(field)))
    config.updated_at = datetime.utcnow()
    config.updated_by_id = getattr(current_user, 'id', None)
    db.session.commit()
    flash('Faixas de referência da água atualizadas.', 'success')
    return_to = request.form.get('return_to') or 'water_page'
    if return_to not in {'water_page', 'water_quality_page'}:
        return_to = 'water_page'
    return redirect(url_for(return_to, unit_id=request.args.get('unit_id', type=int)))


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
            cost.start_date = parse_date(request.form.get('start_date'), local_today())
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
            close_date = parse_date(request.form.get('end_date'), local_today())
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
        larva_unit_cost = parse_float(request.form.get('larva_unit_cost'))
        larva_total_cost = parse_float(request.form.get('larva_total_cost'))
        lot.larva_unit_cost = larva_unit_cost if larva_unit_cost is not None else None
        if larva_total_cost is not None:
            lot.larva_total_cost = larva_total_cost
        elif larva_unit_cost and lot.initial_count:
            lot.larva_total_cost = round((lot.initial_count / 1000) * larva_unit_cost, 2)
        else:
            lot.larva_total_cost = 0
        lot.notes = request.form.get('notes')
        if lot.status == 'encerrado' and request.form.get('end_date'):
            lot.end_date = parse_date(request.form.get('end_date'))
        if form_mode != 'edit_lot':
            db.session.add(lot)
            db.session.flush()
            db.session.add(LotUnitAllocation(lot_id=lot.id, unit_id=lot.unit_id, start_date=lot.start_date, quantity_allocated=lot.initial_count, operational_phase=normalize_phase_value(lot.phase) or (lot.unit.phase if lot.unit else None), notes='Alocação inicial do lote.'))
        else:
            sync_lot_allocations_after_lot_edit(lot, old_unit_id, old_initial_count, old_start_date)
        db.session.commit()
        flash('Lote salvo com sucesso.' if form_mode == 'edit_lot' else 'Lote cadastrado.', 'success')
        return redirect(url_for('lots_page'))
    lots = Lot.query.order_by(Lot.start_date.desc(), Lot.id.desc()).all()
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    fixed_costs = FixedCost.query.order_by(FixedCost.start_date.desc(), FixedCost.id.desc()).all()
    lot_summaries = [lot_financial_summary(lot) for lot in lots]
    return render_template('lots.html', lots=lots, units=units, fixed_costs=fixed_costs, lot_summaries=lot_summaries, today=local_today(), lot_current_units=lot_current_units, edit_lot=edit_lot, edit_cost=edit_cost)


@app.post('/water/import-sheet')
@login_required
@requires_permission('water_manage')
def import_water_sheet():
    upload = request.files.get('sheet_image')
    requested_sheet_type = (request.form.get('sheet_type', 'auto') or 'auto').strip().lower()
    sheet_date = parse_date(request.form.get('sheet_date'), local_today())

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
    transparencies = request.form.getlist('transparency_cm')

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
        monitor_date = parse_date(monitor_dates[idx], local_today())
        if not unit_id or not slot_time:
            ignored += 1
            continue

        values = {
            'dissolved_oxygen': parse_float(oxygens[idx]),
            'temperature_c': parse_float(temperatures[idx]),
            'transparency_cm': parse_float(transparencies[idx]),
            'observation': f'Importado de ficha {"diurna" if pending.get("sheet_type") == "day" else "noturna"} em {local_now().strftime("%d/%m/%Y %H:%M")}',
        }
        if all(values.get(field) is None for field in ['dissolved_oxygen', 'temperature_c', 'transparency_cm']):
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


def build_water_quality_sheet_prompt(units):
    unit_labels = ', '.join(unit.name for unit in units)
    return (
        "Leia esta foto de uma ficha de QUALIDADE DE ÁGUA da fazenda e devolva apenas JSON válido.\n\n"
        f"Unidades esperadas no sistema: {unit_labels}.\n\n"
        "Modelo da ficha: a página pode ter vários quadros, cada quadro com uma DATA e colunas "
        "pH, TAN, NITRITO, NITRATO, ALK, DUREZA e Comentários.\n"
        "Regras obrigatórias:\n"
        "1. Identifique todas as datas visíveis na folha e escolha somente a data mais recente.\n"
        "2. Extraia apenas as linhas preenchidas dentro do quadro da data mais recente. Ignore todos os quadros mais antigos.\n"
        "3. A coluna TAN deve ser retornada como ammonia.\n"
        "4. Preserve o nome da linha exatamente como aparece em row_name, por exemplo BERÇARIO - SP1 ou BERÇARIO - RG1.\n"
        "5. Para cada linha, informe row_name, ph, ammonia, nitrite, nitrate, alkalinity, hardness e observation.\n"
        "6. Use null para campos em branco, ilegíveis ou não preenchidos. Não invente dados.\n"
        "7. Se a data aparecer como 12/05/2026 ou 12/05/26, devolva latest_date em ISO: YYYY-MM-DD.\n"
        "8. Responda exatamente no formato: "
        "{\"latest_date\":\"YYYY-MM-DD\",\"readings\":[{\"row_name\":\"...\",\"ph\":8.2,\"ammonia\":1.0,\"nitrite\":2.9,\"nitrate\":null,\"alkalinity\":340,\"hardness\":450,\"observation\":null}]}\n"
    )


def extract_water_quality_sheet_data_with_openai(file_bytes: bytes, filename: str, content_type: str, units):
    if OpenAI is None:
        raise RuntimeError('A biblioteca OpenAI não está instalada no ambiente.')
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('Defina OPENAI_API_KEY para habilitar a leitura automática por foto.')

    mime_type = content_type or 'image/jpeg'
    model = os.getenv('OPENAI_VISION_MODEL', 'gpt-5.4-mini')
    client = OpenAI(api_key=api_key)
    encoded = base64.b64encode(file_bytes).decode('utf-8')

    response = client.responses.create(
        model=model,
        input=[{
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': build_water_quality_sheet_prompt(units)},
                {'type': 'input_image', 'image_url': f'data:{mime_type};base64,{encoded}'},
            ],
        }],
    )

    raw_text = getattr(response, 'output_text', '') or ''
    payload = extract_json_object(raw_text)
    readings = payload.get('readings') or []
    if not isinstance(readings, list):
        raise ValueError('Formato inválido retornado pela IA.')
    return payload


def build_water_quality_import_preview(payload, units, fallback_date=None):
    preview_rows = []
    warnings = []
    latest_date = parse_sheet_date(payload.get('latest_date'), fallback_date or local_today())
    seen_unknown_rows = []

    for item in payload.get('readings') or []:
        row_name = (item.get('row_name') or '').strip()
        values = {
            'ph': parse_float(item.get('ph')) if item.get('ph') is not None else None,
            'ammonia': parse_float(item.get('ammonia')) if item.get('ammonia') is not None else None,
            'nitrite': parse_float(item.get('nitrite')) if item.get('nitrite') is not None else None,
            'nitrate': parse_float(item.get('nitrate')) if item.get('nitrate') is not None else None,
            'alkalinity': parse_float(item.get('alkalinity')) if item.get('alkalinity') is not None else None,
            'hardness': parse_float(item.get('hardness')) if item.get('hardness') is not None else None,
        }
        observation = (item.get('observation') or '').strip() if item.get('observation') is not None else ''
        if all(values.get(field) is None for field in QUALITY_WATER_FIELDS) and not observation:
            continue

        unit = match_unit_from_sheet_row(row_name, units)
        if not unit and row_name:
            seen_unknown_rows.append(row_name)

        preview_rows.append({
            'row_name': row_name,
            'unit_id': unit.id if unit else '',
            'unit_name': unit.name if unit else '',
            'monitor_date': latest_date.isoformat(),
            'ph': values['ph'],
            'ammonia': values['ammonia'],
            'nitrite': values['nitrite'],
            'nitrate': values['nitrate'],
            'alkalinity': values['alkalinity'],
            'hardness': values['hardness'],
            'observation': observation,
            'selected': True,
        })

    if seen_unknown_rows:
        warnings.append('Algumas linhas não bateram com os viveiros cadastrados: ' + ', '.join(sorted(set(seen_unknown_rows))))
    if latest_date == (fallback_date or local_today()) and not payload.get('latest_date'):
        warnings.append('A IA não devolveu uma data clara; usei a data de hoje como fallback. Confira antes de salvar.')

    return preview_rows, warnings, latest_date


def store_pending_water_quality_import(sheet_date: date, preview_rows, warnings):
    session['pending_water_quality_import'] = {
        'sheet_date': sheet_date.isoformat(),
        'rows': preview_rows,
        'warnings': warnings,
    }


def pop_pending_water_quality_import():
    return session.pop('pending_water_quality_import', None)


def get_pending_water_quality_import():
    return session.get('pending_water_quality_import')


@app.route('/water', methods=['GET', 'POST'])
@login_required
@requires_permission('water_manage')
def water_page():
    """Monitoramento operacional: somente temperatura, OD e transparência."""
    if request.method == 'POST':
        mode = request.form.get('entry_mode', 'single')
        unit_id = int(request.form['unit_id'])
        monitor_date = parse_date(request.form.get('monitor_date'), local_today())
        lot = active_lot_for_unit(unit_id, on_date=monitor_date)

        if mode == 'batch':
            slot_times = request.form.getlist('slot_time')
            temperatures = parse_multi_float_list(request.form.getlist('temperature_c'))
            oxygens = parse_multi_float_list(request.form.getlist('dissolved_oxygen'))
            transparencies = parse_multi_float_list(request.form.getlist('transparency_cm'))
            observations = request.form.getlist('observation')

            created = 0
            for idx, slot in enumerate(slot_times):
                values = [temperatures[idx], oxygens[idx], transparencies[idx], (observations[idx] or '').strip()]
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
                    transparency_cm=transparencies[idx],
                    observation=(observations[idx] or '').strip() or None,
                )
                db.session.add(rec)
                created += 1

            if created == 0:
                flash('Preencha pelo menos um horário no lançamento em lote.', 'warning')
                return redirect(url_for('water_page', unit_id=unit_id))

            db.session.commit()
            flash(f'{created} leituras de monitoramento salvas em lote.', 'success')
            return redirect(url_for('water_page', unit_id=unit_id))

        rec = WaterMonitoring(
            monitor_date=monitor_date,
            shift=request.form['shift'],
            monitor_time=parse_time(request.form.get('monitor_time')),
            unit_id=unit_id,
            lot_id=lot.id if lot else None,
            temperature_c=parse_float(request.form.get('temperature_c')),
            dissolved_oxygen=parse_float(request.form.get('dissolved_oxygen')),
            transparency_cm=parse_float(request.form.get('transparency_cm')),
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

    records_query = WaterMonitoring.query.join(Unit).filter(physical_water_filter())
    if selected_unit_id:
        records_query = records_query.filter(WaterMonitoring.unit_id == selected_unit_id)

    sort_map = {
        'monitor_date': [WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'monitor_time': [WaterMonitoring.monitor_time, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'shift': [WaterMonitoring.shift, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'unit': [func.lower(Unit.name), WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'od': [WaterMonitoring.dissolved_oxygen, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'temperature': [WaterMonitoring.temperature_c, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
        'transparency': [WaterMonitoring.transparency_cm, WaterMonitoring.monitor_date, WaterMonitoring.monitor_time, WaterMonitoring.id],
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
        today=local_today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
        selected_unit=selected_unit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        sort_indicator=sort_indicator,
        build_sort_url=build_sort_url,
        batch_slots=batch_monitor_slots(),
        reference_config=get_water_reference_config(),
        reference_summary=build_reference_summary(fields=PHYSICAL_WATER_FIELDS),
        pending_water_import=get_pending_water_import(),
    )


@app.post('/water-quality/import-sheet')
@login_required
@requires_permission('water_manage')
def import_water_quality_sheet():
    upload = request.files.get('sheet_image')
    fallback_date = parse_date(request.form.get('fallback_date'), local_today())

    if not upload or not upload.filename:
        flash('Envie a foto da ficha antes de importar.', 'warning')
        return redirect(url_for('water_quality_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    try:
        file_bytes = upload.read()
        payload = extract_water_quality_sheet_data_with_openai(
            file_bytes=file_bytes,
            filename=upload.filename,
            content_type=upload.mimetype,
            units=units,
        )
        preview_rows, warnings, sheet_date = build_water_quality_import_preview(payload, units, fallback_date=fallback_date)
    except Exception as exc:
        flash(f'Não consegui ler a ficha de qualidade automaticamente: {exc}', 'danger')
        return redirect(url_for('water_quality_page'))

    if not preview_rows:
        flash('Não encontrei leituras válidas na data mais recente da ficha.', 'warning')
        return redirect(url_for('water_quality_page'))

    store_pending_water_quality_import(sheet_date, preview_rows, warnings)
    flash(f'Prévia da importação gerada usando a data mais recente da folha: {sheet_date.strftime("%d/%m/%Y")}. Confira antes de confirmar.', 'success')
    return redirect(url_for('water_quality_page', show_import_preview=1))


@app.post('/water-quality/import-sheet/confirm')
@login_required
@requires_permission('water_manage')
def confirm_import_water_quality_sheet():
    pending = get_pending_water_quality_import()
    if not pending:
        flash('A prévia da importação expirou. Gere a leitura da ficha novamente.', 'warning')
        return redirect(url_for('water_quality_page'))

    selected_indices = {int(value) for value in request.form.getlist('selected_indices') if str(value).isdigit()}
    unit_ids = request.form.getlist('unit_id')
    monitor_dates = request.form.getlist('monitor_date')
    ph_values = request.form.getlist('ph')
    ammonias = request.form.getlist('ammonia')
    nitrites = request.form.getlist('nitrite')
    nitrates = request.form.getlist('nitrate')
    alkalinities = request.form.getlist('alkalinity')
    hardness_values = request.form.getlist('hardness')
    observations = request.form.getlist('observation')

    created = 0
    updated = 0
    ignored = 0

    total_rows = len(unit_ids)
    for idx in range(total_rows):
        if idx not in selected_indices:
            ignored += 1
            continue
        unit_id = parse_int(unit_ids[idx])
        monitor_date = parse_date(monitor_dates[idx], local_today())
        if not unit_id:
            ignored += 1
            continue
        values = {
            'ph': parse_float(ph_values[idx]),
            'ammonia': parse_float(ammonias[idx]),
            'nitrite': parse_float(nitrites[idx]),
            'nitrate': parse_float(nitrates[idx]),
            'alkalinity': parse_float(alkalinities[idx]),
            'hardness': parse_float(hardness_values[idx]),
            'observation': (observations[idx] or '').strip() or f'Importado de ficha de qualidade em {local_now().strftime("%d/%m/%Y %H:%M")}',
        }
        if all(values.get(field) is None for field in QUALITY_WATER_FIELDS):
            ignored += 1
            continue
        result = upsert_water_reading(unit_id, monitor_date, None, values)
        if result == 'created':
            created += 1
        else:
            updated += 1

    db.session.commit()
    pop_pending_water_quality_import()
    flash(f'Importação de qualidade confirmada. {created} leitura(s) criada(s), {updated} atualizada(s) e {ignored} ignorada(s).', 'success')
    return redirect(url_for('water_quality_page'))


@app.post('/water-quality/import-sheet/cancel')
@login_required
@requires_permission('water_manage')
def cancel_import_water_quality_sheet():
    pop_pending_water_quality_import()
    flash('Prévia da importação de qualidade cancelada.', 'warning')
    return redirect(url_for('water_quality_page'))


@app.route('/water-quality', methods=['GET', 'POST'])
@login_required
@requires_permission('water_manage')
def water_quality_page():
    """Qualidade de água: parâmetros químicos e físico-químicos fora do monitoramento de rotina."""
    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()

    if request.method == 'POST':
        mode = request.form.get('entry_mode', 'batch')
        monitor_date = parse_date(request.form.get('monitor_date'), local_today())

        if mode == 'batch':
            unit_ids = request.form.getlist('unit_id')
            ph_values = parse_multi_float_list(request.form.getlist('ph'))
            salinities = parse_multi_float_list(request.form.getlist('salinity'))
            ammonias = parse_multi_float_list(request.form.getlist('ammonia'))
            nitrites = parse_multi_float_list(request.form.getlist('nitrite'))
            nitrates = parse_multi_float_list(request.form.getlist('nitrate'))
            alkalinities = parse_multi_float_list(request.form.getlist('alkalinity'))
            hardness_values = parse_multi_float_list(request.form.getlist('hardness'))
            observations = request.form.getlist('observation')

            created = 0
            updated = 0
            for idx, raw_unit_id in enumerate(unit_ids):
                unit_id = parse_int(raw_unit_id)
                values = {
                    'ph': ph_values[idx],
                    'salinity': salinities[idx],
                    'ammonia': ammonias[idx],
                    'nitrite': nitrites[idx],
                    'nitrate': nitrates[idx],
                    'alkalinity': alkalinities[idx],
                    'hardness': hardness_values[idx],
                    'observation': (observations[idx] or '').strip() or None,
                }
                if not unit_id or all(values.get(field) is None for field in QUALITY_WATER_FIELDS):
                    continue
                result = upsert_water_reading(unit_id, monitor_date, None, values)
                if result == 'created':
                    created += 1
                else:
                    updated += 1

            if created + updated == 0:
                flash('Preencha pelo menos uma unidade no lançamento em lote.', 'warning')
                return redirect(url_for('water_quality_page'))
            db.session.commit()
            flash(f'Qualidade de água salva. {created} leitura(s) criada(s) e {updated} atualizada(s).', 'success')
            return redirect(url_for('water_quality_page'))

        unit_id = int(request.form['unit_id'])
        values = {
            'ph': parse_float(request.form.get('ph')),
            'salinity': parse_float(request.form.get('salinity')),
            'ammonia': parse_float(request.form.get('ammonia')),
            'nitrite': parse_float(request.form.get('nitrite')),
            'nitrate': parse_float(request.form.get('nitrate')),
            'alkalinity': parse_float(request.form.get('alkalinity')),
            'hardness': parse_float(request.form.get('hardness')),
            'observation': request.form.get('observation'),
        }
        if all(values.get(field) is None for field in QUALITY_WATER_FIELDS):
            flash('Informe pelo menos um parâmetro de qualidade.', 'warning')
            return redirect(url_for('water_quality_page', unit_id=unit_id))
        result = upsert_water_reading(unit_id, monitor_date, None, values)
        db.session.commit()
        flash('Qualidade de água lançada.' if result == 'created' else 'Qualidade de água atualizada para essa data/unidade.', 'success')
        return redirect(url_for('water_quality_page', unit_id=unit_id))

    selected_unit_id = request.args.get('unit_id', type=int)
    sort_by = request.args.get('sort_by', 'monitor_date')
    sort_dir = 'asc' if request.args.get('sort_dir', 'desc').lower() == 'asc' else 'desc'

    records_query = WaterMonitoring.query.join(Unit).filter(quality_water_filter())
    if selected_unit_id:
        records_query = records_query.filter(WaterMonitoring.unit_id == selected_unit_id)

    sort_map = {
        'monitor_date': [WaterMonitoring.monitor_date, WaterMonitoring.id],
        'unit': [func.lower(Unit.name), WaterMonitoring.monitor_date, WaterMonitoring.id],
        'ph': [WaterMonitoring.ph, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'salinity': [WaterMonitoring.salinity, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'ammonia': [WaterMonitoring.ammonia, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'nitrite': [WaterMonitoring.nitrite, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'nitrate': [WaterMonitoring.nitrate, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'alkalinity': [WaterMonitoring.alkalinity, WaterMonitoring.monitor_date, WaterMonitoring.id],
        'hardness': [WaterMonitoring.hardness, WaterMonitoring.monitor_date, WaterMonitoring.id],
    }
    order_columns = sort_map.get(sort_by, sort_map['monitor_date'])
    ordered = [col.asc().nullslast() if sort_dir == 'asc' else col.desc().nullslast() for col in order_columns]
    records = records_query.order_by(*ordered).limit(100).all()

    edit_id = request.args.get('edit_id', type=int)
    edit_record = db.session.get(WaterMonitoring, edit_id) if edit_id else None
    selected_unit = db.session.get(Unit, selected_unit_id) if selected_unit_id else None
    return render_template(
        'water_quality.html',
        units=units,
        records=records,
        today=local_today(),
        edit_record=edit_record,
        selected_unit_id=selected_unit_id,
        selected_unit=selected_unit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        sort_indicator=sort_indicator,
        build_sort_url=build_sort_url,
        reference_config=get_water_reference_config(),
        reference_summary=build_reference_summary(fields=QUALITY_WATER_FIELDS),
        pending_water_quality_import=get_pending_water_quality_import(),
    )



OPERATION_CATEGORY_LABELS = {
    'alimentacao': 'Alimentação viveiros',
    'bercario': 'Alimentação berçário',
    'aditivo': 'Aditivo de água',
    'troca_agua': 'Troca de água',
    'rotina': 'Rotina operacional',
}

OPERATION_PRIORITY_LABELS = {
    'critica': 'Crítica',
    'alta': 'Alta',
    'media': 'Média',
    'baixa': 'Baixa',
}

OPERATION_PRIORITY_ORDER = {
    'critica': 1,
    'alta': 2,
    'media': 3,
    'baixa': 4,
}


def normalize_operation_priority(value):
    value = (value or 'media').strip().lower()
    return value if value in OPERATION_PRIORITY_ORDER else 'media'


def operation_category_label(value):
    return OPERATION_CATEGORY_LABELS.get(value or 'rotina', 'Rotina operacional')


def operation_priority_label(value):
    return OPERATION_PRIORITY_LABELS.get(normalize_operation_priority(value), 'Média')


def operation_task_source_marker(task_id):
    return f'Origem Rotina ID: {task_id}'


def operation_task_completed_record(task):
    if not task or not task.unit_id:
        return None
    return DailyManagement.query.filter(
        DailyManagement.manage_date == task.operation_date,
        DailyManagement.unit_id == task.unit_id,
        DailyManagement.notes.contains(operation_task_source_marker(task.id)),
    ).order_by(DailyManagement.id.desc()).first()


def build_operation_management_note(task):
    lines = [
        '[Integração rotina operacional]',
        'Lançamento automático pela conclusão da Rotina do Dia.',
        f'Origem Rotina ID: {task.id}',
        f'Categoria: {operation_category_label(task.category)}',
        f'Prioridade: {operation_priority_label(task.priority)}',
        f'Ação: {task.title}',
    ]
    if task.scheduled_time:
        lines.append(f'Horário programado: {task.scheduled_time.strftime("%H:%M")}')
    if task.ration_label:
        lines.append(f'Rótulo TV: {task.ration_label}')
    if task.notes:
        lines.append(f'Observações da rotina: {task.notes}')
    lines.append('[/Integração rotina operacional]')
    return '\n'.join(lines)


def completed_operation_task_ids(target_date):
    records = DailyManagement.query.filter(
        DailyManagement.manage_date == target_date,
        DailyManagement.notes.contains('[Integração rotina operacional]'),
    ).all()
    ids = set()
    for record in records:
        for match in re.findall(r'Origem Rotina ID:\s*(\d+)', record.notes or ''):
            ids.add(int(match))
    return ids


def complete_operation_task_into_management(task):
    if not task or not task.active:
        return 'skipped', 'Item inativo.'
    if operation_task_completed_record(task):
        return 'skipped', 'Já estava lançado no manejo.'
    if not task.unit_id:
        return 'skipped', 'Item sem viveiro/berçário vinculado.'

    lot = active_lot_for_unit(task.unit_id, on_date=task.operation_date)
    notes = build_operation_management_note(task)

    if task.category in ('alimentacao', 'bercario'):
        quantity_kg = feed_quantity_kg_for_task(task)
        if quantity_kg is None:
            return 'skipped', 'Alimentação precisa estar em kg ou g para baixa automática.'
        if quantity_kg <= 0:
            return 'skipped', 'Quantidade de ração zerada.'
        if not task.feed_product:
            return 'skipped', 'Ração não vinculada ao estoque.'
        validation_error = validate_feed_usage(task.feed_product, quantity_kg)
        if validation_error:
            return 'error', validation_error
        management = DailyManagement(
            manage_date=task.operation_date,
            unit_id=task.unit_id,
            lot_id=lot.id if lot else None,
            feed_product_id=task.feed_product_id,
            feed_offered_kg=quantity_kg,
            feed_consumed_kg=quantity_kg,
            mortality_qty=0,
            notes=notes,
            updated_at=datetime.utcnow(),
        )
        db.session.add(management)
        db.session.flush()
        sync_management_feed_movement(management, task.feed_product, quantity_kg)
        return 'created', 'Ração lançada no manejo.'

    if task.category in ('aditivo', 'troca_agua'):
        quantity = supply_quantity_for_stock(task)
        if quantity is None:
            return 'skipped', 'Unidade do insumo incompatível com a unidade cadastrada no estoque.'
        if quantity <= 0 or not task.supply_product:
            return 'skipped', 'Insumo/aditivo sem produto ou quantidade.'
        entries = [{'product': task.supply_product, 'quantity': quantity, 'notes': notes}]
        validation_error = validate_supply_usage(entries)
        if validation_error:
            return 'error', validation_error
        management = DailyManagement(
            manage_date=task.operation_date,
            unit_id=task.unit_id,
            lot_id=lot.id if lot else None,
            feed_offered_kg=0,
            feed_consumed_kg=0,
            mortality_qty=0,
            notes=notes,
            updated_at=datetime.utcnow(),
        )
        db.session.add(management)
        db.session.flush()
        sync_management_supply_usages(management, entries)
        return 'created', 'Insumo lançado no manejo.'

    # Rotinas sem consumo de estoque entram no manejo apenas como registro operacional,
    # desde que estejam vinculadas a uma unidade.
    management = DailyManagement(
        manage_date=task.operation_date,
        unit_id=task.unit_id,
        lot_id=lot.id if lot else None,
        feed_offered_kg=0,
        feed_consumed_kg=0,
        mortality_qty=0,
        notes=notes,
        updated_at=datetime.utcnow(),
    )
    db.session.add(management)
    return 'created', 'Rotina operacional lançada no manejo.'

def delete_previous_imported_nursery_tasks(target_date, unit_id):
    """Remove somente itens criados automaticamente pelo botão de importação.

    Assim, ao clicar novamente em "Puxar alimentação do berçário", a Rotina do Dia
    vira um espelho limpo do protocolo atual, sem manter linhas antigas ou mapeadas errado.
    """
    imported_markers = [
        'Importado da aba Alimentação berçário',
        'Importado do manejo da água/controle',
    ]
    query = OperationalTask.query.filter(
        OperationalTask.operation_date == target_date,
        OperationalTask.unit_id == unit_id,
    )
    deleted = 0
    for task in query.all():
        notes = task.notes or ''
        if any(marker in notes for marker in imported_markers):
            db.session.delete(task)
            deleted += 1
    if deleted:
        db.session.flush()
    return deleted


def import_nursery_feed_plan_to_operation_schedule(target_date):
    plans = build_nursery_digest_for_date(target_date)
    created = 0
    skipped = 0

    for plan in plans:
        unit = plan.get('unit')
        if not unit:
            skipped += 1
            continue

        # O botão "Puxar alimentação" deve sincronizar a rotina com o protocolo do dia.
        # Antes, linhas antigas importadas podiam ficar presas e bloquear uma mistura nova.
        delete_previous_imported_nursery_tasks(target_date, unit.id)

        total_day_g = sum(int(item.get('grams') or 0) for item in plan.get('mixes', []))
        schedule_count = len(plan.get('schedule', []))

        if total_day_g <= 0:
            skipped += 1
        else:
            for schedule_item in plan.get('schedule', []):
                scheduled = parse_time(schedule_item.get('time'))
                portion_g = int(schedule_item.get('grams') or 0)
                if portion_g <= 0:
                    continue

                for mix in plan.get('mixes', []):
                    source_label = (mix.get('label') or 'Ração berçário').strip()
                    mix_total_g = int(mix.get('grams') or 0)
                    portion_mix_g = round((portion_g * mix_total_g) / total_day_g, 1)
                    if portion_mix_g <= 0:
                        continue

                    product = find_or_create_nursery_feed_product(source_label, create_missing=True)
                    display_label = product.full_name if product else source_label

                    existing = OperationalTask.query.filter_by(
                        operation_date=target_date,
                        scheduled_time=scheduled,
                        category='bercario',
                        unit_id=unit.id,
                        feed_product_id=product.id if product else None,
                        ration_label=display_label,
                    ).first()
                    if existing:
                        skipped += 1
                        continue

                    task = OperationalTask(
                        operation_date=target_date,
                        scheduled_time=scheduled,
                        category='bercario',
                        priority='alta',
                        priority_order=OPERATION_PRIORITY_ORDER['alta'],
                        title=f'Alimentação berçário {unit.name}',
                        unit_id=unit.id,
                        feed_product_id=product.id if product else None,
                        ration_label=display_label,
                        quantity=portion_mix_g,
                        measure_unit='g',
                        frequency=f"{schedule_count}x ao dia",
                        notes=(
                            f"Importado da aba Alimentação berçário ({plan.get('protocol_name', 'protocolo')}). "
                            f"Mix original: {source_label}. "
                            "O sistema converte g para kg ao lançar no manejo."
                        ),
                        updated_at=datetime.utcnow(),
                    )
                    db.session.add(task)
                    created += 1

        for item in plan.get('water_items', []):
            label = item.get('label') or item.get('source_label') or 'Manejo da água'
            category = item.get('category') or 'aditivo'
            scheduled = parse_time(item.get('scheduled_time')) or time(hour=8)
            priority = normalize_operation_priority(item.get('priority') or ('alta' if category == 'aditivo' else 'media'))
            quantity = item.get('quantity')
            measure_unit = item.get('measure_unit') or 'g'
            supply_product = None
            if category in ('aditivo', 'troca_agua'):
                supply_product = find_or_create_supply_product_for_protocol(label, measure_unit=measure_unit, create_missing=True)
            display_label = supply_product.full_name if supply_product else label
            existing = OperationalTask.query.filter_by(
                operation_date=target_date,
                scheduled_time=scheduled,
                category=category,
                unit_id=unit.id,
                ration_label=display_label,
            ).first()
            if existing:
                skipped += 1
                continue
            title = f"Aplicar {display_label}"
            if category == 'rotina':
                title = display_label
            task = OperationalTask(
                operation_date=target_date,
                scheduled_time=scheduled,
                category=category,
                priority=priority,
                priority_order=OPERATION_PRIORITY_ORDER[priority],
                title=title,
                unit_id=unit.id,
                supply_product_id=supply_product.id if supply_product else None,
                ration_label=display_label,
                quantity=quantity,
                measure_unit=measure_unit,
                frequency='Protocolo diário',
                notes=f"Importado do manejo da água/controle do {plan.get('protocol_name', 'protocolo')} — estágio PL{plan.get('stage_today')}. Ao concluir o dia, itens com insumo vinculado baixam estoque e entram no Manejo Diário.",
                updated_at=datetime.utcnow(),
            )
            db.session.add(task)
            created += 1

    return created, skipped


def weekday_label_pt(value):
    labels = ['Segunda-feira', 'Terça-feira', 'Quarta-feira', 'Quinta-feira', 'Sexta-feira', 'Sábado', 'Domingo']
    return labels[value.weekday()] if value else ''


def format_decimal_pt(value, decimals=1):
    if value is None:
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.00001:
        return str(int(round(number)))
    return f"{number:.{decimals}f}".replace('.', ',')


def format_integer_pt(value):
    if value is None:
        return '0'
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return f'{number:,}'.replace(',', '.')


def spreadsheet_column_label(index: int) -> str:
    """Converte 1 -> A, 27 -> AA para o cabeçalho visual tipo Excel."""
    index = int(index or 0)
    label = ''
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label or 'A'


def feed_label_for_task(task):
    if task.ration_label:
        return task.ration_label
    if task.feed_product:
        parts = [task.feed_product.full_name]
        if task.feed_product.technical_summary:
            parts.append(task.feed_product.technical_summary)
        return ' · '.join([p for p in parts if p]) or 'Ração'
    return 'Ração não vinculada'


def supply_label_for_task(task):
    if getattr(task, 'supply_product', None):
        return task.supply_product.full_name
    if task.ration_label:
        return task.ration_label
    return task.title or 'Insumo não vinculado'


def clear_product_links_by_category(task):
    # A programação é planejamento/TV. O estoque só deve baixar no manejo real.
    if task.category in ('alimentacao', 'bercario'):
        task.supply_product_id = None
    elif task.category in ('aditivo', 'troca_agua'):
        task.feed_product_id = None




def canonical_measure_unit(unit):
    """Normaliza unidades para conversão simples entre cadastro, TV e estoque."""
    raw = (unit or '').strip()
    normalized = normalize_text(raw).replace(' ', '')
    aliases = {
        'kg': 'kg', 'quilo': 'kg', 'quilos': 'kg', 'kilograma': 'kg', 'kilogramas': 'kg',
        'g': 'g', 'gr': 'g', 'grama': 'g', 'gramas': 'g',
        'l': 'L', 'lt': 'L', 'litro': 'L', 'litros': 'L',
        'ml': 'mL', 'mililitro': 'mL', 'mililitros': 'mL',
        '%': '%', 'porcentagem': '%', 'percentual': '%',
        'un': 'un', 'unidade': 'un', 'unidades': 'un',
    }
    return aliases.get(normalized, raw or 'kg')


def convert_quantity_between_units(quantity, from_unit, to_unit):
    """Converte unidades compatíveis. Retorna None quando não há conversão segura."""
    if quantity is None:
        return None
    try:
        value = float(quantity)
    except (TypeError, ValueError):
        return None
    source = canonical_measure_unit(from_unit)
    target = canonical_measure_unit(to_unit)
    if source == target:
        return value
    # Conversões diretas entre massa e volume usando densidade 1:1.
    # Isso permite baixar, por exemplo, Melaço lançado em gramas no protocolo
    # contra estoque cadastrado em litros: 600 g -> 0,6 L.
    conversions = {
        ('g', 'kg'): value / 1000,
        ('kg', 'g'): value * 1000,
        ('mL', 'L'): value / 1000,
        ('L', 'mL'): value * 1000,
        ('g', 'mL'): value,
        ('mL', 'g'): value,
        ('kg', 'L'): value,
        ('L', 'kg'): value,
        ('g', 'L'): value / 1000,
        ('L', 'g'): value * 1000,
        ('kg', 'mL'): value * 1000,
        ('mL', 'kg'): value / 1000,
    }
    return conversions.get((source, target))


def feed_quantity_kg_for_task(task):
    return convert_quantity_between_units(task.quantity or 0, task.measure_unit or 'kg', 'kg')


def feed_quantity_g_for_task(task):
    return convert_quantity_between_units(task.quantity or 0, task.measure_unit or 'kg', 'g')


def supply_quantity_for_stock(task):
    if not task or not task.supply_product:
        return None
    return convert_quantity_between_units(task.quantity or 0, task.measure_unit or task.supply_product.measure_unit, task.supply_product.measure_unit)


def operation_task_is_completed(task, completed_task_ids=None):
    if not task:
        return False
    if completed_task_ids is not None:
        return task.id in completed_task_ids
    return bool(operation_task_completed_record(task))


def operation_stock_warnings(tasks, completed_task_ids=None):
    """Mostra pendências prováveis antes de concluir o dia, sem baixar estoque."""
    warnings = []
    feed_needed = defaultdict(float)
    supply_needed = defaultdict(float)
    for task in tasks:
        if not task.active or operation_task_is_completed(task, completed_task_ids):
            continue
        if task.category in ('alimentacao', 'bercario'):
            qty_kg = feed_quantity_kg_for_task(task)
            if not task.feed_product:
                warnings.append(f'{task_display_unit(task)} · {task.title}: ração não vinculada ao estoque.')
                continue
            if qty_kg is None:
                warnings.append(f'{task_display_unit(task)} · {task.title}: unidade de ração inválida para baixa no estoque.')
                continue
            feed_needed[task.feed_product_id] += qty_kg
        elif task.category in ('aditivo', 'troca_agua'):
            if not task.supply_product:
                warnings.append(f'{task_display_unit(task)} · {task.title}: insumo/aditivo não vinculado ao estoque.')
                continue
            qty_stock = supply_quantity_for_stock(task)
            if qty_stock is None:
                warnings.append(f'{task_display_unit(task)} · {task.title}: unidade incompatível com o estoque do insumo.')
                continue
            supply_needed[task.supply_product_id] += qty_stock
    for product_id, needed in feed_needed.items():
        product = db.session.get(FeedProduct, product_id)
        available = available_stock_for_product(product_id)
        if needed > available:
            warnings.append(f'{product.full_name if product else "Ração"}: programado {format_decimal_pt(needed)} kg, disponível {format_decimal_pt(available)} kg.')
    for product_id, needed in supply_needed.items():
        product = db.session.get(SupplyProduct, product_id)
        available = available_stock_for_supply(product_id)
        unit_label = product.measure_unit if product else 'un'
        if needed > available:
            warnings.append(f'{product.full_name if product else "Insumo"}: programado {format_decimal_pt(needed)} {unit_label}, disponível {format_decimal_pt(available)} {unit_label}.')
    return warnings


def feed_product_option_label(product, stock_map=None):
    label = product.full_name
    if product.technical_summary:
        label += f' · {product.technical_summary}'
    if stock_map is not None:
        label += f' — saldo: {format_decimal_pt(stock_map.get(product.id, 0))} kg'
    return label


def supply_product_option_label(product, stock_map=None):
    label = product.full_name
    if product.technical_summary:
        label += f' · {product.technical_summary}'
    if stock_map is not None:
        label += f' — saldo: {format_decimal_pt(stock_map.get(product.id, 0))} {product.measure_unit}'
    return label

def quantity_label_for_task(task):
    if task.quantity is None:
        return ''
    unit = task.measure_unit or 'kg'
    return f"{format_decimal_pt(task.quantity)} {unit}"


def task_display_unit(task):
    return task.unit.name if task.unit else 'Geral'


def task_payload(task):
    return {
        'id': task.id,
        'time': task.scheduled_time.strftime('%H:%M') if task.scheduled_time else '--:--',
        'category': task.category,
        'category_label': operation_category_label(task.category),
        'priority': task.priority,
        'priority_label': operation_priority_label(task.priority),
        'priority_order': task.priority_order or OPERATION_PRIORITY_ORDER.get(task.priority, 3),
        'title': supply_label_for_task(task) if task.category in ('aditivo', 'troca_agua') else task.title,
        'unit': task_display_unit(task),
        'feed_label': feed_label_for_task(task),
        'supply_label': supply_label_for_task(task),
        'quantity_label': quantity_label_for_task(task),
        'frequency': task.frequency or '',
        'notes': task.notes or '',
    }



TV_UNIT_COLOR_CLASSES = ['green', 'blue', 'orange', 'purple', 'teal', 'pink']


def tv_unit_color_map(unit_names):
    mapping = {}
    for idx, name in enumerate(sorted([u for u in unit_names if u])):
        mapping[name] = TV_UNIT_COLOR_CLASSES[idx % len(TV_UNIT_COLOR_CLASSES)]
    return mapping


def smart_quantity_from_kg(value_kg):
    if value_kg is None:
        return ''
    if abs(value_kg) >= 1:
        return f"{format_decimal_pt(value_kg)} kg"
    return f"{format_integer_pt(round(value_kg * 1000))} g"


def group_tv_feeding_by_unit(tasks, color_map=None):
    color_map = color_map or {}
    grouped = {}
    for task in tasks:
        unit_label = task_display_unit(task)
        entry = grouped.setdefault(unit_label, {
            'unit': unit_label,
            'color': color_map.get(unit_label, 'green'),
            'feeds': {},
            'times': [],
        })
        if task.scheduled_time:
            entry['times'].append(task.scheduled_time.strftime('%H:%M'))
        feed_label = feed_label_for_task(task)
        key = (feed_label, task.measure_unit or 'kg')
        feed = entry['feeds'].setdefault(key, {
            'label': feed_label,
            'measure_unit': task.measure_unit or 'kg',
            'per_offer_label': quantity_label_for_task(task),
            'count': 0,
            'total_kg': 0.0,
        })
        feed['count'] += 1
        qty_kg = feed_quantity_kg_for_task(task)
        if qty_kg is not None:
            feed['total_kg'] += qty_kg
    rows = []
    for unit_label, entry in grouped.items():
        feed_items = []
        for feed in entry['feeds'].values():
            feed_items.append({
                'label': feed['label'],
                'per_offer_label': feed['per_offer_label'],
                'frequency_label': f"{feed['count']}x ao dia",
                'total_label': smart_quantity_from_kg(feed['total_kg']),
            })
        rows.append({
            'unit': entry['unit'],
            'color': entry['color'],
            'feed_items': sorted(feed_items, key=lambda item: item['label']),
            'times_label': ' • '.join(sorted(set(entry['times']))) if entry['times'] else 'Sem horário fixo',
        })
    return sorted(rows, key=lambda row: row['unit'])


def group_tv_additives_by_unit(tasks, color_map=None):
    color_map = color_map or {}
    grouped = {}
    for task in tasks:
        unit_label = task_display_unit(task)
        entry = grouped.setdefault(unit_label, {
            'unit': unit_label,
            'color': color_map.get(unit_label, 'purple'),
            'items': {},
        })
        title = supply_label_for_task(task)
        unit = task.measure_unit or (task.supply_product.measure_unit if task.supply_product else '') or ''
        key = (title, unit)
        item = entry['items'].setdefault(key, {
            'title': title,
            'measure_unit': unit,
            'quantity': 0.0,
            'count': 0,
            'times': [],
        })
        item['quantity'] += task.quantity or 0
        item['count'] += 1
        if task.scheduled_time:
            item['times'].append(task.scheduled_time.strftime('%H:%M'))
    rows = []
    for unit_label, entry in grouped.items():
        items = []
        for item in entry['items'].values():
            qty_label = f"{format_decimal_pt(item['quantity'])} {item['measure_unit']}".strip()
            items.append({
                'title': item['title'],
                'quantity_label': qty_label,
                'count_label': f"{item['count']}x",
                'times_label': ' • '.join(sorted(set(item['times']))),
            })
        rows.append({
            'unit': entry['unit'],
            'color': entry['color'],
            'items': sorted(items, key=lambda item: item['title']),
        })
    return sorted(rows, key=lambda row: row['unit'])


def group_tv_activities(tasks, completed_ids=None, color_map=None):
    color_map = color_map or {}
    completed_ids = completed_ids or set()
    grouped = {}
    for task in tasks:
        unit_label = task_display_unit(task)
        key = (task.title, unit_label, task.priority)
        entry = grouped.setdefault(key, {
            'title': task.title,
            'unit': unit_label,
            'color': color_map.get(unit_label, 'orange'),
            'priority': task.priority,
            'priority_label': operation_priority_label(task.priority),
            'priority_order': task.priority_order or OPERATION_PRIORITY_ORDER.get(task.priority, 3),
            'times': [],
            'pending_times': [],
            'count': 0,
        })
        entry['count'] += 1
        if task.scheduled_time:
            time_label = task.scheduled_time.strftime('%H:%M')
            entry['times'].append(time_label)
            if task.id not in completed_ids:
                entry['pending_times'].append(time_label)
    rows = []
    for entry in grouped.values():
        next_time = sorted(entry['pending_times'] or entry['times'])
        rows.append({
            'title': entry['title'],
            'unit': entry['unit'],
            'color': entry['color'],
            'priority': entry['priority'],
            'priority_label': entry['priority_label'],
            'priority_order': entry['priority_order'],
            'frequency_label': f"{entry['count']}x hoje",
            'next_time': next_time[0] if next_time else '--:--',
        })
    return sorted(rows, key=lambda row: (row['priority_order'], row['next_time'], row['title'], row['unit']))


def group_tv_feeding_rows(tasks):
    grouped = defaultdict(lambda: defaultdict(list))
    for task in tasks:
        time_label = task.scheduled_time.strftime('%H:%M') if task.scheduled_time else '--:--'
        unit_label = task_display_unit(task)
        grouped[time_label][unit_label].append(f'{feed_label_for_task(task)}: {quantity_label_for_task(task)}')
    rows = []
    for time_label in sorted(grouped.keys()):
        item_labels = []
        for unit_label in sorted(grouped[time_label].keys()):
            item_labels.append(f'{unit_label} — ' + ' | '.join(grouped[time_label][unit_label]))
        rows.append({'time': time_label, 'item_labels': item_labels})
    return rows

def build_tv_dashboard_data(target_date):
    tasks = (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.operation_date == target_date, OperationalTask.active.is_(True))
        .order_by(OperationalTask.scheduled_time.asc().nullslast(), OperationalTask.priority_order.asc(), OperationalTask.id.asc())
        .all()
    )
    completed_ids = completed_operation_task_ids(target_date)
    feeding_tasks = [t for t in tasks if t.category in ('alimentacao', 'bercario')]
    additive_tasks = [t for t in tasks if t.category in ('aditivo', 'troca_agua')]
    routine_tasks = [t for t in tasks if t.category == 'rotina']
    priority_routines = sorted(routine_tasks, key=lambda t: (t.priority_order or 3, t.scheduled_time or time(23, 59), t.id))
    priority_current = next((t for t in priority_routines if t.id not in completed_ids), priority_routines[0] if priority_routines else None)
    total_feed_kg = 0.0
    nursery_total_g = 0.0
    for task in feeding_tasks:
        qty_kg = feed_quantity_kg_for_task(task)
        if qty_kg is not None:
            total_feed_kg += qty_kg
        if task.category == 'bercario':
            qty_g = feed_quantity_g_for_task(task)
            if qty_g is not None:
                nursery_total_g += qty_g
    completed_count = len([t for t in tasks if t.id in completed_ids])
    pending_count = len([t for t in tasks if t.id not in completed_ids])
    all_units = {task_display_unit(t) for t in feeding_tasks + additive_tasks + routine_tasks}
    unit_colors = tv_unit_color_map(all_units)
    return {
        'tasks': tasks,
        'feeding_rows': group_tv_feeding_rows(feeding_tasks),
        'feeding_by_unit': group_tv_feeding_by_unit(feeding_tasks, unit_colors),
        'additives': [task_payload(t) for t in additive_tasks],
        'additives_by_unit': group_tv_additives_by_unit(additive_tasks, unit_colors),
        'priority_routines': [task_payload(t) for t in priority_routines],
        'activities_by_unit': group_tv_activities(priority_routines, completed_ids, unit_colors),
        'priority_current': task_payload(priority_current) if priority_current else None,
        'total_feed_kg': round(total_feed_kg, 1),
        'nursery_total_g': round(nursery_total_g, 0),
        'water_changes': len([t for t in additive_tasks if t.category == 'troca_agua']),
        'additive_count': len([t for t in additive_tasks if t.category == 'aditivo']),
        'routine_count': len(routine_tasks),
        'completed_count': completed_count,
        'in_progress_count': max(0, len(tasks) - completed_count - pending_count),
        'pending_count': pending_count,
    }

@app.context_processor
def inject_operation_helpers():
    return {
        'operation_category_label': operation_category_label,
        'operation_priority_label': operation_priority_label,
        'operation_category_labels': OPERATION_CATEGORY_LABELS,
        'operation_priority_labels': OPERATION_PRIORITY_LABELS,
        'format_decimal_pt': format_decimal_pt,
        'format_integer_pt': format_integer_pt,
        'weekday_label_pt': weekday_label_pt,
        'feed_product_option_label': feed_product_option_label,
        'supply_product_option_label': supply_product_option_label,
        'quantity_label_for_task': quantity_label_for_task,
        'feed_label_for_task': feed_label_for_task,
        'supply_label_for_task': supply_label_for_task,
        'nursery_entry_water_items': nursery_entry_water_items,
    }


@app.route('/operation-schedule', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def operation_schedule_page():
    selected_date = parse_date(request.values.get('operation_date'), local_today())
    if request.method == 'POST':
        category = request.form.get('category') or 'rotina'
        priority = normalize_operation_priority(request.form.get('priority'))
        title = (request.form.get('title') or '').strip()
        feed_product_id = request.form.get('feed_product_id', type=int)
        supply_product_id = request.form.get('supply_product_id', type=int)
        selected_feed = db.session.get(FeedProduct, feed_product_id) if feed_product_id else None
        selected_supply = db.session.get(SupplyProduct, supply_product_id) if supply_product_id else None
        if category in ('alimentacao', 'bercario'):
            supply_product_id = None
            selected_supply = None
        elif category in ('aditivo', 'troca_agua'):
            feed_product_id = None
            selected_feed = None
        ration_label = (request.form.get('ration_label') or '').strip()
        unit_id = request.form.get('unit_id', type=int)
        if not title:
            if category in ('alimentacao', 'bercario'):
                title = f"Alimentação {request.form.get('scheduled_time') or ''}".strip()
            elif category == 'troca_agua':
                title = 'Troca de água'
            elif category == 'aditivo':
                title = ration_label or (selected_supply.full_name if selected_supply else 'Aditivo de água')
            else:
                flash('Informe o nome da rotina.', 'danger')
                return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))
        task = OperationalTask(
            operation_date=selected_date,
            scheduled_time=parse_time(request.form.get('scheduled_time')),
            category=category,
            priority=priority,
            priority_order=OPERATION_PRIORITY_ORDER[priority],
            title=title,
            unit_id=unit_id,
            feed_product_id=feed_product_id,
            supply_product_id=supply_product_id,
            ration_label=ration_label or None,
            quantity=parse_float(request.form.get('quantity')),
            measure_unit=(request.form.get('measure_unit') or 'kg').strip(),
            frequency=(request.form.get('frequency') or '').strip(),
            notes=(request.form.get('notes') or '').strip(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(task)
        db.session.flush()
        if task.category == 'alimentacao' and ensure_auto_tray_check_after_feeding(task):
            flash('Verificação de bandeja gerada automaticamente 1h30 após a alimentação.', 'info')
        db.session.commit()
        flash('Item adicionado à rotina operacional do dia.', 'success')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    units = Unit.query.filter_by(active=True).order_by(Unit.name).all()
    feed_products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()
    supply_products = SupplyProduct.query.order_by(SupplyProduct.active.desc(), SupplyProduct.name.asc()).all()
    feed_stock_map = {row['product'].id: row['stock_kg'] for row in build_feed_stock_snapshot()['rows'] if row.get('product')}
    supply_stock_map = {row['product'].id: row['stock_qty'] for row in build_supply_stock_snapshot()['rows'] if row.get('product')}
    tasks = (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.operation_date == selected_date)
        .order_by(OperationalTask.scheduled_time.asc().nullslast(), OperationalTask.priority_order.asc(), OperationalTask.category.asc(), OperationalTask.id.asc())
        .all()
    )
    completed_task_ids = completed_operation_task_ids(selected_date)
    stock_warnings = operation_stock_warnings(tasks, completed_task_ids)
    operation_unit_cards = build_operation_unit_cards(tasks, completed_task_ids)
    return render_template(
        'operation_schedule.html',
        selected_date=selected_date,
        units=units,
        feed_products=feed_products,
        supply_products=supply_products,
        feed_stock_map=feed_stock_map,
        supply_stock_map=supply_stock_map,
        tasks=tasks,
        operation_unit_cards=operation_unit_cards,
        completed_task_ids=completed_task_ids,
        stock_warnings=stock_warnings,
        week_end_date=selected_date + timedelta(days=6),
    )


def selected_operation_units_from_form(default_all=False):
    unit_ids = [parse_int(value) for value in request.form.getlist('unit_ids')]
    unit_ids = [value for value in unit_ids if value]
    unit_scope = request.form.get('unit_scope')
    if unit_scope == 'all' or (default_all and not unit_ids):
        return Unit.query.filter_by(active=True).order_by(Unit.name).all()
    if not unit_ids:
        return []
    return Unit.query.filter(Unit.id.in_(unit_ids), Unit.active.is_(True)).order_by(Unit.name).all()


def create_operational_task_for_unit(operation_date, unit, category, priority, scheduled_time, title, feed_product, supply_product, ration_label, quantity, measure_unit, frequency, notes):
    feed_product_id = feed_product.id if feed_product else None
    supply_product_id = supply_product.id if supply_product else None
    selected_title = (title or '').strip()
    selected_label = (ration_label or '').strip()
    if category in ('alimentacao', 'bercario'):
        supply_product_id = None
        selected_label = selected_label or (feed_product.full_name if feed_product else '')
        selected_title = selected_title or f'Alimentação {unit.name}'
    elif category in ('aditivo', 'troca_agua'):
        feed_product_id = None
        selected_label = selected_label or (supply_product.full_name if supply_product else '')
        if category == 'troca_agua':
            selected_title = selected_title or 'Troca de água'
        else:
            selected_title = selected_title or selected_label or f'Aditivo {unit.name}'
    else:
        feed_product_id = None
        supply_product_id = None
        selected_title = selected_title or f'Rotina operacional {unit.name}'
    return OperationalTask(
        operation_date=operation_date,
        scheduled_time=scheduled_time,
        category=category,
        priority=priority,
        priority_order=OPERATION_PRIORITY_ORDER[priority],
        title=selected_title,
        unit_id=unit.id,
        feed_product_id=feed_product_id,
        supply_product_id=supply_product_id,
        ration_label=selected_label or None,
        quantity=quantity,
        measure_unit=measure_unit,
        frequency=frequency,
        notes=notes,
        updated_at=datetime.utcnow(),
    )


AUTO_TRAY_CHECK_TITLE = 'Verificar bandeja'
AUTO_TRAY_CHECK_MARKER = '[Rotina automática: verificar bandeja]'


def time_plus_minutes(value, minutes):
    if not value:
        return None
    return (datetime.combine(date(2000, 1, 1), value) + timedelta(minutes=minutes)).time()


def tray_check_notes_for_feeding_task(task):
    feed_name = feed_label_for_task(task)
    time_label = task.scheduled_time.strftime('%H:%M') if task.scheduled_time else '--:--'
    return '\n'.join([
        AUTO_TRAY_CHECK_MARKER,
        'Gerada automaticamente 1h30 após a alimentação do viveiro.',
        f'Origem alimentação ID: {task.id}',
        f'Alimentação de referência: {time_label} · {feed_name} · {quantity_label_for_task(task)}',
        'Não consome estoque. Serve para orientar a conferência de sobra na bandeja.',
    ])


def auto_tray_check_for_feeding_task(task):
    if not task or not task.id:
        return None
    marker = f'Origem alimentação ID: {task.id}'
    return OperationalTask.query.filter(
        OperationalTask.operation_date == task.operation_date,
        OperationalTask.category == 'rotina',
        OperationalTask.notes.contains(marker),
    ).order_by(OperationalTask.id.asc()).first()


def ensure_auto_tray_check_after_feeding(task):
    """Cria/atualiza a rotina de verificar bandeja 1h30 após uma alimentação de viveiro."""
    if not task or task.category != 'alimentacao' or not task.unit_id or not task.scheduled_time or not task.id:
        return None
    check_time = time_plus_minutes(task.scheduled_time, 90)
    auto_task = auto_tray_check_for_feeding_task(task)
    if not auto_task:
        # Evita duplicar caso já exista uma conferência automática no mesmo horário/viveiro.
        auto_task = OperationalTask.query.filter(
            OperationalTask.operation_date == task.operation_date,
            OperationalTask.unit_id == task.unit_id,
            OperationalTask.category == 'rotina',
            OperationalTask.scheduled_time == check_time,
            OperationalTask.title == AUTO_TRAY_CHECK_TITLE,
            OperationalTask.notes.contains(AUTO_TRAY_CHECK_MARKER),
        ).order_by(OperationalTask.id.asc()).first()
    if not auto_task:
        auto_task = OperationalTask(
            operation_date=task.operation_date,
            unit_id=task.unit_id,
            category='rotina',
            title=AUTO_TRAY_CHECK_TITLE,
            created_at=datetime.utcnow(),
        )
        db.session.add(auto_task)
    auto_task.scheduled_time = check_time
    auto_task.priority = 'media'
    auto_task.priority_order = OPERATION_PRIORITY_ORDER['media']
    auto_task.feed_product_id = None
    auto_task.supply_product_id = None
    auto_task.ration_label = 'Conferir sobra de ração'
    auto_task.quantity = None
    auto_task.measure_unit = 'un'
    auto_task.frequency = 'Automática'
    auto_task.notes = tray_check_notes_for_feeding_task(task)
    auto_task.active = True if task.active is None else bool(task.active)
    auto_task.updated_at = datetime.utcnow()
    return auto_task


def delete_auto_tray_check_for_feeding_task(task):
    auto_task = auto_tray_check_for_feeding_task(task)
    if auto_task:
        db.session.delete(auto_task)
        return True
    return False


def build_operation_unit_cards(tasks, completed_task_ids=None):
    """Agrupa a Rotina do Dia por viveiro/berçário para evitar uma lista sem fim por horário."""
    completed_task_ids = completed_task_ids or set()
    colors = ['green', 'blue', 'orange', 'purple', 'teal', 'pink']
    grouped = OrderedDict()

    for task in tasks:
        key = task.unit_id or 'general'
        if key not in grouped:
            unit_name = task.unit.name if task.unit else 'Geral'
            phase = (getattr(task.unit, 'phase', '') or '').capitalize() if task.unit else 'Geral'
            grouped[key] = {
                'key': key,
                'unit': task.unit,
                'unit_id': task.unit_id,
                'unit_name': unit_name,
                'phase': phase,
                'color': colors[(len(grouped)) % len(colors)],
                'task_ids': [],
                'completed_ids': [],
                'pending_ids': [],
                'times': [],
                'priority_order': 9,
                'priority': 'media',
                'feeding_map': OrderedDict(),
                'additive_map': OrderedDict(),
                'activity_map': OrderedDict(),
            }

        card = grouped[key]
        card['task_ids'].append(task.id)
        if task.id in completed_task_ids:
            card['completed_ids'].append(task.id)
        else:
            card['pending_ids'].append(task.id)
        if task.scheduled_time:
            card['times'].append(task.scheduled_time.strftime('%H:%M'))
        if (task.priority_order or 9) < card['priority_order']:
            card['priority_order'] = task.priority_order or 9
            card['priority'] = task.priority

        if task.category in ('alimentacao', 'bercario'):
            label = feed_label_for_task(task)
            bucket = card['feeding_map'].setdefault(label, {
                'label': label,
                'count': 0,
                'quantity': task.quantity,
                'measure_unit': task.measure_unit or 'kg',
                'quantity_label': quantity_label_for_task(task),
                'total_quantity': 0.0,
                'can_sum': True,
                'frequency': task.frequency,
                'task_ids': [],
                'category': task.category,
                'priority': task.priority,
                'title': task.title,
                'feed_product_id': task.feed_product_id,
                'supply_product_id': task.supply_product_id,
                'ration_label': task.ration_label or label,
                'notes': task.notes or '',
            })
            bucket['task_ids'].append(task.id)
            bucket['count'] += 1
            if not bucket.get('frequency') and task.frequency:
                bucket['frequency'] = task.frequency
            try:
                if canonical_measure_unit(task.measure_unit or bucket['measure_unit']) == canonical_measure_unit(bucket['measure_unit']):
                    bucket['total_quantity'] += float(task.quantity or 0)
                else:
                    bucket['can_sum'] = False
            except (TypeError, ValueError):
                bucket['can_sum'] = False

        elif task.category in ('aditivo', 'troca_agua'):
            label = supply_label_for_task(task)
            bucket = card['additive_map'].setdefault(label, {
                'label': label,
                'count': 0,
                'quantity': task.quantity,
                'measure_unit': task.measure_unit or 'kg',
                'quantity_label': quantity_label_for_task(task),
                'total_quantity': 0.0,
                'can_sum': True,
                'frequency': task.frequency,
                'task_ids': [],
                'category': task.category,
                'priority': task.priority,
                'title': task.title,
                'feed_product_id': task.feed_product_id,
                'supply_product_id': task.supply_product_id,
                'ration_label': task.ration_label or label,
                'notes': task.notes or '',
            })
            bucket['task_ids'].append(task.id)
            bucket['count'] += 1
            if not bucket.get('frequency') and task.frequency:
                bucket['frequency'] = task.frequency
            try:
                if canonical_measure_unit(task.measure_unit or bucket['measure_unit']) == canonical_measure_unit(bucket['measure_unit']):
                    bucket['total_quantity'] += float(task.quantity or 0)
                else:
                    bucket['can_sum'] = False
            except (TypeError, ValueError):
                bucket['can_sum'] = False

        else:
            title = 'Verificar bandeja' if AUTO_TRAY_CHECK_MARKER in (task.notes or '') else (task.title or operation_category_label(task.category))
            bucket = card['activity_map'].setdefault(title, {
                'title': title,
                'count': 0,
                'category': task.category,
                'category_label': operation_category_label(task.category),
                'next_time': None,
                'priority': task.priority,
                'task_ids': [],
                'quantity': task.quantity,
                'measure_unit': task.measure_unit or 'un',
                'frequency': task.frequency,
                'ration_label': task.ration_label or '',
                'notes': task.notes or '',
            })
            bucket['task_ids'].append(task.id)
            bucket['count'] += 1
            if task.scheduled_time and not bucket['next_time']:
                bucket['next_time'] = task.scheduled_time.strftime('%H:%M')

    cards = []
    for card in grouped.values():
        feeding = []
        for item in card['feeding_map'].values():
            if item['can_sum'] and item['count'] > 1:
                item['total_label'] = f"{format_decimal_pt(item['total_quantity'])} {item['measure_unit']}"
            else:
                item['total_label'] = item['quantity_label'] or '-'
            item['frequency_label'] = item.get('frequency') or f"{item['count']}x hoje"
            feeding.append(item)

        additives = []
        for item in card['additive_map'].values():
            if item['can_sum'] and item['count'] > 1:
                item['total_label'] = f"{format_decimal_pt(item['total_quantity'])} {item['measure_unit']}"
            else:
                item['total_label'] = item['quantity_label'] or '-'
            item['frequency_label'] = item.get('frequency') or f"{item['count']}x hoje"
            additives.append(item)

        activities = list(card['activity_map'].values())
        card['feeding'] = feeding
        card['additives'] = additives
        card['activities'] = activities
        card['times'] = sorted(set(card['times']))
        card['pending_count'] = len(card['pending_ids'])
        card['completed_count'] = len(card['completed_ids'])
        card['status_label'] = 'Concluído' if card['pending_count'] == 0 and card['task_ids'] else 'Pendente'
        cards.append(card)

    return cards


@app.post('/operation-schedule/batch-day')
@login_required
@requires_permission('management_manage')
def batch_day_operation_schedule():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    unit_id = request.form.get('unit_id', type=int)
    unit = db.session.get(Unit, unit_id) if unit_id else None
    if not unit or not unit.active:
        flash('Selecione o viveiro/berçário antes de salvar as rotinas em lote.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    categories = request.form.getlist('row_category')
    start_times = request.form.getlist('row_start_time') or request.form.getlist('row_scheduled_time')
    priorities = request.form.getlist('row_priority')
    titles = request.form.getlist('row_title')
    feed_ids = request.form.getlist('row_feed_product_id')
    supply_ids = request.form.getlist('row_supply_product_id')
    ration_labels = request.form.getlist('row_ration_label')
    total_quantities = request.form.getlist('row_total_quantity') or request.form.getlist('row_quantity')
    measure_units = request.form.getlist('row_measure_unit')
    frequency_counts = request.form.getlist('row_frequency_count')
    interval_minutes_list = request.form.getlist('row_interval_minutes')
    notes_list = request.form.getlist('row_notes')

    total_rows = max(
        len(categories), len(start_times), len(priorities), len(titles), len(feed_ids), len(supply_ids),
        len(ration_labels), len(total_quantities), len(measure_units), len(frequency_counts),
        len(interval_minutes_list), len(notes_list), 0
    )
    created = 0
    auto_created = 0
    skipped = 0

    def row_value(values, index, default=''):
        return values[index] if index < len(values) else default

    def safe_frequency_count(raw_value):
        count = parse_int(raw_value)
        if not count or count < 1:
            return 1
        return min(count, 12)

    def safe_interval_minutes(raw_value, frequency_count):
        minutes = parse_int(raw_value)
        if minutes is None:
            minutes = 180 if frequency_count > 1 else 0
        return max(0, min(minutes, 720))

    for idx in range(total_rows):
        category = row_value(categories, idx, 'rotina') or 'rotina'
        priority = normalize_operation_priority(row_value(priorities, idx, 'media'))
        start_time = parse_time(row_value(start_times, idx, ''))
        title = (row_value(titles, idx, '') or '').strip()
        ration_label = (row_value(ration_labels, idx, '') or '').strip()
        total_quantity = parse_float(row_value(total_quantities, idx, ''))
        measure_unit = (row_value(measure_units, idx, 'kg') or ('g' if category == 'bercario' else 'kg')).strip()
        frequency_count = safe_frequency_count(row_value(frequency_counts, idx, '1'))
        interval_minutes = safe_interval_minutes(row_value(interval_minutes_list, idx, ''), frequency_count)
        user_notes = (row_value(notes_list, idx, '') or '').strip()
        feed_product_id = parse_int(row_value(feed_ids, idx, ''))
        supply_product_id = parse_int(row_value(supply_ids, idx, ''))
        feed_product = db.session.get(FeedProduct, feed_product_id) if feed_product_id else None
        supply_product = db.session.get(SupplyProduct, supply_product_id) if supply_product_id else None

        # Linha totalmente vazia não entra.
        if not any([title, ration_label, total_quantity, feed_product, supply_product, user_notes]):
            skipped += 1
            continue
        if not start_time:
            skipped += 1
            continue
        # Alimentação/aditivo/troca sem quantidade não deve gerar baixa futura confusa.
        if category != 'rotina' and (total_quantity is None or total_quantity <= 0):
            skipped += 1
            continue

        portion_quantity = None
        if total_quantity is not None:
            portion_quantity = round(total_quantity / frequency_count, 4) if frequency_count > 1 else total_quantity

        frequency_label = f'{frequency_count}x ao dia'
        if frequency_count > 1 and interval_minutes:
            hours = interval_minutes // 60
            minutes = interval_minutes % 60
            interval_label = f'{hours}h{minutes:02d}' if minutes else f'{hours}h'
            frequency_label = f'{frequency_label} · intervalo {interval_label}'
        system_note = ''
        if frequency_count > 1:
            system_note = f'Gerado automaticamente pela frequência diária: total {format_decimal_pt(total_quantity)} {measure_unit}, {frequency_count} trato(s), intervalo de {interval_minutes} min.'
        notes = user_notes
        if system_note:
            notes = f'{user_notes}\n{system_note}' if user_notes else system_note

        for occurrence in range(frequency_count):
            scheduled_time = time_plus_minutes(start_time, occurrence * interval_minutes) if occurrence else start_time
            task = create_operational_task_for_unit(
                selected_date, unit, category, priority, scheduled_time, title,
                feed_product, supply_product, ration_label, portion_quantity, measure_unit, frequency_label, notes,
            )
            db.session.add(task)
            db.session.flush()
            created += 1
            if category == 'alimentacao' and ensure_auto_tray_check_after_feeding(task):
                auto_created += 1

    if created:
        db.session.commit()
        msg = f'{created} rotina(s) criada(s) para {unit.name}.'
        if auto_created:
            msg += f' {auto_created} verificação(ões) de bandeja foram geradas automaticamente 1h30 após cada ração.'
        flash(msg, 'success')
        if skipped:
            flash(f'{skipped} linha(s) vazia(s), sem horário ou incompletas foram ignoradas.', 'info')
    else:
        db.session.rollback()
        flash('Nenhuma rotina foi criada. Preencha pelo menos um item para o viveiro selecionado.', 'warning')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

@app.post('/operation-schedule/batch-week')
@login_required
@requires_permission('management_manage')
def batch_week_operation_schedule():
    start_date = parse_date(request.form.get('start_date'), local_today())
    end_date = parse_date(request.form.get('end_date'), start_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    selected_weekdays = {parse_int(value) for value in request.form.getlist('weekdays')}
    selected_weekdays = {value for value in selected_weekdays if value is not None}
    if not selected_weekdays:
        flash('Selecione pelo menos um dia da semana.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=start_date.isoformat()))
    units = selected_operation_units_from_form(default_all=True)
    if not units:
        flash('Nenhum viveiro/berçário ativo foi selecionado para programar a semana.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=start_date.isoformat()))

    categories = request.form.getlist('row_category')
    start_times = request.form.getlist('row_start_time') or request.form.getlist('row_scheduled_time')
    priorities = request.form.getlist('row_priority')
    titles = request.form.getlist('row_title')
    feed_ids = request.form.getlist('row_feed_product_id')
    supply_ids = request.form.getlist('row_supply_product_id')
    ration_labels = request.form.getlist('row_ration_label')
    total_quantities = request.form.getlist('row_total_quantity') or request.form.getlist('row_quantity')
    measure_units = request.form.getlist('row_measure_unit')
    frequency_counts = request.form.getlist('row_frequency_count')
    interval_minutes_list = request.form.getlist('row_interval_minutes')
    notes_list = request.form.getlist('row_notes')

    total_rows = max(
        len(categories), len(start_times), len(priorities), len(titles), len(feed_ids), len(supply_ids),
        len(ration_labels), len(total_quantities), len(measure_units), len(frequency_counts),
        len(interval_minutes_list), len(notes_list), 0
    )
    if total_rows == 0:
        flash('Adicione pelo menos uma ação na programação semanal.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=start_date.isoformat()))

    created = 0
    auto_created = 0
    skipped = 0

    def row_value(values, index, default=''):
        return values[index] if index < len(values) else default

    def safe_frequency_count(raw_value):
        count = parse_int(raw_value)
        if not count or count < 1:
            return 1
        return min(count, 12)

    def safe_interval_minutes(raw_value, frequency_count):
        minutes = parse_int(raw_value)
        if minutes is None:
            minutes = 180 if frequency_count > 1 else 0
        return max(0, min(minutes, 720))

    current = start_date
    while current <= end_date:
        if current.weekday() in selected_weekdays:
            for idx in range(total_rows):
                category = row_value(categories, idx, 'rotina') or 'rotina'
                priority = normalize_operation_priority(row_value(priorities, idx, 'media'))
                start_time = parse_time(row_value(start_times, idx, ''))
                title = (row_value(titles, idx, '') or '').strip()
                ration_label = (row_value(ration_labels, idx, '') or '').strip()
                total_quantity = parse_float(row_value(total_quantities, idx, ''))
                measure_unit = (row_value(measure_units, idx, 'kg') or ('g' if category == 'bercario' else 'kg')).strip()
                frequency_count = safe_frequency_count(row_value(frequency_counts, idx, '1'))
                interval_minutes = safe_interval_minutes(row_value(interval_minutes_list, idx, ''), frequency_count)
                user_notes = (row_value(notes_list, idx, '') or '').strip()
                feed_product_id = parse_int(row_value(feed_ids, idx, ''))
                supply_product_id = parse_int(row_value(supply_ids, idx, ''))
                feed_product = db.session.get(FeedProduct, feed_product_id) if feed_product_id else None
                supply_product = db.session.get(SupplyProduct, supply_product_id) if supply_product_id else None

                if not any([title, ration_label, total_quantity, feed_product, supply_product, user_notes]):
                    skipped += 1
                    continue
                if not start_time:
                    skipped += 1
                    continue
                if category != 'rotina' and (total_quantity is None or total_quantity <= 0):
                    skipped += 1
                    continue

                portion_quantity = None
                if total_quantity is not None:
                    portion_quantity = round(total_quantity / frequency_count, 4) if frequency_count > 1 else total_quantity

                frequency_label = f'{frequency_count}x ao dia'
                if frequency_count > 1 and interval_minutes:
                    hours = interval_minutes // 60
                    minutes = interval_minutes % 60
                    interval_label = f'{hours}h{minutes:02d}' if minutes else f'{hours}h'
                    frequency_label = f'{frequency_label} · intervalo {interval_label}'
                system_note = ''
                if frequency_count > 1:
                    system_note = f'Gerado automaticamente pela programação semanal: total {format_decimal_pt(total_quantity)} {measure_unit}, {frequency_count} ocorrência(s), intervalo de {interval_minutes} min.'
                notes = user_notes
                if system_note:
                    notes = f'{user_notes}\n{system_note}' if user_notes else system_note

                for unit in units:
                    for occurrence in range(frequency_count):
                        scheduled_time = time_plus_minutes(start_time, occurrence * interval_minutes) if occurrence else start_time
                        task = create_operational_task_for_unit(
                            current, unit, category, priority, scheduled_time, title,
                            feed_product, supply_product, ration_label, portion_quantity, measure_unit, frequency_label, notes,
                        )
                        db.session.add(task)
                        db.session.flush()
                        created += 1
                        if category == 'alimentacao' and ensure_auto_tray_check_after_feeding(task):
                            auto_created += 1
        current += timedelta(days=1)

    if created:
        db.session.commit()
        msg = f'Programação semanal criada: {created} item(ns) gerados.'
        if auto_created:
            msg += f' {auto_created} verificação(ões) de bandeja foram geradas automaticamente.'
        flash(msg, 'success')
        if skipped:
            flash(f'{skipped} linha(s)/ocorrência(s) vazia(s), sem horário ou incompletas foram ignoradas.', 'info')
    else:
        db.session.rollback()
        flash('Nenhuma rotina semanal foi criada. Preencha pelo menos uma linha completa.', 'warning')
    return redirect(url_for('operation_schedule_page', operation_date=start_date.isoformat()))


@app.post('/operation-schedule/import-nursery')
@login_required
@requires_permission('management_manage')
def import_nursery_to_operation_schedule():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    created, skipped = import_nursery_feed_plan_to_operation_schedule(selected_date)
    db.session.commit()
    if created:
        flash(f'Protocolo do berçário importado para a Rotina do Dia: {created} item(ns) criado(s), incluindo alimentação e manejo da água.', 'success')
    else:
        flash('Nenhum item novo do protocolo de berçário foi importado. Verifique se há berçários/lotes ativos ou se os itens já existem.', 'warning')
    if skipped:
        flash(f'{skipped} item(ns) foram ignorados por já existirem ou não terem dados suficientes.', 'info')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


def complete_operation_tasks_and_flash(tasks, success_prefix='Rotina concluída'):
    created = skipped = errors = 0
    error_messages = []
    for task in tasks:
        status, message = complete_operation_task_into_management(task)
        if status == 'created':
            created += 1
        elif status == 'error':
            errors += 1
            error_messages.append(f'{task_display_unit(task)} · {task.title}: {message}')
        else:
            skipped += 1
    if errors:
        db.session.rollback()
        flash('Não lancei a rotina no manejo porque existem pendências de estoque/vínculo.', 'danger')
        for msg in error_messages[:6]:
            flash(msg, 'danger')
    else:
        db.session.commit()
        flash(f'{success_prefix}: {created} item(ns) lançado(s) no Manejo Diário.', 'success')
        if skipped:
            flash(f'{skipped} item(ns) foram ignorados por já estarem lançados, não terem unidade ou não consumirem estoque.', 'info')
    return created, skipped, errors


@app.post('/operation-schedule/complete-day')
@login_required
@requires_permission('management_manage')
def complete_operation_schedule_day():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    tasks = (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.operation_date == selected_date, OperationalTask.active.is_(True))
        .order_by(OperationalTask.priority_order.asc(), OperationalTask.scheduled_time.asc().nullslast(), OperationalTask.id.asc())
        .all()
    )
    complete_operation_tasks_and_flash(tasks, 'Dia concluído')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


@app.post('/operation-schedule/complete-selected')
@login_required
@requires_permission('management_manage')
def complete_selected_operation_tasks():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    ids = [parse_int(value) for value in request.form.getlist('task_ids')]
    ids = [value for value in ids if value]
    if not ids:
        flash('Selecione pelo menos uma rotina para concluir.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))
    tasks = (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.id.in_(ids), OperationalTask.operation_date == selected_date, OperationalTask.active.is_(True))
        .order_by(OperationalTask.priority_order.asc(), OperationalTask.scheduled_time.asc().nullslast(), OperationalTask.id.asc())
        .all()
    )
    complete_operation_tasks_and_flash(tasks, 'Itens selecionados concluídos')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


@app.post('/operation-schedule/<int:task_id>/complete')
@login_required
@requires_permission('management_manage')
def complete_single_operation_task(task_id):
    task = (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.id == task_id)
        .first()
    )
    if not task:
        flash('Item da rotina não encontrado.', 'warning')
        return redirect(url_for('operation_schedule_page'))
    operation_date = task.operation_date
    complete_operation_tasks_and_flash([task], 'Item concluído')
    return redirect(url_for('operation_schedule_page', operation_date=operation_date.isoformat()))

@app.post('/operation-schedule/<int:task_id>/edit')
@login_required
@requires_permission('management_manage')
def edit_operation_task(task_id):
    task = db.session.get(OperationalTask, task_id)
    if not task:
        flash('Item da rotina não encontrado.', 'warning')
        return redirect(url_for('operation_schedule_page'))
    priority = normalize_operation_priority(request.form.get('priority'))
    task.operation_date = parse_date(request.form.get('operation_date'), task.operation_date)
    task.scheduled_time = parse_time(request.form.get('scheduled_time'))
    task.category = request.form.get('category') or task.category
    task.priority = priority
    task.priority_order = OPERATION_PRIORITY_ORDER[priority]
    task.title = (request.form.get('title') or task.title).strip()
    task.unit_id = request.form.get('unit_id', type=int)
    task.feed_product_id = request.form.get('feed_product_id', type=int)
    task.supply_product_id = request.form.get('supply_product_id', type=int)
    clear_product_links_by_category(task)
    task.ration_label = (request.form.get('ration_label') or '').strip() or None
    task.quantity = parse_float(request.form.get('quantity'))
    task.measure_unit = (request.form.get('measure_unit') or 'kg').strip()
    task.frequency = (request.form.get('frequency') or '').strip()
    task.notes = (request.form.get('notes') or '').strip()
    task.active = bool(request.form.get('active'))
    task.updated_at = datetime.utcnow()
    if task.category == 'alimentacao':
        ensure_auto_tray_check_after_feeding(task)
    else:
        delete_auto_tray_check_for_feeding_task(task)
    db.session.commit()
    flash('Item da rotina atualizado.', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=task.operation_date.isoformat()))


@app.post('/operation-schedule/<int:task_id>/delete')
@login_required
@requires_permission('management_manage')
def delete_operation_task(task_id):
    task = db.session.get(OperationalTask, task_id)
    if not task:
        flash('Item da rotina não encontrado.', 'warning')
        return redirect(url_for('operation_schedule_page'))
    operation_date = task.operation_date
    delete_auto_tray_check_for_feeding_task(task)
    db.session.delete(task)
    db.session.commit()
    flash('Item removido da rotina operacional.', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=operation_date.isoformat()))


def operation_task_ids_from_form():
    ids = [parse_int(value) for value in request.form.getlist('task_ids')]
    return [value for value in ids if value]


def operation_tasks_from_form_ids():
    ids = operation_task_ids_from_form()
    if not ids:
        return []
    return (
        OperationalTask.query
        .options(joinedload(OperationalTask.unit), joinedload(OperationalTask.feed_product), joinedload(OperationalTask.supply_product))
        .filter(OperationalTask.id.in_(ids))
        .order_by(OperationalTask.operation_date.asc(), OperationalTask.scheduled_time.asc().nullslast(), OperationalTask.id.asc())
        .all()
    )


@app.post('/operation-schedule/card/edit')
@login_required
@requires_permission('management_manage')
def edit_operation_card():
    selected_date = parse_date(request.form.get('current_operation_date'), local_today())
    tasks = operation_tasks_from_form_ids()
    if not tasks:
        flash('Card da rotina não encontrado para edição.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    new_date = parse_date(request.form.get('operation_date'), selected_date)
    new_unit_id = request.form.get('unit_id', type=int)
    new_unit = db.session.get(Unit, new_unit_id) if new_unit_id else None
    if not new_unit or not new_unit.active:
        flash('Selecione uma unidade ativa para mover/editar o card.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    for task in tasks:
        task.operation_date = new_date
        task.unit_id = new_unit.id
        task.updated_at = datetime.utcnow()
    db.session.flush()
    for task in tasks:
        if task.category == 'alimentacao':
            ensure_auto_tray_check_after_feeding(task)
    db.session.commit()
    flash(f'Card atualizado: {len(tasks)} item(ns) movidos para {new_unit.name}.', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=new_date.isoformat()))


@app.post('/operation-schedule/card/delete')
@login_required
@requires_permission('management_manage')
def delete_operation_card():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    tasks = operation_tasks_from_form_ids()
    if not tasks:
        unit_id = request.form.get('unit_id', type=int)
        query = OperationalTask.query.filter(
            OperationalTask.operation_date == selected_date,
            OperationalTask.active.is_(True),
        )
        if unit_id:
            query = query.filter(OperationalTask.unit_id == unit_id)
        else:
            query = query.filter(OperationalTask.unit_id.is_(None))
        tasks = query.all()
    if not tasks:
        flash('Card da rotina não encontrado para exclusão.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    by_id = {task.id: task for task in tasks}
    for task in list(tasks):
        if task.category == 'alimentacao':
            auto_task = auto_tray_check_for_feeding_task(task)
            if auto_task:
                by_id[auto_task.id] = auto_task
    for task in by_id.values():
        db.session.delete(task)
    db.session.commit()
    flash(f'Card excluído da Rotina do Dia: {len(by_id)} item(ns) removido(s).', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


@app.post('/operation-schedule/group/edit')
@login_required
@requires_permission('management_manage')
def edit_operation_group():
    selected_date = parse_date(request.form.get('current_operation_date'), local_today())
    tasks = operation_tasks_from_form_ids()
    if not tasks:
        flash('Grupo da rotina não encontrado para edição.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    category = request.form.get('category') or tasks[0].category
    priority = normalize_operation_priority(request.form.get('priority') or tasks[0].priority)
    unit_id = request.form.get('unit_id', type=int)
    new_unit = db.session.get(Unit, unit_id) if unit_id else None
    feed_product_id = request.form.get('feed_product_id', type=int)
    supply_product_id = request.form.get('supply_product_id', type=int)
    ration_label = (request.form.get('ration_label') or '').strip() or None
    title = (request.form.get('title') or '').strip()
    measure_unit = (request.form.get('measure_unit') or '').strip() or None
    frequency = (request.form.get('frequency') or '').strip()
    notes = (request.form.get('notes') or '').strip()
    quantity = parse_float(request.form.get('quantity'))

    if unit_id and (not new_unit or not new_unit.active):
        flash('Unidade selecionada para o grupo não está ativa.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))

    for task in tasks:
        old_category = task.category
        task.category = category
        task.priority = priority
        task.priority_order = OPERATION_PRIORITY_ORDER[priority]
        if unit_id:
            task.unit_id = unit_id
        if title:
            task.title = title
        elif category in ('alimentacao', 'bercario'):
            task.title = f'Alimentação {task.unit.name}' if task.unit else 'Alimentação'
        elif category == 'aditivo' and ration_label:
            task.title = f'Aplicar {ration_label}'
        task.feed_product_id = feed_product_id if feed_product_id else None
        task.supply_product_id = supply_product_id if supply_product_id else None
        clear_product_links_by_category(task)
        task.ration_label = ration_label
        task.quantity = quantity
        if measure_unit:
            task.measure_unit = measure_unit
        task.frequency = frequency
        task.notes = notes
        task.active = True
        task.updated_at = datetime.utcnow()
        if old_category == 'alimentacao' and category != 'alimentacao':
            delete_auto_tray_check_for_feeding_task(task)
    db.session.flush()
    for task in tasks:
        if task.category == 'alimentacao':
            ensure_auto_tray_check_after_feeding(task)
    db.session.commit()
    flash(f'Grupo atualizado: {len(tasks)} item(ns) alterado(s).', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


@app.post('/operation-schedule/group/delete')
@login_required
@requires_permission('management_manage')
def delete_operation_group():
    selected_date = parse_date(request.form.get('operation_date'), local_today())
    tasks = operation_tasks_from_form_ids()
    if not tasks:
        flash('Grupo da rotina não encontrado para exclusão.', 'warning')
        return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))
    by_id = {task.id: task for task in tasks}
    for task in list(tasks):
        if task.category == 'alimentacao':
            auto_task = auto_tray_check_for_feeding_task(task)
            if auto_task:
                by_id[auto_task.id] = auto_task
    for task in by_id.values():
        db.session.delete(task)
    db.session.commit()
    flash(f'Grupo removido da rotina: {len(by_id)} item(ns) excluído(s).', 'success')
    return redirect(url_for('operation_schedule_page', operation_date=selected_date.isoformat()))


@app.route('/painel-tv')
@login_required
@requires_permission('dashboard')
def tv_panel_page():
    selected_date = parse_date(request.args.get('operation_date'), local_today())
    tv_data = build_tv_dashboard_data(selected_date)
    return render_template('tv_panel.html', selected_date=selected_date, tv_data=tv_data, now=local_now())


@app.route('/management', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def management_page():
    if request.method == 'POST':
        unit_id = int(request.form['unit_id'])
        manage_date = parse_date(request.form['manage_date'], local_today())
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
        today=local_today(),
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
    manage_date = parse_date(request.args.get('manage_date'), local_today())
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
    for field in (
        'temperature_c', 'dissolved_oxygen', 'ph', 'salinity', 'transparency_cm',
        'ammonia', 'nitrite', 'nitrate', 'alkalinity', 'hardness'
    ):
        if field in request.form:
            setattr(rec, field, parse_float(request.form.get(field)))
    if 'observation' in request.form:
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
        'temperature': {'group': 'water', 'field': 'temperature_c', 'label': 'Temperatura', 'unit': '°C', 'title': 'Temperatura x tempo', 'threshold_key': 'temperature_c'},
        'transparency': {'group': 'water', 'field': 'transparency_cm', 'label': 'Transparência', 'unit': 'cm', 'title': 'Transparência x tempo', 'threshold_key': 'transparency_cm'},
        'ph': {'group': 'water', 'field': 'ph', 'label': 'pH', 'unit': '', 'title': 'pH x tempo', 'threshold_key': 'ph'},
        'salinity': {'group': 'water', 'field': 'salinity', 'label': 'Salinidade', 'unit': '‰', 'title': 'Salinidade x tempo', 'threshold_key': 'salinity'},
        'ammonia': {'group': 'water', 'field': 'ammonia', 'label': 'Amônia/TAN', 'unit': 'mg/L', 'title': 'Amônia/TAN x tempo', 'threshold_key': 'ammonia'},
        'nitrite': {'group': 'water', 'field': 'nitrite', 'label': 'Nitrito', 'unit': 'mg/L', 'title': 'Nitrito x tempo', 'threshold_key': 'nitrite'},
        'nitrate': {'group': 'water', 'field': 'nitrate', 'label': 'Nitrato', 'unit': 'mg/L', 'title': 'Nitrato x tempo', 'threshold_key': 'nitrate'},
        'alkalinity': {'group': 'water', 'field': 'alkalinity', 'label': 'Alcalinidade', 'unit': 'mg/L', 'title': 'Alcalinidade x tempo', 'threshold_key': 'alkalinity'},
        'hardness': {'group': 'water', 'field': 'hardness', 'label': 'Dureza', 'unit': 'mg/L', 'title': 'Dureza x tempo', 'threshold_key': 'hardness'},
        'feed_offered': {'group': 'management', 'field': 'feed_offered_kg', 'label': 'Ração ofertada', 'unit': 'kg', 'title': 'Ração ofertada x tempo', 'threshold_key': None},
        'tray_score': {'group': 'management', 'field': 'tray_score', 'label': 'Score de bandeja', 'unit': '0–4', 'title': 'Score de bandeja x tempo', 'threshold_key': None},
        'mortality': {'group': 'management', 'field': 'mortality_qty', 'label': 'Mortalidade', 'unit': 'un', 'title': 'Mortalidade x tempo', 'threshold_key': None},
        'average_weight': {'group': 'management', 'field': 'average_weight_g', 'label': 'Peso médio', 'unit': 'g', 'title': 'Peso médio x tempo', 'threshold_key': None},
    }


def build_chart_thresholds():
    config = get_water_reference_config()
    return {
        'dissolved_oxygen': {'label': 'Faixa ideal de OD', 'min': config.od_min, 'max': config.od_max},
        'temperature_c': {'label': 'Faixa ideal de temperatura', 'min': config.temperature_min, 'max': config.temperature_max},
        'transparency_cm': {'label': 'Faixa ideal de transparência', 'min': config.transparency_min, 'max': config.transparency_max},
        'ph': {'label': 'Faixa ideal de pH', 'min': config.ph_min, 'max': config.ph_max},
        'salinity': {'label': 'Faixa de salinidade alvo', 'min': config.salinity_min, 'max': config.salinity_max},
        'ammonia': {'label': 'Faixa ideal de amônia/TAN', 'min': config.ammonia_min, 'max': config.ammonia_max},
        'nitrite': {'label': 'Faixa ideal de nitrito', 'min': config.nitrite_min, 'max': config.nitrite_max},
        'nitrate': {'label': 'Faixa ideal de nitrato', 'min': config.nitrate_min, 'max': config.nitrate_max},
        'alkalinity': {'label': 'Faixa ideal de alcalinidade', 'min': config.alkalinity_min, 'max': config.alkalinity_max},
        'hardness': {'label': 'Faixa ideal de dureza', 'min': config.hardness_min, 'max': config.hardness_max},
    }


def build_chart_meta():
    return {
        'water': {
            'od': {'label': 'OD', 'unit': 'mg/L'},
            'temperature': {'label': 'Temperatura', 'unit': '°C'},
            'transparency': {'label': 'Transparência', 'unit': 'cm'},
            'ph': {'label': 'pH', 'unit': ''},
            'salinity': {'label': 'Salinidade', 'unit': '‰'},
            'ammonia': {'label': 'Amônia/TAN', 'unit': 'mg/L'},
            'nitrite': {'label': 'Nitrito', 'unit': 'mg/L'},
            'nitrate': {'label': 'Nitrato', 'unit': 'mg/L'},
            'alkalinity': {'label': 'Alcalinidade', 'unit': 'mg/L'},
            'hardness': {'label': 'Dureza', 'unit': 'mg/L'},
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


def serialize_weight_series_with_real_transfers(mgmt_records, unit_id=None, start_date=None):
    """Série de peso médio incluindo manejo, biometria e biometria real informada na transferência."""
    rows = []
    for r in mgmt_records:
        if r.average_weight_g is None or not r.manage_date:
            continue
        rows.append({
            'sort_key': (r.manage_date, 1, r.id),
            'label': f"{r.unit.name + ' · ' if r.unit else ''}{r.manage_date.strftime('%d/%m/%Y')} · Manejo",
            'value': round(r.average_weight_g, 3),
            'unit': r.unit.name if r.unit else '',
            'date': r.manage_date.strftime('%d/%m/%Y'),
            'source': 'Manejo diário',
        })

    bio_query = BiometricsSample.query.options(joinedload(BiometricsSample.unit), joinedload(BiometricsSample.lot)).filter(BiometricsSample.average_weight_g.isnot(None))
    if start_date:
        bio_query = bio_query.filter(BiometricsSample.sample_date >= start_date)
    if unit_id:
        bio_query = bio_query.filter(BiometricsSample.unit_id == unit_id)
    for r in bio_query.order_by(BiometricsSample.sample_date.asc(), BiometricsSample.id.asc()).all():
        if r.average_weight_g is None or not r.sample_date:
            continue
        rows.append({
            'sort_key': (r.sample_date, 2, r.id),
            'label': f"{r.unit.name + ' · ' if r.unit else ''}{r.sample_date.strftime('%d/%m/%Y')} · Biometria",
            'value': round(r.average_weight_g, 3),
            'unit': r.unit.name if r.unit else '',
            'date': r.sample_date.strftime('%d/%m/%Y'),
            'source': 'Biometria',
        })

    transfer_query = Transfer.query.options(joinedload(Transfer.destination_unit), joinedload(Transfer.source_lot)).filter(Transfer.avg_weight_g.isnot(None))
    if start_date:
        transfer_query = transfer_query.filter(Transfer.transfer_date >= start_date)
    if unit_id:
        transfer_query = transfer_query.filter(Transfer.destination_unit_id == unit_id)
    for r in transfer_query.order_by(Transfer.transfer_date.asc(), Transfer.id.asc()).all():
        if r.avg_weight_g is None or not r.transfer_date:
            continue
        rows.append({
            'sort_key': (r.transfer_date, 3, r.id),
            'label': f"{r.destination_unit.name + ' · ' if r.destination_unit else ''}{r.transfer_date.strftime('%d/%m/%Y')} · Transferência real",
            'value': round(r.avg_weight_g, 3),
            'unit': r.destination_unit.name if r.destination_unit else '',
            'date': r.transfer_date.strftime('%d/%m/%Y'),
            'source': 'Transferência real',
        })

    rows.sort(key=lambda item: item['sort_key'])
    for item in rows:
        item.pop('sort_key', None)
    return rows


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
    start_date = local_today() - timedelta(days=days - 1)

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
    elif selected_parameter_key == 'average_weight':
        points = serialize_weight_series_with_real_transfers(mgmt_records, unit_id=unit_id, start_date=start_date)
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



@app.route('/feeding-protocol', methods=['GET', 'POST'])
@login_required
@requires_permission('protocols_manage')
def feeding_protocol_page():
    ensure_feeding_protocol_seeded()

    if request.method == 'POST':
        action = (request.form.get('action') or 'save').strip()
        if action == 'reset_default':
            ensure_feeding_protocol_seeded(force=True)
            flash('Tabela base restaurada para o padrão do PDF.', 'success')
            return redirect(url_for('feeding_protocol_page'))

        feed_labels = []
        try:
            feed_labels = json.loads(request.form.get('feed_labels_json') or '[]') or []
        except Exception:
            feed_labels = []
        if not feed_labels:
            feed_labels = get_protocol_feed_labels()

        recalc_totals = request.form.get('recalc_totals') == 'on'
        now = datetime.utcnow()

        # Mapeamento global: cada ração/coluna da tabela pode apontar para uma ração real do estoque.
        for idx, label in enumerate(feed_labels):
            label = (label or '').strip()
            if not label:
                continue
            mapping = FeedingProtocolFeedMap.query.filter_by(protocol_label=label).first()
            if not mapping:
                mapping = FeedingProtocolFeedMap(protocol_label=label)
                db.session.add(mapping)
            product_id = parse_int(request.form.get(f'map_{idx}'))
            mapping.feed_product_id = product_id if product_id else None
            mapping.updated_at = now

        rows = FeedingProtocolRow.query.options(joinedload(FeedingProtocolRow.feeds)).order_by(FeedingProtocolRow.id.asc()).all()
        for row in rows:
            prefix = f'row_{row.id}_'
            phase = normalize_phase_value(request.form.get(prefix + 'phase')) or row.phase
            phase_day = parse_int(request.form.get(prefix + 'phase_day'), row.phase_day) or row.phase_day or 1
            cycle_day = parse_int(request.form.get(prefix + 'cycle_day'), row.cycle_day) or row.cycle_day or phase_day
            population = parse_int(request.form.get(prefix + 'population'), row.population) or 0
            survival_pct = parse_float(request.form.get(prefix + 'survival_pct'), row.survival_pct) or 0
            individual_weight_g = parse_float(request.form.get(prefix + 'individual_weight_g'), row.individual_weight_g) or 0
            feed_rate_pct = parse_float(request.form.get(prefix + 'feed_rate_pct'), row.feed_rate_pct) or 0
            feedings_per_day = parse_int(request.form.get(prefix + 'feedings_per_day'), row.feedings_per_day) or feedings_per_day_for_phase(phase)
            biomass_kg = round((population * individual_weight_g) / 1000.0, 3) if population and individual_weight_g else 0
            row_dirty = request.form.get(prefix + 'dirty') == '1'
            if recalc_totals and row_dirty:
                total_day_g = int(round(biomass_kg * (feed_rate_pct / 100.0) * 1000))
            else:
                total_day_g = parse_int(request.form.get(prefix + 'total_day_g'), row.total_day_g) or 0

            row.phase = phase
            row.phase_day = phase_day
            row.cycle_day = cycle_day
            row.stage_label = (request.form.get(prefix + 'stage_label') or '').strip() or row.stage_label
            row.population = population
            row.survival_pct = survival_pct
            row.individual_weight_g = individual_weight_g
            row.biomass_kg = biomass_kg
            row.feed_rate_pct = feed_rate_pct
            row.total_day_g = total_day_g
            row.feedings_per_day = feedings_per_day
            row.active = request.form.get(prefix + 'active') == 'on'
            row.updated_at = now

            feeds_by_label = {feed.protocol_label: feed for feed in row.feeds}
            for idx, label in enumerate(feed_labels):
                label = (label or '').strip()
                if not label:
                    continue
                grams = parse_int(request.form.get(f'mix_{row.id}_{idx}'), 0) or 0
                feed = feeds_by_label.get(label)
                if grams > 0:
                    if not feed:
                        feed = FeedingProtocolFeed(row_id=row.id, protocol_label=label, sort_order=idx)
                        db.session.add(feed)
                    feed.grams = grams
                    feed.sort_order = idx
                    feed.updated_at = now
                elif feed:
                    db.session.delete(feed)

        db.session.commit()
        flash('Tabela base de alimentação salva. Berçário, juvenil e engorda passam a usar estes valores.', 'success')
        return redirect(url_for('feeding_protocol_page'))

    rows = (
        FeedingProtocolRow.query.options(joinedload(FeedingProtocolRow.feeds))
        .order_by(
            case((FeedingProtocolRow.phase == 'bercario', 1), (FeedingProtocolRow.phase == 'juvenil', 2), (FeedingProtocolRow.phase == 'engorda', 3), else_=4),
            FeedingProtocolRow.phase_day.asc(),
            FeedingProtocolRow.id.asc(),
        )
        .all()
    )
    row_dicts = [feeding_protocol_row_to_dict(row) | {'id': row.id, 'active': row.active} for row in rows]
    for row in row_dicts:
        row['mix_by_label'] = {item.get('label'): item.get('grams') for item in row.get('mixes', [])}
    feed_labels = get_protocol_feed_labels(row_dicts)
    # Garante que labels mapeadas continuem aparecendo como coluna mesmo que todas as células estejam zeradas.
    for mapping in FeedingProtocolFeedMap.query.order_by(FeedingProtocolFeedMap.protocol_label.asc()).all():
        if mapping.protocol_label not in feed_labels:
            feed_labels.append(mapping.protocol_label)
    feed_maps = {item.protocol_label: item.feed_product_id for item in FeedingProtocolFeedMap.query.all()}
    feed_products = FeedProduct.query.order_by(FeedProduct.active.desc(), FeedProduct.brand.asc(), FeedProduct.feed_type.asc()).all()
    fixed_protocol_columns = [
        'Ativa', 'Fase real', 'Idade PL', 'Dia fase', 'Dia ciclo', 'Estágio', 'População',
        'Sobrev. %', 'Peso tabela (g)', 'Biomassa ref. (kg)', 'Taxa alim. %', 'Total/dia (g)', 'Tratos/dia'
    ]
    column_letters = [spreadsheet_column_label(idx + 1) for idx in range(len(fixed_protocol_columns) + len(feed_labels))]
    return render_template(
        'feeding_protocol.html',
        rows=row_dicts,
        feed_labels=feed_labels,
        feed_labels_json=json.dumps(feed_labels, ensure_ascii=False),
        feed_maps=feed_maps,
        feed_products=feed_products,
        phase_options=[('bercario', 'Berçário'), ('juvenil', 'Juvenil'), ('engorda', 'Engorda')],
        column_letters=column_letters,
        fixed_protocol_columns=fixed_protocol_columns,
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



def transfer_real_marker_token(transfer_id):
    return f'[TRANSFER_REAL_ID:{transfer_id}]'


def transfer_estimated_biomass_kg(transfer):
    if not transfer or not transfer.transferred_qty or not transfer.avg_weight_g:
        return None
    return round(((transfer.transferred_qty or 0) * (transfer.avg_weight_g or 0)) / 1000.0, 2)


def sync_transfer_real_data_marker(transfer):
    """Registra a transferência como novo marco real de população/peso/biomassa do lote.

    A transferência pode superar a estimativa do berçário porque a entrada inicial vem do
    peso informado pelo laboratório. O histórico Transfer continua sendo a fonte oficial da
    contagem real; esta linha em DailyManagement serve para dashboards, biomassa e gráficos.
    """
    if not transfer or not transfer.id or not transfer.source_lot_id or not transfer.destination_unit_id:
        return None

    token = transfer_real_marker_token(transfer.id)
    row = DailyManagement.query.filter(DailyManagement.notes.contains(token)).order_by(DailyManagement.id.desc()).first()
    if not row:
        row = DailyManagement(
            feed_offered_kg=0,
            feed_consumed_kg=0,
            mortality_qty=0,
        )
        db.session.add(row)

    biomass_kg = transfer_estimated_biomass_kg(transfer)
    row.manage_date = transfer.transfer_date or local_today()
    row.unit_id = transfer.destination_unit_id
    row.lot_id = transfer.source_lot_id
    row.feed_product_id = None
    row.feed_offered_kg = row.feed_offered_kg or 0
    row.feed_consumed_kg = row.feed_consumed_kg or 0
    row.mortality_qty = row.mortality_qty or 0
    row.average_weight_g = transfer.avg_weight_g
    row.estimated_biomass_kg = biomass_kg

    qty_label = format_integer_pt(transfer.transferred_qty or 0)
    if transfer.avg_weight_g:
        weight_label = format_decimal_pt(transfer.avg_weight_g)
        biomass_label = format_decimal_pt(biomass_kg) if biomass_kg is not None else '—'
        metric_line = f'Contagem real: {qty_label} PL; peso médio {weight_label} g; biomassa {biomass_label} kg.'
    else:
        metric_line = f'Contagem real: {qty_label} PL; peso médio não informado.'
    row.notes = '\n'.join([
        '[Transferência real]',
        metric_line,
        'Este registro recalibra população/biomassa do lote após transferência entre fases.',
        token,
        '[/Transferência real]',
    ])
    row.updated_at = datetime.utcnow()

    lot = db.session.get(Lot, transfer.source_lot_id)
    if lot and transfer.avg_weight_g is not None:
        lot.estimated_weight_g = transfer.avg_weight_g
    return row

@app.route('/transfers', methods=['GET', 'POST'])
@login_required
@requires_permission('transfers_manage')
def transfers_page():
    edit_id = parse_int(request.args.get('edit_id'))
    edit_transfer = db.session.get(Transfer, edit_id) if edit_id else None

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        if form_mode == 'rebuild_allocations':
            rebuild_warnings = rebuild_all_lot_allocations_from_transfer_history()
            for lot in Lot.query.all():
                sync_lot_phase_from_allocations(lot, local_today())
            db.session.commit()
            if rebuild_warnings:
                flash('Saldos recalculados, mas revise estes pontos: ' + ' | '.join(rebuild_warnings[:3]), 'warning')
            else:
                flash('Saldos vivos recalculados com base nas transferências reais.', 'success')
            return redirect(url_for('transfers_page'))

        transfer_date = parse_date(request.form.get('transfer_date'), local_today())
        destination_unit_id = parse_int(request.form.get('destination_unit_id'))
        destination_unit = db.session.get(Unit, destination_unit_id) if destination_unit_id else None
        transferred_qty = parse_int(request.form.get('transferred_qty')) or 0
        avg_weight_g = parse_float(request.form.get('avg_weight_g'))
        requested_source_phase = (request.form.get('source_phase') or '').strip()
        requested_destination_phase = (request.form.get('destination_phase') or '').strip()
        close_source_after_transfer = request.form.get('close_source_allocation') == '1'

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
        source_phase = normalize_phase_value(requested_source_phase) or allocation_operational_phase(source_allocation) or normalize_phase_value(src_lot.phase if src_lot else None)
        destination_phase = normalize_phase_value(requested_destination_phase) or normalize_phase_value(destination_unit.phase if destination_unit else None)
        if source_phase not in valid_phases or destination_phase not in valid_phases:
            flash('Informe corretamente a fase de origem e a fase de destino.', 'danger')
            return redirect(url_for('transfers_page'))

        available_qty = source_allocation.quantity_allocated if source_allocation else None
        if available_qty is not None and transferred_qty > available_qty and form_mode != 'edit':
            flash(
                f'Quantidade informada ({transferred_qty:,} un.) maior que o saldo estimado da origem ({available_qty:,} un.). '
                'A transferência será aceita como contagem real e passará a recalibrar o lote.',
                'warning'
            )

        if form_mode == 'edit':
            tr = db.session.get(Transfer, parse_int(request.form.get('transfer_id')))
            if not tr:
                flash('Transferência não encontrada.', 'warning')
                return redirect(url_for('transfers_page'))

            old_lot_id = tr.source_lot_id
            tr.transfer_date = transfer_date
            tr.source_unit_id = src_id
            tr.destination_unit_id = destination_unit_id
            tr.source_lot_id = src_lot.id
            tr.destination_lot_code = src_lot.lot_code
            tr.source_phase = source_phase
            tr.destination_phase = destination_phase
            tr.transferred_qty = transferred_qty
            tr.close_source_after_transfer = close_source_after_transfer
            tr.avg_weight_g = avg_weight_g
            tr.notes = request.form.get('notes')

            db.session.flush()
            sync_transfer_real_data_marker(tr)
            affected_lot_ids = {lot_id for lot_id in (old_lot_id, src_lot.id) if lot_id}
            rebuild_warnings = []
            for lot_id in affected_lot_ids:
                lot_to_sync = db.session.get(Lot, lot_id)
                rebuild_warnings.extend(rebuild_lot_allocations_from_transfer_history(lot_to_sync))
                sync_lot_phase_from_allocations(lot_to_sync, transfer_date)

            db.session.commit()
            if rebuild_warnings:
                flash('Transferência atualizada e saldos recalculados, mas revise o histórico: ' + ' | '.join(rebuild_warnings[:3]), 'warning')
            else:
                flash('Transferência atualizada e saldos vivos recalculados automaticamente.', 'success')
            return redirect(url_for('transfers_page'))

        existing_allocation = find_active_allocation(src_lot.id, destination_unit_id, transfer_date)
        if not existing_allocation:
            db.session.add(LotUnitAllocation(
                lot_id=src_lot.id,
                unit_id=destination_unit_id,
                start_date=transfer_date,
                quantity_allocated=transferred_qty,
                operational_phase=destination_phase,
                notes='Transferência trifásica entre fases.'
            ))
        else:
            existing_allocation.quantity_allocated = (existing_allocation.quantity_allocated or 0) + transferred_qty
            existing_allocation.operational_phase = destination_phase or existing_allocation.operational_phase
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
            close_source_after_transfer=close_source_after_transfer,
            avg_weight_g=avg_weight_g,
            notes=request.form.get('notes')
        )
        db.session.add(tr)

        remaining_qty = None
        if source_allocation.quantity_allocated is not None:
            remaining_qty = max((source_allocation.quantity_allocated or 0) - transferred_qty, 0)
            source_allocation.quantity_allocated = remaining_qty
        should_close_source = close_source_after_transfer or remaining_qty == 0
        tr.close_source_after_transfer = should_close_source
        if should_close_source:
            source_allocation.end_date = transfer_date

        db.session.flush()
        sync_transfer_real_data_marker(tr)
        rebuild_warnings = rebuild_lot_allocations_from_transfer_history(src_lot)
        sync_lot_phase_from_allocations(src_lot, transfer_date)
        db.session.commit()
        if rebuild_warnings:
            flash('Transferência registrada e saldos recalculados, mas revise o histórico: ' + ' | '.join(rebuild_warnings[:3]), 'warning')
        else:
            flash('Transferência registrada. Os saldos vivos foram recalculados automaticamente.', 'success')
        return redirect(url_for('transfers_page'))

    units = Unit.query.filter_by(active=True).order_by(Unit.phase, Unit.name).all()
    lots = Lot.query.filter_by(status='ativo').order_by(Lot.start_date.desc()).all()
    rows = Transfer.query.options(joinedload(Transfer.source_unit), joinedload(Transfer.destination_unit), joinedload(Transfer.source_lot)).order_by(Transfer.transfer_date.desc(), Transfer.id.desc()).limit(80).all()
    allocations = active_allocation_rows(local_today())
    return render_template(
        'transfers.html',
        units=units,
        lots=lots,
        rows=rows,
        allocations=allocations,
        today=local_today(),
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
        row.movement_date = parse_date(request.form['movement_date'], local_today())
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
    return render_template('feed.html', rows=rows, today=local_today(), total_stock=snapshot['total_stock_kg'], snapshot_rows=snapshot['rows'], low_stock_count=snapshot['low_stock_count'], active_product_count=snapshot['active_product_count'], feed_products=feed_products, stock_by_product=stock_by_product, movement_origin_label=movement_origin_label, edit_product=edit_product, edit_movement=edit_movement)


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
        row.movement_date = parse_date(request.form.get('movement_date'), local_today())
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
        today=local_today(),
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
    start_date = local_today() - timedelta(days=period_days - 1)

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
        'larva_cost': round(sum(summary.get('larva_cost', 0) for summary in lot_summaries), 2),
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
    today = local_today()
    if report_key == 'stock':
        ws.title = 'Estoque'
        ws.append(['Categoria', 'Item', 'Grupo', 'Saldo', 'Unidade', 'Estoque mínimo', 'Custo médio'])
        for row in build_feed_stock_snapshot()['rows']:
            ws.append(['Ração', row['name'] if 'name' in row else row['feed_name'], row.get('feed_type') or row.get('category'), row.get('stock_kg', row.get('stock_qty')), 'kg', row.get('minimum_stock_kg', row.get('minimum_stock_qty')), row.get('avg_unit_cost')])
        for row in build_supply_stock_snapshot()['rows']:
            ws.append(['Insumo/material', row['name'], row['category'], row['stock_qty'], row['measure_unit'], row['minimum_stock_qty'], row.get('avg_unit_cost')])
    elif report_key == 'production':
        ws.title = 'Producao'
        ws.append(['Lote', 'Status', 'Fornecedora', 'Unidades atuais', 'Custo ração', 'Custo insumos', 'Custo larva', 'Custo fixo', 'Custo total', 'FCR real', 'Sobrevivência %'])
        for summary in [lot_financial_summary(lot) for lot in Lot.query.order_by(Lot.start_date.desc()).all()]:
            ws.append([summary['lot'].lot_code, summary['lot'].status, summary['lot'].larva_supplier, ', '.join(item['unit_name'] for item in summary['allocations']), summary['feed_cost'], summary.get('supply_cost', 0), summary.get('larva_cost', 0), summary['fixed_cost'], summary['total_cost'], summary['fcr_real'], summary['survival_pct']])
    elif report_key == 'financial':
        ws.title = 'Financeiro'
        ws.append(['Data', 'Lote', 'Viveiro', 'Receita', 'Custo ração', 'Custo insumos', 'Custo larva', 'Custo fixo', 'Custo total', 'Resultado'])
        for sale in Sale.query.options(joinedload(Sale.lot), joinedload(Sale.unit)).order_by(Sale.sale_date.desc()).all():
            summary = sale_financial_summary(sale)
            if not summary:
                continue
            ws.append([sale.sale_date.strftime('%d/%m/%Y'), sale.lot.lot_code if sale.lot else '', sale.unit.name if sale.unit else '', summary['revenue'], summary['feed_cost'], summary.get('supply_cost', 0), summary.get('larva_cost', 0), summary['fixed_cost'], summary['total_cost'], summary['profit']])
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




def save_all_stage_feed_entries_for_date(phase, target_date):
    """Cria/atualiza todos os lançamentos de alimentação de uma fase no dia.

    Usa o mesmo plano que aparece nos cards da tela e chama sync_nursery_feed_to_management,
    então as rações do mix viram registros no Manejo Diário e os aditivos de água padrão
    continuam dando baixa exatamente pela lógica já existente.
    """
    phase = normalize_phase_value(phase) or 'bercario'
    plans = build_stage_feed_digest_for_date(target_date, phase=phase)
    created = 0
    updated = 0
    skipped = 0
    entries = []

    for plan in plans:
        unit = plan.get('unit')
        lot = plan.get('lot')
        if not unit or not lot or not getattr(unit, 'id', None) or not getattr(lot, 'id', None):
            skipped += 1
            continue

        entry = NurseryFeeding.query.filter_by(
            feed_date=target_date,
            unit_id=unit.id,
            lot_id=lot.id,
        ).order_by(NurseryFeeding.id.desc()).first()

        if entry:
            updated += 1
        else:
            entry = NurseryFeeding(
                feed_date=target_date,
                unit_id=unit.id,
                lot_id=lot.id,
                quantity_kg=0,
            )
            db.session.add(entry)
            created += 1

        entry.feed_date = target_date
        entry.unit_id = unit.id
        entry.lot_id = lot.id
        entry.quantity_kg = plan.get('total_day_kg') or grams_to_kg(plan.get('total_day_g') or 0)
        if entry.score_adjustment_pct is not None:
            previous_adjustment = nursery_cumulative_adjustments(entry.lot_id, entry.feed_date)
            entry.active_feed_factor = nursery_next_active_feed_factor(
                previous_adjustment.get('factor', 1.0),
                entry.score_adjustment_pct,
            )
        else:
            plan_factor = plan.get('score_factor')
            entry.active_feed_factor = plan_factor if plan_factor is not None else 1.0
        entry.water_items_json = json.dumps(selected_nursery_water_items_for_plan(plan), ensure_ascii=False)
        if not (entry.notes or '').strip():
            entry.notes = 'Salvo automaticamente pelo botão Salvar todas as rações do dia.'
        entry.updated_at = datetime.utcnow()

        db.session.flush()
        sync_nursery_feed_to_management(entry)
        entries.append(entry)

    return {
        'plans_count': len(plans),
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'entries': entries,
    }


def handle_save_all_stage_feed(phase, endpoint_name, success_label):
    feed_date = parse_date(request.form.get('feed_date'), local_today())
    result = save_all_stage_feed_entries_for_date(phase, feed_date)
    if result['plans_count'] <= 0:
        flash(f'Nenhum {success_label} ativo encontrado para salvar nesta data.', 'warning')
    else:
        details = []
        if result['created']:
            details.append(f"{result['created']} criado(s)")
        if result['updated']:
            details.append(f"{result['updated']} atualizado(s)")
        if result['skipped']:
            details.append(f"{result['skipped']} ignorado(s)")
        details_label = ', '.join(details) if details else 'sem alterações'
        extra_message = (
            'Na engorda, probiótico, LOTHAR e melaço não são lançados na água.'
            if normalize_phase_value(phase) == 'engorda'
            else 'A lógica de mix, tratos, probiótico, LOTHAR e demais aditivos foi mantida.'
        )
        flash(
            f'Todas as rações do dia do {success_label} foram salvas no Manejo Diário: {details_label}. '
            f'{extra_message}',
            'success',
        )
    db.session.commit()
    return redirect(url_for(endpoint_name, feed_date=feed_date.isoformat()))



@app.route('/feed-preparation')
@login_required
@requires_permission('management_manage')
def feed_preparation_page():
    start_date = parse_date(request.args.get('start_date'), local_today())
    days = parse_int(request.args.get('days')) or 7
    plan_data = build_feed_preparation_plan(start_date=start_date, days=days)
    return render_template(
        'feed_preparation.html',
        selected_start_date=start_date,
        selected_days=plan_data['days'],
        plan=plan_data,
        phase_choices=FEED_PREPARATION_PHASES,
    )


@app.post('/nursery-feed/save-all')
@login_required
@requires_permission('management_manage')
def save_all_nursery_feed_entries():
    return handle_save_all_stage_feed('bercario', 'nursery_feed_page', 'berçário')


@app.post('/juvenile-feed/save-all')
@login_required
@requires_permission('management_manage')
def save_all_juvenile_feed_entries():
    return handle_save_all_stage_feed('juvenil', 'juvenile_feed_page', 'juvenil')


@app.post('/growout-feed/save-all')
@login_required
@requires_permission('management_manage')
def save_all_growout_feed_entries():
    return handle_save_all_stage_feed('engorda', 'growout_feed_page', 'engorda')


@app.route('/nursery-feed', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def nursery_feed_page():
    selected_date = parse_date(request.args.get('feed_date'), local_today())
    edit_id = parse_int(request.args.get('edit_id'))
    edit_entry = db.session.get(NurseryFeeding, edit_id) if edit_id else None
    nursery_units = active_units_for_operational_phase('bercario', on_date=selected_date)

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        entry = db.session.get(NurseryFeeding, parse_int(request.form.get('entry_id'))) if form_mode == 'edit' else NurseryFeeding()
        if form_mode == 'edit' and not entry:
            flash('Registro de alimentação de berçário não encontrado.', 'warning')
            return redirect(url_for('nursery_feed_page'))

        entry.feed_date = parse_date(request.form['feed_date'])
        entry.unit_id = int(request.form['unit_id'])
        unit = db.session.get(Unit, entry.unit_id)
        active_lot = active_lot_for_unit(entry.unit_id, on_date=entry.feed_date)
        entry.lot_id = parse_int(request.form.get('lot_id')) or (active_lot.id if active_lot else None)
        lot = db.session.get(Lot, entry.lot_id) if entry.lot_id else active_lot
        submitted_quantity_kg = parse_float(request.form.get('quantity_kg'), 0) or 0
        adjustment = apply_nursery_adjustment_state_from_request(entry)
        entry.quantity_kg = submitted_quantity_kg
        entry.notes = request.form.get('notes')

        plan_for_submission = build_nursery_protocol_for_date(
            lot,
            unit,
            target_date=entry.feed_date,
            cumulative_factor=adjustment['factor'],
            correction_events=adjustment['events'],
        ) if unit and lot else None
        selected_water_items = selected_nursery_water_items_from_request(plan_for_submission) if plan_for_submission else []
        entry.water_items_json = json.dumps(selected_water_items, ensure_ascii=False)

        entry.updated_at = datetime.utcnow()
        if form_mode != 'edit':
            db.session.add(entry)
        db.session.flush()
        sync_nursery_feed_to_management(entry)
        db.session.commit()
        flash('Alimentação de berçário salva e integrada ao manejo diário. Os aditivos marcados também deram baixa no estoque.', 'success')
        return redirect(url_for('nursery_feed_page', feed_date=entry.feed_date.isoformat()))

    recent_entries = (
        NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot))
        .order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc())
        .limit(200)
        .all()
    )
    entries = [entry for entry in recent_entries if feeding_entry_operational_phase(entry) == 'bercario'][:60]
    plans = build_nursery_digest_for_date(selected_date)
    plan_unit_lot_keys = {(plan['unit'].id, plan['lot'].id) for plan in plans}
    entry_by_unit_id = {
        entry.unit_id: entry
        for entry in NurseryFeeding.query.filter(NurseryFeeding.feed_date == selected_date).all()
        if (entry.unit_id, entry.lot_id) in plan_unit_lot_keys
    }
    for plan in plans:
        plan['existing_entry'] = entry_by_unit_id.get(plan['unit'].id)
        plan['water_items_for_form'] = nursery_water_items_for_form(plan, plan['existing_entry'])
    combined_message = '\n\n'.join(plan['message_text'] for plan in plans)
    return render_template(
        'nursery_feed.html',
        today=local_today(),
        selected_date=selected_date,
        nursery_units=nursery_units,
        entries=entries,
        edit_entry=edit_entry,
        plans=plans,
        combined_message=combined_message,
        phase_label='berçário',
        phase_label_plural='berçários',
        phase_label_title='Berçário',
        feed_endpoint='nursery_feed_page',
        delete_endpoint='delete_nursery_feed_entry',
        bulk_save_endpoint='save_all_nursery_feed_entries',
    )



@app.route('/juvenile-feed', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def juvenile_feed_page():
    selected_date = parse_date(request.args.get('feed_date'), local_today())
    edit_id = parse_int(request.args.get('edit_id'))
    edit_entry = db.session.get(NurseryFeeding, edit_id) if edit_id else None
    juvenile_units = active_units_for_operational_phase('juvenil', on_date=selected_date)

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        entry = db.session.get(NurseryFeeding, parse_int(request.form.get('entry_id'))) if form_mode == 'edit' else NurseryFeeding()
        if form_mode == 'edit' and not entry:
            flash('Registro de alimentação de juvenil não encontrado.', 'warning')
            return redirect(url_for('juvenile_feed_page'))

        entry.feed_date = parse_date(request.form['feed_date'])
        entry.unit_id = int(request.form['unit_id'])
        unit = db.session.get(Unit, entry.unit_id)
        if not unit or not unit_is_active_in_operational_phase(unit.id, 'juvenil', on_date=entry.feed_date):
            flash('Selecione uma unidade com lote ativo na fase Juvenil.', 'danger')
            return redirect(url_for('juvenile_feed_page', feed_date=entry.feed_date.isoformat()))
        active_lot = active_lot_for_unit(entry.unit_id, on_date=entry.feed_date)
        entry.lot_id = parse_int(request.form.get('lot_id')) or (active_lot.id if active_lot else None)
        lot = db.session.get(Lot, entry.lot_id) if entry.lot_id else active_lot
        submitted_quantity_kg = parse_float(request.form.get('quantity_kg'), 0) or 0
        adjustment = apply_nursery_adjustment_state_from_request(entry)
        entry.quantity_kg = submitted_quantity_kg
        entry.notes = request.form.get('notes')

        plan_for_submission = build_nursery_protocol_for_date(
            lot,
            unit,
            target_date=entry.feed_date,
            cumulative_factor=adjustment['factor'],
            correction_events=adjustment['events'],
        ) if unit and lot else None
        selected_water_items = selected_nursery_water_items_from_request(plan_for_submission) if plan_for_submission else []
        entry.water_items_json = json.dumps(selected_water_items, ensure_ascii=False)

        entry.updated_at = datetime.utcnow()
        if form_mode != 'edit':
            db.session.add(entry)
        db.session.flush()
        sync_nursery_feed_to_management(entry)
        db.session.commit()
        flash('Alimentação de juvenil salva e integrada ao manejo diário. Os aditivos marcados também deram baixa no estoque.', 'success')
        return redirect(url_for('juvenile_feed_page', feed_date=entry.feed_date.isoformat()))

    recent_entries = (
        NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot))
        .order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc())
        .limit(200)
        .all()
    )
    entries = [entry for entry in recent_entries if feeding_entry_operational_phase(entry) == 'juvenil'][:60]
    plans = build_juvenile_digest_for_date(selected_date)
    plan_unit_lot_keys = {(plan['unit'].id, plan['lot'].id) for plan in plans}
    entry_by_unit_id = {
        entry.unit_id: entry
        for entry in NurseryFeeding.query.filter(NurseryFeeding.feed_date == selected_date).all()
        if (entry.unit_id, entry.lot_id) in plan_unit_lot_keys
    }
    for plan in plans:
        plan['existing_entry'] = entry_by_unit_id.get(plan['unit'].id)
        plan['water_items_for_form'] = nursery_water_items_for_form(plan, plan['existing_entry'])
    combined_message = '\n\n'.join(plan['message_text'] for plan in plans)
    return render_template(
        'nursery_feed.html',
        today=local_today(),
        selected_date=selected_date,
        nursery_units=juvenile_units,
        entries=entries,
        edit_entry=edit_entry,
        plans=plans,
        combined_message=combined_message,
        phase_label='juvenil',
        phase_label_plural='juvenis',
        phase_label_title='Juvenil',
        feed_endpoint='juvenile_feed_page',
        delete_endpoint='delete_juvenile_feed_entry',
        bulk_save_endpoint='save_all_juvenile_feed_entries',
    )


@app.route('/growout-feed', methods=['GET', 'POST'])
@login_required
@requires_permission('management_manage')
def growout_feed_page():
    selected_date = parse_date(request.args.get('feed_date'), local_today())
    edit_id = parse_int(request.args.get('edit_id'))
    edit_entry = db.session.get(NurseryFeeding, edit_id) if edit_id else None
    growout_units = active_units_for_operational_phase('engorda', on_date=selected_date)

    if request.method == 'POST':
        form_mode = request.form.get('form_mode', 'create')
        entry = db.session.get(NurseryFeeding, parse_int(request.form.get('entry_id'))) if form_mode == 'edit' else NurseryFeeding()
        if form_mode == 'edit' and not entry:
            flash('Registro de alimentação de engorda não encontrado.', 'warning')
            return redirect(url_for('growout_feed_page'))

        entry.feed_date = parse_date(request.form['feed_date'])
        entry.unit_id = int(request.form['unit_id'])
        unit = db.session.get(Unit, entry.unit_id)
        if not unit or not unit_is_active_in_operational_phase(unit.id, 'engorda', on_date=entry.feed_date):
            flash('Selecione uma unidade com lote ativo na fase Engorda.', 'danger')
            return redirect(url_for('growout_feed_page', feed_date=entry.feed_date.isoformat()))
        active_lot = active_lot_for_unit(entry.unit_id, on_date=entry.feed_date)
        entry.lot_id = parse_int(request.form.get('lot_id')) or (active_lot.id if active_lot else None)
        lot = db.session.get(Lot, entry.lot_id) if entry.lot_id else active_lot
        submitted_quantity_kg = parse_float(request.form.get('quantity_kg'), 0) or 0
        adjustment = apply_nursery_adjustment_state_from_request(entry)
        entry.quantity_kg = submitted_quantity_kg
        entry.notes = request.form.get('notes')

        plan_for_submission = build_nursery_protocol_for_date(
            lot,
            unit,
            target_date=entry.feed_date,
            cumulative_factor=adjustment['factor'],
            correction_events=adjustment['events'],
        ) if unit and lot else None
        selected_water_items = selected_nursery_water_items_from_request(plan_for_submission) if plan_for_submission else []
        entry.water_items_json = json.dumps(selected_water_items, ensure_ascii=False)

        entry.updated_at = datetime.utcnow()
        if form_mode != 'edit':
            db.session.add(entry)
        db.session.flush()
        sync_nursery_feed_to_management(entry)
        db.session.commit()
        flash('Alimentação de engorda salva e integrada ao manejo diário. Na engorda, probiótico, LOTHAR e melaço não são lançados na água.', 'success')
        return redirect(url_for('growout_feed_page', feed_date=entry.feed_date.isoformat()))

    recent_entries = (
        NurseryFeeding.query.options(joinedload(NurseryFeeding.unit), joinedload(NurseryFeeding.lot))
        .order_by(NurseryFeeding.feed_date.desc(), NurseryFeeding.id.desc())
        .limit(200)
        .all()
    )
    entries = [entry for entry in recent_entries if feeding_entry_operational_phase(entry) == 'engorda'][:60]
    plans = build_growout_digest_for_date(selected_date)
    plan_unit_lot_keys = {(plan['unit'].id, plan['lot'].id) for plan in plans}
    entry_by_unit_id = {
        entry.unit_id: entry
        for entry in NurseryFeeding.query.filter(NurseryFeeding.feed_date == selected_date).all()
        if (entry.unit_id, entry.lot_id) in plan_unit_lot_keys
    }
    for plan in plans:
        plan['existing_entry'] = entry_by_unit_id.get(plan['unit'].id)
        plan['water_items_for_form'] = nursery_water_items_for_form(plan, plan['existing_entry'])
    combined_message = '\n\n'.join(plan['message_text'] for plan in plans)
    return render_template(
        'nursery_feed.html',
        today=local_today(),
        selected_date=selected_date,
        nursery_units=growout_units,
        entries=entries,
        edit_entry=edit_entry,
        plans=plans,
        combined_message=combined_message,
        phase_label='engorda',
        phase_label_plural='engordas',
        phase_label_title='Engorda',
        feed_endpoint='growout_feed_page',
        delete_endpoint='delete_growout_feed_entry',
        bulk_save_endpoint='save_all_growout_feed_entries',
    )


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


@app.post('/juvenile-feed/<int:entry_id>/delete')
@login_required
@requires_permission('management_manage')
def delete_juvenile_feed_entry(entry_id):
    entry = db.session.get(NurseryFeeding, entry_id)
    if not entry:
        flash('Registro de alimentação de juvenil não encontrado.', 'warning')
        return redirect(request.referrer or url_for('juvenile_feed_page'))
    feed_date = entry.feed_date
    delete_nursery_management_records(entry)
    db.session.delete(entry)
    db.session.commit()
    flash('Lançamento do juvenil excluído e removido do manejo diário.', 'success')
    return redirect(url_for('juvenile_feed_page', feed_date=feed_date.isoformat()))


@app.post('/growout-feed/<int:entry_id>/delete')
@login_required
@requires_permission('management_manage')
def delete_growout_feed_entry(entry_id):
    entry = db.session.get(NurseryFeeding, entry_id)
    if not entry:
        flash('Registro de alimentação de engorda não encontrado.', 'warning')
        return redirect(request.referrer or url_for('growout_feed_page'))
    feed_date = entry.feed_date
    delete_nursery_management_records(entry)
    db.session.delete(entry)
    db.session.commit()
    flash('Lançamento da engorda excluído e removido do manejo diário.', 'success')
    return redirect(url_for('growout_feed_page', feed_date=feed_date.isoformat()))


@app.get('/api/nursery-feed-digest')
def nursery_feed_digest_api():
    token = os.getenv('NURSERY_DIGEST_TOKEN', '').strip()
    provided = (request.headers.get('X-Nursery-Token') or request.args.get('token') or '').strip()
    if token and provided != token:
        return jsonify({'ok': False, 'message': 'Token inválido.'}), 403

    target_date = parse_date(request.args.get('feed_date'), local_today())
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
        sale_date = parse_date(request.form['sale_date'], local_today())
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
    return render_template('sales.html', units=units, lots=lots, rows=rows, row_summaries=row_summaries, today=local_today(), total_revenue=total_revenue, edit_sale=edit_sale, selected_summary=selected_summary)


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
        'Peso medio g', 'Unidades despescadas', 'Custo racao viveiro', 'Custo insumos viveiro', 'Custo larva viveiro', 'Custo fixo viveiro',
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
            summary.get('larva_cost', 0),
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


def recompute_weekly_gains_for_lot(lot_id):
    """Recalcula o ganho semanal das biometrias em ordem cronológica.

    Isso evita que uma correção de data deixe o histórico com ganho semanal baseado
    na data antiga ou em uma biometria cadastrada fora de ordem.
    """
    if not lot_id:
        return
    previous = None
    samples = (
        BiometricsSample.query
        .filter(BiometricsSample.lot_id == lot_id)
        .order_by(BiometricsSample.sample_date.asc(), BiometricsSample.id.asc())
        .all()
    )
    for sample in samples:
        if previous and sample.sample_date and previous.sample_date and sample.average_weight_g is not None and previous.average_weight_g is not None:
            days = max((sample.sample_date - previous.sample_date).days, 1)
            sample.weekly_gain_g = round(((sample.average_weight_g - previous.average_weight_g) / days) * 7, 3)
        else:
            sample.weekly_gain_g = None
        previous = sample


def clear_old_biometry_management_sync(lot_id, unit_id, sample_date, old_weight_g=None, old_biomass_kg=None):
    """Remove o reflexo de manejo da data antiga quando a biometria é editada.

    Não apaga o manejo inteiro para não perder ração/mortalidade lançadas no mesmo dia.
    Apenas limpa peso/biomassa sincronizados pela biometria antiga.
    """
    if not lot_id or not unit_id or not sample_date:
        return
    sync_note = f'Biometria sincronizada em {sample_date.strftime("%d/%m/%Y")}'
    row = (
        DailyManagement.query
        .filter(
            DailyManagement.lot_id == lot_id,
            DailyManagement.unit_id == unit_id,
            DailyManagement.manage_date == sample_date,
        )
        .order_by(DailyManagement.id.desc())
        .first()
    )
    if not row or not (row.notes and sync_note in row.notes):
        return

    # Evita limpar manualmente outro peso caso o operador já tenha corrigido o manejo depois.
    if old_weight_g is not None and row.average_weight_g is not None and abs((row.average_weight_g or 0) - old_weight_g) > 0.0005:
        return
    if old_biomass_kg is not None and row.estimated_biomass_kg is not None and abs((row.estimated_biomass_kg or 0) - old_biomass_kg) > 0.01:
        return

    row.average_weight_g = None
    row.estimated_biomass_kg = None
    notes = [part.strip() for part in (row.notes or '').split('|') if part.strip() and part.strip() != sync_note]
    row.notes = ' | '.join(notes) or None
    row.updated_at = datetime.utcnow()


def merged_weight_observations(lot_id):
    observations_by_date = {}

    def put_observation(payload):
        existing = observations_by_date.get(payload['date'])
        if not existing or (payload['source_priority'], payload['id']) >= (existing['source_priority'], existing['id']):
            observations_by_date[payload['date']] = payload

    for row in DailyManagement.query.filter(DailyManagement.lot_id == lot_id, DailyManagement.average_weight_g.isnot(None)).order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all():
        if not row.manage_date or row.average_weight_g is None:
            continue
        put_observation({
            'date': row.manage_date,
            'weight_g': round(row.average_weight_g or 0, 3),
            'source': 'manejo',
            'source_label': 'Manejo diário',
            'source_priority': 1,
            'id': row.id,
        })

    for row in BiometricsSample.query.filter(BiometricsSample.lot_id == lot_id, BiometricsSample.average_weight_g.isnot(None)).order_by(BiometricsSample.sample_date.asc(), BiometricsSample.id.asc()).all():
        if not row.sample_date or row.average_weight_g is None:
            continue
        put_observation({
            'date': row.sample_date,
            'weight_g': round(row.average_weight_g or 0, 3),
            'source': 'biometria',
            'source_label': 'Biometria',
            'source_priority': 2,
            'id': row.id,
        })

    for row in Transfer.query.filter(Transfer.source_lot_id == lot_id, Transfer.avg_weight_g.isnot(None)).order_by(Transfer.transfer_date.asc(), Transfer.id.asc()).all():
        if not row.transfer_date or row.avg_weight_g is None:
            continue
        unit_label = row.destination_unit.name if row.destination_unit else 'destino'
        put_observation({
            'date': row.transfer_date,
            'weight_g': round(row.avg_weight_g or 0, 3),
            'source': 'transferencia',
            'source_label': f'Transferência real para {unit_label}',
            'source_priority': 3,
            'id': row.id,
        })

    return [item for _day, item in sorted(observations_by_date.items(), key=lambda pair: pair[0])]


def lot_density_snapshot(lot: Lot, on_date=None):
    on_date = on_date or local_today()
    allocation = (active_allocations_for_lot(lot, on_date=on_date) or [None])[-1]
    qty = allocation.quantity_allocated if allocation and allocation.quantity_allocated else allocation_live_count_for_lot(lot, on_date=on_date)
    unit = allocation.unit if allocation and allocation.unit else lot.unit
    if not unit or not unit.area_m2 or not qty:
        return None
    return round(qty / unit.area_m2, 2)


def lot_environment_snapshot(lot: Lot, ref_date=None, days=5):
    ref_date = ref_date or local_today()
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
    protocol_key = nursery_protocol_key_for_unit(getattr(lot, 'unit', None))
    protocol_meta = get_nursery_protocol_meta(protocol_key)
    rows = protocol_meta.get('rows', [])
    if not rows:
        return None
    # Usa o dia do ciclo, não a fase física, porque o protocolo novo é contínuo.
    reference_date = (lot.start_date or local_today()) + timedelta(days=int(max(age_days or 0, 0)))
    cycle_day = nursery_cycle_day_for_lot(lot, reference_date, protocol_key=protocol_key)
    row = get_nursery_protocol_row_by_cycle_day(cycle_day, protocol_key=protocol_key)
    if not row:
        return None
    return {
        'age_days': int(max(age_days or 0, 0)),
        'expected_weight_g': round(row['individual_weight_g'], 4),
        'daily_gain_g': row.get('daily_growth_g') or 0.001,
        'survival_pct': row.get('survival_pct'),
        'feed_rate_pct': row.get('feed_rate_pct'),
        'estimated_fcr': row.get('estimated_fcr'),
        'source': f"{protocol_meta.get('name', protocol_key.upper())} — ciclo completo",
    }


def standard_growout_curve_point(age_days: int | float):
    rows = [row for row in PRODUCTION_PROTOCOL_ROWS if row.get('phase') == 'engorda']
    return _protocol_curve_point(rows, age_days)


def standard_growout_curve_by_weight(weight_g: float | int | None):
    weight = max(parse_float(weight_g, 0) or 0, 0)
    rows = PRODUCTION_PROTOCOL_ROWS if 0 < weight < 1.5 else [row for row in PRODUCTION_PROTOCOL_ROWS if row.get('phase') == 'engorda']
    return _protocol_curve_by_weight(rows, weight)


def standard_expected_weight_at_age(lot: Lot, age_days: int):
    """Peso esperado inicial pela tabela base de alimentação e idade de PL."""
    table_base = feeding_table_expected_weight_for_lot(lot, age_days=age_days)
    if table_base:
        return table_base

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
    on_date = on_date or local_today()
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
        rows = get_nursery_protocol_rows(nursery_protocol_key_for_unit(getattr(lot, 'unit', None)))
        return rows[-1].get('survival_pct') if rows else None
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
    """Contagem viva usada em projeções/sugestão.

    Depois de uma transferência, a quantidade transferida vira o novo marco real do lote.
    A curva de sobrevivência só limita lotes que ainda não têm contagem real de transferência.
    """
    on_date = on_date or local_today()
    mortality_adjusted = allocation_live_count_for_lot(lot, on_date=on_date)
    base_count = sum((allocation.quantity_allocated or 0) for allocation in active_allocations_for_lot(lot, on_date=on_date)) or (parse_int(getattr(lot, 'initial_count', 0), 0) or 0)
    harvested = lot_total_harvested_units(lot.id)
    if latest_transfer_for_lot(lot, on_date=on_date):
        return mortality_adjusted
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
    age = max((local_today() - lot.start_date).days, 0) if lot and lot.start_date else 0
    if is_nursery_lot(lot):
        curve = nursery_protocol_curve_for_lot(lot, age)
        return round(curve.get('daily_gain_g') or 0.001, 4) if curve else 0.001
    if is_growout_lot(lot):
        curve_now = production_protocol_curve_for_lot(lot, age) or standard_growout_curve_point(age)
        curve_future = production_protocol_curve_for_lot(lot, age + 7) or standard_growout_curve_point(age + 7)
        return round(max((curve_future['expected_weight_g'] - curve_now['expected_weight_g']) / 7, 0.03), 3)
    return 0.08


def smart_growth_projection(lot, days_ahead=7):
    current_age = max((local_today() - lot.start_date).days, 0)
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
    return allocation_live_count_for_lot(lot, on_date=local_today())


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
        age_days = max((local_today() - lot.start_date).days, 0)
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
        age_days = max((local_today() - lot.start_date).days, 0)
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
        age_days = max((local_today() - lot.start_date).days, 0) if lot and lot.start_date else 0
        baseline = standard_expected_weight_at_age(lot, age_days)
        weight = parse_float(baseline.get('expected_weight_g'), 0) or 0
    live_count = modeled_live_count_for_lot(lot)
    records_by_lot = {lot.id: DailyManagement.query.filter(DailyManagement.lot_id == lot.id).order_by(DailyManagement.manage_date.asc(), DailyManagement.id.asc()).all()}
    allocations_by_lot = {lot.id: LotUnitAllocation.query.filter(
        LotUnitAllocation.lot_id == lot.id,
        or_(LotUnitAllocation.end_date.is_(None), LotUnitAllocation.end_date >= local_today()),
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
        curve = feeding_table_expected_weight_for_lot(lot, target_date=obs['date'], age_days=days) or adaptive_expected_weight_at_age(lot, days)
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
    current_age = max((local_today() - lot.start_date).days, 0)
    curve_today = feeding_table_expected_weight_for_lot(lot, target_date=local_today(), age_days=current_age) or adaptive_expected_weight_at_age(lot, current_age)
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
    current_cost_summary = lot_financial_summary(lot)
    current_lot_cost = current_cost_summary['total_cost']
    fixed_cost_total = current_cost_summary['fixed_cost']
    cycle_days = max((local_today() - lot.start_date).days, 1)
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
            'projected_date': local_today() + timedelta(days=days_wait),
            'weight_g': round(weight_g or 0, 2),
            'price_kg': price_kg,
            'biomass_kg': biomass_kg,
            'revenue': revenue,
            'extra_feed_cost': extra_feed_cost,
            'extra_fixed_cost': extra_fixed_cost,
            'net_value': round(revenue - current_lot_cost - extra_feed_cost - extra_fixed_cost, 2),
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
        'current_lot_cost': current_lot_cost,
        'current_cost_summary': current_cost_summary,
        'scenarios': scenarios,
        'best': best,
        'decision': decision,
        'current_recommendation': current_rec,
    }


def projected_cashflow_rows(days=90, base_price_10g=22.0):
    rows = []
    horizon = local_today() + timedelta(days=days)
    for lot in Lot.query.filter_by(status='ativo').order_by(Lot.start_date.asc()).all():
        current_weight = current_weight_for_lot(lot)
        projection = smart_growth_projection(lot, 7)
        growth = projection.get('daily_gain_g') or 0
        if not current_weight or growth <= 0:
            continue
        if current_weight >= TARGET_HARVEST_WEIGHT_G:
            harvest_date = local_today()
            projected_weight = current_weight
        else:
            days_to_target = max(int(round((TARGET_HARVEST_WEIGHT_G - current_weight) / growth)), 1)
            harvest_date = local_today() + timedelta(days=days_to_target)
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
    start = local_today()
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
    pdf.drawString(40, y, f'Gerado em {local_now().strftime("%d/%m/%Y %H:%M")}')
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
        edit_id = parse_int(request.form.get('biometrics_id'))
        sample_to_edit = db.session.get(BiometricsSample, edit_id) if edit_id else None
        old_state = None
        if edit_id and not sample_to_edit:
            flash('Biometria não encontrada para edição.', 'warning')
            return redirect(url_for('biometrics_page'))
        if sample_to_edit:
            old_state = {
                'lot_id': sample_to_edit.lot_id,
                'unit_id': sample_to_edit.unit_id,
                'sample_date': sample_to_edit.sample_date,
                'average_weight_g': sample_to_edit.average_weight_g,
                'estimated_biomass_kg': sample_to_edit.estimated_biomass_kg,
            }

        submitted_lot_id = parse_int(request.form.get('lot_id'))
        unit_id = parse_int(request.form.get('unit_id'))
        sample_date = parse_date(request.form.get('sample_date'), default=local_today())
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
            redirect_args = {'edit_id': edit_id} if edit_id else {}
            return redirect(url_for('biometrics_page', **redirect_args))

        if not estimated_biomass_kg:
            live_count = current_live_count_for_lot(lot)
            estimated_biomass_kg = round((live_count * average_weight_g) / 1000, 2) if live_count and average_weight_g else None

        if sample_to_edit:
            row = sample_to_edit
            row.sample_date = sample_date
            row.lot_id = lot_id
            row.unit_id = unit_id
            row.sample_size = sample_size
            row.average_weight_g = average_weight_g
            row.cv_pct = cv_pct
            row.estimated_biomass_kg = estimated_biomass_kg
            row.notes = notes or None
            flash_message = 'Biometria atualizada e manejo diário resincronizado.', 'success'
        else:
            row = BiometricsSample(
                sample_date=sample_date,
                lot_id=lot_id,
                unit_id=unit_id,
                sample_size=sample_size,
                average_weight_g=average_weight_g,
                cv_pct=cv_pct,
                estimated_biomass_kg=estimated_biomass_kg,
                notes=notes or None,
            )
            db.session.add(row)
            flash_message = 'Biometria registrada e sincronizada com o manejo diário.', 'success'

        sync_biometrics_to_management(lot, unit_id, sample_date, average_weight_g, estimated_biomass_kg, notes)

        if old_state and (
            old_state['lot_id'] != lot_id or old_state['unit_id'] != unit_id or old_state['sample_date'] != sample_date
        ):
            clear_old_biometry_management_sync(
                old_state['lot_id'],
                old_state['unit_id'],
                old_state['sample_date'],
                old_state['average_weight_g'],
                old_state['estimated_biomass_kg'],
            )

        affected_lot_ids = {lot_id}
        if old_state and old_state.get('lot_id'):
            affected_lot_ids.add(old_state['lot_id'])
        for affected_lot_id in affected_lot_ids:
            recompute_weekly_gains_for_lot(affected_lot_id)

        db.session.commit()
        flash(*flash_message)
        return redirect(url_for('biometrics_page', history_unit_id=unit_id) if unit_id else url_for('biometrics_page'))

    history_unit_id = parse_int(request.args.get('history_unit_id'))
    edit_id = parse_int(request.args.get('edit_id'))
    edit_sample = None
    if edit_id:
        edit_sample = BiometricsSample.query.options(joinedload(BiometricsSample.lot), joinedload(BiometricsSample.unit)).filter(BiometricsSample.id == edit_id).first()
        if not edit_sample:
            flash('Biometria não encontrada para edição.', 'warning')
            return redirect(url_for('biometrics_page', history_unit_id=history_unit_id) if history_unit_id else url_for('biometrics_page'))
        history_unit_id = history_unit_id or edit_sample.unit_id

    rows_query = BiometricsSample.query.options(joinedload(BiometricsSample.lot), joinedload(BiometricsSample.unit))
    if history_unit_id:
        rows_query = rows_query.filter(BiometricsSample.unit_id == history_unit_id)
    rows = rows_query.order_by(BiometricsSample.sample_date.desc(), BiometricsSample.id.desc()).limit(60).all()

    enriched_rows = []
    for row in rows:
        if not row.lot:
            continue
        age_days = max((row.sample_date - row.lot.start_date).days, 0)
        days_at_farm = inclusive_day_count(row.lot.start_date, row.sample_date)
        expected = feeding_table_expected_weight_for_lot(row.lot, target_date=row.sample_date, age_days=age_days) or adaptive_expected_weight_at_age(row.lot, age_days)
        linked_management = DailyManagement.query.filter(DailyManagement.lot_id == row.lot_id, DailyManagement.manage_date == row.sample_date).first()
        enriched_rows.append({
            'row': row,
            'days_at_farm': days_at_farm,
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
        today=local_today(),
        history_unit_id=history_unit_id,
        edit_sample=edit_sample,
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
            entry_date=parse_date(request.form.get('entry_date'), default=local_today()),
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
    reading_time = parse_time(payload.get('monitor_time')) if payload.get('monitor_time') else local_now().time().replace(second=0, microsecond=0)
    reading_date = parse_date(payload.get('monitor_date')) if payload.get('monitor_date') else local_today()
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
    today = local_today()
    if report_key == 'stock':
        headers = ['Categoria', 'Item', 'Saldo', 'Unidade', 'Minimo']
        rows = []
        for row in build_feed_stock_snapshot()['rows']:
            rows.append(['Ração', row.get('name') or row.get('feed_name'), row.get('stock_kg'), 'kg', row.get('minimum_stock_kg')])
        for row in build_supply_stock_snapshot()['rows']:
            rows.append(['Insumo', row['name'], row.get('stock_qty'), row.get('measure_unit'), row.get('minimum_stock_qty')])
        return build_pdf_response('Relatório de estoque', headers, rows, f'relatorio_estoque_{today.strftime("%Y%m%d")}.pdf')
    elif report_key == 'production':
        headers = ['Lote', 'Fornecedor', 'Custo larva', 'Custo total', 'FCR', 'Sobrevivencia']
        rows = []
        for summary in [lot_financial_summary(lot) for lot in Lot.query.order_by(Lot.start_date.desc()).all()]:
            rows.append([summary['lot'].lot_code, summary['lot'].larva_supplier or '-', summary.get('larva_cost', 0), summary['total_cost'], summary['fcr_real'], summary['survival_pct']])
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
