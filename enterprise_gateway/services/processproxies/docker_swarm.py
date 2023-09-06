"""Code related to managing kernels running in docker-based containers."""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import logging
import os
from typing import Any

from docker.client import DockerClient
from docker.errors import NotFound
from docker.models.containers import Container
from docker.models.services import Service

# Debug logging level of docker produces too much noise - raise to info by default.
from ..kernels.remotemanager import RemoteKernelManager
from .container import ContainerProcessProxy

logging.getLogger("urllib3.connectionpool").setLevel(
    os.environ.get("EG_DOCKER_LOG_LEVEL", logging.WARNING)
)

docker_network = os.environ.get("EG_DOCKER_NETWORK", "bridge")

client = DockerClient.from_env()


class DockerSwarmProcessProxy(ContainerProcessProxy):
    """
    Kernel lifecycle management for kernels in Docker Swarm.
    """

    def __init__(self, kernel_manager: RemoteKernelManager, proxy_config: dict):
        """Initialize the proxy."""
        super().__init__(kernel_manager, proxy_config)

    def launch_process(
        self, kernel_cmd: str, **kwargs: dict[str, Any] | None
    ) -> DockerSwarmProcessProxy:
        """
        Launches the specified process within a Docker Swarm environment.
        """
        # Convey the network to the docker launch script
        kwargs["env"]["EG_DOCKER_NETWORK"] = docker_network
        kwargs["env"]["EG_DOCKER_MODE"] = "swarm"
        return super().launch_process(kernel_cmd, **kwargs)

    def get_initial_states(self) -> set:
        """Return list of states in lowercase indicating container is starting (includes running)."""
        return {"preparing", "starting", "running"}

    def get_error_states(self) -> set:
        """Returns the list of error states indicating container is shutting down or receiving error."""
        return {"failed", "rejected", "complete", "shutdown", "orphaned", "remove"}

    def _get_service(self) -> Service:
        # Fetches the service object corresponding to the kernel with a matching label.
        service = None
        services = client.services.list(
            filters={"label": f"kernel_id={self.kernel_id}"}
        )
        num_services = len(services)
        if num_services == 1:
            service = services[0]
            self.container_name = service.name
        elif num_services > 1:
            msg = f"{self.__class__.__name__}: Found more than one service ({num_services}) for kernel_id '{self.kernel_id}'!"
            raise RuntimeError(msg)
        return service

    def _get_task(self) -> dict:
        # Fetches the task object corresponding to the service associated with the kernel.  We only ask for the
        # current task with desired-state == running.  This eliminates failed states.

        task = None
        if service := self._get_service():
            tasks = service.tasks(filters={"desired-state": "running"})
            num_tasks = len(tasks)
            if num_tasks == 1:
                task = tasks[0]
            elif num_tasks > 1:
                msg = f"{self.__class__.__name__}: Found more than one task ({num_tasks}) for service '{service.name}', kernel_id '{self.kernel_id}'!"
                raise RuntimeError(msg)
        return task

    def get_container_status(self, iteration: int | None) -> str:
        """Return current container state."""
        # Locates the kernel container using the kernel_id filter.  If the status indicates an initial state we
        # should be able to get at the NetworksAttachments and determine the associated container's IP address.
        task_state = ""
        task_id = None
        if task := self._get_task():
            task_id = task["ID"]
            if task_status := task["Status"]:
                task_state = task_status["State"].lower()
                if (
                    not self.assigned_host and task_state == "running"
                ):  # in self.get_initial_states()
                    # get the NetworkAttachments and pick out the first of the Network and first
                    networks_attachments = task["NetworksAttachments"]
                    if len(networks_attachments) > 0:
                        address = networks_attachments[0]["Addresses"][0]
                        ip = address.split("/")[0]
                        self.assigned_ip = ip
                        self.assigned_host = self.container_name

        if iteration:  # only log if iteration is not None (otherwise poll() is too noisy)
            self.log.debug(
                f"{iteration}: Waiting to connect to docker container. Name: '{self.container_name}', Status: '{task_state}', IPAddress: '{self.assigned_ip}', KernelID: '{self.kernel_id}', TaskID: '{task_id}'"
            )
        return task_state

    def terminate_container_resources(self) -> bool | None:
        """Terminate any artifacts created on behalf of the container's lifetime."""
        # Remove the docker service.

        result = True  # We'll be optimistic
        if service := self._get_service():
            try:
                service.remove()  # Service still exists, attempt removal
            except Exception as err:
                self.log.debug(
                    f"{self.__class__.__name__} Termination of service: {service.name} raised exception: {err}"
                )
                if not isinstance(err, NotFound):
                    result = False
                    self.log.warning(f"Error occurred removing service: {err}")
        if result:
            self.log.debug(
                f"{self.__class__.__name__}.terminate_container_resources, service {self.container_name}, kernel ID: {self.kernel_id} has been terminated."
            )
            self.container_name = None
            result = None  # maintain jupyter contract
        else:
            self.log.warning(
                f"{self.__class__.__name__}.terminate_container_resources, container {self.container_name}, kernel ID: {self.kernel_id} has not been terminated."
            )
        return result


class DockerProcessProxy(ContainerProcessProxy):
    """Kernel lifecycle management for Docker kernels (non-Swarm)."""

    def __init__(self, kernel_manager: RemoteKernelManager, proxy_config: dict):
        """Initialize the proxy."""
        super().__init__(kernel_manager, proxy_config)

    def launch_process(
        self, kernel_cmd: str, **kwargs: dict[str, Any] | None
    ) -> DockerProcessProxy:
        """Launches the specified process within a Docker environment."""
        # Convey the network to the docker launch script
        kwargs["env"]["EG_DOCKER_NETWORK"] = docker_network
        kwargs["env"]["EG_DOCKER_MODE"] = "docker"
        return super().launch_process(kernel_cmd, **kwargs)

    def get_initial_states(self) -> set:
        """Return list of states in lowercase indicating container is starting (includes running)."""
        return {"created", "running"}

    def get_error_states(self) -> set:
        """Returns the list of error states indicating container is shutting down or receiving error."""
        return {"restarting", "removing", "paused", "exited", "dead"}

    def _get_container(self) -> Container:
        # Fetches the container object corresponding the the kernel_id label.
        # Only used when docker mode == regular (not swarm)

        container = None
        containers = client.containers.list(
            filters={"label": f"kernel_id={self.kernel_id}"}
        )
        num_containers = len(containers)
        if num_containers == 1:
            container = containers[0]
        elif num_containers > 1:
            msg = f"{self.__class__.__name__}: Found more than one container ({num_containers}) for kernel_id '{self.kernel_id}'!"
            raise RuntimeError(msg)
        return container

    def get_container_status(self, iteration: int | None) -> str:
        """Return current container state."""
        # Locates the kernel container using the kernel_id filter.  If the phase indicates Running, the pod's IP
        # is used for the assigned_ip.  Only used when docker mode == regular (non swarm)
        container_status = ""

        if container := self._get_container():
            self.container_name = container.name
            if container.status:
                container_status = container.status.lower()
                if container_status == "running" and not self.assigned_host:
                    # Container is running, capture IP

                    # we'll use this as a fallback in case we don't find our network
                    self.assigned_ip = container.attrs.get("NetworkSettings").get("IPAddress")
                    networks = container.attrs.get("NetworkSettings").get("Networks")
                    if len(networks) > 0:
                        self.assigned_ip = networks.get(docker_network).get("IPAddress")
                        self.log.debug(
                            f"Using assigned_ip {self.assigned_ip} from docker network '{docker_network}'."
                        )
                    else:
                        self.log.warning(
                            f"Docker network '{docker_network}' could not be located in container attributes - using assigned_ip '{self.assigned_ip}'."
                        )

                    self.assigned_host = self.container_name

        if iteration:  # only log if iteration is not None (otherwise poll() is too noisy)
            self.log.debug(
                f"{iteration}: Waiting to connect to docker container. Name: '{self.container_name}', Status: '{container_status}', IPAddress: '{self.assigned_ip}', KernelID: '{self.kernel_id}'"
            )

        return container_status

    def terminate_container_resources(self) -> bool | None:
        """Terminate any artifacts created on behalf of the container's lifetime."""
        # Remove the container

        result = True  # Since we run containers with remove=True, we'll be optimistic
        if container := self._get_container():
            try:
                container.remove(force=True)  # Container still exists, attempt forced removal
            except Exception as err:
                self.log.debug(
                    f"Container termination for container: {container.name} raised exception: {err}"
                )
                if not isinstance(err, NotFound):
                    result = False
                    self.log.warning(f"Error occurred removing container: {err}")

        if result:
            self.log.debug(
                f"{self.__class__.__name__}.terminate_container_resources, container {self.container_name}, kernel ID: {self.kernel_id} has been terminated."
            )
            self.container_name = None
            result = None  # maintain jupyter contract
        else:
            self.log.warning(
                f"{self.__class__.__name__}.terminate_container_resources, container {self.container_name}, kernel ID: {self.kernel_id} has not been terminated."
            )
        return result
