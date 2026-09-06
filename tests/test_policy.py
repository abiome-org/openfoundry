import pytest
import yaml
from openfoundry.errors import ValidationError
from openfoundry.policy import PolicyEngine, PolicyRule, ProjectPolicy, promotion_gate

NAMESPACE = "local/test-project"
PROJECT = {
    "metadata": {"namespace": NAMESPACE},
    "spec": {"extensions": {"policyDirectory": "policies"}},
}


def _policy_document(**config):
    return {
        "apiVersion": "openfoundry.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {"name": "default", "namespace": NAMESPACE},
        "spec": {
            "rules": [
                {
                    "name": "allow-owner",
                    "effect": "allow",
                    "match": {"actor": "local-user", "resource": NAMESPACE},
                }
            ],
            "config": {"dirtyWorktree": "deny", **config},
        },
    }


def _write_policy(root, document, name="default.yaml"):
    (root / "policies").mkdir(exist_ok=True)
    (root / "policies" / name).write_text(yaml.safe_dump(document))


def test_deny_overrides_allow_and_default_deny():
    engine = PolicyEngine(
        [
            PolicyRule("yes", "allow", {"action": "deploy"}),
            PolicyRule("no", "deny", {"actor": "bad"}),
        ]
    )
    assert engine.evaluate({"action": "deploy", "actor": "bad"}).outcome == "deny"
    assert engine.evaluate({"action": "read"}).outcome == "deny"


def test_project_policy_loads_documents_and_authorizes_by_actor(tmp_path):
    _write_policy(tmp_path, _policy_document())
    policy = ProjectPolicy.load(tmp_path, PROJECT)

    assert policy.enforced
    assert policy.dirty_worktree == "deny"
    assert policy.directory == "policies"
    assert policy.digest.startswith("sha256:")
    assert policy.documents[0]["path"] == "default.yaml"
    owner = {"actor": "local-user", "action": "workload.run", "resource": NAMESPACE}
    assert policy.authorize(owner).outcome == "allow"
    assert policy.authorize({**owner, "actor": "stranger"}).outcome == "deny"
    assert ProjectPolicy.load(tmp_path, PROJECT).digest == policy.digest


def test_project_policy_without_documents_allows_and_records_no_enforcement(tmp_path):
    policy = ProjectPolicy.load(tmp_path, PROJECT)

    assert not policy.enforced
    assert policy.dirty_worktree == "allow"
    decision = policy.authorize({"actor": "anyone", "action": "workload.run"})
    assert decision.outcome == "allow"
    assert decision.explanations[0]["rule"] == "no-policy-documents"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"dirtyWorktree": "sometimes"}, "dirtyWorktree"),
        ({"unsignedModules": "allow"}, "unsignedModules"),
        ({"sync": {"allowDelete": True}}, "not enforced"),
        ({"sync": {"retries": 3}}, "not enforced"),
        ({"promotion": {"requireEvaluationPass": "yes"}}, "boolean"),
        ({"retention": {"days": 3}}, "not enforced"),
    ],
)
def test_project_policy_rejects_unenforced_or_weakened_config(tmp_path, config, message):
    _write_policy(tmp_path, _policy_document(**config))
    with pytest.raises(ValidationError, match=message):
        ProjectPolicy.load(tmp_path, PROJECT)


def test_project_policy_rejects_conflicts_namespace_and_directory_escape(tmp_path):
    _write_policy(tmp_path, _policy_document())
    _write_policy(tmp_path, _policy_document(dirtyWorktree="allow"), name="second.yaml")
    with pytest.raises(ValidationError, match="disagree"):
        ProjectPolicy.load(tmp_path, PROJECT)

    foreign = _policy_document()
    foreign["metadata"]["namespace"] = "local/other"
    _write_policy(tmp_path, foreign, name="second.yaml")
    with pytest.raises(ValidationError, match="namespace"):
        ProjectPolicy.load(tmp_path, PROJECT)

    (tmp_path / "policies/second.yaml").unlink()
    (tmp_path / "policies/notes.txt").write_text("ignored")
    assert len(ProjectPolicy.load(tmp_path, PROJECT).documents) == 1
    with pytest.raises(ValidationError, match="relative path"):
        ProjectPolicy.load(
            tmp_path,
            {
                "metadata": {"namespace": NAMESPACE},
                "spec": {"extensions": {"policyDirectory": ".."}},
            },
        )


def test_promotion_requires_integrity_and_respects_project_quality_requirements(tmp_path):
    evidence = {"lineage_complete": True, "rights_valid": True, "evaluation_passed": True}
    assert promotion_gate(evidence).outcome == "allow"
    assert promotion_gate({**evidence, "evaluation_passed": False}).outcome == "deny"
    assert promotion_gate(evidence, {"requireVulnerabilityScan": True}).outcome == "deny"
    assert promotion_gate({**evidence, "vulnerabilities_present": True}).outcome == "deny"
    assert promotion_gate(evidence, {"requireCompatibilityPass": True}).outcome == "deny"
    requirements = {"requireEvaluationPass": False}
    _write_policy(tmp_path, _policy_document(promotion=requirements))
    policy = ProjectPolicy.load(tmp_path, PROJECT)
    assert (
        promotion_gate({**evidence, "evaluation_passed": False}, policy.config["promotion"]).outcome
        == "allow"
    )
    assert promotion_gate({**evidence, "rights_valid": False}, requirements).outcome == "deny"
    assert promotion_gate({**evidence, "lineage_complete": False}, requirements).outcome == "deny"


def test_policy_cannot_silently_ignore_a_misspelled_actor_constraint():
    with pytest.raises(ValidationError, match="actro"):
        PolicyEngine([PolicyRule("owner-only", "allow", {"actro": "owner"})])
