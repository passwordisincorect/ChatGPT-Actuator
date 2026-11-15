"""Ephemeral, bounded service containers for integration tests."""

from __future__ import annotations

from typing import Any

SERVICE_PORTS = {"mysql": 3306, "redis": 6379, "rabbitmq": 5672}
_IMAGES = {
    "mysql": "mysql:8.4",
    "redis": "redis:7.4-alpine",
    "rabbitmq": "rabbitmq:4.1-alpine",
}


def build_compose(services: list[str]) -> dict[str, Any]:
    """Return a Docker Compose JSON document for isolated local test services."""
    if not isinstance(services, list) or any(s not in SERVICE_PORTS for s in services):
        raise ValueError("services must contain only mysql, redis, and rabbitmq")
    if len(set(services)) != len(services):
        raise ValueError("services must not contain duplicates")

    definitions: dict[str, Any] = {}
    for name in services:
        port = SERVICE_PORTS[name]
        service: dict[str, Any] = {
            "image": _IMAGES[name],
            "restart": "no",
            "ports": [{"target": port, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}],
            "cpus": 1.0,
            "mem_limit": "512m",
            "pids_limit": 256,
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "2"}},
        }
        if name == "mysql":
            service.update(
                environment={"MYSQL_DATABASE": "actuator_test", "MYSQL_USER": "actuator_test",
                             "MYSQL_PASSWORD": "${ACTUATOR_TEST_PASSWORD:?ACTUATOR_TEST_PASSWORD is required}",
                             "MYSQL_ROOT_PASSWORD": "${ACTUATOR_TEST_PASSWORD:?ACTUATOR_TEST_PASSWORD is required}"},
                healthcheck={"test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u root -p\"$$MYSQL_ROOT_PASSWORD\" --silent"],
                             "interval": "5s", "timeout": "3s", "retries": 20, "start_period": "20s"},
            )
        elif name == "redis":
            service.update(
                command=["sh", "-c", "exec redis-server --requirepass \"$$ACTUATOR_TEST_PASSWORD\""],
                environment={"ACTUATOR_TEST_PASSWORD": "${ACTUATOR_TEST_PASSWORD:?ACTUATOR_TEST_PASSWORD is required}"},
                healthcheck={"test": ["CMD-SHELL", "redis-cli -a \"$$ACTUATOR_TEST_PASSWORD\" ping | grep -q PONG"],
                             "interval": "5s", "timeout": "3s", "retries": 20},
            )
        else:
            service.update(
                environment={"RABBITMQ_DEFAULT_USER": "actuator_test", "RABBITMQ_DEFAULT_PASS": "${ACTUATOR_TEST_PASSWORD:?ACTUATOR_TEST_PASSWORD is required}",
                             "RABBITMQ_DEFAULT_VHOST": "actuator_test"},
                healthcheck={"test": ["CMD", "rabbitmq-diagnostics", "-q", "ping"],
                             "interval": "5s", "timeout": "5s", "retries": 20, "start_period": "20s"},
            )
        definitions[name] = service
    return {"services": definitions}


def build_environment(service: str, port: int, password: str) -> dict[str, str]:
    """Return conventional client and Spring Boot settings for a service."""
    if service not in SERVICE_PORTS:
        raise ValueError(f"unsupported service: {service}")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not password:
        raise ValueError("password must not be empty")
    common = {"SPRING_PROFILES_ACTIVE": "test"}
    if service == "mysql":
        return {**common, "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": str(port), "MYSQL_DATABASE": "actuator_test",
                "MYSQL_USER": "actuator_test", "MYSQL_PASSWORD": password,
                "MYSQL_JDBC_URL": f"jdbc:mysql://127.0.0.1:{port}/actuator_test?allowPublicKeyRetrieval=true&useSSL=false",
                "SPRING_DATASOURCE_URL": f"jdbc:mysql://127.0.0.1:{port}/actuator_test?allowPublicKeyRetrieval=true&useSSL=false",
                "SPRING_DATASOURCE_USERNAME": "actuator_test", "SPRING_DATASOURCE_PASSWORD": password}
    if service == "redis":
        return {**common, "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(port), "REDIS_PASSWORD": password,
                "SPRING_DATA_REDIS_HOST": "127.0.0.1", "SPRING_DATA_REDIS_PORT": str(port),
                "SPRING_DATA_REDIS_PASSWORD": password}
    return {**common, "RABBITMQ_HOST": "127.0.0.1", "RABBITMQ_PORT": str(port), "RABBITMQ_USERNAME": "actuator_test",
            "RABBITMQ_PASSWORD": password, "RABBITMQ_VHOST": "actuator_test",
            "SPRING_RABBITMQ_HOST": "127.0.0.1", "SPRING_RABBITMQ_PORT": str(port),
            "SPRING_RABBITMQ_USERNAME": "actuator_test", "SPRING_RABBITMQ_PASSWORD": password,
            "SPRING_RABBITMQ_VIRTUAL_HOST": "actuator_test"}
