# Test Infrastructure Kueue Blueprint

This blueprint provisions a Google Kubernetes Engine (GKE) cluster configured with Kueue for managing test job queues and resource sharing. It is used as the underlying infrastructure for running daily and PR validation tests.

For more details, please refer to this [implementation doc](https://docs.google.com/document/d/1IUjslUpnsvG9FROfVi7WPHx5ARl0fvmtg0yMoWmplOs/edit?usp=sharing&resourcekey=0-CBtjfsqf-oDsG9QExgvgOA).

## Overview

The blueprint sets up:
- A GKE cluster with workload identity enabled.
- A basic node pool for running Kueue and test runners.
- Service accounts with necessary permissions for deploying test infrastructure.
- Dummy device plugins to simulate various hardware resources (like GPUs and Filestores) without actually provisioning them.
- Kueue configuration including local queues, cluster queues, and resource flavors for job scheduling.

## Usage

This cluster is used by integration tests to schedule and run tests.
