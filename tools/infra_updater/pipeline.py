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
Cluster Toolkit Infrastructure Update Agent Pipeline (Pure Agentic ADK / Gemini Implementation)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import List, Optional
from pydantic import BaseModel, Field
import yaml

# Google GenAI SDK (Google ADK Foundation)
try:
    from google import genai
    from google.genai import types
    from google.genai import errors
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

# ==============================================================================
# 1. Pydantic Structured Data Contracts (ADK / Gemini Function Calling)
# ==============================================================================

class ComponentDiscovery(BaseModel):
    component: str = Field(description="Name of the infrastructure component (e.g. nvidia-cuda-toolkit, doca-ofed, gce-image)")
    current_version: str = Field(description="Current hardcoded version string in the file")
    upstream_url: str = Field(description="Exact in-repo download URL or repository endpoint")
    file_path: str = Field(description="Relative path of the scanned file")
    matched_line: int = Field(description="Line number where the version or download URL is declared")
    variable_name: str = Field(description="Variable name or configuration key declaring this version")
    context_lines: str = Field(description="Surrounding lines containing the version and any coupled variables")

class CartographerInventory(BaseModel):
    scanned_files: List[str]
    total_discoveries: int
    inventory: List[ComponentDiscovery]

class CandidateUpdate(BaseModel):
    component: str
    file_path: str
    variable_name: str
    current_version: str
    current_url: str
    target_version: str = Field(description="New production-stable GA version")
    download_url: str = Field(description="Verified upstream download URL for the new version")
    release_channel: str = Field(description="Must be production-stable (never beta/rc)")
    update_available: bool

class CandidateUpdateList(BaseModel):
    candidates: List[CandidateUpdate]

class CodeChangeModification(BaseModel):
    file_path: str
    coupled_variables_identified: List[str] = Field(description="Coupled sibling variables that must be updated together")
    replacement_chunks: List[dict] = Field(description="Exact old and new string pairs to replace atomically")
    updated_file_content: str = Field(description="Complete modified content of the file")
    reasoning: str = Field(description="Explanation of why each variable was updated and how coupling was resolved")

# ==============================================================================
# 2. Pure LLM Agent Implementations (Zero Regex Fallbacks)
# ==============================================================================

class CartographerAgent:
    """LLM Agent that semantically scans code using the Repository Scanning Skill."""
    def __init__(self, client: "genai.Client", skill_path: str, model: str = "gemini-3.8-flash"):
        self.client = client
        self.model = model
        with open(skill_path, "r") as f:
            self.skill_instructions = f.read()

    def run(self, target_files: List[str]) -> CartographerInventory:
        print(f"\n[Cartographer Agent] Running LLM ({self.model}) with skill: infra-update-cartographer")
        
        all_discoveries = []
        
        for file_rel in target_files:
            file_abs = os.path.join(REPO_ROOT, file_rel)
            if not os.path.exists(file_abs):
                continue
                
            with open(file_abs, "r") as f:
                content = f.read()

            # Skill defines all instructions and rules in system_instruction.
            # The prompt is strictly the runtime input data payload.
            prompt = f"""Target File: `{file_rel}`

```yaml
{content}
```"""
            print(f"[Cartographer Agent] Invoking Gemini LLM with structured output schema...")
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=self.skill_instructions,
                        response_mime_type="application/json",
                        response_schema=CartographerInventory,
                        temperature=0.0
                    )
                )
            except errors.APIError as e:
                print(f"\n[FATAL] Cartographer LLM API call failed: {e}")
                raise

            parsed: CartographerInventory = response.parsed
            print(f"[Cartographer Agent] LLM Discovered {len(parsed.inventory)} components:")
            for item in parsed.inventory:
                print(f"  - {item.component} (v{item.current_version}) at line {item.matched_line}")
                all_discoveries.extend(parsed.inventory)

        return CartographerInventory(
            scanned_files=target_files,
            total_discoveries=len(all_discoveries),
            inventory=all_discoveries
        )

class SourceAgent:
    """LLM Agent that queries upstream and filters unstable releases using the Qualification Skill."""
    def __init__(self, client: "genai.Client", skill_path: str, model: str = "gemini-3.8-flash"):
        self.client = client
        self.model = model
        with open(skill_path, "r") as f:
            self.skill_instructions = f.read()

    def run(self, inventory: CartographerInventory) -> List[CandidateUpdate]:
        print(f"\n[Source Agent] Running LLM ({self.model}) with skill: infra-update-qualification")
        
        # Skill defines all instructions and rules in system_instruction.
        # The prompt is strictly the runtime input data payload.
        prompt = f"""Discovered Components Inventory:
{inventory.model_dump_json(indent=2)}"""
        
        print(f"[Source Agent] Invoking Gemini LLM with web tools & qualification skill...")
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.skill_instructions,
                    response_mime_type="application/json",
                    response_schema=CandidateUpdateList,
                    tools=[{"google_search": {}}],
                    temperature=0.0
                )
            )
        except errors.APIError as e:
            print(f"\n[FATAL] Source Qualification LLM API call failed: {e}")
            raise

        parsed: CandidateUpdateList = response.parsed
        print(f"[Source Agent] LLM Qualified {len(parsed.candidates)} updates:")
        for cand in parsed.candidates:
            print(f"  - {cand.component}: {cand.current_version} -> {cand.target_version} ({cand.release_channel})")
        return parsed.candidates

class CodeChangerAgent:
    """LLM Agent that reasons about co-dependencies and applies atomic updates."""
    def __init__(self, client: "genai.Client", skill_path: str, model: str = "gemini-3.8-flash"):
        self.client = client
        self.model = model
        with open(skill_path, "r") as f:
            self.skill_instructions = f.read()

    def run(self, candidates: List[CandidateUpdate]) -> str:
        print(f"\n[Code Changer Agent] Running LLM ({self.model}) with skill: infra-update-code-changer")
        
        for cand in candidates:
            file_abs = os.path.join(REPO_ROOT, cand.file_path)
            with open(file_abs, "r") as f:
                content = f.read()

            # Skill defines all instructions and rules in system_instruction.
            # The prompt is strictly the runtime input data payload.
            prompt = f"""Target Qualified Update:
{cand.model_dump_json(indent=2)}

File Content to Update (`{cand.file_path}`):
```yaml
{content}
```"""
            print(f"[Code Changer Agent] Invoking Gemini LLM for co-dependency analysis & atomic update...")
            response = None
            for attempt in range(3):
                try:
                    response = self.client.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=self.skill_instructions,
                            response_mime_type="application/json",
                            response_schema=CodeChangeModification,
                            temperature=0.0
                        )
                    )
                    break
                except errors.APIError as e:
                    if ("503" in str(e) or "UNAVAILABLE" in str(e) or "DECODE_PREEMPTED" in str(e)) and attempt < 2:
                        print(f"[Code Changer Agent] [WARN] Transient Vertex preemption (attempt {attempt+1}/3), retrying in 3s...")
                        time.sleep(3)
                    else:
                        print(f"\n[FATAL] Code Changer LLM API call failed: {e}")
                        raise

            parsed: CodeChangeModification = response.parsed
            print(f"[Code Changer Agent] LLM Co-Dependency Reasoning:\n  {parsed.reasoning}")
            print(f"[Code Changer Agent] Coupled variables updated: {parsed.coupled_variables_identified}")

            # Validate syntax
            yaml.safe_load(parsed.updated_file_content)
            print(f"[Code Changer Agent] YAML syntax check PASSED.")

            # Write updated content
            with open(file_abs, "w") as f:
                f.write(parsed.updated_file_content)

        diff = subprocess.check_output(["git", "diff", candidates[0].file_path], cwd=REPO_ROOT, text=True)
        return diff

class PRTestOrchestrator:
    """Deterministic orchestrator mapping git diff to Cloud Build daily tests."""
    def run(self, changed_files: List[str]) -> List[str]:
        print(f"\n[PR & Test Orchestrator] Mapping test impact for changed files...")
        daily_tests_dir = os.path.join(REPO_ROOT, "tools/cloud-build/daily-tests")
        targeted_tests = set()
        
        for f in changed_files:
            try:
                res = subprocess.check_output(["grep", "-rn", f, daily_tests_dir], text=True)
                for line in res.strip().split("\n"):
                    cfg = line.split(":")[0]
                    rel_cfg = os.path.relpath(cfg, REPO_ROOT)
                    if rel_cfg.endswith(".yaml") or rel_cfg.endswith(".yml"):
                        targeted_tests.add(rel_cfg)
            except subprocess.CalledProcessError:
                pass
                
        test_list = sorted(list(targeted_tests))
        print(f"[PR & Test Orchestrator] Selected targeted daily tests:")
        for t in test_list:
            print(f"  - ./tools/cloud-build/babysit -c {t}")
        return test_list

# ==============================================================================
# 3. Main Pipeline Orchestrator Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Cluster Toolkit Agentic Update Pipeline (Pure LLM)")
    parser.add_argument("--target", default="examples/machine-learning/a3-ultragpu-8g/a3ultra-slurm-blueprint.yaml")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--location", default="global")
    args = parser.parse_args()

    skills_dir = os.path.join(REPO_ROOT, "skills")
    
    if not GENAI_AVAILABLE:
        print("[ERROR] google-genai is not installed. Install with: pip install google-genai")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        client = genai.Client(api_key=api_key)
        print("[INFO] Initialized Google GenAI Client with GEMINI_API_KEY.")
    else:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "hpc-toolkit-dev")
        location = os.environ.get("GOOGLE_CLOUD_REGION", args.location)
        print(f"[INFO] Connecting to Vertex AI (project={project}, location={location})...")
        client = genai.Client(vertexai=True, project=project, location=location)

    cartographer = CartographerAgent(client, os.path.join(skills_dir, "infra-update-cartographer/SKILL.md"), model=args.model)
    source = SourceAgent(client, os.path.join(skills_dir, "infra-update-qualification/SKILL.md"), model=args.model)
    code_changer = CodeChangerAgent(client, os.path.join(skills_dir, "infra-update-code-changer/SKILL.md"), model=args.model)
    orchestrator = PRTestOrchestrator()

    # Step 1: Cartographer LLM Agent
    inventory = cartographer.run([args.target])
    
    # Step 2: Source Qualification LLM Agent
    candidates = source.run(inventory)
    
    # Filter only candidates where an actual update is available and target != current
    active_updates = [c for c in candidates if c.update_available and c.current_version != c.target_version]
    
    if not active_updates:
        print("\nPipeline finished: All discovered components are already at their latest production-stable versions.")
        return

    print(f"\n[Pipeline] Found {len(active_updates)} component(s) requiring code update.")

    # Step 3: Code Changer LLM Agent
    diff = code_changer.run(active_updates)
    
    # Step 4: Test Orchestrator
    tests = orchestrator.run([args.target])

    print("\n======================================================================")
    print("PIPELINE EXECUTION SUMMARY")
    print("======================================================================")
    print(f"Discoveries:       {inventory.total_discoveries}")
    print(f"Updates Qualified: {len(candidates)}")
    print(f"Targeted Tests:    {len(tests)}")
    print("\nGenerated Git Diff:")
    print(diff)

if __name__ == "__main__":
    main()
