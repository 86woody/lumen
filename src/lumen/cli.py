"""Owner CLI and unprivileged MCP adapter for the offline daemon."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .daemon import call, enroll, serve
from .model import LumenError
from .workspace import Workspace


def parser():
    p = argparse.ArgumentParser(prog="lumen")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--home", type=Path, default=Path.home() / ".lumen")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--workspace", type=Path, required=True)
    init.add_argument("--project", default="local")
    init.add_argument('--owner', action='append', help='Explicit CODEOWNERS identity for a new workspace')
    daemon = sub.add_parser("daemon")
    daemon.add_argument("--workspace", type=Path)
    sub.add_parser("mcp")
    for name, help_text in (('hook-session', 'Bounded unprivileged session-start hook endpoint'),
                            ('hook-prompt', 'Bounded capture of a submitted prompt; payload on stdin'),
                            ('hook-stop', 'Bounded capture of a finished turn; payload on stdin')):
        hook = sub.add_parser(name, help=help_text)
        hook.add_argument('--workspace', type=Path, required=True)
        hook.add_argument('--cwd', type=Path, required=True)
        hook.add_argument('--session', required=True)
        hook.add_argument('--host', choices=['claude-code', 'copilot-cli'], required=True)
    hooks = sub.add_parser('hooks', help='Host hook settings; preview unless write is explicit')
    hooks.add_argument('action', choices=['preview', 'write', 'check'])
    hooks.add_argument('--host', choices=['claude-code', 'copilot-cli'], required=True)
    hooks.add_argument('--python', type=Path, default=Path(sys.executable))
    hooks.add_argument('--settings', type=Path, default=None,
                       help='Claude: ~/.claude/settings.json; Copilot: the owned file under the Copilot hooks directory')
    why = sub.add_parser("why")
    why.add_argument("eid")
    audit = sub.add_parser("audit")
    audit.add_argument("--ci", action="store_true")
    for op in ("remember", "revise", "recall", "expand", "session-start", "export", "restore", "purge", "share"):
        command = sub.add_parser(op)
        command.add_argument("--json", dest="payload", help="JSON arguments, or '-' to read stdin", default="{}")
    for op in ("doctor", "reindex", "journal-checkpoint", "pending", "skills"):
        sub.add_parser(op)
    feedback = sub.add_parser('feedback', help='Record a verdict on a recalled event: helpful, wrong, stale or harmful')
    feedback.add_argument('--json', dest='payload', help='{"id": ..., "verdict": ..., "note": ...}, or - for stdin', default='{}')
    consolidate = sub.add_parser('consolidate', help='Run the deterministic consolidator over new episodes')
    consolidate.add_argument('--force', action='store_true', help='Ignore the minimum interval since the last run')
    imp = sub.add_parser('import', help='Import a host memory file or directory as agent-observed candidates')
    imp.add_argument('--host', choices=['claude-code', 'codex-cli', 'hermes'], required=True)
    imp.add_argument('--path', type=Path, help='Memory file or directory; required except for the Codex default')
    imp.add_argument('--scope', default='user')
    imp.add_argument('--all', action='store_true', help='Allow more than twenty candidates in one run')
    catalog = sub.add_parser("catalog")
    catalog.add_argument("action", choices=["build", "check", "repos"])
    catalog.add_argument("--workspace", type=Path, required=True)
    repos = sub.add_parser('repos', help='Inspect repository identities and availability')
    repos.add_argument('--workspace', type=Path, required=True)
    owners = sub.add_parser('codeowners')
    owners.add_argument('action', choices=['preview', 'write', 'check'])
    owners.add_argument('--workspace', type=Path, required=True)
    instructions = sub.add_parser('instructions', help='Fenced Lumen section of the root AGENTS.md')
    instructions.add_argument('action', choices=['preview', 'write', 'check'])
    instructions.add_argument('--workspace', type=Path, required=True)
    bench = sub.add_parser("bench")
    bench.add_argument("--suite", required=True)
    bench.add_argument("--mode")
    bench.add_argument("--manifest", type=Path)
    bench.add_argument("--clients", type=Path)
    bench.add_argument('--native-config', type=Path, help='Explicit account-bound native acceptance configuration')
    bench.add_argument('--native-output', type=Path, help='New directory for actual native acceptance artifacts')
    bench.add_argument("--max-cost-usd", type=int, default=0)
    bench.add_argument("--output", type=Path, default=Path("artifacts/local/last-run.json"))
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            if not (args.workspace / ".lumen/workspace.json").exists():
                Workspace.initialize(args.workspace, [{"id": args.project, "root": ".", "kind": "project",
                    "purpose": args.project, "owners": args.owner or ["local-owner"], "depends_on": []}])
            workspace = Workspace(args.workspace, args.home)
            workspace.build()
            result = enroll(args.home, workspace.id, workspace.visible_scopes(args.project), args.project)
            try:
                result['codeowners'] = workspace.manage_codeowners('write')
            except LumenError as exc:
                result['codeowners'] = {'status': 'pending', 'reason': str(exc)}
            from .hosts import IMPORT_NOTE
            try:
                check = workspace.manage_instructions('check')
                result['instructions'] = {'status': 'healthy' if check['healthy'] else 'pending',
                                          'path': check['path'], 'claude_md': check['claude_md']}
            except LumenError as exc:
                result['instructions'] = {'status': 'pending', 'reason': str(exc)}
            if result['instructions']['status'] != 'healthy':
                result['instructions']['action'] = 'lumen instructions preview --workspace ' + str(args.workspace) +                     '  then  lumen instructions write --workspace ' + str(args.workspace)
            result['notes'] = [IMPORT_NOTE]
            result['team_files'] = {'relations': str(workspace.root / '.lumen/relations.yaml'),
                                    'config': str(workspace.root / '.lumen/config.toml'),
                                    'policy_digest': workspace.policy['digest'], 'policy_source': workspace.policy['source']}
        elif args.command == "daemon":
            workspace = Workspace(args.workspace, args.home) if args.workspace else None
            serve(args.home, workspace)
            return 0
        elif args.command == "mcp":
            from .mcp import serve_stdio
            serve_stdio(args.home)
            return 0
        elif args.command in ('hook-session', 'hook-prompt', 'hook-stop'):
            from .security import safe_source
            workspace = Workspace(args.workspace, args.home)
            relative = args.cwd.resolve().relative_to(workspace.root).as_posix()
            cwd = safe_source(workspace.root, relative, workspace.manifest['exclusions'])
            project = workspace.resolve(cwd)
            if args.command != 'hook-session':
                raw = sys.stdin.buffer.read(262145)
                if len(raw) > 262144:
                    return 1
                payload = json.loads(raw.decode('utf-8'))
                if args.host == 'copilot-cli':
                    from .hosts import copilot_turn
                    if args.command != 'hook-stop' or not isinstance(payload, dict) or payload.get('sessionId') != args.session:
                        return 1
                    turn = copilot_turn(payload.get('transcriptPath'))
                    if turn is None:
                        return 1
                    arguments = {'checkout': workspace.checkout, 'host': args.host, 'session': args.session,
                                 'turn': turn['id'], 'project': project, 'workspace': workspace.id, 'assistant': turn['assistant']}
                    if turn['user'] is not None:
                        arguments['user'] = turn['user']
                    result = call(args.home, 'capture_stop', arguments, owner=False, timeout=2)
                    return 1 if 'error' in result else 0
                if not isinstance(payload, dict) or not isinstance(payload.get('prompt_id'), str) \
                        or not 0 < len(payload['prompt_id']) <= 128 or payload.get('session_id') != args.session:
                    return 1
                # Claude Code 2.1.268 sends the submitted text as `prompt`; the reference names `user_prompt`.
                field = 'last_assistant_message' if args.command == 'hook-stop' else                     'prompt' if isinstance(payload.get('prompt'), str) else 'user_prompt'
                if not isinstance(payload.get(field), str) or not payload[field]:
                    return 1
                operation = 'capture_prompt' if args.command == 'hook-prompt' else 'capture_stop'
                arguments = {'checkout': workspace.checkout, 'host': args.host, 'session': args.session,
                             'turn': payload['prompt_id'], 'project': project, 'workspace': workspace.id,
                             ('text' if operation == 'capture_prompt' else 'assistant'): payload[field]}
                result = call(args.home, operation, arguments, owner=False, timeout=2)
                return 1 if 'error' in result else 0
            result = call(args.home, 'session_start', {'session': args.session, 'project': project,
                          'workspace': workspace.id}, owner=False, timeout=2)
            if 'error' in result:
                return 1
            # The hint plus the catalog slice, frozen per session, under 2,000 estimated tokens (6 KiB).
            context = result['result']['context']
            if len(context.encode('utf-8')) > 6144:
                return 1
            if args.host == 'copilot-cli':
                print(json.dumps({'additionalContext': context}))
            else:
                print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context}}))
            return 0
        elif args.command == "catalog":
            workspace = Workspace(args.workspace, args.home)
            result = workspace.build() if args.action == "build" else workspace.check() if args.action == "check" else workspace.snapshot()
        elif args.command == 'repos':
            result = Workspace(args.workspace, args.home).snapshot()
        elif args.command == 'codeowners':
            result = Workspace(args.workspace, args.home).manage_codeowners(args.action)
        elif args.command == 'instructions':
            result = Workspace(args.workspace, args.home).manage_instructions(args.action)
        elif args.command == 'hooks':
            from .hosts import claude_handlers, copilot_handlers, copilot_hooks_path, manage_copilot_hooks, manage_hooks
            if args.host == 'copilot-cli':
                result = manage_copilot_hooks(args.action, args.settings or copilot_hooks_path(),
                                              copilot_handlers(args.python.resolve(), args.home.resolve()))
            else:
                result = manage_hooks(args.action, args.settings or Path.home() / '.claude' / 'settings.json',
                                      claude_handlers(args.python.resolve(), args.home.resolve()))
        elif args.command == "bench":
            from .evaluation import bench
            result = bench(args)
        elif args.command == "why":
            result = call(args.home, "why", {"eid": args.eid}, owner=True)
        elif args.command == 'consolidate':
            result = call(args.home, 'consolidate', {'force': args.force}, owner=True, timeout=60)
        elif args.command == 'import':
            arguments = {'host': args.host, 'scope': args.scope, 'all': args.all}
            if args.path is not None:
                arguments['path'] = str(args.path.resolve())
            result = call(args.home, 'import', arguments, owner=True, timeout=60)
        elif args.command == "audit":
            result = call(args.home, "audit", {'ci': args.ci}, owner=True)
            if args.ci:
                result['passed'] = result.get('result', {}).get('passed', False)
        else:
            payload = getattr(args, "payload", "{}")
            arguments = json.load(sys.stdin) if payload == "-" else json.loads(payload)
            result = call(args.home, args.command.replace("-", "_"), arguments, owner=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if "error" in result or result.get("healthy") is False or result.get("passed") is False or result.get("result", {}).get("healthy") is False or result.get('result', {}).get('passed') is False else 0
    except (LumenError, OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "source_unavailable"), "message": str(exc)}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
