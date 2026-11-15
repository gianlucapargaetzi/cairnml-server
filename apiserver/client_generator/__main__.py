import argparse
import sys
from pathlib import Path

from .callers import Allow, check_callers, read_allow
from .python_renderer import Unsupported, render_service
from .schema_model import load_schema
from .targets import TARGETS

DEFAULT_SCHEMA = Path(__file__).parents[1] / "schema" / "services"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apiserver.client_generator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "check-callers"):
        command = sub.add_parser(name)
        command.add_argument("--target", required=True, help=f"one of: {', '.join(TARGETS)}")
        command.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="folder with the schema's .conf files")
    generate = sub.choices["generate"]
    generate.add_argument("--services", help="comma separated; default: the services of the target")
    generate.add_argument("--out", type=Path, required=True, help="folder for the generated modules")
    generate.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file differs or is missing")
    callers = sub.choices["check-callers"]
    callers.add_argument("--root", type=Path, required=True, help="folder or file with the Python callers")
    callers.add_argument("--allow", type=Path, help="allow file with `exempt <service.action> <reason>` lines")
    return parser


def _usage(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    target = TARGETS.get(args.target)
    if target is None:
        return _usage(f"unknown target {args.target!r} (known: {', '.join(TARGETS)})")
    schema = load_schema(args.schema)

    if args.command == "check-callers":
        allow = read_allow(args.allow) if args.allow else None
        findings = check_callers(args.root, schema, set(target.services), allow or Allow())
        for finding in findings:
            print(finding)
        return 1 if findings else 0

    services = args.services.split(",") if args.services else list(target.services)
    for name in services:
        if name not in target.services:
            return _usage(f"{name!r} is not a service of the target {args.target!r} ({', '.join(target.services)})")
        if name not in schema:
            return _usage(f"{name!r} is not in the schema folder {args.schema}")
    stale = 0
    for name in services:
        try:
            text = render_service(schema[name], target.session_package)
        except Unsupported as error:
            return _usage(f"{name}: {error}")
        path = args.out / f"{name}.py"
        if args.check:
            if not path.is_file():
                print(f"{path.name} is missing in {args.out}")
                stale += 1
            elif path.read_text() != text:
                print(f"{path.name} is out of date in {args.out}")
                stale += 1
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
