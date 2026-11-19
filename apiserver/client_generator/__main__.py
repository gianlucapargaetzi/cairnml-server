import argparse
import sys
from pathlib import Path

from .callers import Allow, check_callers, read_allow
from .web_check import check_web
from .python_renderer import HEADER, Unsupported, render_package, render_service_files
from .schema_model import load_schema
from .targets import TARGETS

DEFAULT_SCHEMA = Path(__file__).parents[1] / "schema" / "services"
HEADER_INIT = HEADER + "\n"


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
    static = Path(__file__).parent / "web_static"
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
    # the shared parts of the client (base path, configuration, parameter encoding): written once
    # api.ts is the options type the generated services import from their own folder; the others are shared by the client
    # the files the services import from their own folder (api options, the transport) sit beside them; the response
    # wrapper of the transport sits with the models; the rest are shared by the whole client
    beside_services = {"api.ts", "api-requests.service.ts"}
    places = {
        src.name: (args.out / "api-services" if src.name in beside_services
                   else args.out / "model" if src.name == "api-request.ts" else args.out)
        for src in sorted(static.glob("*.ts"))
    }
    for path in (places[src.name] / src.name for src in sorted(static.glob("*.ts"))):
        text = (static / path.name).read_text()
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
            findings = check_callers(
                args.root, schema, set(target.resolve(schema)), allow, sessions=target.checks_sessions
            )
        except (OSError, ValueError, SyntaxError) as error:
            return _usage(str(error))
        for finding in findings:
            print(finding)
        return 1 if findings else 0

    if not target.generates:
        return _usage(f"the target {args.target!r} is only checked (check-callers), not generated")
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
            rendered[name] = render_service_files(schema[name], target.session_package)
        except (Unsupported, ValueError) as error:
            return _usage(f"{name}: {error}")
    files = {}
    for modules in rendered.values():
        for relative, text in modules.items():
            files[relative] = text
    whole = set(services) == set(available)  # the whole target: the package file names every service of it
    if whole:
        package = render_package(available, schema)
        files["__init__.py"] = package
        for folder in sorted({"/".join(path.split("/")[:-1]) for path in files if path.startswith("legacy/")}):
            files[f"{folder}/__init__.py"] = HEADER_INIT
        if any(path.startswith("legacy/") for path in files):
            files["legacy/__init__.py"] = HEADER_INIT
    stale = 0
    for relative, text in files.items():
        path = args.out / relative
        if args.check:
            if not path.is_file():
                print(f"{relative} is missing in {args.out}")
                stale += 1
            elif path.read_text() != text:
                print(f"{relative} is out of date in {args.out}")
                stale += 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if whole:  # a legacy module of a definition that is no longer retained is not left behind
        legacy = args.out / "legacy"
        for path in sorted(legacy.rglob("*.py")) if legacy.is_dir() else []:
            if path.relative_to(args.out).as_posix() in files:
                continue
            if args.check:
                print(f"{path.relative_to(args.out).as_posix()} is not generated any more in {args.out}")
                stale += 1
            else:
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
