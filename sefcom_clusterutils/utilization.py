from __future__ import annotations

import argparse
import csv
import sys
from typing import NamedTuple

from kubernetes import client, config
from rich import box
from rich.color import Color, ColorType, blend_rgb, parse_rgb_hex
from rich.console import Console
from rich.style import Style
from rich.table import Column, Table
from rich.text import Text

from sefcom_clusterutils import print_version


class TableRow(NamedTuple):
    namespace: str
    cpu_request: float
    cpu_request_percent: float
    cpu_limit: float
    cpu_limit_percent: float
    cpu_usage: float
    cpu_usage_percent: float
    mem_request: int
    mem_request_percent: float
    mem_limit: int
    mem_limit_percent: float
    mem_usage: int
    mem_usage_percent: float
    storage_request: int
    storage_request_percent: float
    storage_limit: int
    storage_limit_percent: float


def parse_cpu(size_str: str, fallback_unit: str | None) -> int:
    """Emits in nanocores."""
    if not size_str:
        return 0

    size_str = size_str.strip()
    size = int("".join(filter(str.isdigit, size_str)))

    if size == 0:
        result = 0
    elif size_str.endswith("m"):
        result = size * 10**6
    elif size_str.endswith("u"):
        result = size * 10**3
    elif size_str.endswith("n"):
        result = size
    elif fallback_unit == "m":
        result = size * 10**6
    elif fallback_unit == "u":
        result = size * 10**3
    elif fallback_unit == "n":
        result = size
    elif fallback_unit == "unit":
        result = size * 10**9
    else:
        msg = f"CPU unit unknown for string {size_str}"
        raise Exception(msg)

    return result


def parse_memory(size_str: str, fallback_unit: str | None = None) -> int:
    if not size_str:
        return 0

    size_str = size_str.strip()
    size = float("".join(filter(str.isdigit, size_str)))

    if size_str.endswith(("G", "Gi", "g", "gi")):
        result = int(size * pow(1024, 3))
    elif size_str.endswith(("M", "Mi", "m", "mi")):
        result = int(size * pow(1024, 2))
    elif size_str.endswith(("K", "Ki", "k", "ki")):
        result = int(size * 1024)
    elif fallback_unit == "g":
        result = int(size * pow(1024, 3))
    elif fallback_unit == "m":
        result = int(size * pow(1024, 2))
    elif fallback_unit == "k":
        result = int(size * 1024)
    elif fallback_unit == "b":
        result = int(size)
    else:
        msg = f"Memory unit unknown for string {size_str}"
        raise Exception(msg)

    return result


def get_summary_pod_resources() -> dict[str, dict[str, int]]:
    config.load_kube_config()
    v1 = client.CoreV1Api()

    resources: dict[str, dict[str, int]] = {}
    pods = v1.list_pod_for_all_namespaces(
        field_selector="status.phase=Running",
        watch=False,
    )

    for i in pods.items:
        namespace = i.metadata.namespace
        if not i.spec.containers:
            continue
        for container in i.spec.containers:
            if not container.resources:
                continue
            resource_dict = resources.get(
                namespace,
                {
                    "cpu_request": 0,
                    "cpu_limit": 0,
                    "mem_request": 0,
                    "mem_limit": 0,
                    "storage_request": 0,
                    "storage_limit": 0,
                },
            )

            cpu_request = (
                container.resources.requests.get("cpu")
                if container.resources.requests
                else None
            )
            cpu_limit = (
                container.resources.limits.get("cpu")
                if container.resources.limits
                else None
            )
            mem_request = (
                container.resources.requests.get("memory")
                if container.resources.requests
                else None
            )
            mem_limit = (
                container.resources.limits.get("memory")
                if container.resources.limits
                else None
            )
            storage_request = (
                container.resources.requests.get("ephemeral-storage")
                if container.resources.requests
                else None
            )
            storage_limit = (
                container.resources.limits.get("ephemeral-storage")
                if container.resources.limits
                else None
            )

            resource_dict["cpu_request"] += (
                parse_cpu(cpu_request, fallback_unit="unit") if cpu_request else 0
            )
            resource_dict["cpu_limit"] += (
                parse_cpu(cpu_limit, fallback_unit="unit") if cpu_limit else 0
            )
            resource_dict["mem_request"] += (
                parse_memory(mem_request) if mem_request else 0
            )
            resource_dict["mem_limit"] += parse_memory(mem_limit) if mem_limit else 0
            resource_dict["storage_request"] += (
                parse_memory(storage_request) if storage_request else 0
            )
            resource_dict["storage_limit"] += (
                parse_memory(storage_limit) if storage_limit else 0
            )

            resources[namespace] = resource_dict

    return resources


def get_pod_metrics() -> dict[str, dict[str, int]]:
    config.load_kube_config()
    api = client.CustomObjectsApi()

    metrics = api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")

    all_metrics: dict[str, dict[str, int]] = {}

    for item in metrics["items"]:
        ns_name = item["metadata"]["namespace"]

        ns_metrics = all_metrics.get(
            ns_name,
            {"cpu_usage": 0, "mem_usage": 0},
        )

        for container in item["containers"]:
            ns_metrics["cpu_usage"] += parse_cpu(
                container["usage"]["cpu"],
                fallback_unit="n",
            )
            ns_metrics["mem_usage"] += parse_memory(container["usage"]["memory"])

        all_metrics[ns_name] = ns_metrics

    return all_metrics


def get_cluster_capacity() -> dict[str, int]:
    config.load_kube_config()
    v1 = client.CoreV1Api()

    nodes = v1.list_node()
    total_capacity = {"cpu": 0, "memory": 0, "storage": 0}

    for node in nodes.items:
        total_capacity["cpu"] += parse_cpu(
            node.status.capacity.get("cpu", "0"),
            fallback_unit="unit",
        )
        total_capacity["memory"] += parse_memory(
            node.status.capacity.get("memory", "0"),
            fallback_unit="b",
        )
        total_capacity["storage"] += parse_memory(
            node.status.capacity.get("ephemeral-storage", "0"),
            fallback_unit="b",
        )

    return total_capacity


def format_cpu(cpu: float) -> Text:
    """Format CPU resource value."""
    mcpu_divide_threshold = 1000

    cpu = cpu / (10**6)

    if cpu >= mcpu_divide_threshold:
        return Text(f"{cpu / 1000:.2f} CPU")

    return Text(f"{cpu:.2f} mCPU")


def format_mem(num_bytes: int) -> Text:
    """Takes in bytes, returns in formatted unit."""
    bytes_base = 1024

    if num_bytes < bytes_base:  # Bytes
        result = f"{num_bytes:.2f} B"
    elif num_bytes < bytes_base * 2:  # KB
        result = f"{num_bytes / bytes_base:.2f} KB"
    elif num_bytes < bytes_base * 3:  # MB
        result = f"{num_bytes / (bytes_base*2):.2f} MB"
    elif num_bytes < bytes_base * 4:  # GB
        result = f"{num_bytes / (1024**3):.2f} GB"
    else:  # TB
        result = f"{num_bytes / (1024**4):.2f} TB"

    return Text(result)


def build_table(
    resources: dict[str, dict[str, int]],
    metrics: dict[str, dict[str, int]],
    total_capacity: dict[str, int],
) -> list[TableRow]:
    table: list[TableRow] = []

    for namespace, resource_dict in resources.items():
        metric_dict = metrics.get(
            namespace,
            {"cpu_usage": 0, "mem_usage": 0},
        )

        row = TableRow(
            namespace,
            resource_dict["cpu_request"],
            resource_dict["cpu_request"] / total_capacity["cpu"] * 100,
            resource_dict["cpu_limit"],
            resource_dict["cpu_limit"] / total_capacity["cpu"] * 100,
            metric_dict["cpu_usage"],
            metric_dict["cpu_usage"] / total_capacity["cpu"] * 100,
            resource_dict["mem_request"],
            resource_dict["mem_request"] / total_capacity["memory"] * 100,
            resource_dict["mem_limit"],
            resource_dict["mem_limit"] / total_capacity["memory"] * 100,
            metric_dict["mem_usage"],
            metric_dict["mem_usage"] / total_capacity["memory"] * 100,
            resource_dict["storage_request"],
            resource_dict["storage_request"] / total_capacity["storage"] * 100,
            resource_dict["storage_limit"],
            resource_dict["storage_limit"] / total_capacity["storage"] * 100,
        )
        table.append(row)

    return table


def sort_table(table: list[TableRow], sort_key: str) -> list[TableRow]:
    """Sorts the table rows based on the provided sort_key."""
    # Define a mapping from sort_key strings to TableRow attributes
    key_mapping = {
        "name": lambda row: row.namespace,
        "cpu-request": lambda row: row.cpu_request,
        "cpu-limit": lambda row: row.cpu_limit,
        "cpu-usage": lambda row: row.cpu_usage,
        "mem-request": lambda row: row.mem_request,
        "mem-limit": lambda row: row.mem_limit,
        "mem-usage": lambda row: row.mem_usage,
        "storage-request": lambda row: row.storage_request,
        "storage-limit": lambda row: row.storage_limit,
        "n": lambda row: row.namespace,
        "cr": lambda row: row.cpu_request,
        "cl": lambda row: row.cpu_limit,
        "cu": lambda row: row.cpu_usage,
        "mr": lambda row: row.mem_request,
        "ml": lambda row: row.mem_limit,
        "mu": lambda row: row.mem_usage,
        "sr": lambda row: row.storage_request,
        "sl": lambda row: row.storage_limit,
    }

    # Ensure the provided sort_key is valid
    if sort_key not in key_mapping:
        sort_key = "name"

    # Sort the table using the selected key
    return sorted(table, key=key_mapping[sort_key])


def add_total_row(table: list[TableRow]) -> list[TableRow]:
    total_cpu_request = sum(row.cpu_request for row in table)
    total_cpu_limit = sum(row.cpu_limit for row in table)
    total_cpu_usage = sum(row.cpu_usage for row in table)
    total_mem_request = sum(row.mem_request for row in table)
    total_mem_limit = sum(row.mem_limit for row in table)
    total_mem_usage = sum(row.mem_usage for row in table)
    total_storage_request = sum(row.storage_request for row in table)
    total_storage_limit = sum(row.storage_limit for row in table)

    total_row = TableRow(
        namespace="Total Used",
        cpu_request=total_cpu_request,
        cpu_request_percent=sum(row.cpu_request_percent for row in table),
        cpu_limit=total_cpu_limit,
        cpu_limit_percent=sum(row.cpu_limit_percent for row in table),
        cpu_usage=total_cpu_usage,
        cpu_usage_percent=sum(row.cpu_usage_percent for row in table),
        mem_request=total_mem_request,
        mem_request_percent=sum(row.mem_request_percent for row in table),
        mem_limit=total_mem_limit,
        mem_limit_percent=sum(row.mem_limit_percent for row in table),
        mem_usage=total_mem_usage,
        mem_usage_percent=sum(row.mem_usage_percent for row in table),
        storage_request=total_storage_request,
        storage_request_percent=sum(row.storage_request_percent for row in table),
        storage_limit=total_storage_limit,
        storage_limit_percent=sum(row.storage_limit_percent for row in table),
    )

    table.append(total_row)
    return table


def add_capacity_row(
    table: list[TableRow],
    total_capacity: dict[str, int],
) -> list[TableRow]:
    capacity_row = TableRow(
        namespace="Total Capacity",
        cpu_request=total_capacity["cpu"],
        cpu_request_percent=100.0,
        cpu_limit=total_capacity["cpu"],
        cpu_limit_percent=100.0,
        cpu_usage=0,
        cpu_usage_percent=0,
        mem_request=total_capacity["memory"],
        mem_request_percent=100.0,
        mem_limit=total_capacity["memory"],
        mem_limit_percent=100.0,
        mem_usage=0,
        mem_usage_percent=0,
        storage_request=total_capacity["storage"],
        storage_request_percent=100.0,
        storage_limit=total_capacity["storage"],
        storage_limit_percent=100.0,
    )

    table.append(capacity_row)
    return table


def calc_severity(percent: float, colorize: bool) -> Text:
    # Turns percent into green->red gradient
    text = f"{percent:.2f}"
    if not colorize:
        return Text(text)

    triplet = blend_rgb(
        parse_rgb_hex("00ff00"),
        parse_rgb_hex("ff0000"),
        cross_fade=percent / 100,
    )
    color = Color(text, ColorType.TRUECOLOR, triplet=triplet)
    return Text(text, style=Style(color=color))


def print_table(table: list[TableRow]) -> None:
    headers = [
        Column("Namespace"),
        Column("CPU Request"),
        Column("%", justify="right"),
        Column("CPU Limit"),
        Column("%", justify="right"),
        Column("CPU Usage"),
        Column("%", justify="right"),
        Column("Mem Request"),
        Column("%", justify="right"),
        Column("Mem Limit"),
        Column("%", justify="right"),
        Column("Mem Usage"),
        Column("%", justify="right"),
        Column("Storage Req"),
        Column("%", justify="right"),
        Column("Storage Lim"),
        Column("%", justify="right"),
    ]

    rich_table = Table(*headers, box=box.SIMPLE)
    for idx, row in enumerate(table):
        if idx == len(table) - 2:
            rich_table.add_section()

        pretty_row: list[Text] = [
            Text(row.namespace),
            format_cpu(row.cpu_request),
            calc_severity(row.cpu_request_percent, row.namespace != "Total Capacity"),
            format_cpu(row.cpu_limit),
            calc_severity(row.cpu_limit_percent, row.namespace != "Total Capacity"),
            format_cpu(row.cpu_usage),
            calc_severity(row.cpu_usage_percent, row.namespace != "Total Capacity"),
            format_mem(row.mem_request),
            calc_severity(row.mem_request_percent, row.namespace != "Total Capacity"),
            format_mem(row.mem_limit),
            calc_severity(row.mem_limit_percent, row.namespace != "Total Capacity"),
            format_mem(row.mem_usage),
            calc_severity(row.mem_usage_percent, row.namespace != "Total Capacity"),
            format_mem(row.storage_request),
            calc_severity(
                row.storage_request_percent,
                row.namespace != "Total Capacity",
            ),
            format_mem(row.storage_limit),
            calc_severity(row.storage_limit_percent, row.namespace != "Total Capacity"),
        ]
        rich_table.add_row(*pretty_row)

    with Console() as console:
        console.print(rich_table)


def print_csv(table: list[TableRow]) -> None:
    writer = csv.writer(sys.stdout)

    # Print header
    headers = [
        "namespace",
        "cpu_request",
        "cpu_request_percent",
        "cpu_limit",
        "cpu_limit_percent",
        "cpu_usage",
        "cpu_usage_percent",
        "mem_request",
        "mem_request_percent",
        "mem_limit",
        "mem_limit_percent",
        "mem_usage",
        "mem_usage_percent",
        "storage_request",
        "storage_request_percent",
        "storage_limit",
        "storage_limit_percent",
    ]
    writer.writerow(headers)

    # Print each row of the table
    for row in table:
        writer.writerow(
            [
                row.namespace,
                row.cpu_request,
                row.cpu_request_percent,
                row.cpu_limit,
                row.cpu_limit_percent,
                row.cpu_usage,
                row.cpu_usage_percent,
                row.mem_request,
                row.mem_request_percent,
                row.mem_limit,
                row.mem_limit_percent,
                row.mem_usage,
                row.mem_usage_percent,
                row.storage_request,
                row.storage_request_percent,
                row.storage_limit,
                row.storage_limit_percent,
            ],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="show program's version number and exit",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="output data as CSV",
    )
    parser.add_argument(
        "-s",
        "--sort-by",
        choices=[
            "name",
            "cpu-request",
            "cpu-limit",
            "cpu-usage",
            "mem-request",
            "mem-limit",
            "mem-usage",
            "storage-request",
            "storage-limit",
            "n",
            "cr",
            "cl",
            "cu",
            "mr",
            "ml",
            "mu",
            "sr",
            "sl",
        ],
        help="sort by specified field",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.version:
        print_version()
        sys.exit()

    resources = get_summary_pod_resources()
    metrics = get_pod_metrics()
    total_capacity = get_cluster_capacity()
    table = build_table(resources, metrics, total_capacity)
    table = sort_table(table, args.sort_by)
    table = add_total_row(table)
    table = add_capacity_row(table, total_capacity)
    if args.csv:
        print_csv(table)
    else:
        print_table(table)


if __name__ == "__main__":
    main()
