import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status, Body
from playhouse.shortcuts import model_to_dict


logger = logging.getLogger(__name__)


def get_crud_router(model, key, create_fields=None, update_fields=None):
  """
  Generic CRUD router factory.
    
    Args:
    model: Peewee model class
    key: Primary key field name
    create_fields: List of fields allowed for creation
    update_fields: List of fields allowed for updates
  """
  router = APIRouter()
  model_name = model.__name__.lower().capitalize()
  
  if key not in create_fields and create_fields is not None:
    create_fields.append(key)

  def get_instance_by_pk(value):
    """Helper to fetch instance by primary key."""
    return model.select().where(getattr(model, key) == value).first()

  @router.get("")
  def list_all():
    """List all resources."""
    try:
      rows = list(model.select().dicts())
      return rows
    except Exception as e:
      logger.error(f"Error listing {model_name}: {e}")
      raise HTTPException(status_code=500, detail=f"Failed to list {model_name}")


  def extract_data(payload):
    """Convert payload to dict, handling both dict and Pydantic model."""
    if isinstance(payload, dict):
      return payload
    return payload.dict() if hasattr(payload, 'dict') else payload.model_dump()

  @router.post("", status_code=status.HTTP_201_CREATED)
  def create(payload: Any = Body(...)):
    """Create a new resource."""
    try:
      data = extract_data(payload)
      
      # Extract primary key value from payload
      pk_value = data.get(key)
      if not pk_value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
      
      # Check for existing resource
      if get_instance_by_pk(pk_value):
        raise HTTPException(status_code=409, detail=f"{model_name} already exists")
      
      # Filter to allowed fields for creation
      if create_fields:
        data = {field: data[field] for field in create_fields if field in data}
      instance = model.create(**data)
      return model_to_dict(instance)
    except HTTPException:
      raise
    except Exception as e:
      logger.error(f"Error creating {model_name}: {e}")
      raise HTTPException(status_code=500, detail=f"Failed to create {model_name}")


  @router.get("/{pk_value}")
  def get(pk_value: str):
    """Get a specific resource by primary key."""
    try:
      instance = get_instance_by_pk(pk_value)
      if not instance:
        raise HTTPException(status_code=404, detail=f"{model_name} not found")
      return model_to_dict(instance)
    except HTTPException:
      raise
    except Exception as e:
      logger.error(f"Error fetching {model_name}: {e}")
      raise HTTPException(status_code=500, detail=f"Failed to fetch {model_name}")


  @router.put("/{pk_value}")
  def update(pk_value: str, payload: Any = Body(...)):
    """Update a resource."""
    try:
      instance = get_instance_by_pk(pk_value)
      if not instance:
        raise HTTPException(status_code=404, detail=f"{model_name} not found")
      
      # Extract and filter data
      data = extract_data(payload)
      if update_fields:
        data = {field: data[field] for field in update_fields if field in data}
      
      # Apply updates
      for field, value in data.items():
        if value is not None:
          setattr(instance, field, value)
      
      instance.save()
      return model_to_dict(instance)
    except HTTPException:
      raise
    except Exception as e:
      logger.error(f"Error updating {model_name}: {e}")
      raise HTTPException(status_code=500, detail=f"Failed to update {model_name}")


  @router.delete("/{pk_value}", status_code=status.HTTP_204_NO_CONTENT)
  def delete(pk_value: str):
    """Delete a resource."""
    try:
      instance = get_instance_by_pk(pk_value)
      if not instance:
        raise HTTPException(status_code=404, detail=f"{model_name} not found")
      
      instance.delete_instance()
    except HTTPException:
      raise
    except Exception as e:
      logger.error(f"Error deleting {model_name}: {e}")
      raise HTTPException(status_code=500, detail=f"Failed to delete {model_name}")
    
  return router
