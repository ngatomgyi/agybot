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
    """Check if caveman global rule is active in Antigravity rules."""
    return CAVEMAN_RULE_FILE.is_file() and CAVEMAN_RULE_FILE.stat().st_size > 0


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
    """Enable or disable the global caveman rule in ~/.gemini/config/rules/caveman.md."""
    try:
        if enabled:
            content = get_bundled_rule_content()
            if not content:
                logger.warning("Bundled caveman rule content not found.")
                return False
            CAVEMAN_RULES_DIR.mkdir(parents=True, exist_ok=True)
            CAVEMAN_RULE_FILE.write_text(content, encoding="utf-8")
            logger.info("Enabled caveman global rule at %s", CAVEMAN_RULE_FILE)
            return True
        else:
            if CAVEMAN_RULE_FILE.is_file():
                CAVEMAN_RULE_FILE.unlink()
                logger.info(
                    "Disabled caveman global rule (removed %s)", CAVEMAN_RULE_FILE
                )
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
