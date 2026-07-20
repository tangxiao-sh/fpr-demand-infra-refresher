"""Refresh local Gradle and Docker access from the build-artifact AWS role."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
import shutil
import subprocess
from typing import Any

import boto3

from config import BuildArtifactsConfig, Settings


LOG = logging.getLogger("accessor.build_artifacts")


def _write_gradle_property(path: Path, name: str, value: str) -> None:
    """Replace one Gradle property atomically without exposing its token.

    Project repositories read ``~/.gradle/gradle.properties``. Keep unrelated
    properties and comments intact, remove duplicate token lines, and write the
    replacement with owner-only permissions before atomically moving it in.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    prefix = f"{name}="
    updated = [line for line in lines if not line.startswith(prefix)]
    updated.append(f"{prefix}{value}")
    temporary = path.with_name(f".{path.name}.accessor-tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _login_ecr(session: Any, artifacts: BuildArtifactsConfig) -> None:
    """Log Docker into configured ECR registries without leaking passwords.

    Docker is intentionally best effort. It is useful for local image work but
    is unrelated to Gradle dependency resolution, so an unavailable Docker
    daemon must not invalidate an otherwise working build credential refresh.
    """
    if not artifacts.ecr_registry_ids:
        return
    docker = shutil.which("docker")
    if docker is None:
        LOG.info("Docker is unavailable; skipping optional ECR login")
        return
    try:
        authorizations = session.client("ecr").get_authorization_token(
            registryIds=list(artifacts.ecr_registry_ids)
        ).get("authorizationData", [])
        for authorization in authorizations:
            encoded = authorization.get("authorizationToken", "")
            endpoint = str(authorization.get("proxyEndpoint", "")).removeprefix("https://")
            try:
                _username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
            except (ValueError, UnicodeDecodeError, TypeError) as error:
                LOG.warning("Skipping malformed ECR authorization response: %s", error)
                continue
            result = subprocess.run(
                [docker, "login", "--username", "AWS", "--password-stdin", endpoint],
                input=password + "\n",
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                LOG.warning("ECR Docker login failed for %s", endpoint)
    except Exception as error:
        # boto3 exceptions are intentionally handled here too. This optional
        # action must never make Gradle's already-refreshed token unavailable.
        LOG.warning("Optional ECR Docker login failed: %s", error)


def refresh_build_artifacts(settings: Settings) -> bool:
    """Write a fresh CodeArtifact token and optionally renew Docker ECR login."""
    artifacts = settings.build_artifacts
    if not artifacts.enabled:
        return True
    try:
        session = boto3.Session(profile_name=artifacts.profile, region_name=artifacts.region)
        response = session.client("codeartifact").get_authorization_token(
            domain=artifacts.codeartifact_domain,
            domainOwner=artifacts.codeartifact_domain_owner,
        )
        token = response.get("authorizationToken")
        if not isinstance(token, str) or not token:
            LOG.error("CodeArtifact did not return an authorization token")
            return False
        _write_gradle_property(
            artifacts.gradle_properties_path, artifacts.gradle_property, token
        )
    except Exception as error:
        LOG.error("Unable to refresh Gradle CodeArtifact token: %s", error)
        return False
    LOG.info("Gradle CodeArtifact token refreshed")
    _login_ecr(session, artifacts)
    return True
