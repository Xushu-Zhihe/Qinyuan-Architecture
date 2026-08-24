from dataclasses import dataclass


MAX_FACTS_PER_RESULT = 4
MAX_FACT_CONTENT_LENGTH = 800
MAX_NEW_RESULT_FACT_CONTENT_LENGTH = 4_000
MAX_TOTAL_FACT_CONTENT_LENGTH = 6_000
MAX_RESULT_FACT_CONTENT_LENGTH = 1_000_000
MAX_SUPPORTING_FACTS = 20
MAX_SUPPORTING_FACT_CONTENT_LENGTH = 20_000


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
    answer_key: str | None = None
    protocol_version: int = 1
    goal_result_ref: str | None = None


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
    project_id: str
    run_dir: str
    status: str
    highest_generation: int
    final_answer: str | None
    candidates: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "achieved" else 1


@dataclass(frozen=True, slots=True)
class GoalResultFact:
    id: str
    content: str
    sources: tuple[str, ...] = ()
    produced_by_intent_id: str | None = None
    produced_by_execution_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Goal result Fact id must be non-empty")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Goal result Fact content must be non-empty")
        if len(self.content) > MAX_RESULT_FACT_CONTENT_LENGTH:
            raise ValueError("Goal result Fact content exceeds the domain limit")


@dataclass(frozen=True, slots=True)
class GoalResult:
    project_id: str
    goal_version: int
    goal_node_id: str
    result_ref: str
    fact: GoalResultFact
    supporting_facts: tuple[GoalResultFact, ...] = ()
    supporting_providers: tuple[str, ...] = ()
    completion_threshold: int = 1
    result_value: str | None = None

    def __post_init__(self) -> None:
        if not self.project_id.strip() or not self.goal_node_id.strip():
            raise ValueError("GoalResult project_id and goal_node_id must be non-empty")
        if self.goal_version < 1:
            raise ValueError("GoalResult goal_version must be positive")
        expected_ref = f"{self.project_id}:{self.goal_version}:{self.fact.id}"
        if self.result_ref != expected_ref:
            raise ValueError("GoalResult result_ref does not match project, version, and Fact")
        if len(self.supporting_facts) > MAX_SUPPORTING_FACTS:
            raise ValueError("too many supporting Facts")
        supporting_ids = [fact.id for fact in self.supporting_facts]
        if len(set(supporting_ids)) != len(supporting_ids) or self.fact.id in supporting_ids:
            raise ValueError("supporting Facts must be distinct from the result Fact")
        if any(len(fact.content) > MAX_SUPPORTING_FACT_CONTENT_LENGTH for fact in self.supporting_facts):
            raise ValueError("supporting Fact content exceeds the domain limit")
        if self.completion_threshold < 1:
            raise ValueError("completion_threshold must be positive")
        if self.result_value is not None and not self.result_value.strip():
            raise ValueError("GoalResult result_value must be non-empty when provided")
