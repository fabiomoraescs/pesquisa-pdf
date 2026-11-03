import logging
from logging.config import fileConfig

from flask import current_app

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# A execução de upgrade em testes não pode desabilitar o logger do Flask que
# atende a aplicação já em memória; a migração continua usando sua configuração.
fileConfig(config.config_file_name, disable_existing_loggers=False)
logger = logging.getLogger('alembic.env')


def get_engine():
    try:
        # this works with Flask-SQLAlchemy<3 and Alchemical
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        # this works with Flask-SQLAlchemy>=3
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = dict(current_app.extensions['migrate'].configure_args)
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    connectable = get_engine()

    with connectable.connect() as connection:
        # SQLite cannot change PRAGMA foreign_keys inside a real transaction.
        # Its schema migrations therefore own their short, explicit commit
        # boundaries when a batch rebuild needs that PRAGMA; the migration
        # itself verifies the state before touching the schema.
        if connection.dialect.name == "sqlite":
            conf_args["transactional_ddl"] = False
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args
        )

        if connection.dialect.name == "sqlite":
            # Não abra uma transação externa: migrations SQLite que precisam
            # alternar foreign_keys devem fazê-lo fora dela. Ao fim, este
            # commit persiste também a atualização de alembic_version; em
            # falha, o que ainda for transacional é revertido.
            try:
                context.run_migrations()
                if connection.in_transaction():
                    connection.commit()
            except Exception:
                if connection.in_transaction():
                    connection.rollback()
                raise
        else:
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
