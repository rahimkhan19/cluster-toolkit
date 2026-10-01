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

"""Version parsing and comparison helpers shared by qualification, policy and apply."""

import re
from typing import Optional

from packaging.version import InvalidVersion, Version


def parse_semver(v_str: str) -> Optional[Version]:
    """Parses the numeric core of diverse version formats (v-prefix, '1:' epoch, '-flavor', '_build')."""
    if not v_str:
        return None
    clean = re.sub(r'^[vV]', '', v_str.strip())
    clean = re.sub(r'^[0-9]+:', '', clean)
    clean = clean.split('_')[0].split('-')[0]
    try:
        return Version(clean)
    except InvalidVersion:
        return None


def clean_version_str(v_str: str) -> str:
    s = re.sub(r'^[vV]', '', v_str.strip())
    s = re.sub(r'^[0-9]+:', '', s)
    return re.sub(r'-[A-Za-z][\w.-]*$', '', s)  # drop a flavor suffix such as '-base-ubuntu24.04'


def is_version_greater(v1: str, v2: Optional[str]) -> bool:
    """Returns True if v1 is strictly greater than v2 (a missing v2 counts as older)."""
    if not v1:
        return False
    if not v2:
        return True
    c1, c2 = clean_version_str(v1), clean_version_str(v2)
    if c1 == c2:
        return False
    s1, s2 = parse_semver(v1), parse_semver(v2)
    if s1 and s2 and s1 != s2:
        return s1 > s2
    try:
        p1 = Version(re.sub(r'[^0-9.]+', '.', c1).strip('.'))
        p2 = Version(re.sub(r'[^0-9.]+', '.', c2).strip('.'))
        if p1 != p2:
            return p1 > p2
    except InvalidVersion:
        pass
    return c1 > c2


def match_v_prefix(reference: str, value: str) -> str:
    """Adds or strips a leading 'v' on value so it matches the reference's style (v1.2.3 vs 1.2.3)."""
    if not reference or not value:
        return value
    if re.match(r'^[vV][0-9]', reference) and re.match(r'^[0-9]', value):
        return f"v{value}"
    if re.match(r'^[0-9]', reference) and re.match(r'^[vV][0-9]', value):
        return value[1:]
    return value


def oldest_version(versions) -> Optional[str]:
    """The lowest of the given version strings (None if there are none)."""
    oldest = None
    for v in versions:
        if v and (oldest is None or is_version_greater(oldest, v)):
            oldest = v
    return oldest
