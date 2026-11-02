"""Provisionamento explícito; nunca grava credenciais no código."""

import click
from flask import Flask
from sqlalchemy import select

from .extensions import db
from .models import User
from .password_policy import TEMPORARY_PASSWORD
from .services import record_audit, seed_platform


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-platform")
    def seed_command() -> None:
        seed_platform()
        click.echo("Planos, ferramentas e biblioteca oficial conferidos.")

    @app.cli.command("create-admin")
    @click.option("--name", prompt="Nome")
    @click.option("--email", prompt="E-mail")
    @click.password_option(confirmation_prompt=True)
    def create_admin(name: str, email: str, password: str) -> None:
        email = email.strip().casefold()
        if len(password) < 12:
            raise click.ClickException("A senha precisa ter pelo menos 12 caracteres.")
        if password == TEMPORARY_PASSWORD:
            raise click.ClickException("Escolha uma senha diferente da senha temporária administrativa.")
        if db.session.scalar(select(User.id).where(User.email == email)):
            raise click.ClickException("E-mail já cadastrado.")
        seed_platform()
        user = User(name=name.strip(), email=email, role="admin", status="active")
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        record_audit(user, "admin_created", "user", user.id, after={"role": "admin", "status": "active"})
        db.session.commit()
        click.echo("Administrador criado. A senha não foi exibida nem gravada em arquivo.")

    @app.cli.command("instagram-meta-sync-worker")
    @click.option("--once", is_flag=True, help="Processa no máximo um job pendente e encerra.")
    @click.option("--poll-interval", default=5.0, show_default=True, type=float,
                  help="Segundos de espera quando não houver job pendente.")
    @click.option("--stale-seconds", default=7200, show_default=True, type=int,
                  help="Prazo conservador para recuperar job running órfão.")
    def instagram_meta_sync_worker(once: bool, poll_interval: float, stale_seconds: int) -> None:
        """Executa a fila durável da sincronização Meta em processo separado."""
        from .instagram_meta_sync_jobs import run_meta_sync_worker

        run_meta_sync_worker(
            app,
            once=once,
            poll_interval=poll_interval,
            stale_seconds=stale_seconds,
        )

    @app.cli.command("ojs-seo-worker")
    @click.option("--once", is_flag=True, help="Processa no máximo uma auditoria pendente e encerra.")
    @click.option("--audit-run-id", default=None, help="Processa somente a auditoria pendente indicada.")
    @click.option("--max-jobs", default=None, type=int,
                  help="Máximo de auditorias antes de encerrar; omita para manter o worker em espera.")
    @click.option("--poll-interval", default=5.0, show_default=True, type=float,
                  help="Segundos de espera quando não houver auditoria pendente.")
    @click.option("--lease-seconds", default=300, show_default=True, type=int,
                  help="Prazo de posse durável antes de uma execução ser considerada interrompida.")
    def ojs_seo_worker(
        once: bool,
        audit_run_id: str | None,
        max_jobs: int | None,
        poll_interval: float,
        lease_seconds: int,
    ) -> None:
        """Executa auditorias OJS SEO em processo separado e sem paralelismo interno."""
        from .ojs_seo_executor import run_ojs_seo_worker

        try:
            run_ojs_seo_worker(
                app,
                once=once,
                audit_run_id=audit_run_id,
                max_jobs=max_jobs,
                poll_interval=poll_interval,
                lease_seconds=lease_seconds,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    @app.cli.command("ojs-seo-html-worker")
    @click.option("--once", is_flag=True, help="Processa no máximo uma análise HTML pendente e encerra.")
    @click.option("--audit-run-id", default=None, help="Processa somente a análise técnica indicada.")
    @click.option("--max-jobs", default=None, type=int,
                  help="Máximo de análises antes de encerrar; omita para manter o worker em espera.")
    @click.option("--until-idle", is_flag=True,
                  help="Processa lotes e campanhas pendentes até a fila HTML ficar vazia e encerra.")
    @click.option("--poll-interval", default=5.0, show_default=True, type=float,
                  help="Segundos de espera quando não houver análise pendente.")
    @click.option("--lease-seconds", default=300, show_default=True, type=int,
                  help="Prazo de posse durável da análise técnica.")
    def ojs_seo_html_worker(
        once: bool,
        audit_run_id: str | None,
        max_jobs: int | None,
        until_idle: bool,
        poll_interval: float,
        lease_seconds: int,
    ) -> None:
        """Executa análises HTML OJS em processo separado, sem HTTP na web."""
        from .ojs_seo_html_executor import run_ojs_html_worker

        if until_idle and (once or audit_run_id):
            raise click.ClickException("--until-idle não pode ser combinado com --once ou --audit-run-id.")
        try:
            run_ojs_html_worker(
                app,
                once=once,
                audit_run_id=audit_run_id,
                max_jobs=max_jobs,
                until_idle=until_idle,
                poll_interval=poll_interval,
                lease_seconds=lease_seconds,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    def _development_confirmation(confirmed: bool) -> None:
        if not confirmed:
            raise click.ClickException(
                "Informe --confirm-local-development para executar uma operação OJS SEO local."
            )

    def _provisioning_confirmation(
        confirm_local_development: bool, confirm_production_provisioning: bool
    ) -> bool:
        """Exige uma única confirmação de escopo para publicar OJS SEO."""
        if confirm_local_development == confirm_production_provisioning:
            raise click.ClickException(
                "Informe exatamente uma confirmação: --confirm-local-development "
                "ou --confirm-production-provisioning."
            )
        return confirm_production_provisioning

    def _development_admin(admin_user_id: str):
        admin = db.session.get(User, admin_user_id)
        if admin is None:
            raise click.ClickException("O administrador informado não foi encontrado.")
        return admin

    @app.cli.command("ojs-seo-provision")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    @click.option("--confirm-production-provisioning", is_flag=True,
                  help="Confirma o provisionamento controlado de produção.")
    def ojs_seo_provision(
        admin_user_id: str, confirm_local_development: bool, confirm_production_provisioning: bool
    ) -> None:
        """Cria a Tool OJS SEO inativa e seus vínculos comerciais idempotentes."""
        from .ojs_seo_activation import OjsSeoActivationError, provision_ojs_seo_tool

        allow_explicit_production = _provisioning_confirmation(
            confirm_local_development, confirm_production_provisioning
        )
        try:
            result = provision_ojs_seo_tool(
                _development_admin(admin_user_id),
                allow_explicit_production=allow_explicit_production,
            )
            db.session.commit()
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"OJS SEO provisionado: tool_created={result.tool_created}; "
            f"plan_links_created={len(result.plan_links_created)}; active={result.tool_active}."
        )

    @app.cli.command("ojs-seo-prepare-development")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--test-user-id", required=True, help="UUID da conta de teste dedicada.")
    @click.option("--workspace-name", required=True, help="Nome identificável da área de desenvolvimento.")
    @click.option("--plan-id", required=True, type=click.Choice(["marketing_sites", "marketing_complete", "integral"]))
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    def ojs_seo_prepare_development(
        admin_user_id: str,
        test_user_id: str,
        workspace_name: str,
        plan_id: str,
        confirm_local_development: bool,
    ) -> None:
        """Cria uma assinatura de desenvolvimento OJS para uma conta indicada."""
        from .ojs_seo_activation import OjsSeoActivationError, prepare_ojs_seo_development_environment

        _development_confirmation(confirm_local_development)
        try:
            result = prepare_ojs_seo_development_environment(
                _development_admin(admin_user_id),
                test_user_id=test_user_id,
                workspace_label=workspace_name,
                plan_id=plan_id,
            )
            db.session.commit()
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Ambiente OJS SEO preparado: workspace={result.workspace_id}; "
            f"subscription={result.subscription_id}; ojs_journal_limit={result.ojs_journal_limit}."
        )

    @app.cli.command("ojs-seo-activate")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    @click.option("--confirm-production-provisioning", is_flag=True,
                  help="Confirma o provisionamento controlado de produção.")
    def ojs_seo_activate(
        admin_user_id: str, confirm_local_development: bool, confirm_production_provisioning: bool
    ) -> None:
        """Ativa somente a Tool OJS SEO já provisionada e migrada."""
        from .ojs_seo_activation import OjsSeoActivationError, activate_ojs_seo_tool

        allow_explicit_production = _provisioning_confirmation(
            confirm_local_development, confirm_production_provisioning
        )
        try:
            changed = activate_ojs_seo_tool(
                _development_admin(admin_user_id),
                allow_explicit_production=allow_explicit_production,
            )
            db.session.commit()
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo("OJS SEO ativado." if changed else "OJS SEO já estava ativo; nenhuma alteração foi feita.")

    @app.cli.command("ojs-seo-deactivate")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    def ojs_seo_deactivate(admin_user_id: str, confirm_local_development: bool) -> None:
        """Desativa a Tool OJS SEO preservando todos os registros relacionados."""
        from .ojs_seo_activation import OjsSeoActivationError, deactivate_ojs_seo_tool

        _development_confirmation(confirm_local_development)
        try:
            changed = deactivate_ojs_seo_tool(_development_admin(admin_user_id))
            db.session.commit()
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo("OJS SEO desativado." if changed else "OJS SEO já estava inativo; nenhuma alteração foi feita.")

    @app.cli.command("ojs-seo-set-visibility")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--scope", required=True, type=click.Choice(["group", "tool"]), help="Altera apenas Analytics ou apenas OJS SEO.")
    @click.option("--visible/--hidden", default=None, help="Estado de visibilidade explícito.")
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    def ojs_seo_set_visibility(
        admin_user_id: str,
        scope: str,
        visible: bool | None,
        confirm_local_development: bool,
    ) -> None:
        """Altera uma camada de visibilidade sem ativar a ferramenta."""
        from .ojs_seo_activation import OjsSeoActivationError, set_ojs_seo_navigation_visibility

        _development_confirmation(confirm_local_development)
        if visible is None:
            raise click.ClickException("Informe --visible ou --hidden.")
        try:
            changed = set_ojs_seo_navigation_visibility(
                _development_admin(admin_user_id), scope_type=scope, visible=visible
            )
            db.session.commit()
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo("Visibilidade atualizada." if changed else "A visibilidade já possuía esse estado.")

    @app.cli.command("ojs-seo-verify-development-access")
    @click.option("--admin-user-id", required=True, help="UUID do administrador responsável.")
    @click.option("--test-user-id", required=True, help="UUID da conta de teste dedicada.")
    @click.option("--confirm-local-development", is_flag=True, help="Confirma o ambiente local de desenvolvimento.")
    def ojs_seo_verify_development_access(
        admin_user_id: str, test_user_id: str, confirm_local_development: bool
    ) -> None:
        """Confere as camadas de acesso da conta de teste sem iniciar coletas."""
        from .ojs_seo_activation import OjsSeoActivationError, verify_ojs_seo_development_access

        _development_confirmation(confirm_local_development)
        try:
            result = verify_ojs_seo_development_access(
                _development_admin(admin_user_id), test_user_id=test_user_id
            )
        except OjsSeoActivationError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"OJS SEO access: tool_provisioned={result.tool_provisioned}; "
            f"tool_active={result.tool_active}; operational_access={result.operational_access}; "
            f"eligible_subscriptions={len(result.eligible_subscription_ids)}."
        )
