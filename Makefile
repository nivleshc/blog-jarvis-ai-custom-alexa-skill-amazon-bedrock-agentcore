# ============================================================
# Local CI -- mirrors .github/workflows/ci.yml job-for-job, so you
# can run the exact same checks locally before pushing, instead of
# pushing, waiting for GitHub Actions, finding a failure, and
# looping. Run `make ci` to run everything the pipeline runs, in
# the same order, with the same commands and the same pinned tool
# versions.
#
# This is deliberately just a Makefile, not a git pre-commit hook --
# you asked to run it on demand, not have it forced on every commit.
#
# Targets map 1:1 to ci.yml's jobs:
#   lint               -> ci.yml "Lint Python" job
#   test               -> ci.yml "pytest" job
#   tf-validate        -> ci.yml "Terraform fmt/validate" job
#   script-dry-run     -> ci.yml "Script --help dry-run" job
#   ci                 -> all of the above, in the same order,
#                         stopping at the first failure (same as
#                         separate GitHub Actions jobs would each
#                         report independently, but here you get a
#                         fast single-command pass/fail instead of
#                         four job pages to check).
#
# Tool versions are pinned to match ci.yml exactly:
#   ruff==0.14.0            (ci.yml's lint job)
#   terraform >= 1.6.0      (versions.tf's required_version; ci.yml
#                            uses 1.9.0 in its runner, but any
#                            Terraform satisfying required_version
#                            works locally -- fmt/validate output is
#                            not version-sensitive here)
#   requirements.txt / requirements-dev.txt (same files ci.yml's
#                            test/script-dry-run jobs install from)
# ============================================================

SHELL := /bin/bash

# Everything runs inside a dedicated local virtualenv so this never
# pollutes (or is polluted by) whatever Python environment/pyenv
# version you normally use for other projects. Deleted and rebuilt
# on demand with `make clean` / `make setup`.
VENV_DIR := .venv-ci
VENV_PY  := $(VENV_DIR)/bin/python3
VENV_BIN := $(VENV_DIR)/bin

TERRAFORM_DIR := terraform
RUFF_VERSION := ruff==0.14.0

.PHONY: help setup lint test tf-validate script-dry-run ci clean

help:
	@echo "Local CI targets (mirrors .github/workflows/ci.yml):"
	@echo "  make ci              run everything, in the same order as GitHub Actions"
	@echo "  make lint            ruff check alexa_lambda/ and scripts/"
	@echo "  make test            full pytest suite"
	@echo "  make tf-validate     terraform fmt -check + init -backend=false + validate"
	@echo "  make script-dry-run  --help dry-run of all 3 operational scripts"
	@echo "  make setup           create/refresh the local .venv-ci virtualenv"
	@echo "  make clean           remove .venv-ci, __pycache__, .pytest_cache, lambda_build"

# ------------------------------------------------------------
# setup -- one virtualenv, dependencies pinned exactly the same way
# ci.yml installs them (requirements.txt + requirements-dev.txt +
# the same ruff version). Re-run automatically (via the venv
# stamp file dependency below) whenever those files change, so a
# stale venv never silently hides a version drift from CI.
# ------------------------------------------------------------
$(VENV_DIR)/.stamp: alexa_lambda/requirements.txt alexa_lambda/requirements-dev.txt agents/boredom_buster_agent/requirements.txt
	python3 -m venv $(VENV_DIR)
	$(VENV_BIN)/pip install --quiet --upgrade pip
	$(VENV_BIN)/pip install --quiet -r alexa_lambda/requirements.txt
	$(VENV_BIN)/pip install --quiet -r alexa_lambda/requirements-dev.txt
	$(VENV_BIN)/pip install --quiet -r agents/boredom_buster_agent/requirements.txt
	$(VENV_BIN)/pip install --quiet $(RUFF_VERSION)
	touch $(VENV_DIR)/.stamp

setup: $(VENV_DIR)/.stamp

# ------------------------------------------------------------
# lint -- identical commands to ci.yml's "Lint Python" job, minus
# the --output-format=github flag (that format is meant for
# annotating a GitHub PR diff, not a terminal).
# ------------------------------------------------------------
lint: setup
	$(VENV_BIN)/ruff check alexa_lambda/
	$(VENV_BIN)/ruff check scripts/
	$(VENV_BIN)/ruff check lambda_tasks/
	$(VENV_BIN)/ruff check agents/boredom_buster_agent/

# ------------------------------------------------------------
# test -- identical to ci.yml's "pytest" job. pytest.ini's
# testpaths already covers both alexa_lambda/tests and
# scripts/tests, so this one command matches `pytest -v` in CI.
# ------------------------------------------------------------
test: setup
	$(VENV_BIN)/pytest -v

# ------------------------------------------------------------
# tf-validate -- identical to ci.yml's "Terraform fmt/validate"
# job: fmt check, then init with no backend/credentials, then
# validate with a placeholder alert_email (same placeholder value
# ci.yml uses) so the required-variable check passes without a
# real value. Never touches AWS or a real backend.
#
# fmt -check only targets *.tf here (not *.tfvars) deliberately --
# ci.yml's checkout never includes your local terraform.tfvars (it's
# gitignored, and CI has no working copy of it at all), so `terraform
# fmt -check -recursive` in CI only ever sees .tf files. Checking
# *.tfvars locally would fail this target on cosmetic spacing in your
# own gitignored, personal config file -- a false mismatch with what
# CI actually checks, not a real drift to fix.
# ------------------------------------------------------------
tf-validate:
	terraform -chdir=$(TERRAFORM_DIR) fmt -check -- $$(cd $(TERRAFORM_DIR) && ls *.tf)
	terraform -chdir=$(TERRAFORM_DIR) init -backend=false -input=false
	TF_VAR_alert_email="ci-placeholder@example.com" terraform -chdir=$(TERRAFORM_DIR) validate

# ------------------------------------------------------------
# script-dry-run -- identical to ci.yml's "Script --help dry-run"
# job. deploy_skill.py has no third-party imports so it works even
# without the venv, but it's run through the same venv here for
# consistency with the other two scripts (which import boto3).
# ------------------------------------------------------------
script-dry-run: setup
	$(VENV_BIN)/python3 scripts/approve_user.py --help
	$(VENV_BIN)/python3 scripts/usage_report.py --help
	$(VENV_BIN)/python3 scripts/deploy_skill.py --help

# ------------------------------------------------------------
# ci -- everything, in the same order ci.yml's jobs run. Uses `set
# -e` semantics (each recipe line already stops make on failure by
# default) so the first failing check stops the run, same as a
# failed GitHub Actions job would stop that job -- but you see it
# in seconds locally instead of after a push.
# ------------------------------------------------------------
ci: lint test tf-validate script-dry-run
	@echo ""
	@echo "All local CI checks passed -- matches what .github/workflows/ci.yml will run."

# ------------------------------------------------------------
# clean -- remove the local venv and Python/Terraform build
# artifacts so a fresh `make ci` rebuilds everything from scratch.
# ------------------------------------------------------------
clean:
	rm -rf $(VENV_DIR)
	rm -rf lambda_build
	find . -name "__pycache__" -not -path "./$(VENV_DIR)/*" -exec rm -rf {} +
	find . -name ".pytest_cache" -not -path "./$(VENV_DIR)/*" -exec rm -rf {} +
