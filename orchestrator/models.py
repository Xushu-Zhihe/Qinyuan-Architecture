from dataclasses import dataclass


NODE_TYPES = frozenset({"goal", "origin", "fact", "hint"})
AGENT_NODE_TYPES = frozenset({"fact"})
RELATION_EDGE_TYPES = frozenset(
    {
        "supports",
        "contradicts",
        "derived_from",
        "addresses",
        "similar_to",
        "equivalent_to",
    }
)
EDGE_TYPES = frozenset(
    {
        "supports",
        "contradicts",
        "derived_from",
        "addresses",
        "plans_from",
        "supersedes",
        "similar_to",
        "equivalent_to",
    }
)


@dataclass(frozen=True, slots=True)
class IntentProposal:
    source_node_id: str
    target_node_id: str
    content: str


@dataclass(frozen=True, slots=True)
class IntentDecision:
    action: str
    intent: IntentProposal | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class FactProposal:
    ref: str
    content: str


@dataclass(frozen=True, slots=True)
class RelationProposal:
    type: str
    from_ref: str
    to_ref: str


@dataclass(frozen=True, slots=True)
class ExecutionUpdate:
    intent_id: str
    facts: tuple[FactProposal, ...]
    edges: tuple[RelationProposal, ...]
    goal_achieved: bool
    final_answer: str | None


@dataclass(frozen=True, slots=True)
class IntentParseResult:
    status: str
    decision: IntentDecision | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ResultParseResult:
    status: str
    update: ExecutionUpdate | None
    error: str | None


@dataclass(frozen=True, slots=True)
class NodeProposal:
    ref: str
    type: str
    content: str


@dataclass(frozen=True, slots=True)
class EdgeProposal:
    type: str
    from_ref: str
    to_ref: str


@dataclass(frozen=True, slots=True)
class AgentUpdate:
    nodes: tuple[NodeProposal, ...]
    edges: tuple[EdgeProposal, ...]
    goal_achieved: bool
    final_answer: str | None


@dataclass(frozen=True, slots=True)
class ProtocolResult:
    status: str
    update: AgentUpdate | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AgentRoundResult:
    provider: str
    process_status: str
    protocol_status: str
    exit_code: int | None
    duration_seconds: float
    output: str
    stdout_path: str
    stderr_path: str
    work_dir: str
    update: AgentUpdate | None
    protocol_error: str | None


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: str
    run_dir: str
    status: str
    rounds_completed: int
    final_answer: str | None
    candidates: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "achieved" else 1
