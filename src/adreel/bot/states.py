from aiogram.fsm.state import State, StatesGroup


class BrandForm(StatesGroup):
    """Used for first-time onboarding (mode=onboard) and single-field edits (mode=edit)."""

    language = State()
    name = State()
    btype = State()
    logo = State()
    color = State()
    phone = State()
    handle = State()


class NewVideo(StatesGroup):
    template = State()
    clips = State()
    product = State()
    price = State()
    highlight = State()
    voice = State()


class FixCaptions(StatesGroup):
    waiting = State()
