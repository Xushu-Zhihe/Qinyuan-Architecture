import unittest

from orchestrator.models import (
    AGENT_NODE_TYPES,
    NODE_TYPES,
    IntentDecision,
    IntentProposal,
)


class DomainModelTests(unittest.TestCase):
    def test_intent_is_not_a_node_type(self) -> None:
        self.assertEqual(
            NODE_TYPES,
            frozenset({"goal", "origin", "fact", "hint"}),
        )
        self.assertEqual(AGENT_NODE_TYPES, frozenset({"fact"}))

    def test_plan_decision_contains_first_class_intent_edge(self) -> None:
        proposal = IntentProposal(
            source_node_id="fact-1",
            target_node_id="goal-1",
            content="Verify the key source",
        )
        decision = IntentDecision(
            action="plan",
            intent=proposal,
            reason=None,
        )

        self.assertEqual(decision.intent.source_node_id, "fact-1")
        self.assertEqual(decision.intent.target_node_id, "goal-1")


if __name__ == "__main__":
    unittest.main()
