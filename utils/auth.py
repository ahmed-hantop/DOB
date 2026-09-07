from functools import wraps
from flask import abort
from flask_login import current_user
from services.rbac import user_has_role


def require_roles(*roles):
    """Decorator to enforce role-based access for views."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if roles and not user_has_role(current_user, *roles):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_roles_or_owner(*roles, owner_loader=None, owner_attr="id"):
    """Allow access if user has one of roles OR owns the resource provided by owner_loader."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if roles and user_has_role(current_user, *roles):
                return fn(*args, **kwargs)
            if owner_loader:
                resource = owner_loader(*args, **kwargs)
                if resource and getattr(resource, owner_attr, None) == current_user.id:
                    return fn(*args, **kwargs)
            abort(403)
        return wrapper
    return decorator
