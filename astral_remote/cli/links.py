# Centralized social-link registry for Astral Interactive.
# All URLs are configured here; nothing opens arbitrary input.
# Ported from src/cli/social-links.js.

SOCIAL_LINKS = {
    "github": "https://github.com/Astral-Interactive-team/astral-remote",
    "telegram": "https://t.me/astral_int",
    "discord": "https://discord.gg/bSvcQG8K4f",
}

for _key, _url in SOCIAL_LINKS.items():
    if not _url.startswith("https://") and not _url.startswith("http://"):
        raise ValueError(f'social-links: "{_key}" is not a valid URL: {_url}')
