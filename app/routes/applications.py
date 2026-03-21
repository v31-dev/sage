from services.db import Application
from utils.api import get_crud_router


router = get_crud_router(model=Application, key='name', 
                         create_fields=['description', 'env', 'repo', 'env', 'args'], 
                         update_fields=['description', 'env', 'repo'])