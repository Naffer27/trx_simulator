# simulator/support_knowledge/catalogue.py
"""
CUSTOMER-SUPPORT-01D — deterministic, code-backed knowledge catalogue.

Source of truth: docs/CUSTOMER_SUPPORT_KNOWLEDGE_BASE_SPEC_V1.md. Every
GREEN item's approved_answer text below is taken directly from that
document's approved wording (categories 4-11), narrowed to CONFIRMED
items only. YELLOW/RED items reuse the document's own safe/security
wording verbatim rather than each carrying a bespoke message.

NOT a Django model. Pure Python, version-controlled, deterministic, no
migration. Immutable dataclass instances — this module has no side
effects and performs no I/O.

action taxonomy is intentionally narrower here than the source
document's full ANSWER/LOOKUP/SUPPORT/OPS/OWNER set — 01D implements
only:
  ANSWER  -> GREEN. Return approved_answer instantly, no ticket needed.
  SUPPORT -> YELLOW. Never fabricate account/transaction status — show
             the safe wording and offer human handoff instead. This is
             also where the source document's LOOKUP action lands,
             since 01D deliberately does not implement account-data
             lookup (out of scope per the 01D authorization, §8).
  OPS     -> RED. Security-specific wording, human handoff — 01D never
             auto-escalates to Ops itself (that remains the existing
             01B human "escalate" control); this action only marks the
             item as security-sensitive for correct widget styling and
             for a future block to route more deliberately.

enabled=False marks a POLICY_PENDING item — its business policy and/or
code enforcement has not been reconciled yet, so it must not produce an
automatic customer-facing answer. Disabled items are never returned by
service.py's query functions — not filtered out at the view layer, but
structurally absent from every query result.

WITHDRAWAL-POLICY-CORRECTION-02 (resolved): withdrawal_minimum was
disabled from 01D's original authorization until code enforcement (the
$20 floor in WithdrawForm + the authoritative backend gate in
withdraw_otp_verify_view._finalize()) was reconciled with the confirmed
USD 20 policy. That correction has landed — this item is now enabled
below, stating only the confirmed policy figures (minimum, the $20-
$1,000 normal-flow band, and the >$1,000 review band), with no invented
processing times, fees, or provider guarantees.
"""
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class KnowledgeAction(str, Enum):
    ANSWER = "ANSWER"
    SUPPORT = "SUPPORT"
    OPS = "OPS"


class KnowledgeCategory(str, Enum):
    WITHDRAWALS = "withdrawals"
    DEPOSITS = "deposits"
    KYC = "kyc"
    TRADING = "trading"
    ACCOUNT = "account"
    SECURITY = "security"
    TECHNICAL = "technical"


CATEGORY_LABELS = {
    KnowledgeCategory.WITHDRAWALS: "Retiros",
    KnowledgeCategory.DEPOSITS: "Depósitos",
    KnowledgeCategory.KYC: "KYC",
    KnowledgeCategory.TRADING: "Trading",
    KnowledgeCategory.ACCOUNT: "Cuenta",
    KnowledgeCategory.SECURITY: "Seguridad",
    KnowledgeCategory.TECHNICAL: "Soporte técnico",
}

# Where the human-handoff flow's new-ticket form lands, per category —
# mirrors the spec document's own Appendix mapping. SECURITY has no
# dedicated SupportTicket category yet (spec §14 — a known, separate
# gap), so it maps to the same "account_issue" bucket the spec itself
# uses for lack of a better fit.
TICKET_CATEGORY_MAP = {
    KnowledgeCategory.WITHDRAWALS: "withdrawal_issue",
    KnowledgeCategory.DEPOSITS: "deposit_issue",
    KnowledgeCategory.KYC: "kyc_issue",
    KnowledgeCategory.TRADING: "trading_issue",
    KnowledgeCategory.ACCOUNT: "account_issue",
    KnowledgeCategory.SECURITY: "account_issue",
    KnowledgeCategory.TECHNICAL: "bug",
}


@dataclass(frozen=True)
class KnowledgeItem:
    intent: str
    category: KnowledgeCategory
    question: str
    answer: str
    risk_level: RiskLevel
    action: KnowledgeAction
    enabled: bool = True
    requires_account_data: bool = False


_YELLOW_SAFE_MESSAGE = (
    "Este caso requiere revisar información específica de tu cuenta. "
    "Un miembro de nuestro equipo de soporte puede ayudarte con esto."
)

_RED_SECURITY_MESSAGE = (
    "Este es un caso de seguridad. Por tu protección, conéctate directamente "
    "con nuestro equipo de soporte — no intentes resolverlo por tu cuenta."
)


def _answer_item(intent, category, question, answer, *, requires_account_data=False):
    return KnowledgeItem(
        intent=intent, category=category, question=question, answer=answer,
        risk_level=RiskLevel.GREEN, action=KnowledgeAction.ANSWER,
        enabled=True, requires_account_data=requires_account_data,
    )


def _support_item(intent, category, question, *, requires_account_data=True):
    return KnowledgeItem(
        intent=intent, category=category, question=question, answer=_YELLOW_SAFE_MESSAGE,
        risk_level=RiskLevel.YELLOW, action=KnowledgeAction.SUPPORT,
        enabled=True, requires_account_data=requires_account_data,
    )


def _ops_item(intent, category, question, *, requires_account_data=True):
    return KnowledgeItem(
        intent=intent, category=category, question=question, answer=_RED_SECURITY_MESSAGE,
        risk_level=RiskLevel.RED, action=KnowledgeAction.OPS,
        enabled=True, requires_account_data=requires_account_data,
    )


def _pending_item(intent, category, question):
    """POLICY_PENDING placeholder. enabled=False — kept in the
    catalogue (so the gap is visible and testable) but never returned
    by any service.py query, so it can never produce an automatic
    customer-facing answer."""
    return KnowledgeItem(
        intent=intent, category=category, question=question, answer="",
        risk_level=RiskLevel.YELLOW, action=KnowledgeAction.SUPPORT,
        enabled=False, requires_account_data=False,
    )


CATALOGUE: tuple = (
    # ── Withdrawals (spec §4) ────────────────────────────────────────
    _answer_item(
        "withdrawal_how_to", KnowledgeCategory.WITHDRAWALS, "¿Cómo retiro fondos?",
        "Ve a Retirar, selecciona el activo/red, elige una wallet verificada, confirma con 2FA "
        "y luego verifica el código que te enviamos por email. Los fondos se mueven solo después "
        "de completar ambos pasos.",
    ),
    # WITHDRAWAL-POLICY-CORRECTION-02 — enabled after code enforcement
    # (WithdrawForm min_value + the authoritative _finalize() gate) was
    # reconciled with the confirmed USD 20 policy. States only confirmed
    # figures — no processing times, fees, or provider guarantees.
    _answer_item(
        "withdrawal_minimum", KnowledgeCategory.WITHDRAWALS, "¿Cuál es el monto mínimo de retiro?",
        "El monto mínimo de retiro es USD 20. Montos entre USD 20 y USD 1,000 siguen el flujo "
        "de seguridad automatizado normal. Montos mayores a USD 1,000 requieren una revisión "
        "interna adicional antes de procesarse.",
    ),
    _answer_item(
        "withdrawal_kyc_required", KnowledgeCategory.WITHDRAWALS, "¿Necesito KYC para retirar?",
        "Sí. La verificación KYC debe estar aprobada antes de solicitar cualquier retiro.",
    ),
    _answer_item(
        "withdrawal_processing_time", KnowledgeCategory.WITHDRAWALS, "¿Cuánto tarda un retiro?",
        "Los retiros de hasta USD 1,000 se procesan automáticamente tras la verificación. "
        "Montos mayores requieren un paso adicional de revisión interna antes de procesarse.",
    ),
    _support_item("withdrawal_processing_status", KnowledgeCategory.WITHDRAWALS, "¿Cuál es el estado de mi retiro?"),
    _support_item("withdrawal_not_received", KnowledgeCategory.WITHDRAWALS, "No he recibido mi retiro"),
    _answer_item(
        "withdraw_all", KnowledgeCategory.WITHDRAWALS, "¿Puedo retirar todo mi saldo?",
        "Sí — usa la opción \"Retirar todo\" y se solicitará el saldo disponible completo de tu wallet.",
    ),
    _answer_item(
        "withdrawable_balance_difference", KnowledgeCategory.WITHDRAWALS,
        "¿Por qué mi saldo retirable es diferente a mi equity de trading?",
        "Tu equity de trading (balance + P&L flotante) no es lo mismo que el saldo de tu wallet. "
        "Solo los fondos ya transferidos a tu Wallet son retirables.",
    ),
    _answer_item(
        "withdrawal_2fa_required", KnowledgeCategory.WITHDRAWALS, "¿Por qué me pide 2FA para retirar?",
        "La autenticación de dos factores (2FA) es obligatoria en cada solicitud de retiro para proteger tus fondos.",
    ),
    _ops_item("lost_2fa_withdrawal", KnowledgeCategory.WITHDRAWALS, "Perdí acceso a mi autenticador (2FA)"),
    _support_item("withdrawal_email_otp_missing", KnowledgeCategory.WITHDRAWALS, "No recibí el código por email"),
    _answer_item(
        "withdrawal_otp_expired", KnowledgeCategory.WITHDRAWALS, "Mi código de verificación expiró",
        "Los códigos por email expiran después de un tiempo corto por tu seguridad — solicita uno nuevo "
        "y el anterior se invalida automáticamente.",
    ),
    _answer_item(
        "withdrawal_old_otp_invalid", KnowledgeCategory.WITHDRAWALS, "¿Por qué mi código anterior ya no funciona?",
        "Solo el código más reciente es válido; solicitar uno nuevo invalida automáticamente el anterior.",
    ),
    _answer_item(
        "withdrawal_wallet_requirement", KnowledgeCategory.WITHDRAWALS,
        "¿Por qué debo registrar una wallet antes de retirar?",
        "Por seguridad, los retiros solo pueden enviarse a una dirección de wallet previamente verificada — "
        "esto evita que los fondos se envíen a una dirección que un atacante acabe de agregar.",
    ),
    _answer_item(
        "withdrawal_wallet_cooldown", KnowledgeCategory.WITHDRAWALS, "¿Por qué no puedo usar mi wallet nueva de inmediato?",
        "Una wallet recién agregada o modificada tiene un breve período de seguridad antes de poder recibir un retiro.",
    ),
    _answer_item(
        "withdrawal_change_wallet", KnowledgeCategory.WITHDRAWALS, "¿Cómo cambio mi wallet de retiro?",
        "Registra la nueva dirección desde la página de Wallets de Retiro; pasará por su propia verificación "
        "por email y período de seguridad antes de activarse.",
    ),
    _answer_item(
        "withdrawal_review_required", KnowledgeCategory.WITHDRAWALS, "¿Por qué mi retiro está en revisión?",
        "Los retiros superiores a cierto monto reciben una revisión interna adicional antes de enviarse, "
        "como medida de seguridad.",
    ),
    _answer_item(
        "withdrawal_large_amount", KnowledgeCategory.WITHDRAWALS, "¿Puedo retirar un monto grande?",
        "Sí — los retiros más grandes simplemente pasan por un paso adicional de revisión interna.",
    ),
    _support_item("withdrawal_cancel", KnowledgeCategory.WITHDRAWALS, "¿Puedo cancelar mi retiro?"),
    _support_item("withdrawal_rejected", KnowledgeCategory.WITHDRAWALS, "¿Por qué se rechazó mi retiro?"),
    _support_item("withdrawal_sent_not_received", KnowledgeCategory.WITHDRAWALS, "Dice enviado pero no lo veo"),
    _support_item("withdrawal_txid", KnowledgeCategory.WITHDRAWALS, "¿Cuál es el ID de mi transacción?"),
    _support_item("withdrawal_received_less", KnowledgeCategory.WITHDRAWALS, "Recibí menos de lo solicitado"),
    _ops_item("withdrawal_unauthorized", KnowledgeCategory.WITHDRAWALS, "No autoricé este retiro"),

    # ── Deposits (spec §5) ───────────────────────────────────────────
    _answer_item(
        "deposit_how_to", KnowledgeCategory.DEPOSITS, "¿Cómo deposito?",
        "Ve a Depositar, elige tu activo, y envía los fondos a la dirección mostrada. Tu wallet se "
        "acredita automáticamente una vez que la red confirme la transacción.",
    ),
    _pending_item("deposit_supported_assets", KnowledgeCategory.DEPOSITS, "¿Qué activos puedo depositar?"),
    _pending_item("deposit_supported_networks", KnowledgeCategory.DEPOSITS, "¿Qué redes son compatibles?"),
    _answer_item(
        "deposit_processing_time", KnowledgeCategory.DEPOSITS, "¿Cuánto tarda un depósito?",
        "Los depósitos se acreditan automáticamente una vez que la red confirma tu transacción — el "
        "tiempo depende de la congestión de la red, no de nosotros.",
    ),
    _support_item("deposit_missing", KnowledgeCategory.DEPOSITS, "Mi depósito no aparece"),
    _support_item(
        "deposit_confirmed_not_credited", KnowledgeCategory.DEPOSITS,
        "Está confirmado en blockchain pero no en mi saldo",
    ),
    _ops_item("deposit_wrong_network", KnowledgeCategory.DEPOSITS, "Envié a la red equivocada"),
    _ops_item("deposit_wrong_address", KnowledgeCategory.DEPOSITS, "Envié a la dirección equivocada"),
    _support_item("deposit_address", KnowledgeCategory.DEPOSITS, "¿Cuál es mi dirección de depósito?"),
    _pending_item("deposit_minimum", KnowledgeCategory.DEPOSITS, "¿Hay un depósito mínimo?"),
    _pending_item("deposit_fee", KnowledgeCategory.DEPOSITS, "¿Hay comisiones por depositar?"),
    _answer_item(
        "deposit_wrong_amount", KnowledgeCategory.DEPOSITS, "Envié un monto distinto al que quería",
        "Tu wallet se acredita según lo efectivamente recibido y confirmado en blockchain, no según el "
        "monto que originalmente pretendías enviar.",
    ),
    _support_item("deposit_cancel", KnowledgeCategory.DEPOSITS, "¿Puedo cancelar un depósito?"),
    _answer_item(
        "deposit_without_kyc", KnowledgeCategory.DEPOSITS, "¿Puedo depositar sin KYC?",
        "Sí — no se requiere KYC para depositar.",
    ),
    _pending_item(
        "deposit_third_party_wallet", KnowledgeCategory.DEPOSITS, "¿Puedo depositar desde la wallet de otra persona?",
    ),
    _support_item("deposit_pending", KnowledgeCategory.DEPOSITS, "Mi depósito dice \"pending\""),
    _answer_item(
        "deposit_finished", KnowledgeCategory.DEPOSITS, "Mi depósito dice \"finished\", ¿y ahora?",
        "\"Finished\" significa que está totalmente confirmado y ya fue acreditado a tu wallet.",
    ),
    _answer_item(
        "deposit_history", KnowledgeCategory.DEPOSITS, "¿Dónde veo mi historial de depósitos?",
        "Tu historial completo de depósitos está disponible en la página de Historial de Depósitos.",
    ),
    _support_item("deposit_txid", KnowledgeCategory.DEPOSITS, "¿Cuál es el ID de mi transacción de depósito?"),
    _support_item("deposit_balance_mismatch", KnowledgeCategory.DEPOSITS, "Mi saldo no coincide con lo que deposité"),

    # ── KYC (spec §6) ────────────────────────────────────────────────
    _answer_item(
        "kyc_why", KnowledgeCategory.KYC, "¿Por qué necesito verificar mi identidad?",
        "La verificación de identidad (KYC) es obligatoria antes de retirar fondos, como medida "
        "regulatoria y de seguridad.",
    ),
    _pending_item("kyc_documents", KnowledgeCategory.KYC, "¿Qué documentos necesito?"),
    _support_item("kyc_processing_time", KnowledgeCategory.KYC, "¿Cuánto tarda la revisión de mi KYC?"),
    _support_item("kyc_rejected", KnowledgeCategory.KYC, "¿Por qué se rechazó mi KYC?"),
    _answer_item(
        "kyc_resubmit", KnowledgeCategory.KYC, "¿Cómo vuelvo a enviar mis documentos?",
        "Puedes volver a enviarlos desde la página de Verificación después de un rechazo.",
    ),
    _answer_item(
        "kyc_deposit_requirement", KnowledgeCategory.KYC, "¿Necesito KYC para depositar?",
        "No — el KYC no es necesario para depositar.",
    ),
    _answer_item(
        "kyc_trading_requirement", KnowledgeCategory.KYC, "¿Necesito KYC para operar (trading)?",
        "No — el KYC no es necesario para trading demo ni normal.",
    ),
    _answer_item(
        "kyc_withdrawal_requirement", KnowledgeCategory.KYC, "¿Necesito KYC para retirar?",
        "Sí — la verificación KYC debe estar aprobada antes de cualquier retiro.",
    ),

    # ── Trading (spec §7) ────────────────────────────────────────────
    _answer_item(
        "trading_equity", KnowledgeCategory.TRADING, "¿Qué es el equity?",
        "El equity es tu balance más el profit/loss flotante (no realizado) de tus posiciones abiertas.",
    ),
    _answer_item(
        "trading_margin", KnowledgeCategory.TRADING, "¿Qué es el margen?",
        "El margen es la parte de tu balance reservada para mantener abierta una posición.",
    ),
    _answer_item(
        "trading_stop_loss", KnowledgeCategory.TRADING, "¿Cómo funciona el stop loss?",
        "Un stop loss cierra automáticamente tu posición al precio que definas, para limitar pérdidas.",
    ),
    _answer_item(
        "trading_take_profit", KnowledgeCategory.TRADING, "¿Cómo funciona el take profit?",
        "Un take profit cierra automáticamente tu posición al precio que definas, para asegurar ganancias.",
    ),
    _answer_item(
        "trading_modify_sl_tp", KnowledgeCategory.TRADING,
        "¿Puedo cambiar mi stop loss/take profit después de abrir la posición?",
        "Sí, puedes modificar el SL/TP de una posición abierta en cualquier momento.",
    ),
    _answer_item(
        "trading_history", KnowledgeCategory.TRADING, "¿Dónde veo mi historial de operaciones?",
        "Tu historial completo de operaciones está disponible en la página de Historial.",
    ),
    _support_item("trading_insufficient_margin", KnowledgeCategory.TRADING, "¿Por qué no puedo abrir esta operación?"),
    _support_item("trading_order_rejected", KnowledgeCategory.TRADING, "¿Por qué se rechazó mi orden?"),
    _support_item("trading_pnl_calculation", KnowledgeCategory.TRADING, "¿Cómo se calcula mi P&L?"),
    _support_item(
        "trading_chart_execution_mismatch", KnowledgeCategory.TRADING,
        "Mi precio de ejecución no coincide con el gráfico",
    ),

    # ── Account (spec §8) ────────────────────────────────────────────
    _answer_item(
        "forgot_password", KnowledgeCategory.ACCOUNT, "Olvidé mi contraseña",
        "Usa \"Olvidé mi contraseña\" en la pantalla de inicio de sesión para restablecerla por email.",
    ),
    _answer_item(
        "email_verification", KnowledgeCategory.ACCOUNT, "¿Cómo verifico mi email?",
        "Revisa tu bandeja de entrada para el enlace de verificación, o solicita uno nuevo desde tu perfil.",
    ),
    _answer_item(
        "profile_update", KnowledgeCategory.ACCOUNT, "¿Cómo actualizo mi perfil?",
        "Puedes actualizar los datos de tu perfil desde la página de Perfil.",
    ),
    _answer_item(
        "2fa_setup", KnowledgeCategory.ACCOUNT, "¿Cómo activo el 2FA?",
        "Ve a Configuración de Seguridad y sigue los pasos para activar 2FA con tu app autenticadora.",
    ),
    _support_item("login_problems", KnowledgeCategory.ACCOUNT, "No puedo iniciar sesión"),
    _ops_item("account_locked", KnowledgeCategory.ACCOUNT, "Mi cuenta está bloqueada"),
    _ops_item("lost_2fa_account", KnowledgeCategory.ACCOUNT, "Perdí acceso a mi autenticador (2FA)"),
    _ops_item("account_closure", KnowledgeCategory.ACCOUNT, "¿Cómo cierro mi cuenta?"),

    # ── Security (spec §9 — every intent is RED by definition) ──────
    _ops_item("unauthorized_withdrawal", KnowledgeCategory.SECURITY, "No autoricé un retiro"),
    _ops_item("unauthorized_trade", KnowledgeCategory.SECURITY, "No autoricé una operación"),
    _ops_item("account_takeover_suspicion", KnowledgeCategory.SECURITY, "Sospecho que mi cuenta fue comprometida"),
    _ops_item("phishing", KnowledgeCategory.SECURITY, "Recibí un mensaje sospechoso a nombre de Money Broker"),
    _ops_item("unknown_login_activity", KnowledgeCategory.SECURITY, "Veo un inicio de sesión que no reconozco"),
    _ops_item("balance_anomaly", KnowledgeCategory.SECURITY, "Mi saldo cambió y no sé por qué"),

    # ── Technical Support (spec §11) ─────────────────────────────────
    _support_item("page_not_loading", KnowledgeCategory.TECHNICAL, "La página no carga", requires_account_data=False),
    _support_item("chart_frozen", KnowledgeCategory.TECHNICAL, "Mi gráfico no se actualiza", requires_account_data=False),
    _support_item("order_button_not_working", KnowledgeCategory.TECHNICAL, "El botón de operar no responde"),
    _support_item("balance_display_issue", KnowledgeCategory.TECHNICAL, "Mi saldo se ve incorrecto en pantalla"),
    _support_item(
        "email_not_received", KnowledgeCategory.TECHNICAL, "No estoy recibiendo emails", requires_account_data=False,
    ),
    _support_item("document_upload_issue", KnowledgeCategory.TECHNICAL, "No puedo subir mi documento KYC"),
)
