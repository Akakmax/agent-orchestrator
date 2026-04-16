"""Orchestrator CLI — entry point for Frank and agents.

Provides build, sprint, contract, message, tick, and log management
via argparse subcommands. All output goes to stdout as JSON or
human-readable text.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .db import OrchestratorDB
from .models import (
    BuildStatus,
    ContractStatus,
    MsgType,
    OrchestratorConfig,
    SprintStatus,
)
from .state_machine import (
    BuildTransitionError,
    SprintTransitionError,
    transition_build,
    transition_sprint,
)
from .contracts import propose_contract, review_contract
from .tick import run_tick


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Orchestrator CLI — multi-agent build management",
    )
    parser.add_argument("--db", default=OrchestratorConfig.DB_PATH, help="DB path")

    sub = parser.add_subparsers(dest="command")

    # ── build ────────────────────────────────────────────────────
    p_build = sub.add_parser("build", help="Create a new build")
    p_build.add_argument("prompt", help="Build prompt/description")

    # ── status ───────────────────────────────────────────────────
    sub.add_parser("status", help="List all builds")

    # ── sprints ──────────────────────────────────────────────────
    p_sprints = sub.add_parser("sprints", help="Show sprints for a build")
    p_sprints.add_argument("--build", required=True, dest="build_id", help="Build ID")

    # ── sprint (update / create-from-plan) ───────────────────────
    p_sprint = sub.add_parser("sprint", help="Sprint operations")
    sprint_sub = p_sprint.add_subparsers(dest="sprint_command")

    p_sprint_update = sprint_sub.add_parser("update", help="Transition sprint status")
    p_sprint_update.add_argument("--id", required=True, dest="sprint_id")
    p_sprint_update.add_argument("--status", required=True, choices=SprintStatus.ALL)

    p_sprint_plan = sprint_sub.add_parser("create-from-plan", help="Create sprints from plan JSON")
    p_sprint_plan.add_argument("--build", required=True, dest="build_id")
    p_sprint_plan.add_argument("--plan", required=True, help="Path to sprint_plan.json")

    # ── build-update ─────────────────────────────────────────────
    p_build_update = sub.add_parser("build-update", help="Update build fields")
    p_build_update.add_argument("--id", required=True, dest="build_id")
    p_build_update.add_argument("--status", default=None, choices=BuildStatus.ALL)
    p_build_update.add_argument("--spec-path", default=None)
    p_build_update.add_argument("--project-path", default=None)
    p_build_update.add_argument("--git-branch", default=None)
    p_build_update.add_argument("--total-sprints", type=int, default=None)

    # ── msg send / msg list ──────────────────────────────────────
    p_msg = sub.add_parser("msg", help="Message operations")
    msg_sub = p_msg.add_subparsers(dest="msg_command")

    p_msg_send = msg_sub.add_parser("send", help="Send a message")
    p_msg_send.add_argument("--build", required=True, dest="build_id")
    p_msg_send.add_argument("--from", required=True, dest="from_agent")
    p_msg_send.add_argument("--to", default=None, dest="to_agent")
    p_msg_send.add_argument("--type", required=True, dest="msg_type", choices=MsgType.ALL)
    p_msg_send.add_argument("--body", required=True)
    p_msg_send.add_argument("--sprint", default=None, dest="sprint_id")
    p_msg_send.add_argument("--parent", type=int, default=None, dest="parent_id")

    p_msg_list = msg_sub.add_parser("list", help="List messages")
    p_msg_list.add_argument("--build", required=True, dest="build_id")
    p_msg_list.add_argument("--to", default=None, dest="to_agent")
    p_msg_list.add_argument("--sprint", default=None, dest="sprint_id")

    # ── contract propose / approve / reject / show ───────────────
    p_contract = sub.add_parser("contract", help="Contract operations")
    contract_sub = p_contract.add_subparsers(dest="contract_command")

    p_propose = contract_sub.add_parser("propose", help="Propose a contract")
    p_propose.add_argument("--sprint", required=True, dest="sprint_id")
    p_propose.add_argument("--criteria", required=True, help="JSON criteria string")

    p_approve = contract_sub.add_parser("approve", help="Approve a contract")
    p_approve.add_argument("--sprint", required=True, dest="sprint_id")
    p_approve.add_argument("--version", required=True, type=int)

    p_reject = contract_sub.add_parser("reject", help="Reject a contract")
    p_reject.add_argument("--sprint", required=True, dest="sprint_id")
    p_reject.add_argument("--version", required=True, type=int)
    p_reject.add_argument("--notes", default=None)

    p_show = contract_sub.add_parser("show", help="Show contracts")
    p_show.add_argument("--sprint", default=None, dest="sprint_id")
    p_show.add_argument("--build", default=None, dest="build_id")

    # ── tick ─────────────────────────────────────────────────────
    p_tick = sub.add_parser("tick", help="Run tick loop")
    p_tick.add_argument("--dry-run", action="store_true")

    # ── log ──────────────────────────────────────────────────────
    p_log = sub.add_parser("log", help="Create agent log entry")
    p_log.add_argument("--build", required=True, dest="build_id")
    p_log.add_argument("--sprint", default=None, dest="sprint_id")
    p_log.add_argument("--agent", required=True)
    p_log.add_argument("--summary", default=None)
    p_log.add_argument("--log-path", default=None)
    p_log.add_argument("--duration", type=int, default=None)
    p_log.add_argument("--exit-code", type=int, default=None)
    p_log.add_argument("--session-id", default=None)

    # ── msgs (pretty-print) ─────────────────────────────────────
    p_msgs = sub.add_parser("msgs", help="Pretty-print messages")
    p_msgs.add_argument("--build", required=True, dest="build_id")
    p_msgs.add_argument("--sprint", default=None, dest="sprint_id")

    # ── watch ────────────────────────────────────────────────────
    p_watch = sub.add_parser("watch", help="Attach to build tmux session")
    p_watch.add_argument("build_id", help="Build ID to watch")

    # ── merge-status ────────────────────────────────────────────
    p_merge = sub.add_parser("merge-status", help="Show merge queue status")
    p_merge.add_argument("--build", required=True, dest="build_id")

    return parser


def _out(data):
    """Print data as formatted JSON."""
    print(json.dumps(data, indent=2, default=str))


def _cmd_build(db: OrchestratorDB, args):
    build = db.create_build(args.prompt)
    print(f"BUILD-{build['id']} created (status: {build['status']})")
    _out(build)


def _cmd_status(db: OrchestratorDB, args):
    builds = db.list_builds()
    if not builds:
        print("No builds found.")
        return
    for b in builds:
        prompt_preview = b["prompt"][:60] + "..." if len(b["prompt"]) > 60 else b["prompt"]
        sprint_info = f"sprint {b['current_sprint']}/{b['total_sprints']}"
        print(f"BUILD-{b['id']}  {b['status']:12s}  {sprint_info}  {prompt_preview}")


def _cmd_sprints(db: OrchestratorDB, args):
    sprints = db.get_sprints(args.build_id)
    if not sprints:
        print(f"No sprints for BUILD-{args.build_id}")
        return
    for s in sprints:
        print(f"  Sprint {s['sprint_number']}: {s['title']}  "
              f"[{s['status']}]  attempts: {s['attempts']}/{s['max_attempts']}")


def _cmd_sprint_update(db: OrchestratorDB, args):
    try:
        sprint = transition_sprint(db, args.sprint_id, args.status)
        print(f"Sprint {sprint['id']} -> {sprint['status']}")
        _out(sprint)
    except SprintTransitionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_sprint_create_from_plan(db: OrchestratorDB, args):
    plan = json.loads(Path(args.plan).read_text())
    sprints = plan.get("sprints", [])
    for s in sprints:
        depends_on = json.dumps(s.get("depends_on", []))
        sprint = db.create_sprint(
            build_id=args.build_id,
            sprint_number=s["number"],
            title=s["title"],
            description=s.get("description"),
            depends_on=depends_on,
        )
        criteria = s.get("criteria")
        if criteria:
            propose_contract(db, sprint["id"], criteria)
    db.update_build(args.build_id, total_sprints=len(sprints))
    print(f"Created {len(sprints)} sprints for BUILD-{args.build_id}")


def _cmd_build_update(db: OrchestratorDB, args):
    build = db.get_build(args.build_id)
    if not build:
        print(f"Build {args.build_id} not found", file=sys.stderr)
        sys.exit(1)

    # Handle non-status fields first
    kwargs = {}
    if args.spec_path:
        kwargs["spec_path"] = args.spec_path
    if args.project_path:
        kwargs["project_path"] = args.project_path
    if args.git_branch:
        kwargs["git_branch"] = args.git_branch
    if args.total_sprints is not None:
        kwargs["total_sprints"] = args.total_sprints
    if kwargs:
        db.update_build(args.build_id, **kwargs)

    # Handle status via state machine (M2 fix)
    if args.status:
        try:
            transition_build(db, args.build_id, args.status)
        except BuildTransitionError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    updated = db.get_build(args.build_id)
    print(f"BUILD-{updated['id']} updated: {updated['status']}")
    _out(updated)


def _cmd_msg_send(db: OrchestratorDB, args):
    msg = db.send_message(
        build_id=args.build_id,
        from_agent=args.from_agent,
        msg_type=args.msg_type,
        body=args.body,
        to_agent=args.to_agent,
        sprint_id=args.sprint_id,
        parent_id=args.parent_id,
    )
    _out(msg)


def _cmd_msg_list(db: OrchestratorDB, args):
    msgs = db.list_messages(
        build_id=args.build_id,
        to_agent=args.to_agent,
        sprint_id=args.sprint_id,
    )
    _out(msgs)


def _cmd_contract_propose(db: OrchestratorDB, args):
    criteria = json.loads(args.criteria)
    contract = propose_contract(db, args.sprint_id, criteria)
    _out(contract)


def _cmd_contract_approve(db: OrchestratorDB, args):
    # Find contract by sprint_id + version
    contracts = db.get_contracts(args.sprint_id)
    contract = next((c for c in contracts if c["version"] == args.version), None)
    if not contract:
        print(f"Contract v{args.version} not found for sprint {args.sprint_id}",
              file=sys.stderr)
        sys.exit(1)
    result = review_contract(db, args.sprint_id, contract["id"], approve=True)
    _out(result)


def _cmd_contract_reject(db: OrchestratorDB, args):
    contracts = db.get_contracts(args.sprint_id)
    contract = next((c for c in contracts if c["version"] == args.version), None)
    if not contract:
        print(f"Contract v{args.version} not found for sprint {args.sprint_id}",
              file=sys.stderr)
        sys.exit(1)
    result = review_contract(db, args.sprint_id, contract["id"],
                             approve=False, notes=args.notes)
    _out(result)


def _cmd_contract_show(db: OrchestratorDB, args):
    # M5 fix: --build without --sprint shows all sprints' contracts
    if args.build_id and not args.sprint_id:
        sprints = db.get_sprints(args.build_id)
        for s in sprints:
            contracts = db.get_contracts(s["id"])
            if contracts:
                print(f"\nSprint {s['sprint_number']}: {s['title']}")
                for c in contracts:
                    _out(c)
    elif args.sprint_id:
        contracts = db.get_contracts(args.sprint_id)
        _out(contracts)
    else:
        print("Provide --sprint or --build", file=sys.stderr)
        sys.exit(1)


def _cmd_tick(db: OrchestratorDB, args):
    actions = run_tick(db, dry_run=args.dry_run)
    _out({"actions": actions, "dry_run": args.dry_run})


def _cmd_log(db: OrchestratorDB, args):
    entry = db.create_agent_log(
        build_id=args.build_id,
        agent=args.agent,
        sprint_id=args.sprint_id,
        session_id=args.session_id,
        log_path=args.log_path,
        summary=args.summary,
        duration_seconds=args.duration,
        exit_code=args.exit_code,
    )
    _out(entry)


def _cmd_msgs(db: OrchestratorDB, args):
    msgs = db.list_messages(build_id=args.build_id, sprint_id=args.sprint_id)
    if not msgs:
        print(f"No messages for BUILD-{args.build_id}")
        return
    for m in msgs:
        to_str = f" -> {m['to_agent']}" if m.get("to_agent") else " (broadcast)"
        sprint_str = f" [sprint:{m['sprint_id']}]" if m.get("sprint_id") else ""
        print(f"[{m['created_at']}] {m['from_agent']}{to_str}{sprint_str} "
              f"({m['msg_type']}): {m['body']}")


def _cmd_watch(db: OrchestratorDB, args):
    session_name = f"build-{args.build_id}"
    os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


def _cmd_merge_status(db: OrchestratorDB, args):
    pending = db.get_pending_merges(args.build_id)
    # Also get non-pending entries
    all_entries = db.execute(
        "SELECT * FROM merge_queue WHERE build_id = ? ORDER BY created_at",
        (args.build_id,),
    ).fetchall()

    if not all_entries:
        print(f"No merge queue entries for BUILD-{args.build_id}")
        return

    for m in all_entries:
        m = dict(m)
        status_icon = {"pending": "⏳", "merging": "🔄", "resolved": "✅", "failed": "❌"}.get(m["status"], "?")
        conflicts = json.loads(m.get("conflict_files") or "[]")
        conflict_str = f" ({len(conflicts)} conflicts)" if conflicts else ""
        print(f"  {status_icon} {m['source_branch']} → {m['target_branch']}  "
              f"[{m['status']}]{conflict_str}  sprint:{m['sprint_id'][:8]}")


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return

    db = OrchestratorDB(args.db)
    try:
        if args.command == "build":
            _cmd_build(db, args)
        elif args.command == "status":
            _cmd_status(db, args)
        elif args.command == "sprints":
            _cmd_sprints(db, args)
        elif args.command == "sprint":
            if args.sprint_command == "update":
                _cmd_sprint_update(db, args)
            elif args.sprint_command == "create-from-plan":
                _cmd_sprint_create_from_plan(db, args)
            else:
                print("Use: sprint update | sprint create-from-plan", file=sys.stderr)
                sys.exit(1)
        elif args.command == "build-update":
            _cmd_build_update(db, args)
        elif args.command == "msg":
            if args.msg_command == "send":
                _cmd_msg_send(db, args)
            elif args.msg_command == "list":
                _cmd_msg_list(db, args)
            else:
                print("Use: msg send | msg list", file=sys.stderr)
                sys.exit(1)
        elif args.command == "contract":
            if args.contract_command == "propose":
                _cmd_contract_propose(db, args)
            elif args.contract_command == "approve":
                _cmd_contract_approve(db, args)
            elif args.contract_command == "reject":
                _cmd_contract_reject(db, args)
            elif args.contract_command == "show":
                _cmd_contract_show(db, args)
            else:
                print("Use: contract propose|approve|reject|show", file=sys.stderr)
                sys.exit(1)
        elif args.command == "tick":
            _cmd_tick(db, args)
        elif args.command == "log":
            _cmd_log(db, args)
        elif args.command == "msgs":
            _cmd_msgs(db, args)
        elif args.command == "watch":
            _cmd_watch(db, args)
        elif args.command == "merge-status":
            _cmd_merge_status(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
