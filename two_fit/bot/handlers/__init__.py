from aiogram import Router

from . import admin, catalog, common, my, pay, prices, sell


def build_router() -> Router:
    root = Router(name="root")
    # Order matters: main-menu buttons work from any step, the fallback goes last.
    # pay goes early: a payment arrives as a message whatever step the seller is on.
    children = (common.menu_router, pay.router, admin.router, prices.router, catalog.router, sell.router, my.router,
                common.fallback_router)
    for child in children:
        # Routers are module-level; detach them so a dispatcher can be rebuilt in the same process
        # (tests simulate a bot restart this way).
        child._parent_router = None
        root.include_router(child)
    return root
