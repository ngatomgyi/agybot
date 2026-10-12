"""Caveman skill and rule manager for ultra-compressed, token-efficient communication.

Responsibilities:
  - Verify and install/update ~/.gemini/config/skills/caveman/SKILL.md
  - Manage ~/.gemini/config/rules/caveman.md for default session-wide compression
  - Provide helper functions for checking and toggling Caveman mode
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

GEMINI_CONFIG_DIR = Path.home() / ".gemini" / "config"
CAVEMAN_SKILL_DIR = GEMINI_CONFIG_DIR / "skills" / "caveman"
CAVEMAN_SKILL_FILE = CAVEMAN_SKILL_DIR / "SKILL.md"
CAVEMAN_RULES_DIR = GEMINI_CONFIG_DIR / "rules"
CAVEMAN_RULE_FILE = CAVEMAN_RULES_DIR / "caveman.md"

# Global GEMINI.md files that Antigravity CLI discovers as always-on agent rules
GEMINI_ROOT_DIR = Path.home() / ".gemini"
GLOBAL_GEMINI_FILE = GEMINI_ROOT_DIR / "GEMINI.md"
CONFIG_GEMINI_FILE = GEMINI_CONFIG_DIR / "GEMINI.md"
WORKSPACE_PARENT_GEMINI_FILE = Path.home() / "GEMINI.md"

# Bundled fallback resources located in repo
RESOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "resources"
BUNDLED_SKILL = RESOURCES_DIR / "skills" / "caveman" / "SKILL.md"
BUNDLED_RULE = RESOURCES_DIR / "rules" / "caveman.md"


def get_bundled_skill_content() -> str:
    """Return bundled caveman SKILL.md content."""
    if BUNDLED_SKILL.is_file():
        return BUNDLED_SKILL.read_text(encoding="utf-8")
    return ""


def get_bundled_rule_content() -> str:
    """Return bundled caveman rule content."""
    if BUNDLED_RULE.is_file():
        return BUNDLED_RULE.read_text(encoding="utf-8")
    return ""


def is_caveman_skill_installed() -> bool:
    """Check if caveman skill is installed in Antigravity config."""
    return CAVEMAN_SKILL_FILE.is_file() and CAVEMAN_SKILL_FILE.stat().st_size > 0


def is_caveman_rule_active() -> bool:
    """Check if caveman global rule is active in Antigravity rules or GEMINI.md."""
    return (
        (CAVEMAN_RULE_FILE.is_file() and CAVEMAN_RULE_FILE.stat().st_size > 0)
        or (GLOBAL_GEMINI_FILE.is_file() and GLOBAL_GEMINI_FILE.stat().st_size > 0)
    )


def install_or_update_skill() -> bool:
    """Install or update the caveman skill in ~/.gemini/config/skills/caveman/SKILL.md."""
    try:
        content = get_bundled_skill_content()
        if not content:
            logger.warning("Bundled caveman skill content not found.")
            return False
        CAVEMAN_SKILL_DIR.mkdir(parents=True, exist_ok=True)
        CAVEMAN_SKILL_FILE.write_text(content, encoding="utf-8")
        logger.info("Installed/updated caveman skill at %s", CAVEMAN_SKILL_FILE)
        return True
    except Exception as e:
        logger.error("Failed to install caveman skill: %s", e)
        return False


def set_caveman_rule(enabled: bool) -> bool:
    """Enable or disable the global caveman rule across Antigravity rules and GEMINI.md files."""
    try:
        if enabled:
            content = get_bundled_rule_content()
            if not content:
                logger.warning("Bundled caveman rule content not found.")
                return False
            CAVEMAN_RULES_DIR.mkdir(parents=True, exist_ok=True)
            CAVEMAN_RULE_FILE.write_text(content, encoding="utf-8")

            # Also write to GEMINI.md locations so Antigravity CLI automatically loads it as an always-on rule
            for g_file in (
                GLOBAL_GEMINI_FILE,
                CONFIG_GEMINI_FILE,
                WORKSPACE_PARENT_GEMINI_FILE,
            ):
                try:
                    g_file.parent.mkdir(parents=True, exist_ok=True)
                    g_file.write_text(content, encoding="utf-8")
                except Exception as ex:
                    logger.debug("Could not write to %s: %s", g_file, ex)

            logger.info(
                "Enabled caveman global rule at %s and GEMINI.md files",
                CAVEMAN_RULE_FILE,
            )
            return True
        else:
            if CAVEMAN_RULE_FILE.is_file():
                CAVEMAN_RULE_FILE.unlink()
            for g_file in (
                GLOBAL_GEMINI_FILE,
                CONFIG_GEMINI_FILE,
                WORKSPACE_PARENT_GEMINI_FILE,
            ):
                if g_file.is_file():
                    try:
                        g_file.unlink()
                    except Exception as ex:
                        logger.debug("Could not remove %s: %s", g_file, ex)
            logger.info("Disabled caveman global rule and removed GEMINI.md files")
            return True
    except Exception as e:
        logger.error("Failed to set caveman rule (enabled=%s): %s", enabled, e)
        return False


def sync_caveman_configuration(mode: str) -> dict[str, bool]:
    """Sync caveman skill and rule according to mode ('full', 'lite', 'ultra', or 'off')."""
    skill_ok = install_or_update_skill()
    rule_enabled = mode.lower() in ("full", "lite", "ultra", "on", "yes", "true")
    rule_ok = set_caveman_rule(rule_enabled)
    return {
        "skill_installed": is_caveman_skill_installed(),
        "rule_active": is_caveman_rule_active(),
        "success": skill_ok and rule_ok,
    }
