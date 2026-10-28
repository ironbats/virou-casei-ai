from sqlalchemy import select

from viracase.domain.rules import canonical_hash
from viracase.infrastructure.models import IntegrationConfig, Operation


def verification_resource_name(policy, connection_id, stack):
    identity = canonical_hash({"account_id": policy["account_id"], "connection_id": connection_id, "stack": stack})
    return policy["worker_prefix"] + "-check-" + identity[:24]


def retained_resource(repo, policy, connection_id, stack, name, evidence):
    def match(result):
        if result.get("resource_policy") != "retain":
            return None
        for resource in result.get("verification_resources", []):
            if (
                resource.get("stack") == stack
                and resource.get("worker_name") == name
                and resource.get("creation_started")
            ):
                return dict(resource)
        return None

    current = match(evidence)
    if current:
        return current
    with repo.sessions() as db:
        previous = db.scalars(
            select(Operation)
            .join(IntegrationConfig, IntegrationConfig.id == Operation.policy["config_id"].as_string())
            .where(
                Operation.type == "publication_check",
                Operation.payload["connection_id"].as_string() == connection_id,
                Operation.result["resource_policy"].as_string() == "retain",
                IntegrationConfig.settings["account_id"].as_string() == policy["account_id"],
                IntegrationConfig.settings["worker_prefix"].as_string() == policy["worker_prefix"],
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .execution_options(yield_per=50)
        )
        for operation in previous:
            resource = match(operation.result)
            if resource:
                return resource
    return None
