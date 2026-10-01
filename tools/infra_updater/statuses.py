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
Single source of truth for package, candidate and test statuses.

StrEnum members are plain strings, so they are stored in Firestore/JSON and
compared with values read back from the DataStore without conversion.
"""

from enum import StrEnum
from typing import Iterable, Optional


class PackageStatus(StrEnum):
    REGISTERED = "REGISTERED"            # Monitored; no qualification result yet (or unblocked).
    UP_TO_DATE = "UP_TO_DATE"
    UPDATE_FOUND = "UPDATE_FOUND"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    TESTING = "TESTING"
    TEST_FAILED = "TEST_FAILED"
    SNOOZED = "SNOOZED"
    BLOCKED = "BLOCKED"
    UNREACHABLE = "UNREACHABLE"          # Upstream artifact URL failed the liveness check.
    ERROR = "ERROR"
    OBSOLETE = "OBSOLETE"


class CandidateStatus(StrEnum):
    UPDATE_FOUND = "UPDATE_FOUND"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"  # PR open; tests passed or none mapped.
    TESTING = "TESTING"
    TEST_FAILED = "TEST_FAILED"
    MERGED = "MERGED"
    SNOOZED = "SNOOZED"
    BLOCKED = "BLOCKED"


class TestStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"      # Build could not be found / triggered.
    TIMEOUT = "TIMEOUT"


# Manual policy states: never overridden automatically (except when a PR merges).
POLICY_STATUSES = frozenset({CandidateStatus.SNOOZED, CandidateStatus.BLOCKED})

# Candidates that represent a pending update.
ACTIVE_CANDIDATE_STATUSES = frozenset({
    CandidateStatus.UPDATE_FOUND, CandidateStatus.READY_FOR_REVIEW,
    CandidateStatus.TESTING, CandidateStatus.TEST_FAILED,
})

# Candidates that may be (re-)applied, i.e. a PR (re-)created for them.
APPLICABLE_CANDIDATE_STATUSES = frozenset({
    CandidateStatus.UPDATE_FOUND, CandidateStatus.READY_FOR_REVIEW, CandidateStatus.TEST_FAILED,
})

# Candidates whose state is driven by an open PR.
PR_TRACKED_CANDIDATE_STATUSES = frozenset({
    CandidateStatus.READY_FOR_REVIEW, CandidateStatus.TESTING, CandidateStatus.TEST_FAILED,
})

TEST_FAILED_STATUSES = frozenset({TestStatus.FAILURE, TestStatus.ERROR, TestStatus.TIMEOUT})
TEST_TERMINAL_STATUSES = TEST_FAILED_STATUSES | {TestStatus.SUCCESS}

# Most advanced first: a package mirrors its most advanced active candidate.
_CANDIDATE_PRECEDENCE = (
    CandidateStatus.TESTING, CandidateStatus.TEST_FAILED,
    CandidateStatus.READY_FOR_REVIEW, CandidateStatus.UPDATE_FOUND,
)


def derive_package_status(candidate_statuses: Iterable[str]) -> Optional[str]:
    """Package status implied by its candidates' statuses, or None if no candidate is active."""
    present = set(candidate_statuses)
    return next((s for s in _CANDIDATE_PRECEDENCE if s in present), None)
