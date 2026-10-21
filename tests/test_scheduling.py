import pytest

from triagem.scheduling import classification_queue_priority


def test_common_ticket_keeps_default_queue_priority() -> None:
    assert classification_queue_priority(
        subject="Prazo de entrega",
        message="Quero saber quando meu pedido chegará.",
    ) == 0


def test_strong_urgency_signal_elevates_queue_priority() -> None:
    assert classification_queue_priority(
        subject="Compras estranhas",
        message="Minha conta foi invadida e não reconheço essas compras.",
    ) == 100


@pytest.mark.parametrize(
    ("subject", "message"),
    [
        ("FRAUDE", "Preciso de ajuda."),
        ("Invasão de conta", "Não consigo mais entrar."),
        ("Segurança", "Meu cartão foi CLONADO."),
        ("Cobrança não reconhecida", "Não fui eu quem fez essa compra."),
        ("Cobrança duplicada", "O valor apareceu duas vezes."),
        ("Conta", "Minha conta está hackeada."),
        ("Segurança comprometida", "Troquem minhas credenciais."),
        ("Ajuda", "É uma emergência."),
    ],
)
def test_scheduler_handles_case_accents_and_both_ticket_fields(
    subject: str,
    message: str,
) -> None:
    assert classification_queue_priority(subject=subject, message=message) == 100


def test_scheduler_returns_only_queue_order_not_a_classification() -> None:
    result = classification_queue_priority(
        subject="Urgente",
        message="Não reconheço esta cobrança.",
    )

    assert result == 100
    assert isinstance(result, int)
