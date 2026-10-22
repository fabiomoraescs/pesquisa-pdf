"""Servidor descartável apenas para revisão visual local do dashboard."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from platform_core.extensions import db
from platform_core.instagram_taxonomy import classify_media, create_initial_taxonomy, taxonomy_dimensions
from platform_core.models import (
    AnalyticsProject, AnalyticsRun, InstagramAccount, InstagramAccountSnapshot,
    InstagramMedia, InstagramMediaObservation, PlanTool, User,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL, replace_grant, seed_platform


with TemporaryDirectory(prefix="instagram-dashboard-visual-") as temp:
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(Path(temp) / 'visual.sqlite3').as_posix()}",
        "PLATFORM_DATA_DIR": temp,
        "SECRET_KEY": "visual-only-local-session",
        "PRESENTATION_TIMEZONE": "America/Maceio",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
    })
    with app.app_context():
        db.create_all()
        seed_platform()
        user = User(name="Revisão visual", email="visual@example.org", role="user", status="active")
        user.set_password("senha-de-teste-segura-123")
        db.session.add(user)
        db.session.flush()
        replace_grant(user, "research_quali", "admin_courtesy", "active")
        db.session.add(PlanTool(plan_id="research_quali", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        project = AnalyticsProject(owner_user_id=user.id, name="Projeto visual Instagram")
        db.session.add(project)
        db.session.flush()
        account = InstagramAccount(analytics_project_id=project.id, username="@revista_exemplo",
                                   username_normalized="revista_exemplo")
        db.session.add(account)
        db.session.flush()
        first_at = datetime(2026, 10, 2, 13, tzinfo=timezone.utc)
        second_at = datetime(2026, 10, 9, 13, tzinfo=timezone.utc)
        archived_project = AnalyticsProject(
            owner_user_id=user.id, name="Projeto arquivado para revisão visual",
            status="archived", archived_at=second_at,
        )
        db.session.add(archived_project)
        db.session.flush()
        old = AnalyticsRun(analytics_project_id=project.id, status="completed",
                           started_at=first_at, completed_at=first_at)
        new = AnalyticsRun(analytics_project_id=project.id, status="completed",
                           started_at=second_at, completed_at=second_at)
        db.session.add_all((old, new))
        db.session.flush()
        media_items = []
        for index, day in enumerate((3, 6, 10, 12, 15, 18, 21, 24, 27, 30), start=1):
            media = InstagramMedia(
                instagram_account_id=account.id, external_id=f"post_{index:03d}",
                published_at=datetime(2026, 9, day, 21, 30, tzinfo=timezone.utc),
                media_type_raw="REEL" if index % 2 else "IMAGE",
                caption_original=f"Publicação editorial número {index} sobre pesquisa e comunicação.",
                permalink=f"https://www.instagram.com/p/post_{index:03d}/",
            )
            db.session.add(media)
            db.session.flush()
            media_items.append(media)
            db.session.add_all((
                InstagramMediaObservation(instagram_media_id=media.id, analytics_run_id=old.id,
                                          observed_at=first_at, reach=1000 * index,
                                          impressions=1200 * index, plays=500 * index if index % 2 else None,
                                          likes=50 * index, comments_count=index, shares=index,
                                          saves=index, interactions=53 * index),
                InstagramMediaObservation(instagram_media_id=media.id, analytics_run_id=new.id,
                                          observed_at=second_at, reach=1500 * index,
                                          impressions=1700 * index, plays=700 * index if index % 2 else None,
                                          likes=70 * index, comments_count=2 * index, shares=index,
                                          saves=index, interactions=74 * index),
            ))
        db.session.add_all((
            InstagramAccountSnapshot(instagram_account_id=account.id, analytics_run_id=old.id,
                                     observed_at=first_at, followers_count=8450),
            InstagramAccountSnapshot(instagram_account_id=account.id, analytics_run_id=new.id,
                                     observed_at=second_at, followers_count=8675),
        ))
        create_initial_taxonomy(project)
        dimensions = {dimension.key: dimension for dimension in taxonomy_dimensions(project.id, active_only=True)}
        objective = dimensions["communicational-objective"]
        editorial = dimensions["editorial-type"]
        cta = dimensions["call-to-action"]
        for index, media in enumerate(media_items, start=1):
            classify_media(project, media.id, {
                objective.id: (objective.categories[index % len(objective.categories)].id,),
                editorial.id: (editorial.categories[index % len(editorial.categories)].id,),
                cta.id: (cta.categories[index % len(cta.categories)].id,) if index % 2 else (),
            }, user)
        db.session.commit()
        print(f"VISUAL_PROJECT_ID={project.id}", flush=True)
        print(f"VISUAL_ARCHIVED_PROJECT_ID={archived_project.id}", flush=True)
    app.run(host="127.0.0.1", port=5006, debug=False, use_reloader=False)
