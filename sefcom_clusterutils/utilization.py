from __future__ import annotations

import argparse
import csv
import sys

from kubernetes import client, config
from tabulate import tabulate

from sefcom_clusterutils import print_version


def parse_cpu(size_str: str, fallback_unit: str | None) -> float:
    """Emits in nanocores."""
    if not size_str:
        return 0.0

    size_str = size_str.strip()
    size = float("".join(filter(str.isdigit, size_str)))

    if size == 0:
        result = 0.0
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


def get_summary_pod_resources():
    config.load_kube_config()
    v1 = client.CoreV1Api()

    resources = {}
    pods = v1.list_pod_for_all_namespaces(watch=False)

    for i in pods.items:
        namespace = i.metadata.namespace
        if not i.spec.containers or i.status.phase != "Running":
            continue
        for container in i.spec.containers:
            if not container.resources:
                continue
            resource_dict = resources.get(
                namespace,
                {"cpu_request": 0, "cpu_limit": 0, "mem_request": 0, "mem_limit": 0},
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

            resources[namespace] = resource_dict

    return resources


def get_pod_metrics():
    config.load_kube_config()
    api = client.CustomObjectsApi()

    metrics = api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")

    all_metrics = {}

    for item in metrics["items"]:
        ns_name = item["metadata"]["namespace"]
        item["metadata"]["name"]

        ns_metrics = all_metrics.get(ns_name, {"cpu_usage": 0, "mem_usage": 0})

        for container in item["containers"]:
            ns_metrics["cpu_usage"] += parse_cpu(
                container["usage"]["cpu"],
                fallback_unit="n",
            )
            ns_metrics["mem_usage"] += parse_memory(container["usage"]["memory"])

        all_metrics[ns_name] = ns_metrics

    return all_metrics


def get_cluster_capacity():
    config.load_kube_config()
    v1 = client.CoreV1Api()

    nodes = v1.list_node()
    total_capacity = {"cpu": 0, "memory": 0}

    for node in nodes.items:
        total_capacity["cpu"] += parse_cpu(
            node.status.capacity.get("cpu", "0"),
            fallback_unit="unit",
        )
        total_capacity["memory"] += parse_memory(
            node.status.capacity.get("memory", "0"),
            fallback_unit="b",
        )

    return total_capacity


def format_cpu(cpu):
    """Format CPU resource value."""
    mcpu_divide_threshold = 1000

    cpu = cpu / (10**6)

    if cpu >= mcpu_divide_threshold:
        return f"{cpu / 1000:.2f} CPU"

    return f"{cpu:.2f} mCPU"


def format_mem(num_bytes: int) -> str:
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

    return result


def build_table(resources, metrics, total_capacity):
    table = []

    for namespace, resource_dict in resources.items():
        metric_dict = metrics.get(namespace, {"cpu_usage": 0, "mem_usage": 0})

        row = [
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
        ]
        table.append(row)

    return table


def sort_table(table, sort_key):
    if sort_key is None:
        return table  # No sorting needed

    # Convert args.sort_by into an index
    sort_index_map = {
        "name": 0,
        "n": 0,
        "cpu-request": 1,
        "cr": 1,
        "cpu-limit": 3,
        "cl": 3,
        "cpu-usage": 5,
        "cu": 5,
        "mem-request": 7,
        "mr": 7,
        "mem-limit": 9,
        "ml": 9,
        "mem-usage": 11,
        "mu": 11,
    }
    sort_by_index = sort_index_map.get(sort_key)

    return sorted(table, key=lambda t: t[sort_by_index], reverse=True)


def add_total_row(table):
    row = [
        "Total Used",
        *[sum([row[column] for row in table]) for column in range(1, len(table[0]))],
    ]
    table.append(row)
    return table


def add_capacity_row(table, total_capacity):
    row = [
        "Capacity",
        total_capacity["cpu"],
        100.0,
        total_capacity["cpu"],
        100.0,
        total_capacity["cpu"],
        100.0,
        total_capacity["memory"],
        100.0,
        total_capacity["memory"],
        100.0,
        total_capacity["memory"],
        100.0,
    ]
    table.append(row)
    return table


def make_pretty(table):
    for row in table:
        row[1] = format_cpu(row[1])
        row[3] = format_cpu(row[3])
        row[5] = format_cpu(row[5])
        row[7] = format_mem(row[7])
        row[9] = format_mem(row[9])
        row[11] = format_mem(row[11])

    return table


def print_table(table):
    table = make_pretty(table)

    headers = [
        "Namespace",
        "CPU Request",
        "%",
        "CPU Limit",
        "%",
        "CPU Usage",
        "%",
        "Mem Request",
        "%",
        "Mem Limit",
        "%",
        "Mem Usage",
        "%",
    ]
    colalign = [
        "left",
        "left",
        "decimal",
        "left",
        "decimal",
        "left",
        "decimal",
        "left",
        "decimal",
        "left",
        "decimal",
        "left",
        "decimal",
    ]

    print(
        tabulate(
            table,
            headers=headers,
            tablefmt="plain",
            numalign="left",
            stralign="left",
            floatfmt=".2f",
            colalign=colalign,
        ),
    )


def print_csv(table):
    writer = csv.writer(sys.stdout)
    writer.writerows(table)


def parse_args():
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
            "n",
            "cr",
            "cl",
            "cu",
            "mr",
            "ml",
            "mu",
        ],
        help="sort by specifid field",
    )
    return parser.parse_args()


def main():
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
