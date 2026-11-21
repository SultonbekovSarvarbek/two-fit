"""«Sell a membership» wizard. Also used to edit an existing listing (re-moderation)."""
from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...i18n import CITIES
from ...money import fmt_money
from ...repo import LK, LS, RepoError, today_local
from ...validators import (
    TARIFF_PLANS,
    ValidationError,
    check_plan_end,
    normalize_phone,
    parse_description,
    parse_end_date,
    parse_freeze_days,
    parse_monthly,
    parse_name,
    parse_payments_left,
    parse_price,
    parse_promo_code,
    parse_start_date,
    parse_tariff,
)
from ..callbacks import AdmCb, SellCb
from ..context import Ctx
from ..middlewares import Btn
from ..notify import Notifier
from ..ui import (
    advice_text,
    btn,
    commission_block,
    esc,
    kb,
    left_text,
    listing_public,
    main_menu,
    phone_kb,
    price_advice,
)

router = Router(name="sell")


class SellForm(StatesGroup):
    kind = State()
    city = State()
    plan = State()
    tariff = State()
    date = State()
    start = State()
    payment = State()
    monthly = State()
    left = State()
    freeze = State()
    freeze_days = State()
    code = State()
    price = State()
    desc = State()
    photo = State()
    name = State()
    phone = State()
    preview = State()
    choose = State()


STEPS = ["kind", "city", "plan", "tariff", "date", "start", "payment", "monthly", "left", "freeze", "freeze_days", "code", "price", "desc", "photo",
         "name", "phone"]
INSTALLMENT_STEPS = ("monthly", "left")  # asked only when the membership is paid in installments
MEMBERSHIP_ONLY = ("plan", "start", "payment", "monthly", "left", "freeze", "freeze_days")
PROMO_ONLY = ("tariff", "code")  # a membership has no tariff: «12 months» misled buyers
STATE_OF = {s: getattr(SellForm, s) for s in STEPS}
# Steps shown as «Step n/total»; the installment answers and the phone belong to the step before them.
NUMBERED = {
    LK.MEMBERSHIP: ["city", "plan", "date", "start", "payment", "freeze", "price", "desc", "photo", "name"],
    LK.PROMO: ["city", "tariff", "date", "code", "price", "desc", "photo", "name"],
}
FIELDS = {  # field in «Change» menu -> first step to ask
    "city": "city", "plan": "plan", "tariff": "tariff", "date": "date", "start": "start", "payment": "payment", "freeze": "freeze", "code": "code",
    "price": "price", "desc": "desc", "photo": "photo", "contacts": "name",
}
# A promo code is described in its own words: what it gives, until when to activate it, what it costs.
PROMO_TEXTS = {"tariff": "sell.ask_tariff_promo", "date": "sell.ask_date_promo", "price": "sell.ask_price_promo"}


def _kind(data: dict) -> str:
    return (data.get("draft") or {}).get("kind") or LK.MEMBERSHIP


def _installment(data: dict) -> bool:
    return _kind(data) == LK.MEMBERSHIP and (data.get("draft") or {}).get("paid_in_full") is False


def _skipped(data: dict, step: str) -> bool:
    if _kind(data) == LK.PROMO:
        return step in MEMBERSHIP_ONLY
    if step == "price" and data.get("auto_price"):  # worked out from the tariff, the end date and the installments
        return True
    if step == "freeze_days":  # asked only after «yes, there are freeze days»
        return not (data.get("draft") or {}).get("has_freeze")
    return step in PROMO_ONLY or (step in INSTALLMENT_STEPS and not _installment(data))


def _neighbour(data: dict, step: str, delta: int) -> str:
    """Next (+1) or previous (-1) step, skipping what does not apply to this promo code or membership."""
    i = STEPS.index(step) + delta
    while _skipped(data, STEPS[i]):
        i += delta
    return STEPS[i]


def _fields(data: dict) -> list[str]:
    """What the «Change» menu offers for this promo code or membership."""
    return [f for f, step in FIELDS.items() if not _skipped(data, step)]


# ------------------------------------------------------------------ prompts
def _nav(ctx: Ctx, step: str, extra: list | None = None, single: bool = False,
         first: bool = False) -> InlineKeyboardMarkup:
    """``first``: the first question, so there is nothing to go back to."""
    rows = list(extra or [])
    bottom = []
    if (step != "kind" and not first) or single:
        bottom.append(btn(ctx.t("common.back"), SellCb(action="back")))
    bottom.append(btn(ctx.t("common.cancel"), SellCb(action="cancel")))
    rows.append(bottom)
    return kb(*rows)


def step_title(ctx: Ctx, data: dict, step: str) -> str:
    numbered = [s for s in NUMBERED[_kind(data)] if not _skipped(data, s)]
    if step not in numbered:
        return ""
    return ctx.t("sell.step", n=numbered.index(step) + 1, total=len(numbered))


def prompt(ctx: Ctx, data: dict, step: str) -> str:
    key = PROMO_TEXTS.get(step) if _kind(data) == LK.PROMO else None
    return step_title(ctx, data, step) + ctx.t(key or f"sell.ask_{step}", percent=ctx.percent(_kind(data)))


async def ask(message: Message, state: FSMContext, ctx: Ctx, step: str) -> None:
    await state.set_state(STATE_OF[step])
    data = await state.get_data()
    if step == "kind":
        options = [[btn(ctx.t("sell.kind_membership"), SellCb(action="kind", value=LK.MEMBERSHIP))],
                   [btn(ctx.t("sell.kind_promo"), SellCb(action="kind", value=LK.PROMO))]]
        await message.answer(ctx.t("sell.ask_kind"), reply_markup=_nav(ctx, step, options))
    elif step == "city":
        names = [btn(ctx.i18n.city(ctx.lang, c), SellCb(action="city", value=c)) for c in CITIES]
        rows = [names[i:i + 2] for i in range(0, len(names), 2)]
        await message.answer(prompt(ctx, data, step),
                             reply_markup=_nav(ctx, step, rows, first=not ctx.cfg.promo_enabled and data.get("mode") == "new"))
    elif step == "plan":
        options = [[btn(ctx.t(f"app.plan.{days}"), SellCb(action="plan", value=str(days)))] for days in TARIFF_PLANS]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, options))
    elif step in ("tariff", "date", "code"):
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step))
    elif step == "start":
        skip = [[btn(ctx.t("common.skip"), SellCb(action="skip"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, skip))
    elif step == "payment":
        options = [[btn(ctx.t("sell.pay_full"), SellCb(action="pay", value="full"))],
                   [btn(ctx.t("sell.pay_installment"), SellCb(action="pay", value="installment"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, options))
    elif step == "monthly":
        await message.answer(ctx.t("sell.ask_monthly"), reply_markup=_nav(ctx, step))
    elif step == "left":
        await message.answer(ctx.t("sell.ask_left"), reply_markup=_nav(ctx, step))
    elif step == "freeze":
        options = [[btn(ctx.t("sell.freeze_no"), SellCb(action="freeze", value="no")),
                    btn(ctx.t("sell.freeze_yes"), SellCb(action="freeze", value="yes"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, options))
    elif step == "freeze_days":
        await message.answer(ctx.t("sell.ask_freeze_days"), reply_markup=_nav(ctx, step))
    elif step == "price":
        d = data.get("draft") or {}
        if _installment(data):
            text = step_title(ctx, data, step) + ctx.t(
                "sell.ask_price_installment", monthly=fmt_money(d["monthly_payment"]), left=d["payments_left"])
        else:
            text = prompt(ctx, data, step)
        await message.answer(text + _advice_part(ctx, d), reply_markup=_nav(ctx, step, _advice_rows(ctx, d)))
    elif step == "desc":
        skip = [[btn(ctx.t("common.skip"), SellCb(action="skip"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, skip))
    elif step == "photo":
        skip = [[btn(ctx.t("common.skip"), SellCb(action="skip"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, skip))
    elif step == "name":
        extra = []
        u = ctx.user
        if u.get("contact_name") and u.get("contact_phone"):
            extra = [[btn(ctx.t("sell.use_saved", name=u["contact_name"], phone=u["contact_phone"]),
                          SellCb(action="saved"))]]
        await message.answer(prompt(ctx, data, step), reply_markup=_nav(ctx, step, extra))
    elif step == "phone":
        await message.answer(ctx.t("sell.ask_phone"), reply_markup=phone_kb(ctx))


def _advice_part(ctx: Ctx, draft: dict) -> str:
    a = price_advice(ctx, draft)
    return "\n\n" + advice_text(ctx, a) if a else ""


def _advice_rows(ctx: Ctx, draft: dict) -> list:
    """One tap to take the recommended price."""
    a = price_advice(ctx, draft)
    if not a:
        return []
    return [[btn(ctx.t("advice.use_btn", price=fmt_money(a.recommended)), SellCb(action="advice", value=str(a.recommended)))]]


async def advance(message: Message, state: FSMContext, ctx: Ctx, step: str) -> None:
    """Go to the step after ``step`` — or back to the preview when editing a single field."""
    data = await state.get_data()
    has_freeze = bool((data.get("draft") or {}).get("has_freeze"))
    if step == "name" or (step in ("payment", "monthly") and _installment(data)) or (step == "freeze" and has_freeze):
        # Steps that belong together: name -> phone, installment -> monthly payment -> payments left.
        await ask(message, state, ctx, _neighbour(data, step, 1))
        return
    if data.get("single") or step == "phone":
        await show_preview(message, state, ctx)
        return
    await ask(message, state, ctx, _neighbour(data, step, 1))


async def draft_update(state: FSMContext, **values) -> dict:
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    draft.update(values)
    await state.update_data(draft=draft)
    return draft


# ------------------------------------------------------------------ preview
def preview_text(ctx: Ctx, data: dict) -> str:
    d = data["draft"]
    private = ctx.t(
        "sell.private_block",
        name=esc(d["seller_name"]),
        phone=esc(d["seller_phone"]),
        photo=ctx.t("sell.photo_yes" if d.get("screenshot_file_id") else "sell.photo_no"),
    )
    if d.get("promo_code"):
        private += ctx.t("sell.private_code", code=esc(d["promo_code"]))
    parts = [
        ctx.t("sell.preview_title"),
        listing_public(ctx, d, data.get("listing_id") or "—"),
        "—",
        commission_block(ctx, d["price"], _kind(data)),
        ctx.t("commission.note"),
        private,
    ]
    if data.get("auto_price") and _kind(data) == LK.MEMBERSHIP:
        parts.insert(3, ctx.t("sell.price_auto"))  # right under the card: where the price comes from
    return "\n\n".join(parts)


def preview_kb(ctx: Ctx, data: dict) -> InlineKeyboardMarkup:
    accepted = bool(data.get("accepted"))
    negotiable = bool((data.get("draft") or {}).get("price_negotiable"))
    percent = ctx.percent(_kind(data))
    return kb(
        [btn(ctx.t("sell.negotiable_on" if negotiable else "sell.negotiable_off"), SellCb(action="negotiable"))],
        [btn(ctx.t("sell.accept_on" if accepted else "sell.accept_off", percent=percent), SellCb(action="accept"))],
        [btn(ctx.t("sell.send"), SellCb(action="send"))],
        [btn(ctx.t("sell.edit"), SellCb(action="edit")), btn(ctx.t("sell.cancel"), SellCb(action="cancel"))],
    )


async def apply_auto_price(state: FSMContext, ctx: Ctx) -> None:
    """With AUTO_PRICE a membership costs the recommended price; any change of it needs the consent again."""
    data = await state.get_data()
    if not data.get("auto_price") or _kind(data) != LK.MEMBERSHIP:
        return
    draft = data.get("draft") or {}
    a = price_advice(ctx, draft) if draft.get("tariff_days") else None
    if a and draft.get("price") != a.recommended:
        await draft_update(state, price=a.recommended)
        await state.update_data(accepted=False)


async def show_preview(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.set_state(SellForm.preview)
    await state.update_data(single=False)
    await apply_auto_price(state, ctx)
    data = await state.get_data()
    await message.answer(preview_text(ctx, data), reply_markup=preview_kb(ctx, data))


# ------------------------------------------------------------------ entry points
@router.message(Btn("menu.sell"))
async def menu_sell(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    if not ctx.cfg.promo_enabled:  # promo codes are hidden: only a membership can be sold
        await state.set_data({"mode": "new", "draft": {"kind": LK.MEMBERSHIP}, "accepted": False,
                              "auto_price": ctx.cfg.auto_price})
        await message.answer(ctx.t("sell.intro_membership"), reply_markup=main_menu(ctx))
        await ask(message, state, ctx, "city")
        return
    await state.set_data({"mode": "new", "draft": {}, "accepted": False, "auto_price": ctx.cfg.auto_price})
    await message.answer(ctx.t("sell.intro"), reply_markup=main_menu(ctx))
    await ask(message, state, ctx, "kind")


async def start_edit(message: Message, state: FSMContext, ctx: Ctx, listing: dict) -> None:
    """Called from «My listings»: open the preview of an existing listing for editing."""
    draft = {k: listing[k] for k in (
        "city", "tariff", "end_date", "start_date", "price", "price_negotiable", "description", "screenshot_file_id",
        "seller_name", "seller_phone", "monthly_payment", "payments_left", "kind", "promo_code", "tariff_days", "freeze_days",
    )}
    draft["paid_in_full"] = None if listing["paid_in_full"] is None else bool(listing["paid_in_full"])
    draft["has_freeze"] = bool(listing["freeze_days"])
    await state.clear()
    await state.set_data({
        "mode": "edit",
        "listing_id": listing["id"],
        "draft": draft,
        # Already accepted for this exact price and today's rate; a price or rate change resets it.
        "accepted": listing["commission_accepted_price"] == listing["price"]
        and ctx.repo.listing_percent(listing) == ctx.percent(listing["kind"]),
        "auto_price": ctx.cfg.auto_price,
    })
    await message.answer(ctx.t("sell.edit_intro", id=listing["id"]))
    if draft["kind"] == LK.MEMBERSHIP and draft["tariff_days"] is None:
        # Listed before the tariff was picked from a list: ask it first.
        await state.update_data(single=True)
        await ask(message, state, ctx, "plan")
        return
    if draft["kind"] == LK.MEMBERSHIP and draft["paid_in_full"] is None:
        # Listed before the question existed: ask it first, then show the preview.
        await state.update_data(single=True)
        await ask(message, state, ctx, "payment")
        return
    await show_preview(message, state, ctx)


# ------------------------------------------------------------------ navigation
_WIZARD = (SellForm.kind, SellForm.code, SellForm.city, SellForm.plan, SellForm.freeze, SellForm.freeze_days, SellForm.tariff, SellForm.date, SellForm.start, SellForm.payment, SellForm.monthly,
           SellForm.left, SellForm.price, SellForm.desc, SellForm.photo, SellForm.name, SellForm.phone,
           SellForm.preview, SellForm.choose)


@router.callback_query(SellCb.filter(F.action == "cancel"), F.message)
async def on_cancel(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if await state.get_state() not in {s.state for s in _WIZARD}:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("sell.cancelled"), reply_markup=main_menu(ctx))


@router.callback_query(SellCb.filter(F.action == "back"))
async def on_back(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    current = await state.get_state()
    step = next((s for s in STEPS if STATE_OF[s].state == current), None)
    if step is None:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if data.get("single") and step in INSTALLMENT_STEPS:
        await ask(call.message, state, ctx, _neighbour(data, step, -1))  # the installment answers come together
    elif data.get("single") and step != "phone":
        await show_preview(call.message, state, ctx)
    elif step == "kind":
        await show_preview(call.message, state, ctx)
    else:
        await ask(call.message, state, ctx, _neighbour(data, step, -1))


async def _phone_text_nav(message: Message, state: FSMContext, ctx: Ctx) -> bool:
    """«Back»/«Cancel» typed via the reply keyboard on the phone step."""
    text = message.text or ""
    if text in ctx.i18n.variants("common.cancel"):
        await state.clear()
        await message.answer(ctx.t("sell.cancelled"), reply_markup=main_menu(ctx))
        return True
    if text in ctx.i18n.variants("common.back"):
        await message.answer(ctx.t("menu.hint"), reply_markup=main_menu(ctx))
        await ask(message, state, ctx, "name")
        return True
    return False


# ------------------------------------------------------------------ steps
@router.callback_query(SellForm.kind, SellCb.filter(F.action == "kind"))
async def on_kind(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    if callback_data.value not in LK.ALL or (callback_data.value == LK.PROMO and not ctx.cfg.promo_enabled):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    choice = ctx.t("sell.kind_promo" if callback_data.value == LK.PROMO else "sell.kind_membership")
    await call.message.edit_text(ctx.t("sell.ask_kind") + "\n\n<b>" + esc(choice) + "</b>")
    if _kind(await state.get_data()) != callback_data.value:
        await state.update_data(accepted=False)  # another kind, another commission: confirm it again
    await draft_update(state, kind=callback_data.value)
    await advance(call.message, state, ctx, "kind")


@router.message(SellForm.code)
async def on_code(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "code", parse_promo_code, "promo_code")


@router.callback_query(SellForm.city, SellCb.filter(F.action == "city"))
async def on_city(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    if callback_data.value not in CITIES:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        prompt(ctx, await state.get_data(), "city") + " <b>" + esc(ctx.i18n.city(ctx.lang, callback_data.value)) + "</b>"
    )
    await draft_update(state, city=callback_data.value)
    await advance(call.message, state, ctx, "city")


async def _text_step(message: Message, state: FSMContext, ctx: Ctx, step: str, parser, field: str) -> None:
    if not message.text:
        await message.answer(ctx.t("err.text_expected"))
        return
    try:
        value = parser(message.text)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=_nav(ctx, step))
        return
    await draft_update(state, **{field: value})
    await advance(message, state, ctx, step)


@router.message(SellForm.tariff)
async def on_tariff(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "tariff", parse_tariff, "tariff")


@router.message(SellForm.date)
async def on_date(message: Message, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    start = (data.get("draft") or {}).get("start_date")
    if start and message.text:
        # A start date that is no longer before the new end date would show nonsense — drop it.
        try:
            if start >= parse_end_date(message.text, today_local()).isoformat():
                await draft_update(state, start_date=None)
        except ValidationError:
            pass
    plan_days = (data.get("draft") or {}).get("tariff_days")

    def parse(text: str) -> str:
        end = parse_end_date(text, today_local())
        check_plan_end(end, today_local(), plan_days)  # 20 months left on a 12-month tariff is a mistake
        return end.isoformat()

    await _text_step(message, state, ctx, "date", parse, "end_date")


@router.message(SellForm.start)
async def on_start(message: Message, state: FSMContext, ctx: Ctx) -> None:
    end = date.fromisoformat((await state.get_data())["draft"]["end_date"])
    await _text_step(message, state, ctx, "start",
                     lambda t: parse_start_date(t, today_local(), end).isoformat(), "start_date")


@router.callback_query(SellForm.start, SellCb.filter(F.action == "skip"))
async def on_start_skip(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, start_date=None)
    await advance(call.message, state, ctx, "start")


@router.callback_query(SellForm.payment, SellCb.filter(F.action == "pay"))
async def on_payment(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    if callback_data.value not in ("full", "installment"):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    full = callback_data.value == "full"
    await call.answer()
    choice = ctx.t("sell.pay_full" if full else "sell.pay_installment")
    await call.message.edit_text(prompt(ctx, await state.get_data(), "payment") + "\n\n<b>" + esc(choice) + "</b>")
    if full:
        await draft_update(state, paid_in_full=True, monthly_payment=None, payments_left=None)
    else:
        await draft_update(state, paid_in_full=False)
    await advance(call.message, state, ctx, "payment")


@router.message(SellForm.monthly)
async def on_monthly(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "monthly", parse_monthly, "monthly_payment")


@router.message(SellForm.left)
async def on_left(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "left", parse_payments_left, "payments_left")


@router.callback_query(SellForm.freeze, SellCb.filter(F.action == "freeze"))
async def on_freeze(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    """Unused freeze days go to the buyer with the membership: worth showing."""
    if callback_data.value not in ("yes", "no"):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    yes = callback_data.value == "yes"
    await call.answer()
    choice = ctx.t("sell.freeze_yes" if yes else "sell.freeze_no")
    await call.message.edit_text(prompt(ctx, await state.get_data(), "freeze") + "\n\n<b>" + esc(choice) + "</b>")
    if yes:
        await draft_update(state, has_freeze=True)
    else:
        await draft_update(state, has_freeze=False, freeze_days=0)
    await advance(call.message, state, ctx, "freeze")


@router.message(SellForm.freeze_days)
async def on_freeze_days(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "freeze_days", parse_freeze_days, "freeze_days")


@router.message(SellForm.price)
async def on_price(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not message.text:
        await message.answer(ctx.t("err.text_expected"))
        return
    try:
        price = parse_price(message.text)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=_nav(ctx, "price"))
        return
    await _set_price(message, state, ctx, price)


@router.callback_query(SellForm.price, SellCb.filter(F.action == "advice"))
async def on_price_advice(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    """«✅ Set 4 530 000» under the price question: take the recommended price."""
    a = price_advice(ctx, (await state.get_data()).get("draft") or {})
    if not a or callback_data.value != str(a.recommended):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await _set_price(call.message, state, ctx, a.recommended)


async def _set_price(message: Message, state: FSMContext, ctx: Ctx, price: int, confirmed: bool = False) -> None:
    data = await state.get_data()
    draft = data.get("draft") or {}
    a = price_advice(ctx, draft)
    if a and a.suspicious(price) and not confirmed:
        # «485 000 for 9 months» is almost surely the monthly price: ask before it reaches the catalog.
        await state.update_data(low_price=price)
        await message.answer(
            ctx.t("sell.price_suspicious", price=fmt_money(price), left=left_text(ctx, draft["end_date"]),
                  month=fmt_money(a.month_cost(price)), fit_month=fmt_money(a.fit_month),
                  recommended=fmt_money(a.recommended)),
            reply_markup=kb([btn(ctx.t("sell.price_low_ok"), SellCb(action="lowok", value=str(price)))],
                            [btn(ctx.t("sell.price_low_no"), SellCb(action="lowno"))]))
        return
    if a and a.too_high(price):
        # Nobody buys it dearer than 1Fit sells a new one in installments.
        await message.answer(ctx.t("err.price_too_high", max=fmt_money(a.max_price), price=fmt_money(a.recommended)),
                             reply_markup=_nav(ctx, "price", _advice_rows(ctx, draft)))
        return
    old = draft.get("price")
    if price != old:
        # New price -> new commission -> the seller must confirm again.
        if data.get("accepted"):
            await message.answer(ctx.t("sell.accept_reset"))
        await state.update_data(accepted=False)
    await draft_update(state, price=price)
    text = ctx.t("sell.price_calc", block=commission_block(ctx, price, _kind(data)))
    if a and a.too_low(price):
        text += "\n\n" + ctx.t("advice.too_low", price=fmt_money(a.recommended))
    await message.answer(text)
    await advance(message, state, ctx, "price")


@router.callback_query(SellForm.price, SellCb.filter(F.action == "lowok"))
async def on_low_price_ok(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    """The seller confirms that the very low price is for the whole term."""
    price = (await state.get_data()).get("low_price")
    if not price or callback_data.value != str(price):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await _set_price(call.message, state, ctx, price, confirmed=True)


@router.callback_query(SellForm.price, SellCb.filter(F.action == "lowno"))
async def on_low_price_no(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await ask(call.message, state, ctx, "price")


@router.callback_query(SellForm.plan, SellCb.filter(F.action == "plan"))
async def on_plan(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    if not callback_data.value.isdigit() or int(callback_data.value) not in TARIFF_PLANS:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    days = int(callback_data.value)
    end = (await state.get_data()).get("draft", {}).get("end_date")
    if end:
        try:
            check_plan_end(date.fromisoformat(end), today_local(), days)
        except ValidationError as e:
            await call.answer(ctx.t(f"err.{e.code}"), show_alert=True)
            return
    await call.answer()
    await call.message.edit_text(prompt(ctx, await state.get_data(), "plan") + "\n\n<b>" + esc(ctx.t(f"app.plan.{days}")) + "</b>")
    await draft_update(state, tariff_days=days)
    await advance(call.message, state, ctx, "plan")


@router.message(SellForm.desc)
async def on_desc(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "desc", parse_description, "description")


@router.callback_query(SellForm.desc, SellCb.filter(F.action == "skip"))
async def on_desc_skip(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, description="")
    await advance(call.message, state, ctx, "desc")


@router.message(SellForm.photo, F.photo)
async def on_photo(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await draft_update(state, screenshot_file_id=message.photo[-1].file_id)
    await advance(message, state, ctx, "photo")


@router.message(SellForm.photo)
async def on_photo_wrong(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("err.bad_photo"), reply_markup=_nav(ctx, "photo", [
        [btn(ctx.t("common.skip"), SellCb(action="skip"))]
    ]))


@router.callback_query(SellForm.photo, SellCb.filter(F.action == "skip"))
async def on_photo_skip(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, screenshot_file_id=None)
    await advance(call.message, state, ctx, "photo")


@router.message(SellForm.name)
async def on_name(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "name", parse_name, "seller_name")


@router.callback_query(SellForm.name, SellCb.filter(F.action == "saved"))
async def on_saved_contact(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    u = ctx.user
    if not (u.get("contact_name") and u.get("contact_phone")):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, seller_name=u["contact_name"], seller_phone=u["contact_phone"])
    await show_preview(call.message, state, ctx)


@router.message(SellForm.phone)
async def on_phone(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not message.contact and await _phone_text_nav(message, state, ctx):
        return
    raw = message.contact.phone_number if message.contact else message.text
    try:
        phone = normalize_phone(raw)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=phone_kb(ctx))
        return
    draft = await draft_update(state, seller_phone=phone)
    await ctx.repo.save_contact(ctx.user_id, draft["seller_name"], phone)
    await message.answer(ctx.t("sell.phone_ok"), reply_markup=main_menu(ctx))
    await advance(message, state, ctx, "phone")


# ------------------------------------------------------------------ preview actions
@router.callback_query(SellForm.preview, SellCb.filter(F.action == "accept"))
async def on_accept(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    accepted = not data.get("accepted")
    await state.update_data(accepted=accepted)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=preview_kb(ctx, await state.get_data()))


@router.callback_query(SellForm.preview, SellCb.filter(F.action == "negotiable"))
async def on_negotiable(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    """Fixed or negotiable price: shown to buyers next to the price, the commission math is unchanged."""
    data = await state.get_data()
    await draft_update(state, price_negotiable=not (data.get("draft") or {}).get("price_negotiable"))
    data = await state.get_data()
    await call.answer()
    await call.message.edit_text(preview_text(ctx, data), reply_markup=preview_kb(ctx, data))


@router.callback_query(SellForm.preview, SellCb.filter(F.action == "edit"))
async def on_edit(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.set_state(SellForm.choose)
    data = await state.get_data()
    promo = _kind(data) == LK.PROMO
    names = [btn(ctx.t(f"sell.field.{f}_promo" if promo and f in ("tariff", "date") else f"sell.field.{f}"),
                 SellCb(action="field", value=f)) for f in _fields(data)]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append([btn(ctx.t("sell.to_preview"), SellCb(action="preview"))])
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("sell.choose_field"), reply_markup=kb(*rows))


@router.callback_query(SellForm.choose, SellCb.filter(F.action == "field"))
async def on_field(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    step = FIELDS.get(callback_data.value)
    if not step or callback_data.value not in _fields(await state.get_data()):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await state.update_data(single=True)
    await ask(call.message, state, ctx, step)


@router.callback_query(SellForm.choose, SellCb.filter(F.action == "preview"))
async def on_to_preview(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await show_preview(call.message, state, ctx)


@router.callback_query(SellForm.preview, SellCb.filter(F.action == "send"))
async def on_send(call: CallbackQuery, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    await apply_auto_price(state, ctx)  # 1Fit prices may have moved since the preview
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    if not data.get("accepted"):
        await call.answer(ctx.t("sell.need_accept"), show_alert=True)
        return
    if draft.get("kind") is None:
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await ask(call.message, state, ctx, "kind")
        return
    if draft["kind"] == LK.MEMBERSHIP and not draft.get("tariff_days"):
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await state.update_data(single=True)
        await ask(call.message, state, ctx, "plan")
        return
    if draft["kind"] == LK.MEMBERSHIP and (draft.get("paid_in_full") is None or (draft["paid_in_full"] is False
                                             and not (draft.get("monthly_payment") and draft.get("payments_left")))):
        # Fully paid or installment is not answered yet (an old listing or an abandoned step).
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await state.update_data(single=True)
        await ask(call.message, state, ctx, "payment")
        return
    a = price_advice(ctx, draft)
    if a and a.too_high(draft["price"]):
        # The end date or the installments changed after the price was set: the price is now above the limit.
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(ctx.t("err.price_too_high", max=fmt_money(a.max_price), price=fmt_money(a.recommended)))
        await state.update_data(single=True)
        await ask(call.message, state, ctx, "price")
        return
    draft["commission_accepted_price"] = draft.get("price")
    try:
        if data.get("mode") == "edit":
            listing_id = data["listing_id"]
            await ctx.repo.update_listing_by_seller(listing_id, ctx.user_id, ctx.lang, draft)
            done_key, admin_key = "sell.resent", "admin.n.edited_listing"
        else:
            listing_id = await ctx.repo.create_listing(ctx.user_id, ctx.lang, draft)
            done_key, admin_key = "sell.sent", "admin.n.new_listing"
    except RepoError as e:
        await call.answer()
        await state.clear()
        key = "sell.edit_blocked" if e.code in ("bad_status", "not_found") else "common.error"
        await call.message.answer(ctx.t(key), reply_markup=main_menu(ctx))
        return

    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t(done_key, id=listing_id), reply_markup=main_menu(ctx))
    await notifier.admins(ctx, lambda a: (
        a.t(admin_key, id=listing_id),
        kb([btn(a.t("admin.open_btn", id=listing_id), AdmCb(action="open", id=listing_id))]),
    ))


# Text typed while a button is expected (kind / city / payment / preview / field menu).
@router.message(SellForm.kind)
@router.message(SellForm.city)
@router.message(SellForm.plan)
@router.message(SellForm.freeze)
@router.message(SellForm.payment)
@router.message(SellForm.preview)
@router.message(SellForm.choose)
async def on_text_instead_of_button(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("common.unknown"))


__all__ = ["router", "start_edit", "LS"]
