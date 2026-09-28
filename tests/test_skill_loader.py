"""Tests for SkillLoader including the new full_content() method."""
import tempfile
from pathlib import Path


from agentflow.core.skill_loader import SkillLoader


def _make_skill(root: Path, name: str, overview: str, topics: dict[str, str] | None = None) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(overview)
    for topic_name, content in (topics or {}).items():
        (skill_dir / topic_name).write_text(content)


def test_full_content_empty():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(tmp)
        assert loader.full_content([]) == ""


def test_full_content_overview_embedded():
    with tempfile.TemporaryDirectory() as tmp:
        _make_skill(Path(tmp), "my-skill", "# My Skill\nDo things.")
        loader = SkillLoader(tmp)
        content = loader.full_content(["my-skill"])
        assert "# My Skill" in content
        assert "Do things." in content
        assert "my-skill" in content


def test_full_content_includes_topic_docs():
    with tempfile.TemporaryDirectory() as tmp:
        _make_skill(
            Path(tmp), "my-skill", "# Overview",
            topics={"guide.md": "Step-by-step guide content."},
        )
        loader = SkillLoader(tmp)
        content = loader.full_content(["my-skill"])
        assert "Step-by-step guide content." in content
        assert "guide" in content


def test_full_content_skips_missing_skill():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(tmp)
        content = loader.full_content(["nonexistent"])
        assert "nonexistent" not in content or content.strip() in ("", "\n".join([
            "", "## Skill Reference", "",
            "The following skill documentation is pre-loaded. Apply it directly — no need to call read_skill.",
        ]))


def test_full_content_multiple_skills():
    with tempfile.TemporaryDirectory() as tmp:
        _make_skill(Path(tmp), "skill-a", "# Skill A")
        _make_skill(Path(tmp), "skill-b", "# Skill B")
        loader = SkillLoader(tmp)
        content = loader.full_content(["skill-a", "skill-b"])
        assert "# Skill A" in content
        assert "# Skill B" in content


def test_full_content_says_no_read_skill_needed():
    with tempfile.TemporaryDirectory() as tmp:
        _make_skill(Path(tmp), "my-skill", "# Overview")
        loader = SkillLoader(tmp)
        content = loader.full_content(["my-skill"])
        assert "read_skill" in content
        assert "no need to call" in content


def test_full_content_warns_on_invalid_or_missing_skill(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(tmp)
        with caplog.at_level("WARNING"):
            content = loader.full_content(["bad_name", "missing-skill"])
    assert "bad_name" not in content
    assert "not a valid skill name" in caplog.text
    assert "missing-skill" in caplog.text


def test_every_manifest_skill_exists():
    """Each skill a manifest declares must resolve to a skills/ folder — a typo
    (e.g. an underscore) otherwise silently drops the skill from the agent."""
    import yaml

    repo = Path(__file__).resolve().parent.parent
    for manifest_path in sorted((repo / "manifests").glob("*.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text())
        for skill in manifest.get("skills") or []:
            assert (repo / "skills" / skill / "SKILL.md").exists(), (
                f"{manifest_path.name}: skill {skill!r} not found in skills/"
            )
