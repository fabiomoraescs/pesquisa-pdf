"""Os mesmos editores atendem contextos oficiais e privados, definidos pela rota."""
from functools import wraps

from flask import Blueprint, abort, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from .extensions import db
from .models import VocabularyLibrary
from .services import can_use_tool

user_libraries_bp = Blueprint("user_libraries", __name__, url_prefix="/busca-estruturada/bibliotecas")


def private_library_flow():
    return request.blueprint == "user_libraries"


def library_editor(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if private_library_flow():
            if not can_use_tool(current_user, "document_analysis"):
                abort(403)
            if "library_id" in kwargs:
                library = db.session.get(VocabularyLibrary, kwargs["library_id"])
                if library is None or library.owner_user_id != current_user.id:
                    abort(404)
        elif current_user.role != "admin" or not current_user.is_active:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def library_url(endpoint, **values):
    if private_library_flow() and endpoint == "libraries":
        return url_for("projects.new_project")
    return url_for(("user_libraries." if private_library_flow() else "admin.") + endpoint, **values)


def available_libraries(user):
    return (VocabularyLibrary.active.is_(True), VocabularyLibrary.status == "published",
            or_(VocabularyLibrary.owner_user_id.is_(None), VocabularyLibrary.owner_user_id == user.id))


@user_libraries_bp.app_context_processor
def library_template_context():
    return {"library_url": library_url, "private_library_flow": private_library_flow()}
