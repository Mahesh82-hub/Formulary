from pydantic import SecretStr

from app.core.config import Settings


def test_database_url_uses_psycopg_and_hides_password() -> None:
    settings = Settings(
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("secret-password"),
    )

    assert settings.database_url.drivername == "postgresql+psycopg"
    assert "secret-password" not in str(settings.database_url)


def test_default_chat_prompt_requires_bioequivalence_intake_and_bounded_claims() -> None:
    settings = Settings(
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("secret-password"),
    )

    assert "prepare_bioequivalence_evidence_request" in settings.chat_system_prompt
    assert "Do not volunteer Cmax, Tmax, AUC" in settings.chat_system_prompt
    assert "question alone is not a request for that workflow" in settings.chat_system_prompt
    assert "ask at most its three returned questions" in settings.chat_system_prompt
    assert "simulator-neutral evidence package" in settings.chat_system_prompt
    assert "Never predict or invent candidate PK values" in settings.chat_system_prompt
    assert "Do not claim bioequivalence from point estimates" in settings.chat_system_prompt
    assert "analyze_bioequivalence_summary" in settings.chat_system_prompt
    assert "ingest_openfda_query" in settings.chat_system_prompt
    assert "Use normal structured FDA API tools before PDF retrieval" in settings.chat_system_prompt
    assert "Never cite a publication" in settings.chat_system_prompt
    assert settings.embedding_model == "BAAI/bge-small-en-v1.5"
    assert settings.embedding_dimensions == 384
    assert settings.embedding_enabled is True


def test_detailed_chat_errors_are_forcibly_disabled_in_production() -> None:
    development = Settings(
        app_env="development",
        chat_expose_error_details=True,
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("secret-password"),
    )
    production = Settings(
        app_env="production",
        chat_expose_error_details=True,
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("secret-password"),
    )

    assert development.chat_error_details_enabled is True
    assert production.chat_error_details_enabled is False
