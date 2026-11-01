"""Shared helper for adapter-pilot override tests (IVG-69 Stage B).

Provides ``assert_installer_selects_adapter(skill_name)`` — the single
source-of-truth assertion that ``installer.deploy_skills()`` deploys the
Claude adapter SKILL.md for a given skill.

Design notes
------------
* Uses a SYNTHETIC source tree (no real PKG_DIR copy) so there are
  no ``__QUOIN_HOME__`` tokens in the source file.  installer.py calls
  ``_copy_with_substitution`` unconditionally (line 282), which substitutes
  every ``__QUOIN_HOME__`` token with the absolute dest_root; 16 of 21
  skills carry that token in their real adapter SKILL.md, making byte-equal
  comparison against the deployed file fail for those skills.  The synthetic
  approach sidesteps the substitution entirely — deployed text == adapter
  text is trivially exact.  (Same approach used by the canonical
  ``test_deploy_skills_from_wheel_style_data_prefers_claude_adapter``.)
* Import form: top-level ``import _adapter_pilot_helpers`` (NOT a relative
  import).  The tests directory has no ``__init__.py`` and pytest's default
  prepend-mode adds the test root to sys.path, making sibling imports safe.
* The underscore prefix intentionally prevents pytest auto-collection.

IVG-69 Stage B retarget — D-06 decision.
"""
import pathlib
import tempfile

# PKG_DIR resolution: this file lives at quoin/quoin/dev/tests/
# so parent.parent.parent is quoin/quoin/
_THIS = pathlib.Path(__file__).resolve()
PKG_DIR = _THIS.parent.parent.parent  # quoin/quoin/


def assert_installer_selects_adapter(skill_name: str) -> None:
    """Assert that installer.deploy_skills() deploys the adapter SKILL.md.

    Steps
    -----
    (a) skill_name in installer.CANONICAL_SKILLS
    (b) real adapter SKILL.md exists at PKG_DIR/adapters/claude/skills/<name>/SKILL.md
    (c) synthetic deploy: create a tmp adapter source with placeholder-free content,
        deploy into a tmp dest, assert deployed text == synthetic adapter content.

    Raises AssertionError with a descriptive message on any failure.
    """
    from quoin import installer  # noqa: PLC0415  # imported here to keep top-level import light

    # (a) skill must be in CANONICAL_SKILLS
    assert skill_name in installer.CANONICAL_SKILLS, (
        f"installer.CANONICAL_SKILLS is missing {skill_name!r} — "
        "either the skill was never added or CANONICAL_SKILLS is stale"
    )

    # (b) real adapter SKILL.md must exist
    adapter_path = PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"
    assert adapter_path.is_file(), (
        f"Adapter SKILL.md missing for {skill_name!r}: {adapter_path}"
    )

    # (c) synthetic deploy — placeholder-free content, exact byte equality after deploy
    active_content = (
        f"# {skill_name}\n\n"
        f"Active Claude adapter skill for {skill_name}.\n"
    )

    with tempfile.TemporaryDirectory() as src_d:
        with tempfile.TemporaryDirectory() as dst_d:
            src = pathlib.Path(src_d)
            adapter_dir = src / "adapters" / "claude" / "skills" / skill_name
            adapter_dir.mkdir(parents=True)

            (adapter_dir / "SKILL.md").write_text(active_content, encoding="utf-8")

            dest = pathlib.Path(dst_d) / ".claude"
            installer.deploy_skills(src, dest)

            deployed_path = dest / "skills" / skill_name / "SKILL.md"
            assert deployed_path.exists(), (
                f"deploy_skills() did not create {deployed_path} for skill {skill_name!r}"
            )
            deployed = deployed_path.read_text(encoding="utf-8")
            assert deployed == active_content, (
                f"deploy_skills() did not deploy the adapter content "
                f"for skill {skill_name!r}.\n"
                f"  Expected: {active_content!r}\n"
                f"  Got:      {deployed!r}"
            )
