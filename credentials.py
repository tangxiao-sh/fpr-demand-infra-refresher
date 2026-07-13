"""Standalone AWS service-credential rotation.

The business repositories originally carried copies of this code in their
``establish_proxy_connection.py`` files.  Accessor owns the implementation so
those repositories are not needed at runtime.
"""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
from typing import Any

import boto3

from config import ProjectConfig, Settings


LOG = logging.getLogger("accessor.credentials")


class CredentialError(RuntimeError):
    """Raised when AWS cannot provide a service session."""


def _tags(instance: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag.get("Key", "")).lower(): str(tag.get("Value", ""))
        for tag in instance.get("Tags", [])
    }


def _java_instance_role(session: Any, project: ProjectConfig) -> str | None:
    ec2 = session.client("ec2")
    filters = [
        {"Name": "tag:Service", "Values": [project.service_name]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    if project.ec2_cluster_tag:
        filters.append({"Name": "tag:Cluster", "Values": [project.ec2_cluster_tag]})
    response = ec2.describe_instances(Filters=filters)
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            tags = _tags(instance)
            if "java" not in tags.get("application", "").lower():
                continue
            profile = instance.get("IamInstanceProfile", {}).get("Arn")
            if not profile:
                continue
            profile_name = str(profile).rsplit("/", 1)[-1]
            iam = session.client("iam")
            roles = iam.get_instance_profile(InstanceProfileName=profile_name)
            entries = roles.get("InstanceProfile", {}).get("Roles", [])
            if entries:
                return entries[0].get("Arn")
    return None


def _ecs_role(ecs: Any, project: ProjectConfig) -> str | None:
    clusters: list[str] = []
    if project.ecs_cluster_name:
        response = ecs.describe_clusters(clusters=[project.ecs_cluster_name])
        clusters = [item["clusterArn"] for item in response.get("clusters", [])]
    else:
        token: str | None = None
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = ecs.list_clusters(**kwargs)
            clusters.extend(response.get("clusterArns", []))
            token = response.get("nextToken")
            if not token:
                break

    for cluster in clusters:
        token = None
        while True:
            kwargs = {"cluster": cluster, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = ecs.list_services(**kwargs)
            for service_arn in response.get("serviceArns", []):
                service = ecs.describe_services(
                    cluster=cluster, services=[service_arn], include=["TAGS"]
                ).get("services", [{}])[0]
                service_tags = {
                    str(tag.get("key", "")).lower(): str(tag.get("value", ""))
                    for tag in service.get("tags", [])
                }
                if service_tags.get(project.discovery_tag.lower()) != project.discovery_value:
                    continue
                task_definition = service.get("taskDefinition")
                if not task_definition:
                    continue
                definition = ecs.describe_task_definition(taskDefinition=task_definition)
                role = definition.get("taskDefinition", {}).get("taskRoleArn")
                if role:
                    return role
            token = response.get("nextToken")
            if not token:
                break
    return None


def _lambda_role(lam: Any, project: ProjectConfig) -> str | None:
    marker: str | None = None
    while True:
        response = lam.list_functions(**({"Marker": marker} if marker else {}))
        for function in response.get("Functions", []):
            if not function.get("FunctionName", "").startswith(project.service_name):
                continue
            tags = lam.list_tags(Resource=function["FunctionArn"]).get("Tags", {})
            if tags.get("Service") == project.service_name:
                return function.get("Role")
        marker = response.get("NextMarker")
        if not marker:
            return None


def _discover_role(session: Any, project: ProjectConfig) -> str:
    role = _java_instance_role(session, project)
    if role:
        return role
    role = _ecs_role(session.client("ecs"), project)
    if role:
        return role
    role = _lambda_role(session.client("lambda"), project)
    if role:
        return role
    raise CredentialError(f"no Java EC2, ECS task, or Lambda role found for {project.service_name}")


def _session_name(session: Any, project: ProjectConfig) -> str:
    if project.session_name_mode == "caller":
        user_id = session.client("sts").get_caller_identity().get("UserId", "accessor")
        suffix = str(user_id).split(":", 1)[-1]
        return f"local_staging,{project.service_name},{suffix}"
    aws_config = configparser.RawConfigParser()
    aws_config.read(Path.home() / ".aws" / "config", encoding="utf-8")
    section = f"profile {project.credential_profile}"
    suffix = aws_config.get(section, "granted_sso_role_name", fallback="")
    if not suffix:
        suffix = str(session.client("sts").get_caller_identity().get("UserId", "accessor"))
    return f"{project.session_name_prefix}-{suffix}"


def _write_credentials(service_name: str, credentials: dict[str, Any]) -> None:
    path = Path.home() / ".aws" / "credentials"
    parser = configparser.RawConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section(service_name):
        parser.add_section(service_name)
    parser[service_name] = {
        "aws_access_key_id": credentials["AccessKeyId"],
        "aws_secret_access_key": credentials["SecretAccessKey"],
        "aws_session_token": credentials["SessionToken"],
    }
    temporary = path.with_suffix(".accessor-tmp")
    with temporary.open("w", encoding="utf-8") as output:
        parser.write(output)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def refresh_service_credentials(settings: Settings, project: ProjectConfig) -> str:
    """Assume the service role and write its temporary AWS profile."""
    LOG.info("Refreshing service credentials for %s", project.name)
    session = boto3.Session(
        profile_name=project.credential_profile,
        region_name=settings.proxy.region,
    )
    role_arn = _discover_role(session, project)
    credentials = session.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName=_session_name(session, project),
    )["Credentials"]
    _write_credentials(project.service_name, credentials)
    return project.service_name
