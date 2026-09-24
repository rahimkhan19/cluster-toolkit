#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Initializes and populates the canonical state store for the Cluster Toolkit
Automated Dependency Management pipeline.

Fully aligned with Section 2 of the Implementation Guide:
- Entity: packages (Canonical Package Registry with status: REGISTERED, UPDATE_FOUND, READY_FOR_REVIEW, UP_TO_DATE, SNOOZED, BLOCKED, OBSOLETE)
- Entity: blueprint_instances (Blueprint Locations with coupled variables and signature keywords)
- Entity: candidate_updates (Candidate Update Lifecycle: UPDATE_FOUND, TESTING, READY_FOR_REVIEW, MERGED, CANCELLED)
- Entity: learned_rules (Persistent Rule Engine with multi-blueprint scope, action, source, and expiration)
"""

import json
import os
import sys
from typing import Optional, Dict, Any, List

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

JSON_PATH = os.path.join(BASE_DIR, "updater_state.json")

def init_database(preserve_candidates: bool = False):
    # -------------------------------------------------------------------------
    # Seed Canonical Infrastructure Packages (Section 2.1)
    # -------------------------------------------------------------------------
    packages_data = [
        (
            "nvidia-cuda-x86",
            "NVIDIA CUDA Toolkit (x86_64)",
            "13.0.3_580.126.20",
            "https://developer.nvidia.com/cuda-downloads",
            "archive_scraper",
            "REGISTERED",
            None
        ),
        (
            "nvidia-cuda-arm64",
            "NVIDIA CUDA Toolkit (ARM64 SBSA)",
            "13.0.0_580.65.06",
            "https://developer.nvidia.com/cuda-downloads",
            "archive_scraper",
            "REGISTERED",
            None
        ),
        (
            "gve-dkms",
            "Google Virtual Ethernet Kernel Driver",
            "1.4.3",
            "https://github.com/GoogleCloudPlatform/compute-virtual-ethernet-linux",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "mft",
            "Mellanox Firmware Tools",
            "4.34.0-145",
            "https://network.nvidia.com/products/adapter-software/firmware-tools/",
            "mft_api",
            "REGISTERED",
            None
        ),
        (
            "nccl-tcpx",
            "GPUDirect TCPX Network Plugin",
            "v3.1.9",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpx/nccl-tcpx-installer.yaml",
            "raw_manifest",
            "REGISTERED",
            None
        ),
        (
            "nccl-tcpxo",
            "GPUDirect TCPXO Network Plugin",
            "v1.0.15",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpxo/nccl-tcpxo-installer.yaml",
            "raw_manifest",
            "REGISTERED",
            None
        ),
        (
            "nccl-plugin",
            "GPUDirect RDMA NCCL Plugin",
            "v1.1.1",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-rdma/nccl-rdma-installer-a4x.yaml",
            "raw_manifest",
            "REGISTERED",
            None
        ),
        (
            "slurm-gcp",
            "SchedMD Slurm on GCP Engine",
            "6.13.1",
            "https://github.com/GoogleCloudPlatform/slurm-gcp",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "nvidia-dcgm",
            "NVIDIA Data Center GPU Manager",
            "1:4.6.1-1",
            "https://developer.download.nvidia.com/compute/cuda/repos/",
            "apt_repository",
            "REGISTERED",
            None
        ),
        (
            "mpi-operator",
            "Kubeflow MPI Operator",
            "v0.8.2",
            "https://github.com/kubeflow/mpi-operator",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "spack",
            "Spack HPC Package Manager",
            "v0.19.0",
            "https://github.com/spack/spack",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "openmpi",
            "OpenMPI Library",
            "5.0.8",
            "https://github.com/open-mpi/ompi",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "nvidia-dra-driver",
            "NVIDIA GPU DRA Driver for Kubernetes",
            "v25.8.0",
            "https://github.com/kubernetes-sigs/dra-driver-nvidia-gpu",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "nvidia-cuda-image",
            "NVIDIA CUDA Test Container Image",
            "13.0.0-base-ubuntu24.04",
            "https://hub.docker.com/v2/repositories/nvidia/cuda/tags",
            "docker_hub",
            "REGISTERED",
            None
        ),
        (
            "kueue",
            "Kubernetes SIG Kueue Queueing System",
            "0.17.1",
            "https://github.com/kubernetes-sigs/kueue",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "cmake",
            "Kitware CMake Build System",
            "3.26.0",
            "https://github.com/Kitware/CMake",
            "github_release",
            "REGISTERED",
            None
        ),
        (
            "miniforge",
            "Miniforge3 Conda Installer",
            "24.7.1-2",
            "https://github.com/conda-forge/miniforge",
            "github_release",
            "REGISTERED",
            None
        ),
    ]

    # -------------------------------------------------------------------------
    # Seed Blueprint Instances (Section 2.2 naming: <blueprint>-<package>)
    # -------------------------------------------------------------------------
    instances_data = [
        # nvidia-cuda-x86
        (
            "a3ultra-slurm-cuda",
            "nvidia-cuda-x86",
            "examples/machine-learning/a3-ultragpu-8g/a3ultra-slurm-blueprint.yaml",
            "cuda_installer_url",
            json.dumps([{"variable_name": "cuda_installer_file", "pattern": "/tmp/{filename}"}]),
            json.dumps(["cuda", "nvidia"])
        ),
        (
            "a4high-slurm-cuda",
            "nvidia-cuda-x86",
            "examples/machine-learning/a4-highgpu-8g/a4high-slurm-blueprint.yaml",
            "cuda_installer_url",
            json.dumps([{"variable_name": "cuda_installer_file", "pattern": "/tmp/{filename}"}]),
            json.dumps(["cuda", "nvidia"])
        ),

        # nvidia-cuda-arm64
        (
            "a4xhigh-slurm-cuda",
            "nvidia-cuda-arm64",
            "examples/machine-learning/a4x-highgpu-4g/a4xhigh-slurm-blueprint.yaml",
            "cuda_installer_url",
            json.dumps([{"variable_name": "cuda_installer_file", "pattern": "/tmp/{filename}"}]),
            json.dumps(["cuda", "nvidia"])
        ),

        # gve-dkms
        (
            "a3m-image-gve",
            "gve-dkms",
            "examples/machine-learning/build-service-images/a3m/blueprint.yaml",
            "package_url",
            json.dumps([]),
            json.dumps(["gve", "ethernet"])
        ),
        (
            "a3mega-image-gve",
            "gve-dkms",
            "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-gcsfuse-lssd-blueprint.yaml",
            "package_url",
            json.dumps([]),
            json.dumps(["gve", "ethernet"])
        ),

        # mft
        (
            "a4xmax-slurm-mft",
            "mft",
            "examples/machine-learning/a4x-maxgpu-4g-metal/a4xmax-bm-slurm-blueprint.yaml",
            "mft_installer_url",
            json.dumps([]),
            json.dumps(["mft", "mellanox"])
        ),
        (
            "a4x-vm-mft",
            "mft",
            "examples/machine-learning/a4x-highgpu-4g/a4x-vm.yaml",
            "mft_installer_url",
            json.dumps([]),
            json.dumps(["mft", "mellanox"])
        ),

        # slurm-gcp
        (
            "shared-image-slurm",
            "slurm-gcp",
            "examples/machine-learning/build-service-images/shared.yaml",
            "slurm_gcp_version",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "a3high-slurm-gcp",
            "slurm-gcp",
            "examples/machine-learning/a3-highgpu-8g/a3high-slurm-blueprint.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "a3mega-slurm-gcp",
            "slurm-gcp",
            "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-blueprint.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "a3mega-gcsfuse-slurm-gcp",
            "slurm-gcp",
            "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-gcsfuse-lssd-blueprint.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "a4xmax-slurm-gcp",
            "slurm-gcp",
            "examples/machine-learning/a4x-maxgpu-4g-metal/a4xmax-bm-slurm-blueprint.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "ml-slurm-g4-gcp",
            "slurm-gcp",
            "examples/ml-slurm-g4.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),
        (
            "ml-slurm-g4-vgpu-gcp",
            "slurm-gcp",
            "examples/ml-slurm-g4-vgpu.yaml",
            "build_slurm_from_git_ref",
            json.dumps([]),
            json.dumps(["slurm", "schedmd"])
        ),

        # nccl-tcpx
        (
            "gke-a3-tcpx",
            "nccl-tcpx",
            "examples/gke-a3-highgpu/gke-a3-highgpu.yaml",
            "nccl_tcpx_version",
            json.dumps([]),
            json.dumps(["tcpx", "nccl"])
        ),

        # nccl-tcpxo
        (
            "gke-a3mega-tcpxo",
            "nccl-tcpxo",
            "examples/gke-a3-megagpu/gke-a3-megagpu.yaml",
            "nccl_tcpxo_version",
            json.dumps([]),
            json.dumps(["tcpxo", "nccl"])
        ),

        # nccl-plugin
        (
            "a4xmax-slurm-nccl",
            "nccl-plugin",
            "examples/machine-learning/a4x-maxgpu-4g-metal/a4xmax-bm-slurm-blueprint.yaml",
            "nccl_plugin_version",
            json.dumps([]),
            json.dumps(["nccl", "rdma"])
        ),
        (
            "gke-a3ultra-gib",
            "nccl-plugin",
            "examples/gke-a3-ultragpu/gke-a3-ultragpu.yaml",
            "version",
            json.dumps([]),
            json.dumps(["gib", "template_vars"])
        ),
        (
            "gke-a4-gib",
            "nccl-plugin",
            "examples/gke-a4/gke-a4.yaml",
            "version",
            json.dumps([]),
            json.dumps(["gib", "template_vars"])
        ),
        (
            "gke-a4x-gib",
            "nccl-plugin",
            "examples/gke-a4x/gke-a4x.yaml",
            "version",
            json.dumps([]),
            json.dumps(["gib", "template_vars"])
        ),
        (
            "gke-a4xmax-gib",
            "nccl-plugin",
            "examples/gke-a4x-max-bm/gke-a4x-max-bm.yaml",
            "version",
            json.dumps([]),
            json.dumps(["gib", "template_vars"])
        ),

        # nvidia-dcgm
        (
            "a3ultra-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a3-ultragpu-8g/a3ultra-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),
        (
            "a4high-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a4-highgpu-8g/a4high-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),
        (
            "a4xhigh-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a4x-highgpu-4g/a4xhigh-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),
        (
            "a3mega-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),
        (
            "a4xmax-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a4x-maxgpu-4g-metal/a4xmax-bm-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),
        (
            "a3high-slurm-dcgm",
            "nvidia-dcgm",
            "examples/machine-learning/a3-highgpu-8g/a3high-slurm-blueprint.yaml",
            "nvidia_packages",
            json.dumps([]),
            json.dumps(["dcgm", "datacenter-gpu-manager"])
        ),

        # mpi-operator
        (
            "gke-h4d-mpi-operator",
            "mpi-operator",
            "examples/gke-h4d/gke-h4d.yaml",
            "source",
            json.dumps([]),
            json.dumps(["mpi-operator", "kubeflow"])
        ),
        (
            "dws-gke-h4d-mpi-operator",
            "mpi-operator",
            "examples/gke-consumption-options/dws-flex-start-compact-placement/gke-h4d/gke-h4d.yaml",
            "source",
            json.dumps([]),
            json.dumps(["mpi-operator", "kubeflow"])
        ),

        # spack
        (
            "batch-mpi-spack",
            "spack",
            "examples/batch-mpi.yaml",
            "spack_ref",
            json.dumps([]),
            json.dumps(["spack", "mpi"])
        ),

        # openmpi
        (
            "a4x-vm-openmpi",
            "openmpi",
            "examples/machine-learning/a4x-highgpu-4g/a4x-vm.yaml",
            "openmpi_version",
            json.dumps([]),
            json.dumps(["openmpi", "mpi"])
        ),

        # nvidia-dra-driver
        (
            "gke-a4xmax-dra",
            "nvidia-dra-driver",
            "examples/gke-a4x-max-bm/gke-a4x-max-bm.yaml",
            "version",
            json.dumps([]),
            json.dumps(["nvidia_dra_driver"])
        ),

        # nvidia-cuda-image
        (
            "gke-a3ultra-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-a3-ultragpu/gke-a3-ultragpu.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),
        (
            "gke-a4-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-a4/gke-a4.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),
        (
            "gke-a4x-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-a4x/gke-a4x.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),
        (
            "gke-a4xmax-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-a4x-max-bm/gke-a4x-max-bm.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),
        (
            "gke-g4-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-g4/gke-g4.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),
        (
            "gke-g4conf-cuda-image",
            "nvidia-cuda-image",
            "examples/gke-g4-confidential/gke-g4-confidential.yaml",
            "image",
            json.dumps([]),
            json.dumps(["nvidia-smi"])
        ),

        # kueue
        (
            "gke-a3high-kueue",
            "kueue",
            "examples/gke-a3-highgpu/gke-a3-highgpu.yaml",
            "version",
            json.dumps([]),
            json.dumps(["kueue"])
        ),
        (
            "gke-a4x-kueue",
            "kueue",
            "examples/gke-a4x/gke-a4x.yaml",
            "version",
            json.dumps([]),
            json.dumps(["kueue"])
        ),
        (
            "gke-a4xmax-kueue",
            "kueue",
            "examples/gke-a4x-max-bm/gke-a4x-max-bm.yaml",
            "version",
            json.dumps([]),
            json.dumps(["kueue"])
        ),

        # cmake
        (
            "ml-slurm-g4-vgpu-cmake",
            "cmake",
            "examples/ml-slurm-g4-vgpu.yaml",
            "url",
            json.dumps([
                {"variable_name": "dest", "pattern": "/tmp/{filename}"},
                {"variable_name": "cmd", "pattern": "/tmp/{filename} --skip-license"}
            ]),
            json.dumps(["cmake"])
        ),

        # miniforge
        (
            "ml-slurm-miniforge",
            "miniforge",
            "examples/ml-slurm.yaml",
            "miniforge",
            json.dumps([]),
            json.dumps(["miniforge"])
        )
    ]

    # -------------------------------------------------------------------------
    # Seed Learned Rules (Section 2.4 & Case 3 Multi-Blueprint Scoping)
    # -------------------------------------------------------------------------
    rules_data = []

    # Build seed_json for updater_state.json
    pkgs = {}
    for p in packages_data:
        pkg_id = p[0]
        pkgs[pkg_id] = {
            "package_id": pkg_id,
            "name": p[1],
            "current_version": p[2],
            "upstream_version": "-",
            "source_url": p[3],
            "upstream_type": p[4],
            "status": p[5],
            "qualification_summary": "Monitored baseline.",
            "snooze_until": p[6],
            "updated_at": "2026-09-23T06:00:00Z",
            "blueprints": []
        }

    for inst in instances_data:
        pkg_id = inst[1]
        bp_dict = {
            "instance_id": inst[0],
            "blueprint_path": inst[2],
            "variable_name": inst[3],
            "coupled_vars": json.loads(inst[4]) if isinstance(inst[4], str) else inst[4],
            "signature_keywords": json.loads(inst[5]) if isinstance(inst[5], str) else inst[5]
        }
        if pkg_id in pkgs:
            pkgs[pkg_id]["blueprints"].append(bp_dict)

    rules = []
    for r in rules_data:
        rules.append({
            "rule_id": r[0],
            "package_id": r[1],
            "rule_type": r[2],
            "version_constraint": r[3],
            "scope": json.loads(r[4]) if isinstance(r[4], str) else r[4],
            "action": r[5],
            "reason": r[6],
            "source": r[7],
            "expires_at": r[8],
            "created_at": "2026-09-23T06:00:00Z"
        })

    seed_json = {
        "packages": pkgs,
        "candidate_updates": {},
        "learned_rules": rules,
        "audit_runs": []
    }

    # Only preserve active candidates or qualified upstream versions if explicitly requested
    if preserve_candidates and os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                old_json = json.load(f)
                if old_json.get("candidate_updates"):
                    seed_json["candidate_updates"] = old_json["candidate_updates"]
                for pid, old_pkg in old_json.get("packages", {}).items():
                    if pid in seed_json["packages"]:
                        if old_pkg.get("upstream_version") and old_pkg["upstream_version"] != "-":
                            seed_json["packages"][pid]["upstream_version"] = old_pkg["upstream_version"]
                        if old_pkg.get("qualification_summary"):
                            seed_json["packages"][pid]["qualification_summary"] = old_pkg["qualification_summary"]
        except Exception:
            pass

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(seed_json, f, indent=2, ensure_ascii=False)
    print(f"[SUCCESS] JSON state store initialized and seeded at {JSON_PATH}.")


def get_seed_data() -> dict:
    """Returns canonical seed data from JSON_PATH (initializing it if necessary)."""
    if not os.path.exists(JSON_PATH):
        init_database()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def preview_tables():
    from tools.infra_updater.datastore import get_datastore
    store = get_datastore()
    packages = store.list_packages()

    print("\n" + "=" * 145)
    print("STORE 1: packages (Canonical Package Registry - Status: REGISTERED, SNOOZED, BLOCKED, OBSOLETE)")
    print("=" * 145)
    print(f"{'Package ID':<18} | {'Current':<16} | {'Upstream':<16} | {'Source Type':<16} | {'Status':<14} | Upstream Assessment Summary")
    print("-" * 145)
    for p in packages:
        up = p.get("upstream_version") or "-"
        src_type = p.get("upstream_type") or "generic"
        summary = p.get("qualification_summary") or "Monitored baseline."
        print(f"{p['package_id']:<18} | {p['current_version']:<16} | {up:<16} | {src_type:<16} | {p.get('status', '-'):<14} | {summary}")
    print("=" * 145)

    blueprints = store.list_all_blueprints()
    rules = store.list_rules()
    candidates = store.list_candidates()

    print("\n" + "=" * 145)
    print("STORE 2: blueprints (Tracked Blueprint Locations - Naming: <blueprint>-<package>)")
    print("=" * 145)
    print(f"{'Instance ID':<24} | {'Package ID':<18} | {'Target Variable':<20} | {'Coupled':<10} | {'Keywords':<18} | Blueprint File Path")
    print("-" * 145)
    for bp in blueprints:
        c_list = bp.get("coupled_vars", [])
        coupled = "None" if not c_list else "Coupled"
        kw_list = bp.get("signature_keywords", [])
        keywords = ", ".join(kw_list) if isinstance(kw_list, list) else str(kw_list)
        print(f"{bp.get('instance_id', ''):<24} | {bp.get('package_id', ''):<18} | {bp.get('variable_name', ''):<20} | {coupled:<10} | {keywords:<18} | {bp.get('blueprint_path', '')}")

    print("\n" + "=" * 135)
    print("STORE 3: learned_rules (Persistent Rule Engine with Scope & Source)")
    print("=" * 135)
    print(f"{'Rule ID':<22} | {'Package ID':<18} | {'Type':<16} | {'Constraint':<20} | {'Action':<6} | {'Source':<18} | Reason")
    print("-" * 135)
    for r in rules:
        print(f"{r.get('rule_id', ''):<22} | {r.get('package_id', ''):<18} | {r.get('rule_type', ''):<16} | {r.get('version_constraint', ''):<20} | {r.get('action', ''):<6} | {r.get('source', ''):<18} | {r.get('reason', '')}")

    print("\n" + "=" * 135)
    print("STORE 4: candidate_updates (Candidate Update Lifecycle: UPDATE_FOUND -> READY_FOR_REVIEW -> MERGED)")
    print("=" * 135)
    if not candidates:
        print("Empty (No candidates currently queued).")
    else:
        print(f"{'Candidate ID':<15} | {'Package ID':<18} | {'Target Version':<18} | {'Workflow Status':<18} | Summary")
        print("-" * 135)
        for c in candidates:
            s = c.get("summary") or c.get("changelog_summary", "") or "N/A"
            summary = (s[:65] + "...") if len(s) > 65 else s
            print(f"{c.get('candidate_id', ''):<15} | {c.get('package_id', ''):<18} | {c.get('version', ''):<18} | {c.get('status', ''):<18} | {summary}")
    print("=" * 135)

if __name__ == "__main__":
    init_database()
    preview_tables()
