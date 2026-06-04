#!/usr/bin/env python3
# CayteringWork Bot — Railway standalone (polling mode)

import json, math, os, asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, Update,
)
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cayteringwork")
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, and_, func, select, update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from aiogram.fsm.storage.memory import MemoryStorage
try:
    from aiogram.fsm.storage.redis import RedisStorage
    _redis_available = True
except ImportError:
    _redis_available = False

# ── FSM STORAGE (MemoryStorage — fine for polling on Railway) ─────────────────

def md(t: str) -> str:
    """Escape Markdown special chars to prevent TelegramBadRequest parse errors."""
    for ch in ("_", "*", "`", "["):
        t = str(t).replace(ch, f"\\{ch}")
    return t

# ── ENV & CONSTANTS ───────────────────────────────────────────────────────────

def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

GEO_RADIUS: int = int(_get("GEO_RADIUS_METERS", "300"))

# Support both SUPER_ADMIN_IDS (comma-separated) and legacy SUPER_ADMIN_ID
_sa_raw = _get("SUPER_ADMIN_IDS", "") or _get("SUPER_ADMIN_ID", "742587575")
SUPER_ADMIN_IDS: set[int] = {int(x.strip()) for x in _sa_raw.split(",") if x.strip()}

def make_storage():
    """Use Redis if REDIS_URL is set, fallback to MemoryStorage."""
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url and _redis_available:
        return RedisStorage.from_url(redis_url)
    return MemoryStorage()

# ── COMPANY RULES TEXT ────────────────────────────────────────────────────────
COMPANY_RULES = (
    "📋 <b>ПРАВИЛА СОТРУДНИКОВ CAYTERINGWORK_BOT</b>\n\n"
    "1. Приходить на смену строго в назначенное время.\n"
    "2. Иметь опрятный внешний вид и чистую форму.\n"
    "3. Вежливо и профессионально общаться с гостями.\n"
    "4. Соблюдать стандарты обслуживания заведения.\n"
    "5. Бережно обращаться с инвентарём и оборудованием.\n"
    "6. Не использовать телефон в зале во время смены.\n"
    "7. Немедленно сообщать о проблемах менеджеру.\n"
    "8. Соблюдать санитарно-гигиенические нормы.\n"
    "9. Не покидать рабочее место без разрешения менеджера.\n"
    "10. Сохранять коммерческую тайну заведения."
)

class Base(DeclarativeBase):
    pass

class Tenant(Base):
    __tablename__ = "tenants"
    id             = Column(Integer, primary_key=True)
    name           = Column(String(200), nullable=False)
    activation_code= Column(String(50), unique=True, nullable=False)
    admin_ids_json = Column(Text, default="[]")
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def admin_ids(self):
        return json.loads(self.admin_ids_json or "[]")

    employees    = relationship("Employee", back_populates="tenant")
    subscription = relationship("Subscription", back_populates="tenant", uselist=False)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id         = Column(Integer, primary_key=True)
    tenant_id  = Column(Integer, ForeignKey("tenants.id"), unique=True, nullable=False)
    plan       = Column(String(20), default="monthly")
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active  = Column(Boolean, default=True)
    tenant     = relationship("Tenant", back_populates="subscription")

class Employee(Base):
    __tablename__ = "employees"
    id           = Column(Integer, primary_key=True)
    tenant_id    = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    telegram_id  = Column(BigInteger, unique=True, nullable=False)
    first_name   = Column(String(100), nullable=False)
    last_name    = Column(String(100), nullable=False)
    role         = Column(String(20), nullable=False)
    status       = Column(String(20), default="pending")
    hourly_rate  = Column(Float, default=0.0)
    rules_accepted = Column(Boolean, default=False)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    tenant       = relationship("Tenant", back_populates="employees")
    shifts       = relationship("Shift", back_populates="employee")

    @property
    def full_name(self): return f"{self.first_name} {self.last_name}"
    @property
    def role_display(self): return "🤵 Официант" if self.role == "waiter" else "👷 Грузчик"

class Shift(Base):
    __tablename__ = "shifts"
    id                  = Column(Integer, primary_key=True)
    employee_id         = Column(Integer, ForeignKey("employees.id"), nullable=False)
    status              = Column(String(10), default="open")
    started_at          = Column(DateTime(timezone=True), server_default=func.now())
    ended_at            = Column(DateTime(timezone=True), nullable=True)
    hours_worked        = Column(Float, nullable=True)
    salary_earned       = Column(Float, nullable=True)
    start_lat           = Column(Float, nullable=True)
    start_lon           = Column(Float, nullable=True)
    start_geo_ok        = Column(Boolean, nullable=True)
    start_location_name = Column(String(255), nullable=True)
    end_lat             = Column(Float, nullable=True)
    end_lon             = Column(Float, nullable=True)
    end_geo_ok          = Column(Boolean, nullable=True)
    end_location_name   = Column(String(255), nullable=True)
    employee            = relationship("Employee", back_populates="shifts")



class Rating(Base):
    """Employee rating after each shift (1-5 stars)."""
    __tablename__ = "ratings"
    id          = Column(Integer, primary_key=True)
    shift_id    = Column(Integer, ForeignKey("shifts.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    tenant_id   = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    admin_id    = Column(BigInteger, nullable=False)
    score       = Column(Integer, nullable=False)
    comment     = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

class ShiftAnnouncement(Base):
    """Mass shift vacancy broadcast."""
    __tablename__ = "shift_announcements"
    id             = Column(Integer, primary_key=True)
    tenant_id      = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    admin_id       = Column(BigInteger, nullable=False)
    text           = Column(Text, nullable=False)        # vacancy description
    required_count = Column(Integer, nullable=False)     # total slots
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    is_active      = Column(Boolean, default=True)
    responses      = relationship("ShiftResponse", back_populates="announcement")

    @property
    def responded_count(self):
        return len(self.responses)


class ShiftResponse(Base):
    """Employee response to a shift announcement."""
    __tablename__ = "shift_responses"
    id              = Column(Integer, primary_key=True)
    announcement_id = Column(Integer, ForeignKey("shift_announcements.id"), nullable=False)
    employee_id     = Column(Integer, ForeignKey("employees.id"), nullable=False)
    telegram_msg_id = Column(Integer, nullable=True)     # message id for editing
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    announcement    = relationship("ShiftAnnouncement", back_populates="responses")
class ShiftInvitation(Base):
    """Shift invitation sent by admin to an employee."""
    __tablename__ = "shift_invitations"
    id          = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    tenant_id   = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    admin_id    = Column(BigInteger, nullable=False)
    shift_date  = Column(String(100), nullable=False)
    status      = Column(String(20), default="pending")  # pending/accepted/declined
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
class Location(Base):
    __tablename__  = "locations"
    id             = Column(Integer, primary_key=True)
    tenant_id      = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    name           = Column(String(255), nullable=False)
    lat            = Column(Float, nullable=False)
    lon            = Column(Float, nullable=False)
    radius_meters  = Column(Integer, default=300)
    is_active      = Column(Boolean, default=True)

class Rate(Base):
    __tablename__ = "rates"
    id           = Column(Integer, primary_key=True)
    tenant_id    = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    role         = Column(String(20), nullable=False)
    hourly_rate  = Column(Float, nullable=False, default=500.0)

# ── LAZY DB ───────────────────────────────────────────────────────────────────

_engine = None
_Session = None
_db_lock = asyncio.Lock()

async def ensure_db():
    global _engine, _Session
    if _Session is not None:
        return
    async with _db_lock:
        if _Session is not None:
            return
        raw = os.environ["DATABASE_URL"]
        # Strip ALL query params — asyncpg does NOT support ?sslmode=require etc.
        db_url = raw.replace("postgresql://", "postgresql+asyncpg://", 1) \
                    .replace("postgres://", "postgresql+asyncpg://", 1)
        db_url = db_url.split("?")[0]  # remove everything after ?
        _engine = create_async_engine(
            db_url,
            connect_args={"ssl": True},   # Neon.tech requires SSL
            pool_pre_ping=True,
        )
        _Session = async_sessionmaker(_engine, expire_on_commit=False)
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialised")

def db():
    return _Session()

# ── QUERIES ───────────────────────────────────────────────────────────────────

async def q_tenant_code(s, code):
    r = await s.execute(select(Tenant).where(Tenant.activation_code == code.upper()))
    return r.scalar_one_or_none()

async def q_tenant_id(s, tid):
    r = await s.execute(select(Tenant).where(Tenant.id == tid))
    return r.scalar_one_or_none()

async def q_sub(s, tid):
    r = await s.execute(select(Subscription).where(Subscription.tenant_id == tid))
    return r.scalar_one_or_none()

async def q_sub_active(s, tid):
    sub = await q_sub(s, tid)
    if not sub or not sub.is_active:
        return False
    if sub.expires_at and sub.expires_at < datetime.now(timezone.utc):
        await s.execute(update(Subscription).where(Subscription.tenant_id == tid).values(is_active=False))
        await s.commit()
        return False
    return True

async def q_emp(s, tg_id):
    r = await s.execute(select(Employee).where(Employee.telegram_id == tg_id))
    return r.scalar_one_or_none()

async def q_emps(s, tenant_id, status=None):
    q = select(Employee).where(Employee.tenant_id == tenant_id)
    if status: q = q.where(Employee.status == status)
    r = await s.execute(q.order_by(Employee.created_at.desc()))
    return r.scalars().all()

async def q_open_shift(s, emp_id):
    r = await s.execute(select(Shift).where(and_(Shift.employee_id == emp_id, Shift.status == "open")))
    return r.scalar_one_or_none()

async def q_shifts(s, emp_id, month=None, year=None):
    q = select(Shift).where(and_(Shift.employee_id == emp_id, Shift.status == "closed"))
    if month and year:
        q = q.where(and_(func.extract("month", Shift.started_at) == month,
                         func.extract("year",  Shift.started_at) == year))
    r = await s.execute(q.order_by(Shift.started_at.desc()))
    return r.scalars().all()

async def q_monthly(s, emp_id, month, year):
    sh = await q_shifts(s, emp_id, month, year)
    return {"shifts": len(sh),
            "hours":  round(sum(x.hours_worked  or 0 for x in sh), 2),
            "salary": round(sum(x.salary_earned or 0 for x in sh), 2)}

async def q_rate(s, tenant_id, role):
    r = await s.execute(select(Rate).where(and_(Rate.tenant_id == tenant_id, Rate.role == role)))
    rt = r.scalar_one_or_none()
    return rt.hourly_rate if rt else 500.0

async def q_locs(s, tenant_id):
    r = await s.execute(select(Location).where(and_(Location.tenant_id == tenant_id, Location.is_active == True)))
    return r.scalars().all()


async def q_create_rating(s, shift_id, employee_id, tenant_id, admin_id, score, comment=None):
    """Save a rating for a completed shift."""
    rating = Rating(shift_id=shift_id, employee_id=employee_id,
                    tenant_id=tenant_id, admin_id=admin_id, score=score, comment=comment)
    s.add(rating); await s.commit()
    return rating


async def q_create_invite(s, employee_id, tenant_id, admin_id, shift_date):
    inv = ShiftInvitation(employee_id=employee_id, tenant_id=tenant_id,
                          admin_id=admin_id, shift_date=shift_date)
    s.add(inv); await s.flush(); await s.commit(); await s.refresh(inv)
    return inv

async def q_get_invite(s, invite_id):
    r = await s.execute(select(ShiftInvitation).where(ShiftInvitation.id == invite_id))
    return r.scalar_one_or_none()

async def q_update_invite_status(s, invite_id, status):
    await s.execute(update(ShiftInvitation).where(ShiftInvitation.id == invite_id).values(status=status))
    await s.commit()

async def q_create_announcement(s, tenant_id, admin_id, text, count):
    ann = ShiftAnnouncement(tenant_id=tenant_id, admin_id=admin_id,
                            text=text, required_count=count)
    s.add(ann); await s.flush(); await s.commit(); await s.refresh(ann)
    return ann

async def q_get_announcement(s, ann_id):
    r = await s.execute(
        select(ShiftAnnouncement).where(ShiftAnnouncement.id == ann_id))
    return r.scalar_one_or_none()

async def q_get_announcement_with_responses(s, ann_id):
    from sqlalchemy.orm import selectinload
    r = await s.execute(
        select(ShiftAnnouncement)
        .options(selectinload(ShiftAnnouncement.responses))
        .where(ShiftAnnouncement.id == ann_id))
    return r.scalar_one_or_none()

async def q_has_responded(s, ann_id, employee_id):
    r = await s.execute(
        select(ShiftResponse).where(
            and_(ShiftResponse.announcement_id == ann_id,
                 ShiftResponse.employee_id == employee_id)))
    return r.scalar_one_or_none() is not None

async def q_add_response(s, ann_id, employee_id):
    resp = ShiftResponse(announcement_id=ann_id, employee_id=employee_id)
    s.add(resp); await s.commit()
    return resp

async def q_announcement_responders(s, ann_id):
    """Get all employees who responded to an announcement."""
    r = await s.execute(
        select(Employee).join(ShiftResponse, ShiftResponse.employee_id == Employee.id)
        .where(ShiftResponse.announcement_id == ann_id)
        .order_by(ShiftResponse.created_at))
    return r.scalars().all()
async def q_avg_rating(s, employee_id):
    """Get average rating and count for an employee."""
    r = await s.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.employee_id == employee_id))
    avg, cnt = r.one()
    return round(float(avg), 1) if avg else None, cnt or 0

async def q_shift_rated(s, shift_id):
    """Check if a shift already has a rating."""
    r = await s.execute(select(Rating).where(Rating.shift_id == shift_id))
    return r.scalar_one_or_none() is not None

# ── GEO ───────────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def check_geo(lat, lon, locations):
    if not locations: return True, 0, "Координаты зафиксированы"
    best, bd, bn = None, float("inf"), ""
    for loc in locations:
        d = haversine(lat, lon, loc.lat, loc.lon)
        if d < bd: bd, best, bn = d, loc, loc.name
    r = best.radius_meters if best else GEO_RADIUS
    return bd <= r, round(bd), bn

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────

def kb_main(has_open=False):
    row = [KeyboardButton(text="⏹ Завершить смену")] if has_open else [KeyboardButton(text="✅ Начать смену")]
    return ReplyKeyboardMarkup(keyboard=[row,
        [KeyboardButton(text="💰 Моя зарплата"), KeyboardButton(text="📋 История смен")],
        [KeyboardButton(text="📊 Статистика")]], resize_keyboard=True)

def kb_loc():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True)

def kb_roles():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤵 Официант", callback_data="role:waiter")],
        [InlineKeyboardButton(text="👷 Грузчик",  callback_data="role:loader")]])

def kb_rules():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю правила", callback_data="accept_rules")]])

def kb_approve(eid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{eid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{eid}")]])

def kb_admin():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👥 Сотрудники"), KeyboardButton(text="⏳ Ожидают")],
        [KeyboardButton(text="💵 Ставки"),     KeyboardButton(text="📊 Отчёт")],
        [KeyboardButton(text="📍 Локации"),    KeyboardButton(text="📤 Google Sheets")],
        [KeyboardButton(text="📢 Рассылка вакансии")]],
        resize_keyboard=True)

def kb_emp_list(emps):
    icons = {"active":"🟢","pending":"🟡","blocked":"🔴"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{icons.get(e.status,'⚪')} {e.full_name} ({e.role_display})",
                              callback_data=f"emp:{e.id}")] for e in emps])

def kb_emp_actions(eid, blocked):
    blk = ("✅ Разблокировать", f"unblock:{eid}") if blocked else ("🚫 Заблокировать", f"block:{eid}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Пригласить на смену", callback_data=f"invite_emp:{eid}")],
        [InlineKeyboardButton(text="💵 Установить ставку", callback_data=f"setrate:{eid}")],
        [InlineKeyboardButton(text="📋 История смен",     callback_data=f"empshifts:{eid}")],
        [InlineKeyboardButton(text=blk[0], callback_data=blk[1])],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_list")]])

def kb_rate_roles():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤵 Ставка официанта", callback_data="rate_role:waiter")],
        [InlineKeyboardButton(text="👷 Ставка грузчика",  callback_data="rate_role:loader")]])



def kb_invite_response(invite_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_invite:{invite_id}"),
        InlineKeyboardButton(text="❌ Отказаться", callback_data=f"decline_invite:{invite_id}"),
    ]])

def kb_broadcast_respond(ann_id: int, remaining: int, total: int,
                          already_responded: bool = False) -> InlineKeyboardMarkup:
    """Button for employee to respond to a broadcast."""
    if already_responded:
        text = f"✅ Вы откликнулись ({total - remaining}/{total} занято)"
        cb   = f"ann_already:{ann_id}"
    elif remaining <= 0:
        text = f"Все места заняты ✅ ({total}/{total})"
        cb   = f"ann_full:{ann_id}"
    else:
        text = f"✅ Откликнуться (свободно {remaining}/{total})"
        cb   = f"ann_respond:{ann_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=cb)
    ]])
def kb_rating(shift_id: int) -> InlineKeyboardMarkup:
    """Star rating buttons for a completed shift."""
    stars = ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{s} {i+1}", callback_data=f"rate_shift:{shift_id}:{i+1}")
        for i, s in enumerate(stars)
    ]])

# ── FSM ───────────────────────────────────────────────────────────────────────

class RegSt(StatesGroup):
    code = State(); name = State(); role = State(); rules = State()

class AdminSt(StatesGroup):
    rate = State(); rate_role = State(); loc_name = State(); loc_geo = State()
    ind_rate = State()   # individual rate for a specific employee


class RatingSt(StatesGroup):
    waiting_comment = State()   # optional comment after star click

class InviteSt(StatesGroup):
    waiting_date = State()

class BroadcastSt(StatesGroup):
    waiting_text  = State()   # admin enters vacancy description
    waiting_count = State()   # admin enters number of slots


# ── FILTERS ───────────────────────────────────────────────────────────────────

class IsSA(Filter):
    async def __call__(self, message: Message):
        return message.from_user.id in SUPER_ADMIN_IDS

class IsTA(Filter):
    async def __call__(self, obj):
        uid = obj.from_user.id
        await ensure_db()
        async with db() as s:
            emp = await q_emp(s, uid)
            if not emp: return False
            t = await q_tenant_id(s, emp.tenant_id)
            return t is not None and uid in t.admin_ids

# ── HELPERS ───────────────────────────────────────────────────────────────────

async def sub_off(msg: Message):
    await msg.answer("⏸ Бот приостановлен. Обратитесь к менеджеру заведения для продления подписки.")

async def get_es(msg: Message):
    await ensure_db()
    async with db() as s:
        emp = await q_emp(s, msg.from_user.id)
        if not emp: return None, None
        return emp, await q_sub_active(s, emp.tenant_id)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

sa_r    = Router()
admin_r = Router()
reg_r   = Router()
shift_r = Router()
sal_r   = Router()

# ════════════════ SUPERADMIN ══════════════════════════════════════════════════

@sa_r.message(IsSA(), F.text.startswith("/sa"))
async def sa_cmd(msg: Message):
    await ensure_db()
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return await msg.answer(
            "🛠 *Superadmin:*\n"
            "`/sa create CODE ADMIN_ID MONTHS`\n"
            "`/sa renew CODE MONTHS`\n"
            "`/sa deactivate CODE`\n"
            "`/sa list`\n"
            "`/sa addadmin CODE TG_ID`\n\n"
            "MONTHS: 1/6/12/0=lifetime", parse_mode="Markdown")
    cmd = parts[1].lower()

    if cmd == "create":
        if len(parts) < 5:
            return await msg.answer("Формат: `/sa create CODE ADMIN_ID MONTHS`", parse_mode="Markdown")
        code = parts[2].upper()
        try: admin_id, months = int(parts[3]), int(parts[4])
        except ValueError: return await msg.answer("❌ ADMIN_ID и MONTHS — числа")
        plan = {1:"monthly",6:"biannual",12:"annual"}.get(months, "lifetime" if months==0 else "monthly")
        expires = datetime.now(timezone.utc) + timedelta(days=30*months) if months > 0 else None
        async with db() as s:
            if await q_tenant_code(s, code):
                return await msg.answer(f"❌ Код `{code}` уже занят", parse_mode="Markdown")
            t = Tenant(name=code, activation_code=code, admin_ids_json=json.dumps([admin_id]))
            s.add(t); await s.flush()
            s.add(Subscription(tenant_id=t.id, plan=plan, expires_at=expires, is_active=True))
            await s.commit()
        exp = "безлимит" if not expires else f"до {expires.strftime('%d.%m.%Y')}"
        await msg.answer(f"✅ Клиент создан!\n\n🔑 Код: `{code}`\n👤 Админ: `{admin_id}`\n📅 {exp}\n\n"
                         "Передайте код сотрудникам — вводят при /start", parse_mode="Markdown")

    elif cmd == "renew":
        if len(parts) < 4:
            return await msg.answer("Формат: `/sa renew CODE MONTHS`", parse_mode="Markdown")
        code = parts[2].upper()
        try: months = int(parts[3])
        except ValueError: return await msg.answer("❌ MONTHS — число")
        async with db() as s:
            t = await q_tenant_code(s, code)
            if not t: return await msg.answer(f"❌ `{code}` не найден", parse_mode="Markdown")
            sub = await q_sub(s, t.id)
            now = datetime.now(timezone.utc)
            base = max(sub.expires_at, now) if sub and sub.expires_at and sub.expires_at > now else now
            new_exp = base + timedelta(days=30*months) if months > 0 else None
            if sub:
                await s.execute(update(Subscription).where(Subscription.tenant_id == t.id)
                                .values(is_active=True, expires_at=new_exp))
            else:
                s.add(Subscription(tenant_id=t.id, is_active=True, expires_at=new_exp))
            await s.commit()
        exp = "безлимит" if not new_exp else f"до {new_exp.strftime('%d.%m.%Y')}"
        await msg.answer(f"✅ Подписка `{code}` продлена ({exp})", parse_mode="Markdown")

    elif cmd == "deactivate":
        if len(parts) < 3: return await msg.answer("Формат: `/sa deactivate CODE`", parse_mode="Markdown")
        code = parts[2].upper()
        async with db() as s:
            t = await q_tenant_code(s, code)
            if not t: return await msg.answer(f"❌ `{code}` не найден", parse_mode="Markdown")
            await s.execute(update(Subscription).where(Subscription.tenant_id == t.id).values(is_active=False))
            await s.commit()
        await msg.answer(f"⏸ Подписка `{code}` отключена", parse_mode="Markdown")

    elif cmd == "list":
        async with db() as s:
            r = await s.execute(select(Tenant)); tenants = r.scalars().all()
        if not tenants: return await msg.answer("Нет клиентов")
        lines = ["📋 *Клиенты:*\n"]
        async with db() as s:
            for t in tenants:
                sub = await q_sub(s, t.id)
                if not sub: status = "❓ нет подписки"
                elif not sub.is_active: status = "⛔ отключён"
                elif not sub.expires_at: status = "♾ безлимит"
                else:
                    d = (sub.expires_at - datetime.now(timezone.utc)).days
                    status = f"✅ {d} дн." if d > 0 else "⚠️ истёк"
                nc = await s.execute(select(func.count()).select_from(Employee).where(Employee.tenant_id == t.id))
                lines.append(f"• `{t.activation_code}` — {status} | {nc.scalar()} сотр.")
        await msg.answer("\n".join(lines), parse_mode="Markdown")

    elif cmd == "addadmin":
        if len(parts) < 4: return await msg.answer("Формат: `/sa addadmin CODE TG_ID`", parse_mode="Markdown")
        code = parts[2].upper()
        try: new_adm = int(parts[3])
        except ValueError: return await msg.answer("❌ TG_ID — число")
        async with db() as s:
            t = await q_tenant_code(s, code)
            if not t: return await msg.answer(f"❌ `{code}` не найден", parse_mode="Markdown")
            ids = t.admin_ids
            if new_adm not in ids: ids.append(new_adm)
            await s.execute(update(Tenant).where(Tenant.id == t.id).values(admin_ids_json=json.dumps(ids)))
            await s.commit()
        await msg.answer(f"✅ `{new_adm}` добавлен как админ `{code}`", parse_mode="Markdown")
    else:
        await msg.answer("❓ `/sa` — список команд", parse_mode="Markdown")

# ════════════════ REGISTRATION ════════════════════════════════════════════════

@reg_r.callback_query(F.data.startswith("ann_respond:"))
async def emp_ann_respond(cb: CallbackQuery):
    """Employee responds to a broadcast announcement."""
    await ensure_db()
    ann_id = int(cb.data.split(":")[1])
    async with db() as s:
        emp = await q_emp(s, cb.from_user.id)
        if not emp:
            return await cb.answer("Сначала зарегистрируйтесь — /start", show_alert=True)
        ann = await q_get_announcement_with_responses(s, ann_id)
        if not ann or not ann.is_active:
            return await cb.answer("Вакансия уже закрыта", show_alert=True)
        if await q_has_responded(s, ann_id, emp.id):
            return await cb.answer("Вы уже откликнулись на эту вакансию!", show_alert=True)
        # Count current responses
        responded = len(ann.responses)
        remaining = ann.required_count - responded
        if remaining <= 0:
            await cb.message.edit_reply_markup(
                reply_markup=kb_broadcast_respond(ann_id, 0, ann.required_count))
            return await cb.answer("К сожалению, все места уже заняты", show_alert=True)
        # Add response
        await q_add_response(s, ann_id, emp.id)
        new_responded = responded + 1
        new_remaining = ann.required_count - new_responded
        # Update button for this employee
        await cb.message.edit_reply_markup(
            reply_markup=kb_broadcast_respond(ann_id, new_remaining, ann.required_count,
                                              already_responded=True))
        await cb.answer(f"✅ Вы откликнулись! Свободно мест: {new_remaining}/{ann.required_count}")
        # Notify admin
        try:
            status_msg = (f"📢 *{emp.full_name}* откликнулся!\n"
                          f"Занято: *{new_responded}/{ann.required_count}*")
            if new_remaining == 0:
                # Get all responders
                responders = await q_announcement_responders(s, ann_id)
                names = "\n".join(f"• {e.full_name} ({e.role_display})" for e in responders)
                status_msg += f"\n\n✅ *Все места заняты!*\nСписок:\n{names}"
                # Deactivate announcement
                await s.execute(
                    update(ShiftAnnouncement).where(ShiftAnnouncement.id == ann_id)
                    .values(is_active=False))
                await s.commit()
            await cb.bot.send_message(ann.admin_id, status_msg, parse_mode="Markdown")
        except Exception: pass


@reg_r.callback_query(F.data.startswith("ann_full:"))
async def emp_ann_full(cb: CallbackQuery):
    await cb.answer("Все места уже заняты", show_alert=True)


@reg_r.callback_query(F.data.startswith("ann_already:"))
async def emp_ann_already(cb: CallbackQuery):
    await cb.answer("Вы уже откликнулись на эту вакансию ✅", show_alert=True)


@reg_r.callback_query(F.data.startswith("accept_invite:"))
async def emp_accept_invite(cb: CallbackQuery):
    """Employee accepts shift invitation."""
    await ensure_db()
    inv_id = int(cb.data.split(":")[1])
    async with db() as s:
        inv = await q_get_invite(s, inv_id)
        if not inv: return await cb.answer("Приглашение не найдено или устарело", show_alert=True)
        if inv.status != "pending": return await cb.answer("Вы уже ответили на это приглашение", show_alert=True)
        await q_update_invite_status(s, inv_id, "accepted")
        r = await s.execute(select(Employee).where(Employee.id == inv.employee_id))
        employee = r.scalar_one_or_none()
        tenant = await q_tenant_id(s, inv.tenant_id)
    await cb.message.edit_text(
        f"✅ *Вы приняли приглашение!*\n\n"
        f"🏢 {tenant.name if tenant else ''}\n"
        f"📅 {inv.shift_date}\n\nЖдём вас на смене!",
        parse_mode="Markdown")
    try:
        await cb.bot.send_message(inv.admin_id,
            f"✅ *{employee.full_name if employee else 'Сотрудник'} принял приглашение!*\n📅 {inv.shift_date}",
            parse_mode="Markdown")
    except Exception: pass


@reg_r.callback_query(F.data.startswith("decline_invite:"))
async def emp_decline_invite(cb: CallbackQuery):
    """Employee declines shift invitation."""
    await ensure_db()
    inv_id = int(cb.data.split(":")[1])
    async with db() as s:
        inv = await q_get_invite(s, inv_id)
        if not inv: return await cb.answer("Приглашение не найдено или устарело", show_alert=True)
        if inv.status != "pending": return await cb.answer("Вы уже ответили на это приглашение", show_alert=True)
        await q_update_invite_status(s, inv_id, "declined")
        r = await s.execute(select(Employee).where(Employee.id == inv.employee_id))
        employee = r.scalar_one_or_none()
    await cb.message.edit_text(
        f"❌ *Вы отказались от приглашения.*\n\n📅 {inv.shift_date}\n\n"
        "Если передумаете — свяжитесь с менеджером.", parse_mode="Markdown")
    try:
        await cb.bot.send_message(inv.admin_id,
            f"❌ *{employee.full_name if employee else 'Сотрудник'} отказался.*\n📅 {inv.shift_date}",
            parse_mode="Markdown")
    except Exception: pass


@reg_r.message(F.text == "/start")
async def cmd_start(msg: Message, state: FSMContext):
    await ensure_db()
    async with db() as s:
        emp = await q_emp(s, msg.from_user.id)
    if emp:
        if emp.status == "pending":
            return await msg.answer("⏳ Ваша заявка на рассмотрении. Ожидайте подтверждения менеджера.")
        if emp.status == "blocked":
            return await msg.answer("🚫 Ваш аккаунт заблокирован. Обратитесь к менеджеру.")
        async with db() as s:
            active = await q_sub_active(s, emp.tenant_id)
            op = await q_open_shift(s, emp.id)
        if not active: return await sub_off(msg)
        return await msg.answer(f"👋 С возвращением, {emp.first_name}!", reply_markup=kb_main(bool(op)))
    await msg.answer("👋 Добро пожаловать в CayteringWork Bot!\n\n"
                     "Для регистрации введите *код вашего ресторана* (выдаёт менеджер):",
                     parse_mode="Markdown")
    await state.set_state(RegSt.code)

@reg_r.message(RegSt.code)
async def reg_code(msg: Message, state: FSMContext):
    await ensure_db()
    code = msg.text.strip().upper()
    async with db() as s:
        t = await q_tenant_code(s, code)
        if not t: return await msg.answer("❌ Код не найден. Попросите менеджера уточнить код ресторана.")
        active = await q_sub_active(s, t.id)
    if not active: return await msg.answer("⏸ Доступ приостановлен. Обратитесь к менеджеру заведения.")
    await state.update_data(tenant_id=t.id)
    await msg.answer("✅ Ресторан найден!\n\nВведите ваше *имя и фамилию*:", parse_mode="Markdown")
    await state.set_state(RegSt.name)

@reg_r.message(RegSt.name)
async def reg_name(msg: Message, state: FSMContext):
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return await msg.answer("⚠️ Введите имя И фамилию через пробел. Пример: *Иван Иванов*", parse_mode="Markdown")
    await state.update_data(first_name=parts[0], last_name=" ".join(parts[1:]))
    await msg.answer("Выберите вашу роль:", reply_markup=kb_roles())
    await state.set_state(RegSt.role)

@reg_r.callback_query(RegSt.role, F.data.startswith("role:"))
async def reg_role(cb: CallbackQuery, state: FSMContext):
    await cb.answer()  # acknowledge button press immediately
    await ensure_db()
    role = cb.data.split(":")[1]
    await state.update_data(role=role)
    d = await state.get_data()
    # Get tenant name for dynamic rules header
    async with db() as s:
        tenant = await q_tenant_id(s, d.get("tenant_id"))
        tname = tenant.name.title() if tenant else "CayteringWork"
    # Replace header in rules with tenant-specific name (HTML safe)
    rules_text = COMPANY_RULES.replace(
        "📋 <b>ПРАВИЛА СОТРУДНИКОВ CAYTERINGWORK_BOT</b>",
        f"📋 <b>ПРАВИЛА СОТРУДНИКОВ {tname.upper()} CATERING</b>"
    )
    await cb.message.edit_text(rules_text, parse_mode="HTML", reply_markup=kb_rules())
    await state.set_state(RegSt.rules)

@reg_r.callback_query(RegSt.rules, F.data == "accept_rules")
async def reg_accept(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()  # acknowledge immediately
    await ensure_db()
    d = await state.get_data()
    async with db() as s:
        rate = await q_rate(s, d["tenant_id"], d["role"])
        emp = Employee(tenant_id=d["tenant_id"], telegram_id=cb.from_user.id,
                       first_name=d["first_name"], last_name=d["last_name"],
                       role=d["role"], status="pending", hourly_rate=rate, rules_accepted=True)
        s.add(emp); await s.flush(); eid = emp.id
        t = await q_tenant_id(s, d["tenant_id"])
        await s.commit()
    await cb.message.edit_text(
        f"✅ Регистрация завершена!\n\n"
        f"👤 {d['first_name']} {d['last_name']}\n"
        f"💼 {'🤵 Официант' if d['role']=='waiter' else '👷 Грузчик'}\n"
        f"💵 Ставка: {rate} ₽/час\n\n"
        f"⏳ Заявка отправлена менеджеру. Ожидайте подтверждения.")
    await state.clear()
    for aid in t.admin_ids:
        try:
            await bot.send_message(aid,
                f"🆕 Новый сотрудник ожидает:\n\n"
                f"👤 {d['first_name']} {d['last_name']}\n"
                f"💼 {'🤵 Официант' if d['role']=='waiter' else '👷 Грузчик'}\n"
                f"🆔 TG: {cb.from_user.id}", reply_markup=kb_approve(eid))
        except Exception: pass

# ════════════════ SHIFTS ══════════════════════════════════════════════════════

@shift_r.message(F.text == "✅ Начать смену")
async def shift_ask_start(msg: Message):
    emp, active = await get_es(msg)
    if not emp: return await msg.answer("Сначала зарегистрируйтесь — /start")
    if not active: return await sub_off(msg)
    if emp.status != "active": return await msg.answer("⛔ Нет доступа. Обратитесь к менеджеру.")
    async with db() as s:
        if await q_open_shift(s, emp.id): return await msg.answer("⚠️ У вас уже открыта смена!")
    await msg.answer("📍 Для начала смены отправьте геолокацию:", reply_markup=kb_loc())

@shift_r.message(F.text == "⏹ Завершить смену")
async def shift_ask_end(msg: Message):
    emp, active = await get_es(msg)
    if not emp or not active or emp.status != "active": return
    async with db() as s:
        if not await q_open_shift(s, emp.id): return await msg.answer("Нет активной смены.")
    await msg.answer("📍 Отправьте геолокацию для завершения смены:", reply_markup=kb_loc())

@shift_r.message(F.location)
async def handle_loc(msg: Message, bot: Bot):
    emp, active = await get_es(msg)
    if not emp or not active or emp.status != "active": return
    lat, lon = msg.location.latitude, msg.location.longitude
    now = datetime.now(timezone.utc)
    async with db() as s:
        locs = await q_locs(s, emp.tenant_id)
        in_zone, dist, loc_name = check_geo(lat, lon, locs)
        op = await q_open_shift(s, emp.id)
        if not op:
            rate = await q_rate(s, emp.tenant_id, emp.role)
            s.add(Shift(employee_id=emp.id, status="open",
                        start_lat=lat, start_lon=lon, start_geo_ok=in_zone, start_location_name=loc_name))
            await s.commit()
            gt = f"✅ На месте ({dist}м)" if in_zone else f"⚠️ Вне зоны ({dist}м от {loc_name})"
            await msg.answer(f"✅ *Смена начата!*\n\n🕐 {now.strftime('%H:%M %d.%m.%Y')}\n"
                             f"💵 Ставка: {rate} ₽/час\n📍 {gt}",
                             parse_mode="Markdown", reply_markup=kb_main(has_open=True))
            if not in_zone:
                t = await q_tenant_id(s, emp.tenant_id)
                for aid in t.admin_ids:
                    try: await bot.send_message(aid, f"⚠️ {emp.full_name} начал смену вне зоны! ({dist}м от {loc_name})")
                    except Exception: pass
        else:
            delta = now - op.started_at.replace(tzinfo=timezone.utc)
            hours = round(delta.total_seconds() / 3600, 2)
            rate  = emp.hourly_rate or await q_rate(s, emp.tenant_id, emp.role)
            earned = round(hours * rate, 2)
            op.status = "closed"; op.ended_at = now; op.hours_worked = hours; op.salary_earned = earned
            op.end_lat = lat; op.end_lon = lon; op.end_geo_ok = in_zone; op.end_location_name = loc_name
            await s.commit()
            gt = f"✅ На месте ({dist}м)" if in_zone else f"⚠️ Вне зоны ({dist}м)"
            await msg.answer(f"⏹ *Смена завершена!*\n\n⏱ {hours}ч | 💵 {earned} ₽\n📍 {gt}\n\nСпасибо, {emp.first_name}!",
                             parse_mode="Markdown", reply_markup=kb_main(has_open=False))

# ════════════════ SALARY ══════════════════════════════════════════════════════

@sal_r.message(F.text == "💰 Моя зарплата")
async def my_salary(msg: Message):
    emp, active = await get_es(msg)
    if not emp or not active or emp.status != "active": return
    now = datetime.now()
    async with db() as s: sm = await q_monthly(s, emp.id, now.month, now.year)
    await msg.answer(f"💰 *Зарплата за {now.strftime('%B %Y')}*\n\n"
                     f"📋 Смен: {sm['shifts']}\n⏱ Часов: {sm['hours']}\n💵 Ставка: {emp.hourly_rate} ₽/час\n"
                     f"━━━━━━━━━━━━━━\n💳 К выплате: *{sm['salary']} ₽*", parse_mode="Markdown")

@sal_r.message(F.text == "📋 История смен")
async def shift_hist(msg: Message):
    emp, active = await get_es(msg)
    if not emp or not active or emp.status != "active": return
    async with db() as s: shifts = await q_shifts(s, emp.id)
    if not shifts: return await msg.answer("📭 Нет завершённых смен.")
    lines = ["📋 *Последние смены:*\n"]
    for sh in shifts[:10]:
        g = "✅" if sh.start_geo_ok else "⚠️"
        te = sh.ended_at.strftime("%H:%M") if sh.ended_at else "—"
        lines.append(f"{g} {sh.started_at.strftime('%d.%m')} | {sh.started_at.strftime('%H:%M')}–{te} | {sh.hours_worked}ч | {sh.salary_earned}₽")
    await msg.answer("\n".join(lines), parse_mode="Markdown")

@sal_r.message(F.text == "📊 Статистика")
async def my_stats(msg: Message):
    emp, active = await get_es(msg)
    if not emp or not active or emp.status != "active": return
    now = datetime.now()
    async with db() as s:
        month = await q_monthly(s, emp.id, now.month, now.year)
        all_sh = await q_shifts(s, emp.id)
    await msg.answer(f"📊 *Статистика*\n\n*Этот месяц:*\n"
                     f"  📋 {month['shifts']} смен | ⏱ {month['hours']}ч | 💵 {month['salary']} ₽\n\n"
                     f"*За всё время:*\n"
                     f"  📋 {len(all_sh)} смен | ⏱ {round(sum(s.hours_worked or 0 for s in all_sh),2)}ч | "
                     f"💵 {round(sum(s.salary_earned or 0 for s in all_sh),2)} ₽", parse_mode="Markdown")

# ════════════════ ADMIN ═══════════════════════════════════════════════════════

@admin_r.message(IsTA(), F.text == "/admin")
async def adm_menu(msg: Message):
    emp, active = await get_es(msg)
    if not active: return await sub_off(msg)
    async with db() as s:
        tenant = await q_tenant_id(s, emp.tenant_id)
        tname = tenant.name.title() if tenant else "CayteringWork"
    await msg.answer(f"🛠 *Админ-панель {tname} Catering*", parse_mode="Markdown", reply_markup=kb_admin())

@admin_r.message(IsTA(), F.text == "👥 Сотрудники")
async def adm_active(msg: Message):
    emp, _ = await get_es(msg)
    async with db() as s: emps = await q_emps(s, emp.tenant_id, "active")
    if not emps: return await msg.answer("📭 Нет активных сотрудников.")
    await msg.answer("👥 *Активные:*", parse_mode="Markdown", reply_markup=kb_emp_list(emps))

@admin_r.message(IsTA(), F.text == "⏳ Ожидают")
async def adm_pending(msg: Message):
    emp, _ = await get_es(msg)
    async with db() as s: emps = await q_emps(s, emp.tenant_id, "pending")
    if not emps: return await msg.answer("✅ Нет ожидающих.")
    await msg.answer("⏳ *Ожидают:*", parse_mode="Markdown", reply_markup=kb_emp_list(emps))

@admin_r.callback_query(F.data.startswith("emp:"), IsTA())
async def adm_emp(cb: CallbackQuery):
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        r = await s.execute(select(Employee).where(Employee.id == eid))
        e = r.scalar_one_or_none()
        if not e: return await cb.answer("Не найден")
        sm = await q_monthly(s, e.id, datetime.now().month, datetime.now().year)
        avg_r, cnt_r = await q_avg_rating(s, e.id)
    rating_line = f"\n⭐ Рейтинг: {avg_r}/5 ({cnt_r} оц.)" if avg_r else "\n⭐ Рейтинг: нет оценок"
    await cb.message.edit_text(
        f"👤 *{e.full_name}*\n💼 {e.role_display}\n💵 {e.hourly_rate} ₽/ч\nСтатус: {e.status}{rating_line}\n\n"
        f"*Этот месяц:* {sm['shifts']} смен | {sm['hours']}ч | {sm['salary']} ₽",
        parse_mode="Markdown", reply_markup=kb_emp_actions(eid, e.status == "blocked"))

@admin_r.callback_query(F.data.startswith("approve:"), IsTA())
async def adm_approve(cb: CallbackQuery, bot: Bot):
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        await s.execute(update(Employee).where(Employee.id == eid).values(status="active"))
        await s.commit()
        r = await s.execute(select(Employee).where(Employee.id == eid)); e = r.scalar_one_or_none()
    await cb.message.edit_text(f"✅ *{md(e.full_name)}* одобрен.", parse_mode="Markdown")
    try: await bot.send_message(e.telegram_id, "✅ Заявка одобрена! Нажмите /start")
    except Exception: pass

@admin_r.callback_query(F.data.startswith("reject:"), IsTA())
async def adm_reject(cb: CallbackQuery, bot: Bot):
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        await s.execute(update(Employee).where(Employee.id == eid).values(status="blocked"))
        await s.commit()
        r = await s.execute(select(Employee).where(Employee.id == eid)); e = r.scalar_one_or_none()
    await cb.message.edit_text(f"❌ Заявка *{md(e.full_name)}* отклонена.", parse_mode="Markdown")
    try: await bot.send_message(e.telegram_id, "❌ Заявка отклонена. Обратитесь к менеджеру.")
    except Exception: pass

@admin_r.callback_query(F.data.startswith("block:"), IsTA())
async def adm_block(cb: CallbackQuery, bot: Bot):
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        await s.execute(update(Employee).where(Employee.id == eid).values(status="blocked"))
        await s.commit()
        r = await s.execute(select(Employee).where(Employee.id == eid)); e = r.scalar_one_or_none()
    await cb.answer(f"🚫 {e.full_name} заблокирован")
    try: await bot.send_message(e.telegram_id, "🚫 Аккаунт заблокирован.")
    except Exception: pass

@admin_r.message(IsTA(), F.text == "📢 Рассылка вакансии")
async def adm_broadcast_ask(msg: Message, state: FSMContext):
    """Admin starts a broadcast vacancy."""
    await msg.answer(
        "📢 *Новая вакансия на смену*\n\n"
        "Введите описание (что, когда, где):\n"
        "Пример:\n`Требуется 10 официантов\n📅 26.05.2025 18:00\n📍 Москва, Красная площадь`",
        parse_mode="Markdown")
    await state.set_state(BroadcastSt.waiting_text)


@admin_r.message(IsTA(), BroadcastSt.waiting_text)
async def adm_broadcast_text(msg: Message, state: FSMContext):
    """Admin enters the vacancy description."""
    await state.update_data(broadcast_text=msg.text.strip())
    await msg.answer("Сколько человек нужно? Введите число (например: `10`):",
                     parse_mode="Markdown")
    await state.set_state(BroadcastSt.waiting_count)


@admin_r.message(IsTA(), BroadcastSt.waiting_count)
async def adm_broadcast_send(msg: Message, state: FSMContext):
    """Admin enters count — create announcement and broadcast."""
    await ensure_db()
    try:
        count = int(msg.text.strip())
        if count <= 0 or count > 1000: raise ValueError()
    except (ValueError, TypeError):
        return await msg.answer("⚠️ Введите корректное число от 1 до 1000")
    d = await state.get_data()
    text = d.get("broadcast_text", "")
    emp_admin, _ = await get_es(msg)
    await state.clear()
    async with db() as s:
        ann = await q_create_announcement(s, emp_admin.tenant_id,
                                          msg.from_user.id, text, count)
        ann_id = ann.id
        # Get all active employees
        employees = await q_emps(s, emp_admin.tenant_id, status="active")
    sent, failed = 0, 0
    ann_msg = (f"📢 *Открыта вакансия на смену!*\n\n"
               f"{text}\n\n"
               f"Мест доступно: *{count}/{count}*")
    for emp in employees:
        if emp.telegram_id == msg.from_user.id:
            continue  # skip admin
        try:
            await _bot.send_message(
                emp.telegram_id, ann_msg,
                parse_mode="Markdown",
                reply_markup=kb_broadcast_respond(ann_id, count, count))
            sent += 1
        except Exception:
            failed += 1
    await msg.answer(
        f"✅ *Рассылка отправлена!*\n\n"
        f"📢 {text[:100]}...\n"
        f"👥 Мест: {count}\n\n"
        f"Отправлено: {sent} | Не доставлено: {failed}\n\n"
        f"Вы получите уведомление когда все места будут заняты.",
        parse_mode="Markdown", reply_markup=kb_admin())


@admin_r.callback_query(F.data.startswith("invite_emp:"), IsTA())
async def adm_invite_ask(cb: CallbackQuery, state: FSMContext):
    """Admin selects employee to invite — ask for shift date/time."""
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        r = await s.execute(select(Employee).where(Employee.id == eid))
        e = r.scalar_one_or_none()
        if not e: return await cb.answer("Сотрудник не найден")
    await state.update_data(invite_emp_id=eid)
    await state.set_state(InviteSt.waiting_date)
    await cb.message.answer(
        f"📩 *Приглашение для {e.full_name}*\n\n"
        "Введите дату и время смены:\n"
        "Пример: `26.05.2025 18:00`",
        parse_mode="Markdown")
    await cb.answer()


@admin_r.message(IsTA(), InviteSt.waiting_date)
async def adm_invite_send(msg: Message, state: FSMContext):
    """Send shift invitation to employee."""
    await ensure_db()
    shift_date = msg.text.strip()
    d = await state.get_data()
    eid = d.get("invite_emp_id")
    if not eid:
        await state.clear(); return await msg.answer("❌ Ошибка. Попробуйте снова через /admin")
    async with db() as s:
        r = await s.execute(select(Employee).where(Employee.id == eid))
        employee = r.scalar_one_or_none()
        if not employee:
            await state.clear(); return await msg.answer("❌ Сотрудник не найден.")
        tenant = await q_tenant_id(s, employee.tenant_id)
        tenant_name = tenant.name if tenant else "Ресторан"
        inv = await q_create_invite(s, employee_id=eid, tenant_id=employee.tenant_id,
                                    admin_id=msg.from_user.id, shift_date=shift_date)
    await state.clear()
    try:
        await _bot.send_message(
            employee.telegram_id,
            f"📨 *Приглашение на смену*\n\n"
            f"🏢 {tenant_name}\n"
            f"📅 {shift_date}\n\n"
            f"Менеджер приглашает вас на работу. Пожалуйста, подтвердите:",
            parse_mode="Markdown",
            reply_markup=kb_invite_response(inv.id))
        await msg.answer(
            f"✅ Приглашение отправлено *{employee.full_name}*!\n"
            f"📅 {shift_date}\n\nВы получите уведомление о его ответе.",
            parse_mode="Markdown", reply_markup=kb_admin())
    except Exception as ex:
        await msg.answer(
            f"❌ Не удалось отправить. Убедитесь что сотрудник уже начал чат с ботом.\nОшибка: {ex}",
            reply_markup=kb_admin())


@admin_r.callback_query(F.data.startswith("setrate:"), IsTA())
async def adm_setrate_ask(cb: CallbackQuery, state: FSMContext):
    """Ask admin for individual hourly rate for a specific employee."""
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        r = await s.execute(select(Employee).where(Employee.id == eid))
        e = r.scalar_one_or_none()
        if not e:
            return await cb.answer("Сотрудник не найден")
    await state.update_data(ind_rate_emp_id=eid)
    await state.set_state(AdminSt.ind_rate)
    await cb.message.answer(
        f"💵 Введите индивидуальную ставку для *{e.full_name}* (₽/час):\n"
        f"Текущая ставка: {e.hourly_rate} ₽/час",
        parse_mode="Markdown")
    await cb.answer()


@admin_r.message(IsTA(), AdminSt.ind_rate)
async def adm_setrate_save(msg: Message, state: FSMContext):
    """Save individual rate for the employee."""
    await ensure_db()
    try:
        rate = float(msg.text.strip().replace(",", "."))
        if rate <= 0 or rate > 100_000:
            raise ValueError()
    except (ValueError, TypeError):
        return await msg.answer("⚠️ Введите корректное число, например: 750")
    d = await state.get_data()
    eid = d.get("ind_rate_emp_id")
    if not eid:
        await state.clear()
        return await msg.answer("❌ Ошибка: сотрудник не выбран. Попробуйте снова через /admin")
    async with db() as s:
        await s.execute(update(Employee).where(Employee.id == eid).values(hourly_rate=rate))
        await s.commit()
        r = await s.execute(select(Employee).where(Employee.id == eid))
        e = r.scalar_one_or_none()
    await state.clear()
    await msg.answer(
        f"✅ Индивидуальная ставка установлена!\n\n"
        f"👤 {md(e.full_name)}\n"
        f"💵 Новая ставка: *{rate} ₽/час*",
        parse_mode="Markdown",
        reply_markup=kb_admin())


@admin_r.callback_query(F.data.startswith("rate_shift:"), IsTA())
async def adm_rate_shift(cb: CallbackQuery, state: FSMContext):
    """Handle star rating click for a completed shift."""
    await ensure_db()
    _, shift_id_s, score_s = cb.data.split(":")
    shift_id, score = int(shift_id_s), int(score_s)
    async with db() as s:
        already = await q_shift_rated(s, shift_id)
        if already:
            return await cb.answer("Эта смена уже оценена!", show_alert=True)
    await state.update_data(rating_shift_id=shift_id, rating_score=score,
                            rating_admin_id=cb.from_user.id)
    await state.set_state(RatingSt.waiting_comment)
    stars = "⭐" * score
    await cb.message.edit_text(
        f"Оценка {stars} *{score}/5* принята!\n\n"
        f"Напишите комментарий для сотрудника (или /skip чтобы пропустить):",
        parse_mode="Markdown")
    await cb.answer()


@admin_r.message(IsTA(), RatingSt.waiting_comment)
async def adm_rating_comment(msg: Message, state: FSMContext):
    """Save rating with optional comment."""
    await ensure_db()
    d = await state.get_data()
    comment = None if msg.text.strip() == "/skip" else msg.text.strip()
    async with db() as s:
        # Get shift to find employee
        from sqlalchemy import select as sa_select
        r = await s.execute(sa_select(Shift).where(Shift.id == d["rating_shift_id"]))
        sh = r.scalar_one_or_none()
        if not sh:
            await state.clear()
            return await msg.answer("❌ Смена не найдена.")
        emp = await q_emp(s, None)  # get by id
        r2 = await s.execute(sa_select(Employee).where(Employee.id == sh.employee_id))
        employee = r2.scalar_one_or_none()
        # Get admin's tenant_id
        admin_emp = await q_emp(s, msg.from_user.id)
        tenant_id = admin_emp.tenant_id if admin_emp else sh.employee_id
        await q_create_rating(s, shift_id=d["rating_shift_id"],
                              employee_id=sh.employee_id,
                              tenant_id=tenant_id,
                              admin_id=d["rating_admin_id"],
                              score=d["rating_score"],
                              comment=comment)
        avg, cnt = await q_avg_rating(s, sh.employee_id)
    await state.clear()
    stars = "⭐" * d["rating_score"]
    comment_txt = f"\n💬 «{comment}»" if comment else ""
    await msg.answer(
        f"✅ Оценка сохранена!\n{stars} *{d['rating_score']}/5*{comment_txt}\n\n"
        f"Средний рейтинг {employee.full_name if employee else ''}: *{avg}/5* ({cnt} оц.)",
        parse_mode="Markdown", reply_markup=kb_admin())
    # Notify the employee
    if employee:
        try:
            star_disp = "⭐" * d["rating_score"]
            emp_msg = (f"📊 *Ваша смена оценена!*\n\n{star_disp} *{d['rating_score']}/5*"
                      + (f"\n💬 Комментарий: «{comment}»" if comment else "")
                      + f"\n\n⭐ Ваш средний рейтинг: *{avg}/5* ({cnt} оценок)")
            from aiogram import Bot as _Bot
            _b = _bot  # use global bot
            await _b.send_message(employee.telegram_id, emp_msg, parse_mode="Markdown")
        except Exception:
            pass


@admin_r.callback_query(F.data.startswith("unblock:"), IsTA())
async def adm_unblock(cb: CallbackQuery):
    await ensure_db()
    eid = int(cb.data.split(":")[1])
    async with db() as s:
        await s.execute(update(Employee).where(Employee.id == eid).values(status="active"))
        await s.commit()
    await cb.answer("✅ Разблокирован")

@admin_r.message(IsTA(), F.text == "💵 Ставки")
async def adm_rates(msg: Message):
    await msg.answer(
        "💵 *Общие ставки по ролям*\n"
        "_(применяются при регистрации новых сотрудников)_\n\n"
        "Для индивидуальной ставки откройте карточку сотрудника через 👥 Сотрудники → выбери сотрудника → 💵 Установить ставку",
        parse_mode="Markdown",
        reply_markup=kb_rate_roles())

@admin_r.callback_query(F.data.startswith("rate_role:"), IsTA())
async def adm_rate_ask(cb: CallbackQuery, state: FSMContext):
    role = cb.data.split(":")[1]
    await state.update_data(rate_role=role)
    rn = "официанта" if role == "waiter" else "грузчика"
    await cb.message.answer(f"Введите ставку {rn} (₽/час):")
    await state.set_state(AdminSt.rate)

@admin_r.message(IsTA(), AdminSt.rate)
async def adm_rate_save(msg: Message, state: FSMContext):
    try: rate = float(msg.text.strip().replace(",", "."))
    except ValueError: return await msg.answer("⚠️ Введите число, например: 600")
    d = await state.get_data(); emp, _ = await get_es(msg)
    async with db() as s:
        r = await s.execute(select(Rate).where(and_(Rate.tenant_id == emp.tenant_id, Rate.role == d["rate_role"])))
        ex = r.scalar_one_or_none()
        if ex: await s.execute(update(Rate).where(Rate.id == ex.id).values(hourly_rate=rate))
        else: s.add(Rate(tenant_id=emp.tenant_id, role=d["rate_role"], hourly_rate=rate))
        await s.commit()
    rn = "🤵 Официанты" if d["rate_role"] == "waiter" else "👷 Грузчики"
    await msg.answer(f"✅ {rn}: {rate} ₽/час"); await state.clear()

@admin_r.message(IsTA(), F.text == "📊 Отчёт")
async def adm_report(msg: Message):
    emp, _ = await get_es(msg)
    now = datetime.now()
    async with db() as s:
        emps = await q_emps(s, emp.tenant_id, "active")
        lines = [f"📊 *Отчёт за {now.strftime('%B %Y')}*\n"]; total = 0.0
        for e in emps:
            sm = await q_monthly(s, e.id, now.month, now.year)
            lines.append(f"👤 {e.full_name} ({e.role_display})\n   ⏱ {sm['hours']}ч | 💵 {sm['salary']} ₽")
            total += sm["salary"]
        lines.append(f"━━━━━━━━━━━━━━\n💳 Итого: *{round(total,2)} ₽*")
    await msg.answer("\n".join(lines), parse_mode="Markdown")

@admin_r.message(IsTA(), F.text == "📍 Локации")
async def adm_locs(msg: Message, state: FSMContext):
    emp, _ = await get_es(msg)
    async with db() as s: locs = await q_locs(s, emp.tenant_id)
    if locs:
        await msg.answer("📍 *Текущие локации:*\n" + "\n".join(f"• {l.name} ({l.radius_meters}м)" for l in locs),
                         parse_mode="Markdown")
    await msg.answer("Введите название новой локации (или /skip):"); await state.set_state(AdminSt.loc_name)

@admin_r.message(IsTA(), AdminSt.loc_name)
async def adm_loc_name(msg: Message, state: FSMContext):
    if msg.text == "/skip":
        await state.clear(); return await msg.answer("Отменено.", reply_markup=kb_admin())
    await state.update_data(loc_name=msg.text.strip())
    await msg.answer("📍 Отправьте геолокацию:", reply_markup=kb_loc())
    await state.set_state(AdminSt.loc_geo)

@admin_r.message(IsTA(), AdminSt.loc_geo, F.location)
async def adm_loc_geo(msg: Message, state: FSMContext):
    d = await state.get_data(); emp, _ = await get_es(msg)
    async with db() as s:
        loc = Location(tenant_id=emp.tenant_id, name=d["loc_name"],
                       lat=msg.location.latitude, lon=msg.location.longitude, radius_meters=GEO_RADIUS)
        s.add(loc); await s.commit()
    await msg.answer(f"✅ Локация «{loc.name}» добавлена ({loc.radius_meters}м)", reply_markup=kb_admin())
    await state.clear()

@admin_r.message(IsTA(), F.text == "📤 Google Sheets")
async def adm_export(msg: Message):
    emp, _ = await get_es(msg); await msg.answer("⏳ Экспортирую данные...")
    now = datetime.now()
    async with db() as s:
        emps = await q_emps(s, emp.tenant_id)
        rows = [["TG ID","ФИО","Роль","Статус","Ставка","Смен","Часов","Сумма"]]
        for e in emps:
            sm = await q_monthly(s, e.id, now.month, now.year)
            rows.append([str(e.telegram_id), e.full_name, e.role_display, e.status,
                         str(e.hourly_rate), str(sm["shifts"]), str(sm["hours"]), str(sm["salary"])])
    try:
        async with AsyncCodewordsClient() as client:
            r1 = await client.run(service_id="composio", inputs={
                "tool_slug": "GOOGLESHEETS_CREATE_GOOGLE_SHEET1",
                "arguments": {"title": f"Bellini — {now.strftime('%B %Y')}"}})
            rd  = r1.json().get("data", {}).get("response_data", {})
            sid = rd.get("spreadsheet_id", "")
            sheets = rd.get("sheets", [])
            sn = sheets[0]["properties"]["title"] if sheets else "Sheet1"
            if not sid: raise ValueError("no sheet id")
            await client.run(service_id="composio", inputs={
                "tool_slug": "GOOGLESHEETS_VALUES_UPDATE",
                "arguments": {"spreadsheet_id": sid, "range": f"{sn}!A1",
                              "value_input_option": "USER_ENTERED", "values": rows}})
        await msg.answer(f"✅ Данные экспортированы!\nhttps://docs.google.com/spreadsheets/d/{sid}",
                         reply_markup=kb_admin())
    except Exception as ex:
        logger.error("sheets export failed")
        await msg.answer("❌ Ошибка экспорта. Проверьте подключение к Google Sheets.")

# ════════════════ EXPIRY CHECK ════════════════════════════════════════════════

async def check_expiry(bot: Bot):
    await ensure_db()
    async with db() as s:
        r = await s.execute(select(Subscription).where(Subscription.is_active == True))
        subs = r.scalars().all()
        now = datetime.now(timezone.utc)
        for sub in subs:
            if not sub.expires_at: continue
            days = (sub.expires_at - now).days
            if 0 <= days <= 3:
                t = await q_tenant_id(s, sub.tenant_id)
                if t:
                    for aid in t.admin_ids:
                        try:
                            await bot.send_message(aid,
                                f"⚠️ *Подписка истекает через {days} дн.!*\n"
                                f"Обратитесь к разработчику для продления.", parse_mode="Markdown")
                        except Exception: pass


# ════════════════ MAIN (POLLING) ══════════════════════════


# ── FALLBACK ROUTER (included LAST) ───────────────────────────────────────────
fallback_r = Router()

@fallback_r.callback_query()
async def fallback_callback(cb: CallbackQuery, state: FSMContext):
    """Catch stale callbacks (e.g. from messages before bot restart)."""
    await cb.answer("⚠️ Сессия истекла — напишите /start", show_alert=True)


async def main():
    token = os.environ["BOT_TOKEN"]
    bot = Bot(token=token)
    dp = Dispatcher(storage=make_storage())
    dp.include_router(sa_r)
    dp.include_router(reg_r)
    dp.include_router(admin_r)
    dp.include_router(shift_r)
    dp.include_router(sal_r)
    dp.include_router(fallback_r)  # MUST be last
    await ensure_db()
    logger.info("CayteringWork Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
