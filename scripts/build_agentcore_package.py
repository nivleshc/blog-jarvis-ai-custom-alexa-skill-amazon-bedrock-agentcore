#!/usr/bin/env python3
"""
build_agentcore_package.py -- builds a deployable .zip package for a
Bedrock AgentCore Runtime "direct code deployment" agent (code_configuration,
not a container image), invoked from Terraform via a null_resource +
local-exec (see terraform/agentcore_runtime.tf).

WHY THIS EXISTS: AgentCore Runtime's direct-code-deploy path needs the
agent's own .py files AND its third-party dependencies (bedrock-agentcore,
strands-agents, etc.) bundled into one .zip -- and those dependencies must
be ARM64-compatible wheels, since AgentCore Runtime only supports the
arm64 instruction set (AWS Graviton), confirmed against AWS's own
direct-code-deployment troubleshooting docs
(docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
runtime-code-deploy-common-issues.html -- "AgentCore Runtime only
supports the arm64 instruction set architecture"). A plain `zip -r` of
the source directory doesn't install dependencies at all, and a plain
`pip install -r requirements.txt` on most developer laptops (Intel/AMD
x86_64, or even Apple Silicon, which uses a different wheel tag) does
not reliably produce ARM64 Linux wheels -- `pip install --platform
manylinux2014_aarch64 --only-binary=:all:` is what forces pip to fetch
prebuilt ARM64 wheels regardless of the host machine's own architecture,
confirmed working end-to-end against this project's own
agents/boredom_buster_agent/requirements.txt before wiring this into
Terraform (produces a ~28MB zip, well under AgentCore Runtime's 250MB
direct-code-deploy package size limit).

Terraform's built-in `archive_file` data source (used for the plain-
Python Lambda functions in lambda.tf/lambda_tasks.tf) can't do this --
it only zips existing files, it doesn't run pip. Hence a real build
step, run via local-exec, in Python -- matching this project's existing
"Python wherever possible" approach (see scripts/deploy_skill.py's own
docstring for the same principle).

USAGE (invoked by Terraform, but usable standalone for local testing):
    python3 scripts/build_agentcore_package.py \\
        --source-dir agents/boredom_buster_agent \\
        --requirements agents/boredom_buster_agent/requirements.txt \\
        --output-zip terraform/agent_build/boredom_buster_agent.zip \\
        --python-version 3.12

Reusable for any future AgentCore direct-code-deploy agent this project
adds -- not hardcoded to Boredom Buster specifically, per DEPLOYMENT.md's
"adding a new AgentCore agent" guidance.
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Directories never included in the package, wherever they appear --
# test suites and Python bytecode caches have no place in a deployed
# agent's runtime package.
EXCLUDED_DIR_NAMES = {"tests", "__pycache__", ".pytest_cache"}


def build_package(source_dir: Path, requirements_file: Path, output_zip: Path, python_version: str) -> None:
    build_dir = output_zip.parent / f"{output_zip.stem}_build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Step 1: install third-party dependencies as ARM64 wheels directly
    # into the build directory. --platform/--implementation/--python-
    # version/--only-binary together tell pip to fetch prebuilt wheels
    # for that exact target, never build/compile anything locally for
    # the host machine's own architecture -- this is what makes the
    # output correct regardless of whether this script runs on Intel,
    # AMD, or Apple Silicon.
    if requirements_file.exists() and requirements_file.read_text().strip():
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--platform",
                "manylinux2014_aarch64",
                "--implementation",
                "cp",
                "--python-version",
                python_version,
                "--only-binary=:all:",
                "--target",
                str(build_dir),
                "-r",
                str(requirements_file),
            ],
            check=True,
        )

    # Step 2: copy the agent's own source .py files on top of the
    # installed dependencies -- copied last so the agent's own modules
    # are never shadowed by a same-named dependency (not expected to
    # happen here, but keeps the intent unambiguous).
    for py_file in sorted(source_dir.glob("*.py")):
        shutil.copy2(py_file, build_dir / py_file.name)

    # Step 3: zip it up. Preserves each file's existing permission bits
    # via ZipInfo.from_file() -- AgentCore Runtime's direct-code-deploy
    # package uses POSIX file permissions (confirmed against AWS's own
    # docs), and zipfile's default write path can silently drop them.
    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(build_dir.rglob("*")):
            if path.is_dir():
                continue
            relative_parts = path.relative_to(build_dir).parts
            if any(part in EXCLUDED_DIR_NAMES for part in relative_parts):
                continue
            zip_info = zipfile.ZipInfo.from_file(path, arcname=str(path.relative_to(build_dir)))
            with path.open("rb") as f:
                zf.writestr(zip_info, f.read(), zipfile.ZIP_DEFLATED)

    shutil.rmtree(build_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", required=True, type=Path, help="Directory containing the agent's own .py files (e.g. agents/boredom_buster_agent)")
    parser.add_argument("--requirements", required=True, type=Path, help="Path to the agent's requirements.txt")
    parser.add_argument("--output-zip", required=True, type=Path, help="Path to write the built .zip package to")
    parser.add_argument("--python-version", default="3.12", help="Target Python version for ARM64 wheel selection (default: 3.12)")
    args = parser.parse_args()

    args.output_zip.parent.mkdir(parents=True, exist_ok=True)
    build_package(args.source_dir, args.requirements, args.output_zip, args.python_version)
    print(f"Built {args.output_zip} ({args.output_zip.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
