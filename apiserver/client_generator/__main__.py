import argparse
import sys
from pathlib import Path

from .callers import Allow, check_callers, read_allow
from .web_check import check_web
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
    web = sub.add_parser("check-web", help="check the web app's TypeScript API layer against the schema")
    web.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="folder with the schema's .conf files")
    web.add_argument("--root", type=Path, required=True, help="the web app's src/app/business-logic folder")
    web.add_argument("--allow", type=Path, help="allow file with `exempt <service.action> <reason>` lines")
    return parser


def _usage(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _generate_web(args, schema) -> int:
    """Writes the web app's API layer into --out (the business-logic folder): api-services/ and model/<service>/."""
    from .web_renderer import render_api, render_models

    names = args.services.split(",") if args.services else sorted(schema)
    rendered = {}
    for name in names:
        if name not in schema:
            return _usage(f"{name!r} is not a service of the schema")
        try:
            rendered[name] = (render_api(schema[name]), render_models(schema[name]))
        except Unsupported as error:
            return _usage(f"{name}: {error}")
    stale = 0
    for name, (api, models) in rendered.items():
        targets = {args.out / "api-services" / f"{name}.service.ts": api}
        folder = args.out / "model" / name
        for stem, text in models.items():
            targets[folder / f"{stem}.ts"] = text
        targets[folder / "models.ts"] = "".join(f"export * from '././{stem}';\n" for stem in sorted(models))
        for path, text in targets.items():
            if args.check:
                if not path.is_file() or path.read_text() != text:
                    print(f"{path} is out of date")
                    stale += 1
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
    return 1 if stale else 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate" and args.target == "web":
        try:
            schema = load_schema(args.schema)
        except (OSError, ValueError) as error:
            return _usage(f"cannot read the schema in {args.schema}: {error}")
        return _generate_web(args, schema)
    if args.command == "check-web":
        try:
            findings = check_web(args.root, load_schema(args.schema), read_allow(args.allow) if args.allow else Allow())
        except (OSError, ValueError) as error:
            return _usage(str(error))
        for finding in findings:
            print(finding)
        return 1 if findings else 0
    target = TARGETS.get(args.target)
    if target is None:
        return _usage(f"unknown target {args.target!r} (known: {', '.join(TARGETS)})")
    try:
        schema = load_schema(args.schema)
    except (OSError, ValueError) as error:
        return _usage(f"cannot read the schema in {args.schema}: {error}")

    if args.command == "check-callers":
        if not args.root.exists():
            return _usage(f"{args.root} does not exist")
        try:
            allow = read_allow(args.allow) if args.allow else Allow()
            findings = check_callers(args.root, schema, set(target.resolve(schema)), allow)
        except (OSError, ValueError, SyntaxError) as error:
            return _usage(str(error))
        for finding in findings:
            print(finding)
        return 1 if findings else 0

    available = target.resolve(schema)
    services = args.services.split(",") if args.services else list(available)
    for name in services:
        if name not in available:
            return _usage(f"{name!r} is not a service of the target {args.target!r} ({', '.join(available)})")
        if name not in schema:
            return _usage(f"{name!r} is not in the schema folder {args.schema}")
    rendered = {}
    for name in services:  # render everything first, so a failure writes nothing
        try:
            rendered[name] = render_service(schema[name], target.session_package)
        except (Unsupported, ValueError) as error:
            return _usage(f"{name}: {error}")
    stale = 0
    for name, text in rendered.items():
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
