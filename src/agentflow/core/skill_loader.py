"""Skill loader — reads SKILL.md headers and reference documents from the skills directory."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from agentflow.config import settings

logger = logging.getLogger(__name__)

# Skill names may only contain lowercase letters, numbers, and hyphens.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")
# Reference document names allow letters, numbers, hyphens, underscores, and dots.
_TOPIC_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _is_valid_skill_name(name: str) -> bool:
    return bool(name) and _SKILL_NAME_RE.fullmatch(name) is not None


def _is_valid_topic(name: str) -> bool:
    return bool(name) and _TOPIC_NAME_RE.fullmatch(name) is not None and ".." not in name


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter delimited by '---' lines.

    Returns (metadata_dict, body_without_frontmatter). If no frontmatter is
    present the dict is empty and the full content is returned as body.
    Only simple scalar key: value pairs are supported — sufficient for name
    and description fields.
    """
    if not content.startswith("---"):
        return {}, content
    close = content.find("\n---", 3)
    if close == -1:
        return {}, content
    fm_text = content[3:close].strip()
    body = content[close + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


class SkillLoader:
    def __init__(self, skills_dir: str) -> None:
        self._dir = Path(skills_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _skill_dir(self, name: str) -> Path:
        return self._dir / name

    def _read_skill_md(self, skill_name: str) -> tuple[dict[str, str], str] | None:
        """Read and parse SKILL.md, returning (frontmatter, body) or None if missing."""
        skill_md = self._skill_dir(skill_name) / "SKILL.md"
        if not skill_md.exists():
            return None
        try:
            return _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read SKILL.md for %r: %s", skill_name, exc)
            return None

    def _extract_description_from_body(self, body: str) -> str:
        """Fallback: first non-empty, non-heading line after the # title."""
        past_title = False
        for line in body.splitlines():
            stripped = line.strip()
            if not past_title:
                if stripped.startswith("# "):
                    past_title = True
                continue
            if stripped and not stripped.startswith("#"):
                return stripped
        return "(no description)"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def frontmatter(self, skill_name: str) -> dict[str, str]:
        """Return the parsed YAML frontmatter for a skill, or an empty dict."""
        parsed = self._read_skill_md(skill_name)
        return parsed[0] if parsed else {}

    def description(self, skill_name: str) -> str:
        """Return the skill description.

        Uses the frontmatter 'description' field if present; falls back to
        extracting the first paragraph after the # title.
        """
        parsed = self._read_skill_md(skill_name)
        if parsed is None:
            return "(no description)"
        meta, body = parsed
        return meta.get("description") or self._extract_description_from_body(body)

    def name(self, skill_name: str) -> str:
        """Return the name from frontmatter, falling back to the folder name.

        Logs a warning if the frontmatter name is present but does not match
        the folder name, since they are required to be identical.
        """
        fm_name = self.frontmatter(skill_name).get("name")
        if fm_name and fm_name != skill_name:
            logger.warning(
                "Skill %r: frontmatter name %r does not match folder name",
                skill_name, fm_name,
            )
        return fm_name or skill_name

    def read(self, skill_name: str, topic: str = "general") -> str:
        """Return the content of SKILL.md (topic='general') or a named reference document."""
        if not _is_valid_skill_name(skill_name):
            return f"Invalid skill name {skill_name!r}: must match [a-z0-9-]+"
        if topic != "general" and not _is_valid_topic(topic):
            return f"Invalid topic {topic!r}: must match [a-zA-Z0-9._-]+"

        skill_dir = self._skill_dir(skill_name)
        if not skill_dir.exists():
            available = sorted(d.name for d in self._dir.iterdir() if d.is_dir()) \
                if self._dir.exists() else []
            return f"Skill {skill_name!r} not found. Available skills: {available}"

        if topic == "general":
            path = skill_dir / "SKILL.md"
        else:
            # Accept the name with or without the .md extension.
            path = skill_dir / topic
            if not path.exists():
                path = skill_dir / f"{topic}.md"

        if not path.exists():
            docs = sorted(f.name for f in skill_dir.iterdir() if f.is_file())
            return (
                f"Document {topic!r} not found in skill {skill_name!r}. "
                f"Available documents: {docs}"
            )

        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading {topic!r} from skill {skill_name!r}: {exc}"

    def preamble(self, skill_names: list[str]) -> str:
        """Return the system-prompt section that advertises available skills."""
        if not skill_names:
            return ""
        lines = [
            "",
            "## Available Skills",
            "",
            "Use the `read_skill` tool to load in-depth guidance before tackling tasks that fall",
            "within a skill's domain. Pass `skill` (a name from the list below) and `topic`",
            '("general" for the SKILL.md overview, or a reference document name listed there).',
            "",
        ]
        for skill_name in skill_names:
            desc = self.description(skill_name)
            lines.append(f"- **{skill_name}**: {desc}")
        return "\n".join(lines)

    def full_content(self, skill_names: list[str]) -> str:
        """Return all skill documents embedded for pre-injection into the system prompt.

        Embeds SKILL.md and every topic document for each declared skill so the
        agent can apply guidance directly without calling read_skill.
        """
        if not skill_names:
            return ""
        sections: list[str] = [
            "",
            "## Skill Reference",
            "",
            "The following skill documentation is pre-loaded. Apply it directly — "
            "no need to call read_skill.",
        ]
        for skill_name in skill_names:
            if not _is_valid_skill_name(skill_name):
                continue
            skill_dir = self._skill_dir(skill_name)
            if not skill_dir.exists():
                continue
            sections.append(f"\n### {skill_name}")
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                try:
                    sections.append(skill_md.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Could not read SKILL.md for %r: %s", skill_name, exc)
            for doc_path in sorted(skill_dir.iterdir()):
                if doc_path.name == "SKILL.md" or not doc_path.is_file():
                    continue
                sections.append(f"\n#### {doc_path.stem}\n")
                try:
                    sections.append(doc_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Could not read %r for skill %r: %s", doc_path.name, skill_name, exc)
        return "\n".join(sections)


# Global instance — imported by the tool handler and the agent.
skill_loader = SkillLoader(settings.skills_dir)
