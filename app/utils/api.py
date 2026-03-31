import logging

from fastapi import HTTPException
from playhouse.shortcuts import model_to_dict


logger = logging.getLogger(__name__)

def generic_list(model, query_condition=None):
  """Generic LIST handler - returns all instances."""
  try:
    if query_condition:
      rows = list(model.select().where(query_condition).dicts())
    else:
      rows = list(model.select().dicts())
    return rows
  except Exception as e:
    logger.error(f"Error listing {model.__name__}: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to list {model.__name__} with error: {e}")

def generic_create(model, data: dict):
  """Generic CREATE handler - insert new instance."""
  try:
    instance = model.create(**data)
    return model_to_dict(instance)
  except Exception as e:
    logger.error(f"Error creating {model.__name__}: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to create {model.__name__} with error: {e}")

def generic_get(model, query_condition, return_model=False):
  """Generic GET handler - fetch single instance by query condition.
  
  Args:
    model: Peewee model class
    query_condition: Peewee query expression
  """
  try:
    instance = model.get_or_none(query_condition)
    if not instance:
      raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    if return_model:
      return instance
    else:
      return model_to_dict(instance)
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error fetching {model.__name__}: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to fetch {model.__name__} with error: {e}")

def generic_update(model, instance, data: dict):
  """Generic UPDATE handler - update instance by query condition.
  
  Args:
    model: Peewee model class
    instance: Peewee model instance
    data: Dictionary of fields to update
  """
  try:
    if not instance:
      raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    
    for field, value in data.items():
      if value is not None:
        setattr(instance, field, value)
    
    instance.save()
    return model_to_dict(instance)
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error updating {model.__name__}: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to update {model.__name__} with error: {e}")

def generic_delete(model, instance):
  """Generic DELETE handler - delete instance by query condition.
  
  Args:
    model: Peewee model class
    instance: Peewee model instance
  """
  try:
    if not instance:
      raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    
    instance.delete_instance()
    return {"status": "deleted"}
  except HTTPException:
    raise
  except Exception as e:
    logger.error(f"Error deleting {model.__name__}: {e}")
    raise HTTPException(status_code=500, detail=f"Failed to delete {model.__name__} with error: {e}")
  
def parse_api_data(data, keys: list[str]):
  """Helper to parse API data with optional keys."""
  return {key: data.get(key).strip() if isinstance(data.get(key), str) else data.get(key) for key in keys if key in data}