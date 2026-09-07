from functools import wraps
from flask import redirect, url_for, flash
from flask_login import current_user
from services.rbac import user_has_role

def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not user_has_role(current_user, *roles):
                # Redirect to proper dashboard based on role
                if user_has_role(current_user, "HR"):
                    return redirect(url_for("hr.dashboard_hr"))
                elif user_has_role(current_user, "SuperAdmin"):
                    return redirect(url_for("admin.dashboard"))  # <- actual Admin blueprint endpoint
                else:
                    return redirect(url_for("front_page"))  # fallback
            return f(*args, **kwargs)
        return decorated_function
    return decorator

