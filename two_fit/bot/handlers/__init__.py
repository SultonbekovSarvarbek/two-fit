from aiogram import Router

from . import admin, buy, catalog, common, my, sell


def build_router() -> Router:
    root = Router(name="root")
    # Order matters: main-menu buttons work from any step, the fallback goes last.
    children = (common.menu_router, admin.router, catalog.router, buy.router, sell.router, my.router,
                common.fallback_router)
    for child in children:
        # Routers are module-level; detach them so a dispatcher can be rebuilt in the same process
        # (tests simulate a bot restart this way).
        child._parent_router = None
        root.include_router(child)
    return root
