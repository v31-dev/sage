from services.db import Application
from utils.api import get_crud_router


router = get_crud_router(model=Application, key=['project', 'name'])