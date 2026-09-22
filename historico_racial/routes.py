"""Rotas da ferramenta independente de análise histórico-racial."""

from flask import Blueprint, render_template


historico_racial_bp = Blueprint("historico_racial", __name__)


@historico_racial_bp.get("/historico-racial")
def inicio():
    """Exibe o esqueleto da nova ferramenta, sem processamento analítico."""
    return render_template("historico_racial/index.html")
