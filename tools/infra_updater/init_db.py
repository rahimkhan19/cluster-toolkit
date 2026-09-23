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
Initializes and populates the SQLite state database for the Cluster Toolkit
Automated Dependency Management pipeline.

Fully aligned with Section 2 of the Implementation Guide:
- Table: packages (Canonical Package Registry with status: REGISTERED, UPDATE_FOUND, READY_FOR_REVIEW, UP_TO_DATE, SNOOZED, BLOCKED, OBSOLETE)
- Table: blueprint_instances (Blueprint Locations with coupled variables and signature keywords)
- Table: candidate_updates (Candidate Update Lifecycle: UPDATE_FOUND, TESTING, READY_FOR_REVIEW, MERGED, CANCELLED)
- Table: learned_rules (Persistent Rule Engine with multi-blueprint scope, action, source, and expiration)
- Table: benchmark_cases (Dynamic Test Data for upfront rule enforcement and semantic changelog triage)
"""

import json
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "updater_state.db")

def init_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[INFO] Cleared existing database at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Table: packages (Canonical Package Registry - Section 2.1)
    # Long-term policy state: REGISTERED, SNOOZED, BLOCKED, OBSOLETE
    cursor.execute("""
    CREATE TABLE packages (
        package_id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        current_version VARCHAR(64) NOT NULL,
        upstream_version VARCHAR(64) NULL,
        source_url TEXT NOT NULL,
        upstream_type VARCHAR(32) NOT NULL DEFAULT 'github_release',
        version_pattern TEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'REGISTERED',
        qualification_summary TEXT NULL,
        snooze_until TIMESTAMP NULL,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Table: blueprint_instances (Blueprint Locations - Section 2.2)
    # instance_id format: <blueprint-slug>-<package-slug>
    cursor.execute("""
    CREATE TABLE blueprint_instances (
        instance_id VARCHAR(64) PRIMARY KEY,
        package_id VARCHAR(64) NOT NULL REFERENCES packages(package_id),
        blueprint_path VARCHAR(255) NOT NULL,
        variable_name VARCHAR(64) NOT NULL,
        coupled_vars JSON NOT NULL DEFAULT '[]',
        signature_keywords JSON NOT NULL DEFAULT '[]'
    );
    """)

    # 3. Table: candidate_updates (Candidate Update Lifecycle - Section 2.3 & 3.1)
    # Workflow states: UPDATE_FOUND, TESTING, READY_FOR_REVIEW, TEST_FAILED, MERGED, SUPERSEDED, CANCELLED
    cursor.execute("""
    CREATE TABLE candidate_updates (
        candidate_id VARCHAR(36) PRIMARY KEY,
        package_id VARCHAR(64) NOT NULL REFERENCES packages(package_id),
        version VARCHAR(64) NOT NULL,
        download_url TEXT NOT NULL,
        checksum VARCHAR(128) NULL,
        pr_url TEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'UPDATE_FOUND',
        compatibility_verdict VARCHAR(32) NULL,
        changelog_summary TEXT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 4. Table: learned_rules (Persistent Rule Engine with Scope - Section 2.4 & Case 3)
    cursor.execute("""
    CREATE TABLE learned_rules (
        rule_id VARCHAR(36) PRIMARY KEY,
        package_id VARCHAR(64) NOT NULL REFERENCES packages(package_id),
        rule_type VARCHAR(32) NOT NULL,
        version_constraint VARCHAR(64) NOT NULL,
        scope JSON NOT NULL DEFAULT '{"blueprint": "*"}',
        action VARCHAR(32) NOT NULL DEFAULT 'BLOCK',
        reason TEXT NOT NULL,
        source VARCHAR(32) NOT NULL DEFAULT 'CI_FAILURE_LOG',
        expires_at TIMESTAMP NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. Table: benchmark_cases (Dynamic Test Suites)
    cursor.execute("""
    CREATE TABLE benchmark_cases (
        case_id VARCHAR(64) PRIMARY KEY,
        category VARCHAR(32) NOT NULL,
        package_id VARCHAR(64) NOT NULL REFERENCES packages(package_id),
        test_version VARCHAR(64) NOT NULL,
        sample_changelog TEXT NULL,
        expected_verdict VARCHAR(32) NOT NULL,
        description TEXT NOT NULL
    );
    """)

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
            r"https://developer\.download\.nvidia\.com/compute/cuda/[0-9\.]+/local_installers/cuda_([0-9\.]+).*_linux\.run",
            "REGISTERED",
            None
        ),
        (
            "nvidia-cuda-arm64",
            "NVIDIA CUDA Toolkit (ARM64 SBSA)",
            "13.0.0_580.65.06",
            "https://developer.nvidia.com/cuda-downloads",
            "archive_scraper",
            r"https://developer\.download\.nvidia\.com/compute/cuda/[0-9\.]+/local_installers/cuda_([0-9\.]+).*_linux_sbsa\.run",
            "REGISTERED",
            None
        ),
        (
            "gve-dkms",
            "Google Virtual Ethernet Kernel Driver",
            "1.4.3",
            "https://github.com/GoogleCloudPlatform/compute-virtual-ethernet-linux",
            "github_release",
            r".*\.deb$",
            "REGISTERED",
            None
        ),
        (
            "mft",
            "Mellanox Firmware Tools",
            "4.34.0-145",
            "https://network.nvidia.com/products/adapter-software/firmware-tools/",
            "mft_api",
            r"mft-([0-9\.\-]+)-",
            "REGISTERED",
            None
        ),
        (
            "nccl-tcpx",
            "GPUDirect TCPX Network Plugin",
            "v3.1.9",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpx/nccl-tcpx-installer.yaml",
            "raw_manifest",
            r"nccl-plugin-gpudirecttcpx-dev:(v[0-9\.]+)",
            "REGISTERED",
            None
        ),
        (
            "nccl-tcpxo",
            "GPUDirect TCPXO Network Plugin",
            "v1.0.15",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-tcpxo/nccl-tcpxo-installer.yaml",
            "raw_manifest",
            r"nccl-plugin-gpudirecttcpx-dev:(v[0-9\.]+)",
            "REGISTERED",
            None
        ),
        (
            "nccl-plugin",
            "GPUDirect RDMA NCCL Plugin",
            "v1.1.1",
            "https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/gpudirect-rdma/nccl-rdma-installer-a4x.yaml",
            "raw_manifest",
            r"nccl-plugin-gib-arm64:(v[0-9\.]+)",
            "REGISTERED",
            None
        ),
        (
            "slurm-gcp",
            "SchedMD Slurm on GCP Engine",
            "6.13.1",
            "https://github.com/GoogleCloudPlatform/slurm-gcp",
            "github_release",
            None,
            "REGISTERED",
            None
        ),
        (
            "nvidia-dcgm",
            "NVIDIA Data Center GPU Manager",
            "1:4.6.1-1",
            "https://developer.download.nvidia.com/compute/cuda/repos/",
            "apt_repository",
            r"datacenter-gpu-manager-4-[a-z0-9]+[=:]([0-9\.\-:]+)",
            "REGISTERED",
            None
        ),
        (
            "mpi-operator",
            "Kubeflow MPI Operator",
            "v0.8.2",
            "https://github.com/kubeflow/mpi-operator",
            "github_release",
            r"v[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "spack",
            "Spack HPC Package Manager",
            "v0.19.0",
            "https://github.com/spack/spack",
            "github_release",
            r"v[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "openmpi",
            "OpenMPI Library",
            "5.0.8",
            "https://github.com/open-mpi/ompi",
            "github_release",
            r"v?[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "nvidia-dra-driver",
            "NVIDIA GPU DRA Driver for Kubernetes",
            "v25.8.0",
            "https://github.com/kubernetes-sigs/dra-driver-nvidia-gpu",
            "github_release",
            r"v[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "nvidia-cuda-image",
            "NVIDIA CUDA Test Container Image",
            "13.0.0-base-ubuntu24.04",
            "https://hub.docker.com/v2/repositories/nvidia/cuda/tags",
            "docker_hub",
            r"[0-9\.]+-(?:base|runtime)-ubuntu[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "kueue",
            "Kubernetes SIG Kueue Queueing System",
            "0.17.1",
            "https://github.com/kubernetes-sigs/kueue",
            "github_release",
            r"v?[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "cmake",
            "Kitware CMake Build System",
            "3.26.0",
            "https://github.com/Kitware/CMake",
            "github_release",
            r"v?[0-9\.]+",
            "REGISTERED",
            None
        ),
        (
            "miniforge",
            "Miniforge3 Conda Installer",
            "24.7.1-2",
            "https://github.com/conda-forge/miniforge",
            "github_release",
            r"[0-9\.\-]+",
            "REGISTERED",
            None
        ),
    ]

    cursor.executemany("""
    INSERT INTO packages (package_id, name, current_version, source_url, upstream_type, version_pattern, status, snooze_until)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """, packages_data)

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

    cursor.executemany("""
    INSERT INTO blueprint_instances (instance_id, package_id, blueprint_path, variable_name, coupled_vars, signature_keywords)
    VALUES (?, ?, ?, ?, ?, ?);
    """, instances_data)

    # -------------------------------------------------------------------------
    # Seed Learned Rules (Section 2.4 & Case 3 Multi-Blueprint Scoping)
    # -------------------------------------------------------------------------
    rules_data = [
        (
            "rule-cuda-13-1-block",
            "nvidia-cuda-x86",
            "BAD_VERSION",
            "== 13.1.0",
            json.dumps({"blueprint": "*"}),
            "BLOCK",
            "Known compilation failure with GCC 12 on Debian 12",
            "CI_FAILURE_LOG",
            None
        ),
        (
            "rule-gve-1-5-block",
            "gve-dkms",
            "INCOMPATIBILITY",
            ">= 1.5.0, < 1.6.0",
            json.dumps({"blueprint": "examples/machine-learning/build-service-images/a3m/blueprint.yaml"}),
            "BLOCK",
            "Kernel panic on Linux 6.1 LTS during heavy network load",
            "CI_FAILURE_LOG",
            None
        )
    ]

    cursor.executemany("""
    INSERT INTO learned_rules (rule_id, package_id, rule_type, version_constraint, scope, action, reason, source, expires_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, rules_data)

    # -------------------------------------------------------------------------
    # Seed Benchmark Cases (Dynamic Test Data)
    # -------------------------------------------------------------------------
    benchmarks_data = [
        (
            "bench-rule-cuda-fail",
            "RULE_GATE",
            "nvidia-cuda-x86",
            "13.1.0",
            None,
            "BLOCKED",
            "Candidate matching known faulty compiler bug rule"
        ),
        (
            "bench-rule-gve-fail",
            "RULE_GATE",
            "gve-dkms",
            "1.5.0",
            None,
            "BLOCKED",
            "Candidate matching kernel panic incompatibility constraint"
        ),
        (
            "bench-rule-cuda-pass",
            "RULE_GATE",
            "nvidia-cuda-x86",
            "13.3.1_610.43.02",
            None,
            "PASSED",
            "Valid candidate passing all rules"
        ),
        (
            "bench-rule-gve-pass",
            "RULE_GATE",
            "gve-dkms",
            "1.4.11",
            None,
            "PASSED",
            "Valid candidate passing all rules"
        ),
        (
            "bench-triage-cuda-break",
            "CHANGELOG_TRIAGE",
            "nvidia-cuda-x86",
            "13.4.0",
            (
                "NVIDIA CUDA Toolkit 13.4 Release Notes:\n"
                "- Dropped support for Linux Kernel < 5.15 and Debian 11.\n"
                "- Deprecated nvcc command-line flag '--gpu-architecture=compute_35'.\n"
                "- Removed legacy libcuda.so.1 compatibility symlink.\n"
                "- Bug Fixes: Fixed NVLink memory synchronization stall on H100/H200."
            ),
            "INCOMPATIBLE",
            "Simulated release with dropped kernel/OS support & deprecated CLI flags"
        ),
        (
            "bench-triage-gve-compat",
            "CHANGELOG_TRIAGE",
            "gve-dkms",
            "1.4.11",
            (
                "Google Virtual Ethernet Driver Release Notes:\n"
                "- Fix header buffer corruption when using header-split with HW-GRO.\n"
                "- Fix Rx queue stall on buffer allocation failures under memory pressure.\n"
                "- Migrate to standard generic power management.\n"
                "- Verified compatibility with Linux 6.1 LTS and 6.6 LTS on Debian 12 / Rocky 9."
            ),
            "COMPATIBLE",
            "Real upstream release with non-breaking bug fixes & kernel compatibility"
        ),
    ]

    cursor.executemany("""
    INSERT INTO benchmark_cases (case_id, category, package_id, test_version, sample_changelog, expected_verdict, description)
    VALUES (?, ?, ?, ?, ?, ?, ?);
    """, benchmarks_data)

    conn.commit()
    conn.close()
    print("[SUCCESS] SQLite database initialized and seeded successfully.")

def preview_tables():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n" + "=" * 145)
    print("TABLE 1: packages (Canonical Package Registry - Status: REGISTERED, SNOOZED, BLOCKED, OBSOLETE)")
    print("=" * 145)
    cursor.execute("SELECT package_id, current_version, upstream_version, upstream_type, status, qualification_summary FROM packages ORDER BY package_id")
    rows = cursor.fetchall()
    print(f"{'Package ID':<18} | {'Current':<16} | {'Upstream':<16} | {'Source Type':<16} | {'Status':<14} | Upstream Assessment Summary")
    print("-" * 145)
    for r in rows:
        up = r[2] or "-"
        src_type = r[3] or "generic"
        summary = r[5] or "Monitored baseline."
        print(f"{r[0]:<18} | {r[1]:<16} | {up:<16} | {src_type:<16} | {r[4]:<14} | {summary}")
    print("=" * 145)

    print("\n" + "=" * 145)
    print("TABLE 2: blueprint_instances (Tracked Blueprint Locations - Naming: <blueprint>-<package>)")
    print("=" * 145)
    cursor.execute("SELECT instance_id, package_id, variable_name, coupled_vars, signature_keywords, blueprint_path FROM blueprint_instances")
    rows = cursor.fetchall()
    print(f"{'Instance ID':<24} | {'Package ID':<18} | {'Target Variable':<20} | {'Coupled':<10} | {'Keywords':<18} | Blueprint File Path")
    print("-" * 145)
    for r in rows:
        coupled = "None" if r[3] == "[]" else "Coupled"
        keywords = ", ".join(json.loads(r[4]))
        print(f"{r[0]:<24} | {r[1]:<18} | {r[2]:<20} | {coupled:<10} | {keywords:<18} | {r[5]}")

    print("\n" + "=" * 135)
    print("TABLE 3: learned_rules (Persistent Rule Engine with Scope & Source)")
    print("=" * 135)
    cursor.execute("SELECT rule_id, package_id, rule_type, version_constraint, action, source, reason FROM learned_rules")
    rows = cursor.fetchall()
    print(f"{'Rule ID':<22} | {'Package ID':<18} | {'Type':<16} | {'Constraint':<20} | {'Action':<6} | {'Source':<18} | Reason")
    print("-" * 135)
    for r in rows:
        print(f"{r[0]:<22} | {r[1]:<18} | {r[2]:<16} | {r[3]:<20} | {r[4]:<6} | {r[5]:<18} | {r[6]}")

    print("\n" + "=" * 135)
    print("TABLE 4: candidate_updates (Candidate Update Lifecycle: UPDATE_FOUND -> READY_FOR_REVIEW -> MERGED)")
    print("=" * 135)
    cursor.execute("SELECT candidate_id, package_id, version, status, compatibility_verdict, changelog_summary FROM candidate_updates")
    rows = cursor.fetchall()
    if not rows:
        print("Empty (No candidates currently queued).")
    else:
        print(f"{'Candidate ID':<15} | {'Package ID':<18} | {'Target Version':<18} | {'Workflow Status':<18} | {'LLM Verdict':<14} | Summary")
        print("-" * 135)
        for r in rows:
            summary = (r[5][:50] + "...") if r[5] and len(r[5]) > 50 else (r[5] or "N/A")
            verdict = r[4] or "UNKNOWN"
            print(f"{r[0]:<15} | {r[1]:<18} | {r[2]:<18} | {r[3]:<18} | {verdict:<14} | {summary}")
    print("=" * 135)

    conn.close()

if __name__ == "__main__":
    init_database()
    preview_tables()
