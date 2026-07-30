import json
import sys
import time
from pathlib import Path


def state_path(root: Path, container_id: str) -> Path:
    return root / f"{container_id}.json"


def load_state(root: Path, container_id: str) -> dict[str, object]:
    return json.loads(state_path(root, container_id).read_text(encoding="utf-8"))


def save_state(root: Path, container_id: str, state: dict[str, object]) -> None:
    state_path(root, container_id).write_text(
        json.dumps(state, sort_keys=True),
        encoding="utf-8",
    )


def parse_create(arguments: list[str]) -> tuple[str, list[str]]:
    name = "fake-container"
    index = 0
    options_with_values = {
        "--name",
        "--network",
        "--workdir",
        "--mount",
        "--cap-add",
        "--env",
        "--memory",
        "--cpus",
        "--pids-limit",
    }
    options_without_values = {"--interactive", "--read-only"}
    while index < len(arguments):
        argument = arguments[index]
        if argument in options_with_values:
            if argument == "--name":
                name = arguments[index + 1]
            index += 2
            continue
        if argument in options_without_values:
            index += 1
            continue
        # First non-option is the image; remaining values are the command.
        return name, arguments[index + 1 :]
    return name, []


def main() -> int:
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    arguments = sys.argv[2:]
    if not arguments:
        print("missing fake docker command", file=sys.stderr)
        return 2
    action = arguments[0]
    rest = arguments[1:]

    if action == "create":
        name, command = parse_create(rest)
        container_id = f"fake-{name}"
        save_state(
            root,
            container_id,
            {"command": command, "running": False, "exit_code": 0},
        )
        print(container_id)
        return 0

    if action == "start":
        container_id = rest[-1]
        state = load_state(root, container_id)
        state["running"] = True
        save_state(root, container_id, state)
        request = sys.stdin.read()
        command = [str(value) for value in state.get("command", [])]
        if "fake-sleep" in command:
            kill_path = root / f"{container_id}.kill"
            for _ in range(600):
                if kill_path.exists():
                    state["running"] = False
                    state["exit_code"] = 137
                    save_state(root, container_id, state)
                    return 137
                time.sleep(0.05)
            state["running"] = False
            state["exit_code"] = 0
            save_state(root, container_id, state)
            return 0
        if "fake-fail" in command:
            print("fake container failure", file=sys.stderr)
            state["running"] = False
            state["exit_code"] = 7
            save_state(root, container_id, state)
            return 7
        sys.stdout.write(request)
        state["running"] = False
        state["exit_code"] = 0
        save_state(root, container_id, state)
        return 0

    if action == "inspect":
        container_id = rest[-1]
        try:
            state = load_state(root, container_id)
        except FileNotFoundError:
            print("no such container", file=sys.stderr)
            return 1
        template = rest[1] if len(rest) >= 2 and rest[0] == "--format" else ""
        if "ExitCode" in template:
            print(state.get("exit_code", 0))
        elif "Running" in template:
            print("true" if state.get("running") else "false")
        else:
            print(json.dumps(state))
        return 0

    if action in {"kill", "stop"}:
        container_id = rest[-1]
        try:
            state = load_state(root, container_id)
        except FileNotFoundError:
            return 1
        (root / f"{container_id}.kill").write_text("1", encoding="ascii")
        state["running"] = False
        state["exit_code"] = 137 if action == "kill" else state.get("exit_code", 0)
        save_state(root, container_id, state)
        print(container_id)
        return 0

    if action == "rm":
        container_id = rest[-1]
        target = state_path(root, container_id)
        if target.exists():
            target.unlink()
        kill_path = root / f"{container_id}.kill"
        if kill_path.exists():
            kill_path.unlink()
        print(container_id)
        return 0

    print(f"unsupported fake docker command: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
