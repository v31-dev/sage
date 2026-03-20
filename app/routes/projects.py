from services.db import Project
from utils.api import get_crud_router


router = get_crud_router(model=Project, key='name', 
                         create_fields=['env', 'description'], update_fields=['env', 'description'])