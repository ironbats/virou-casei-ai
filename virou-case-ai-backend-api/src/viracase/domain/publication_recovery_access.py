from viracase.domain.rules import RuleError

RECOVER_OWN_PUBLICATION_NO_EFFECT = "recover_own_publication_no_effect"
RECOVERY_OWNER_ROLES = frozenset({"student"})


def authorize_publication_recovery(actor_id, role, project_owner_id, operation_owner_id):
    if actor_id != project_owner_id or actor_id != operation_owner_id:
        raise RuleError("NOT_FOUND", "Publicação indisponível.", 404)
    if role not in RECOVERY_OWNER_ROLES:
        raise RuleError("ACCESS_DENIED", "Seu papel não permite recuperar esta tentativa de publicação.", 403)
