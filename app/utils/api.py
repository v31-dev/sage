import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status, Body
from playhouse.shortcuts import model_to_dict


logger = logging.getLogger(__name__)


def get_crud_router(model, key: list[str], create_fields: list[str] = None, update_fields: list[str] = None):
  """
  Generic CRUD router factory.
    
    Args:
    model: Peewee model class
    key: Primary key field name(s)
    create_fields: List of fields allowed for creation
    update_fields: List of fields allowed for updates
  """
  router = APIRouter()
  model_name = model.__name__.lower().capitalize()
  model_fields = model.get_local_fields()

  if isinstance(key, str):
    key = [key]

  # Ensure key field is included in creation
  if create_fields is None:
    create_fields = model_fields.copy()

  for k in key:
    if k not in create_fields:  
      create_fields.append(k)

  # Don't allow key field to be updated
  if update_fields is None:
    update_fields = model_fields.copy()

  for k in key:
    if k in update_fields:
      update_fields.remove(k)

  def get_instance_by_pk(value):
    """Helper to fetch instance by primary key."""
    query = model.select()
    for k in key:
      query = query.where(getattr(model, k) == value[k])
    return query.first()
  
  # Composite keys will be represented as CSV
  def url_param_to_pk_value(param: str): 
    values = param.split(",")
    if len(values) != len(key):
      raise HTTPException(status_code=400, detail=f"Invalid {model_name} key")
    return {k: v for k, v in zip(key, values)}

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
      pk_value = {}
      for k in key:
        pk_value[k] = data.get(k)
      if not all(pk_value.values()):
        raise HTTPException(status_code=400, detail=f"{list(key)} is required")
      
      # Check for existing resource
      if get_instance_by_pk(pk_value):
        raise HTTPException(status_code=409, detail=f"{model_name} already exists")
      
      # Filter to allowed fields for creation
      filtered_data = {}
      for field in create_fields:
        if field in data:
          filtered_data[field] = data[field]
      
      logger.info(f"Filtered data for {model_name} create: {filtered_data}")
      
      # Ensure all key fields are present
      for k in key:
        if k not in filtered_data:
          filtered_data[k] = data.get(k)
      
      instance = model.create(**filtered_data)
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
      pk_value = url_param_to_pk_value(pk_value)
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
      pk_value = url_param_to_pk_value(pk_value)
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
      pk_value = url_param_to_pk_value(pk_value)
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
